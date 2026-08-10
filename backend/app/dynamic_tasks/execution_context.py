"""
@Time       : 2026/08/03 23:24
@Author     : zhanglp8181
@File       : execution_context.py
@CallChain  : DynamicTaskAgent resume/planning → database projection → provider execution view
@Description: 从权威执行表机械生成不受对话语义压缩覆盖的动态任务上下文。
"""

from __future__ import annotations

from collections.abc import Mapping
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


_SENSITIVE_RESULT_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "credentials", "password", "secret", "token"}
)
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_ITEMS = 100
_MAX_RESULT_STRING_CHARS = 12_000


class ExecutionContextProjection(BaseModel):
    """保存 provider 组装前的机械执行事实，不包含 locator、凭据或 audit-only 数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    execution_revision: int
    goal: dict[str, Any]
    plan_revision_id: str
    plan_checksum: str
    constraints: tuple[str, ...]
    success_criteria: tuple[dict[str, Any], ...]
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
    success_criteria = revision.plan_json.get("success_criteria", [])
    if (
        not isinstance(plan_steps, list)
        or not isinstance(constraints, list)
        or not isinstance(success_criteria, list)
    ):
        raise ValueError("动态 Execution 当前计划已损坏。")
    operation_by_node_id = {item.node_execution_id: item for item in operations}
    capability_by_name = _capability_snapshots_by_name(instance)
    completed_items: list[dict[str, Any]] = []
    for step in steps:
        if step.status != "succeeded":
            continue
        operation = operation_by_node_id.get(step.id)
        model_output = (
            _model_visible_explore_output(step.output_json)
            if step.step_kind == "explore"
            else _model_visible_operation_output(
                operation,
                capability_by_name=capability_by_name,
            )
        )
        completed_items.append(
            {
                "step_key": step.step_key,
                "attempt": step.attempt,
                "plan_revision_id": step.plan_revision_id,
                "output_ref": {
                    "node_execution_id": step.id,
                    "checksum": canonical_checksum(step.output_json),
                },
                "model_output": model_output,
            }
        )
    completed = tuple(completed_items)
    pending = proposals[-1] if proposals else None
    return ExecutionContextProjection(
        execution_id=instance.id,
        execution_revision=instance.revision,
        goal=dict(instance.goal_snapshot_json or {}),
        plan_revision_id=revision.id,
        plan_checksum=revision.checksum,
        constraints=tuple(str(value) for value in constraints),
        success_criteria=tuple(
            dict(value) for value in success_criteria if isinstance(value, dict)
        ),
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


def _capability_snapshots_by_name(instance: SopInstance) -> dict[str, dict[str, Any]]:
    """从冻结目录按能力名索引工具、连接器和知识快照，不读取 audit view。"""

    indexed: dict[str, dict[str, Any]] = {}
    snapshot = instance.capability_snapshot_json or {}
    for group in ("tools", "connectors", "knowledge"):
        values = snapshot.get(group)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                indexed[item["name"]] = item
    return indexed


def _model_visible_operation_output(
    operation: SopOperation | None,
    *,
    capability_by_name: dict[str, dict[str, Any]],
) -> object | None:
    """按冻结能力的 output schema 回注工具结果，剥离未声明字段和展示侧带。"""

    if operation is None or operation.status != "succeeded":
        return None
    snapshot = capability_by_name.get(operation.operation_name)
    model_view = snapshot.get("model_view") if isinstance(snapshot, dict) else None
    schema = model_view.get("output_schema") if isinstance(model_view, dict) else None
    data = (operation.result_json or {}).get("data")
    if not isinstance(schema, dict):
        return None
    return _project_result_by_schema(data, schema, depth=0)


def project_result_for_model(value: object, schema: Mapping[str, Any]) -> object:
    """按能力发布的 output schema 投影探索回执，禁止临时上下文看到完整 adapter 侧带。"""

    return _project_result_by_schema(value, dict(schema), depth=0)


def _model_visible_explore_output(output: object) -> dict[str, object] | None:
    """只把探索压缩报告与证据引用回注父上下文，排除中间工具大结果。"""

    if not isinstance(output, dict):
        return None
    report = output.get("report")
    evidence = output.get("evidence")
    limitations = output.get("limitations")
    if not isinstance(report, str) or not isinstance(evidence, list):
        return None
    return {
        "report": report[:_MAX_RESULT_STRING_CHARS],
        "evidence": [
            {
                "operation_id": str(item.get("operation_id") or "")[:512],
                "capability_ref": str(item.get("capability_ref") or "")[:256],
            }
            for item in evidence[:50]
            if isinstance(item, dict)
        ],
        "limitations": [
            str(item)[:1000]
            for item in (limitations if isinstance(limitations, list) else [])[:20]
        ],
    }


def _project_result_by_schema(value: object, schema: dict[str, Any], *, depth: int) -> object:
    """递归投影显式 JSON Schema；开放对象仍过滤凭据名、私有侧带并施加体积边界。"""

    if depth >= _MAX_RESULT_DEPTH:
        return "[result depth clipped]"
    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        projected: dict[str, object] = {}
        if isinstance(properties, dict):
            for key, child_schema in list(properties.items())[:_MAX_RESULT_ITEMS]:
                if key in value and isinstance(child_schema, dict):
                    projected[str(key)] = _project_result_by_schema(
                        value[key], child_schema, depth=depth + 1
                    )
        if schema.get("additionalProperties") is True:
            for key, item in list(value.items())[:_MAX_RESULT_ITEMS]:
                normalized = str(key).lower()
                if key in projected or str(key).startswith("_") or normalized in _SENSITIVE_RESULT_KEYS:
                    continue
                projected[str(key)] = _sanitize_unstructured_result(item, depth=depth + 1)
        return projected
    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return []
        return [
            _project_result_by_schema(item, item_schema, depth=depth + 1)
            for item in value[:_MAX_RESULT_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_MAX_RESULT_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_RESULT_STRING_CHARS]


def _sanitize_unstructured_result(value: object, *, depth: int) -> object:
    """裁剪显式开放 schema 下的结构化值，并阻止常见凭据字段进入模型上下文。"""

    if depth >= _MAX_RESULT_DEPTH:
        return "[result depth clipped]"
    if isinstance(value, dict):
        return {
            str(key): _sanitize_unstructured_result(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_RESULT_ITEMS]
            if not str(key).startswith("_") and str(key).lower() not in _SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [
            _sanitize_unstructured_result(item, depth=depth + 1)
            for item in value[:_MAX_RESULT_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_MAX_RESULT_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_RESULT_STRING_CHARS]
