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
    NonSopCapabilityDecision,
    NonSopCapabilityRouter,
)
from app.db.models import ChatSession, GeneralSkill, Skill
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgentError
from app.dynamic_tasks.quotas import DynamicTaskQuotaError
from app.general_skills.schema import GeneralSkillSelection
from app.session.session_schema import ChatTurnRequest, RouterDecision


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


def test_dynamic_task_becomes_effective_only_with_separate_execution_kill_switch() -> None:
    """验证 B1 执行开关与 shadow 开关分离，只有高置信完整任务可成为权威选择。"""

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
    assert result.shadow_decision is not None
    assert result.execution_created is False


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


def test_agent_loop_rollout_denial_falls_back_without_creating_execution(monkeypatch) -> None:
    """tenant 或 Agent 未命中灰度时只记脱敏拒绝事件，不创建动态 Execution。"""

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
    loop.db = SimpleNamespace()
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
    loop.db = SimpleNamespace()
    loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
    loop._dynamic_task_rollout_allows = lambda _tenant, _agent: False
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


def test_dynamic_task_clarification_returns_durable_waiting_projection(monkeypatch) -> None:
    """动态执行等待澄清时必须正常结束聊天回合，而不是把可恢复状态抛成异常。"""

    route = _route(
        NonSopCapabilityRouter(
            shadow_enabled=False,
            execution_enabled=True,
            shadow_selector=_ShadowSelector(
                NonSopCapabilityDecision(
                    mode="dynamic_task",
                    goal="生成风险简报",
                    success_criteria=["明确报告区域"],
                    confidence=0.95,
                )
            ),
        ),
        _GeneralSelector(GeneralSkillSelection()),
    )
    calls: list[str] = []

    class _DynamicAgent:
        """模拟创建成功后进入受控澄清等待的独立动态 Agent。"""

        def __init__(self, _db) -> None:
            """记录 Agent 初始化。"""

            calls.append("init")

        def start_task(self, **kwargs):
            """返回已经创建的权威 Execution。"""

            calls.append("start")
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
