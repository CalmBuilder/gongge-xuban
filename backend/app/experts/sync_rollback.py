"""
@Time       : 2026/08/29 15:00
@Author     : zhanglp8181
@File       : sync_rollback.py
@CallChain  : sync apply 结果 → 快照/当前摘要校验 → 租户专家安全回滚 → 回滚结果
@Description: 依据 apply 结果中的前置快照恢复专家或删除本批新增专家，拒绝覆盖后续业务变更。
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, select

from app.agents.identity import agent_is_published
from app.db.models import AgentProfile, utc_now
from app.experts.import_service import ExpertImportError, validate_admin
from app.experts.sync_apply import (
    SyncAgentSnapshot,
    SyncApplyItem,
    SyncApplyResult,
    metadata_sha256,
)
from app.experts.sync_plan import profile_content_sha256


SessionFactory = Callable[[], Session]
SyncRollbackItemStatus = Literal[
    "deleted",
    "restored",
    "skipped_not_applied",
    "skipped_no_snapshot",
    "skipped_modified_or_used",
    "failed",
]


class ExpertSyncRollbackError(ValueError):
    """同步回滚的输入、权限或安全前置条件不满足。"""


class SyncRollbackItem(BaseModel):
    """一个 apply 条目的可验证回滚结果。"""

    model_config = ConfigDict(frozen=True)

    upstream_path: str
    status: SyncRollbackItemStatus
    agent_id: str | None = None
    profile_revision: int | None = None
    updated_at: str | None = None
    message: str | None = None


class SyncRollbackResult(BaseModel):
    """可审计的同步回滚结果，保留每个条目的跳过原因。"""

    model_config = ConfigDict(frozen=True)

    operation: Literal["rollback"] = "rollback"
    tenant_id: str
    apply_result_path: Path
    source_batch_id: str
    source_commit: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[SyncRollbackItem] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """返回按回滚状态聚合的数量。"""

        return dict(Counter(item.status for item in self.items))


def _iso_now() -> str:
    """返回当前 UTC 时间的 ISO 文本。"""

    return utc_now().isoformat()


def _atomic_write(path: Path, content: bytes) -> None:
    """以同目录临时文件原子写入回滚结果。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_result(result: SyncRollbackResult) -> None:
    """持久化本地回滚结果，不把审计结果混入业务表。"""

    _atomic_write(
        result.result_path,
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _load_apply_result(path: Path, tenant_id: str) -> SyncApplyResult:
    """读取 apply 结果并确认租户、操作类型和回滚快照格式。"""

    try:
        result = SyncApplyResult.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ExpertSyncRollbackError(f"Invalid sync apply result: {exc}") from exc
    if result.operation != "apply":
        raise ExpertSyncRollbackError("Sync rollback requires an apply result")
    if result.tenant_id != tenant_id:
        raise ExpertSyncRollbackError("Sync apply result tenant does not match command tenant")
    return result


def _iter_table_models() -> list[type[SQLModel]]:
    """列出当前模型注册表，便于新增加的 Agent 引用也进入删除保护。"""

    discovered: list[type[SQLModel]] = []
    visited: set[type[SQLModel]] = set()

    def visit(parent: type[SQLModel]) -> None:
        """递归收集 SQLModel 表类。"""

        for model in parent.__subclasses__():
            if model in visited:
                continue
            visited.add(model)
            table = getattr(model, "__table__", None)
            if table is not None:
                discovered.append(model)
            visit(model)

    visit(SQLModel)
    return discovered


def _dependent_reference(
    db: Session,
    tenant_id: str,
    agent_id: str,
) -> str | None:
    """查找所有已注册表中的 Agent 引用，返回首个阻止回滚的字段。"""

    reference_columns = ("agent_id", "target_agent_id", "adopted_agent_id", "source_agent_id")
    for model in _iter_table_models():
        table = model.__table__
        if "tenant_id" not in table.c or "id" not in table.c:
            continue
        for column_name in reference_columns:
            if column_name not in table.c:
                continue
            statement = (
                select(table.c.id)
                .where(
                    table.c.tenant_id == tenant_id,
                    table.c[column_name] == agent_id,
                )
                .limit(1)
            )
            if db.exec(statement).first() is not None:
                return f"{table.name}.{column_name}"
    return None


def _current_agent(
    db: Session,
    tenant_id: str,
    item: SyncApplyItem,
) -> AgentProfile | None:
    """按租户和结果中的主键读取当前专家，防止跨租户回滚。"""

    if not item.agent_id:
        return None
    return db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == item.agent_id,
        )
        .with_for_update()
    ).first()


def _guard_reason(item: SyncApplyItem, agent: AgentProfile) -> str | None:
    """核对 apply 后版本、内容和元数据摘要，拒绝回滚后续改动。"""

    if item.profile_revision is None or agent.profile_revision != item.profile_revision:
        return "profile_revision changed after apply"
    if item.updated_at is None or agent.updated_at.isoformat() != item.updated_at:
        return "updated_at changed after apply"
    if item.applied_content_sha256 is None:
        return "apply result has no content snapshot digest"
    if profile_content_sha256(agent) != item.applied_content_sha256:
        return "agent content changed after apply"
    if item.applied_metadata_sha256 is None:
        return "apply result has no metadata snapshot digest"
    if metadata_sha256(agent.metadata_json) != item.applied_metadata_sha256:
        return "agent metadata changed after apply"
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    if metadata.get("upstream_path") != item.upstream_path:
        return "agent source path changed after apply"
    return None


