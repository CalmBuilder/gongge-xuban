"""
@Time       : 2026/07/22 05:16
@Author     : zhanglp8181
@File       : legacy_skill_card_adapter.py
@CallChain  : SkillCard 发布/兼容扫描 → compile_legacy_skill_card → CompiledSopDefinition
@Description: 将旧版 SkillCard 图编译为统一 SOP 定义并集中隔离历史自由字符串语义。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.skills.skill_schema import SkillCard, SkillGraphNode
from app.sop_runtime.capabilities import CapabilityRegistry, DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.condition_dsl import (
    CONDITION_PATH_PATTERN,
    ConditionBranch,
    ConditionCompilationError,
    compile_condition_branches,
    compile_condition_dsl,
    parse_condition_dsl,
)
from app.sop_runtime.definition import (
    AuthenticatedInputBinding,
    CanonicalEdge,
    CanonicalNode,
    CompilationDiagnostic,
    CompiledSopDefinition,
    ConditionExpression,
    RestrictedConditionExpression,
    DecisionConfig,
    DecisionNode,
    DiagnosticSeverity,
    ExplicitConfirmationPolicy,
    HumanTaskConfig,
    HumanTaskKind,
    ParticipantScopeResolver,
    HumanTaskNode,
    KnowledgeTaskConfig,
    NodeType,
    CollectInputConfig,
    CollectInputNode,
    ServiceTaskConfig,
    ServiceTaskKind,
    ServiceTaskNode,
    SourceDefinitionFormat,
    TerminalConfig,
    TerminalNode,
    WorkItemOutcomeOption,
)
from app.sop_runtime.contracts import (
    CompletionMode,
    TimeoutAction,
    TimeoutPolicy,
    WorkItemCompletionPolicy,
)


LEGACY_SKILL_CARD_SCHEMA_VERSION = 2
CURRENT_META_MODEL_VERSION = 1
IDENTITY_CONTEXT_META_MODEL_VERSION = 2
SLOT_VALUE_ALIASES_META_MODEL_VERSION = 3
EXPLICIT_CONFIRMATION_META_MODEL_VERSION = 4
KNOWLEDGE_SERVICE_META_MODEL_VERSION = 5
KNOWN_LEGACY_NODE_TYPES = frozenset(
    {
        "collect_info",
        "collect_input",
        "decision",
        "knowledge_query",
        "tool_call",
        "service_task",
        "handoff",
        "human_task",
        "response",
        "terminal",
    }
)


class SopCompilationError(ValueError):
    """定义存在阻断发布的结构、条件或能力错误。"""

    code = "SOP_DEFINITION_COMPILATION_FAILED"

    def __init__(self, diagnostics: tuple[CompilationDiagnostic, ...]) -> None:
        """保存全部错误诊断，便于 API 一次返回完整修复清单。"""

        self.diagnostics = diagnostics
        summary = "; ".join(item.code for item in diagnostics)
        super().__init__(f"SOP definition compilation failed: {summary}")


def compile_legacy_skill_card(
    content: SkillCard | Mapping[str, Any],
    *,
    registry: CapabilityRegistry = DEFAULT_CAPABILITY_REGISTRY,
) -> CompiledSopDefinition:
    """校验旧版 SkillCard，并将其编译为 Runtime 唯一接受的规范定义。"""

    raw_content = _content_mapping(content)
    preflight_errors = _preflight_errors(raw_content)
    if preflight_errors:
        raise SopCompilationError(preflight_errors)
    try:
        card = SkillCard.model_validate(raw_content)
    except ValidationError as exc:
        raise SopCompilationError(
            (
                _error(
                    "INVALID_SKILL_CARD",
                    f"旧版 SkillCard 结构无效：{exc.errors(include_url=False)}",
                ),
            )
        ) from exc

    diagnostics: list[CompilationDiagnostic] = []
    raw_nodes = _raw_nodes_by_id(raw_content)
    terminal_ids = frozenset(card.terminal_node_ids)
    adjacency = _adjacency(card)
    _validate_graph(card, adjacency, diagnostics)
    _validate_restricted_branches(card, diagnostics)

    compiled_nodes: list[CanonicalNode] = []
    for node in card.nodes:
        raw_node = raw_nodes.get(node.node_id, {})
        compiled = _compile_node(
            node,
            raw_node,
            is_terminal=node.node_id in terminal_ids,
            outgoing_count=len(adjacency[node.node_id]),
            uses_restricted_decision=any(
                isinstance(edge.condition, Mapping)
                for edge in card.edges
                if edge.source_node_id == node.node_id
            ),
            condition_schemas=card.condition_schemas,
            diagnostics=diagnostics,
        )
        if compiled is None:
            continue
        _validate_capability(compiled, registry, diagnostics)
        compiled_nodes.append(compiled)

    compiled_edges = tuple(
        CanonicalEdge(
            source_node_id=edge.source_node_id,
            target_node_id=edge.next_node_id,
            condition=_condition(
                edge.condition,
                diagnostics,
                condition_schemas=card.condition_schemas,
                edge_index=index,
            ),
            priority=edge.priority,
            label=edge.label,
        )
        for index, edge in enumerate(card.edges)
    )
    errors = tuple(item for item in diagnostics if item.severity is DiagnosticSeverity.ERROR)
    if errors:
        raise SopCompilationError(errors)

    definition_payload = {
        "meta_model_version": _meta_model_version(compiled_nodes),
        "source_format": SourceDefinitionFormat.LEGACY_SKILL_CARD,
        "source_schema_version": LEGACY_SKILL_CARD_SCHEMA_VERSION,
        "skill_id": card.skill_id,
        "skill_version": card.version,
        "name": card.name,
        "start_node_id": card.start_node_id,
        "terminal_node_ids": tuple(card.terminal_node_ids),
        "nodes": tuple(compiled_nodes),
        "edges": compiled_edges,
        "diagnostics": tuple(
            item for item in diagnostics if item.severity is DiagnosticSeverity.WARNING
        ),
    }
    checksum = _definition_checksum(definition_payload)
    return CompiledSopDefinition(**definition_payload, checksum=checksum)


def _content_mapping(content: SkillCard | Mapping[str, Any]) -> dict[str, Any]:
    """复制输入定义，避免编译过程修改 API 或数据库持有的原对象。"""

    if isinstance(content, SkillCard):
        return content.model_dump(mode="json")
    return dict(content)


def _preflight_errors(content: Mapping[str, Any]) -> tuple[CompilationDiagnostic, ...]:
    """在 Pydantic 聚合校验前保留关键发布门禁的稳定领域错误码。"""

    diagnostics: list[CompilationDiagnostic] = []
    terminal_node_ids = content.get("terminal_node_ids")
    if not isinstance(terminal_node_ids, (list, tuple)) or not terminal_node_ids:
        diagnostics.append(_error("MISSING_TERMINAL", "SOP 定义必须声明至少一个终态节点。"))
    return tuple(diagnostics)


def _raw_nodes_by_id(content: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """按节点 ID 索引原始节点，以识别旧数据中缺失的 type 字段。"""

    indexed: dict[str, Mapping[str, Any]] = {}
    for raw_node in content.get("nodes") or []:
        if isinstance(raw_node, Mapping):
            node_id = str(raw_node.get("node_id") or "")
            if node_id:
                indexed[node_id] = raw_node
    return indexed


def _adjacency(card: SkillCard) -> dict[str, list[str]]:
    """构造包含无出边节点的邻接表，供可达性和终态校验复用。"""

    adjacency = {node.node_id: [] for node in card.nodes}
    for edge in card.edges:
        adjacency[edge.source_node_id].append(edge.next_node_id)
    return adjacency


def _validate_graph(
    card: SkillCard,
    adjacency: dict[str, list[str]],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """校验起点可达性、终态出边和每个节点到终态的路径完整性。"""

    reachable = _walk_forward(card.start_node_id, adjacency)
    all_node_ids = set(adjacency)
    for node_id in sorted(all_node_ids - reachable):
        diagnostics.append(_error("UNREACHABLE_NODE", "节点无法从起点到达。", node_id=node_id))

    for terminal_id in card.terminal_node_ids:
        if adjacency[terminal_id]:
            diagnostics.append(
                _error(
                    "TERMINAL_HAS_OUTGOING", "终态节点不能继续指向后继节点。", node_id=terminal_id
                )
            )

    reverse_adjacency: dict[str, list[str]] = defaultdict(list)
    for source, targets in adjacency.items():
        for target in targets:
            reverse_adjacency[target].append(source)
    can_reach_terminal: set[str] = set()
    queue = deque(card.terminal_node_ids)
    while queue:
        node_id = queue.popleft()
        if node_id in can_reach_terminal:
            continue
        can_reach_terminal.add(node_id)
        queue.extend(reverse_adjacency[node_id])
    for node_id in sorted(reachable - can_reach_terminal):
        diagnostics.append(
            _error("NO_PATH_TO_TERMINAL", "节点不存在通往任何声明终态的路径。", node_id=node_id)
        )


def _walk_forward(start_node_id: str, adjacency: dict[str, list[str]]) -> set[str]:
    """从起点遍历图并返回全部可达节点。"""

    visited: set[str] = set()
    queue = deque([start_node_id])
    while queue:
        node_id = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        queue.extend(adjacency[node_id])
    return visited


def _compile_node(
    node: SkillGraphNode,
    raw_node: Mapping[str, Any],
    *,
    is_terminal: bool,
    outgoing_count: int,
    uses_restricted_decision: bool,
    condition_schemas: Mapping[str, Mapping[str, object]],
    diagnostics: list[CompilationDiagnostic],
) -> CanonicalNode | None:
    """依据旧节点类型、动作和终态位置确定唯一规范节点原语。"""

    raw_type_value = raw_node.get("type")
    raw_type = str(raw_type_value).strip() if raw_type_value is not None else ""
    if raw_type and raw_type not in KNOWN_LEGACY_NODE_TYPES:
        diagnostics.append(
            _error(
                "UNKNOWN_NODE_TYPE",
                f"不支持的旧节点类型：{raw_type}",
                node_id=node.node_id,
            )
        )
        return None

    actions = tuple(str(action).strip() for action in node.allowed_actions if str(action).strip())
    operations = tuple(
        action.removeprefix("call_tool:") for action in actions if action.startswith("call_tool:")
    )
    inferred_type = _canonical_node_type(
        raw_type or node.type,
        actions=actions,
        expected_inputs=tuple(node.expected_user_info),
        is_terminal=is_terminal,
    )
    if operations and node.expected_user_info:
        diagnostics.append(
            _warning(
                "LEGACY_MIXED_NODE_REQUIRES_SPLIT",
                "旧节点同时收集输入并调用工具，迁移到确定性 Runtime 前必须拆分为两个节点。",
                node_id=node.node_id,
            )
        )
    activation_condition = _condition(
        node.condition,
        diagnostics,
        condition_schemas=condition_schemas,
        node_id=node.node_id,
    )
    common = {
        "node_id": node.node_id,
        "name": node.name,
        "instruction": node.instruction,
        "optional": node.optional,
        "activation_condition": activation_condition,
        "is_terminal": is_terminal,
    }
    if inferred_type is NodeType.COLLECT_INPUT:
        return CollectInputNode(
            **common,
            config=CollectInputConfig(
                required_inputs=tuple(node.expected_user_info),
                input_bindings=_input_bindings(
                    node.metadata,
                    tuple(node.expected_user_info),
                    diagnostics,
                    node_id=node.node_id,
                ),
                value_aliases=_value_aliases(
                    node.metadata,
                    tuple(node.expected_user_info),
                    diagnostics,
                    node_id=node.node_id,
                ),
                confirmation_policy=_confirmation_policy(
                    node.metadata,
                    tuple(node.expected_user_info),
                    diagnostics,
                    node_id=node.node_id,
                ),
            ),
        )
    if inferred_type is NodeType.DECISION:
        if outgoing_count < 2:
            diagnostics.append(
                _warning(
                    "DECISION_SINGLE_ROUTE",
                    "决策节点少于两条出边，发布后应复核是否需要降级为普通服务节点。",
                    node_id=node.node_id,
                )
            )
        capability = (
            "decision.restricted_dsl" if uses_restricted_decision else "decision.legacy_expression"
        )
        return DecisionNode(**common, config=DecisionConfig(capability=capability))
    if inferred_type is NodeType.HUMAN_TASK:
        human_task_config = _human_task_config(node.metadata, diagnostics, node.node_id)
        return HumanTaskNode(
            **common,
            config=human_task_config,
        )
    if inferred_type is NodeType.TERMINAL:
        if not is_terminal:
            diagnostics.append(
                _error(
                    "TERMINAL_TYPE_NOT_DECLARED",
                    "terminal 类型节点必须同时出现在 terminal_node_ids。",
                    node_id=node.node_id,
                )
            )
        return TerminalNode(
            **{**common, "is_terminal": True},
            config=TerminalConfig(),
        )

    service_kind, capability = _service_capability(raw_type or node.type, actions)
    if service_kind is ServiceTaskKind.TOOL and not operations:
        diagnostics.append(
            _warning(
                "LEGACY_TOOL_OPERATION_UNDECLARED",
                "旧工具节点未声明 call_tool 动作，只能保留为待补全的工具服务节点。",
                node_id=node.node_id,
            )
        )
    result_key = _operation_result_key(
        node.metadata,
        operations,
        diagnostics,
        node_id=node.node_id,
    )
    knowledge_query = _knowledge_task_config(
        node.metadata,
        diagnostics,
        node_id=node.node_id,
    )
    if service_kind is ServiceTaskKind.KNOWLEDGE and knowledge_query is not None and not result_key:
        diagnostics.append(
            _error(
                "KNOWLEDGE_RESULT_KEY_REQUIRED",
                "确定性知识节点必须声明 operation_result_key。",
                node_id=node.node_id,
            )
        )
    return ServiceTaskNode(
        **common,
        config=ServiceTaskConfig(
            kind=service_kind,
            capability=capability,
            operations=operations,
            input_mapping=_operation_input_mapping(
                node.metadata,
                condition_schemas,
                diagnostics,
                node_id=node.node_id,
            ),
            result_key=result_key,
            knowledge_query=knowledge_query if service_kind is ServiceTaskKind.KNOWLEDGE else None,
        ),
    )


def _canonical_node_type(
    legacy_type: str,
    *,
    actions: tuple[str, ...],
    expected_inputs: tuple[str, ...],
    is_terminal: bool,
) -> NodeType:
    """用稳定优先级把旧类型和动作归一到五类节点原语。"""

    if legacy_type in {"handoff", "human_task"}:
        return NodeType.HUMAN_TASK
    if legacy_type == "terminal" or (legacy_type == "response" and is_terminal):
        return NodeType.TERMINAL
    if legacy_type == "decision":
        return NodeType.DECISION
    if legacy_type in {"knowledge_query", "tool_call", "service_task"}:
        return NodeType.SERVICE_TASK
    if any(action.startswith("call_tool:") for action in actions) and not expected_inputs:
        return NodeType.SERVICE_TASK
    if legacy_type == "response" and not expected_inputs:
        return NodeType.SERVICE_TASK
    if is_terminal and not expected_inputs and "answer_user" in actions:
        return NodeType.TERMINAL
    return NodeType.COLLECT_INPUT


def _service_capability(legacy_type: str, actions: tuple[str, ...]) -> tuple[ServiceTaskKind, str]:
    """根据旧节点类型和动作选择服务任务的唯一能力。"""

    if legacy_type == "knowledge_query" or "knowledge_query" in actions:
        return ServiceTaskKind.KNOWLEDGE, "service.knowledge"
    if legacy_type == "tool_call" or any(action.startswith("call_tool:") for action in actions):
        return ServiceTaskKind.TOOL, "service.tool"
    return ServiceTaskKind.RESPONSE, "service.response"


def _assignment_hint(metadata: Mapping[str, Any]) -> str | None:
    """从旧 handoff metadata 提取仅供迁移使用的责任位提示。"""

    value = metadata.get("handoff_target")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized[:256] or None


def _knowledge_task_config(
    metadata: Mapping[str, Any],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> KnowledgeTaskConfig | None:
    """把显式知识查询策略编译为严格配置，旧定义缺省时保持原 checksum。"""

    raw_config = metadata.get("knowledge_query")
    if raw_config is None:
        return None
    if not isinstance(raw_config, Mapping):
        diagnostics.append(
            _error(
                "INVALID_KNOWLEDGE_QUERY_CONFIG",
                "knowledge_query 必须是结构化对象。",
                node_id=node_id,
            )
        )
        return None
    try:
        return KnowledgeTaskConfig.model_validate(raw_config)
    except (ValidationError, TypeError, ValueError) as error:
        diagnostics.append(
            _error(
                "INVALID_KNOWLEDGE_QUERY_CONFIG",
                f"知识查询配置无效：{error}",
                node_id=node_id,
            )
        )
        return None


def _human_task_config(
    metadata: Mapping[str, Any],
    diagnostics: list[CompilationDiagnostic],
    node_id: str,
) -> HumanTaskConfig:
    """将显式参与者策略编译为结构化工作项，旧节点继续保持问答接管语义。"""

    raw_policy = metadata.get("participant_policy")
    if raw_policy is None:
        return HumanTaskConfig(
            kind=HumanTaskKind.CONVERSATIONAL_HANDOFF,
            capability="human.conversational_handoff",
            assignment_hint=_assignment_hint(metadata),
        )
    if not isinstance(raw_policy, Mapping):
        diagnostics.append(
            _error(
                "INVALID_PARTICIPANT_POLICY",
                "人工任务 participant_policy 必须是结构化对象。",
                node_id=node_id,
            )
        )
        return HumanTaskConfig(
            kind=HumanTaskKind.CONVERSATIONAL_HANDOFF,
            capability="human.conversational_handoff",
            assignment_hint=_assignment_hint(metadata),
        )
    try:
        timeout_seconds = raw_policy.get("timeout_seconds")
        timeout_policy = (
            TimeoutPolicy(
                timeout_seconds=int(timeout_seconds),
                action=TimeoutAction(str(raw_policy.get("timeout_action") or "fail")),
            )
            if timeout_seconds is not None
            else None
        )
        completion_policy = WorkItemCompletionPolicy(
            mode=CompletionMode(str(raw_policy.get("completion_mode") or "single")),
            claim_required=bool(raw_policy.get("claim_required", False)),
            required_count=(
                int(raw_policy["required_count"])
                if raw_policy.get("required_count") is not None
                else None
            ),
            distinct_actors=True,
        )
        outcome_options = _work_item_outcome_options(raw_policy.get("outcome_options"))
        allowed_outcomes = (
            tuple(option.value for option in outcome_options)
            or _string_tuple(raw_policy.get("allowed_outcomes"))
            or ("approved", "rejected")
        )
        return HumanTaskConfig(
            kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
            capability="human.structured_work_item",
            assignment_hint=_assignment_hint(metadata),
            candidate_role_codes=_string_tuple(raw_policy.get("candidate_role_codes")),
            candidate_user_ids=_string_tuple(raw_policy.get("candidate_user_ids")),
            participant_scope_resolver=ParticipantScopeResolver(
                str(raw_policy.get("participant_scope_resolver") or "tenant")
            ),
            participant_scope_org_unit_id=(
                str(raw_policy["participant_scope_org_unit_id"]).strip()
                if raw_policy.get("participant_scope_org_unit_id") is not None
                else None
            ),
            completion_policy=completion_policy,
            exclude_initiator=bool(raw_policy.get("exclude_initiator", True)),
            allowed_outcomes=allowed_outcomes,
            outcome_options=outcome_options,
            action_permissions={
                str(action_key).strip(): str(permission_code).strip()
                for action_key, permission_code in (
                    raw_policy.get("action_permissions") or {}
                ).items()
            }
            if isinstance(raw_policy.get("action_permissions"), Mapping)
            else {},
            waiting_message=str(
                raw_policy.get("waiting_message")
                or "申请已提交人工处理，当前正在等待有权限的处理人。"
            ).strip(),
            timeout_policy=timeout_policy,
        )
    except (TypeError, ValueError, ValidationError) as error:
        diagnostics.append(
            _error(
                "INVALID_PARTICIPANT_POLICY",
                f"人工任务参与者策略无效：{error}",
                node_id=node_id,
            )
        )
        return HumanTaskConfig(
            kind=HumanTaskKind.CONVERSATIONAL_HANDOFF,
            capability="human.conversational_handoff",
            assignment_hint=_assignment_hint(metadata),
        )


def _work_item_outcome_options(value: object) -> tuple[WorkItemOutcomeOption, ...]:
    """把定义中的办理结果选项编译为严格、不可变且顺序稳定的契约。"""

    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("outcome_options must be an array")
    return tuple(WorkItemOutcomeOption.model_validate(item) for item in value)


def _string_tuple(value: object) -> tuple[str, ...]:
    """把 JSON 字符串数组规范化为去空白但不静默去重的不可变序列。"""

    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("participant selector must be a string array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("participant selector only accepts strings")
    return tuple(item.strip() for item in value)


def _input_bindings(
    metadata: Mapping[str, Any],
    required_inputs: tuple[str, ...],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> dict[str, AuthenticatedInputBinding]:
    """编译可信身份输入绑定，并拒绝绑定到节点未声明的槽位。"""

    raw_bindings = metadata.get("input_bindings")
    if raw_bindings is None:
        return {}
    if not isinstance(raw_bindings, Mapping):
        diagnostics.append(
            _error(
                "INVALID_INPUT_BINDING",
                "input_bindings 必须是以槽位名为键的对象。",
                node_id=node_id,
            )
        )
        return {}
    compiled: dict[str, AuthenticatedInputBinding] = {}
    for raw_slot_name, raw_binding in raw_bindings.items():
        slot_name = str(raw_slot_name).strip()
        if slot_name not in required_inputs:
            diagnostics.append(
                _error(
                    "INPUT_BINDING_SLOT_UNDECLARED",
                    f"身份输入绑定引用了节点未声明的槽位：{slot_name}",
                    node_id=node_id,
                )
            )
            continue
        try:
            compiled[slot_name] = AuthenticatedInputBinding.model_validate(raw_binding)
        except (ValidationError, ValueError, TypeError) as exc:
            diagnostics.append(
                _error(
                    "INVALID_INPUT_BINDING",
                    f"身份输入绑定无效：{exc}",
                    node_id=node_id,
                )
            )
    return compiled


def _value_aliases(
    metadata: Mapping[str, Any],
    required_inputs: tuple[str, ...],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> dict[str, dict[str, str]]:
    """编译槽位白名单别名，拒绝未声明槽位、空值和大小写冲突。"""

    raw_aliases = metadata.get("value_aliases")
    if raw_aliases is None:
        return {}
    if not isinstance(raw_aliases, Mapping):
        diagnostics.append(
            _error(
                "INVALID_INPUT_VALUE_ALIASES",
                "value_aliases 必须是以槽位名为键的对象。",
                node_id=node_id,
            )
        )
        return {}
    compiled: dict[str, dict[str, str]] = {}
    for raw_slot_name, raw_mapping in raw_aliases.items():
        slot_name = str(raw_slot_name).strip()
        if slot_name not in required_inputs:
            diagnostics.append(
                _error(
                    "INPUT_VALUE_ALIAS_SLOT_UNDECLARED",
                    f"槽位别名引用了节点未声明的槽位：{slot_name}",
                    node_id=node_id,
                )
            )
            continue
        if not isinstance(raw_mapping, Mapping):
            diagnostics.append(
                _error(
                    "INVALID_INPUT_VALUE_ALIASES",
                    f"槽位 {slot_name} 的别名必须是字符串映射。",
                    node_id=node_id,
                )
            )
            continue
        normalized_mapping: dict[str, str] = {}
        invalid = False
        for raw_alias, raw_canonical in raw_mapping.items():
            alias = str(raw_alias).strip()
            canonical = str(raw_canonical).strip()
            normalized_alias = alias.casefold()
            if not alias or not canonical:
                invalid = True
                break
            existing = normalized_mapping.get(normalized_alias)
            if existing is not None and existing != canonical:
                invalid = True
                break
            normalized_mapping[normalized_alias] = canonical
        if not invalid and any(
            normalized_mapping.get(canonical.casefold()) != canonical
            for canonical in set(normalized_mapping.values())
        ):
            invalid = True
        if invalid:
            diagnostics.append(
                _error(
                    "INVALID_INPUT_VALUE_ALIASES",
                    f"槽位 {slot_name} 的别名包含空值、大小写冲突或缺少规范值自身映射。",
                    node_id=node_id,
                )
            )
            continue
        compiled[slot_name] = normalized_mapping
    return compiled


def _confirmation_policy(
    metadata: Mapping[str, Any],
    required_inputs: tuple[str, ...],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> ExplicitConfirmationPolicy | None:
    """编译当前轮明确确认策略，拒绝未声明槽位和归一化后的短语冲突。"""

    raw_policy = metadata.get("confirmation_policy")
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, Mapping):
        diagnostics.append(
            _error(
                "INVALID_CONFIRMATION_POLICY",
                "confirmation_policy 必须是对象。",
                node_id=node_id,
            )
        )
        return None
    slot_name = str(raw_policy.get("slot_name") or "").strip()
    if slot_name not in required_inputs:
        diagnostics.append(
            _error(
                "CONFIRMATION_SLOT_UNDECLARED",
                f"明确确认策略引用了节点未声明的槽位：{slot_name}",
                node_id=node_id,
            )
        )
        return None
    raw_phrase_values = raw_policy.get("phrase_values")
    if not isinstance(raw_phrase_values, Mapping):
        diagnostics.append(
            _error(
                "INVALID_CONFIRMATION_POLICY",
                "confirmation_policy.phrase_values 必须是字符串映射。",
                node_id=node_id,
            )
        )
        return None
    from app.sop_runtime.explicit_confirmation import normalize_confirmation_phrase

    phrase_values: dict[str, str] = {}
    for raw_phrase, raw_value in raw_phrase_values.items():
        phrase = normalize_confirmation_phrase(str(raw_phrase))
        canonical_value = str(raw_value).strip()
        existing_value = phrase_values.get(phrase)
        if (
            not phrase
            or not canonical_value
            or (existing_value is not None and existing_value != canonical_value)
        ):
            diagnostics.append(
                _error(
                    "INVALID_CONFIRMATION_POLICY",
                    "确认短语包含空值或归一化冲突。",
                    node_id=node_id,
                )
            )
            return None
        phrase_values[phrase] = canonical_value
    try:
        return ExplicitConfirmationPolicy.model_validate(
            {
                "slot_name": slot_name,
                "phrase_values": phrase_values,
                "prompt": raw_policy.get("prompt"),
            }
        )
    except (ValidationError, ValueError, TypeError) as exc:
        diagnostics.append(
            _error(
                "INVALID_CONFIRMATION_POLICY",
                f"明确确认策略无效：{exc}",
                node_id=node_id,
            )
        )
        return None


def _operation_input_mapping(
    metadata: Mapping[str, Any],
    condition_schemas: Mapping[str, Mapping[str, object]],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> dict[str, str]:
    """提取工具参数到 Runtime 数据路径的显式绑定，不从自然语言猜测参数。"""

    raw_mapping = metadata.get("operation_input")
    if not isinstance(raw_mapping, Mapping):
        return {}
    validated: dict[str, str] = {}
    for argument, path in raw_mapping.items():
        argument_name = str(argument).strip()
        data_path = str(path).strip()
        if not argument_name or not CONDITION_PATH_PATTERN.fullmatch(data_path):
            diagnostics.append(
                _error(
                    "INVALID_OPERATION_INPUT_BINDING",
                    "工具参数绑定必须声明非空参数名和受限数据路径。",
                    node_id=node_id,
                )
            )
            continue
        try:
            compile_condition_dsl({"op": "exists", "path": data_path}, schemas=condition_schemas)
        except (ConditionCompilationError, ValidationError, ValueError) as exc:
            diagnostics.append(
                _error(
                    "INVALID_OPERATION_INPUT_BINDING",
                    f"工具参数绑定引用了未声明路径：{exc}",
                    node_id=node_id,
                )
            )
            continue
        validated[argument_name] = data_path
    return validated


def _operation_result_key(
    metadata: Mapping[str, Any],
    operations: tuple[str, ...],
    diagnostics: list[CompilationDiagnostic],
    *,
    node_id: str,
) -> str | None:
    """生成工具回执在 tool_result 数据根下使用的稳定字段名。"""

    configured = str(metadata.get("operation_result_key") or "").strip()
    if configured:
        if not configured[0].isalpha() or not all(
            character.isalnum() or character == "_" for character in configured
        ):
            diagnostics.append(
                _error(
                    "INVALID_OPERATION_RESULT_KEY",
                    "工具回执字段名必须以字母开头且只包含字母、数字和下划线。",
                    node_id=node_id,
                )
            )
            return None
        return configured
    if len(operations) != 1:
        return None
    normalized = "".join(
        character if character.isalnum() else "_" for character in operations[0]
    ).strip("_")
    return normalized or None


def _condition(
    value: str | Mapping[str, object] | None,
    diagnostics: list[CompilationDiagnostic],
    *,
    condition_schemas: Mapping[str, Mapping[str, object]],
    node_id: str | None = None,
    edge_index: int | None = None,
) -> ConditionExpression | RestrictedConditionExpression | None:
    """编译受限条件 AST，或把旧字符串明确隔离在兼容边界。"""

    if isinstance(value, Mapping):
        try:
            compiled = compile_condition_dsl(value, schemas=condition_schemas)
        except (ConditionCompilationError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "CONDITION_INVALID_AST")
            diagnostics.append(
                _error(
                    str(getattr(code, "value", code)),
                    f"受限条件 DSL 编译失败：{exc}",
                    node_id=node_id,
                    edge_index=edge_index,
                )
            )
            return None
        return RestrictedConditionExpression(ast=compiled.ast.model_dump(mode="json"))

    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > 512 or "\x00" in normalized:
        diagnostics.append(
            _error(
                "INVALID_LEGACY_CONDITION",
                "旧条件为空字符污染或超过 512 字符，无法安全编译。",
                node_id=node_id,
                edge_index=edge_index,
            )
        )
        return None
    if not any(item.code == "LEGACY_CONDITION_REQUIRES_UPGRADE" for item in diagnostics):
        diagnostics.append(
            _warning(
                "LEGACY_CONDITION_REQUIRES_UPGRADE",
                "旧条件仅通过传输安全校验，进入确定性 Runtime 前必须升级为受限条件 DSL。",
            )
        )
    return ConditionExpression(expression=normalized)


def _validate_restricted_branches(
    card: SkillCard,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """校验多出边 DSL 必须全量迁移并具有唯一优先级和默认分支。"""

    grouped: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for index, edge in enumerate(card.edges):
        grouped[edge.source_node_id].append((index, edge))
    for source_node_id, indexed_edges in grouped.items():
        if len(indexed_edges) < 2 or not any(
            isinstance(edge.condition, Mapping) for _, edge in indexed_edges
        ):
            continue
        if not all(isinstance(edge.condition, Mapping) for _, edge in indexed_edges):
            diagnostics.append(
                _error(
                    "CONDITION_MIXED_LANGUAGES",
                    "同一多出边节点不能混用旧字符串条件和受限条件 DSL。",
                    node_id=source_node_id,
                )
            )
            continue
        try:
            branches = tuple(
                ConditionBranch(
                    branch_id=f"edge_{index}",
                    condition=parse_condition_dsl(edge.condition),
                    priority=edge.priority,
                )
                for index, edge in indexed_edges
                if isinstance(edge.condition, Mapping)
            )
            compile_condition_branches(branches, schemas=card.condition_schemas)
        except (ConditionCompilationError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "CONDITION_INVALID_AST")
            diagnostics.append(
                _error(
                    str(getattr(code, "value", code)),
                    f"受限条件分支集合编译失败：{exc}",
                    node_id=source_node_id,
                )
            )


def _validate_capability(
    node: CanonicalNode,
    registry: CapabilityRegistry,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """确保节点声明的能力已注册且适用于该节点类型。"""

    capability = node.config.capability if hasattr(node.config, "capability") else "input.collect"
    if not registry.supports_definition(capability, NodeType(node.type)):
        diagnostics.append(
            _error(
                "MISSING_CAPABILITY",
                f"Runtime 未注册节点所需能力：{capability}",
                node_id=node.node_id,
            )
        )


def _meta_model_version(nodes: list[CanonicalNode]) -> int:
    """按定义实际使用的最高能力选择元模型版本，保持旧发布 checksum 稳定。"""

    uses_knowledge_service = any(
        isinstance(node, ServiceTaskNode) and node.config.knowledge_query is not None
        for node in nodes
    )
    uses_explicit_confirmation = any(
        isinstance(node, CollectInputNode) and node.config.confirmation_policy is not None
        for node in nodes
    )
    uses_value_aliases = any(
        isinstance(node, CollectInputNode) and bool(node.config.value_aliases) for node in nodes
    )
    uses_identity_context = any(
        isinstance(node, CollectInputNode) and bool(node.config.input_bindings) for node in nodes
    )
    if uses_knowledge_service:
        return KNOWLEDGE_SERVICE_META_MODEL_VERSION
    if uses_explicit_confirmation:
        return EXPLICIT_CONFIRMATION_META_MODEL_VERSION
    if uses_value_aliases:
        return SLOT_VALUE_ALIASES_META_MODEL_VERSION
    return (
        IDENTITY_CONTEXT_META_MODEL_VERSION if uses_identity_context else CURRENT_META_MODEL_VERSION
    )


def _definition_checksum(payload: Mapping[str, Any]) -> str:
    """对规范 JSON 计算稳定 SHA-256，供版本不可变和报告对比使用。"""

    serializable = {
        key: [_checksum_item(item) for item in value]
        if key in {"nodes", "edges", "diagnostics"}
        else value.value
        if hasattr(value, "value")
        else value
        for key, value in payload.items()
    }
    encoded = json.dumps(
        serializable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checksum_item(item: Any) -> dict[str, Any]:
    """移除新增空默认字段，确保旧元模型定义的历史 checksum 不漂移。"""

    payload = item.model_dump(mode="json")
    config = payload.get("config")
    if isinstance(config, dict):
        if config.get("input_bindings") == {}:
            config.pop("input_bindings")
        if config.get("knowledge_query") is None:
            config.pop("knowledge_query", None)
    return payload


def _error(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    edge_index: int | None = None,
) -> CompilationDiagnostic:
    """构造阻断发布的稳定错误诊断。"""

    return CompilationDiagnostic(
        severity=DiagnosticSeverity.ERROR,
        code=code,
        message=message,
        node_id=node_id,
        edge_index=edge_index,
    )


def _warning(code: str, message: str, *, node_id: str | None = None) -> CompilationDiagnostic:
    """构造允许兼容编译但必须进入报告的警告诊断。"""

    return CompilationDiagnostic(
        severity=DiagnosticSeverity.WARNING,
        code=code,
        message=message,
        node_id=node_id,
    )
