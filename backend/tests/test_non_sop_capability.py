"""
@Time       : 2026/08/03 18:30
@Author     : zhanglp8181
@File       : test_non_sop_capability.py
@CallChain  : pytest → NonSopCapabilityRouter → GeneralSkill/动态任务 shadow 决策
@Description: 验证 A 批非 SOP 兼容分流、失效降级和脱敏审计契约。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_loop import AgentLoop
from app.core.non_sop_capability import (
    NonSopCapabilityDecision,
    NonSopCapabilityRouter,
)
from app.db.models import ChatSession, GeneralSkill, Skill
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
