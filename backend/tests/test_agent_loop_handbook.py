"""docs/Module_Description/agent_loop.md 手册论断的可执行探针。

每个测试对应手册中的一处论断（docstring 注明章节），
手册修改或代码演进后跑本文件即可发现手册与实现的偏差。
"""

from types import SimpleNamespace
from typing import get_args

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop
from app.core.cancellation import (
    cancel_chat_turn,
    clear_chat_turn_cancelled,
    is_chat_turn_cancelled,
)
from app.core.response_generator import ResponseGenerator
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.core.reflection_agent import ReflectionAgent
from app.db.models import AgentEvent, ChatSession, Skill
from app.general_skills.runner import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import GeneralSkillSelection
from app.llm import LLMClient, LLMError
from app.memory.service import MemoryService
from app.observability.event_log import EventLog
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


# ---------------------------------------------------------------- helpers


def _loop_off_init() -> AgentLoop:
    """绕过 __init__ 的纯逻辑探针实例（与既有测试同一模式）。"""
    return object.__new__(AgentLoop)


def _db_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _leave_skill() -> Skill:
    """请假 SOP：n1 收集（限 oa_submit 工具），n2 确认（终态）。"""
    return Skill(
        id="skill_row_1",
        tenant_id="tenant_demo",
        skill_id="sop_leave",
        version="1.0.0",
        name="请假",
        content_json={
            "skill_id": "sop_leave",
            "name": "请假",
            "start_node_id": "n1",
            "terminal_node_ids": ["n2"],
            "nodes": [
                {
                    "node_id": "n1",
                    "type": "collect_info",
                    "name": "收集信息",
                    "allowed_actions": ["call_tool:oa_submit"],
                    "expected_user_info": ["leave_date"],
                },
                {"node_id": "n2", "type": "confirm", "name": "确认"},
            ],
            "edges": [{"source_node_id": "n1", "next_node_id": "n2"}],
        },
        status="published",
    )


def _tool(name: str, *, enabled: bool = True, allowed_skills: list[str] | None = None):
    return SimpleNamespace(
        enabled=enabled,
        name=name,
        allowed_skills_json=allowed_skills or [],
    )


# ---------------------------------------------------------------- §3 AgentLoop 组装


def test_handbook_s3_init_assembles_nine_collaborators() -> None:
    """§3：__init__ 组装 9 个协作者。"""
    with _db_session() as db:
        loop = AgentLoop(db)
    assert isinstance(loop.events, EventLog)
    assert isinstance(loop.router, Router)
    assert isinstance(loop.runtime, SkillRuntime)
    assert isinstance(loop.step_agent, StepAgent)
    assert isinstance(loop.reflection_agent, ReflectionAgent)
    assert isinstance(loop.response_generator, ResponseGenerator)
    assert isinstance(loop.general_skill_selector, GeneralSkillSelector)
    assert isinstance(loop.general_skill_runner, GeneralSkillRunner)
    assert isinstance(loop.tool_executor, ToolExecutor)
    assert isinstance(loop.memory, MemoryService)


def test_handbook_s2_step_agent_action_vocabulary_is_exactly_seven() -> None:
    """§5.1：StepAgentResult.action 词汇表恰好 7 种。"""
    annotation = StepAgentResult.model_fields["action"].annotation
    literal = get_args(annotation)[0]  # Optional[Literal[...]] -> Literal[...]
    assert set(get_args(literal)) == {
        "ask_user",
        "clarify",
        "reply",
        "advance",
        "call_tool",
        "query_knowledge",
        "handoff",
    }


def test_handbook_s4_capability_selection_defaults_are_off() -> None:
    """§4.1⑦：能力布尔值默认关闭，但知识模式默认 auto，允许有界相关性预检索。"""
    selection = GeneralSkillSelection()
    assert selection.use_general_skill is False
    assert selection.use_knowledge is False
    assert selection.selected_slug is None
    assert selection.knowledge_query is None
    assert selection.knowledge_mode == "auto"


# ---------------------------------------------------------------- §4.1 分支矩阵


