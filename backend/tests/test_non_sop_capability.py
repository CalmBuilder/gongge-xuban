"""
@Time       : 2026/08/03 18:30
@Author     : zhanglp8181
@File       : test_non_sop_capability.py
@CallChain  : pytest → NonSopCapabilityRouter → GeneralSkill/动态任务 shadow 决策
@Description: 验证 A 批非 SOP 兼容分流、失效降级和脱敏审计契约。
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.agent_loop import AgentLoop
from app.core.non_sop_capability import (
    LlmDynamicTaskShadowSelector,
    NonSopCapabilityDecision,
    NonSopCapabilityRouteResult,
    NonSopCapabilityRouter,
)
from app.db.models import ChatSession, GeneralSkill, Skill
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgentError
from app.dynamic_tasks.quotas import DynamicTaskQuotaError
from app.general_skills.schema import GeneralSkillSelection
from app.session.session_schema import ChatAttachmentRef, ChatTurnRequest, RouterDecision


class _GeneralSelector:
    """返回预置通用能力选择并记录调用次数。"""

    def __init__(self, selection: GeneralSkillSelection) -> None:
        self.selection = selection
        self.calls = 0

    def decide(self, *_args: object, **_kwargs: object) -> GeneralSkillSelection:
        """返回测试指定的通用能力决策。"""

        self.calls += 1
        return self.selection


class _ShadowSelector:
    """返回预置 shadow 决策或抛出预置异常。"""

    def __init__(
        self,
        decision: NonSopCapabilityDecision | None = None,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.error = error
        self.calls = 0

    def decide(self, *_args: object, **_kwargs: object) -> NonSopCapabilityDecision:
        """模拟动态任务 shadow 模型调用。"""

        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


def test_empty_skill_and_knowledge_catalog_enters_primary_selector() -> None:
    """无 Skill 且无知识候选时跳过旧 Skill 选择器，但仍进入动态主路由。"""

    general_selector = _GeneralSelector(GeneralSkillSelection())
    router = NonSopCapabilityRouter(
        shadow_enabled=True,
        execution_enabled=True,
        shadow_selector=_ShadowSelector(
            NonSopCapabilityDecision(
                mode="dynamic_task",
                goal="分析隔离工作区中的故障",
                success_criteria=["形成可校验诊断"],
                confidence=0.95,
            )
        ),
    )

    result = router.decide(
        message="分析隔离工作区中的故障",
        general_skills=[],
        model_config=SimpleNamespace(),
        general_skill_selector=general_selector,
        conversation_context=None,
        memory_context=None,
        knowledge_capability={"available": False, "accessible_count": 0},
    )

    assert general_selector.calls == 0
    assert result.primary_attempted is True
    assert result.shadow_attempted is False
    assert result.primary_decision is not None
    assert result.primary_decision.mode == "dynamic_task"
    assert result.effective_decision.mode == "dynamic_task"


def test_shadow_selector_receives_skill_tool_requirements(monkeypatch) -> None:
    """执行模式路由必须看到服务端审定工具声明，但不得把声明误解为本轮必调用。"""

    captured: dict[str, object] = {}

    class _Client:
        """捕获动态路由阶段的结构化能力目录。"""

        def __init__(self, *_args, **_kwargs) -> None:
            """兼容 LLMClient 构造签名。"""

        def generate_json(self, _system: str, payload: dict[str, object]):
            """返回轻量回答，同时保存模型实际获得的 Skill 元数据。"""

            captured.update(payload)
            return {"mode": "answer", "confidence": 1.0}

    monkeypatch.setattr("app.core.non_sop_capability.LLMClient", _Client)
    skill = _skill()
    skill.permissions_json = {"requested_tools": ["crm.customer.read"]}
    decision = LlmDynamicTaskShadowSelector(1.0).decide(
        "解释退款规则",
        [skill],
        SimpleNamespace(),
        None,
        None,
        {"available": False},
    )

    assert decision.mode == "answer"
    stage_skills = captured["general_skills"]
    assert stage_skills == [
        {
            "slug": "weather-zh",
            "name": "中国城市天气",
            "description": None,
            "requested_tools": ["crm.customer.read"],
        }
    ]


def _skill() -> GeneralSkill:
    """构造已发布且可由服务端解析的通用技能候选。"""

    return GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        skill_markdown="# 天气",
        status="published",
    )


def _route(
    router: NonSopCapabilityRouter,
    general_selector: _GeneralSelector,
    skills: list[GeneralSkill] | None = None,
):
    """用固定上下文调用非 SOP 路由，减少测试样板。"""

    return router.decide(
        message="整理两个系统的数据并生成风险简报",
        general_skills=skills or [],
        model_config=SimpleNamespace(tenant_id="tenant_demo"),
        general_skill_selector=general_selector,
        conversation_context={"messages": [{"role": "user", "content": "历史消息"}]},
        memory_context=[{"kind": "profile", "content": "内部记忆"}],
        knowledge_capability={"available": True, "accessible_count": 1},
    )


def test_dynamic_execution_status_text_matches_current_input_scope() -> None:
    """动态任务进度文案必须区分本轮有无附件，避免普通问题被误报为读取日志。"""

    without_attachment = ChatTurnRequest(
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="计算机专业，山东所有大学排名",
    )
    with_attachment = without_attachment.model_copy(
        update={
            "attachments": [
                ChatAttachmentRef(
                    resource_id="resource_demo",
                    resource_version="v1",
                )
            ]
        }
    )

    assert AgentLoop._dynamic_execution_status_text(without_attachment) == (
        "已接管任务，正在执行分析；任务可能需要一些时间。"
    )
    assert AgentLoop._dynamic_execution_status_text(with_attachment) == (
        "已接管任务，正在读取附件并执行分析；长日志可能需要一些时间。"
    )


def test_shadow_disabled_adds_no_shadow_call() -> None:
    """默认关闭时只执行旧选择器，不产生任何额外模型调用。"""

    general = _GeneralSelector(GeneralSkillSelection())
    shadow = _ShadowSelector(error=AssertionError("disabled shadow must not run"))

    result = _route(
        NonSopCapabilityRouter(shadow_enabled=False, shadow_selector=shadow),
        general,
    )

    assert general.calls == 1
    assert shadow.calls == 0
    assert result.effective_decision.mode == "answer"
    assert result.shadow_decision is None
    assert result.shadow_attempted is False


def test_existing_general_skill_remains_authoritative() -> None:
    """已匹配通用技能时必须保持旧 runner 优先，动态 shadow 不得抢占。"""

    general = _GeneralSelector(
        GeneralSkillSelection(
            use_general_skill=True,
            selected_slug="weather-zh",
            confidence=0.96,
        )
    )
    shadow = _ShadowSelector(error=AssertionError("general skill must short-circuit shadow"))

    result = _route(
        NonSopCapabilityRouter(shadow_enabled=True, shadow_selector=shadow),
        general,
        [_skill()],
    )

    assert result.selected_general_skill is not None
    assert result.selected_general_skill.slug == "weather-zh"
    assert result.effective_decision.mode == "general_skill"
    assert result.shadow_decision is not None
    assert result.shadow_decision.mode == "general_skill"
    assert shadow.calls == 0


def test_selected_general_skill_and_dynamic_execution_are_orthogonal() -> None:
    """执行开关开启时，选中 Skill 仍必须继续判断复杂任务而不是短路动态路由。"""

    general = _GeneralSelector(
        GeneralSkillSelection(
            use_general_skill=True,
            selected_slug="weather-zh",
            confidence=0.96,
        )
    )
    dynamic = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="整理跨系统数据并生成风险简报",
            success_criteria=["形成可审计简报"],
            requires_durable_execution=True,
            requires_artifact=True,
            confidence=0.93,
        )
    )

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=dynamic,
        ),
        general,
        [_skill()],
    )

    assert result.selected_general_skill is not None
    assert result.selected_general_skill.slug == "weather-zh"
    assert result.effective_decision.mode == "dynamic_task"
    assert result.primary_decision is not None
    assert result.primary_decision.mode == "dynamic_task"
    assert result.shadow_decision is None
    assert result.primary_attempted is True
    assert dynamic.calls == 1


def test_selected_general_skill_keeps_lightweight_answer_for_simple_request() -> None:
    """Skill 与执行模式拆分后，普通解释请求仍走轻量 guidance 回复。"""

    general = _GeneralSelector(
        GeneralSkillSelection(
            use_general_skill=True,
            selected_slug="weather-zh",
            confidence=0.96,
        )
    )
    answer = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="answer",
            confidence=0.95,
            reason="单轮可直接完成，无需持久执行。",
        )
    )

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=answer,
        ),
        general,
        [_skill()],
    )

    assert result.selected_general_skill is not None
    assert result.effective_decision.mode == "general_skill"
    assert result.primary_decision is not None
    assert result.primary_decision.mode == "answer"
    assert result.shadow_decision is None
    assert result.primary_attempted is True
    assert answer.calls == 1


def test_forced_skill_is_resolved_before_catalog_projection(monkeypatch) -> None:
    """显式选择必须查询完整权威目录，不能因自动推荐 top-K 截断而被误判不可用。"""

    preferred = _skill()
    preferred.id = "skill_forced_outside_top_k"
    unrelated = GeneralSkill(
        id="skill_projected_top_one",
        tenant_id="tenant_demo",
        slug="projected-top-one",
        name="自动推荐第一名",
        skill_markdown="# 推荐",
        status="published",
    )
    projection_calls: list[str] = []

    class _Runtime:
        """若生产代码错误地对显式选择先做 top-K 投影，则记录并暴露反例。"""

        def __init__(self, _db) -> None:
            """兼容 Runtime 构造签名。"""

        def projected_catalog(self, *_args, **_kwargs):
            """只返回无关 Skill，模拟目标 Skill 排在 top-K 之外。"""

            projection_calls.append("called")
            return [SimpleNamespace(skill_id=unrelated.id)]

    monkeypatch.setattr(
        "app.core.agent_loop.get_settings",
        lambda: SimpleNamespace(general_skill_dynamic_guidance_enabled=True),
    )
    monkeypatch.setattr("app.core.agent_loop.GeneralSkillRuntimeService", _Runtime)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(get=lambda *_args: SimpleNamespace(tenant_id="tenant_demo"))
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._list_published_general_skills = lambda *_args: [unrelated, preferred]
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    loop.general_skill_selector = _GeneralSelector(GeneralSkillSelection())
    loop.non_sop_capability_router = NonSopCapabilityRouter(
        shadow_enabled=False,
        execution_enabled=False,
        shadow_selector=_ShadowSelector(error=AssertionError("shadow must remain disabled")),
    )

    result = loop._decide_non_sop_capability(
        "按显式 Skill 回答",
        SimpleNamespace(tenant_id="tenant_demo"),
        ChatSession(id="session_forced_projection", tenant_id="tenant_demo", agent_id="agent"),
        "agent",
        user_id="user",
        forced_general_skill_id=preferred.id,
    )

    assert result.selected_general_skill is preferred
    assert result.general_selection.use_general_skill is True
    assert projection_calls == []


def test_dynamic_task_is_shadow_only_in_batch_a() -> None:
    """高置信度动态任务只能写入 shadow，权威行为仍保持直接回答。"""

    general = _GeneralSelector(GeneralSkillSelection(knowledge_mode="required"))
    shadow = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="生成风险简报",
            success_criteria=["覆盖两个系统", "给出风险证据"],
            requires_durable_execution=True,
            confidence=0.93,
            reason="需要多步骤和可恢复执行",
        )
    )

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=True,
            shadow_selector=shadow,
            minimum_confidence=0.7,
        ),
        general,
    )

    assert result.effective_decision.mode == "answer"
    assert result.shadow_decision is not None
    assert result.shadow_decision.mode == "dynamic_task"
    assert result.shadow_decision.knowledge_mode == "required"
    assert result.shadow_attempted is True
    assert result.execution_created is False


def test_dynamic_task_becomes_effective_with_primary_execution_switch() -> None:
    """验证执行主路由开关与 shadow 开关分离，高置信完整任务成为权威选择。"""

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    requires_durable_execution=True,
                    confidence=0.93,
                )
            ),
            minimum_confidence=0.7,
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )

    assert result.effective_decision.mode == "dynamic_task"
    assert result.primary_decision is not None
    assert result.primary_decision.mode == "dynamic_task"
    assert result.primary_attempted is True
    assert result.shadow_decision is None
    assert result.execution_created is False


def test_primary_and_shadow_are_independent_when_both_are_explicit() -> None:
    """显式同时配置时主路由负责执行语义，shadow 只负责比较观测。"""

    primary = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="主路由任务",
            success_criteria=["主路由标准"],
            confidence=0.95,
        )
    )
    shadow = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="answer",
            confidence=0.95,
            reason="shadow 仅供比较",
        )
    )

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=True,
            execution_enabled=True,
            primary_selector=primary,
            shadow_selector=shadow,
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )

    assert result.effective_decision.mode == "dynamic_task"
    assert result.primary_decision is not None
    assert result.primary_decision.mode == "dynamic_task"
    assert result.shadow_decision is not None
    assert result.shadow_decision.mode == "answer"
    assert primary.calls == 1
    assert shadow.calls == 1


def test_agent_loop_delegates_effective_dynamic_route_without_copying_loop(monkeypatch) -> None:
    """验证 Agent Loop 只做入口委托，最终回复来自独立 DynamicTaskAgent 的闭环结果。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    calls: list[str] = []

    class _DynamicAgent:
        """模拟已经完成统一 Runtime 闭环的独立 Agent。"""

        def __init__(self, db) -> None:
            calls.append("init")

        def start_task(self, **kwargs):
            calls.append("start")
            return SimpleNamespace(id="execution_1"), True

        def run_until_blocked_or_complete(self, **kwargs):
            calls.append("run")
            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_1",
                message=SimpleNamespace(content="# 风险简报"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
        get=lambda _model, _identity: session,
    )
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    session = ChatSession(
        id="session_dynamic",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="生成风险简报",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_1",
    )

    assert response is not None
    assert response.reply == "# 风险简报"
    assert calls == ["init", "start", "run"]


