"""
@Time       : 2026/07/22 05:16
@Author     : zhanglp8181
@File       : definition.py
@CallChain  : 发布期适配器 → 规范 SOP 定义 → Runtime 实例创建/节点调度
@Description: 定义与具体业务无关的统一 SOP 节点、连线、诊断和编译结果契约。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from app.sop_runtime.contracts import (
    CompletionMode,
    RuntimeContract,
    TimeoutAction,
    TimeoutPolicy,
    WorkItemCompletionPolicy,
)


class NodeType(StrEnum):
    """统一元模型首批支持的稳定节点原语。"""

    COLLECT_INPUT = "collect_input"
    DECISION = "decision"
    SERVICE_TASK = "service_task"
    HUMAN_TASK = "human_task"
    TERMINAL = "terminal"


class ServiceTaskKind(StrEnum):
    """服务任务调用的能力类别，而不是具体业务名称。"""

    TOOL = "tool"
    KNOWLEDGE = "knowledge"
    RESPONSE = "response"


class HumanTaskKind(StrEnum):
    """人工任务的交互性质，防止把问答接管误当成结构化审批。"""

    CONVERSATIONAL_HANDOFF = "conversational_handoff"
    STRUCTURED_WORK_ITEM = "structured_work_item"


class ParticipantScopeResolver(StrEnum):
    """限定结构化人工任务候选人的组织范围解析方式。"""

    TENANT = "tenant"
    INITIATOR_PRIMARY_ORG = "initiator_primary_org"
    INITIATOR_PRIMARY_ORG_SUBTREE = "initiator_primary_org_subtree"
    EXPLICIT_ORG = "explicit_org"


class WorkItemOutcomeTone(StrEnum):
    """工作项结果按钮的有限视觉语义，不承载业务状态机含义。"""

    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"
    NEUTRAL = "neutral"


class ConditionLanguage(StrEnum):
    """条件表达式来源；旧条件只能在兼容边界解释。"""

    LEGACY_EXPRESSION = "legacy_expression"
    RESTRICTED_DSL = "restricted_dsl"


class SourceDefinitionFormat(StrEnum):
    """进入编译器前的定义格式。"""

    LEGACY_SKILL_CARD = "legacy_skill_card"


class AuthenticatedEmployeeAttribute(StrEnum):
    """员工档案允许向 SOP 暴露的白名单属性。"""

    EMPLOYEE_ID = "employee_id"
    EMPLOYEE_NAME = "employee_name"
    DEPARTMENT_ID = "department_id"


class InputBindingSource(StrEnum):
    """由 Runtime 负责解析而不是交给模型猜测的输入来源。"""

    AUTHENTICATED_EMPLOYEE = "authenticated_employee"


class DiagnosticSeverity(StrEnum):
    """编译诊断严重程度。"""

    WARNING = "warning"
    ERROR = "error"


class ConditionExpression(RuntimeContract):
    """经过边界校验但尚未升级为确定性 DSL 的旧条件表达式。"""

    language: Literal[ConditionLanguage.LEGACY_EXPRESSION] = ConditionLanguage.LEGACY_EXPRESSION
    expression: str = Field(min_length=1, max_length=512)


class RestrictedConditionExpression(RuntimeContract):
    """携带已经通过发布期编译的受限条件 JSON AST。"""

    language: Literal[ConditionLanguage.RESTRICTED_DSL] = ConditionLanguage.RESTRICTED_DSL
    ast: dict[str, object]


CanonicalCondition: TypeAlias = Annotated[
    ConditionExpression | RestrictedConditionExpression,
    Field(discriminator="language"),
]


class AuthenticatedInputBinding(RuntimeContract):
    """声明槽位来自当前登录账号绑定的员工档案及其代办规则。"""

    source: Literal[InputBindingSource.AUTHENTICATED_EMPLOYEE] = (
        InputBindingSource.AUTHENTICATED_EMPLOYEE
    )
    attribute: AuthenticatedEmployeeAttribute
    allow_override_roles: tuple[str, ...] = ()
    required_override_permission: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.]*(?::[a-z0-9_*.-]+)?$",
    )


class ExplicitConfirmationPolicy(RuntimeContract):
    """声明必须由当前用户消息明确给出的确认槽位和控制提示。"""

    slot_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    phrase_values: dict[str, str] = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_phrase_values(self) -> "ExplicitConfirmationPolicy":
        """拒绝空确认短语、空规范值和无法形成稳定分支的重复配置。"""

        if any(
            not phrase.strip() or not value.strip() for phrase, value in self.phrase_values.items()
        ):
            raise ValueError("confirmation phrases and values must not be empty")
        return self


class CollectInputConfig(RuntimeContract):
    """收集用户输入节点的规范配置。"""

    required_inputs: tuple[str, ...] = ()
    allow_partial: bool = True
    input_bindings: dict[str, AuthenticatedInputBinding] = Field(default_factory=dict)
    value_aliases: dict[str, dict[str, str]] = Field(default_factory=dict)
    confirmation_policy: ExplicitConfirmationPolicy | None = None


class DecisionConfig(RuntimeContract):
    """根据有序连线条件选择后继节点的规范配置。"""

    capability: str = "decision.legacy_expression"


class KnowledgeTaskConfig(RuntimeContract):
    """冻结知识检索意图、证据要求和有界检索预算。"""

    query_type: Literal["answer", "policy_check", "tool_discovery", "skill_discovery"] = "answer"
    desired_evidence: str | None = Field(default=None, min_length=1, max_length=1000)
    max_chunks: int = Field(default=6, ge=1, le=12)
    max_depth: int = Field(default=2, ge=1, le=4)


class ServiceTaskConfig(RuntimeContract):
    """工具、知识检索或确定性响应服务的规范配置。"""

    kind: ServiceTaskKind
    capability: str
    operations: tuple[str, ...] = ()
    input_mapping: dict[str, str] = Field(default_factory=dict)
    result_key: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    knowledge_query: KnowledgeTaskConfig | None = None

    @model_validator(mode="after")
    def validate_kind_specific_config(self) -> "ServiceTaskConfig":
        """拒绝把知识检索配置挂到其他服务类型，保持服务任务语义唯一。"""

        if self.kind is not ServiceTaskKind.KNOWLEDGE and self.knowledge_query is not None:
            raise ValueError("knowledge_query config is only valid for knowledge service tasks")
        return self


class WorkItemOutcomeOption(RuntimeContract):
    """冻结一个人工办理结果的值、显示方式、意见要求和申请人通知。"""

    value: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=32)
    tone: WorkItemOutcomeTone = WorkItemOutcomeTone.PRIMARY
    comment_required: bool = False
    completion_message: str = Field(min_length=1, max_length=1000)


class HumanTaskConfig(RuntimeContract):
    """人工接管或结构化工作项的规范配置。"""

    kind: HumanTaskKind
    capability: str
    assignment_hint: str | None = Field(default=None, max_length=256)
    candidate_role_codes: tuple[str, ...] = ()
    candidate_user_ids: tuple[str, ...] = ()
    participant_scope_resolver: ParticipantScopeResolver = ParticipantScopeResolver.TENANT
    participant_scope_org_unit_id: str | None = Field(default=None, max_length=128)
    completion_policy: WorkItemCompletionPolicy = Field(default_factory=WorkItemCompletionPolicy)
    exclude_initiator: bool = True
    allowed_outcomes: tuple[str, ...] = ("approved", "rejected")
    outcome_options: tuple[WorkItemOutcomeOption, ...] = ()
    action_permissions: dict[str, str] = Field(default_factory=dict)
    waiting_message: str = Field(
        default="申请已提交人工处理，当前正在等待有权限的处理人。",
        min_length=1,
        max_length=1000,
    )
    timeout_policy: TimeoutPolicy | None = None

    @model_validator(mode="after")
    def validate_structured_participants(self) -> "HumanTaskConfig":
        """确保结构化工作项有稳定候选来源、唯一结果和可实现的多人规则。"""

        if self.kind is HumanTaskKind.CONVERSATIONAL_HANDOFF:
            return self
        if self.participant_scope_resolver is ParticipantScopeResolver.EXPLICIT_ORG:
            if not (self.participant_scope_org_unit_id or "").strip():
                raise ValueError("explicit_org participant scope requires an organization unit")
        elif self.participant_scope_org_unit_id is not None:
            raise ValueError("participant scope organization is only valid for explicit_org")
        if not self.candidate_role_codes and not self.candidate_user_ids:
            raise ValueError("structured work item requires candidate roles or users")
        if any(not value.strip() for value in self.candidate_role_codes):
            raise ValueError("candidate role codes must not be empty")
        if any(not value.strip() for value in self.candidate_user_ids):
            raise ValueError("candidate user ids must not be empty")
        if len(set(self.candidate_role_codes)) != len(self.candidate_role_codes):
            raise ValueError("candidate role codes must be unique")
        if len(set(self.candidate_user_ids)) != len(self.candidate_user_ids):
            raise ValueError("candidate user ids must be unique")
        valid_action_keys = {"claim", "unclaim"} | {
            f"outcome:{outcome}" for outcome in self.allowed_outcomes
        }
        invalid_action_keys = sorted(set(self.action_permissions) - valid_action_keys)
        if invalid_action_keys:
            raise ValueError(
                "work item action permission keys must be claim, unclaim or a declared outcome"
            )
        if any(not permission_code.strip() for permission_code in self.action_permissions.values()):
            raise ValueError("work item action permission codes must not be empty")
        if not self.allowed_outcomes or any(
            not outcome.strip() for outcome in self.allowed_outcomes
        ):
            raise ValueError("structured work item requires non-empty outcomes")
        if len(set(self.allowed_outcomes)) != len(self.allowed_outcomes):
            raise ValueError("work item outcomes must be unique")
        if self.outcome_options:
            option_values = tuple(option.value for option in self.outcome_options)
            if len(set(option_values)) != len(option_values):
                raise ValueError("work item outcome options must be unique")
            if option_values != self.allowed_outcomes:
                raise ValueError("work item outcome options must match allowed outcomes in order")
            if self.completion_policy.mode in {CompletionMode.ALL, CompletionMode.QUORUM}:
                raise ValueError(
                    "custom work item outcomes currently support only single or any completion"
                )
        if self.completion_policy.mode is CompletionMode.SINGLE and (
            self.candidate_role_codes or len(self.candidate_user_ids) != 1
        ):
            raise ValueError("single completion requires exactly one direct candidate user")
        if (
            self.completion_policy.mode
            in {
                CompletionMode.ALL,
                CompletionMode.QUORUM,
            }
            and self.completion_policy.claim_required
        ):
            raise ValueError("multi-actor completion cannot require a single assignee claim")
        if self.timeout_policy is not None and self.timeout_policy.action is not TimeoutAction.FAIL:
            raise ValueError("structured work item currently supports only fail timeout action")
        return self


class TerminalConfig(RuntimeContract):
    """结束当前 SOP 路径的规范配置。"""

    capability: str = "terminal.response"
    outcome: str = Field(default="completed", min_length=1, max_length=64)


class CanonicalNodeBase(RuntimeContract):
    """所有规范节点共享的身份、说明和兼容信息。"""

    node_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    instruction: str = Field(default="", max_length=10000)
    optional: bool = False
    activation_condition: CanonicalCondition | None = None
    is_terminal: bool = False


class CollectInputNode(CanonicalNodeBase):
    """规范化的输入收集节点。"""

    type: Literal[NodeType.COLLECT_INPUT] = NodeType.COLLECT_INPUT
    config: CollectInputConfig


class DecisionNode(CanonicalNodeBase):
    """规范化的条件决策节点。"""

    type: Literal[NodeType.DECISION] = NodeType.DECISION
    config: DecisionConfig


class ServiceTaskNode(CanonicalNodeBase):
    """规范化的服务任务节点。"""

    type: Literal[NodeType.SERVICE_TASK] = NodeType.SERVICE_TASK
    config: ServiceTaskConfig


class HumanTaskNode(CanonicalNodeBase):
    """规范化的人工任务节点。"""

    type: Literal[NodeType.HUMAN_TASK] = NodeType.HUMAN_TASK
    config: HumanTaskConfig


class TerminalNode(CanonicalNodeBase):
    """规范化的流程终止节点。"""

    type: Literal[NodeType.TERMINAL] = NodeType.TERMINAL
    is_terminal: Literal[True] = True
    config: TerminalConfig


CanonicalNode = Annotated[
    CollectInputNode | DecisionNode | ServiceTaskNode | HumanTaskNode | TerminalNode,
    Field(discriminator="type"),
]


class CanonicalEdge(RuntimeContract):
    """规范节点之间的有序连线。"""

    source_node_id: str = Field(min_length=1, max_length=256)
    target_node_id: str = Field(min_length=1, max_length=256)
    condition: CanonicalCondition | None = None
    priority: int = 0
    label: str | None = Field(default=None, max_length=256)


class CompilationDiagnostic(RuntimeContract):
    """可稳定展示、测试和生成兼容报告的编译诊断。"""

    severity: DiagnosticSeverity
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    node_id: str | None = Field(default=None, max_length=256)
    edge_index: int | None = Field(default=None, ge=0)


class CompiledSopDefinition(RuntimeContract):
    """Runtime 可消费的不可变统一 SOP 定义。"""

    meta_model_version: int = Field(ge=1)
    source_format: SourceDefinitionFormat
    source_schema_version: int = Field(ge=1)
    skill_id: str = Field(min_length=1, max_length=256)
    skill_version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    start_node_id: str = Field(min_length=1, max_length=256)
    terminal_node_ids: tuple[str, ...]
    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]
    diagnostics: tuple[CompilationDiagnostic, ...] = ()
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
