"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_agent_loop_completion.py
@CallChain  : pytest → AgentLoop → 会话完成/消息与事件持久化
@Description: 验证聊天循环完成、恢复、裁剪以及最终消息审计行为。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import (
    GRAPH_PENDING_STEPS_SLOT,
    AgentLoop,
    AgentLoopPreconditionError,
    PreparedTurn,
)
from app.core.non_sop_capability import (
    NonSopCapabilityDecision,
    NonSopCapabilityRouter,
)
from app.core.skill_runtime import SkillRuntime
from app.agents.branching import ensure_open_gallery_binding
from app.db.models import AgentEvent, AgentProfile, ChatSession, MemoryRecord, Message, Skill, Tool
from app.dynamic_tasks.agent import DynamicRunOutcome
from app.knowledge.schema import KnowledgeSearchResponse
from app.general_skills.schema import GeneralSkillSelection
from app.session.session_schema import (
    AwaitingInput,
    ChatTurnResponse,
    KnowledgeQuery,
    PendingTask,
    RouterDecision,
    SessionPublic,
    StepAgentResult,
)
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.scheduler import RuntimeAction, RuntimePlan
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[object] = []
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, row: object) -> None:
        self.refreshed.append(row)

    def flush(self) -> None:
        """模拟生成主键后的事务内可见性。"""

    def exec(self, _statement: object) -> "FakeExecResult":
        """返回当前事务内已添加的消息，供幂等投影检查使用。"""

        return FakeExecResult([row for row in self.added if isinstance(row, Message)])


class WaitingMessageDb(FakeDb):
    """等待消息测试显式提供来源读取边界，不改变其他非数据库测试替身能力。"""

    def get(self, model: type, identity: str) -> object | None:
        """仅返回实际添加的行；未预置Execution保持未知来源而不制造零调用。"""
        return next((row for row in self.added
                     if isinstance(row, model) and getattr(row, "id", None) == identity), None)


class FakeExecResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None


def test_dynamic_waiting_message_is_persisted_once_with_execution_identity() -> None:
    """相同 Execution 阻塞点重放时复用等待消息并保留恢复所需身份。"""

    loop = object.__new__(AgentLoop)
    loop.db = WaitingMessageDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_waiting", tenant_id="tenant_demo")

    first = loop._persist_dynamic_waiting_message(
        chat_session=session,
        execution_id="execution_1",
        blocking_step_key="clarify_region",
        user_message_id="message_1",
    )
    second = loop._persist_dynamic_waiting_message(
        chat_session=session,
        execution_id="execution_1",
        blocking_step_key="clarify_region",
        user_message_id="message_retry",
    )

    assert second is first
    assert first.metadata_json == {
        "message_kind": "dynamic_task_status",
        "execution_id": "execution_1",
        "execution_status": "waiting",
        "blocking_step_key": "clarify_region",
        "feedback_source": {
            "policy_version": "feedback_source_v1", "turn_id": "message_1",
            "agent_id": None, "execution_id": None, "operations": None,
        },
        "turn_id": "message_1",
        "user_message_id": "message_1",
    }
    messages = [row for row in loop.db.added if isinstance(row, Message)]
    assert messages == [first]
    assert [record[2] for record in loop.events.records] == ["assistant_message_created"]


def test_deterministic_tool_result_wait_stops_before_model_continuation() -> None:
    """验证工具回执推进到等待输入后保留 Runtime 控制提示，不再被模型回复覆盖。"""

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.deterministic_runtime = SimpleNamespace(
        record_tool_result=lambda *_args: RuntimePlan(
            action=RuntimeAction.WAIT_INPUT,
            node_id="confirm_leave_submit",
            expected_inputs=("confirmation",),
            control_reply="请回复“确认提交”或“取消提交”。",
        ),
        merge_plan=DeterministicSopCoordinator.merge_plan,
    )
    loop._get_agent_loop_max_actions = lambda _tenant_id: 3
    loop._emit_tool_status = lambda *_args, **_kwargs: None
    loop._record_tool_result_in_slots = lambda *_args, **_kwargs: None
    loop._execute_tool_call = lambda *_args, **_kwargs: ToolResult(
        tool_name="hr.balance_query",
        success=True,
        data={"request_assessment": {"status": "sufficient", "requested_days": 2}},
    )
    loop._run_step_agent_once = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("等待输入时不得再次调用模型")
    )
    session = ChatSession(
        id="session_leave_wait",
        tenant_id="tenant_demo",
        active_skill_id="leave_apply_v1",
    )
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="leave_apply_v1",
        version="2.0.1",
        name="请假申请办理",
        content_json={"execution_mode": "deterministic"},
        status="published",
    )
    step_result, tool_result = loop._execute_tool_action_cycle(
        _request("申请两天年假"),
        session,
        skill,
        [],
        _model_config(),
        StepAgentResult(
            action="call_tool",
            tool_call=ToolCall(name="hr.balance_query", arguments={"employee_id": "E001"}),
        ),
    )

    assert tool_result is not None and tool_result.success is True
    assert step_result.action == "ask_user"
    assert step_result.reply == "请回复“确认提交”或“取消提交”。"
    assert step_result.is_runtime_control_reply() is True
    assert loop.events.records[-1][2] == "agent_loop_completed"


def test_sync_turn_returns_the_reply_persisted_by_finalization() -> None:
    """验证同步响应使用规范化后的持久文本，而不是生成器原始草稿。"""

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop._prepare_turn = lambda _request: PreparedTurn(
        chat_session=ChatSession(id="session_test", tenant_id="tenant_demo"),
        model_config=_model_config(),
        active_skill=None,
        router_decision=RouterDecision(decision="answer_only"),
        step_result=StepAgentResult(),
        tool_result=None,
        memory_context=[],
        conversation_context={},
        reply_override="原始回复 [2]\n\n参考资料：[1][2]",
        user_message_id="msg_user",
    )
    loop._finalize_turn = lambda *_args, **_kwargs: "规范化回复 [1]"
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    response = loop.handle_turn(_request("请回答"))

    assert response.reply == "规范化回复 [1]"
    assert loop.events.records[-1][2] == "complete"
    assert loop.events.records[-1][3]["status"] == "completed"


def test_precondition_failure_keeps_created_user_turn_identity() -> None:
    """验证用户消息创建后发生模型前置失败时仍携带同一 Turn 身份。"""

    loop = object.__new__(AgentLoop)
    session = ChatSession(id="session_precondition", tenant_id="tenant_demo")
    user_message = Message(
        id="message_precondition",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="需要模型配置",
    )
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop._get_or_create_session = lambda _request: session
    loop._assert_agent_available_for_final_write = lambda *_args, **_kwargs: None
    loop._mark_session_running = lambda *_args, **_kwargs: None
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: None
    loop._list_published_skills = lambda *_args, **_kwargs: []
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []

    with pytest.raises(AgentLoopPreconditionError) as captured:
        loop._prepare_turn(_request("需要模型配置"))

    assert captured.value.code == "missing_model_config"
    assert captured.value.user_message_id == user_message.id
    assert [record[2] for record in loop.events.records] == ["user_message_received"]


def test_generic_knowledge_query_forwards_frozen_intent_and_budget(monkeypatch) -> None:
    """验证非 SOP 知识入口同样传递查询意图、证据要求和有界预算。"""

    captured = []

    def search(_service, request, _model_config=None):
        """捕获 Agent Loop 交给统一知识服务的请求。"""

        captured.append(request)
        return KnowledgeSearchResponse(
            evidence_pack=[
                {
                    "chunk_id": "kchunk_leave",
                    "content": "年假申请由直属主管审批。",
                }
            ]
        )

    monkeypatch.setattr("app.core.agent_loop.KnowledgeService.search", search)
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop._accessible_knowledge_scope = lambda *_args: (
        ["kb_hr"],
        ["kbver_hr_current"],
    )
    loop._agent_requires_resource_filter = lambda *_args: True
    query = KnowledgeQuery(
        query="年假申请",
        query_type="policy_check",
        desired_evidence="主管审批时限",
        scope={"document_ids": ["kdoc_leave"]},
        max_chunks=11,
        max_depth=4,
    )

    result = loop._knowledge_items_for_message(
        "tenant_demo",
        "user_hr",
        "agent_hr",
        "年假怎么申请",
        query=query,
    )

    assert result is not None
    assert len(captured) == 1
    request = captured[0]
    assert request.query_type == "policy_check"
    assert request.desired_evidence == "主管审批时限"
    assert request.scope == {"document_ids": ["kdoc_leave"]}
    assert request.knowledge_base_version_ids == ["kbver_hr_current"]
    assert request.max_chunks == 11
    assert request.max_depth == 4


def test_router_decision_only_hydrates_structured_profile_memory() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(id="session_test", tenant_id="tenant_demo", slots_json={})
    decision = RouterDecision(
        decision="start_new_task",
        target_skill_id="purchase",
        target_step_id="collect_user_name",
        slot_hints={"product_name": "a1", "quantity": 1},
        awaiting_input=AwaitingInput(
            skill_id="purchase",
            step_id="collect_user_name",
            expected_fields=["user_name", "product_id"],
        ),
    )

    hydrated = loop._hydrate_router_decision_from_context(
        session,
        decision,
        [_purchase_skill()],
        [{"kind": "profile", "content": "hm", "metadata": {"key": "preferred_name"}}],
    )

    assert hydrated["primary"] == {"user_name": "hm"}
    assert decision.slot_hints == {"product_name": "a1", "quantity": 1, "user_name": "hm"}
    assert decision.awaiting_input is not None
    assert decision.awaiting_input.expected_fields == ["product_id"]


class FakeMessageDb(FakeDb):
    def __init__(self, rows: list[Message]) -> None:
        super().__init__()
        self.rows = rows

    def exec(self, _statement: object) -> FakeExecResult:
        return FakeExecResult(self.rows)


class FakeEventDb(FakeDb):
    def __init__(self, tool: Tool | None, rows: list[AgentEvent]) -> None:
        super().__init__()
        self.tool = tool
        self.rows = rows
        self.exec_calls = 0

    def exec(self, _statement: object) -> FakeExecResult:
        self.exec_calls += 1
        if self.exec_calls == 1:
            return FakeExecResult([self.tool] if self.tool else [])
        return FakeExecResult(self.rows)


class FakeToolExecutor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.commits_seen_before_execute: int | None = None
        self.execute_calls = 0
        self.authorization_result: ToolResult | None = None

    def authorize(
        self,
        _tenant_id: str,
        _tool_call: ToolCall,
        _active_skill_id: str | None = None,
        _agent_id: str | None = None,
        _actor_user_id: str | None = None,
        _execution_org_unit_id: str | None = None,
        _remote_idempotency_key: str | None = None,
        _is_cancelled=None,
    ) -> ToolResult | None:
        """返回可控的当前授权结论，供兼容回执复用测试撤权。"""

        return self.authorization_result

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> ToolResult:
        self.commits_seen_before_execute = self.db.commits
        self.execute_calls += 1
        return ToolResult(tool_name=tool_call.name, success=True, data={"ok": True})


def _no_sop_stream_with_shadow(
    shadow_enabled: bool,
    *,
    scheduled: bool = False,
) -> tuple[list[dict], FakeEvents]:
    """运行无 SOP 流式回答，并返回传输事件和持久领域事件供等价比较。"""

    db = FakeDb()
    events = FakeEvents()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_no_sop_shadow",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="message_no_sop_shadow",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="整理两个系统并生成简报",
    )
    loop.db = db
    loop.events = events
    loop.general_skill_selector = SimpleNamespace(
        decide=lambda *_args, **_kwargs: GeneralSkillSelection()
    )
    loop.non_sop_capability_router = NonSopCapabilityRouter(
        shadow_enabled=shadow_enabled,
        shadow_selector=SimpleNamespace(
            decide=lambda *_args, **_kwargs: NonSopCapabilityDecision(
                mode="dynamic_task",
                goal="生成简报",
                success_criteria=["包含两个系统的证据"],
                requires_durable_execution=True,
                confidence=0.92,
            )
        ),
    )
    loop._get_or_create_session = lambda _request: session
    loop._mark_session_running = lambda *_args, **_kwargs: None
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: []
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._list_published_general_skills = lambda *_args, **_kwargs: []
    loop._knowledge_capability_payload = lambda *_args, **_kwargs: {
        "available": False,
        "accessible_count": 0,
    }
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: [],
        context_memory_payloads=lambda *_args, **_kwargs: [],
    )
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()
    loop._generate_reply_stream_segment = lambda *_args, **_kwargs: iter(["固定回复"])
    loop._finalize_turn = lambda *_args, **_kwargs: "固定回复"
    loop._pace_stream = lambda: None
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    request = _request("整理两个系统并生成简报")
    if scheduled:
        request.channel = "scheduled_task"
        request.interaction_mode = "scheduled_task"
    streamed = list(loop.handle_turn_stream(request))
    return streamed, events