def test_agent_loop_continues_terminal_dynamic_chat_turn(monkeypatch) -> None:
    """终态 DynamicTask 的聊天追问必须走续接接口而不能重新走首轮 start。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="分析同一份日志",
                    success_criteria=["形成可校验结论"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    calls: list[str] = []
    captured: dict[str, object] = {}

    class _DynamicAgent:
        """模拟具备终态聊天续接能力的 DynamicTaskAgent。"""

        def __init__(self, _db) -> None:
            """记录 Agent 初始化。"""

            calls.append("init")

        def continue_chat_turn(self, **kwargs):
            """返回父执行之后的新一轮 Execution。"""

            calls.append("continue")
            captured.update(kwargs)
            return (
                SimpleNamespace(
                    id="execution_child",
                    status="running",
                    goal_snapshot_json={
                        "continued_from_execution_id": "execution_parent",
                    },
                ),
                True,
            )

        def run_until_blocked_or_complete(self, **_kwargs):
            """模拟续接轮次完成并生成回复。"""

            calls.append("run")
            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_child",
                message=SimpleNamespace(content="基于同一会话的续接结论"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
    )
    recorded_events: list[tuple] = []
    loop.events = SimpleNamespace(record=lambda *args, **_kwargs: recorded_events.append(args))
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    session = ChatSession(
        id="session_terminal_continuation",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="根据刚才的日志继续核对上传次数",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_2",
        conversation_context={
            "messages": [
                {"role": "assistant", "content": "上一轮结论"},
                {"role": "user", "content": "根据刚才的日志继续核对上传次数"},
            ]
        },
    )

    assert response is not None
    assert response.reply == "基于同一会话的续接结论"
    assert calls == ["init", "continue", "run"]
    assert captured["conversation_context"]["messages"][-1]["content"] == (
        "根据刚才的日志继续核对上传次数"
    )
    continued_events = [
        args for args in recorded_events if args[2] == "dynamic_task_continued"
    ]
    assert len(continued_events) == 1
    assert continued_events[0][3]["execution_id"] == "execution_child"
    assert continued_events[0][3]["parent_execution_id"] == "execution_parent"


def test_explicit_dynamic_request_cannot_be_downgraded_by_forced_general_skill(
    monkeypatch,
) -> None:
    """用户明确要求持久 DynamicTaskAgent 时，Skill 只能作为指导而不能降级路由。"""

    skill = _skill()
    route = NonSopCapabilityRouteResult(
        selected_general_skill=skill,
        general_selection=GeneralSkillSelection(
            use_general_skill=True,
            selected_slug=skill.slug,
            confidence=1.0,
        ),
        effective_decision=NonSopCapabilityDecision(
            mode="general_skill",
            selected_general_skill_slug=skill.slug,
            confidence=1.0,
        ),
        shadow_decision=None,
        shadow_attempted=True,
        shadow_duration_ms=1.0,
    )
    captured: dict[str, object] = {}

    class _DynamicAgent:
        """捕获 AgentLoop 传给动态任务的固定 Skill 契约。"""

        def __init__(self, _db) -> None:
            """兼容生产构造签名。"""

        def start_task(self, **kwargs):
            """保存委派参数并返回已创建 Execution。"""

            captured.update(kwargs)
            return SimpleNamespace(id="execution_guided"), True

        def run_until_blocked_or_complete(self, **_kwargs):
            """返回已经验收的动态任务产物。"""

            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_guided",
                message=SimpleNamespace(content="# 售后升级处理规范"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
        get=lambda _model, _identity: session,
    )
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    loop._forced_general_skill_capability = lambda *_args: (
        skill,
        route.general_selection,
    )
    session = ChatSession(
        id="session_guided_dynamic",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message=(
                "请通过持久、可恢复、可校验的 DynamicTaskAgent "
                "完成受控操作规范"
            ),
            forced_general_skill_id=skill.id,
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_guided_dynamic",
    )

    assert response is not None
    assert captured["forced_general_skill_id"] == skill.id
    assert captured["forced_general_skill_ids"] == ()


def test_selected_dynamic_engine_delegates_even_when_route_is_answer(monkeypatch) -> None:
    """对话页选择 DynamicTaskAgent 后，普通路由结果不能把本轮降级为直接回答。"""

    captured: dict[str, object] = {}

    class _DynamicAgent:
        """捕获页面引擎选择传入的动态任务委托。"""

        def __init__(self, _db) -> None:
            """兼容生产构造签名。"""

        def start_task(self, **kwargs):
            """记录动态任务创建参数。"""

            captured.update(kwargs)
            return SimpleNamespace(id="execution_selected_engine"), True

        def run_until_blocked_or_complete(self, **_kwargs):
            """模拟动态任务闭环成功。"""

            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_selected_engine",
                message=SimpleNamespace(content="复杂任务结果"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
    )
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    route = NonSopCapabilityRouteResult(
        selected_general_skill=None,
        general_selection=GeneralSkillSelection(),
        effective_decision=NonSopCapabilityDecision(mode="answer"),
        shadow_decision=None,
        shadow_attempted=False,
        shadow_duration_ms=0.0,
    )
    session = ChatSession(
        id="session_selected_engine",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="整理材料并形成结果",
            execution_engine="dynamic_task",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_selected_engine",
    )

    assert response is not None
    assert response.reply == "复杂任务结果"
    assert captured["goal"] == "整理材料并形成结果"


def test_selected_dynamic_engine_does_not_interrupt_active_sop(monkeypatch) -> None:
    """活动 SOP 有游标或等待状态时，页面引擎选择不得并行创建 Dynamic Execution。"""

    monkeypatch.setattr(
        "app.core.agent_loop.DynamicTaskAgent",
        lambda _db: pytest.fail("active SOP must remain in the formal runtime"),
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace()
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    route = NonSopCapabilityRouteResult(
        selected_general_skill=None,
        general_selection=GeneralSkillSelection(),
        effective_decision=NonSopCapabilityDecision(mode="dynamic_task"),
        shadow_decision=None,
        shadow_attempted=False,
        shadow_duration_ms=0.0,
    )
    session = ChatSession(
        id="session_active_sop",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        active_skill_id="skill_sop",
        active_step_id="step_1",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="继续当前任务",
            execution_engine="dynamic_task",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_active_sop",
    )

    assert response is None


def test_explicit_dynamic_request_detection_does_not_capture_explanatory_chat() -> None:
    """服务端只接管明确执行请求，不把概念解释误路由为动态任务。"""

    assert AgentLoop._explicit_dynamic_task_requested(
        "请通过持久、可恢复的 DynamicTaskAgent 完成 INC-742 诊断"
    )
    assert AgentLoop._explicit_dynamic_task_requested(
        "请通过持久、可恢复的分析任务完成仓库协作说明整理"
    )
    assert AgentLoop._explicit_dynamic_task_requested("请创建可恢复的动态任务完成评审")
    assert not AgentLoop._explicit_dynamic_task_requested("请解释 DynamicTaskAgent 是什么")
    assert not AgentLoop._explicit_dynamic_task_requested("普通对话为什么不需要持久动态任务？")


def test_explicit_answer_only_request_blocks_shadow_dynamic_upgrade() -> None:
    """用户要求直接交付且禁止计划/工具任务时，shadow不得升级持久执行。"""

    assert AgentLoop._explicit_answer_only_requested(
        "请直接在回复中交付，不创建计划、文件或工具任务。"
    )
    assert AgentLoop._explicit_answer_only_requested(
        "直接回复这份材料的整理结果，不要创建计划。"
    )
    assert not AgentLoop._explicit_answer_only_requested("请创建计划后生成风险简报。")
    assert not AgentLoop._explicit_answer_only_requested("请直接解释 DynamicTaskAgent 是什么。")


def test_explicit_answer_only_request_does_not_delegate_dynamic_task(monkeypatch) -> None:
    """即使 shadow 返回 dynamic_task，普通单轮契约也不得创建 Execution。"""

    monkeypatch.setattr(
        "app.core.agent_loop.DynamicTaskAgent",
        lambda _db: pytest.fail("direct-delivery request must not initialize DynamicTaskAgent"),
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace()
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    route = NonSopCapabilityRouteResult(
        selected_general_skill=None,
        general_selection=GeneralSkillSelection(),
        effective_decision=NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="整理材料",
            success_criteria=["形成说明"],
            confidence=0.99,
        ),
        shadow_decision=None,
        shadow_attempted=True,
        shadow_duration_ms=1.0,
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="请直接在回复中交付，不创建计划、文件或工具任务。",
        ),
        ChatSession(
            id="session_answer_only",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
        ),
        SimpleNamespace(id="model_1"),
        route,
        "message_answer_only",
    )

    assert response is None


def test_multiple_forced_general_skills_always_enter_composed_dynamic_runtime(monkeypatch) -> None:
    """即使分类器判普通回答，多选 Skill 也不得静默丢弃第二项，必须组合委派。"""

    captured: dict[str, object] = {}

    class _DynamicAgent:
        """捕获多选 Skill 组合参数并返回成功。"""

        def __init__(self, _db) -> None:
            """兼容生产构造签名。"""

        def start_task(self, **kwargs):
            """记录创建参数。"""

            captured.update(kwargs)
            return SimpleNamespace(id="execution_composed"), True

        def run_until_blocked_or_complete(self, **_kwargs):
            """模拟组合动态任务完成。"""

            return DynamicRunOutcome(
                status="succeeded",
                execution_id="execution_composed",
                message=SimpleNamespace(content="组合结果"),
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
    )
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    route = NonSopCapabilityRouteResult(
        selected_general_skill=None,
        general_selection=GeneralSkillSelection(),
        effective_decision=NonSopCapabilityDecision(mode="answer"),
        shadow_decision=None,
        shadow_attempted=False,
        shadow_duration_ms=0.0,
    )
    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="综合两个方法形成规范",
            forced_general_skill_ids=["skill_a", "skill_b"],
        ),
        ChatSession(id="session_composed", tenant_id="tenant_demo", agent_id="agent_demo"),
        SimpleNamespace(id="model_1"),
        route,
        "message_composed",
    )

    assert response is not None and response.reply == "组合结果"
    assert captured["forced_general_skill_id"] is None
    assert captured["forced_general_skill_ids"] == ("skill_a", "skill_b")


def test_agent_loop_defers_temporary_tool_quota_exhaustion_without_failing_execution(
    monkeypatch,
) -> None:
    """验证聊天入口把临时工具满载转成持久恢复信号，而不是终结已创建 Execution。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    failed: list[str] = []

    class _CapacityAgent:
        """模拟 Operation 已准备但工具槽临时耗尽的独立 Agent。"""

        store = SimpleNamespace()

        def __init__(self, _db) -> None:
            """保持测试替身无额外状态。"""

        def start_task(self, **_kwargs):
            """返回已经持久化、仍可恢复的动态 Execution。"""

            return SimpleNamespace(id="execution_capacity", revision=7), True

        def run_until_blocked_or_complete(self, **_kwargs):
            """模拟工具并发槽暂不可用。"""

            raise DynamicTaskQuotaError("DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED")

        def fail_execution(self, **_kwargs):
            """记录误终结；容量退避路径禁止调用。"""

            failed.append("failed")

    class _Control:
        """捕获容量恢复 Signal 的稳定契约。"""

        def __init__(self, _db, _store) -> None:
            """兼容真实控制服务构造签名。"""

        def enqueue_signal(self, _instance, **kwargs):
            """返回持久信号投影并保留入参供断言。"""

            assert kwargs["signal_type"] == "capacity_retry"
            assert kwargs["payload"] == {
                "reason_code": "DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED"
            }
            return SimpleNamespace(id="signal_capacity")

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _CapacityAgent)
    monkeypatch.setattr("app.core.agent_loop.ExecutionControlService", _Control)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
    )
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    loop._persist_dynamic_waiting_message = lambda **_kwargs: SimpleNamespace(
        content="任务正在等待可用执行容量，稍后将自动恢复。"
    )
    session = ChatSession(
        id="session_capacity",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="生成风险简报",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_capacity",
    )

    assert response is not None
    assert response.reply == "任务正在等待可用执行容量，稍后将自动恢复。"
    assert failed == []


