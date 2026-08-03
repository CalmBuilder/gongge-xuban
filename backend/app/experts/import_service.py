"""
@Time       : 2026/07/28 21:48
@Author     : zhanglp8181
@File       : import_service.py
@CallChain  : 专家导入 CLI/API → 校验包 → AgentProfile/资源关系 → 受保护回滚
@Description: 幂等导入已校验专家包，并依据正式发布状态和依赖事实保护回滚。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.agents.identity import agent_is_published
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    Tenant,
    User,
    utc_now,
)
from app.experts.package import ImportPackageError, load_and_verify_package
from app.experts.schema import (
    ApplyItem,
    ApplyResult,
    ImportManifest,
    PreparedExpert,
    RollbackItem,
    RollbackResult,
)


SessionFactory = Callable[[], Session]


class ExpertImportError(ValueError):
    """导入或回滚的顶层前置条件不满足。"""


def _iso_now() -> str:
    return utc_now().isoformat()


def _result_filename(prefix: str) -> str:
    return f"{prefix}-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"


def _write_result(path: Path, value: ApplyResult | RollbackResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    content = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def validate_admin(db: Session, tenant_id: str, username: str) -> User:
    if db.get(Tenant, tenant_id) is None:
        raise ExpertImportError(f"Tenant does not exist: {tenant_id}")
    user = db.exec(
        select(User).where(User.tenant_id == tenant_id, User.username == username)
    ).first()
    if user is None or user.role != "admin":
        raise ExpertImportError("A tenant administrator is required")
    return user


def expert_metadata(
    expert: PreparedExpert,
    manifest: ImportManifest,
    admin: User,
) -> dict[str, object]:
    return {
        "employee_type": "expert",
        "expert_source_code": "agency-agents",
        "role_name": expert.translation.category_zh,
        "expert_category": expert.translation.category_zh,
        "expert_category_original": expert.parsed.category_original,
        "expert_tags": expert.translation.tags_zh,
        "expert_name_original": expert.parsed.name,
        "expert_emoji": expert.parsed.emoji,
        "expert_color": expert.parsed.color,
        "expert_vibe": expert.parsed.vibe,
        "expert_author": expert.parsed.author,
        "expert_declared_tools": expert.parsed.tools,
        "expert_services": [service.model_dump(mode="json") for service in expert.parsed.services],
        "expert_capability_manifest": expert.capability_manifest.model_dump(mode="json"),
        "expert_prompt_estimated_tokens": expert.prompt_estimated_tokens,
        "upstream_path": expert.parsed.upstream_path,
        "upstream_url": expert.upstream_url,
        "upstream_commit": manifest.source_commit,
        "upstream_license": "MIT",
        "import_batch_id": manifest.batch_id,
        "import_content_sha256": expert.content_sha256,
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or admin.username,
        "created_by_user_id": admin.id,
        "created_by_username": admin.username,
        "created_by": admin.username,
        "published_to_gallery": False,
    }


def _existing_import(db: Session, tenant_id: str, upstream_path: str) -> AgentProfile | None:
    rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if (
            metadata.get("employee_type") == "expert"
            and metadata.get("expert_source_code") == "agency-agents"
            and metadata.get("upstream_path") == upstream_path
        ):
            return row
    return None


def _available_name(db: Session, tenant_id: str, requested: str) -> str | None:
    names = set(
        db.exec(select(AgentProfile.name).where(AgentProfile.tenant_id == tenant_id)).all()
    )
    if requested not in names:
        return requested
    suffixed = f"{requested}（Agency Agents）"
    return suffixed if suffixed not in names else None


def apply_package(
    db_factory: SessionFactory,
    package_dir: Path,
    tenant_id: str,
    admin_username: str,
) -> ApplyResult:
    try:
        manifest, experts = load_and_verify_package(package_dir, tenant_id)
    except (OSError, ImportPackageError) as exc:
        raise ExpertImportError(str(exc)) from exc
    with db_factory() as db:
        admin = validate_admin(db, tenant_id, admin_username)
        admin_snapshot = User.model_validate(admin, from_attributes=True)

    result_path = package_dir.resolve() / _result_filename("apply-result")
    result = ApplyResult(
        batch_id=manifest.batch_id,
        tenant_id=tenant_id,
        started_at=_iso_now(),
        result_path=result_path,
        items=[],
    )
    _write_result(result_path, result)
    items: list[ApplyItem] = []
    for expert in experts:
        with db_factory() as db:
            try:
                existing = _existing_import(db, tenant_id, expert.parsed.upstream_path)
                if existing is not None:
                    item = ApplyItem(
                        upstream_path=expert.parsed.upstream_path,
                        status="skipped_existing",
                        name=existing.name,
                        agent_id=existing.id,
                        content_sha256=expert.content_sha256,
                    )
                else:
                    name = _available_name(db, tenant_id, expert.translation.name_zh)
                    if name is None:
                        item = ApplyItem(
                            upstream_path=expert.parsed.upstream_path,
                            status="failed_name_conflict",
                            name=expert.translation.name_zh,
                            content_sha256=expert.content_sha256,
                            message="Both stable expert names already exist",
                        )
                    else:
                        agent = AgentProfile(
                            tenant_id=tenant_id,
                            name=name,
                            description=expert.translation.description_zh,
                            persona_prompt=expert.translation.markdown_zh,
                            is_overall=False,
                            status="active",
                            owner_user_id=admin_snapshot.id,
                            agent_category_code="professional",
                            published_to_gallery=False,
                            visibility_scope="private",
                            metadata_json=expert_metadata(expert, manifest, admin_snapshot),
                        )
                        db.add(agent)
                        db.commit()
                        db.refresh(agent)
                        item = ApplyItem(
                            upstream_path=expert.parsed.upstream_path,
                            status="created",
                            name=agent.name,
                            agent_id=agent.id,
                            content_sha256=expert.content_sha256,
                            imported_updated_at=agent.updated_at.isoformat(),
                        )
            except (SQLAlchemyError, ValueError) as exc:
                db.rollback()
                item = ApplyItem(
                    upstream_path=expert.parsed.upstream_path,
                    status="failed",
                    name=expert.translation.name_zh,
                    content_sha256=expert.content_sha256,
                    message=str(exc),
                )
        items.append(item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result_path, result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result_path, result)
    return result


def _has_dependent_rows(db: Session, tenant_id: str, agent_id: str) -> bool:
    checks = (
        select(AgentResourceBinding.id).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
        ),
        select(AgentModelBinding.id).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == agent_id,
        ),
        select(AgentUsage.id).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.agent_id == agent_id,
        ),
        select(ChatSession.id).where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
        ),
    )
    return any(db.exec(statement).first() is not None for statement in checks)


def _rollback_guard_reason(
    db: Session,
    agent: AgentProfile | None,
    item: ApplyItem,
    batch_id: str,
    tenant_id: str,
) -> str | None:
    """返回阻止导入批次回滚的首个原因；只有未发布且无依赖的原始记录可删除。"""

    if agent is None or agent.tenant_id != tenant_id:
        return "agent missing or tenant mismatch"
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    if metadata.get("import_batch_id") != batch_id:
        return "import batch changed"
    if metadata.get("import_content_sha256") != item.content_sha256:
        return "import content changed"
    if item.imported_updated_at is None or agent.updated_at != datetime.fromisoformat(
        item.imported_updated_at
    ):
        return "agent was edited"
    if agent_is_published(agent):
        return "agent was published"
    if _has_dependent_rows(db, tenant_id, agent.id):
        return "agent is bound or used"
    return None


def rollback_apply_result(
    db_factory: SessionFactory,
    result_file: Path,
    tenant_id: str,
    admin_username: str,
) -> RollbackResult:
    try:
        applied = ApplyResult.model_validate_json(result_file.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ExpertImportError(f"Invalid apply result: {exc}") from exc
    if applied.tenant_id != tenant_id:
        raise ExpertImportError("Apply result tenant does not match command tenant")
    with db_factory() as db:
        validate_admin(db, tenant_id, admin_username)

    result_path = result_file.resolve().parent / _result_filename("rollback-result")
    result = RollbackResult(
        batch_id=applied.batch_id,
        tenant_id=tenant_id,
        started_at=_iso_now(),
        result_path=result_path,
        items=[],
    )
    _write_result(result_path, result)
    items: list[RollbackItem] = []
    for applied_item in applied.items:
        if applied_item.status != "created" or not applied_item.agent_id:
            rollback_item = RollbackItem(
                upstream_path=applied_item.upstream_path,
                agent_id=applied_item.agent_id,
                status="skipped_not_created",
            )
        else:
            with db_factory() as db:
                try:
                    agent = db.get(AgentProfile, applied_item.agent_id)
                    reason = _rollback_guard_reason(
                        db,
                        agent,
                        applied_item,
                        applied.batch_id,
                        tenant_id,
                    )
                    if reason:
                        rollback_item = RollbackItem(
                            upstream_path=applied_item.upstream_path,
                            agent_id=applied_item.agent_id,
                            status="skipped_modified_or_used",
                            message=reason,
                        )
                    else:
                        db.delete(agent)
                        db.commit()
                        rollback_item = RollbackItem(
                            upstream_path=applied_item.upstream_path,
                            agent_id=applied_item.agent_id,
                            status="deleted",
                        )
                except (SQLAlchemyError, ValueError) as exc:
                    db.rollback()
                    rollback_item = RollbackItem(
                        upstream_path=applied_item.upstream_path,
                        agent_id=applied_item.agent_id,
                        status="failed",
                        message=str(exc),
                    )
        items.append(rollback_item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result_path, result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result_path, result)
    return result