def test_stream_dynamic_waiting_does_not_close_turn() -> None:
    """验证聊天动态任务进入等待后只写 waiting，不提前写入 complete 或 interrupted。"""

    db = FakeDb()
    events = FakeEvents()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_dynamic_waiting",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="message_dynamic_waiting",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="搜索最新的开发者工具趋势",
    )
    waiting_reply = "动态任务已创建，等待后续步骤继续。"
    waiting_response = ChatTurnResponse(
        reply=waiting_reply,
        session_id=session.id,
        router_decision=RouterDecision(
            decision="answer_only",
            reason="DynamicTaskAgent paused on a governed clarification.",
        ),
        step_result=StepAgentResult(reply=waiting_reply, is_step_completed=False),
        session_state=SessionPublic(
            session_id=session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            agent_id=session.agent_id,
        ),
    )

    loop.db = db
    loop.events = events
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: [],
        context_memory_payloads=lambda *_args, **_kwargs: [],
    )
    loop._get_or_create_session = lambda _request: session
    loop._mark_session_running = lambda *_args, **_kwargs: None
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: []
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._list_published_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._decide_non_sop_capability = lambda *_args, **_kwargs: SimpleNamespace()
    loop._try_handle_dynamic_task = lambda *_args, **_kwargs: waiting_response
    loop._assert_agent_available_for_final_write = lambda *_args, **_kwargs: None
    loop._pace_stream = lambda: None

    request = _request("搜索最新的开发者工具趋势")
    request.execution_engine = "dynamic_task"
    request.client_turn_id = "client_dynamic_waiting"

    streamed = list(loop.handle_turn_stream(request))
    names = [event["event"] for event in streamed]

    assert "turn_waiting" in names
    assert "complete" not in names
    assert "stream_interrupted" not in [record[2] for record in events.records]
    assert [record[2] for record in events.records].count("turn_waiting") == 1
    assert [record[2] for record in events.records].count("complete") == 0


def test_scheduled_dynamic_waiting_is_not_misclassified_as_success() -> None:
    """调度动态任务的等待投影必须保持 waiting，不能被同步边界写成 complete。"""

    request = _request("按计划生成并等待后续信号")
    request.channel = "scheduled_task"
    request.interaction_mode = "scheduled_task"
    response = ChatTurnResponse(
        reply="调度任务已排队，等待持久信号继续。",
        session_id="session_scheduled_waiting",
        router_decision=RouterDecision(
            decision="answer_only",
            reason="DynamicTaskAgent queued by a durable schedule signal.",
        ),
        step_result=StepAgentResult(
            reply="调度任务已排队，等待持久信号继续。",
            is_step_completed=False,
        ),
        session_state=SessionPublic(
            session_id="session_scheduled_waiting",
            tenant_id="tenant_demo",
        ),
    )

    assert AgentLoop._is_chat_dynamic_waiting_response(request, response) is True


def test_stream_dynamic_success_does_not_append_after_runtime_terminal() -> None:
    """动态成功已由 Runtime 闭合后，聊天流不再追加迟到的普通流事件。"""

    db = FakeDb()
    events = FakeEvents()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_dynamic_success",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="message_dynamic_success",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="搜索并整理开发者工具趋势",
    )
    success_reply = "动态任务已完成。"
    success_response = ChatTurnResponse(
        reply=success_reply,
        session_id=session.id,
        router_decision=RouterDecision(
            decision="answer_only",
            reason="DynamicTaskAgent completed a governed read-only execution.",
        ),
        step_result=StepAgentResult(reply=success_reply, is_step_completed=True),
        session_state=SessionPublic(
            session_id=session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            agent_id=session.agent_id,
        ),
    )

    loop.db = db
    loop.events = events
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: [],
        context_memory_payloads=lambda *_args, **_kwargs: [],
    )
    loop._get_or_create_session = lambda _request: session
    loop._mark_session_running = lambda *_args, **_kwargs: None
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: []
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._list_published_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._decide_non_sop_capability = lambda *_args, **_kwargs: SimpleNamespace()
    loop._assert_agent_available_for_final_write = lambda *_args, **_kwargs: None
    loop._try_handle_dynamic_task = lambda *_args, **_kwargs: (
        events.record(
            "tenant_demo",
            session.id,
            "complete",
            {"turn_id": user_message.id, "user_message_id": user_message.id},
        )
        or success_response
    )

    request = _request("搜索并整理开发者工具趋势")
    request.execution_engine = "dynamic_task"
    request.client_turn_id = "client_dynamic_success"

    streamed = list(loop.handle_turn_stream(request))
    streamed_names = [event["event"] for event in streamed]
    recorded_names = [record[2] for record in events.records]

    assert recorded_names.count("complete") == 1
    assert "stream_end" not in recorded_names
    assert "complete" not in streamed_names


def test_chat_clarification_resume_does_not_append_after_success_terminal(monkeypatch) -> None:
    """澄清恢复成功已写入 complete 后，Agent Loop 不再追加迟到 resumed 事件。"""

    recorded: list[tuple] = []

    class _DynamicAgent:
        """模拟已原子闭合成功结果的聊天动态 Agent。"""

        def __init__(self, _db) -> None:
            """保存测试依赖。"""

        def bind_turn(self, *_args, **_kwargs) -> None:
            """兼容生产 Turn 绑定接口。"""

        def resume_chat_clarification(self, **_kwargs):
            """返回已经由 Runtime 写入成功终态的恢复结果。"""

            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_resume_success",
                message=SimpleNamespace(content="澄清后的最终结果"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        commit=lambda: None,
        refresh=lambda _row: None,
    )
    loop.events = SimpleNamespace(
        record=lambda *args, **_kwargs: recorded.append(args),
    )
    loop._knowledge_capability_payload = lambda *_args, **_kwargs: {"available": False}
    session = ChatSession(
        id="session_resume_success",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        user_id="user_demo",
    )
    route = SimpleNamespace(
        effective_decision=NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="继续澄清任务",
            success_criteria=["形成最终结果"],
            requires_durable_execution=True,
        ),
        general_selection=None,
    )
    request = _request("补充澄清信息")
    request.execution_engine = "dynamic_task"
    request.user_id = "user_demo"
    request.agent_id = "agent_demo"
    request.client_turn_id = "client_resume_success"

    response = loop._try_handle_dynamic_task(
        request,
        session,
        _model_config(),
        route,
        "message_resume_success",
    )

    assert response is not None
    assert response.reply == "澄清后的最终结果"
    assert [args[2] for args in recorded] == []


def test_dynamic_blocked_projects_as_resumable_waiting_with_client_turn_identity() -> None:
    """动态读取因重新认证或限流阻塞时，聊天层必须保留等待态和客户端 Turn 身份。"""

    loop = object.__new__(AgentLoop)
    loop.db = WaitingMessageDb()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_dynamic_blocked",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )

    response = loop._dynamic_task_response(
        chat_session=session,
        outcome=DynamicRunOutcome(
            status="blocked",
            execution_id="execution_blocked",
            blocking_step_key="public_search",
        ),
        user_message_id="message_blocked",
        client_turn_id="client_blocked",
    )

    assert response.step_result is not None
    assert response.step_result.is_step_completed is False
    messages = [row for row in loop.db.added if isinstance(row, Message)]
    assert len(messages) == 1
    assert messages[0].metadata_json["client_turn_id"] == "client_blocked"
    assert loop.events.records[0][3]["client_turn_id"] == "client_blocked"


def _no_sop_prepared_turn_with_shadow(
    shadow_enabled: bool,
    *,
    memories: list[MemoryRecord] | None = None,
) -> tuple[PreparedTurn, FakeEvents]:
    """运行同步无 SOP 准备阶段，返回权威结果和领域事件供兼容比较。"""

    loop = object.__new__(AgentLoop)
    events = FakeEvents()
    session = ChatSession(
        id="session_no_sop_sync_shadow",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="message_no_sop_sync_shadow",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="整理两个系统并生成简报",
    )
    loop.db = FakeDb()
    loop.events = events
    loop.general_skill_selector = SimpleNamespace(
        decide=lambda *_args, **_kwargs: GeneralSkillSelection()
    )
    loop.non_sop_capability_router = NonSopCapabilityRouter(
        shadow_enabled=shadow_enabled,
        shadow_selector=SimpleNamespace(
            decide=lambda *_args, **_kwargs: NonSopCapabilityDecision(
                mode="dynamic_task",
                goal="生成简报",
                success_criteria=["包含两个系统的证据"],
                confidence=0.92,
            )
        ),
    )
    loop._get_or_create_session = lambda _request: session
    loop._mark_session_running = lambda *_args, **_kwargs: None
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: []
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._list_published_general_skills = lambda *_args, **_kwargs: []
    loop._knowledge_capability_payload = lambda *_args, **_kwargs: {
        "available": False,
        "accessible_count": 0,
    }
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: list(memories or []),
        context_memory_payloads=lambda *_args, **_kwargs: [
            {
                "id": row.id,
                "kind": row.kind,
                "content": row.content,
                "metadata": row.metadata_json or {},
            }
            for row in memories or []
        ],
    )
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()

    return loop._prepare_turn(_request("整理两个系统并生成简报")), events


def test_no_sop_prepared_turn_keeps_agent_scoped_memory_context() -> None:
    """无场景 SOP 时仍须把当前数字员工记忆送入路由及后续动态任务上下文。"""

    prepared, events = _no_sop_prepared_turn_with_shadow(
        False,
        memories=[
            MemoryRecord(
                id="memory_agent_preference",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                user_id="user_demo",
                kind="preference",
                content="称呼用户为张工",
            )
        ],
    )

    assert [item["content"] for item in prepared.memory_context] == ["称呼用户为张工"]
    recalled = [record for record in events.records if record[2] == "memory_recalled"]
    assert len(recalled) == 1
    assert recalled[0][3]["memories"] == prepared.memory_context


def test_no_sop_shadow_does_not_change_sse_frames() -> None:
    """开关开启只增加脱敏领域审计，流式帧名称和 payload 必须与关闭时完全一致。"""

    disabled_stream, disabled_events = _no_sop_stream_with_shadow(False)
    enabled_stream, enabled_events = _no_sop_stream_with_shadow(True)

    def without_transport_timestamps(items: list[dict]) -> list[dict]:
        """移除每次发送必然变化的传输时间，保留其余 SSE 契约比较。"""

        normalized: list[dict] = []
        for item in items:
            data = dict(item.get("data") or {})
            data.pop("timestamp", None)
            data.pop("time_context", None)
            normalized.append({**item, "data": data})
        return normalized

    assert without_transport_timestamps(enabled_stream) == without_transport_timestamps(
        disabled_stream
    )
    assert "non_sop_capability_shadow_decided" not in {
        str(item.get("event")) for item in enabled_stream
    }
    assert "non_sop_capability_shadow_decided" not in {
        row[2] for row in disabled_events.records
    }
    shadow_rows = [
        row for row in enabled_events.records if row[2] == "non_sop_capability_shadow_decided"
    ]
    assert len(shadow_rows) == 1
    assert shadow_rows[0][3]["shadow_mode"] == "dynamic_task"
    assert "goal" not in shadow_rows[0][3]


def test_no_sop_shadow_does_not_change_sync_prepared_turn() -> None:
    """同步路径开启 shadow 后仍返回相同 Router、知识和 GeneralSkill 权威结果。"""

    disabled, disabled_events = _no_sop_prepared_turn_with_shadow(False)
    enabled, enabled_events = _no_sop_prepared_turn_with_shadow(True)

    assert enabled.router_decision == disabled.router_decision
    assert enabled.step_result == disabled.step_result
    assert enabled.general_response == disabled.general_response
    assert enabled.conversation_context == disabled.conversation_context
    assert "non_sop_capability_shadow_decided" not in {
        row[2] for row in disabled_events.records
    }
    assert [
        row[2] for row in enabled_events.records if row[2] == "non_sop_capability_shadow_decided"
    ] == ["non_sop_capability_shadow_decided"]


def test_scheduled_stream_uses_same_non_sop_shadow_boundary() -> None:
    """Schedule 经流式 Agent Loop 时复用相同 shadow，并保持传输事件数量与类型不变。"""

    disabled_stream, _ = _no_sop_stream_with_shadow(False, scheduled=True)
    enabled_stream, enabled_events = _no_sop_stream_with_shadow(True, scheduled=True)

    assert [item["event"] for item in enabled_stream] == [
        item["event"] for item in disabled_stream
    ]
    shadow_rows = [
        row for row in enabled_events.records if row[2] == "non_sop_capability_shadow_decided"
    ]
    assert len(shadow_rows) == 1
    assert shadow_rows[0][3]["shadow_mode"] == "dynamic_task"