def _restore_snapshot(
    agent: AgentProfile,
    snapshot: SyncAgentSnapshot,
    apply_result: SyncApplyResult,
) -> None:
    """恢复同步前字段并递增修订号，避免旧会话因回滚重新变得有效。"""

    agent.name = snapshot.name
    agent.description = snapshot.description
    agent.persona_prompt = snapshot.persona_prompt
    agent.original_name = snapshot.original_name
    agent.original_description = snapshot.original_description
    agent.original_persona_prompt = snapshot.original_persona_prompt
    agent.original_locale = snapshot.original_locale
    agent.metadata_json = dict(snapshot.metadata_json)
    agent.metadata_json.update(
        {
            "expert_sync_status": "rollback_restored",
            "expert_sync_rollback_at": _iso_now(),
            "expert_sync_rollback_source_commit": apply_result.source_commit,
        }
    )
    agent.profile_revision = max(agent.profile_revision + 1, snapshot.profile_revision + 1)
    agent.updated_at = utc_now()


def rollback_sync_result(
    db_factory: SessionFactory,
    apply_result_path: Path,
    tenant_id: str,
    admin_username: str,
    *,
    output_path: Path | None = None,
) -> SyncRollbackResult:
    """按 apply 快照执行可重放回滚，遇到发布、引用或并发修改时逐项安全跳过。"""

    apply_result_path = apply_result_path.expanduser().resolve(strict=True)
    try:
        apply_result = _load_apply_result(apply_result_path, tenant_id)
        result_path = (
            output_path.expanduser().resolve()
            if output_path is not None
            else apply_result_path.parent
            / f"sync-rollback-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
        )
        if result_path == apply_result_path:
            raise ExpertSyncRollbackError("Rollback result must not overwrite apply result")
        if result_path.exists():
            raise ExpertSyncRollbackError(f"Sync rollback output already exists: {result_path}")
        with db_factory() as db:
            validate_admin(db, tenant_id, admin_username)
    except (ExpertImportError, ExpertSyncRollbackError, OSError, ValueError) as exc:
        raise ExpertSyncRollbackError(str(exc)) from exc

    result = SyncRollbackResult(
        tenant_id=tenant_id,
        apply_result_path=apply_result_path,
        source_batch_id=apply_result.source_batch_id,
        source_commit=apply_result.source_commit,
        started_at=_iso_now(),
        result_path=result_path,
    )
    _write_result(result)
    items: list[SyncRollbackItem] = []
    for item in apply_result.items:
        if item.status not in {"created", "updated", "metadata_updated", "pending"}:
            rollback_item = SyncRollbackItem(
                upstream_path=item.upstream_path,
                status="skipped_not_applied",
                agent_id=item.agent_id,
                message=f"apply 状态 {item.status} 没有可回滚业务写入",
            )
        elif item.agent_id is None:
            rollback_item = SyncRollbackItem(
                upstream_path=item.upstream_path,
                status="skipped_not_applied",
                message="apply 结果缺少专家主键",
            )
        elif item.status in {"updated", "metadata_updated", "pending"} and item.previous_state is None:
            rollback_item = SyncRollbackItem(
                upstream_path=item.upstream_path,
                status="skipped_no_snapshot",
                agent_id=item.agent_id,
                message="apply 结果缺少更新前快照，拒绝猜测恢复内容",
            )
        else:
            with db_factory() as db:
                try:
                    agent = _current_agent(db, tenant_id, item)
                    if agent is None:
                        rollback_item = SyncRollbackItem(
                            upstream_path=item.upstream_path,
                            status="skipped_not_applied",
                            agent_id=item.agent_id,
                            message="apply 后专家不存在或已被删除",
                        )
                    else:
                        guard = _guard_reason(item, agent)
                        dependency = _dependent_reference(db, tenant_id, agent.id)
                        if guard or agent_is_published(agent) or dependency:
                            reasons = [reason for reason in (guard, dependency) if reason]
                            if agent_is_published(agent):
                                reasons.append("agent is published")
                            rollback_item = SyncRollbackItem(
                                upstream_path=item.upstream_path,
                                status="skipped_modified_or_used",
                                agent_id=agent.id,
                                profile_revision=agent.profile_revision,
                                updated_at=agent.updated_at.isoformat(),
                                message="；".join(reasons),
                            )
                        elif item.status == "created":
                            db.delete(agent)
                            db.commit()
                            rollback_item = SyncRollbackItem(
                                upstream_path=item.upstream_path,
                                status="deleted",
                                agent_id=agent.id,
                            )
                        else:
                            snapshot = item.previous_state
                            if snapshot is None:
                                raise ExpertSyncRollbackError("rollback snapshot disappeared")
                            _restore_snapshot(agent, snapshot, apply_result)
                            db.add(agent)
                            db.commit()
                            db.refresh(agent)
                            rollback_item = SyncRollbackItem(
                                upstream_path=item.upstream_path,
                                status="restored",
                                agent_id=agent.id,
                                profile_revision=agent.profile_revision,
                                updated_at=agent.updated_at.isoformat(),
                            )
                except ExpertSyncRollbackError:
                    db.rollback()
                    raise
                except (SQLAlchemyError, ValueError) as exc:
                    db.rollback()
                    rollback_item = SyncRollbackItem(
                        upstream_path=item.upstream_path,
                        status="failed",
                        agent_id=item.agent_id,
                        message=str(exc),
                    )
        items.append(rollback_item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result)
    return result
