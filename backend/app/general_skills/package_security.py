"""
@Time       : 2026/08/11 23:45
@Author     : zhanglp8181
@File       : package_security.py
@CallChain  : ImportJob worker → package_security.normalize_zip_package → preview/confirm
@Description: 对不受信 Skill 归档执行整包预算、路径、编码、manifest 和确定性校验。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

import yaml


TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".py",
        ".sh",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".xml",
        ".csv",
        ".toml",
    }
)
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class GeneralSkillPackageError(ValueError):
    """携带稳定公开错误码的 Skill 包拒绝结果。"""

    def __init__(self, error_code: str, detail: str) -> None:
        """保存不含原始正文、绝对路径和内部异常栈的错误摘要。"""

        super().__init__(detail)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class PackageLimits:
    """定义租户只能收窄的 Skill 包默认安全预算。"""

    max_raw_bytes: int = 20 * 1024 * 1024
    max_expanded_bytes: int = 80 * 1024 * 1024
    max_text_file_bytes: int = 2 * 1024 * 1024
    max_binary_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 240
    max_path_depth: int = 12
    max_member_compression_ratio: int = 100


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    """表达一个仅由内容寻址、没有主机路径语义的规范资源。"""

    path: str
    content: bytes
    content_checksum: str
    size: int
    media_type: str
    is_text: bool


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """表达归档内一个需由用户显式选择的 SKILL.md 根。"""

    candidate_id: str
    root: str
    manifest_path: str
    name: str
    description: str
    metadata: dict[str, Any]
    allowed_tools: tuple[str, ...]
    resources: tuple[NormalizedResource, ...]
    content_checksum: str
    manifest_checksum: str


@dataclass(frozen=True, slots=True)
class NormalizedSkillPackage:
    """保存整包 checksum、预算事实和全部候选，禁止隐式选择第一个。"""

    raw_checksum: str
    normalized_checksum: str
    expanded_bytes: int
    candidates: tuple[SkillCandidate, ...]


def normalize_zip_package(
    payload: bytes,
    *,
    limits: PackageLimits | None = None,
) -> NormalizedSkillPackage:
    """严格校验 ZIP 全部成员并返回确定性候选树，任何成员失败则整包失败。"""

    policy = limits or PackageLimits()
    if len(payload) > policy.max_raw_bytes:
        _reject_limit("raw package exceeds configured byte limit")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if len(entries) > policy.max_files:
                _reject_limit("archive contains too many files")
            resources = _read_archive_resources(archive, entries, policy)
    except BadZipFile as exc:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive is not a valid ZIP package"
        ) from exc
    return _build_package(payload, resources)


def _read_archive_resources(
    archive: ZipFile,
    entries: list[ZipInfo],
    limits: PackageLimits,
) -> tuple[NormalizedResource, ...]:
    """先检查全量元数据预算，再读取成员，避免静默截断和解压炸弹。"""

    seen_paths: set[str] = set()
    expanded_bytes = 0
    for entry in entries:
        path = _validated_member_path(entry.filename, limits.max_path_depth)
        if path in seen_paths:
            raise GeneralSkillPackageError(
                "GENERAL_SKILL_PACKAGE_INVALID", "archive contains duplicate normalized paths"
            )
        seen_paths.add(path)
        _reject_unsupported_member(entry)
        expanded_bytes += entry.file_size
        if expanded_bytes > limits.max_expanded_bytes:
            _reject_limit("archive expanded size exceeds configured byte limit")
        ratio = entry.file_size / max(entry.compress_size, 1)
        if ratio > limits.max_member_compression_ratio:
            _reject_limit("archive member compression ratio exceeds configured limit")
        is_text = _is_text_path(path)
        file_limit = limits.max_text_file_bytes if is_text else limits.max_binary_file_bytes
        if entry.file_size > file_limit:
            _reject_limit("archive member exceeds configured file byte limit")
    resources: list[NormalizedResource] = []
    for entry in entries:
        path = _validated_member_path(entry.filename, limits.max_path_depth)
        content = archive.read(entry)
        if len(content) != entry.file_size:
            raise GeneralSkillPackageError(
                "GENERAL_SKILL_PACKAGE_INVALID", "archive member size changed while reading"
            )
        if _is_text_path(path):
            _decode_utf8(content)
        resources.append(_normalized_resource(path, content))
    return tuple(sorted(resources, key=lambda item: item.path))


def _build_package(
    raw_payload: bytes,
    resources: tuple[NormalizedResource, ...],
) -> NormalizedSkillPackage:
    """从全量规范资源发现所有候选并生成与归档顺序无关的 checksum。"""

    manifest_resources = [item for item in resources if PurePosixPath(item.path).name == "SKILL.md"]
    if not manifest_resources:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "package does not contain a SKILL.md manifest"
        )
    manifest_paths = {item.path for item in manifest_resources}
    candidates = tuple(
        _build_candidate(manifest, resources, manifest_paths) for manifest in manifest_resources
    )
    normalized_items = [
        {"path": item.path, "checksum": item.content_checksum, "size": item.size}
        for item in resources
    ]
    normalized_checksum = _json_checksum(normalized_items)
    return NormalizedSkillPackage(
        raw_checksum=_sha256(raw_payload),
        normalized_checksum=normalized_checksum,
        expanded_bytes=sum(item.size for item in resources),
        candidates=tuple(sorted(candidates, key=lambda item: item.manifest_path)),
    )


def _build_candidate(
    manifest: NormalizedResource,
    resources: tuple[NormalizedResource, ...],
    manifest_paths: set[str],
) -> SkillCandidate:
    """解析一个候选并只纳入其目录内不属于嵌套 Skill 的资源。"""

    root_path = PurePosixPath(manifest.path).parent
    root = "" if str(root_path) == "." else root_path.as_posix()
    candidate_resources = tuple(
        item
        for item in resources
        if _belongs_to_candidate(item.path, root, manifest.path, manifest_paths)
    )
    markdown = _decode_utf8(manifest.content)
    metadata = _parse_manifest(markdown)
    name = _required_metadata_text(metadata, "name")
    description = _required_metadata_text(metadata, "description")
    allowed_tools = _normalize_allowed_tools(metadata.get("allowed-tools"))
    manifest_checksum = manifest.content_checksum
    content_checksum = _json_checksum(
        [
            {"path": _relative_path(item.path, root), "checksum": item.content_checksum}
            for item in candidate_resources
        ]
    )
    candidate_id = f"gscand_{_sha256(f'{manifest.path}:{content_checksum}'.encode())[:24]}"
    return SkillCandidate(
        candidate_id=candidate_id,
        root=root,
        manifest_path=manifest.path,
        name=name,
        description=description,
        metadata=metadata,
        allowed_tools=allowed_tools,
        resources=candidate_resources,
        content_checksum=content_checksum,
        manifest_checksum=manifest_checksum,
    )


def _belongs_to_candidate(
    path: str,
    root: str,
    own_manifest: str,
    manifest_paths: set[str],
) -> bool:
    """判定资源属于候选根，且不会越过嵌套候选的 SKILL.md 边界。"""

    prefix = f"{root}/" if root else ""
    if not path.startswith(prefix):
        return False
    for manifest_path in manifest_paths:
        if manifest_path == own_manifest:
            continue
        nested_root = PurePosixPath(manifest_path).parent.as_posix()
        if nested_root != "." and (path == nested_root or path.startswith(f"{nested_root}/")):
            return False
    return True


def _validated_member_path(raw_path: str, max_depth: int) -> str:
    """拒绝绝对路径、盘符、反斜杠、父目录、空段和超深路径。"""

    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive member path is invalid"
        )
    if raw_path.startswith("/") or DRIVE_PREFIX.match(raw_path):
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive member path must be relative"
        )
    parts = raw_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive member path contains unsafe segments"
        )
    if len(parts) > max_depth:
        _reject_limit("archive member path exceeds configured depth")
    return PurePosixPath(*parts).as_posix()


def _reject_unsupported_member(entry: ZipInfo) -> None:
    """拒绝加密成员、符号链接和其他非普通文件类型。"""

    if entry.flag_bits & 0x1:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "encrypted archive members are not supported"
        )
    mode = entry.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type and file_type != stat.S_IFREG:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive contains a non-regular file"
        )


def _parse_manifest(markdown: str) -> dict[str, Any]:
    """使用安全 YAML 解析完整 frontmatter，并限制结构深度与字段规模。"""

    if not markdown.startswith("---\n"):
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md must start with YAML frontmatter"
        )
    closing = markdown.find("\n---", 4)
    if closing < 0:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md frontmatter is not closed"
        )
    try:
        parsed = yaml.safe_load(markdown[4:closing])
    except yaml.YAMLError as exc:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md frontmatter is invalid YAML"
        ) from exc
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md frontmatter must be an object"
        )
    if len(parsed) > 64 or _value_depth(parsed) > 8:
        _reject_limit("SKILL.md frontmatter exceeds structure limits")
    return parsed


def _value_depth(value: object, depth: int = 0) -> int:
    """计算 YAML 值深度以阻断病态嵌套输入。"""

    if isinstance(value, dict):
        return max((_value_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_value_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _required_metadata_text(metadata: dict[str, Any], key: str) -> str:
    """读取并验证候选预览所需的非空字符串元数据。"""

    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", f"SKILL.md requires non-empty {key}"
        )
    return value.strip()


def _normalize_allowed_tools(value: object) -> tuple[str, ...]:
    """把字符串或 YAML 字符串列表规范为稳定去重工具候选，不赋予权限。"""

    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = re.split(r"[\s,]+", value.strip())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw_items = value
    else:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "allowed-tools must be a string or string list"
        )
    normalized = {item.strip() for item in raw_items if item.strip()}
    return tuple(sorted(normalized))


def _normalized_resource(path: str, content: bytes) -> NormalizedResource:
    """创建内容寻址资源并推断仅用于展示的媒体类型。"""

    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return NormalizedResource(
        path=path,
        content=content,
        content_checksum=_sha256(content),
        size=len(content),
        media_type=media_type,
        is_text=_is_text_path(path),
    )


def _is_text_path(path: str) -> bool:
    """按受控扩展名判断是否必须通过严格 UTF-8 验证。"""

    return PurePosixPath(path).name == "SKILL.md" or PurePosixPath(path).suffix.lower() in TEXT_EXTENSIONS


def _decode_utf8(content: bytes) -> str:
    """严格解码文本，禁止 replacement character 隐藏 checksum 与审核差异。"""

    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "text resource is not valid UTF-8"
        ) from exc


def _relative_path(path: str, root: str) -> str:
    """把候选资源路径转换为相对候选根的稳定标识。"""

    return path[len(root) + 1 :] if root else path


def _sha256(payload: bytes) -> str:
    """返回小写十六进制 SHA-256 内容标识。"""

    return hashlib.sha256(payload).hexdigest()


def _json_checksum(value: object) -> str:
    """对排序、紧凑且 UTF-8 的规范 JSON 计算 checksum。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _reject_limit(detail: str) -> None:
    """以统一 413 语义拒绝任何资源预算超限。"""

    raise GeneralSkillPackageError("GENERAL_SKILL_PACKAGE_LIMIT_EXCEEDED", detail)