def test_handbook_s4_should_run_step_agent_matrix() -> None:
    """§4.1⑧：有 active_skill 且决策不在排除清单（6 种）才跑 Step Agent。"""
    loop = _loop_off_init()
    skill = SimpleNamespace()
    assert loop._should_run_step_agent(RouterDecision(decision="start_new_task"), None) is False
    for excluded in (
        "answer_only",
        "clarify",
        "create_pending",
        "update_pending",
        "complete_task",
        "handoff_human",
    ):
        assert (
            loop._should_run_step_agent(RouterDecision(decision=excluded), skill) is False
        ), excluded
    for allowed in ("start_new_task", "continue_active", "switch_to_pending"):
        assert (
            loop._should_run_step_agent(RouterDecision(decision=allowed), skill) is True
        ), allowed


def test_handbook_s4_scene_router_deferred_matrix() -> None:
    """§4.1⑦：answer_only/clarify 且无任务载荷时，才给通用技能二次机会。"""
    loop = _loop_off_init()
    assert loop._scene_router_deferred_to_general(RouterDecision(decision="answer_only")) is True
    assert loop._scene_router_deferred_to_general(RouterDecision(decision="clarify")) is True
    assert loop._scene_router_deferred_to_general(
        RouterDecision(decision="start_new_task")
    ) is False
    # 有任何任务载荷（task_frames/pending/created/updates/selected_task_id）→ 不放手
    task = {"task_id": "t1", "target_skill_id": "sop_leave"}
    assert (
        loop._scene_router_deferred_to_general(
            RouterDecision(decision="answer_only", task_frames=[task])
        )
        is False
    )
    assert (
        loop._scene_router_deferred_to_general(
            RouterDecision(decision="answer_only", selected_task_id="t1")
        )
        is False
    )


def test_handbook_s4_auto_knowledge_uses_probe_and_disabled_still_short_circuits() -> None:
    """§4.1⑦：auto 执行词法预检索；只有 disabled 明确短路企业知识。"""

    loop = _loop_off_init()
    calls: list[str] = []
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_items_for_message = lambda *_args, **_kwargs: calls.append("probe") or None
    request = ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="查下制度")
    session = ChatSession(id="s1", tenant_id="tenant_demo")
    model = SimpleNamespace()

    result_none = loop._auto_knowledge_step_result(
        request, session, model, RouterDecision(decision="answer_only"), None
    )
    assert result_none.knowledge_query is None
    assert result_none.knowledge_results == []

    result_off = loop._auto_knowledge_step_result(
        request,
        session,
        model,
        RouterDecision(decision="answer_only"),
        GeneralSkillSelection(knowledge_mode="disabled"),
    )
    assert result_off.knowledge_query is None
    assert result_off.knowledge_results == []
    assert calls == ["probe"]


