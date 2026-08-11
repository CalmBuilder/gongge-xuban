"""
@Time       : 2026/07/22 20:30
@Author     : zhanglp8181
@File       : session_schema.py
@CallChain  : Chat API/Agent Loop/SOP Runtime → 会话命令与响应契约
@Description: 定义聊天路由、步骤结果、会话状态以及仅供 Runtime 使用的控制回复标记。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.tools.tool_schema import ToolCall, ToolResult


RouterDecisionValue = Literal[
    "continue_active",
    "switch_to_pending",
    "create_pending",
    "update_pending",
    "complete_task",
    "start_new_task",
    "answer_only",
    "handoff_human",
    "clarify",
]
MessageFeedbackValue = Literal["up", "down"]


class TaskFrame(BaseModel):
    task_id: Optional[str] = None
    status: str = "pending"
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    intent_summary: Optional[str] = None
    source_turn_id: Optional[str] = None
    source_message: Optional[str] = None
    parent_task_id: Optional[str] = None
    resume_policy: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PendingTask(BaseModel):
    task_id: Optional[str] = None
    status: str = "pending"
    decision: RouterDecisionValue = "start_new_task"
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    task_id: str
    status: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    remove: bool = False


class AwaitingInput(BaseModel):
    task_id: Optional[str] = None
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    expected_fields: list[str] = Field(default_factory=list)
    question_summary: Optional[str] = None
    turn_id: Optional[str] = None


class RouterDecision(BaseModel):
    decision: RouterDecisionValue
    selected_task_id: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    general_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    clarification_question: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    task_frames: list[PendingTask] = Field(default_factory=list)
    pending_tasks: list[PendingTask] = Field(default_factory=list)
    task_updates: list[TaskUpdate] = Field(default_factory=list)
    created_tasks: list[PendingTask] = Field(default_factory=list)
    awaiting_input: Optional[AwaitingInput] = None


class KnowledgeQuery(BaseModel):
    query: str
    reason: Optional[str] = None
    scope: dict[str, Any] = Field(default_factory=dict)
    max_chunks: int = 6
    query_type: Literal["answer", "policy_check", "tool_discovery", "skill_discovery"] = "answer"
    desired_evidence: Optional[str] = None
    max_depth: int = 2


class StepAgentResult(BaseModel):
    """表示模型步骤建议，并保存不能由模型载荷设置的 Runtime 控制回复属性。"""

    _runtime_reply_source: str | None = PrivateAttr(default=None)
    _runtime_reply_policy: str | None = PrivateAttr(default=None)
    _runtime_reply_code: str | None = PrivateAttr(default=None)

    action: Optional[
        Literal[
            "ask_user",
            "clarify",
            "reply",
            "advance",
            "call_tool",
            "query_knowledge",
            "handoff",
        ]
    ] = None
    reply: Optional[str] = None
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    tool_call: Optional[ToolCall] = None
    knowledge_query: Optional[KnowledgeQuery] = None
    knowledge_results: list[dict[str, Any]] = Field(default_factory=list)
    next_step_id: Optional[str] = None
    is_step_completed: bool = False
    handoff: bool = False

    def mark_runtime_control_reply(self, code: str) -> StepAgentResult:
        """把当前回复标记为 Runtime 权威控制消息，要求响应层原样输出。"""

        self._runtime_reply_source = "runtime_control"
        self._runtime_reply_policy = "verbatim"
        self._runtime_reply_code = code
        return self

    def runtime_reply_metadata(self) -> dict[str, str]:
        """返回可持久化的控制回复来源，普通模型结果返回空字典。"""

        if self._runtime_reply_source != "runtime_control":
            return {}
        return {
            "response_source": self._runtime_reply_source,
            "render_policy": self._runtime_reply_policy or "verbatim",
            "runtime_error_code": self._runtime_reply_code or "RUNTIME_CONTROL",
        }

    def is_runtime_control_reply(self) -> bool:
        """判断当前结果是否由受信 Runtime 标记为禁止模型改写的控制回复。"""

        return (
            self._runtime_reply_source == "runtime_control"
            and self._runtime_reply_policy == "verbatim"
        )


class SessionPublic(BaseModel):
    session_id: str
    tenant_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    pending_tasks: list[dict[str, Any]] = Field(default_factory=list)
    awaiting_input: Optional[dict[str, Any]] = None
    knowledge_context: list[dict[str, Any]] = Field(default_factory=list)
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"


class ChatTurnRequest(BaseModel):
    tenant_id: str
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    model_config_id: Optional[str] = None
    client_turn_id: Optional[str] = None
    user_id: Optional[str] = None
    message: str
    attachments: list["ChatAttachmentRead"] = Field(default_factory=list)
    channel: str = "web"
    interaction_mode: Literal["normal", "scheduled_task"] = "normal"
    client_timezone: Optional[str] = None
    debug: bool = False
    forced_general_skill_id: Optional[str] = None


class ChatAttachmentRead(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    kind: Literal["text", "pdf", "image", "binary"] = "binary"
    text: Optional[str] = None
    preview: Optional[str] = None
    data_url: Optional[str] = None
    python_summary: Optional[str] = None
    error: Optional[str] = None
    resource_id: Optional[str] = None
    resource_version: Optional[str] = None
    content_checksum: Optional[str] = None
    ingestion_status: Optional[
        Literal["uploaded", "scanning", "extracting", "ready", "quarantined", "failed", "revoked"]
    ] = None


class ChatTurnResponse(BaseModel):
    reply: str
    session_id: str
    router_decision: Optional[RouterDecision] = None
    step_result: Optional[StepAgentResult] = None
    tool_result: Optional[ToolResult] = None
    session_state: SessionPublic


class ChatSessionCreateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    title: Optional[str] = None
    origin: Literal["gallery", "owned", "expert", "direct", "sop", "scheduled"] = "direct"


class ChatSessionUpdateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    title: str


class ChatSessionRead(BaseModel):
    id: str
    tenant_id: str
    user_id: Optional[str]
    agent_id: Optional[str] = None
    agent_profile_revision: Optional[int] = None
    capability_snapshot: Optional[dict[str, Any]] = None
    origin: Optional[str] = None
    title: Optional[str]
    active_skill_id: Optional[str]
    active_step_id: Optional[str]
    status: str
    summary: Optional[str]
    last_agent_question: Optional[str]
    is_scheduled: bool = False
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class MessageRead(BaseModel):
    id: str
    tenant_id: str
    session_id: str
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    turn_id: Optional[str] = None
    created_at: str
    feedback_rating: Optional[MessageFeedbackValue] = None

    model_config = ConfigDict(from_attributes=True)


class MessageFeedbackRequest(BaseModel):
    tenant_id: str
    rating: MessageFeedbackValue
