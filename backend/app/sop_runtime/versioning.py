"""
@Time       : 2026/07/22 09:10
@Author     : zhanglp8181
@File       : versioning.py
@CallChain  : SOP 发布/回滚 API → 版本策略 → SkillVersion 持久化
@Description: 提供发布快照校验、幂等写入、冲突检测和派生版本号策略。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.models import Skill, SkillVersion, utc_now
from app.sop_runtime.definition import CompiledSopDefinition


class PublishedVersionConflictError(ValueError):
    """表示调用方试图用不同内容覆盖同一已发布业务版本。"""

    def __init__(self, *, skill_id: str, version: str) -> None:
        """保存发生冲突的 SOP 与业务版本，避免向领域层传入 HTTP 语义。"""

        self.skill_id = skill_id
        self.version = version
        super().__init__(f"Published SOP version is immutable: {skill_id}@{version}")


@dataclass(frozen=True, slots=True)
class SkillVersionWriteResult:
    """描述版本快照写入是新建、草稿更新还是幂等命中。"""

    version: SkillVersion
    created: bool
    idempotent: bool


def skill_content_checksum(content_json: dict[str, object]) -> str:
    """对完整 SkillCard JSON 计算稳定 SHA-256，检测任何已发布内容变化。"""

    encoded = json.dumps(
        content_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_skill_version(
    db: Session,
    skill: Skill,
    *,
    compiled_definition: CompiledSopDefinition | None = None,
    derived_from_version_id: str | None = None,
    version_id: str | None = None,
) -> SkillVersionWriteResult:
    """在当前事务内写入版本快照，并保护已发布版本的内容与来源元数据。"""

    candidate_checksum = skill_content_checksum(skill.content_json)
    existing = db.exec(
        select(SkillVersion)
        .where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
        .with_for_update()
    ).first()
    if existing and existing.status == "published":
        existing_checksum = existing.content_checksum or skill_content_checksum(
            existing.content_json
        )
        if existing_checksum != candidate_checksum:
            raise PublishedVersionConflictError(
                skill_id=skill.skill_id,
                version=skill.version,
            )
        _backfill_publication_metadata(existing, compiled_definition, existing_checksum)
        db.add(existing)
        return SkillVersionWriteResult(version=existing, created=False, idempotent=True)

    if existing:
        _copy_editable_snapshot(existing, skill)
        _apply_publication_metadata(
            existing,
            compiled_definition=compiled_definition,
            content_checksum=candidate_checksum,
            derived_from_version_id=derived_from_version_id,
        )
        db.add(existing)
        return SkillVersionWriteResult(version=existing, created=False, idempotent=False)

    identity_fields = {"id": version_id} if version_id else {}
    version_row = SkillVersion(
        **identity_fields,
        tenant_id=skill.tenant_id,
        skill_id=skill.skill_id,
        version=skill.version,
        name=skill.name,
        business_domain=skill.business_domain,
        description=skill.description,
        content_json=dict(skill.content_json),
        status=skill.status,
    )
    _apply_publication_metadata(
        version_row,
        compiled_definition=compiled_definition,
        content_checksum=candidate_checksum,
        derived_from_version_id=derived_from_version_id,
    )
    db.add(version_row)
    db.flush()
    return SkillVersionWriteResult(version=version_row, created=True, idempotent=False)


def next_derived_skill_version(db: Session, skill: Skill) -> str:
    """基于当前头版本生成未占用的派生版本，避免回滚复用历史版本号。"""

    candidate = _increment_version(skill.version)
    while db.exec(
        select(SkillVersion.id).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == candidate,
        )
    ).first():
        candidate = _increment_version(candidate)
    return candidate


def _increment_version(version: str) -> str:
    """优先递增三段式版本的补丁位，并为历史非语义版本提供确定性后缀。"""

    parts = version.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    rollback_prefix, separator, sequence = version.rpartition(".rollback.")
    if separator and sequence.isdigit():
        return f"{rollback_prefix}.rollback.{int(sequence) + 1}"
    return f"{version}.rollback.1"


def _copy_editable_snapshot(target: SkillVersion, source: Skill) -> None:
    """仅在版本尚未发布时，用编辑头内容更新可变草稿快照。"""

    target.name = source.name
    target.business_domain = source.business_domain
    target.description = source.description
    target.content_json = dict(source.content_json)
    target.status = source.status
    target.updated_at = utc_now()


def _apply_publication_metadata(
    version: SkillVersion,
    *,
    compiled_definition: CompiledSopDefinition | None,
    content_checksum: str,
    derived_from_version_id: str | None,
) -> None:
    """为发布快照固化校验和、元模型版本、来源版本和发布时间。"""

    if version.status != "published":
        return
    if compiled_definition is None:
        raise ValueError("Published SOP snapshots require a compiled definition")
    version.content_checksum = content_checksum
    version.compiled_definition_checksum = compiled_definition.checksum
    version.meta_model_version = compiled_definition.meta_model_version
    version.source_schema_version = compiled_definition.source_schema_version
    version.published_at = version.published_at or utc_now()
    version.derived_from_version_id = derived_from_version_id


def _backfill_publication_metadata(
    version: SkillVersion,
    compiled_definition: CompiledSopDefinition | None,
    content_checksum: str,
) -> None:
    """只补齐旧发布行缺失的元数据，不改变其业务内容和既有派生来源。"""

    version.content_checksum = version.content_checksum or content_checksum
    version.published_at = version.published_at or version.created_at
    if compiled_definition is not None:
        version.compiled_definition_checksum = (
            version.compiled_definition_checksum or compiled_definition.checksum
        )
        version.meta_model_version = (
            version.meta_model_version or compiled_definition.meta_model_version
        )
        version.source_schema_version = (
            version.source_schema_version or compiled_definition.source_schema_version
        )
