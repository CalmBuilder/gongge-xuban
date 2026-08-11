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
import math
import mimetypes
import re
import stat
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

import yaml
from yaml.events import AliasEvent


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
FRONTMATTER_END = re.compile(r"^---[ \t]*$", re.MULTILINE)
SKILL_REFERENCE = re.compile(
    r"`/([a-z][a-z0-9-]{0,79})`|(?<![\w./:@-])/([a-z][a-z0-9-]{0,79})(?![a-z0-9-])"
)
PLATFORM_COMMANDS = frozenset({"clear", "compact", "help", "reset"})


class _StrictManifestLoader(yaml.SafeLoader):
    """拒绝 YAML 别名和重复键，确保审核投影只有一种确定解释。"""

    def compose_node(self, parent: object, index: object):  # type: ignore[no-untyped-def]
        """在构建节点图之前拒绝 alias，避免循环、放大和共享引用语义。"""

        if self.check_event(AliasEvent):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "YAML aliases are not allowed",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        """逐项构造 mapping，并在后值覆盖前拒绝重复键。"""

        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "expected a mapping node",
                node.start_mark,
            )
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_StrictManifestLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictManifestLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


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
class SkillDependencyCandidate:
    """表达正文引用产生、仍需人工确认才能成为运行依赖的同包候选边。"""

    dependency_candidate_id: str
    referenced_name: str
    referenced_candidate_id: str
    reference_count: int


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
    invocation_policy: str
    argument_hint: str | None
    dependency_candidates: tuple[SkillDependencyCandidate, ...]
    platform_commands: tuple[str, ...]
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
    source_subpath: str | None = None,
) -> NormalizedSkillPackage:
    """严格校验 ZIP 或显式仓库子树并返回候选，选中子树内任一失败则整包失败。"""

    policy = limits or PackageLimits()
    if len(payload) > policy.max_raw_bytes:
        _reject_limit("raw package exceeds configured byte limit")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            path_overrides: dict[str, str] | None = None
            if source_subpath is not None:
                entries, path_overrides = _select_repository_subtree(
                    entries,
                    source_subpath,
                    policy.max_path_depth,
                )
            if len(entries) > policy.max_files:
                _reject_limit("archive contains too many files")
            resources = _read_archive_resources(archive, entries, policy, path_overrides)
    except BadZipFile as exc:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "archive is not a valid ZIP package"
        ) from exc
    return _build_package(payload, resources)


def _read_archive_resources(
    archive: ZipFile,
    entries: list[ZipInfo],
    limits: PackageLimits,
    path_overrides: dict[str, str] | None = None,
) -> tuple[NormalizedResource, ...]:
    """先检查全量元数据预算，再读取成员，避免静默截断和解压炸弹。"""

    seen_paths: set[str] = set()
    expanded_bytes = 0
    for entry in entries:
        path = (path_overrides or {}).get(entry.filename) or _validated_member_path(
            entry.filename, limits.max_path_depth
        )
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
        path = (path_overrides or {}).get(entry.filename) or _validated_member_path(
            entry.filename, limits.max_path_depth
        )
        content = archive.read(entry)
        if len(content) != entry.file_size:
            raise GeneralSkillPackageError(
                "GENERAL_SKILL_PACKAGE_INVALID", "archive member size changed while reading"
            )
        if _is_text_path(path):
            _decode_utf8(content)
        resources.append(_normalized_resource(path, content))
    return tuple(sorted(resources, key=lambda item: item.path))


def _select_repository_subtree(
    entries: list[ZipInfo],
    source_subpath: str,
    max_path_depth: int,
) -> tuple[list[ZipInfo], dict[str, str]]:
    """先验证全归档路径，再剥离 GitHub 根目录并选择用户明确审核的仓库子树。"""

    normalized_subpath = (
        "" if source_subpath.strip() == "." else _validated_member_path(
            source_subpath.strip("/"), max_path_depth
        )
    )
    all_paths = {
        entry.filename: _validated_member_path(entry.filename, max_path_depth + 1)
        for entry in entries
    }
    first_segments = {path.split("/", 1)[0] for path in all_paths.values()}
    if len(first_segments) != 1:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID",
            "repository archive must have one stable root directory",
        )
    archive_root = next(iter(first_segments))
    prefix = f"{archive_root}/{normalized_subpath}/" if normalized_subpath else f"{archive_root}/"
    selected: list[ZipInfo] = []
    overrides: dict[str, str] = {}
    for entry in entries:
        path = all_paths[entry.filename]
        if not path.startswith(prefix):
            continue
        relative = path[len(archive_root) + 1 :]
        _validated_member_path(relative, max_path_depth)
        selected.append(entry)
        overrides[entry.filename] = relative
    if not selected:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "repository subpath contains no files"
        )
    return selected, overrides


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
    candidates = _attach_reference_graph(candidates)
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
    invocation_policy = _normalize_invocation_policy(metadata.get("disable-model-invocation"))
    argument_hint = _normalize_argument_hint(metadata.get("argument-hint"))
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
        invocation_policy=invocation_policy,
        argument_hint=argument_hint,
        dependency_candidates=(),
        platform_commands=(),
        resources=candidate_resources,
        content_checksum=content_checksum,
        manifest_checksum=manifest_checksum,
    )