def test_tool_call_start_event_is_committed_before_external_execute() -> None:
    db = FakeDb()
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
    )

    result = loop._execute_tool_call(
        _request("下单"),
        session,
        ToolCall(name="product.purchase", arguments={"product_id": "A1"}),
    )

    assert result.success is True
    assert executor.commits_seen_before_execute == 1
    assert db.commits == 2
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]


def test_deterministic_tool_call_passes_remote_idempotency_key_to_executor() -> None:
    """验证 Agent Loop 从确定性 Runtime 取键并传到适配器，而不写入工具业务参数。"""

    captured: dict[str, object] = {}

    class RecordingExecutor:
        """记录 Agent Loop 交给工具适配器的可靠执行上下文。"""

        def execute(
            self,
            tenant_id: str,
            tool_call: ToolCall,
            active_skill_id: str | None = None,
            agent_id: str | None = None,
            actor_user_id: str | None = None,
            remote_idempotency_key: str | None = None,
        ) -> ToolResult:
            """记录 Agent Loop 传入的远端键和原始业务参数。"""

            captured["remote_idempotency_key"] = remote_idempotency_key
            captured["arguments"] = dict(tool_call.arguments)
            return ToolResult(tool_name=tool_call.name, success=True, data={"ok": True})

    class RecordingRuntime:
        """模拟已准备好远端幂等键的确定性 Runtime。"""

        def remote_idempotency_key_for(
            self,
            chat_session: ChatSession,
            operation_name: str,
        ) -> str:
            """返回当前逻辑动作冻结的测试远端键。"""

            assert chat_session.id == "session_test"
            assert operation_name == "product.purchase"
            return "remote-command-key"

    db = FakeDb()
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = RecordingExecutor()
    loop.deterministic_runtime = RecordingRuntime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
    )

    result = loop._execute_tool_call(
        _request("下单"),
        session,
        ToolCall(name="product.purchase", arguments={"product_id": "A1"}),
    )

    assert result.success is True
    assert captured == {
        "remote_idempotency_key": "remote-command-key",
        "arguments": {"product_id": "A1"},
    }


def test_side_effect_tool_call_without_trusted_identity_does_not_reuse_result() -> None:
    """没有持久动作身份时不得按会话内同参数猜测复用外部成功回执。"""

    tool = Tool(
        tenant_id="tenant_demo",
        name="crm.create_ticket",
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    event = AgentEvent(
        id="evt_existing_tool_result",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": "crm.create_ticket",
            "success": True,
            "data": {"ticket_id": "TCK-1001", "status": "created"},
            "tool_call": {
                "name": "crm.create_ticket",
                "arguments": {
                    "customer_id": "C-1",
                    "subject": "发票开具",
                    "priority": "normal",
                },
            },
        },
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(
        id="session_test", tenant_id="tenant_demo", active_skill_id="skill_leave_apply_001"
    )

    result = loop._execute_tool_call(
        _request("重试一下，如果办理失败需要提示我"),
        session,
        ToolCall(
            name="crm.create_ticket",
            arguments={
                "customer_id": "C-1",
                "subject": "发票开具",
                "priority": "normal",
            },
        ),
        tool_call_id="toolcall_retry",
    )

    assert result.success is True
    assert result.data == {"ok": True}
    assert executor.commits_seen_before_execute == 1
    assert db.commits == 2
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]
    assert loop.events.records[-1][3]["success"] is True


def test_sop_operation_receipt_reuse_uses_exact_operation_and_not_executor() -> None:
    """确定性 Runtime 只复用当前精确 Operation 的终态回执，不再次调用适配器。"""

    tool_call = ToolCall(
        name="crm.create_ticket",
        arguments={"customer_id": "C-1", "subject": "发票开具"},
    )
    captured = {"dispatches": 0}
    operation = SimpleNamespace(
        id="operation_1",
        logical_action_id="action_1",
        status="succeeded",
        request_fingerprint=SopExecutionStore.request_fingerprint(tool_call.arguments),
        result_json={"ticket_id": "TCK-1001"},
        error_json={},
        remote_idempotency_key="remote-key-1",
    )

    class Runtime:
        """提供当前 Operation 查询的最小确定性 Runtime 替身。"""

        def current_tool_operation(self, _session, current_call):
            """只返回名称和完整请求指纹均匹配的当前 Operation。"""

            assert current_call == tool_call
            assert operation.request_fingerprint == SopExecutionStore.request_fingerprint(
                current_call.arguments
            )
            return operation

    class Executor:
        """记录授权和真实派发，验证回执复用顺序。"""

        def authorize(self, *_args, **_kwargs):
            """模拟当前授权仍然有效。"""

            return None

        def execute(self, *_args, **_kwargs):
            """真实派发不应在 Operation 终态复用路径被调用。"""

            captured["dispatches"] += 1
            return ToolResult(tool_name=tool_call.name, success=True, data={"unexpected": True})

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = Executor()
    loop.deterministic_runtime = Runtime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="skill_ticket",
    )

    result = loop._execute_tool_call(
        _request("重试工单"),
        session,
        tool_call,
        tool_call_id="temporary_retry_id",
    )

    assert result.success is True
    assert result.data == {
        "ticket_id": "TCK-1001",
        "idempotent_replay": True,
        "receipt_source": "sop_operation",
        "operation_id": "operation_1",
        "logical_action_id": "action_1",
    }
    assert captured["dispatches"] == 0
    assert loop.events.records[0][3]["receipt_source"] == "sop_operation"
    assert loop.events.records[0][3]["operation_id"] == "operation_1"


def test_sop_operation_receipt_reuse_checks_current_authorization_first() -> None:
    """确定性 Operation 即使已有成功回执，撤权后也不得泄露回执或继续复用。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    operation = SimpleNamespace(
        id="operation_revoked",
        logical_action_id="action_revoked",
        status="succeeded",
        request_fingerprint=SopExecutionStore.request_fingerprint(tool_call.arguments),
        result_json={"ticket_id": "TCK-REVOKED"},
        error_json={},
        remote_idempotency_key="remote-key-revoked",
    )

    class Runtime:
        """返回已完成但当前调用人已失去权限的 Operation。"""

        def current_tool_operation(self, _session, _tool_call):
            """返回测试 Operation。"""

            return operation

    class Executor:
        """模拟撤权后的统一授权边界。"""

        def authorize(self, *_args, **_kwargs):
            """拒绝当前授权，不接触真实派发。"""

            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                error=ToolError(code="ACTOR_PERMISSION_REQUIRED", message="权限已撤销。"),
            )

        def execute(self, *_args, **_kwargs):
            """撤权路径不应调用执行器。"""

            raise AssertionError("撤权后不得派发工具")

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = Executor()
    loop.deterministic_runtime = Runtime()
    session = ChatSession(id="session_test", tenant_id="tenant_demo", active_skill_id="skill_ticket")

    result = loop._execute_tool_call(_request("重试工单"), session, tool_call)

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "ACTOR_PERMISSION_REQUIRED"
    assert result.data is None
    assert loop.events.records[0][3]["receipt_source"] == "authorization_rejected"


def test_compatibility_entry_reuses_same_persistent_action_across_temp_call_ids() -> None:
    """兼容入口同一持久动作重试时复用回执，临时 tool_call_id 变化不影响身份。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    event = AgentEvent(
        id="evt_action_1",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": tool_call.name,
            "success": True,
            "data": {"ticket_id": "TCK-1001"},
            "persistent_action_id": "action-1",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=tool_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("重试工单"),
        session,
        tool_call,
        tool_call_id="temporary-call-2",
        persistent_action_id="action-1",
    )

    assert result.success is True
    assert result.data["ticket_id"] == "TCK-1001"
    assert result.data["receipt_source"] == "compatibility_event"
    assert executor.execute_calls == 0
    assert loop.events.records[0][3]["persistent_action_id"] == "action-1"