def test_dynamic_waiting_projection_distinguishes_automatic_and_human_recovery() -> None:
    """验证容量/调度自动恢复不会错误引导用户前往待我处理中心。"""

    capacity = AgentLoop._dynamic_waiting_content("capacity_retry")
    scheduled = AgentLoop._dynamic_waiting_content("scheduled_start")
    clarification = AgentLoop._dynamic_waiting_content("clarify_partner")

    assert "自动恢复" in capacity
    assert "待我处理" not in capacity
    assert "自动开始" in scheduled
    assert "待我处理" not in scheduled
    assert "待我处理" in clarification


def test_agent_loop_optional_dynamic_rollout_denial_can_fall_back(monkeypatch) -> None:
    """非持久、非显式组合的动态建议未命中灰度时可以保留旧普通回答。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    monkeypatch.setattr(
        "app.core.agent_loop.DynamicTaskAgent",
        lambda _db: pytest.fail("灰度拒绝前不得初始化 DynamicTaskAgent"),
    )
    recorded: list[tuple] = []
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(commit=lambda: None)
    loop.events = SimpleNamespace(record=lambda *args, **_kwargs: recorded.append(args))
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False
    session = ChatSession(
        id="session_rollout_denied",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="生成风险简报",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_rollout_denied",
    )

    assert response is None
    assert recorded[0][2] == "dynamic_task_rollout_denied"
    assert recorded[0][3]["reason_code"] == "DYNAMIC_TASK_ROLLOUT_DENIED"
    assert "生成风险简报" not in str(recorded)


def test_runtime_capacity_denial_has_stable_capacity_code_without_model_quota_gate() -> None:
    """运行容量为零时只返回容量错误，不把模型账户配额伪装成产品灰度门禁。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    requires_durable_execution=True,
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    recorded: list[tuple] = []
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(commit=lambda: None)
    loop.events = SimpleNamespace(record=lambda *args, **_kwargs: recorded.append(args))
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False
    loop._dynamic_task_rollout_denial_code = "DYNAMIC_TASK_QUOTA_NOT_CONFIGURED"

    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_TASK_QUOTA_NOT_CONFIGURED"):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="生成风险简报",
            ),
            ChatSession(
                id="session_capacity_not_configured",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
            ),
            SimpleNamespace(id="model_1"),
            route,
            "message_capacity_not_configured",
        )

    assert recorded[0][3]["reason_code"] == "DYNAMIC_TASK_QUOTA_NOT_CONFIGURED"