def _attach_reference_graph(
    candidates: tuple[SkillCandidate, ...],
) -> tuple[SkillCandidate, ...]:
    """把正文斜杠引用分类为同包 Skill 候选或平台命令，不静默授权依赖。"""

    by_name: dict[str, SkillCandidate] = {}
    for candidate in candidates:
        normalized_name = candidate.name.casefold()
        if normalized_name in by_name:
            raise GeneralSkillPackageError(
                "GENERAL_SKILL_DEPENDENCY_INVALID",
                "package contains duplicate Skill names",
            )
        by_name[normalized_name] = candidate
    enriched: list[SkillCandidate] = []
    for candidate in candidates:
        manifest = next(
            resource for resource in candidate.resources if resource.path == candidate.manifest_path
        )
        references: dict[str, int] = {}
        platform_commands: set[str] = set()
        for match in SKILL_REFERENCE.finditer(_manifest_body(_decode_utf8(manifest.content))):
            referenced_name = (match.group(1) or match.group(2)).casefold()
            if referenced_name in PLATFORM_COMMANDS:
                platform_commands.add(referenced_name)
            elif referenced_name in by_name:
                references[referenced_name] = references.get(referenced_name, 0) + 1
        dependency_candidates = tuple(
            SkillDependencyCandidate(
                dependency_candidate_id=(
                    f"gsdepcand_{_sha256(f'{candidate.candidate_id}:{by_name[name].candidate_id}'.encode())[:24]}"
                ),
                referenced_name=name,
                referenced_candidate_id=by_name[name].candidate_id,
                reference_count=count,
            )
            for name, count in sorted(references.items())
        )
        enriched.append(
            replace(
                candidate,
                dependency_candidates=dependency_candidates,
                platform_commands=tuple(sorted(platform_commands)),
            )
        )
    return tuple(enriched)


def _manifest_body(markdown: str) -> str:
    """返回 frontmatter 精确结束行之后的正文，供非授权性的依赖候选扫描。"""

    closing_match = FRONTMATTER_END.search(markdown, 4)
    if closing_match is None:
        return ""
    return markdown[closing_match.end() :]


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
    closing_match = FRONTMATTER_END.search(markdown, 4)
    if closing_match is None:
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md frontmatter is not closed"
        )
    try:
        parsed = yaml.load(markdown[4 : closing_match.start()], Loader=_StrictManifestLoader)
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
    _validate_manifest_value(parsed)
    return parsed


def _value_depth(value: object, depth: int = 0) -> int:
    """计算 YAML 值深度以阻断病态嵌套输入。"""

    if isinstance(value, dict):
        return max((_value_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_value_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _validate_manifest_value(value: object) -> None:
    """只接受可确定编码为 JSON 的值，并拒绝日期、集合和非有限浮点等 YAML 特性。"""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "SKILL.md frontmatter contains a non-finite number"
        )
    if isinstance(value, list):
        for item in value:
            _validate_manifest_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_manifest_value(item)
        return
    raise GeneralSkillPackageError(
        "GENERAL_SKILL_PACKAGE_INVALID",
        "SKILL.md frontmatter contains a non-JSON value",
    )


def _required_metadata_text(metadata: dict[str, Any], key: str) -> str:
    """读取并验证候选预览所需的非空字符串元数据。"""

    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", f"SKILL.md requires non-empty {key}"
        )
    normalized = value.strip()
    limit = 255 if key == "name" else 1_000
    if len(normalized) > limit:
        _reject_limit(f"SKILL.md {key} exceeds configured character limit")
    return normalized


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


def _normalize_invocation_policy(value: object) -> str:
    """把兼容字段映射为明确调用策略，拒绝字符串 truthy 等歧义输入。"""

    if value is None or value is False:
        return "model_allowed"
    if value is True:
        return "user_only"
    raise GeneralSkillPackageError(
        "GENERAL_SKILL_PACKAGE_INVALID", "disable-model-invocation must be a boolean"
    )


def _normalize_argument_hint(value: object) -> str | None:
    """规范化仅用于显式调用提示的 argument-hint，并限制展示预算。"""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GeneralSkillPackageError(
            "GENERAL_SKILL_PACKAGE_INVALID", "argument-hint must be a non-empty string"
        )
    normalized = value.strip()
    if len(normalized) > 500:
        _reject_limit("SKILL.md argument-hint exceeds configured character limit")
    return normalized


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