def test_compatibility_entry_new_identity_same_arguments_dispatches() -> None:
    """兼容入口使用新持久动作身份时，即使参数相同也必须形成独立本地动作。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    old_event = AgentEvent(
        id="evt_action_old",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": tool_call.name,
            "success": True,
            "data": {"ticket_id": "TCK-OLD"},
            "persistent_action_id": "action-old",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=tool_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [old_event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("再次创建工单"),
        session,
        tool_call,
        persistent_action_id="action-new",
    )

    assert result.success is True
    assert result.data == {"ok": True}
    assert executor.execute_calls == 1
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]
    assert loop.events.records[0][3]["persistent_action_id"] == "action-new"


def test_compatibility_entry_same_identity_different_arguments_conflicts() -> None:
    """兼容入口同一持久身份改参数时拒绝把它伪装成新派发。"""

    original_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    changed_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-2"})
    event = AgentEvent(
        id="evt_action_conflict",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": original_call.name,
            "success": True,
            "data": {"ticket_id": "TCK-ORIGINAL"},
            "persistent_action_id": "action-fixed",
            "request_fingerprint": SopExecutionStore.request_fingerprint(
                original_call.arguments
            ),
            "tool_call": original_call.model_dump(mode="json"),
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=original_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("改成另一个客户"),
        session,
        changed_call,
        persistent_action_id="action-fixed",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "SOP_EXECUTION_CONFLICT"
    assert executor.execute_calls == 0


def test_compatibility_entry_unknown_action_never_redispatches() -> None:
    """兼容入口已有 unknown 事实时必须停止并等待对账，不能再次调用执行器。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    unknown_event = AgentEvent(
        id="evt_action_unknown",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": tool_call.name,
            "success": False,
            "outcome_status": "unknown",
            "persistent_action_id": "action-unknown",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=tool_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [unknown_event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("继续重试"),
        session,
        tool_call,
        persistent_action_id="action-unknown",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
    assert executor.execute_calls == 0
    assert loop.events.records[0][3]["receipt_source"] == "compatibility_unknown"
    assert loop.events.records[1][3]["outcome_status"] == "unknown"


def test_compatibility_entry_retries_known_failure_with_same_identity() -> None:
    """兼容入口确定失败后允许同一持久动作重试，不把未完成起始事件误判为 unknown。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    failed_event = AgentEvent(
        id="evt_action_failed",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": tool_call.name,
            "success": False,
            "outcome_status": "failed",
            "persistent_action_id": "action-failed",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
            "tool_call_id": "temporary-failed",
        },
    )
    started_event = AgentEvent(
        id="evt_action_started",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_started",
        payload_json={
            "tool_name": tool_call.name,
            "persistent_action_id": "action-failed",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
            "tool_call_id": "temporary-failed",
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=tool_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [started_event, failed_event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("重试工单"),
        session,
        tool_call,
        tool_call_id="temporary-retry",
        persistent_action_id="action-failed",
    )

    assert result.success is True
    assert executor.execute_calls == 1


def test_compatibility_entry_checks_current_authorization_before_replay() -> None:
    """兼容入口撤权后不得复用历史事件成功数据。"""

    tool_call = ToolCall(name="crm.create_ticket", arguments={"customer_id": "C-1"})
    event = AgentEvent(
        id="evt_action_revoke",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": tool_call.name,
            "success": True,
            "data": {"ticket_id": "TCK-REVOKED"},
            "persistent_action_id": "action-revoke",
            "request_fingerprint": SopExecutionStore.request_fingerprint(tool_call.arguments),
            "tool_call": tool_call.model_dump(mode="json"),
        },
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name=tool_call.name,
        display_name="创建工单",
        method="POST",
        url="http://localhost:8000/api/mock/tickets",
        enabled=True,
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    executor.authorization_result = ToolResult(
        tool_name=tool_call.name,
        success=False,
        error=ToolError(code="ACTOR_PERMISSION_REQUIRED", message="权限已撤销。"),
    )
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    result = loop._execute_tool_call(
        _request("重试工单"),
        session,
        tool_call,
        persistent_action_id="action-revoke",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "ACTOR_PERMISSION_REQUIRED"
    assert result.data is None
    assert executor.execute_calls == 0


def test_post_read_only_tool_does_not_reuse_previous_result() -> None:
    tool = Tool(
        tenant_id="tenant_demo",
        name="order.query",
        display_name="查询订单",
        method="POST",
        url="http://localhost:8000/api/mock/order/query",
        config_json={"idempotency": {"enabled": False}},
        enabled=True,
    )
    event = AgentEvent(
        id="evt_existing_query_result",
        tenant_id="tenant_demo",
        session_id="session_test",
        event_type="tool_call_finished",
        payload_json={
            "tool_name": "order.query",
            "success": True,
            "data": {"order_id": "O-1", "status": "paid"},
            "tool_call": {"name": "order.query", "arguments": {"order_id": "O-1"}},
        },
    )
    db = FakeEventDb(tool, [event])
    executor = FakeToolExecutor(db)
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    loop.tool_executor = executor
    session = ChatSession(id="session_test", tenant_id="tenant_demo", active_skill_id="refund")

    result = loop._execute_tool_call(
        _request("查订单"),
        session,
        ToolCall(name="order.query", arguments={"order_id": "O-1"}),
    )

    assert result.success is True
    assert executor.commits_seen_before_execute == 1
    assert [record[2] for record in loop.events.records] == [
        "tool_call_started",
        "tool_call_finished",
    ]


@pytest.mark.parametrize("compacted_now", [False, True])
def test_stream_emits_context_status_only_when_compaction_runs(compacted_now: bool) -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="msg_user",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="你好",
    )

    loop.db = db
    loop.events = FakeEvents()
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: [],
        context_memory_payloads=lambda *_args, **_kwargs: [],
    )
    loop.runtime = SimpleNamespace(apply_decision=lambda *_args, **_kwargs: None)
    loop.router = SimpleNamespace(
        decide=lambda *_args, **_kwargs: RouterDecision(
            decision="answer_only",
            user_intent="问候",
            reason="普通问候，不需要进入业务流程。",
        )
    )
    loop._get_or_create_session = lambda _request: session
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: [_purchase_skill()]
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._finish_stale_completed_skill = lambda *_args, **_kwargs: None
    loop._scene_router_deferred_to_general = lambda *_args, **_kwargs: False
    loop._hydrate_router_decision_from_context = lambda *_args, **_kwargs: {}
    loop._conversation_context = lambda *_args, **_kwargs: {
        "metadata": {"compacted_now": compacted_now}
    }
    loop._get_active_skill = lambda *_args, **_kwargs: None
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: False
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()
    loop._generate_reply_stream_segment = lambda *_args, **_kwargs: iter(["收到"])
    loop._finalize_turn = lambda *_args, **_kwargs: "规范化回复"
    loop._recent_messages = lambda *_args, **_kwargs: []
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    events = list(loop.handle_turn_stream(_request("你好")))
    names = [event["event"] for event in events]
    router_index = names.index("router_decision")
    reply_index = names.index("stream_delta")

    preparing_indexes = [
        index
        for index, event in enumerate(events)
        if event["event"] == "status" and event["data"].get("phase") == "preparing"
    ]
    if compacted_now:
        assert len(preparing_indexes) == 1
        assert names.index("user_message_received") < preparing_indexes[0] < router_index
    else:
        assert preparing_indexes == []
    assert router_index < reply_index
    replace_index = names.index("stream_replace")
    end_index = names.index("stream_end")
    complete_index = names.index("complete")
    assert reply_index < replace_index < end_index < complete_index
    assert events[replace_index]["data"]["content"] == "规范化回复"
    assert events[complete_index]["data"]["reply"] == "规范化回复"
    router_payload = events[router_index]["data"]
    assert router_payload["user_intent"] == "问候"
    assert router_payload["reason"] == "普通问候，不需要进入业务流程。"


def test_stream_disconnect_does_not_persist_stop_event_without_cancel_flag() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )
    user_message = Message(
        id="msg_user",
        tenant_id="tenant_demo",
        session_id=session.id,
        role="user",
        content="你好",
    )

    loop.db = db
    loop.events = FakeEvents()
    loop.memory = SimpleNamespace(
        context_memories=lambda *_args, **_kwargs: [],
        context_memory_payloads=lambda *_args, **_kwargs: [],
    )
    loop.runtime = SimpleNamespace(apply_decision=lambda *_args, **_kwargs: None)
    loop.router = SimpleNamespace(
        decide=lambda *_args, **_kwargs: RouterDecision(
            decision="answer_only",
            user_intent="问候",
            reason="普通问候，不需要进入业务流程。",
        )
    )
    loop._get_or_create_session = lambda _request: session
    loop._append_message = lambda *_args, **_kwargs: user_message
    loop._get_request_model = lambda *_args, **_kwargs: _model_config()
    loop._list_published_skills = lambda *_args, **_kwargs: [_purchase_skill()]
    loop._list_enabled_tools = lambda *_args, **_kwargs: []
    loop._tools_with_general_skills = lambda *_args, **_kwargs: []
    loop._get_persona_prompt = lambda *_args, **_kwargs: None
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._finish_stale_completed_skill = lambda *_args, **_kwargs: None
    loop._scene_router_deferred_to_general = lambda *_args, **_kwargs: False
    loop._hydrate_router_decision_from_context = lambda *_args, **_kwargs: {}
    loop._conversation_context = lambda *_args, **_kwargs: {}
    loop._get_active_skill = lambda *_args, **_kwargs: None
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: False
    loop._auto_knowledge_step_result = lambda *_args, **_kwargs: StepAgentResult()

    def disconnected_reply_stream(*_args, **_kwargs):
        raise GeneratorExit
        yield ""

    loop._generate_reply_stream_segment = disconnected_reply_stream
    loop._finalize_turn = lambda *_args, **_kwargs: None
    loop._recent_messages = lambda *_args, **_kwargs: []
    loop._enqueue_memory_capture = lambda *_args, **_kwargs: None

    with pytest.raises(GeneratorExit):
        list(loop.handle_turn_stream(_request("你好")))

    assert db.rollbacks == 1
    assert "stream_cancelled" not in [record[2] for record in loop.events.records]


def test_stream_text_events_are_persisted_for_refresh_recovery() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        slots_json={},
        skill_stack_json=[],
        pending_tasks_json=[],
        knowledge_context_json=[],
    )

    loop.db = db
    loop.events = FakeEvents()

    payload = {"turn_id": "msg_user", "user_message_id": "msg_user", "content": "收到"}
    event = loop._stream_event("stream_delta", session, payload)

    assert event["event"] == "stream_delta"
    assert loop.events.records == [
        ("tenant_demo", "session_test", "stream_delta", {**payload, "seq": 1}),
    ]
    assert event["data"]["seq"] == 1
    assert db.commits == 1


def test_stream_trace_events_require_turn_id_for_persistence() -> None:
    db = FakeDb()
    loop = object.__new__(AgentLoop)
    loop.db = db
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )

    without_turn = {"toolName": "weather", "success": True}
    with_turn = {
        "turn_id": "msg_user",
        "user_message_id": "msg_user",
        "toolName": "weather",
        "success": True,
    }

    loop._stream_event("tool_result", session, without_turn)
    event = loop._stream_event("tool_result", session, with_turn)

    assert event["event"] == "tool_result"
    assert loop.events.records == [
        ("tenant_demo", "session_test", "tool_result", with_turn),
    ]
    assert db.commits == 1


def test_router_order_keeps_current_turn_followup_out_of_pending_tasks() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"user_name": "hm"},
    )
    router_decision = RouterDecision(
        decision="continue_active",
        target_skill_id="purchase",
        target_step_id="collect_user_name",
        confidence=0.91,
        user_intent="继续购买 A1，并比较 A1 和 A3",
        reason="用户补充购买目标，同时提出独立比价任务。",
        source_message="我买 A1 前跟 A3 比一下价格",
        slot_hints={"product_id": "A1", "quantity": 1},
        task_frames=[
            PendingTask(
                decision="continue_active",
                target_skill_id="purchase",
                target_step_id="collect_user_name",
                user_intent="继续购买 A1",
                source_message="我买 A1 前跟 A3 比一下价格",
                slot_hints={"product_id": "A1", "quantity": 1},
            ),
            PendingTask(
                task_id="task_price_compare_a1_a3",
                decision="start_new_task",
                target_skill_id="price_compare",
                target_step_id="collect_products",
                user_intent="比较 A1 和 A3 的价格",
                source_message="我买 A1 前跟 A3 比一下价格",
                slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
            )
        ],
    )

    loop.runtime.apply_decision(session, router_decision)

    assert session.active_skill_id == "purchase"
    assert session.active_step_id == "collect_user_name"
    assert session.slots_json == {"user_name": "hm", "product_id": "A1", "quantity": 1}
    assert session.pending_tasks_json == []
    assert [task.target_skill_id for task in router_decision.task_frames] == [
        "purchase",
        "price_compare",
    ]


def test_router_keeps_existing_active_task_in_current_turn_plan_after_new_primary() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"user_name": "hm"},
    )
    router_decision = RouterDecision(
        decision="start_new_task",
        target_skill_id="price_compare",
        target_step_id="collect_products",
        confidence=0.95,
        user_intent="比较 A1 和 A3 的价格",
        reason="用户提出独立比价任务。",
        source_message="我想买一个A1,然后想跟A3比下价格",
        slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
        task_frames=[
            PendingTask(
                decision="start_new_task",
                target_skill_id="price_compare",
                target_step_id="collect_products",
                user_intent="比较 A1 和 A3 的价格",
                source_message="我想买一个A1,然后想跟A3比下价格",
                slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
            ),
            PendingTask(
                task_id="task_purchase_a1",
                decision="continue_active",
                target_skill_id="purchase",
                target_step_id="collect_user_name",
                user_intent="继续购买 A1",
                source_message="我想买一个A1,然后想跟A3比下价格",
                slot_hints={"user_name": "hm"},
            )
        ],
    )

    loop.runtime.apply_decision(session, router_decision)

    assert session.active_skill_id == "price_compare"
    assert session.active_step_id == "collect_products"
    assert session.slots_json == {"product_name_1": "A1", "product_name_2": "A3"}
    assert session.pending_tasks_json == []
    assert [task.target_skill_id for task in router_decision.task_frames] == [
        "price_compare",
        "purchase",
    ]


def test_current_turn_task_frames_execute_in_order_without_pending_queue() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._generate_reply_segment = lambda *_args, **_kwargs: "已完成"

    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    executed: list[str] = []
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, session, active_skill, *_args, **_kwargs):
        executed.append(active_skill.skill_id)
        return StepAgentResult(reply="已完成", is_step_completed=True)

    def finalize(_tenant_id, session, active_skill, *_args, **_kwargs):
        loop.runtime.complete_current_skill(session)
        return "completed"

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = finalize
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        pending_tasks_json=[],
    )
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
            slot_hints={"product_name_1": "A1", "product_name_2": "A3"},
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
            slot_hints={"product_id": "A3", "quantity": 1},
        ),
    ]

    result = loop._try_continue_pending_after_completion(
        _request("先比较 A1 和 A3，再购买 A3"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        turn_task_frames=frames,
    )

    assert executed == ["price_compare", "purchase"]
    assert session.pending_tasks_json == []
    assert result is not None
    assert result.reply == "已完成\n\n已完成"


def test_pending_task_reentry_keeps_same_turn_actions_distinct_by_task_id() -> None:
    """真实待办续接中同 Skill/Step 的不同任务必须各自派发一次。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tool = _price_query_tool()
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="query_product",
            name="查询单个商品",
            status="published",
            content_json=_graph_content(
                "query_product",
                "查询单个商品",
                [
                    {
                        "node_id": "query",
                        "type": "tool_call",
                        "name": "查询商品",
                        "allowed_actions": ["call_tool:product.price_query"],
                    },
                    {"node_id": "reply", "name": "回复", "allowed_actions": ["answer_user"]},
                ],
            ),
        )
        overall = AgentProfile(
            id="overall_pending_tasks",
            tenant_id="tenant_demo",
            name="公开广场",
            is_overall=True,
            status="active",
        )
        chat = ChatSession(id="pending_task_reentry", tenant_id="tenant_demo")
        db.add_all([tool, skill, chat, overall])
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id)
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id)
        db.commit()

        loop = AgentLoop(db)
        executor = _SqlitePriceToolExecutor(db)
        loop.tool_executor = executor

        def model_response(*_args: object, **kwargs: object) -> StepAgentResult:
            """按当前待办任务槽位返回工具调用或工具后的确定性完成结果。"""

            product = str(chat.slots_json.get("product_name") or "")
            if kwargs.get("repair_reason") == "tool_continuation" or chat.active_step_id == "reply":
                return StepAgentResult(reply=f"已查询 {product}。", is_step_completed=True)
            return StepAgentResult(
                tool_call=ToolCall(
                    name=tool.name,
                    arguments={"product_name": product},
                )
            )

        loop._run_step_agent_once = model_response  # type: ignore[method-assign]
        loop._get_reflection_max_rounds = lambda _tenant_id: 0  # type: ignore[method-assign]
        request = _request("先查询 A1，再查询 A3")
        request.client_turn_id = "stable-pending-turn"
        frames = [
            PendingTask(
                task_id=f"task_{product}",
                target_skill_id=skill.skill_id,
                target_step_id="query",
                confidence=1.0,
                user_intent=f"查询 {product}",
                slot_hints={"product_name": product},
            )
            for product in ["A1", "A3"]
        ]

        result = loop._try_continue_pending_after_completion(
            request,
            chat,
            _model_config(),
            [skill],
            [tool],
            None,
            [],
            {},
            "前一任务已完成。",
            turn_task_frames=frames,
        )

        assert executor.products == ["A1", "A3"]
        assert result is not None and result.tool_result is not None
        assert result.tool_result.success is True
        started = db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.session_id == chat.id,
                AgentEvent.event_type == "tool_call_started",
            )
            .order_by(AgentEvent.created_at, AgentEvent.id)
        ).all()
        action_ids = [str((event.payload_json or {}).get("persistent_action_id") or "") for event in started]
        assert len(action_ids) == 2
        assert len(set(action_ids)) == 2
        assert "task:task_A1" in action_ids[0]
        assert "task:task_A3" in action_ids[1]