def test_handbook_s4_capability_selection_llm_failure_enters_observable_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1⑦：选择器失败返回可观察的 auto 模式，后续仍可执行有界知识预检索。"""
    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)

    def _raise(self, system_prompt, payload):  # noqa: ANN001
        raise LLMError("boom")

    monkeypatch.setattr(LLMClient, "generate_json", _raise)
    with _db_session() as db:
        loop = AgentLoop(db)
        skill, selection = loop._select_general_capability(
            "查一下明天北京天气", SimpleNamespace(tenant_id="tenant_demo"), None
        )
    assert skill is None
    assert selection.use_general_skill is False
    assert selection.use_knowledge is False
    assert selection.knowledge_mode == "auto"
    assert selection.degraded is True
    assert selection.failure_code == "capability_selection_failed"
    assert (selection.reason or "").startswith("Capability selection failed")


# ---------------------------------------------------------------- §5 工具链


def test_handbook_s5_step_agent_tools_requires_active_skill() -> None:
    """§5.2：active_skill 为 None → 工具列表为空（无 SOP 即无工具入口的另一层佐证）。"""
    loop = _loop_off_init()
    assert loop._step_agent_tools(None, [_tool("oa_submit")]) == []


def test_handbook_s5_step_agent_tools_explicit_name_only() -> None:
    """§5.2：节点写 call_tool:oa_submit → 只给这个工具；其他工具被过滤。"""
    loop = _loop_off_init()
    skill = _leave_skill()
    tools = [_tool("oa_submit"), _tool("weather_query")]
    scoped = loop._step_agent_tools(skill, tools, active_step_id="n1")
    assert [t.name for t in scoped] == ["oa_submit"]


def test_handbook_s5_step_agent_tools_wildcard_and_allowed_skills_scope() -> None:
    """§5.2：节点写 call_tool → 给全部普通工具；但工具自身 allowed_skills_json
    不含当前 SOP 时仍被过滤。"""
    loop = _loop_off_init()
    skill = _leave_skill()
    skill.content_json["nodes"][0]["allowed_actions"] = ["call_tool"]
    tools = [
        _tool("oa_submit"),
        _tool("crm_query", allowed_skills=["sop_other"]),  # 限定别的 SOP → 过滤
        _tool("weather_query", allowed_skills=["sop_leave"]),  # 限定本 SOP → 保留
        _tool("disabled_tool", enabled=False),  # 停用 → 过滤
    ]
    scoped = loop._step_agent_tools(skill, tools, active_step_id="n1")
    assert sorted(t.name for t in scoped) == ["oa_submit", "weather_query"]


def test_handbook_s5_step_agent_tools_general_skill_gated_by_selector() -> None:
    """§5.2：general_skill.* 伪工具不随普通工具下发；allow_general_skill_selection=False
    或没有模型配置时绝不进入候选。"""
    loop = _loop_off_init()
    skill = _leave_skill()
    skill.content_json["nodes"][0]["allowed_actions"] = ["call_tool"]
    tools = [_tool("oa_submit"), _tool("general_skill.weather-zh")]
    scoped_blocked = loop._step_agent_tools(
        skill,
        tools,
        active_step_id="n1",
        allow_general_skill_selection=False,
    )
    assert [t.name for t in scoped_blocked] == ["oa_submit"]
    # 允许选择但没有 model_config → 选择器不工作，伪工具仍不进列表
    scoped_no_model = loop._step_agent_tools(
        skill, tools, active_step_id="n1", allow_general_skill_selection=True
    )
    assert [t.name for t in scoped_no_model] == ["oa_submit"]


def test_handbook_s5_tool_call_permission_gate_not_allowed() -> None:
    """§5.5 闸口①：普通工具不在员工启用列表 → NOT_ALLOWED，且不真正执行。"""
    with _db_session() as db:
        db.add(
            ChatSession(
                id="s_gate",
                tenant_id="tenant_demo",
                agent_id="agent_missing",  # 员工不存在 → 可见工具为空
                active_skill_id="sop_leave",
                active_step_id="n1",
            )
        )
        db.commit()
        loop = AgentLoop(db)
        loop.tool_executor = SimpleNamespace(  # 若被调用即失败，证明闸口拦截
            execute=lambda *a, **k: pytest.fail("tool_executor 不应被调用")
        )
        result = loop._execute_tool_call(
            ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="提交"),
            db.get(ChatSession, "s_gate"),
            ToolCall(name="oa_submit", arguments={"date": "明天"}),
        )
        assert result.success is False
        assert result.error is not None and result.error.code == "NOT_ALLOWED"
        event_types = db.exec(
            select(AgentEvent.event_type).where(AgentEvent.session_id == "s_gate")
        ).all()
        assert "tool_call_started" in event_types
        assert "tool_call_finished" in event_types


def test_handbook_s5_tool_signature_is_name_plus_arguments() -> None:
    """§5.4 步骤2：防死循环依据'工具名+参数'签名——同参同名同签名，异参异签名。"""
    loop = _loop_off_init()
    call_a = ToolCall(name="oa_submit", arguments={"date": "明天"})
    call_b = ToolCall(name="oa_submit", arguments={"date": "明天"})
    call_c = ToolCall(name="oa_submit", arguments={"date": "后天"})
    call_d = ToolCall(name="weather_query", arguments={"date": "明天"})
    assert loop._tool_call_signature(call_a) == loop._tool_call_signature(call_b)
    assert loop._tool_call_signature(call_a) != loop._tool_call_signature(call_c)
    assert loop._tool_call_signature(call_a) != loop._tool_call_signature(call_d)


def test_handbook_s5_apply_step_result_merges_slots_and_repairs_invalid_next_step() -> None:
    """§5.3：slot_updates 并入会话（记 slot_updated）；非法 next_step_id 被忽略
    并记 step_agent_result_repaired(mode=invalid_next_step_ignored)。"""
    with _db_session() as db:
        db.add(
            ChatSession(
                id="s_apply",
                tenant_id="tenant_demo",
                active_skill_id="sop_leave",
                active_step_id="n1",
                slots_json={},
            )
        )
        db.commit()
        loop = AgentLoop(db)
        step_result = StepAgentResult(
            action="advance",
            slot_updates={"leave_date": "明天"},
            next_step_id="node_not_in_graph",
        )
        loop._apply_step_result(
            "tenant_demo", db.get(ChatSession, "s_apply"), step_result, _leave_skill()
        )
        db.commit()
        session = db.get(ChatSession, "s_apply")
        assert session.slots_json == {"leave_date": "明天"}
        assert step_result.next_step_id is None  # 非法节点被清空
        assert session.active_step_id == "n1"  # 未发生节点切换
        events = db.exec(
            select(AgentEvent).where(AgentEvent.session_id == "s_apply")
        ).all()
        by_type = {e.event_type: e for e in events}
        assert "slot_updated" in by_type
        repaired = by_type.get("step_agent_result_repaired")
        assert repaired is not None
        assert repaired.payload_json.get("mode") == "invalid_next_step_ignored"
        assert repaired.payload_json.get("invalid_next_step_id") == "node_not_in_graph"


# ---------------------------------------------------------------- §4.4 收尾


def test_handbook_s4_reply_citation_labels_are_clamped() -> None:
    """§4.4：回复中越界的引用编号收敛到实际引用数，合法编号保留。"""
    loop = _loop_off_init()
    citations = [{"label": "员工手册"}, {"label": "考勤制度"}]
    reply = "按制度[1]执行，另见[2]，以及编造来源[9]。"
    normalized = loop._normalize_reply_citation_labels(reply, citations)
    assert "[1]" in normalized and "[2]" in normalized
    assert "[9]" not in normalized
    assert normalized.count("[2]") == 2  # [9] 被收敛为 [2]
    # 无引用时不做处理
    assert loop._normalize_reply_citation_labels(reply, []) == reply


def test_handbook_s4_strip_trailing_citation_summary() -> None:
    """§4.4：剥掉回复末尾模型惯用的'参考资料：[1] [2]'尾巴。"""
    loop = _loop_off_init()
    reply = "已为您提交申请。\n参考资料：[1] [2]"
    assert loop._strip_trailing_citation_summary(reply) == "已为您提交申请。"
    untouched = "已为您提交申请，参考[1]。"
    assert loop._strip_trailing_citation_summary(untouched) == untouched


def test_handbook_s4_fallback_session_title() -> None:
    """§4.4：会话标题缺失时用首条消息生成——去结尾标点、压缩空白、截 28 字。"""
    assert AgentLoop._fallback_session_title_from_message("  帮我  请假。 ") == "帮我 请假"
    assert AgentLoop._fallback_session_title_from_message("？！") == ""
    long_msg = "这是一个非常长的用户消息用来验证标题截取逻辑是否按照二十八个字符截断"
    assert len(AgentLoop._fallback_session_title_from_message(long_msg)) == 28


# ---------------------------------------------------------------- §4.1④ 无 SOP 短路详解


def test_handbook_s4_selector_rejects_slug_outside_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1④【A】②：选择器返回的 selected_slug 必须在 published 候选里，
    否则强制 use_general_skill=False（防模型编造 slug）。"""
    from app.db.models import GeneralSkill
    from app.general_skills.runner import GeneralSkillSelector

    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)
    monkeypatch.setattr(
        LLMClient,
        "generate_json",
        lambda self, system_prompt, payload: {
            "use_general_skill": True,
            "selected_slug": "skill-not-exists",
            "use_knowledge": False,
            "confidence": 0.9,
        },
    )
    candidate = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        skill_markdown="# 天气",
        status="published",
    )
    decision = GeneralSkillSelector().decide("查天气", [candidate], SimpleNamespace())
    assert decision.use_general_skill is False
    assert decision.selected_slug is None


