"""原子生成并校验可审计的一次性专家导入预览包。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import ValidationError

from app.experts.capability import prompt_budget_warning
from app.experts.local_source import LocalSource
from app.experts.parser import ParsedExpert
from app.experts.schema import (
    CapabilityManifest,
    ExpertTranslation,
    ImportManifest,
    ManifestExpert,
    PreparedExpert,
    PrepareError,
)


class ImportPackageError(ValueError):
    """预览包格式、摘要或租户不可信。"""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_write(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def prepare_expert(
    parsed: ParsedExpert,
    translation: ExpertTranslation,
    capability_manifest: CapabilityManifest,
    prompt_estimated_tokens: int,
    *,
    source_commit: str,
) -> PreparedExpert:
    upstream_url = (
        "https://github.com/msitarzewski/agency-agents/blob/"
        f"{source_commit}/{quote(parsed.upstream_path, safe='/')}"
    )
    content = {
        "parsed": parsed.model_dump(mode="json"),
        "translation": translation.model_dump(mode="json"),
        "capability_manifest": capability_manifest.model_dump(mode="json"),
        "prompt_estimated_tokens": prompt_estimated_tokens,
        "upstream_url": upstream_url,
    }
    return PreparedExpert(
        **content,
        content_sha256=_sha256(_canonical_bytes(content)),
    )


def _expert_filename(expert: PreparedExpert) -> str:
    stem = expert.parsed.upstream_path.removesuffix(".md").replace("/", "__")
    return f"{stem}__{expert.parsed.source_sha256[:12]}.json"


def _manifest_hash_payload(manifest: ImportManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json", exclude={"manifest_sha256"})


def _render_report(
    source: LocalSource,
    experts: list[PreparedExpert],
    errors: list[PrepareError],
) -> str:
    types = Counter(item.capability_manifest.capability_type for item in experts)
    readiness = Counter(item.capability_manifest.readiness for item in experts)
    lines = [
        "# Agency Agents 导入预览报告",
        "",
        f"- 来源：`{source.root}`",
        f"- 提交：`{source.commit_sha}`",
        f"- 来源已验证：`{str(source.verified).lower()}`",
        f"- 成功：{len(experts)}",
        f"- 失败：{len(errors)}",
        f"- 能力类型：P0={types['P0']}，P1={types['P1']}，P2={types['P2']}，P3={types['P3']}",
        (
            "- 就绪状态："
            f"ready={readiness['ready']}，partial={readiness['partial']}，blocked={readiness['blocked']}"
        ),
        "",
        "## 专家",
        "",
    ]
    for expert in experts:
        capability = expert.capability_manifest
        unresolved = "、".join(capability.unresolved_requirements) or "无"
        warning = "；⚠ 提示词达到 24,000 token" if prompt_budget_warning(
            expert.prompt_estimated_tokens
        ) else ""
        lines.append(
            f"- `{expert.parsed.upstream_path}`：{expert.parsed.name} → "
            f"{expert.translation.name_zh}；{capability.capability_type}/{capability.readiness}；"
            f"未解析：{unresolved}{warning}"
        )
    if errors:
        lines.extend(["", "## 错误", ""])
        lines.extend(f"- `{item.upstream_path}` [{item.stage}] {item.message}" for item in errors)
    return "\n".join(lines) + "\n"


def write_preview_package(
    output: Path,
    source: LocalSource,
    tenant_id: str,
    experts: list[PreparedExpert],
    errors: list[PrepareError],
) -> ImportManifest:
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir() if output.is_dir() else [output]):
        raise ImportPackageError("Preview output directory already exists and is not empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        experts_dir = temporary / "experts"
        experts_dir.mkdir()
        manifest_items: list[ManifestExpert] = []
        filenames: set[str] = set()
        for expert in experts:
            filename = _expert_filename(expert)
            if filename in filenames:
                raise ImportPackageError(f"Expert filename collision: {filename}")
            filenames.add(filename)
            content = _canonical_bytes(expert.model_dump(mode="json"))
            _fsync_write(experts_dir / filename, content)
            manifest_items.append(
                ManifestExpert(
                    upstream_path=expert.parsed.upstream_path,
                    filename=filename,
                    sha256=_sha256(content),
                )
            )
        manifest = ImportManifest(
            batch_id=f"expertimport_{uuid4().hex}",
            generated_at=datetime.now(timezone.utc),
            tenant_id=tenant_id,
            source_absolute_path=str(source.root),
            source_remote_url=source.remote_url,
            source_commit=source.commit_sha,
            source_verified=source.verified,
            candidate_count=len(experts) + len(errors),
            success_count=len(experts),
            failed_count=len(errors),
            experts=manifest_items,
            manifest_sha256="",
        )
        manifest = manifest.model_copy(
            update={"manifest_sha256": _sha256(_canonical_bytes(_manifest_hash_payload(manifest)))}
        )
        _fsync_write(temporary / "manifest.json", _canonical_bytes(manifest.model_dump(mode="json")))
        _fsync_write(
            temporary / "errors.json",
            _canonical_bytes([item.model_dump(mode="json") for item in errors]),
        )
        _fsync_write(temporary / "IMPORT_REPORT.md", _render_report(source, experts, errors).encode())
        if output.exists():
            output.rmdir()
        os.replace(temporary, output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_and_verify_package(
    input_dir: Path,
    tenant_id: str,
) -> tuple[ImportManifest, list[PreparedExpert]]:
    root = input_dir.expanduser().resolve(strict=True)
    try:
        manifest = ImportManifest.model_validate_json((root / "manifest.json").read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ImportPackageError(f"Invalid manifest: {exc}") from exc
    if manifest.tenant_id != tenant_id:
        raise ImportPackageError("Package tenant does not match command tenant")
    expected_manifest_hash = _sha256(_canonical_bytes(_manifest_hash_payload(manifest)))
    if expected_manifest_hash != manifest.manifest_sha256:
        raise ImportPackageError("Manifest SHA-256 mismatch")
    experts: list[PreparedExpert] = []
    seen_paths: set[str] = set()
    for item in manifest.experts:
        if item.upstream_path in seen_paths:
            raise ImportPackageError(f"Duplicate upstream path: {item.upstream_path}")
        seen_paths.add(item.upstream_path)
        if Path(item.filename).name != item.filename:
            raise ImportPackageError("Unsafe expert filename")
        expert_path = root / "experts" / item.filename
        try:
            content = expert_path.read_bytes()
        except OSError as exc:
            raise ImportPackageError(f"Missing expert file: {item.filename}") from exc
        if _sha256(content) != item.sha256:
            raise ImportPackageError(f"Expert SHA-256 mismatch: {item.filename}")
        try:
            expert = PreparedExpert.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise ImportPackageError(f"Invalid expert file {item.filename}: {exc}") from exc
        if expert.parsed.upstream_path != item.upstream_path:
            raise ImportPackageError(f"Expert path mismatch: {item.filename}")
        content_payload = expert.model_dump(mode="json", exclude={"content_sha256"})
        if _sha256(_canonical_bytes(content_payload)) != expert.content_sha256:
            raise ImportPackageError(f"Expert content SHA-256 mismatch: {item.filename}")
        experts.append(expert)
    return manifest, experts
