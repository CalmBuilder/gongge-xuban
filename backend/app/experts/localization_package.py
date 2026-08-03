"""可断点恢复、可校验的专家中文化预览包。"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.experts.localization_integrity import (
    restore_chunk_boundaries,
    split_markdown,
)
from app.experts.localization_schema import (
    LocalizationError,
    LocalizationManifest,
    LocalizationManifestItem,
    LocalizedExpert,
)
from app.experts.localization_translation import (
    LocalizedIdentity,
    LocalizationTranslator,
    NameCandidate,
    UpstreamNameMapping,
    VerifiedChunkTranslation,
    resolve_localized_names,
)
from app.experts.package import load_and_verify_package


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _stem(upstream_path: str) -> str:
    return upstream_path.removesuffix(".md").replace("/", "__")


def _load_model(path: Path, model: type[LocalizedIdentity] | type[VerifiedChunkTranslation]):
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return None


def _translation_hash(expert: LocalizedExpert) -> str:
    return _sha256(_canonical(expert.model_dump(mode="json", exclude={"translation_sha256"})))


def _manifest_hash(manifest: LocalizationManifest) -> str:
    return _sha256(_canonical(manifest.model_dump(mode="json", exclude={"manifest_sha256"})))


def _write_report(
    output: Path,
    manifest: LocalizationManifest,
    experts: list[LocalizedExpert],
    errors: list[LocalizationError],
) -> None:
    lines = [
        "# Agency Agents 中文化预览报告",
        "",
        f"- 来源批次：`{manifest.source_batch_id}`",
        f"- 固定提交：`{manifest.source_commit}`",
        f"- 模型：`{manifest.model_name}` (`{manifest.model_config_id}`)",
        f"- 选择：{manifest.selected_count}",
        f"- 已验证：{manifest.verified_count}",
        f"- 失败：{manifest.failed_count}",
        "",
        "## 专家",
        "",
    ]
    lines.extend(
        f"- `{item.upstream_path}`：{item.original_name} → {item.localized_name}；"
        f"{len(item.chunks)} 块"
        for item in experts
    )
    if errors:
        lines.extend(["", "## 错误", ""])
        lines.extend(f"- `{item.upstream_path}`：{item.message}" for item in errors)
    _atomic_write(output / "TRANSLATION_REPORT.md", ("\n".join(lines) + "\n").encode())


def prepare_localization_package(
    source_package: Path,
    output: Path,
    tenant_id: str,
    model_config_id: str,
    model_name: str,
    translator: LocalizationTranslator,
    upstream_mapping: dict[str, UpstreamNameMapping],
    *,
    only_paths: set[str] | None = None,
    occupied_names: set[str] | None = None,
    max_workers: int = 1,
) -> LocalizationManifest:
    source_manifest, prepared = load_and_verify_package(source_package, tenant_id)
    selected = [
        item for item in prepared if only_paths is None or item.parsed.upstream_path in only_paths
    ]
    output.mkdir(parents=True, exist_ok=True)
    experts_dir = output / "experts"
    work_dir = output / "work"
    experts_dir.mkdir(exist_ok=True)
    work_dir.mkdir(exist_ok=True)
    identities: dict[str, LocalizedIdentity] = {}
    errors: list[LocalizationError] = []
    for item in selected:
        expert = item.parsed
        checkpoint = work_dir / _stem(expert.upstream_path) / "identity.json"
        identity = _load_model(checkpoint, LocalizedIdentity)
        mapping = upstream_mapping.get(expert.name)
        if mapping:
            identity = LocalizedIdentity(
                upstream_path=expert.upstream_path,
                source_sha256=expert.source_sha256,
                name_zh=mapping.name_zh,
                description_zh=mapping.description_zh,
            )
        if identity is None or (
            identity.upstream_path != expert.upstream_path
            or identity.source_sha256 != expert.source_sha256
        ):
            try:
                identity = translator.translate_identity(expert)
            except Exception as exc:  # noqa: BLE001
                errors.append(LocalizationError(upstream_path=expert.upstream_path, message=str(exc)))
                continue
        _atomic_write(checkpoint, _canonical(identity.model_dump(mode="json")))
        identities[expert.upstream_path] = identity

    candidates = [
        NameCandidate(
            upstream_path=item.parsed.upstream_path,
            original_name=item.parsed.name,
            localized_name=identities[item.parsed.upstream_path].name_zh,
        )
        for item in selected
        if item.parsed.upstream_path in identities
    ]
    resolved_names = resolve_localized_names(candidates, occupied_names or set())
    def localize_item(item):
        expert = item.parsed
        identity = identities.get(expert.upstream_path)
        if identity is None:
            return None, None, False
        filename = f"{_stem(expert.upstream_path)}.json"
        existing_path = experts_dir / filename
        try:
            existing = LocalizedExpert.model_validate_json(existing_path.read_bytes())
            if (
                existing.source_content_sha256 == item.content_sha256
                and existing.localized_name == resolved_names[expert.upstream_path]
                and existing.translation_sha256 == _translation_hash(existing)
            ):
                return existing, None, False
        except (OSError, ValidationError, ValueError):
            pass
        chunks: list[VerifiedChunkTranslation] = []
        try:
            for chunk in split_markdown(expert.source_markdown, max_chars=750):
                chunk_path = work_dir / _stem(expert.upstream_path) / f"chunk-{chunk.index:04d}.json"
                translated = _load_model(chunk_path, VerifiedChunkTranslation)
                if translated is None or translated.source_sha256 != chunk.source_sha256:
                    translated = translator.translate_chunk(
                        expert, chunk, resolved_names[expert.upstream_path]
                    )
                    _atomic_write(chunk_path, _canonical(translated.model_dump(mode="json")))
                else:
                    normalized = restore_chunk_boundaries(
                        chunk.source_text, translated.translated_markdown
                    )
                    if normalized != translated.translated_markdown:
                        translated = translated.model_copy(
                            update={"translated_markdown": normalized}
                        )
                        _atomic_write(
                            chunk_path, _canonical(translated.model_dump(mode="json"))
                        )
                chunks.append(translated)
            localized_prompt = "".join(chunk.translated_markdown for chunk in chunks)
            value = LocalizedExpert(
                upstream_path=expert.upstream_path,
                source_batch_id=source_manifest.batch_id,
                source_commit=source_manifest.source_commit,
                source_content_sha256=item.content_sha256,
                original_name=expert.name,
                original_description=expert.description,
                original_prompt=expert.source_markdown,
                localized_name=resolved_names[expert.upstream_path],
                localized_description=identity.description_zh,
                localized_prompt=localized_prompt,
                category_zh=item.translation.category_zh,
                chunks=chunks,
                translation_sha256="",
            )
            value = value.model_copy(update={"translation_sha256": _translation_hash(value)})
            _atomic_write(existing_path, _canonical(value.model_dump(mode="json")))
            return value, None, True
        except Exception as exc:  # noqa: BLE001
            return (
                None,
                LocalizationError(upstream_path=expert.upstream_path, message=str(exc)),
                True,
            )

    localized_experts: list[LocalizedExpert] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        results = executor.map(localize_item, selected)
        for item, (localized, error, processed) in zip(selected, results, strict=True):
            if error is not None:
                errors.append(error)
                print(f"failed {item.parsed.upstream_path}: {error.message}", flush=True)
            elif localized is not None:
                localized_experts.append(localized)
                if processed:
                    print(f"completed {item.parsed.upstream_path}", flush=True)

    items: list[LocalizationManifestItem] = []
    for expert in localized_experts:
        filename = f"{_stem(expert.upstream_path)}.json"
        content = (experts_dir / filename).read_bytes()
        items.append(
            LocalizationManifestItem(
                upstream_path=expert.upstream_path,
                filename=filename,
                sha256=_sha256(content),
            )
        )
    manifest = LocalizationManifest(
        generated_at=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        source_batch_id=source_manifest.batch_id,
        source_commit=source_manifest.source_commit,
        model_config_id=model_config_id,
        model_name=model_name,
        selected_count=len(selected),
        verified_count=len(localized_experts),
        failed_count=len(errors),
        experts=items,
        manifest_sha256="",
    )
    manifest = manifest.model_copy(update={"manifest_sha256": _manifest_hash(manifest)})
    _atomic_write(output / "manifest.json", _canonical(manifest.model_dump(mode="json")))
    _atomic_write(output / "errors.json", _canonical([item.model_dump() for item in errors]))
    _write_report(output, manifest, localized_experts, errors)
    return manifest


def load_and_verify_localization_package(
    output: Path,
    tenant_id: str,
) -> tuple[LocalizationManifest, list[LocalizedExpert]]:
    try:
        manifest = LocalizationManifest.model_validate_json((output / "manifest.json").read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError(f"Invalid localization manifest: {exc}") from exc
    if manifest.tenant_id != tenant_id:
        raise ValueError("Localization package tenant mismatch")
    if manifest.manifest_sha256 != _manifest_hash(manifest):
        raise ValueError("Localization manifest SHA-256 mismatch")
    experts: list[LocalizedExpert] = []
    for item in manifest.experts:
        path = output / "experts" / item.filename
        content = path.read_bytes()
        if _sha256(content) != item.sha256:
            raise ValueError(f"Expert SHA-256 mismatch: {item.filename}")
        expert = LocalizedExpert.model_validate_json(content)
        if expert.translation_sha256 != _translation_hash(expert):
            raise ValueError(f"Translation SHA-256 mismatch: {item.filename}")
        experts.append(expert)
    return manifest, experts