def test_only_started_waiting_task_becomes_pending_while_later_turn_frame_still_runs() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    executed: list[str] = []
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, session, active_skill, *_args, **_kwargs):
        executed.append(active_skill.skill_id)
        if active_skill.skill_id == "price_compare":
            session.awaiting_input_json = {"expected_fields": ["product_name_2"]}
            return StepAgentResult(reply="请补充第二个商品")
        return StepAgentResult(reply="购买完成", is_step_completed=True)

    def finalize(_tenant_id, session, active_skill, *_args, **_kwargs):
        if active_skill.skill_id == "price_compare":
            return "continued"
        loop.runtime.complete_current_skill(session)
        return "completed"

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = finalize
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
            slot_hints={"product_name_1": "A1"},
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
            slot_hints={"product_id": "A3", "quantity": 1},
        ),
    ]

    result = loop._try_continue_pending_after_completion(
        _request("先比较 A1 和另一个商品，再购买 A3"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        turn_task_frames=frames,
    )

    assert executed == ["price_compare", "purchase"]
    assert [frame["skill_id"] for frame in session.pending_tasks_json] == ["price_compare"]
    assert session.pending_tasks_json[0]["awaiting_input"] == {
        "expected_fields": ["product_name_2"]
    }
    assert result is not None
    assert result.reply == "请补充第二个商品\n\n购买完成"


def test_streamed_followup_tasks_collect_results_without_emitting_replies() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    loop._get_agent_loop_max_actions = lambda _tenant_id: 4
    loop._drop_unavailable_skill_state = lambda *_args, **_kwargs: False
    loop._should_record_runtime_event_after_prune = lambda *_args, **_kwargs: False
    loop._should_run_step_agent = lambda *_args, **_kwargs: True
    loop._get_reflection_max_rounds = lambda _tenant_id: 0
    loop._run_reflection_rounds = lambda *args, **_kwargs: tuple(args[5:9])
    loop._auto_progress_skill_graph = lambda *args, **_kwargs: tuple(args[5:9])
    loop._skill_state_payload = lambda *_args, **_kwargs: {}
    loop._runtime_stream_context = lambda *_args, **_kwargs: {}

    skills = [_price_compare_skill(), _purchase_skill()]
    skills_by_id = {skill.skill_id: skill for skill in skills}
    loop._get_active_skill = (
        lambda _tenant_id, skill_id, _agent_id: skills_by_id.get(skill_id or "")
    )

    def run_step(_request, _session, active_skill, *_args, **_kwargs):
        return StepAgentResult(
            action="ask_user",
            reply=f"{active_skill.name}需要补充信息",
        )

    loop._run_step_agent_with_context_repair = run_step
    loop._finalize_execution_after_reply = lambda *_args, **_kwargs: "continued"
    session = ChatSession(id="session_test", tenant_id="tenant_demo", pending_tasks_json=[])
    frames = [
        PendingTask(
            decision="start_new_task",
            target_skill_id="price_compare",
            target_step_id="collect_products",
        ),
        PendingTask(
            decision="start_new_task",
            target_skill_id="purchase",
            target_step_id="collect_user_name",
        ),
    ]

    iterator = loop._stream_continue_pending_after_completion(
        _request("先比价，再购买"),
        session,
        _model_config(),
        skills,
        [],
        None,
        [],
        {},
        "",
        user_message_id="msg_user",
        turn_task_frames=frames,
    )
    events: list[dict[str, object]] = []
    while True:
        try:
            events.append(next(iterator))
        except StopIteration as stop:
            result = stop.value
            break

    assert result is not None
    assert len(result.task_results) == 2
    assert [event["event"] for event in events].count("step_result") == 2
    assert not {"stream_delta", "stream_replace"}.intersection(
        event["event"] for event in events
    )


def test_drop_unavailable_skill_state_removes_disabled_sop_frames() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="archived_sop",
        active_step_id="collect_info",
        slots_json={"field": "value"},
        awaiting_input_json={"skill_id": "archived_sop", "step_id": "collect_info"},
        pending_tasks_json=[
            {"task_id": "task_archived", "target_skill_id": "archived_sop"},
            {"task_id": "task_purchase", "target_skill_id": "purchase"},
        ],
        skill_stack_json=[
            {"task_id": "stack_archived", "skill_id": "archived_sop"},
            {"task_id": "stack_purchase", "skill_id": "purchase"},
        ],
    )

    changed = loop._drop_unavailable_skill_state("tenant_demo", session, [_purchase_skill()])

    assert changed is True
    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.slots_json == {}
    assert session.awaiting_input_json is None
    assert session.pending_tasks_json == [
        {"task_id": "task_purchase", "target_skill_id": "purchase"}
    ]
    assert session.skill_stack_json == []
    assert loop.events.records[-1][2] == "skill_state_pruned"
    assert loop.events.records[-1][3]["removed_skill_ids"] == ["archived_sop"]


def test_skill_state_payload_filters_disabled_sop_frames() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="archived_sop",
        active_step_id="collect_info",
        pending_tasks_json=[
            {
                "task_id": "task_archived",
                "target_skill_id": "archived_sop",
                "target_step_id": "collect_info",
            },
            {
                "task_id": "task_purchase",
                "target_skill_id": "purchase",
                "target_step_id": "collect_user_name",
            },
        ],
        skill_stack_json=[
            {"task_id": "stack_archived", "skill_id": "archived_sop", "step_id": "collect_info"},
            {"task_id": "stack_purchase", "skill_id": "purchase", "step_id": "confirm_product"},
        ],
    )

    payload = loop._skill_state_payload(
        session,
        [_purchase_skill()],
        user_message_id="msg_current_turn",
    )

    assert payload["activeSkillId"] is None
    assert payload["activeStepId"] is None
    assert payload["user_message_id"] == "msg_current_turn"
    assert payload["turn_id"] == "msg_current_turn"
    assert payload["currentSkills"] == [
        {
            "skillId": "purchase",
            "name": "购买商品",
            "stepId": "collect_user_name",
            "state": "pending",
        },
    ]


def test_pruned_disabled_sop_runtime_event_is_not_recorded() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    decision = RouterDecision(
        decision="switch_to_pending",
        target_skill_id="archived_sop",
        target_step_id="collect_info",
    )

    assert (
        loop._should_record_runtime_event_after_prune(
            decision,
            session,
            [_purchase_skill()],
            state_pruned=True,
        )
        is False
    )


def test_finalize_turn_clears_stale_last_question_for_non_question_reply() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        last_agent_question="旧的比价回复。请问您是否决定购买 A1？",
    )
    reply = "好的，已为您确认退款申请。正在为您处理订单 MOCKD57272DB0E 的退款，请您耐心等待。"

    loop._finalize_turn(session, "tenant_demo", reply)

    assert session.last_agent_question == "旧的比价回复。请问您是否决定购买 A1？"
    assert session.summary == f"最近回复：{reply[:120]}"
    assert loop.events.records[0][2] == "assistant_message_created"


def test_finalize_turn_keeps_current_question_reply() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    reply = "请提供您的订单号？"

    loop._finalize_turn(session, "tenant_demo", reply)

    assert session.last_agent_question is None
    assert session.summary == f"最近回复：{reply[:120]}"


def test_finalize_turn_audits_runtime_control_reply_lineage() -> None:
    """验证控制回复的来源、渲染策略和错误码同时进入消息与审计事件。"""

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    step_result = StepAgentResult(action="reply", reply="权限不足")
    step_result.mark_runtime_control_reply("SUBJECT_OVERRIDE_FORBIDDEN")

    loop._finalize_turn(
        session,
        "tenant_demo",
        "权限不足",
        step_result=step_result,
        user_message_id="msg_user",
    )

    assistant_message = next(row for row in loop.db.added if isinstance(row, Message))
    event_payload = loop.events.records[0][3]
    expected_lineage = {
        "response_source": "runtime_control",
        "render_policy": "verbatim",
        "runtime_error_code": "SUBJECT_OVERRIDE_FORBIDDEN",
    }
    assert {key: assistant_message.metadata_json[key] for key in expected_lineage} == expected_lineage
    assert {key: event_payload[key] for key in expected_lineage} == expected_lineage


def test_finalize_turn_drops_unused_knowledge_citations() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        knowledge_context_json=[
            {
                "source_message": "自动任务需要结合业务资料",
                "evidence_pack": [
                    {
                        "source_path": "service-handbook.md / 服务原则 / evidence 1",
                        "excerpt": "服务人员应先确认用户真实诉求。",
                    }
                ],
            }
        ],
    )
    reply = "本次自动任务执行完毕，已成功购买 1 个 A1 商品。"

    loop._finalize_turn(session, "tenant_demo", reply, source_message="自动任务需要结合业务资料")

    message = loop.db.added[-1]
    assert isinstance(message, Message)
    assert message.content == reply
    assert message.metadata_json == {}
    assert "knowledge_citations" not in loop.events.records[0][3]


def test_finalize_turn_keeps_only_inline_knowledge_citations() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    step_result = StepAgentResult(
        knowledge_results=[
            {
                "query": {"query": "前端规范有哪些？"},
                "evidence_pack": [
                    {
                        "source_path": "frontend.md / 目录规范 / evidence 1",
                        "excerpt": "前端目录规范说明。",
                    },
                    {
                        "source_path": "frontend.md / 命名规范 / evidence 1",
                        "excerpt": "前端命名规范说明。",
                    },
                ],
            }
        ],
    )
    reply = "前端规范包括目录组织和命名规范。[2]\n\n参考资料：[1][2]"

    finalized_reply = loop._finalize_turn(
        session,
        "tenant_demo",
        reply,
        step_result=step_result,
        source_message="前端规范有哪些？",
    )

    message = loop.db.added[-1]
    assert isinstance(message, Message)
    assert message.content == "前端规范包括目录组织和命名规范。[1]"
    assert finalized_reply == message.content
    assert loop.events.records[0][3]["reply"] == finalized_reply
    assert [item["label"] for item in message.metadata_json["knowledge_citations"]] == ["[1]"]


def test_finalize_turn_keeps_same_title_knowledge_chunks() -> None:
    """最终回复保留同标题但不同切片身份的全部正文引用。"""

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    step_result = StepAgentResult(
        knowledge_results=[
            {
                "evidence_pack": [
                    {
                        "chunk_id": "chunk-a",
                        "section_path": "退款政策",
                        "excerpt": "第一段规则。",
                    },
                    {
                        "chunk_id": "chunk-b",
                        "section_path": "退款政策",
                        "excerpt": "第二段规则。",
                    },
                ]
            }
        ]
    )

    loop._finalize_turn(
        session,
        "tenant_demo",
        "两段规则分别适用于不同情形。[1]-[2]",
        step_result=step_result,
        source_message="退款政策是什么？",
    )

    message = loop.db.added[-1]
    assert isinstance(message, Message)
    assert [item["chunk_id"] for item in message.metadata_json["knowledge_citations"]] == [
        "chunk-a",
        "chunk-b",
    ]


def test_finalize_turn_restores_unique_truncated_email_from_citation() -> None:
    """最终落库前应从唯一引用恢复被模型省略号截断的邮箱。"""

    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")
    step_result = StepAgentResult(
        knowledge_results=[
            {
                "evidence_pack": [
                    {
                        "source_path": "employee-guide.md",
                        "excerpt": "材料请发送至 ops@example.test。",
                    }
                ]
            }
        ]
    )

    finalized_reply = loop._finalize_turn(
        session,
        "tenant_demo",
        "请发送到 ops@example... [1]",
        step_result=step_result,
    )

    message = loop.db.added[-1]
    assert finalized_reply == "请发送到 ops@example.test [1]"
    assert message.content == finalized_reply
    assert loop.events.records[0][3]["reply"] == finalized_reply