def test_durable_dynamic_rollout_denial_fails_instead_of_fake_answer(monkeypatch) -> None:
    """要求持久执行的复杂任务未命中灰度时必须失败，不能伪装成普通问答。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    requires_durable_execution=True,
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(commit=lambda: None)
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False

    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_TASK_ROLLOUT_DENIED"):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="生成风险简报",
            ),
            ChatSession(
                id="session_durable_rollout_denied",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
            ),
            SimpleNamespace(id="model_1"),
            route,
            "message_durable_rollout_denied",
        )


def test_scheduled_dynamic_task_rollout_denial_fails_instead_of_fake_success(
    monkeypatch,
) -> None:
    """调度入口未命中灰度时必须失败，不能降级成无 Execution 的普通回答。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(NonSopCapabilityDecision(mode="answer")),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    monkeypatch.setattr(
        "app.core.agent_loop.DynamicTaskAgent",
        lambda _db: pytest.fail("灰度拒绝前不得初始化 DynamicTaskAgent"),
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(commit=lambda: None)
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False
    loop._forced_general_skill_capability = lambda *_args, **_kwargs: (
        SimpleNamespace(id="skill_a"),
        GeneralSkillSelection(),
    )
    session = ChatSession(
        id="session_scheduled_rollout_denied",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_TASK_ROLLOUT_DENIED"):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                interaction_mode="scheduled_task",
                message="生成定时风险简报",
            ),
            session,
            SimpleNamespace(id="model_1"),
            route,
            "message_scheduled_rollout_denied",
        )


