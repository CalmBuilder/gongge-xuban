"""
@Time       : 2026/07/22 13:35
@Author     : zhanglp8181
@File       : scheduler.py
@CallChain  : Runtime Coordinator → plan_next_action → 输入等待/服务命令/确定性分支/结束
@Description: 基于统一 SOP 定义和结构化回执计算无副作用、可重复的下一执行动作。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping

from pydantic import Field

from app.sop_runtime.condition_dsl import evaluate_condition, parse_condition_dsl
from app.sop_runtime.contracts import RuntimeContract
from app.sop_runtime.definition import (
    CanonicalEdge,
    CollectInputNode,
    CompiledSopDefinition,
    ConditionLanguage,
    DecisionNode,
    HumanTaskKind,
    HumanTaskNode,
    ServiceTaskKind,
    ServiceTaskNode,
    TerminalNode,
)


class RuntimeAction(StrEnum):
    """调度器可以要求应用层执行的封闭动作集合。"""

    WAIT_INPUT = "wait_input"
    WAIT_WORK_ITEM = "wait_work_item"
    WAIT_OPERATION = "wait_operation"
    ADVANCE = "advance"
    CALL_TOOL = "call_tool"
    QUERY_KNOWLEDGE = "query_knowledge"
    COMPLETE = "complete"
    FAIL = "fail"


class RuntimePlan(RuntimeContract):
    """调度器的结构化决定，不包含数据库或网络副作用。"""

    action: RuntimeAction
    node_id: str
    next_node_id: str | None = None
    expected_inputs: tuple[str, ...] = ()
    operation_name: str | None = None
    operation_arguments: dict[str, object] = Field(default_factory=dict)
    result_key: str | None = None
    outcome: str | None = None
    error_code: str | None = None
    error_path: str | None = None
    control_reply: str | None = None


def plan_next_action(
    definition: CompiledSopDefinition,
    *,
    current_node_id: str,
    slots: Mapping[str, object],
    tool_results: Mapping[str, object] | None = None,
    node_outputs: Mapping[str, object] | None = None,
    work_items: Mapping[str, object] | None = None,
    policy_results: Mapping[str, object] | None = None,
) -> RuntimePlan:
    """根据冻结定义和数据快照计算下一动作，相同输入始终返回相同结果。"""

    nodes = {node.node_id: node for node in definition.nodes}
    node = nodes.get(current_node_id)
    if node is None:
        return _failure(current_node_id, "RUNTIME_NODE_NOT_FOUND")
    runtime_data = {
        "slots": slots,
        "node_output": node_outputs or {},
        "tool_result": tool_results or {},
        "work_item": work_items or {},
        "policy_result": policy_results or {},
    }
    if isinstance(node, CollectInputNode):
        missing = tuple(
            field for field in node.config.required_inputs if not _has_value(slots, field)
        )
        if missing:
            return RuntimePlan(
                action=RuntimeAction.WAIT_INPUT,
                node_id=node.node_id,
                expected_inputs=missing,
                control_reply=(
                    node.config.confirmation_policy.prompt
                    if node.config.confirmation_policy is not None
                    else None
                ),
            )
        return _route(definition, node.node_id, runtime_data)
    if isinstance(node, DecisionNode):
        return _route(definition, node.node_id, runtime_data)
    if isinstance(node, ServiceTaskNode):
        if node.config.kind is ServiceTaskKind.TOOL:
            return _plan_tool(definition, node, runtime_data)
        if node.config.kind is ServiceTaskKind.KNOWLEDGE:
            return _plan_knowledge(definition, node, runtime_data)
        return _failure(node.node_id, "RUNTIME_SERVICE_KIND_NOT_EXECUTABLE")
    if isinstance(node, HumanTaskNode):
        if node.config.kind is not HumanTaskKind.STRUCTURED_WORK_ITEM:
            return _failure(node.node_id, "RUNTIME_HUMAN_TASK_KIND_NOT_EXECUTABLE")
        work_item = runtime_data["work_item"]
        if not isinstance(work_item, Mapping) or work_item.get("status") != "completed":
            return RuntimePlan(
                action=RuntimeAction.WAIT_WORK_ITEM,
                node_id=node.node_id,
                control_reply=node.config.waiting_message,
            )
        if work_item.get("outcome") not in node.config.allowed_outcomes:
            return _failure(node.node_id, "RUNTIME_WORK_ITEM_OUTCOME_INVALID")
        return _route(definition, node.node_id, runtime_data)
    if isinstance(node, TerminalNode):
        return RuntimePlan(
            action=RuntimeAction.COMPLETE,
            node_id=node.node_id,
            outcome=node.config.outcome,
        )
    return _failure(node.node_id, "RUNTIME_NODE_TYPE_NOT_EXECUTABLE")


def _plan_tool(
    definition: CompiledSopDefinition,
    node: ServiceTaskNode,
    runtime_data: Mapping[str, object],
) -> RuntimePlan:
    """准备单工具调用，或根据已经存在的结构化回执继续路由。"""

    if len(node.config.operations) != 1 or not node.config.result_key:
        return _failure(node.node_id, "RUNTIME_TOOL_OPERATION_INVALID")
    operation_name = node.config.operations[0]
    tool_results = runtime_data["tool_result"]
    receipt = (
        tool_results.get(node.config.result_key)
        if isinstance(tool_results, Mapping)
        else None
    )
    if not isinstance(receipt, Mapping):
        arguments: dict[str, object] = {}
        for argument_name, path in node.config.input_mapping.items():
            value, found = _resolve_path(runtime_data, path)
            if found and value is not None:
                arguments[argument_name] = value
        return RuntimePlan(
            action=RuntimeAction.CALL_TOOL,
            node_id=node.node_id,
            operation_name=operation_name,
            operation_arguments=arguments,
            result_key=node.config.result_key,
        )
    return _route(definition, node.node_id, runtime_data)


def _plan_knowledge(
    definition: CompiledSopDefinition,
    node: ServiceTaskNode,
    runtime_data: Mapping[str, object],
) -> RuntimePlan:
    """等待持久化知识回执，或从白名单输入映射生成有界查询计划。"""

    config = node.config
    if config.knowledge_query is None or not config.result_key:
        return _failure(node.node_id, "RUNTIME_KNOWLEDGE_DEFINITION_INVALID")
    node_outputs = runtime_data["node_output"]
    receipt = (
        node_outputs.get(config.result_key)
        if isinstance(node_outputs, Mapping)
        else None
    )
    if isinstance(receipt, Mapping):
        return _route(definition, node.node_id, runtime_data)

    query_parts = [part.strip() for part in (node.name, node.instruction) if part.strip()]
    for argument_name, path in config.input_mapping.items():
        value, found = _resolve_path(runtime_data, path)
        if not found or value is None:
            continue
        if not isinstance(value, str | int | float | bool):
            return _failure(
                node.node_id,
                "RUNTIME_KNOWLEDGE_INPUT_INVALID",
                error_path=path,
            )
        query_parts.append(f"{argument_name}: {value}")
    if not query_parts:
        return _failure(node.node_id, "RUNTIME_KNOWLEDGE_QUERY_EMPTY")

    knowledge_config = config.knowledge_query
    return RuntimePlan(
        action=RuntimeAction.QUERY_KNOWLEDGE,
        node_id=node.node_id,
        operation_name="knowledge.search",
        operation_arguments={
            "query": "\n".join(query_parts),
            "query_type": knowledge_config.query_type,
            "desired_evidence": knowledge_config.desired_evidence,
            "max_chunks": knowledge_config.max_chunks,
            "max_depth": knowledge_config.max_depth,
        },
        result_key=config.result_key,
    )


def _route(
    definition: CompiledSopDefinition,
    source_node_id: str,
    runtime_data: Mapping[str, object],
) -> RuntimePlan:
    """按优先级执行受限条件；单一无条件连线直接推进。"""

    outgoing = sorted(
        (edge for edge in definition.edges if edge.source_node_id == source_node_id),
        key=lambda edge: edge.priority,
        reverse=True,
    )
    if len(outgoing) == 1 and outgoing[0].condition is None:
        return _advance(source_node_id, outgoing[0])
    if not outgoing:
        return _failure(source_node_id, "RUNTIME_ROUTE_NOT_FOUND")
    for edge in outgoing:
        condition = edge.condition
        if condition is None or condition.language is not ConditionLanguage.RESTRICTED_DSL:
            return _failure(source_node_id, "RUNTIME_LEGACY_CONDITION_UNSUPPORTED")
        result = evaluate_condition(parse_condition_dsl(condition.ast), runtime_data)
        if result.error_code is not None:
            return RuntimePlan(
                action=RuntimeAction.FAIL,
                node_id=source_node_id,
                error_code=result.error_code.value,
                error_path=result.error_path,
            )
        if result.matched:
            return _advance(source_node_id, edge)
    return _failure(source_node_id, "RUNTIME_DEFAULT_ROUTE_NOT_FOUND")


def _advance(source_node_id: str, edge: CanonicalEdge) -> RuntimePlan:
    """把命中的规范连线转换为显式节点推进计划。"""

    return RuntimePlan(
        action=RuntimeAction.ADVANCE,
        node_id=source_node_id,
        next_node_id=edge.target_node_id,
    )


def _failure(
    node_id: str,
    error_code: str,
    *,
    error_path: str | None = None,
) -> RuntimePlan:
    """生成具有稳定错误码的失败计划，避免抛出解释器异常。"""

    return RuntimePlan(
        action=RuntimeAction.FAIL,
        node_id=node_id,
        error_code=error_code,
        error_path=error_path,
    )


def _has_value(slots: Mapping[str, object], field: str) -> bool:
    """判断必填槽位存在且不是空字符串。"""

    value = slots.get(field)
    return value is not None and value != ""


def _resolve_path(data: Mapping[str, object], path: str) -> tuple[object, bool]:
    """只通过映射键解析显式数据绑定路径，不允许属性访问和表达式求值。"""

    current: object = data
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True