def test_merge_queued_reply_preserves_each_structured_execution_segment() -> None:
    loop = object.__new__(AgentLoop)
    refund_then_purchase = (
        "好的，已为您提交订单 MOCK7A17191FC9（商品 A1）的退款申请，退款原因为“不想要了”。\n\n"
        "接下来为您购买 A3 高阶商品，请确认以下信息：\n"
        "- 用户：hm\n"
        "- 商品：A3\n"
        "- 数量：1\n\n"
        "请问确认下单吗？"
    )
    purchase_confirmation = (
        "好的，hm。已为您确认购买 A3 高阶商品 1 件，价格 239.0 元。请问确认下单吗？"
    )

    replies, replaced = loop._merge_queued_reply_segment([], refund_then_purchase)
    replies, replaced = loop._merge_queued_reply_segment(replies, purchase_confirmation)

    assert replaced is False
    assert replies == [refund_then_purchase, purchase_confirmation]


def test_merge_queued_reply_keeps_distinct_followup_confirmations() -> None:
    loop = object.__new__(AgentLoop)
    first = "退款已处理。接下来为您购买 A1，请问确认下单吗？"
    second = "好的，hm。已为您确认购买 A3 高阶商品 1 件，价格 239.0 元。请问确认下单吗？"

    replies, replaced = loop._merge_queued_reply_segment([], first)
    replies, replaced = loop._merge_queued_reply_segment(replies, second)

    assert replaced is False
    assert replies == [first, second]


def test_apply_step_result_records_skill_context_for_step_change() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="skill_purchase_001",
        active_step_id="collect_user_name",
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="confirm_purchase"),
    )

    assert session.active_step_id == "confirm_purchase"
    event_type, payload = loop.events.records[0][2], loop.events.records[0][3]
    assert event_type == "skill_step_changed"
    assert payload["from_skill_id"] == "skill_purchase_001"
    assert payload["to_skill_id"] == "skill_purchase_001"
    assert payload["from_step_id"] == "collect_user_name"
    assert payload["to_step_id"] == "confirm_purchase"


def test_record_runtime_event_skips_noop_step_change() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="skill_purchase_001",
        active_step_id="collect_user_name",
    )

    loop._record_runtime_event(
        "tenant_demo",
        session,
        "skill_purchase_001",
        "collect_user_name",
        RouterDecision(
            decision="continue_active",
            target_skill_id="skill_purchase_001",
            target_step_id="collect_user_name",
        ),
    )

    assert loop.events.records == []


def test_apply_step_result_ignores_next_step_outside_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
    )
    step_result = StepAgentResult(next_step_id="collect_user_name", is_step_completed=True)

    loop._apply_step_result("tenant_demo", session, step_result, _refund_skill())

    assert session.active_step_id == "check_refund"
    assert step_result.next_step_id is None
    event_type, payload = loop.events.records[0][2], loop.events.records[0][3]
    assert event_type == "step_agent_result_repaired"
    assert payload["mode"] == "invalid_next_step_ignored"
    assert payload["invalid_next_step_id"] == "collect_user_name"


def test_apply_step_result_does_not_create_step_without_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="confirm_purchase"),
    )

    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert loop.events.records == []


def test_apply_step_result_queues_parallel_sibling_steps_and_merges() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _parallel_audit_skill(
        [
            {"source_node_id": "start", "next_node_id": "check_payee", "condition": "报文已获取"},
            {
                "source_node_id": "start",
                "next_node_id": "check_sensitive",
                "condition": "报文已获取",
            },
            {
                "source_node_id": "check_payee",
                "next_node_id": "report",
                "condition": "一致性检查完成",
            },
            {
                "source_node_id": "check_sensitive",
                "next_node_id": "report",
                "condition": "敏感词检查完成",
            },
        ]
    )
    session = ChatSession(
        id="session_parallel",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="start",
        slots_json={},
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="check_payee", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "check_payee"
    assert session.slots_json == {GRAPH_PENDING_STEPS_SLOT: ["check_sensitive"]}

    first_branch_result = StepAgentResult(next_step_id="report", is_step_completed=True)
    loop._apply_step_result("tenant_demo", session, first_branch_result, skill)

    assert session.active_step_id == "check_sensitive"
    assert first_branch_result.next_step_id == "check_sensitive"
    assert session.slots_json == {GRAPH_PENDING_STEPS_SLOT: ["report"]}

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="report", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "report"
    assert session.slots_json == {}
    assert [record[2] for record in loop.events.records].count("skill_step_changed") == 3


def test_apply_step_result_does_not_queue_exclusive_sibling_conditions() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    skill = _parallel_audit_skill(
        [
            {"source_node_id": "start", "next_node_id": "approve", "condition": "审核通过"},
            {"source_node_id": "start", "next_node_id": "reject", "condition": "审核拒绝"},
        ]
    )
    session = ChatSession(
        id="session_exclusive",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="start",
        slots_json={},
    )

    loop._apply_step_result(
        "tenant_demo",
        session,
        StepAgentResult(next_step_id="approve", is_step_completed=True),
        skill,
    )

    assert session.active_step_id == "approve"
    assert session.slots_json == {}


def test_terminal_skill_completion_when_required_slots_are_complete() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
    )

    assert loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(is_step_completed=True),
        None,
    )


def test_terminal_collect_step_can_complete_with_ask_user_action_when_slots_are_complete() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="collect_refund_reason",
        slots_json={"order_id": "A12345", "refund_reason": "不喜欢"},
    )

    assert loop._should_complete_skill(
        _refund_collect_terminal_skill(),
        session,
        StepAgentResult(is_step_completed=True, next_step_id="collect_refund_reason"),
        None,
    )


def test_stale_terminal_skill_is_cleared_before_next_route() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
    )

    loop._finish_stale_completed_skill("tenant_demo", session, [_repair_skill()])

    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.slots_json == {}
    assert loop.events.records[0][2] == "skill_completed"
    assert loop.events.records[0][3]["reason"] == "stale_terminal_state"


def test_scheduled_task_followup_can_continue_after_stale_terminal_completion() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
        pending_tasks_json=[
            {
                "task_id": "task_purchase_after_compare",
                "status": "pending",
                "skill_id": "purchase",
                "target_skill_id": "purchase",
                "step_id": "collect_user_name",
                "target_step_id": "collect_user_name",
                "slots": {"user_name": "hm"},
                "slot_hints": {"user_name": "hm"},
                "intent_summary": "购买比价后更贵的商品",
            }
        ],
    )
    request = _request("自动任务唤醒：完成维修后继续处理购买任务")
    request.interaction_mode = "scheduled_task"

    should_continue = loop._should_attempt_queued_task_followup(
        request,
        session,
        [_repair_skill(), _purchase_skill()],
        "维修结果已反馈。",
        1,
    )

    assert should_continue is True
    assert session.active_skill_id is None
    assert session.active_step_id is None
    assert session.pending_tasks_json[0]["task_id"] == "task_purchase_after_compare"
    assert [record[2] for record in loop.events.records] == [
        "skill_completed",
        "scheduled_task_followup_requested",
    ]


def test_normal_chat_does_not_auto_continue_pending_after_stale_terminal_completion() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    loop.db = FakeDb()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="reply_ticket_result",
        slots_json={"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
        pending_tasks_json=[
            {
                "task_id": "task_purchase_after_compare",
                "status": "pending",
                "skill_id": "purchase",
                "target_skill_id": "purchase",
                "step_id": "collect_user_name",
                "target_step_id": "collect_user_name",
                "slots": {"user_name": "hm"},
                "slot_hints": {"user_name": "hm"},
            }
        ],
    )

    should_continue = loop._should_attempt_queued_task_followup(
        _request("普通聊天继续处理"),
        session,
        [_repair_skill(), _purchase_skill()],
        "维修结果已反馈。",
        1,
    )

    assert should_continue is False
    assert session.active_skill_id == "repair_ticket"
    assert loop.events.records == []


def test_obsolete_suspended_stack_is_cleared() -> None:
    loop = object.__new__(AgentLoop)
    loop.runtime = SkillRuntime()
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="visitor_badge",
        active_step_id="collect_visit_info",
        skill_stack_json=[
            {
                "skill_id": "repair_ticket",
                "step_id": "reply_ticket_result",
                "slots": {"reporter_name": "hm", "asset_id": "EQ-9", "issue_desc": "无法开机"},
            }
        ],
    )

    loop._finish_stale_completed_skill("tenant_demo", session, [_repair_skill()])

    assert session.active_skill_id == "visitor_badge"
    assert session.skill_stack_json == []
    assert loop.events.records == []


def test_intermediate_step_with_next_step_is_not_completed() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="collect_repair_info",
        slots_json={"reporter_name": "hm"},
    )

    assert not loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(is_step_completed=True, next_step_id="reply_ticket_result"),
        None,
    )


def test_model_can_complete_non_terminal_skill_when_no_next_action() -> None:
    loop = object.__new__(AgentLoop)
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="repair_ticket",
        active_step_id="collect_repair_info",
        slots_json={"reporter_name": "hm"},
    )

    assert loop._should_complete_skill(
        _repair_skill(),
        session,
        StepAgentResult(reply="好的，已取消本次报修流程。", is_step_completed=True),
        None,
    )


def test_successful_tool_call_advances_to_final_reply_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
    )
    step_result = StepAgentResult(tool_call=_refund_tool_call(), is_step_completed=True)

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _refund_skill(),
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )

    assert advanced
    assert session.active_step_id == "reply_result"
    assert step_result.next_step_id == "reply_result"
    assert loop.events.records[0][2] == "skill_step_changed"


def test_answer_step_can_complete_even_if_distilled_order_has_later_satisfied_collect_step() -> (
    None
):
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="refund",
        active_step_id="check_refund",
        slots_json={"order_id": "A12345", "refund_reason": "商品质量"},
    )
    step_result = StepAgentResult(tool_call=_refund_tool_call(), is_step_completed=True)

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _refund_skill_with_late_collect_step(),
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )

    assert not advanced
    assert session.active_step_id == "check_refund"
    assert loop._should_complete_skill(
        _refund_skill_with_late_collect_step(),
        session,
        step_result,
        ToolResult(tool_name="order.query", success=True, data={"eligible": True}),
    )


def test_context_repair_does_not_auto_advance_satisfied_collect_step() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="您好 hm，请问您想购买的商品 ID 是什么？",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"product_id": "A3", "quantity": 1},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我叫hm"),
        session,
        _purchase_skill(),
        [_purchase_tool(), _order_add_tool()],
        _model_config(),
        RouterDecision(decision="continue_active", target_skill_id="purchase"),
    )

    assert session.active_step_id == "collect_user_name"
    assert loop.step_agent.calls == 1
    assert step_result.tool_call is None
    assert not any(
        event_type == "skill_step_changed" and payload.get("reason") == "expected_info_satisfied"
        for _, _, event_type, payload in loop.events.records
    )
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "schema_tool_call"
        for _, _, event_type, payload in loop.events.records
    )


def test_context_repair_does_not_infer_tool_when_router_is_clarifying() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [StepAgentResult(reply="请问您想办理哪类业务？", next_step_id="confirm_product")]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="confirm_product",
        slots_json={"product_id": "A1", "quantity": 1, "user_name": "hm"},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我想查询订单"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="clarify", target_skill_id="skill_order_query"),
    )

    assert step_result.tool_call is None
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "schema_tool_call"
        for _, _, event_type, payload in loop.events.records
    )


def test_model_slot_validation_retry_can_complete_missed_quantity() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="好的，hm。请问您想购买多少件 A1？",
                slot_updates={"user_name": "hm", "product_id": "A1"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="正在为您创建订单，请稍候。",
                slot_updates={"quantity": 1},
                tool_call=ToolCall(
                    name="product.purchase", arguments={"product_id": "A1", "quantity": 1}
                ),
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我要买一个A1，我叫hm"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="start_new_task", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 2
    assert session.slots_json["user_name"] == "hm"
    assert session.slots_json["product_id"] == "A1"
    assert session.slots_json["quantity"] == 1
    assert step_result.tool_call is not None
    assert step_result.tool_call.name == "product.purchase"
    assert any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )
    assert not any(
        event_type == "skill_step_changed" and payload.get("reason") == "expected_info_satisfied"
        for _, _, event_type, payload in loop.events.records
    )


def test_step_agent_receives_full_conversation_context_within_budget() -> None:
    rows = [
        Message(
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )
        for index in range(16)
    ]
    loop = object.__new__(AgentLoop)
    loop.db = FakeMessageDb(rows)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent([StepAgentResult(reply="ok")])
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    loop._run_step_agent_once(
        _request("message 15"),
        session,
        None,
        [],
        _model_config(),
        RouterDecision(decision="clarify"),
    )

    _args, kwargs = loop.step_agent.call_args[0]
    recent_messages = kwargs["recent_messages"]
    conversation_context = kwargs["conversation_context"]
    assert len(recent_messages) == 16
    assert recent_messages[0]["content"] == "message 0"
    assert recent_messages[-1]["content"] == "message 15"
    assert conversation_context["metadata"]["compacted"] is False
    assert conversation_context["metadata"]["total_messages"] == 16
    assert kwargs["current_knowledge"] is None