def test_handbook_s4_capability_corrects_unknown_slug_and_unavailable_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1④【A】④b：未知 slug 与服务端确认不可用的知识能力都必须被纠正。"""
    from app.db.models import GeneralSkill as GeneralSkillRow

    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)
    monkeypatch.setattr(
        LLMClient,
        "generate_json",
        lambda self, system_prompt, payload: {
            "use_general_skill": True,
            "selected_slug": "ghost-slug",
            "use_knowledge": True,
            "knowledge_query": "出差规定",
            "confidence": 0.8,
        },
    )
    with _db_session() as db:
        db.add(
            GeneralSkillRow(
                tenant_id="tenant_demo",
                slug="weather-zh",
                name="中国城市天气",
                skill_markdown="# 天气",
                status="published",
            )
        )
        db.commit()
        loop = AgentLoop(db)
        skill, selection = loop._select_general_capability(
            "查天气", SimpleNamespace(tenant_id="tenant_demo"), None
        )
    assert skill is None
    assert selection.use_general_skill is False  # 被纠正
    assert selection.selected_slug is None
    assert selection.use_knowledge is False
    assert selection.knowledge_mode == "disabled"


def test_handbook_s4_general_skill_gate_blocks_when_router_picked_task() -> None:
    """§4.1④【C】第 1 步：路由选了 start_new_task 时闸门关闭，直接返回 None，
    通用技能不抢 SOP 的活。"""
    from app.db.models import GeneralSkill

    loop = _loop_off_init()
    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        skill_markdown="# 天气",
        status="published",
    )
    capability = (skill, GeneralSkillSelection(use_general_skill=True, selected_slug="weather-zh"))
    result = loop._try_handle_general_skill_after_scene_router(
        ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="查天气"),
        ChatSession(id="s_gate2", tenant_id="tenant_demo"),
        SimpleNamespace(),
        RouterDecision(decision="start_new_task", target_skill_id="sop_leave"),
        capability=capability,
    )
    assert result is None


def test_handbook_s4_general_skill_agent_outputs_conversion() -> None:
    """§4.1④【C】第 5 步：run_response → (StepAgentResult, ToolResult) 的转换规则——
    success=structured_result.success 且 stderr 为空；工具名 general_skill.<slug>。"""
    from app.general_skills.schema import GeneralSkillRunResponse

    loop = _loop_off_init()
    ok = GeneralSkillRunResponse(
        skill_slug="weather-zh",
        execution_trace=[],
        generated_code="echo ok",
        stdout='{"success": true}',
        stderr="",
        structured_result={"success": True, "data": {"temp": "5~15℃"}},
        reply="明天北京晴。",
    )
    step_ok, tool_ok = loop._general_skill_agent_outputs(ok)
    assert tool_ok.tool_name == "general_skill.weather-zh"
    assert tool_ok.success is True
    assert tool_ok.data["structured_result"] == {"success": True, "data": {"temp": "5~15℃"}}
    assert step_ok.reply == "明天北京晴。"
    assert step_ok.is_step_completed is True
    assert step_ok.tool_call is None

    bad = GeneralSkillRunResponse(
        skill_slug="weather-zh",
        execution_trace=[],
        generated_code="echo bad",
        stdout="",
        stderr="boom",
        structured_result={"success": False, "error": "runner_timeout"},
        reply="执行失败。",
    )
    step_bad, tool_bad = loop._general_skill_agent_outputs(bad)
    assert tool_bad.success is False
    assert tool_bad.data is None
    assert tool_bad.error is not None and tool_bad.error.code == "GENERAL_SKILL_FAILED"
    assert step_bad.is_step_completed is False


# ---------------------------------------------------------------- §8 取消机制


def test_handbook_s8_cancellation_marks() -> None:
    """§8：取消标记语义——打标/查询/清除；空参数不打标。"""
    cancel_chat_turn("session_probe", "turn_1")
    assert is_chat_turn_cancelled("session_probe", "turn_1") is True
    assert is_chat_turn_cancelled("session_probe", "turn_2") is False
    clear_chat_turn_cancelled("session_probe", "turn_1")
    assert is_chat_turn_cancelled("session_probe", "turn_1") is False
    cancel_chat_turn("", "turn_x")
    assert is_chat_turn_cancelled("", "turn_x") is False
