"""
@Time       : 2026/08/03 23:24
@Author     : zhanglp8181
@File       : execution_context.py
@CallChain  : DynamicTaskAgent resume/planning → database projection → provider execution view
@Description: 从权威执行表投影动态任务上下文，显式披露模型侧裁剪且不污染原始回执。
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
    memory_context: tuple[dict[str, Any], ...] = ()
    conversation_context: tuple[dict[str, Any], ...] = ()
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
        memory_context=tuple(
            dict(item)
            for item in (instance.context_json or {}).get("memory_context", [])
            if isinstance(item, dict)
        ),
        conversation_context=tuple(
            dict(item)
            for item in (instance.context_json or {}).get("conversation_context", [])
            if isinstance(item, dict)
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
    """只回注探索报告与证据引用，报告再次裁剪时明确说明而不冒充完整内容。"""

    if not isinstance(output, dict):
        return None
    report = output.get("report")
    evidence = output.get("evidence")
    limitations = output.get("limitations")
    if not isinstance(report, str) or not isinstance(evidence, list):
        return None
    return {
        "report": _clip_result_text(report),
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


def _clip_result_text(value: str) -> str:
    """在既有字符预算内保留正文前缀及裁剪说明，不把模型可见部分称为全文。"""

    if len(value) <= _MAX_RESULT_STRING_CHARS:
        return value
    notice = (
        f"\n[模型上下文已截断：原始字段共 {len(value)} 字符，"
        "仅展示开头内容；不得视为全文。]"
    )
    return value[:_MAX_RESULT_STRING_CHARS - len(notice)] + notice


def _mark_projected_truncation(
    original: dict[str, Any], projected: dict[str, object],
) -> dict[str, object]:
    """只更新合同已经允许的相邻截断标记，不新增字段或修改原始 Provider 回执。"""

    any_clipped = False
    for key in projected:
        item = original.get(key)
        clipped = (
            isinstance(item, str) and len(item) > _MAX_RESULT_STRING_CHARS
        ) or (isinstance(item, list) and len(item) > _MAX_RESULT_ITEMS)
        flag = f"{key}_truncated"
        any_clipped = any_clipped or clipped
        if clipped and isinstance(projected.get(flag), bool):
            projected[flag] = True
    if any_clipped:
        for flag in ("truncated", "partial"):
            if isinstance(projected.get(flag), bool):
                projected[flag] = True
    return projected


def _project_result_by_schema(value: object, schema: dict[str, Any], *, depth: int) -> object:
    """按显式合同过滤凭据与侧带；有界裁剪必须披露，不能沿用失真的完整正文标记。"""

    if depth >= _MAX_RESULT_DEPTH:
        return "[result depth clipped]"
    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        projected: dict[str, object] = {}
        if isinstance(properties, dict):
            for key, child_schema in list(properties.items())[:_MAX_RESULT_ITEMS]:
                normalized = str(key).lower()
                if (
                    key in value
                    and isinstance(child_schema, dict)
                    and not str(key).startswith("_")
                    and normalized not in _SENSITIVE_RESULT_KEYS
                ):
                    projected[str(key)] = _project_result_by_schema(
                        value[key], child_schema, depth=depth + 1
                    )
        # Agent-Reach 的若干公开结果使用 ``{"type": "object"}`` 表示
        # 供应商返回的开放对象，没有可枚举的 properties。旧逻辑会把这类
        # repository 等真实对象投影成空对象，导致 answer 阶段误报“无数据”。
        # 只对未声明 properties 且未显式禁止附加字段的开放对象放行有界脱敏
        # 投影；显式 properties schema 仍保持原有白名单语义。
        open_object = properties is None and "additionalProperties" not in schema
        if schema.get("additionalProperties") is True or open_object:
            for key, item in list(value.items())[:_MAX_RESULT_ITEMS]:
                normalized = str(key).lower()
                if key in projected or str(key).startswith("_") or normalized in _SENSITIVE_RESULT_KEYS:
                    continue
                projected[str(key)] = _sanitize_unstructured_result(item, depth=depth + 1)
        return _mark_projected_truncation(value, projected)
    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            # 未声明 items 的数组仍是一个有意义的开放集合；逐项走同一
            # 深度、数量和敏感字段裁剪，不能因为 schema 不够细就静默清空。
            return [
                _sanitize_unstructured_result(item, depth=depth + 1)
                for item in value[:_MAX_RESULT_ITEMS]
            ]
        return [
            _project_result_by_schema(item, item_schema, depth=depth + 1)
            for item in value[:_MAX_RESULT_ITEMS]
        ]
    if isinstance(value, str):
        return _clip_result_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip_result_text(str(value))


def _sanitize_unstructured_result(value: object, *, depth: int) -> object:
    """脱敏开放合同并显式披露正文裁剪，保留原始回执且不扩大模型预算。"""

    if depth >= _MAX_RESULT_DEPTH:
        return "[result depth clipped]"
    if isinstance(value, dict):
        projected = {
            str(key): _sanitize_unstructured_result(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_RESULT_ITEMS]
            if not str(key).startswith("_") and str(key).lower() not in _SENSITIVE_RESULT_KEYS
        }
        return _mark_projected_truncation(value, projected)
    if isinstance(value, list):
        return [
            _sanitize_unstructured_result(item, depth=depth + 1)
            for item in value[:_MAX_RESULT_ITEMS]
        ]
    if isinstance(value, str):
        return _clip_result_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip_result_text(str(value))