def test_all_agent_stages_share_the_same_full_conversation_context() -> None:
    rows = [
        Message(
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )
        for index in range(16)
    ]
    loop = object.__new__(AgentLoop)
    loop.db = FakeMessageDb(rows)
    session = ChatSession(id="session_test", tenant_id="tenant_demo")

    context = loop._conversation_context(session)

    assert len(context["messages"]) == 16
    assert context["messages"][0]["content"] == "message 0"
    assert context["messages"][-1]["content"] == "message 15"
    assert context["metadata"]["total_messages"] == 16


def test_model_slot_validation_retry_does_not_fill_without_model_progress() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(reply="请问您想购买多少件 A1？", next_step_id="collect_user_name"),
            StepAgentResult(reply="请问您想购买多少件 A1？", next_step_id="collect_user_name"),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={"product_id": "A1", "user_name": "hm"},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("随便看看"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="continue_active", target_skill_id="purchase"),
    )

    assert loop.step_agent.calls == 2
    assert "quantity" not in session.slots_json
    assert step_result.tool_call is None
    assert not any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )


def test_start_new_task_slot_validation_accepts_reply_repair() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="好的，hm！请问您想购买什么商品？另外，请提供您的姓名以便我们为您下单。",
                slot_updates={"user_name": "hm"},
                next_step_id="collect_user_name",
            ),
            StepAgentResult(
                reply="好的，hm！请问您想购买什么商品？需要购买多少件？",
                next_step_id="collect_user_name",
            ),
        ]
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="collect_user_name",
        slots_json={},
    )

    step_result = loop._run_step_agent_with_context_repair(
        _request("我想买东西"),
        session,
        _purchase_skill(),
        [_purchase_tool()],
        _model_config(),
        RouterDecision(decision="start_new_task", target_skill_id="purchase"),
        memory_context=[
            {
                "kind": "profile",
                "content": "hm",
                "metadata": {"key": "preferred_name"},
            }
        ],
        conversation_context={"messages": [{"role": "user", "content": "我想买东西"}]},
    )

    assert loop.step_agent.calls == 2
    assert session.slots_json["user_name"] == "hm"
    assert step_result.reply == "好的，hm！请问您想购买什么商品？需要购买多少件？"
    assert any(
        event_type == "step_agent_result_repaired" and payload.get("mode") == "slot_validation"
        for _, _, event_type, payload in loop.events.records
    )


def test_tool_step_self_loop_advances_to_reply_and_completes_after_success() -> None:
    loop = object.__new__(AgentLoop)
    loop.events = FakeEvents()
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="purchase",
        active_step_id="confirm_product",
        slots_json={"product_id": "A1", "quantity": 1, "user_name": "hm"},
    )
    step_result = StepAgentResult(
        tool_call=ToolCall(name="product.purchase", arguments={"product_id": "A1", "quantity": 1}),
        next_step_id="confirm_product",
        is_step_completed=True,
    )

    advanced = loop._advance_after_successful_tool(
        "tenant_demo",
        session,
        _purchase_skill_with_incomplete_required_info(),
        step_result,
        ToolResult(tool_name="product.purchase", success=True, data={"order_id": "MOCK-1"}),
    )

    assert advanced
    assert session.active_step_id == "reply_result"
    assert step_result.next_step_id == "reply_result"
    assert loop._should_complete_skill(
        _purchase_skill_with_incomplete_required_info(),
        session,
        step_result,
        ToolResult(tool_name="product.purchase", success=True, data={"order_id": "MOCK-1"}),
    )


def test_tool_continuation_is_model_driven_and_accumulates_results() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A3"}),
                is_step_completed=True,
            ),
            StepAgentResult(
                reply="A1 和 A3 均已查到，可以给出比价结果。",
                next_step_id="reply_result",
                is_step_completed=True,
            ),
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="query_price",
        slots_json={"product_name_1": "A1", "product_name_2": "A3"},
    )

    stream_events: list[tuple[str, dict[str, object]]] = []
    step_result, tool_result = loop._execute_tool_action_cycle(
        _request("我想比下 A1 和 A3 的价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        stream_events,
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1", "A3"]
    assert loop.step_agent.calls == 2
    assert tool_result is not None
    assert tool_result.data["product_name"] == "A3"
    assert step_result.tool_call is None
    assert session.active_step_id == "reply_result"
    assert len(session.slots_json["_tool_results"]) == 2
    tool_result_events = [payload for event, payload in stream_events if event == "tool_result"]
    assert len(tool_result_events) == 2
    assert tool_result_events[0]["toolCallId"] != tool_result_events[1]["toolCallId"]
    assert any(event == "agent_loop_continued" for event, _ in stream_events)


@pytest.mark.parametrize("client_turn_id", [None, "persisted-price-compare-turn"])
def test_actual_tool_cycle_keeps_same_turn_business_actions_independent(
    client_turn_id: str | None,
) -> None:
    """真实 SQLite 工具循环必须区分同轮同名工具的两个合法业务动作。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        skill = _price_compare_skill()
        tool = _price_query_tool()
        session = ChatSession(
            id="session_test",
            tenant_id="tenant_demo",
            active_skill_id=skill.skill_id,
            active_step_id="query_price",
            slots_json={"product_name_1": "A1", "product_name_2": "A3"},
        )
        db.add_all([skill, tool, session])
        db.commit()

        loop = AgentLoop(db)
        executor = _SqlitePriceToolExecutor(db)
        loop.tool_executor = executor
        continuations = iter(
            [
                StepAgentResult(
                    tool_call=ToolCall(
                        name=tool.name,
                        arguments={"product_name": "A3"},
                    )
                ),
                StepAgentResult(
                    reply="已查到两个商品。",
                    next_step_id="reply_result",
                    is_step_completed=True,
                ),
            ]
        )
        loop._run_step_agent_once = lambda *_args, **_kwargs: next(continuations)  # type: ignore[method-assign]
        request = _request("比较 A1 和 A3 价格")
        request.client_turn_id = client_turn_id

        _step_result, tool_result = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            _model_config(),
            StepAgentResult(
                tool_call=ToolCall(
                    name=tool.name,
                    arguments={"product_name": "A1"},
                )
            ),
        )

        assert executor.products == ["A1", "A3"]
        assert tool_result is not None and tool_result.success is True

        started_events = db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == request.tenant_id,
                AgentEvent.session_id == session.id,
                AgentEvent.event_type == "tool_call_started",
            )
        ).all()
        action_ids = [
            str((event.payload_json or {}).get("persistent_action_id") or "")
            for event in started_events
        ]
        if client_turn_id is None:
            assert action_ids == ["", ""]
            return

        assert len(action_ids) == 2
        assert len(set(action_ids)) == 2
        action_zero = next(
            item for item in action_ids if ":action:0:tool:product.price_query" in item
        )
        assert any(":action:1:tool:product.price_query" in item for item in action_ids)
        action_scope = "skill:price_compare:step:query_price"
        assert loop._trusted_tool_action_id(
            request,
            ToolCall(name=tool.name, arguments={"product_name": "A1"}),
            None,
            action_scope=action_scope,
            action_ordinal=0,
        ) == action_zero

        continuations = iter(
            [
                StepAgentResult(
                    tool_call=ToolCall(
                        name=tool.name,
                        arguments={"product_name": "A3"},
                    )
                ),
                StepAgentResult(
                    reply="已查到两个商品。",
                    next_step_id="reply_result",
                    is_step_completed=True,
                ),
            ]
        )
        loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            _model_config(),
            StepAgentResult(
                tool_call=ToolCall(
                    name=tool.name,
                    arguments={"product_name": "A1"},
                )
            ),
        )
        assert executor.products == ["A1", "A3"]

        changed_result = loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool.name, arguments={"product_name": "A9"}),
            tool_call_id="temporary-changed-arguments",
            persistent_action_id=action_zero,
        )
        assert changed_result.success is False
        assert changed_result.error is not None
        assert changed_result.error.code == "SOP_EXECUTION_CONFLICT"
        assert executor.products == ["A1", "A3"]


@pytest.mark.parametrize("second_product", ["A3", "A1"])
def test_graph_reentry_keeps_same_turn_actions_distinct_across_nodes(
    second_product: str,
) -> None:
    """真实图自动推进再次进入工具循环时必须为不同节点保留不同业务身份。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tool = _price_query_tool()
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id="price_two_nodes",
            name="分节点商品比价",
            status="published",
            content_json=_graph_content(
                "price_two_nodes",
                "分节点商品比价",
                [
                    {
                        "node_id": "query_a1",
                        "type": "tool_call",
                        "name": "查询第一个商品",
                        "allowed_actions": ["call_tool:product.price_query"],
                    },
                    {
                        "node_id": "query_a3",
                        "type": "tool_call",
                        "name": "查询第二个商品",
                        "allowed_actions": ["call_tool:product.price_query"],
                    },
                    {
                        "node_id": "reply",
                        "name": "回复结果",
                        "allowed_actions": ["answer_user"],
                    },
                ],
            ),
        )
        session = ChatSession(
            id="session_two_nodes",
            tenant_id="tenant_demo",
            active_skill_id=skill.skill_id,
            active_step_id="query_a1",
        )
        overall = AgentProfile(
            id="overall_two_nodes",
            tenant_id="tenant_demo",
            name="公开广场",
            is_overall=True,
            status="active",
        )
        db.add_all([skill, tool, session, overall])
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "skill", skill.id)
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id)
        db.commit()

        loop = AgentLoop(db)
        executor = _SqlitePriceToolExecutor(db)
        loop.tool_executor = executor
        loop._get_reflection_max_rounds = lambda _tenant_id: 0  # type: ignore[method-assign]
        continuations = iter(
            [
                StepAgentResult(reply="第一个商品已查询。", is_step_completed=True),
                StepAgentResult(
                    tool_call=ToolCall(
                        name=tool.name,
                        arguments={"product_name": second_product},
                    )
                ),
                StepAgentResult(reply="两个商品均已查询。", is_step_completed=True),
            ]
        )
        loop._run_step_agent_once = lambda *_args, **_kwargs: next(continuations)  # type: ignore[method-assign]
        request = _request("比较 A1 和 A3 价格")
        request.client_turn_id = "stable-two-node-turn"

        first_step, first_result = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            _model_config(),
            StepAgentResult(
                tool_call=ToolCall(
                    name=tool.name,
                    arguments={"product_name": "A1"},
                )
            ),
        )
        _active_skill, _router_decision, final_step, final_result = (
            loop._auto_progress_skill_graph(
                request,
                session,
                [skill],
                [tool],
                _model_config(),
                skill,
                RouterDecision(
                    decision="continue_active",
                    target_skill_id=skill.skill_id,
                    target_step_id=session.active_step_id,
                    confidence=1.0,
                ),
                first_step,
                first_result,
                conversation_context={},
            )
        )

        assert executor.products == ["A1", second_product]
        assert final_result is not None and final_result.success is True
        assert final_step.tool_call is None
        assert session.active_step_id == "reply"
        started_events = db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == request.tenant_id,
                AgentEvent.session_id == session.id,
                AgentEvent.event_type == "tool_call_started",
            )
            .order_by(AgentEvent.created_at, AgentEvent.id)
        ).all()
        action_ids = [
            str((event.payload_json or {}).get("persistent_action_id") or "")
            for event in started_events
        ]
        assert len(action_ids) == 2
        assert len(set(action_ids)) == 2
        assert "step:query_a1" in action_ids[0]
        assert "step:query_a3" in action_ids[1]

        restarted_loop = AgentLoop(db)
        restarted_executor = _SqlitePriceToolExecutor(db)
        restarted_loop.tool_executor = restarted_executor
        recovered_step, recovered = restarted_loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            None,
            StepAgentResult(
                tool_call=ToolCall(
                    name=tool.name,
                    arguments={"product_name": second_product},
                )
            ),
        )
        if second_product == "A3":
            assert recovered is not None and recovered.success is True
            assert recovered.data["receipt_source"] == "compatibility_event"
        else:
            assert recovered_step.tool_call is None
            assert recovered is not None and recovered.success is False
            assert recovered.error is not None
            assert recovered.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
        assert restarted_executor.products == []

        replay = restarted_loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool.name, arguments={"product_name": second_product}),
            tool_call_id="restart-temporary-retry",
            persistent_action_id=action_ids[1],
        )
        assert replay.success is True
        assert replay.data["receipt_source"] == "compatibility_event"
        assert restarted_executor.products == []

        changed = restarted_loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool.name, arguments={"product_name": "A9"}),
            tool_call_id="restart-temporary-conflict",
            persistent_action_id=action_ids[1],
        )
        assert changed.success is False
        assert changed.error is not None
        assert changed.error.code == "SOP_EXECUTION_CONFLICT"
        assert restarted_executor.products == []

        unknown_action_id = (
            "turn:stable-two-node-turn:scope:skill:price_two_nodes:"
            "step:query_unknown:action:0:tool:product.price_query"
        )
        db.add(
            AgentEvent(
                tenant_id=request.tenant_id,
                session_id=session.id,
                event_type="tool_call_started",
                payload_json={
                    "tool_name": tool.name,
                    "tool_call": ToolCall(
                        name=tool.name,
                        arguments={"product_name": "unknown"},
                    ).model_dump(mode="json"),
                    "persistent_action_id": unknown_action_id,
                    "request_fingerprint": AgentLoop._tool_request_fingerprint(
                        ToolCall(name=tool.name, arguments={"product_name": "unknown"})
                    ),
                    "tool_call_id": "unknown-temporary-call",
                },
            )
        )
        db.commit()
        unknown = restarted_loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool.name, arguments={"product_name": "unknown"}),
            tool_call_id="unknown-temporary-retry",
            persistent_action_id=unknown_action_id,
        )
        assert unknown.success is False
        assert unknown.error is not None
        assert unknown.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
        assert restarted_executor.products == []