def test_multi_skill_rollout_denial_fails_instead_of_consuming_only_first_skill() -> None:
    """显式多选依赖动态组合运行时，灰度拒绝时必须失败而不是静默只消费首项。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(NonSopCapabilityDecision(mode="answer")),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(commit=lambda: None)
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False
    loop._forced_general_skill_capability = lambda *_args, **_kwargs: (
        SimpleNamespace(id="skill_a"),
        GeneralSkillSelection(),
    )

    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_TASK_ROLLOUT_DENIED"):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="组合使用两个 Skill",
                forced_general_skill_id="skill_a",
                forced_general_skill_ids=("skill_a", "skill_b"),
            ),
            ChatSession(
                id="session_multi_skill_rollout_denied",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
            ),
            SimpleNamespace(id="model_1"),
            route,
            "message_multi_skill_rollout_denied",
        )


def test_required_knowledge_unavailable_fails_before_dynamic_execution() -> None:
    """用户要求企业知识但没有可用版本时必须明确失败，禁止无证据生成答案。"""

    route = NonSopCapabilityRouteResult(
        selected_general_skill=None,
        general_selection=GeneralSkillSelection(
            use_general_skill=True,
            selected_slug="policy",
            use_knowledge=True,
            knowledge_mode="required",
        ),
        effective_decision=NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="依据企业制度回答",
            success_criteria=["引用企业知识证据"],
        ),
        shadow_decision=None,
        shadow_attempted=False,
        shadow_duration_ms=0,
    )
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace()
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: True
    loop._knowledge_capability_payload = lambda *_args, **_kwargs: {"available": False}

    with pytest.raises(
        DynamicTaskAgentError, match="DYNAMIC_KNOWLEDGE_REQUIRED_UNAVAILABLE"
    ):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="依据企业制度回答",
            ),
            ChatSession(
                id="session_required_knowledge_unavailable",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
            ),
            SimpleNamespace(id="model_1"),
            route,
            "message_required_knowledge_unavailable",
        )


def test_dynamic_task_clarification_returns_durable_waiting_projection(monkeypatch) -> None:
    """动态执行等待澄清时必须正常结束聊天回合，而不是把可恢复状态抛成异常。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="模型改写后的风险简报",
                    success_criteria=["明确报告区域"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    calls: list[str] = []
    observed_start: dict[str, object] = {}

    class _DynamicAgent:
        """模拟创建成功后进入受控澄清等待的独立动态 Agent。"""

        def __init__(self, _db) -> None:
            """记录 Agent 初始化。"""

            calls.append("init")

        def start_task(self, **kwargs):
            """返回已经创建的权威 Execution。"""

            calls.append("start")
            observed_start.update(kwargs)
            return SimpleNamespace(id="execution_waiting"), True

        def run_until_blocked_or_complete(self, **kwargs):
            """返回带稳定阻塞步骤的 waiting 结果。"""

            calls.append("run")
            return DynamicRunOutcome(
                status="waiting",
                execution_id="execution_waiting",
                blocking_step_key="clarify_region",
            )

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _DynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
    )
    recorded_events: list[tuple] = []
    loop.events = SimpleNamespace(record=lambda *args, **_kwargs: recorded_events.append(args))
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    loop._persist_dynamic_waiting_message = lambda **_kwargs: SimpleNamespace(
        content="任务已暂停，正在等待你补充信息。"
    )
    session = ChatSession(
        id="session_dynamic_waiting",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    response = loop._try_handle_dynamic_task(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
            message="生成风险简报",
        ),
        session,
        SimpleNamespace(id="model_1"),
        route,
        "message_waiting",
    )

    assert response is not None
    assert response.reply == "任务已暂停，正在等待你补充信息。"
    assert response.step_result is not None
    assert response.step_result.is_step_completed is False
    assert calls == ["init", "start", "run"]
    assert observed_start["goal"] == "生成风险简报"
    assert any(args[2] == "dynamic_task_delegated" for args in recorded_events)


