"""把已验证中文化包安全应用到现有专家数字员工。"""

from __future__ import annotations

import json
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db.models import AgentProfile, utc_now
from app.experts.import_service import _has_dependent_rows, validate_admin
from app.experts.localization_package import load_and_verify_localization_package
from app.experts.localization_schema import (
    LocalizationApplyItem,
    LocalizationApplyResult,
    LocalizationRollbackItem,
    LocalizationRollbackResult,
    LocalizedExpert,
)


SessionFactory = Callable[[], Session]


def _iso_now() -> str:
    return utc_now().isoformat()


def _write_result(path: Path, result: LocalizationApplyResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    content = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _localized_content_sha(name: str, description: str | None, prompt: str | None) -> str:
    content = json.dumps(
        [name, description, prompt], ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _find_agent(
    db: Session,
    tenant_id: str,
    source_batch_id: str,
    expert: LocalizedExpert,
) -> AgentProfile | None:
    rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if (
            metadata.get("expert_source_code") == "agency-agents"
            and metadata.get("import_batch_id") == source_batch_id
            and metadata.get("upstream_path") == expert.upstream_path
            and metadata.get("upstream_commit") == expert.source_commit
        ):
            return row
    return None


def _already_localized(agent: AgentProfile, expert: LocalizedExpert) -> bool:
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    return (
        metadata.get("expert_translation_sha256") == expert.translation_sha256
        and agent.name == expert.localized_name
        and agent.description == expert.localized_description
        and agent.persona_prompt == expert.localized_prompt
        and agent.original_name == expert.original_name
        and agent.original_description == expert.original_description
        and agent.original_persona_prompt == expert.original_prompt
        and agent.original_locale == "en-US"
    )


def _safe_to_localize(agent: AgentProfile, expert: LocalizedExpert) -> bool:
    return (
        agent.name == expert.original_name
        and agent.description == expert.original_description
        and agent.persona_prompt == expert.original_prompt
        and agent.original_name is None
        and agent.original_description is None
        and agent.original_persona_prompt is None
        and agent.original_locale is None
    )


def apply_localization_package(
    db_factory: SessionFactory,
    package_dir: Path,
    tenant_id: str,
    admin_username: str,
) -> LocalizationApplyResult:
    manifest, experts = load_and_verify_localization_package(package_dir, tenant_id)
    with db_factory() as db:
        validate_admin(db, tenant_id, admin_username)
    result_path = package_dir.resolve() / f"localization-apply-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
    result = LocalizationApplyResult(
        tenant_id=tenant_id,
        source_batch_id=manifest.source_batch_id,
        translation_manifest_sha256=manifest.manifest_sha256,
        started_at=_iso_now(),
        result_path=result_path,
    )
    _write_result(result_path, result)
    items: list[LocalizationApplyItem] = []
    for expert in experts:
        with db_factory() as db:
            agent = _find_agent(db, tenant_id, manifest.source_batch_id, expert)
            if agent is None:
                item = LocalizationApplyItem(
                    upstream_path=expert.upstream_path,
                    status="failed_missing",
                    message="matching imported expert was not found",
                )
            elif _already_localized(agent, expert):
                item = LocalizationApplyItem(
                    upstream_path=expert.upstream_path,
                    agent_id=agent.id,
                    status="skipped_existing_translation",
                )
            elif not _safe_to_localize(agent, expert):
                item = LocalizationApplyItem(
                    upstream_path=expert.upstream_path,
                    agent_id=agent.id,
                    status="skipped_modified",
                    message="expert fields changed after import",
                )
            else:
                try:
                    metadata = dict(agent.metadata_json or {})
                    metadata.update(
                        {
                            "role_name": expert.category_zh,
                            "expert_translation_status": "verified",
                            "expert_translation_model": manifest.model_name,
                            "expert_translation_rules_version": manifest.rules_version,
                            "expert_translation_package_sha256": manifest.manifest_sha256,
                            "expert_translation_sha256": expert.translation_sha256,
                            "expert_translated_at": _iso_now(),
                        }
                    )
                    agent.original_name = expert.original_name
                    agent.original_description = expert.original_description
                    agent.original_persona_prompt = expert.original_prompt
                    agent.original_locale = "en-US"
                    agent.name = expert.localized_name
                    agent.description = expert.localized_description
                    agent.persona_prompt = expert.localized_prompt
                    agent.metadata_json = metadata
                    agent.updated_at = utc_now()
                    db.add(agent)
                    db.commit()
                    db.refresh(agent)
                    item = LocalizationApplyItem(
                        upstream_path=expert.upstream_path,
                        agent_id=agent.id,
                        status="updated",
                        localized_content_sha256=_localized_content_sha(
                            agent.name, agent.description, agent.persona_prompt
                        ),
                        localized_updated_at=agent.updated_at.isoformat(),
                    )
                except (SQLAlchemyError, ValueError) as exc:
                    db.rollback()
                    item = LocalizationApplyItem(
                        upstream_path=expert.upstream_path,
                        agent_id=agent.id,
                        status="failed",
                        message=str(exc),
                    )
        items.append(item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result_path, result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result_path, result)
    return result


def rollback_localization_result(
    db_factory: SessionFactory,
    apply_result_path: Path,
    tenant_id: str,
    admin_username: str,
) -> LocalizationRollbackResult:
    applied = LocalizationApplyResult.model_validate_json(apply_result_path.read_bytes())
    if applied.tenant_id != tenant_id:
        raise ValueError("Localization apply result tenant mismatch")
    with db_factory() as db:
        validate_admin(db, tenant_id, admin_username)
    result_path = apply_result_path.parent / f"localization-rollback-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
    result = LocalizationRollbackResult(
        tenant_id=tenant_id,
        source_batch_id=applied.source_batch_id,
        started_at=_iso_now(),
        result_path=result_path,
    )
    items: list[LocalizationRollbackItem] = []
    for applied_item in applied.items:
        if applied_item.status != "updated" or not applied_item.agent_id:
            items.append(
                LocalizationRollbackItem(
                    upstream_path=applied_item.upstream_path,
                    agent_id=applied_item.agent_id,
                    status="skipped_not_updated",
                )
            )
            continue
        with db_factory() as db:
            agent = db.get(AgentProfile, applied_item.agent_id)
            metadata = dict(agent.metadata_json or {}) if agent else {}
            safe = bool(
                agent
                and agent.tenant_id == tenant_id
                and applied_item.localized_content_sha256
                == _localized_content_sha(agent.name, agent.description, agent.persona_prompt)
                and applied_item.localized_updated_at == agent.updated_at.isoformat()
                and metadata.get("expert_translation_package_sha256")
                == applied.translation_manifest_sha256
                and metadata.get("published_to_gallery") is False
                and agent.original_name
                and agent.original_description
                and agent.original_persona_prompt
                and not _has_dependent_rows(db, tenant_id, agent.id)
            )
            if not safe or agent is None:
                item = LocalizationRollbackItem(
                    upstream_path=applied_item.upstream_path,
                    agent_id=applied_item.agent_id,
                    status="skipped_modified_or_used",
                )
            else:
                try:
                    agent.name = agent.original_name
                    agent.description = agent.original_description
                    agent.persona_prompt = agent.original_persona_prompt
                    metadata["expert_translation_status"] = "rolled_back"
                    agent.metadata_json = metadata
                    agent.updated_at = utc_now()
                    db.add(agent)
                    db.commit()
                    item = LocalizationRollbackItem(
                        upstream_path=applied_item.upstream_path,
                        agent_id=agent.id,
                        status="restored",
                    )
                except (SQLAlchemyError, ValueError) as exc:
                    db.rollback()
                    item = LocalizationRollbackItem(
                        upstream_path=applied_item.upstream_path,
                        agent_id=applied_item.agent_id,
                        status="failed",
                        message=str(exc),
                    )
        items.append(item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result_path, result)  # type: ignore[arg-type]
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result_path, result)  # type: ignore[arg-type]
    return result