class _SqlitePriceToolExecutor(ToolExecutor):
    """使用生产授权边界并在本地记录价格工具实际派发参数。"""

    def __init__(self, db: Session) -> None:
        """保存父类授权依赖和测试用的实际派发记录。"""

        super().__init__(db)
        self.products: list[str] = []

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
        **kwargs: object,
    ) -> ToolResult:
        """授权通过后记录商品参数并返回确定性价格回执，不访问外部 provider。"""

        authorization_error = self.authorize(
            tenant_id,
            tool_call,
            active_skill_id,
            agent_id,
            actor_user_id,
            **kwargs,
        )
        if authorization_error is not None:
            return authorization_error
        product_name = str(tool_call.arguments.get("product_name") or "")
        self.products.append(product_name)
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"product_name": product_name, "price": 100},
        )


def test_tool_continuation_respects_configured_action_limit() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A3"}),
                is_step_completed=True,
            )
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    loop._get_agent_loop_max_actions = lambda tenant_id: 1  # type: ignore[method-assign]
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="query_price",
        slots_json={"product_name_1": "A1", "product_name_2": "A3"},
    )

    loop._execute_tool_action_cycle(
        _request("我想比下 A1 和 A3 的价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        [],
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1"]
    assert loop.step_agent.calls == 1
    assert len(session.slots_json["_tool_results"]) == 1


def test_duplicate_tool_call_with_reply_completes_from_existing_tool_result() -> None:
    loop = object.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop.tool_executor = _RecordingPriceToolExecutor()
    loop.step_agent = _FakeStepAgent(
        [
            StepAgentResult(
                reply="A1 的价格已查到，可以继续。",
                tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
                is_step_completed=False,
            )
        ]
    )
    loop._recent_messages = lambda session: []  # type: ignore[method-assign]
    loop._tool_activity_payload = lambda tenant_id, name, result, *args: {  # type: ignore[method-assign]
        "toolName": name,
        "toolCallId": args[1] if len(args) > 1 else "",
        "content": result.model_dump(mode="json"),
        "success": result.success,
        "isError": not result.success,
    }
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_demo",
        active_skill_id="price_compare",
        active_step_id="step_query_price_1",
        slots_json={"product_name_1": "A1"},
    )

    step_result, tool_result = loop._execute_tool_action_cycle(
        _request("查 A1 价格"),
        session,
        _price_compare_skill(),
        [_price_query_tool()],
        _model_config(),
        StepAgentResult(
            tool_call=ToolCall(name="product.price_query", arguments={"product_name": "A1"}),
            is_step_completed=True,
        ),
        [],
    )

    assert [call.arguments["product_name"] for call in loop.tool_executor.calls] == ["A1"]
    assert tool_result is not None and tool_result.success is True
    assert step_result.tool_call is None
    assert step_result.is_step_completed is True
    assert step_result.reply == "A1 的价格已查到，可以继续。"
    assert not any(record[2] == "agent_loop_stopped" for record in loop.events.records)
    assert any(
        record[2] == "agent_loop_completed" and record[3]["mode"] == "respond_after_duplicate"
        for record in loop.events.records
    )


def _repair_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="repair_ticket",
        name="设备报修",
        content_json=_graph_content(
            "repair_ticket",
            "设备报修",
            [
                {
                    "node_id": "collect_repair_info",
                    "name": "收集报修信息",
                    "expected_user_info": ["reporter_name", "asset_id", "issue_desc"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "reply_ticket_result",
                    "name": "反馈工单结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
            required_info=["reporter_name", "asset_id", "issue_desc"],
        ),
        status="published",
    )


def _refund_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "check_refund",
                    "type": "tool_call",
                    "name": "核实退款条件",
                    "expected_user_info": ["order_id", "refund_reason"],
                    "allowed_actions": ["continue_flow", "call_tool:order.query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _parallel_audit_skill(edges: list[dict[str, object]]) -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="skill_parallel_audit",
        name="并行审核",
        content_json={
            "skill_id": "skill_parallel_audit",
            "name": "并行审核",
            "required_info": ["message_content"],
            "nodes": [
                {
                    "node_id": "start",
                    "type": "collect_info",
                    "name": "收集信息",
                    "instruction": "收集用户报文。",
                    "expected_user_info": ["message_content"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "check_payee",
                    "type": "condition",
                    "name": "收款方一致性检查",
                    "instruction": "检查收款方是否一致。",
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "check_sensitive",
                    "type": "condition",
                    "name": "敏感词检查",
                    "instruction": "检查敏感词。",
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "approve",
                    "type": "response",
                    "name": "通过",
                    "instruction": "反馈通过。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reject",
                    "type": "response",
                    "name": "拒绝",
                    "instruction": "反馈拒绝。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "report",
                    "type": "response",
                    "name": "生成报告",
                    "instruction": "汇总检查结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": str(edge["source_node_id"]),
                    "next_node_id": str(edge["next_node_id"]),
                    "condition": str(edge.get("condition") or ""),
                    "priority": index,
                }
                for index, edge in enumerate(edges)
            ],
            "start_node_id": "start",
            "terminal_node_ids": ["report", "approve", "reject"],
        },
        status="published",
    )


def _refund_collect_terminal_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "collect_order",
                    "name": "收集订单号",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _refund_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="order.query",
        display_name="订单查询",
        method="POST",
        url="http://localhost:8000/api/mock/order/query",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "refund_reason": {"type": "string"},
            },
            "required": ["order_id", "refund_reason"],
        },
        allowed_skills_json=["refund"],
        enabled=True,
    )


def _refund_skill_with_late_collect_step() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="售后退款流程",
        content_json=_graph_content(
            "refund",
            "售后退款流程",
            [
                {
                    "node_id": "collect_order",
                    "type": "tool_call",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "node_id": "check_refund",
                    "name": "查询退款资格",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                },
                {
                    "node_id": "collect_refund_reason",
                    "name": "收集退款原因",
                    "expected_user_info": ["refund_reason"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
            ],
            required_info=["order_id", "refund_reason"],
        ),
        status="published",
    )


def _refund_tool_call():
    return ToolCall(
        name="order.query",
        arguments={"order_id": "A12345", "refund_reason": "商品质量"},
    )


class _FakeStepAgent:
    def __init__(self, results: list[StepAgentResult]) -> None:
        self.results = results
        self.calls = 0
        self.call_args: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run(self, *args: object, **kwargs: object) -> StepAgentResult:
        self.call_args.append((args, kwargs))
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class _RecordingPriceToolExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> ToolResult:
        self.calls.append(tool_call)
        product_name = str(tool_call.arguments.get("product_name") or "")
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={
                "product_name": product_name,
                "found": True,
                "price": 129 if product_name == "A1" else 239,
            },
        )


def _request(message: str):
    from app.session.session_schema import ChatTurnRequest

    return ChatTurnRequest(tenant_id="tenant_demo", session_id="session_test", message=message)


def _model_config():
    from app.db.models import ModelConfig

    return ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo")


def _graph_content(
    skill_id: str,
    name: str,
    nodes: list[dict[str, object]],
    *,
    required_info: list[str] | None = None,
) -> dict[str, object]:
    normalized_nodes = [
        {
            "node_id": str(node["node_id"]),
            "type": node.get("type")
            or ("collect_info" if node.get("expected_user_info") else "response"),
            "name": str(node.get("name") or node["node_id"]),
            "instruction": str(node.get("instruction") or ""),
            "expected_user_info": list(node.get("expected_user_info") or []),
            "allowed_actions": list(node.get("allowed_actions") or []),
            "metadata": dict(node.get("metadata") or {}),
        }
        for node in nodes
    ]
    return {
        "skill_id": skill_id,
        "name": name,
        "required_info": required_info or [],
        "nodes": normalized_nodes,
        "edges": [
            {
                "source_node_id": normalized_nodes[index]["node_id"],
                "next_node_id": normalized_nodes[index + 1]["node_id"],
                "priority": index,
                "label": "默认推进",
            }
            for index in range(len(normalized_nodes) - 1)
        ],
        "start_node_id": normalized_nodes[0]["node_id"],
        "terminal_node_ids": [normalized_nodes[-1]["node_id"]],
    }


def _purchase_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品",
        content_json=_graph_content(
            "purchase",
            "购买商品",
            [
                {
                    "node_id": "collect_user_name",
                    "name": "收集用户与商品",
                    "expected_user_info": ["user_name", "product_id", "quantity"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "confirm_product",
                    "type": "tool_call",
                    "name": "创建订单",
                    "expected_user_info": ["product_id"],
                    "allowed_actions": ["call_tool:product.purchase", "call_tool:order.add"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈订单",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["user_name", "product_id", "quantity"],
        ),
        status="published",
    )


def _purchase_skill_with_incomplete_required_info() -> Skill:
    skill = _purchase_skill()
    skill.content_json = {
        **(skill.content_json or {}),
        "required_info": ["user_id", "product_id", "quantity"],
    }
    return skill


def _purchase_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="product.purchase",
        display_name="购买商品",
        method="POST",
        url="http://localhost:8000/api/mock/product/purchase",
        input_schema={
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "integer"},
                "user_id": {"type": "string"},
            },
            "required": ["product_id"],
        },
        enabled=True,
    )


def _order_add_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="order.add",
        display_name="订单添加",
        method="POST",
        url="http://localhost:8000/api/mock/order/add",
        input_schema={
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
        enabled=True,
    )


def _price_compare_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="price_compare",
        name="商品比价",
        content_json=_graph_content(
            "price_compare",
            "商品比价",
            [
                {
                    "node_id": "collect_products",
                    "name": "收集商品",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "query_price",
                    "type": "tool_call",
                    "name": "查询价格",
                    "expected_user_info": [],
                    "allowed_actions": ["call_tool:product.price_query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["product_name_1", "product_name_2"],
        ),
        status="published",
    )


def _price_query_tool() -> Tool:
    return Tool(
        tenant_id="tenant_demo",
        name="product.price_query",
        display_name="商品价格查询",
        method="POST",
        url="http://localhost:8000/api/mock/product/price-query",
        input_schema={
            "type": "object",
            "properties": {"product_name": {"type": "string"}},
            "required": ["product_name"],
        },
        enabled=True,
    )


def test_step_agent_tools_are_scoped_to_active_skill() -> None:
    loop = object.__new__(AgentLoop)
    purchase_skill = _purchase_skill()
    price_skill = _price_compare_skill()
    price_tool = _price_query_tool()
    price_tool.allowed_skills_json = [price_skill.skill_id]
    global_tool = _order_add_tool()

    purchase_tool_names = {
        tool.name
        for tool in loop._step_agent_tools(
            purchase_skill,
            [price_tool, global_tool],
            active_step_id="confirm_product",
            slots={"product_id": "A1"},
        )
    }
    price_tool_names = {
        tool.name
        for tool in loop._step_agent_tools(
            price_skill,
            [price_tool, global_tool],
            active_step_id="query_price",
        )
    }

    assert purchase_tool_names == {"order.add"}
    assert price_tool_names == {"product.price_query"}
    assert (
        loop._step_agent_tools(
            purchase_skill,
            [price_tool, global_tool],
            active_step_id="collect_user_name",
            slots={},
        )
        == []
    )
    assert loop._step_agent_tools(None, [price_tool, global_tool]) == []


def _refund_skill_with_tool_collect_step() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="refund",
        name="退款",
        content_json=_graph_content(
            "refund",
            "退款",
            [
                {
                    "node_id": "collect_order",
                    "type": "tool_call",
                    "name": "收集订单",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "call_tool:order.query"],
                },
                {
                    "node_id": "reply_result",
                    "name": "反馈结果",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            required_info=["order_id"],
        ),
        status="published",
    )