def test_dynamic_execution_error_is_failed_before_error_propagates(monkeypatch) -> None:
    """动态执行创建后的异常必须先收敛 Execution，再由入口返回明确错误。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["覆盖合同证据"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    calls: list[tuple[str, str | None]] = []

    class _FailingDynamicAgent:
        """模拟 Execution 创建后在后续步骤发生确定性能力错误。"""

        def __init__(self, _db) -> None:
            """记录动态 Agent 初始化。"""

            calls.append(("init", None))

        def start_task(self, **_kwargs):
            """返回已经持久化的 Execution。"""

            calls.append(("start", None))
            return SimpleNamespace(id="execution_failed"), True

        def run_until_blocked_or_complete(self, **_kwargs):
            """模拟运行期发现未冻结能力。"""

            calls.append(("run", None))
            error = RuntimeError("DYNAMIC_KNOWLEDGE_NOT_FROZEN")
            error.code = "DYNAMIC_KNOWLEDGE_NOT_FROZEN"
            raise error

        def fail_execution(self, **kwargs) -> None:
            """记录入口在传播异常前调用了持久失败收敛。"""

            calls.append(("fail", kwargs["error_code"]))

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _FailingDynamicAgent)
    loop = object.__new__(AgentLoop)
    loop.db = SimpleNamespace(
        refresh=lambda _row: None,
        commit=lambda: None,
        begin_nested=lambda: nullcontext(),
        get=lambda _model, _identity: session,
    )
    recorded_events: list[tuple] = []
    loop.events = SimpleNamespace(record=lambda *args, **_kwargs: recorded_events.append(args))
    loop._knowledge_capability_payload = lambda *_args: {"available": False}
    session = ChatSession(
        id="session_dynamic_failure",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
    )

    with pytest.raises(RuntimeError, match="DYNAMIC_KNOWLEDGE_NOT_FROZEN"):
        loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="生成风险简报",
            ),
            session,
            SimpleNamespace(id="model_1"),
            route,
            "message_failure",
        )

    assert calls == [
        ("init", None),
        ("start", None),
        ("run", None),
        ("fail", "DYNAMIC_KNOWLEDGE_NOT_FROZEN"),
    ]
    assert any(args[2] == "dynamic_task_execution_failed" for args in recorded_events)


def test_low_confidence_dynamic_shadow_degrades_to_answer() -> None:
    """低置信度动态提案必须收敛为 answer 并留下结构化失败码。"""

    shadow = _ShadowSelector(
        NonSopCapabilityDecision(
            mode="dynamic_task",
            goal="不确定目标",
            confidence=0.4,
        )
    )

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=True,
            shadow_selector=shadow,
            minimum_confidence=0.7,
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )

    assert result.shadow_decision is not None
    assert result.shadow_decision.mode == "answer"
    assert result.shadow_decision.degraded is True
    assert result.shadow_decision.failure_code == "dynamic_shadow_low_confidence"


def test_shadow_failure_falls_back_without_execution() -> None:
    """shadow 模型异常不得传播到旧链路，也不得创建执行。"""

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=True,
            shadow_selector=_ShadowSelector(error=RuntimeError("provider down")),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )

    assert result.effective_decision.mode == "answer"
    assert result.shadow_decision is not None
    assert result.shadow_decision.mode == "answer"
    assert result.shadow_decision.degraded is True
    assert result.shadow_decision.failure_code == "dynamic_shadow_failed"
    assert result.execution_created is False


def test_shadow_audit_payload_is_allowlisted_and_redacted() -> None:
    """审计只输出枚举、布尔和计量字段，不泄露 prompt、goal、reason 或上下文。"""

    result = _route(
        NonSopCapabilityRouter(
            shadow_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="高度敏感目标",
                    success_criteria=["敏感验收标准"],
                    confidence=0.91,
                    reason="模型的原始推理",
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )

    payload = result.audit_payload()

    assert payload["shadow_mode"] == "dynamic_task"
    assert payload["execution_created"] is False
    assert set(payload) == {
        "effective_mode",
        "shadow_mode",
        "execution_intent",
        "knowledge_mode",
        "confidence_bucket",
        "requires_durable_execution",
        "requires_artifact",
        "degraded",
        "failure_code",
        "shadow_attempted",
        "shadow_duration_ms",
        "execution_created",
    }
    serialized = repr(payload)
    assert "高度敏感目标" not in serialized
    assert "敏感验收标准" not in serialized
    assert "模型的原始推理" not in serialized
    assert "内部记忆" not in serialized
    assert "历史消息" not in serialized


def test_agent_loop_records_shadow_as_domain_audit_not_sse() -> None:
    """共享非 SOP 接入点只持久化脱敏事件，并继续返回旧选择结果。"""

    records: list[tuple[str, str, str, dict[str, object]]] = []
    loop = object.__new__(AgentLoop)
    loop.general_skill_selector = _GeneralSelector(GeneralSkillSelection())
    loop.non_sop_capability_router = NonSopCapabilityRouter(
        shadow_enabled=True,
        shadow_selector=_ShadowSelector(
            NonSopCapabilityDecision(
                mode="dynamic_task",
                goal="高度敏感目标",
                success_criteria=["生成简报"],
                confidence=0.9,
            )
        ),
    )
    loop._list_published_general_skills = lambda *_args, **_kwargs: []
    loop._knowledge_capability_payload = lambda *_args, **_kwargs: {
        "available": False,
        "accessible_count": 0,
    }
    loop.events = SimpleNamespace(
        record=lambda tenant, session, event, payload: records.append(
            (tenant, session, event, payload)
        )
    )
    session = ChatSession(id="session_shadow", tenant_id="tenant_demo")

    skill, selection = loop._route_non_sop_capability(
        "整理两个系统的高度敏感数据",
        SimpleNamespace(tenant_id="tenant_demo"),
        session,
        user_message_id="message_shadow",
    )

    assert skill is None
    assert selection.use_general_skill is False
    assert len(records) == 1
    assert records[0][2] == "non_sop_capability_shadow_decided"
    assert records[0][3]["turn_id"] == "message_shadow"
    assert records[0][3]["shadow_mode"] == "dynamic_task"
    assert "message" not in records[0][3]
    assert "goal" not in records[0][3]
    assert "reason" not in records[0][3]


def test_active_sop_general_intent_never_enters_non_sop_router() -> None:
    """活动 SOP 的 general_intent 继续使用旧选择器，禁止进入动态任务 shadow。"""

    legacy_calls: list[str] = []
    loop = object.__new__(AgentLoop)
    loop._select_general_capability = lambda query, *_args, **_kwargs: (
        legacy_calls.append(query) or None,
        GeneralSkillSelection(),
    )
    loop.non_sop_capability_router = SimpleNamespace(
        decide=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("active SOP must not enter non-SOP router")
        )
    )
    active_skill = Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="采购流程",
        description="采购",
        version=1,
        status="published",
        definition_json={"steps": []},
    )

    result = loop._preselect_general_skill_for_scene(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            message="继续采购并查询天气",
        ),
        ChatSession(id="session_sop", tenant_id="tenant_demo"),
        active_skill,
        [],
        SimpleNamespace(tenant_id="tenant_demo"),
        RouterDecision(
            decision="continue_active",
            target_skill_id="purchase",
            general_intent="查询北京天气",
        ),
        [],
        {},
        None,
    )

    assert result is None
    assert legacy_calls == ["查询北京天气"]
