"""
@Time       : 2026/08/03 23:24
@Author     : zhanglp8181
@File       : execution_context.py
@CallChain  : DynamicTaskAgent resume/planning → database projection → provider execution view
@Description: 从权威执行表机械生成不受对话语义压缩覆盖的动态任务上下文。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.db.models import (
    ActionProposalRecord,
    ExecutionPlanRevision,
    InputResourceSnapshot,
    SopInstance,
    SopNodeExecution,
    SopOperation,
)
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planning import canonical_checksum


class ExecutionContextProjection(BaseModel):
    """保存 provider 组装前的机械执行事实，不包含 locator、凭据或 audit-only 数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    execution_revision: int
    goal: dict[str, Any]
    plan_revision_id: str
    plan_checksum: str
    constraints: tuple[str, ...]
    plan_steps: tuple[dict[str, Any], ...]
    completed_steps: tuple[dict[str, Any], ...]
    pending_action: dict[str, Any] | None
    operations: tuple[dict[str, Any], ...]
    input_resources: tuple[dict[str, Any], ...]
    attention_refs: tuple[str, ...] = ()


def build_execution_context_projection(
    db: Session,
    *,
    tenant_id: str,
    execution_id: str,
) -> ExecutionContextProjection:
    """按 tenant/execution 查询当前计划及子事实，跨租户统一表现为不可用。"""

    instance = db.get(SopInstance, execution_id)
    if instance is None or instance.tenant_id != tenant_id or instance.kind != "dynamic_task":
        raise ValueError("动态 Execution 不可用。")
    revision = db.get(ExecutionPlanRevision, instance.current_plan_revision_id)
    if (
        revision is None
        or revision.tenant_id != tenant_id
        or revision.execution_id != instance.id
        or revision.status != "active"
        or revision.checksum != instance.current_plan_checksum
        or canonical_checksum(revision.plan_json) != revision.checksum
        or capability_checksum(revision.capability_snapshot_json)
        != revision.capability_checksum
        or revision.capability_checksum != instance.capability_checksum
    ):
        raise ValueError("动态 Execution 当前计划不可用。")
    steps = db.exec(
        select(SopNodeExecution)
        .where(
            SopNodeExecution.tenant_id == tenant_id,
            SopNodeExecution.instance_id == execution_id,
        )
        .order_by(SopNodeExecution.created_at, SopNodeExecution.id)
    ).all()
    proposals = db.exec(
        select(ActionProposalRecord)
        .where(
            ActionProposalRecord.tenant_id == tenant_id,
            ActionProposalRecord.execution_id == execution_id,
            ActionProposalRecord.status == "validated",
        )
        .order_by(ActionProposalRecord.created_at, ActionProposalRecord.id)
    ).all()
    operations = db.exec(
        select(SopOperation)
        .where(
            SopOperation.tenant_id == tenant_id,
            SopOperation.instance_id == execution_id,
        )
        .order_by(SopOperation.created_at, SopOperation.id)
    ).all()
    resources = db.exec(
        select(InputResourceSnapshot)
        .where(
            InputResourceSnapshot.tenant_id == tenant_id,
            InputResourceSnapshot.execution_id == execution_id,
        )
        .order_by(InputResourceSnapshot.created_at, InputResourceSnapshot.id)
    ).all()
    for resource in resources:
        expected_identity = capability_checksum(
            {
                "source_type": resource.source_type,
                "source_resource_id": resource.source_resource_id,
                "source_version": resource.source_version,
                "content_checksum": resource.content_checksum,
            }
        )
        if expected_identity != resource.identity_checksum:
            raise ValueError("动态 Execution 输入资源快照已损坏。")
    plan_steps = revision.plan_json.get("steps", [])
    constraints = revision.plan_json.get("constraints", [])
    if not isinstance(plan_steps, list) or not isinstance(constraints, list):
        raise ValueError("动态 Execution 当前计划已损坏。")
    completed = tuple(
        {
            "step_key": step.step_key,
            "attempt": step.attempt,
            "plan_revision_id": step.plan_revision_id,
            "output_ref": {
                "node_execution_id": step.id,
                "checksum": canonical_checksum(step.output_json),
            },
        }
        for step in steps
        if step.status == "succeeded"
    )
    pending = proposals[-1] if proposals else None
    return ExecutionContextProjection(
        execution_id=instance.id,
        execution_revision=instance.revision,
        goal=dict(instance.goal_snapshot_json or {}),
        plan_revision_id=revision.id,
        plan_checksum=revision.checksum,
        constraints=tuple(str(value) for value in constraints),
        plan_steps=tuple(dict(value) for value in plan_steps if isinstance(value, dict)),
        completed_steps=completed,
        pending_action=(
            {
                "proposal_id": pending.id,
                "step_key": pending.step_key,
                "checksum": pending.proposal_checksum,
                "proposal": pending.normalized_proposal_json,
            }
            if pending is not None
            else None
        ),
        operations=tuple(
            {
                "logical_action_id": operation.logical_action_id,
                "status": operation.status,
                "effect_state": operation.effect_state,
                "capability_checksum": operation.capability_checksum,
            }
            for operation in operations
        ),
        input_resources=tuple(
            {
                "snapshot_id": resource.id,
                "source_type": resource.source_type,
                "source_resource_id": resource.source_resource_id,
                "source_version": resource.source_version,
                "content_checksum": resource.content_checksum,
                "extraction_checksum": resource.extraction_checksum,
                "filename": resource.filename,
                "mime_type": resource.mime_type,
                "size_bytes": resource.size_bytes,
                "ingestion_status": resource.ingestion_status,
            }
            for resource in resources
        ),
    )
