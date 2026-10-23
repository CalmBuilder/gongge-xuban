"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_memory_projection_consumers.py
@CallChain  : pytest → Router/DTA/LLMClient → 最终SDK传输替身
@Description: 在实际消费边界验证空白保真、完整事实、冻结修复与预算失败。
"""

import json
from types import SimpleNamespace

import pytest

from app.dynamic_tasks.agent import DynamicTaskAgent
from app.llm.client import LLMClient, LLMError, _fit_request_messages, _request_tokens
from app.llm.stage_protocol import _memory_text
from app.memory.projection import MemoryCandidate, MemoryProjectionPolicy
from app.memory.request_projection import MemoryRequestEnvelope, MemoryRequestPayload


def test_router_preserves_internal_whitespace() -> None:
    """Router实际渲染保留代码/规则中的换行、制表符和连续空格。"""
    content = "姓名：青禾\n规则：a  b\n\t保留缩进"
    assert _memory_text([{"kind": "profile", "content": content}]) == "- " + content


def test_router_memory_text_keeps_safe_kind_and_key_labels() -> None:
    """Router必须携带安全kind/key标签，避免正文附加说明跨字段误归因。"""
    content = "输出偏好：简洁要点；原样保留代码 a  b"
    assert _memory_text([{
        "kind": "preference", "content": content,
        "metadata": {"key": "reply_style", "agent_id": "hidden"},
    }]) == "- preference/reply_style: " + content


def test_router_memory_text_deduplicates_same_content_after_label_formatting() -> None:
    """来源标签格式化后仍按原始正文去重，避免同一事实重复发送。"""
    content = "姓名：青禾"
    items = [
        {"kind": "profile", "content": content, "metadata": {"key": "name"}},
        {"kind": "profile", "content": content, "metadata": {"key": "name"}},
    ]
    assert _memory_text(items) == "- profile/name: " + content


def test_final_router_payload_keeps_safe_kind_and_key_labels() -> None:
    """最终MemoryRequestPayload重渲染也必须保留来源键标签。"""
    from app.llm.stage_protocol import stage_payload
    from app.memory.request_projection import render_memory_payload

    candidate = MemoryCandidate(
        id="style", kind="preference", key="reply_style",
        content="输出偏好：简洁要点；原样保留代码 a  b",
        metadata={"key": "reply_style"},
    )
    envelope = MemoryRequestEnvelope((candidate,), "格式")
    payload = MemoryRequestPayload(stage_payload(
        phase="Router", user_message="格式", conversation_context={},
        memory_context=[candidate.provider_payload()], instructions="路由",
        stage_data={}, output_contract={},
    ), envelope)
    rendered = render_memory_payload(payload, (candidate,))
    assert rendered["_agent_stage"]["memory"] == (
        "- preference/reply_style: 输出偏好：简洁要点；原样保留代码 a  b"
    )


def test_dta_omits_whole_oversize_fact_and_keeps_small_later_item() -> None:
    """不能先截前二十条或把第一个超长事实裁成伪造半句。"""
    huge = {"kind": "profile", "content": "长" * 4001}
    small = {"kind": "fact", "content": " 代码 a  b\n\t完整 "}
    result = DynamicTaskAgent._memory_projection([huge, small])
    assert list(result) == [{"kind": "fact", "content": small["content"], "metadata": {}}]


def test_legacy_dta_restore_without_lineage_omits_stored_memory() -> None:
    """真实旧执行推进不能仅凭 context 正文存在而重新披露无来源代数的事实。"""
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db.models import ModelConfig
    from tests import test_dynamic_task_agent as support

    class InspectProposer(support._Proposer):
        """沿实际动作消费者检查旧恢复信封为空，保留工具动作原合同。"""

        def propose(self, *, view, step):
            """旧正文仍在领域视图，但最终 SDK 投影信封没有可披露候选。"""
            assert view.execution_context["memory_context"]
            assert view._memory_envelope is not None
            assert view._memory_envelope.eligible() == ()
            return super().propose(view=view, step=step)

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        support._ensure_dynamic_agent(db)
        capabilities = support._model_capabilities()
        model = ModelConfig(id="legacy_projection_model", tenant_id="tenant_demo",
            name="旧执行测试模型", model="isolated", api_key_encrypted="synthetic",
            capability_snapshot_json=capabilities,
            capability_checksum=support.capability_checksum(capabilities), preflight_status="ready")
        db.add(model)
        db.flush()
        proposer = InspectProposer()
        agent = DynamicTaskAgent(db, catalog=support._StartCatalog(model, support._snapshot()),
            tool_executor=support._Executor(), planner=support._Planner(), action_proposer=proposer)
        instance, _ = agent.start_task(tenant_id="tenant_demo", session_id="legacy_projection_session",
            agent_id="agent_demo", initiator_user_id="user_demo", goal="核对合同",
            success_criteria=("读取合同",), model_config=model)
        context = dict(instance.context_json)
        context.pop("memory_projection", None)
        context["memory_context"] = [{"kind": "fact", "content": "没有来源证明的旧事实"}]
        instance.context_json = context
        db.add(instance)
        db.commit()
        agent.advance_next_read_step(execution_id=instance.id, model_config=model,
            worker_id="legacy_projection_worker", actor_user_id="user_demo")
        assert proposer.calls == 1


def test_final_fitter_refuses_to_break_protected_current_json() -> None:
    """合法历史清理后仍超限，保护当前JSON和事实而受控失败。"""
    messages = [{"role": "system", "content": "系统"}, {"role": "user", "content":
        json.dumps({"goal": "保留当前要求" * 100, "memory_context": []}, ensure_ascii=False)}]
    with pytest.raises(LLMError, match="INPUT_BUDGET_EXCEEDED"):
        _fit_request_messages(messages, 64, protect_current=True)


class SDK:
    """替换最终SDK传输，生产LLMClient、JSON修复和流式路径保持实际执行。"""

    def __init__(self, outputs: list[str]) -> None:
        """固定远端输出并记录最终已整形参数。"""
        self.outputs = iter(outputs)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        """返回兼容SDK完成响应，流式也使用同一消息记录。"""
        self.calls.append(kwargs)
        text = next(self.outputs)
        if kwargs.get("stream"):
            return iter([SimpleNamespace(id="stream-test", usage=None, choices=[
                SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason="stop")])])
        return SimpleNamespace(id="response-test", usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content=text), finish_reason="stop")])


def llm(sdk: SDK) -> LLMClient:
    """仅替换SDK连接，保留真实预算、阶段、JSON与取消处理。"""
    client = object.__new__(LLMClient)
    client.client = sdk
    client.model = "isolated-contract-test"
    client.temperature = 0
    client.max_output_tokens = 128
    client.base_url = ""
    return client


def envelope_payload(*, validator=None):
    """构造与DTA实际重复provider视图同形的可信请求。"""
    facts = (MemoryCandidate(id="fact", kind="profile", key="name",
                             content='青禾\n\t代码 a  b "\\" 😀', metadata={"key": "name"}),
             MemoryCandidate(id="huge", kind="fact", key="huge", content="超" * 6000))
    envelope = MemoryRequestEnvelope(facts, "名称", MemoryProjectionPolicy(max_items=20),
                                     validate=validator)
    values = [item.provider_payload() for item in facts]
    payload = MemoryRequestPayload({"goal": "新输入纠正保留", "conversation_context": {
        "metadata": {"token_budget": 4096}}, "provider_execution_view": {
        "execution_context": {"execution_id": "isolated", "memory_context": values},
        "messages": [{"role": "system", "content": {"execution_context": {
            "execution_id": "isolated", "memory_context": values}}}]}}, envelope)
    return payload, envelope


def memory_values(call: dict) -> list[list[dict]]:
    """解析最终SDK JSON重复位置，使用行为断言而不是复制选择算法。"""
    value = json.loads(call["messages"][-1]["content"])["provider_execution_view"]
    return [value["execution_context"].get("memory_context", []),
            value["messages"][0]["content"]["execution_context"].get("memory_context", [])]


def test_final_sdk_json_repair_keeps_whole_same_selection_without_sidecar() -> None:
    """真实LLMClient修复两次最终发送；完整值双份相同且来源侧带不外泄。"""
    sdk = SDK(["not-json", '{"ok":true}'])
    payload, envelope = envelope_payload()
    assert llm(sdk).generate_json("系统", payload) == {"ok": True}
    assert len(sdk.calls) == 2
    for call in sdk.calls:
        values = memory_values(call)
        assert values[0] == values[1] == [envelope.candidates[0].provider_payload()]
        assert _request_tokens(call["messages"]) <= 4096
        assert "scope" not in json.dumps(call) and "candidate_digest" not in json.dumps(call)
    assert len(envelope.audits) == 2
    assert envelope.audits[0]["selected"] == envelope.audits[1]["selected"]


def test_final_sdk_stream_and_revoke_between_repairs() -> None:
    """流式仍完整；修复轮实际发送前撤销使旧事实从两个位置同时消失。"""
    payload, envelope = envelope_payload()
    sdk = SDK(["完整输出"])
    assert list(llm(sdk).generate_text_stream("系统", payload)) == ["完整输出"]
    assert memory_values(sdk.calls[0])[0] == [envelope.candidates[0].provider_payload()]
    permitted = [True]
    payload, envelope = envelope_payload(validator=lambda _: permitted[0])

    class RevokingSDK(SDK):
        """首个模型响应后控制精确撤销，不使用sleep碰撞。"""

        def create(self, **kwargs):
            """在第一次发送后撤销，第二次修复必须重新验证披露许可。"""
            response = super().create(**kwargs)
            permitted[0] = False
            return response

    sdk = RevokingSDK(["not-json", '{"ok":true}'])
    assert llm(sdk).generate_json("系统", payload) == {"ok": True}
    assert memory_values(sdk.calls[0])[0]
    assert memory_values(sdk.calls[1]) == [[], []]
    assert envelope.audits[-1]["revoked_count"] == 1


def test_frozen_restore_does_not_add_new_facts_and_tightening_is_monotonic() -> None:
    """序列化恢复只保留原SDK选择；预算收紧后后续修复不回填原大集合。"""
    payload, envelope = envelope_payload()
    sdk = SDK(['{"ok":true}'])
    llm(sdk).generate_json("系统", payload)
    restored = MemoryRequestEnvelope.restore(json.loads(json.dumps(envelope.snapshot())),
                                              validate=lambda _: True)
    restored.policy = MemoryProjectionPolicy(configured_memory_limit=0)
    second = MemoryRequestPayload(dict(payload), restored)
    sdk = SDK(['{"ok":true}', '{"ok":true}'])
    client = llm(sdk)
    client.generate_json("系统", second)
    restored.policy = MemoryProjectionPolicy()
    client.generate_json("系统", second)
    assert memory_values(sdk.calls[0]) == memory_values(sdk.calls[1]) == [[], []]


def test_actual_output_reserve_change_rechecks_verified_window() -> None:
    """可信上下文窗扣本次输出，收紧后非记忆仍超限则不触发SDK。"""
    payload, _ = envelope_payload()
    sdk = SDK(['{"ok":true}'])
    client = llm(sdk)
    client.verified_context_window = 129
    with pytest.raises(LLMError, match="INPUT_BUDGET_EXCEEDED"):
        client.generate_json("系统", payload)
    assert sdk.calls == []


def test_model_and_policy_changes_create_traceable_derived_projection() -> None:
    """恢复沿父投影身份；模型/策略变化形成新版本但不补入其他新事实。"""
    payload, original = envelope_payload()
    llm(SDK(['{"ok":true}'])).generate_json("系统", payload)
    parent = original.snapshot()["projection_id"]
    restored = MemoryRequestEnvelope.restore(original.snapshot(), validate=lambda _: True)
    client = llm(SDK(['{"ok":true}']))
    client.model = "new-authorized-model"
    client.memory_projection_model_identity = {"generation": 2}
    restored.policy = MemoryProjectionPolicy(configured_memory_limit=128, max_items=20)
    client.generate_json("系统", MemoryRequestPayload(dict(payload), restored))
    audit = restored.audits[-1]
    assert audit["parent_projection_id"] == parent
    assert audit["projection_id"] != parent
    assert audit["model_identity"]["generation"] == 2
    assert audit["policy_settings"]["configured_memory_limit"] == 128
    assert {item.id for item in restored.selected} <= {item.id for item in original.selected}


def test_final_dispatch_observer_revocation_blocks_prepared_selection() -> None:
    """准备请求后、真实SDK前发生撤销时，不能仅凭较早的预算快照披露。"""
    permitted = [True]
    payload, envelope = envelope_payload(validator=lambda _: permitted[0])
    sdk = SDK(['{"ok":true}'])
    client = llm(sdk)
    client.dispatch_observer = lambda: permitted.__setitem__(0, False)
    assert client.generate_json("系统", payload) == {"ok": True}
    assert memory_values(sdk.calls[0]) == [[], []]
    assert envelope.audits[-1]["revoked_count"] == 1


def test_router_repair_revocation_removes_stage_echo_and_invalid_model_output() -> None:
    """Router修复在撤销后不经阶段历史或previous_output再次披露旧事实。"""
    from app.llm.stage_protocol import stage_payload

    permitted = [True]
    fact = MemoryCandidate(id="name", kind="profile", key="name", content="青禾旧事实")
    envelope = MemoryRequestEnvelope((fact,), "任务", validate=lambda _: permitted[0])
    payload = MemoryRequestPayload(stage_payload(
        phase="Router", user_message="任务", conversation_context={"messages": [
            {"role": "user", "content": "历史用户问题可保留"},
            {"role": "assistant", "content": "router-revoked-assistant-echo-CANARY"},
        ]},
        memory_context=[fact.provider_payload()], instructions="路由", stage_data={}, output_contract={},
    ), envelope)

    class RevokingSDK(SDK):
        """第一轮返回含记忆的坏JSON，再精确撤销发送许可。"""

        def create(self, **kwargs):
            """控制下一轮发送前权限状态，无概率竞争。"""
            response = super().create(**kwargs)
            permitted[0] = False
            return response

    sdk = RevokingSDK(["青禾旧事实:invalid-json", '{"ok":true}'])
    assert llm(sdk).generate_json("系统", payload) == {"ok": True}
    assert "青禾旧事实" in str(sdk.calls[0]["messages"])
    assert "青禾旧事实" not in str(sdk.calls[1]["messages"])
    assert "历史用户问题可保留" in str(sdk.calls[1]["messages"])
    assert "router-revoked-assistant-echo-CANARY" not in str(sdk.calls[1]["messages"])


def test_dta_action_repair_revocation_scrubs_nested_derived_echoes() -> None:
    """DTA真实动作修复在撤权后清掉嵌套模型回声并保留计划与用户上下文。"""
    from app.dynamic_tasks.action_proposer import DynamicActionProposer
    from app.dynamic_tasks.planning import PlanStep
    from app.dynamic_tasks.provider_view import build_provider_execution_view

    permitted = [True]
    fact = MemoryCandidate(
        id="revocable-name",
        kind="profile",
        key="name",
        content="synthetic-revoked-memory-value",
        metadata={"key": "name"},
    )
    envelope = MemoryRequestEnvelope((fact,), "synthetic current request",
                                     validate=lambda _: permitted[0])
    plan_checksum = "f" * 64
    execution_context = {
        "execution_id": "synthetic-execution",
        "execution_revision": 3,
        "goal": {"goal": "synthetic frozen goal"},
        "plan_revision_id": "synthetic-plan-revision",
        "plan_checksum": plan_checksum,
        "constraints": (),
        "success_criteria": (),
        "plan_steps": ({"step_key": "answer", "kind": "answer"},),
        "completed_steps": ({
            "step_key": "previous-result",
            "model_output": {"summary": "synthetic-derived-echo"},
        },),
        "pending_action": None,
        "operations": (),
        "input_resources": (),
        "memory_context": [fact.provider_payload()],
        "conversation_context": (
            {"role": "user", "content": "synthetic-user-origin-context"},
            {"role": "assistant", "content": "synthetic-derived-assistant-echo"},
        ),
    }
    view = build_provider_execution_view(
        execution_context=execution_context,
        canonical_messages=({"role": "user", "content": "synthetic current request"},),
        model_capabilities={},
    )
    view._memory_envelope = envelope

    class RevokingSDK(SDK):
        """首个最终动作SDK响应后切断来源许可，固定触发修复轮。"""

        def create(self, **kwargs):
            """让下一次最终派发观察到撤权，避免依赖时间竞争。"""
            response = super().create(**kwargs)
            permitted[0] = False
            return response

    response = json.dumps({
        "action_kind": "answer",
        "arguments": {"markdown": "synthetic completion", "criterion_evidence": {},
                      "pending_questions": []},
        "rationale": "synthetic completion",
    })
    sdk = RevokingSDK(["not-json", response])
    proposer = DynamicActionProposer(llm(sdk))
    proposal = proposer.propose(
        view=view,
        step=PlanStep(step_key="answer", title="synthetic answer", kind="answer"),
    )

    assert proposal.proposal.action_kind.value == "answer"
    assert len(sdk.calls) == 2
    second_request = json.loads(sdk.calls[1]["messages"][-1]["content"])
    provider_view = second_request["provider_execution_view"]
    execution = provider_view["execution_context"]
    context_message = provider_view["messages"][0]["content"]["execution_context"]
    second_serialized = json.dumps(second_request, ensure_ascii=False)
    second_system_prompt = "\n".join(
        str(message.get("content", "")) for message in sdk.calls[1]["messages"]
        if message.get("role") == "system"
    )

    revoked_selection_empty = (
        execution["memory_context"] == []
        and context_message["memory_context"] == []
    )
    derived_echo_absent = (
        "synthetic-derived-echo" not in second_serialized
        and "synthetic-derived-assistant-echo" not in second_serialized
    )
    ordinary_user_context_preserved = "synthetic-user-origin-context" in second_serialized
    frozen_plan_identity_preserved = execution["plan_checksum"] == plan_checksum
    assert revoked_selection_empty is True
    assert derived_echo_absent is True
    assert ordinary_user_context_preserved is True
    assert frozen_plan_identity_preserved is True
    first_system_prompt = "\n".join(
        str(message.get("content", "")) for message in sdk.calls[0]["messages"]
        if message.get("role") == "system"
    )
    assert "已因授权状态变化失效" not in first_system_prompt
    assert "已因授权状态变化失效" in second_system_prompt
    assert "只能依据当前用户输入或仍被授权且实际投影的事实" in second_system_prompt


def test_dta_revocation_scrubs_all_planner_echoes_and_keeps_required_receipt_reference() -> None:
    """撤权修复清除计划、提案和回声派生事实，仅保留必需外部回执。"""
    from app.dynamic_tasks.action_proposer import DynamicActionProposer
    from app.dynamic_tasks.planning import PlanStep
    from app.dynamic_tasks.provider_view import build_provider_execution_view

    permitted = [True]
    fact = MemoryCandidate(id="revoked-customer", kind="profile", key="customer",
                           content="客户全名：CYGNUS77")
    envelope = MemoryRequestEnvelope((fact,), "完成原外部任务", validate=lambda _: permitted[0])
    # 该Sidecar由Runtime按已完成外部Operation的reconcile.reference_source和当前输入schema构造；
    # 它不属于候选记忆，也不会序列化进MemoryRequestEnvelope快照。
    envelope.revoked_execution_references = {
        "submit": {"receipt_id": "receipt-opaque-123"},
    }
    view = build_provider_execution_view(
        execution_context={
            "execution_id": "synthetic-execution",
            "plan_revision_id": "frozen-plan",
            "plan_checksum": "f" * 64,
            "constraints": ["revoked-plan-constraint-CANARY"],
            "success_criteria": [{
                "id": "criterion",
                "type": "assertion",
                "spec": {"expected_customer": "revoked-success-criterion-CANARY"},
            }],
            "plan_steps": ({"step_key": "clarify_partner", "kind": "clarification"},
                           {"step_key": "submit", "kind": "tool.write"},
                           {"step_key": "finish", "kind": "tool.write",
                            "title": "revoked-frozen-step-CANARY",
                            "expected_output_schema": {
                                "properties": {
                                    "customer": {"const": "revoked-step-schema-CANARY"},
                                },
                            }}),
            "pending_action": {
                "proposal_id": "proposal-old",
                "step_key": "submit",
                "checksum": "a" * 64,
                "proposal": {
                    "arguments": {"customer": "revoked-pending-proposal-CANARY"},
                    "rationale": "revoked-pending-rationale-CANARY",
                },
            },
            "completed_steps": (
                {"step_key": "submit", "model_output": {
                    "receipt_id": "receipt-opaque-123",
                    "customer": "CYGNUS77",
                    "summary": "synthetic-derived-echo",
                }},
                {"step_key": "clarify_partner", "model_output": {
                    "source_kind": "authenticated_user_clarification",
                    "answer": "当前用户确认：只依据当前授权来源继续。",
                }},
            ),
            "memory_context": [fact.provider_payload()],
            "conversation_context": ({"role": "user", "content": "继续原任务"},
                                     {"role": "assistant", "content": "旧事实 CYGNUS77"}),
        },
        canonical_messages=({"role": "system", "content": {
                                "guidance_requirements": [{
                                    "requirement_id": "requirement-safe-id",
                                    "skill_use_id": "skill-use-safe-id",
                                    "principle": "经权威来源核实的原则",
                                    "task_mapping": "revoked-guidance-mapping-CANARY",
                                    "observable_acceptance": "revoked-guidance-acceptance-CANARY",
                                    "disposition": "apply",
                                }],
                            }},
                            {"role": "user", "content": "继续原任务"},
                            {"role": "user", "content": {
                                "result_repair_feedback": {
                                    "code": "DYNAMIC_ACTION_PROPOSAL_INVALID",
                                    "error": "revoked-repair-feedback-CANARY",
                                },
                            }}),
        model_capabilities={},
    )
    view._memory_envelope = envelope

    class RevokingSDK(SDK):
        """首轮模型响应后撤销来源，确定触发同动作修复轮。"""

        def create(self, **kwargs):
            """只在首轮实际SDK返回后撤权，修复轮必须按撤权后的视图重投影。"""
            response = super().create(**kwargs)
            permitted[0] = False
            return response

    action = json.dumps({
        "action_kind": "call_tool",
        "arguments": {"receipt_id": "receipt-opaque-123"},
        "capability_ref": "final_f1.finish",
        "expected_output_schema": {},
        "rationale": "沿用原已受理任务的回执引用",
    })
    sdk = RevokingSDK(["not-json", action])
    result = DynamicActionProposer(llm(sdk)).propose(
        view=view,
        step=PlanStep(step_key="finish", title="revoked-current-step-CANARY", kind="tool.write",
                      depends_on=("submit",), capability_refs=("final_f1.finish",)),
    )

    assert result.proposal.arguments == {"receipt_id": "receipt-opaque-123"}
    assert len(sdk.calls) == 2
    second = json.loads(sdk.calls[1]["messages"][-1]["content"])
    context = second["provider_execution_view"]["execution_context"]
    completed = context["completed_steps"]
    assert context["memory_context"] == []
    assert completed == [
        {"step_key": "submit", "model_output": {
            "receipt_id": "receipt-opaque-123",
        }},
        {"step_key": "clarify_partner", "model_output": {
            "source_kind": "authenticated_user_clarification",
            "answer": "当前用户确认：只依据当前授权来源继续。",
        }},
    ]
    serialized = json.dumps(second, ensure_ascii=False)
    assert "CYGNUS77" not in serialized
    assert "synthetic-derived-echo" not in serialized
    for revoked_value in (
        "revoked-plan-constraint-CANARY",
        "revoked-success-criterion-CANARY",
        "revoked-frozen-step-CANARY",
        "revoked-step-schema-CANARY",
        "revoked-pending-proposal-CANARY",
        "revoked-pending-rationale-CANARY",
        "revoked-current-step-CANARY",
        "revoked-repair-feedback-CANARY",
        "revoked-guidance-mapping-CANARY",
        "revoked-guidance-acceptance-CANARY",
    ):
        assert revoked_value not in serialized
    assert context["plan_checksum"] == "f" * 64


def test_dta_answer_revocation_scrubs_planner_guidance_mapping_before_sdk() -> None:
    """答案步骤撤权后保留权威Guidance身份，但不重发规划自由文本。"""
    from app.dynamic_tasks.action_proposer import DynamicActionProposer
    from app.dynamic_tasks.planning import PlanStep
    from app.dynamic_tasks.provider_view import build_provider_execution_view

    permitted = [True]
    fact = MemoryCandidate(id="revoked-guidance-source", kind="fact", key="fact",
                           content="revoked-guidance-memory-CANARY")
    envelope = MemoryRequestEnvelope((fact,), "继续当前任务", validate=lambda _: permitted[0])
    requirement = {
        "requirement_id": "requirement-safe-id",
        "skill_use_id": "skill-use-safe-id",
        "principle": "approved-authoritative-principle",
        "task_mapping": "feedback loop hypothesis revoked-guidance-mapping-CANARY",
        "observable_acceptance": "revoked-guidance-acceptance-CANARY",
        "disposition": "apply",
    }
    view = build_provider_execution_view(
        execution_context={
            "execution_id": "synthetic-guidance-execution",
            "execution_revision": 1,
            "goal": {"goal": "继续当前任务"},
            "plan_revision_id": "synthetic-guidance-plan",
            "plan_checksum": "a" * 64,
            "constraints": [],
            "success_criteria": [{"id": "criterion", "type": "assertion", "spec": {}}],
            "plan_steps": [{"step_key": "answer", "kind": "answer"}],
            "completed_steps": [],
            "pending_action": None,
            "operations": [],
            "input_resources": [],
            "memory_context": [fact.provider_payload()],
            "conversation_context": [],
        },
        canonical_messages=(
            {"role": "system", "content": {
                "general_skill_guidance": [{
                    "skill_use_id": "skill-use-safe-id",
                    "instructions": "经批准的通用冻结指导来源",
                }],
                "guidance_requirements": [requirement],
            }},
            {"role": "user", "content": {
                "text": "继续当前任务",
                "instruction": "revoked-dynamic-instruction-CANARY 候选URL数=7",
            }},
        ),
        model_capabilities={},
    )
    view._memory_envelope = envelope

    class RevokingSDK(SDK):
        """在第一次实际请求后撤权，固定触发JSON修复请求。"""

        def create(self, **kwargs):
            """令修复请求观察到已撤销的记忆来源许可。"""
            response = super().create(**kwargs)
            permitted[0] = False
            return response

    answer = json.dumps({
        "action_kind": "answer",
        "arguments": {"markdown": "按当前有效事实完成", "criterion_evidence": {},
                      "pending_questions": [], "guidance_applications": []},
        "rationale": "完成当前答案步骤",
    })
    sdk = RevokingSDK(["not-json", answer])
    proposal = DynamicActionProposer(llm(sdk)).propose(
        view=view,
        step=PlanStep(step_key="answer", title="当前答案", kind="answer"),
    )

    assert proposal.proposal.action_kind.value == "answer"
    assert len(sdk.calls) == 2
    first_request = str(sdk.calls[0]["messages"])
    second_request = str(sdk.calls[1]["messages"])
    assert "revoked-guidance-mapping-CANARY" in first_request
    assert "revoked-guidance-acceptance-CANARY" in first_request
    assert "revoked-dynamic-instruction-CANARY" in first_request
    assert "诊断结论/根因" in first_request
    assert "revoked-guidance-mapping-CANARY" not in second_request
    assert "revoked-guidance-acceptance-CANARY" not in second_request
    assert "revoked-dynamic-instruction-CANARY" not in second_request
    assert "候选URL数=7" not in second_request
    assert "诊断结论/根因" not in second_request
    assert "requirement-safe-id" in second_request
    assert "approved-authoritative-principle" in second_request


def test_router_actual_sdk_whole_facts_and_step_no_memory(monkeypatch) -> None:
    """真实Router阶段经最终SDK完整选事实，Step阶段继续没有长期记忆注入。"""
    from app.core.router import Router
    from app.db.models import ChatSession, ModelConfig
    from app.llm.stage_protocol import stage_payload

    sdk = SDK(['{"decision":"answer_only","confidence":0.9}'])
    monkeypatch.setattr("app.core.router.LLMClient", lambda _: llm(sdk))
    content = "青禾\n规则 a  b\n\t保留"
    facts = [{"kind": "profile", "content": content},
             {"kind": "fact", "content": "超大" * 10000}]
    Router().decide("名称", ChatSession(id="s", tenant_id="t"), [],
                    ModelConfig(tenant_id="t", name="test", api_key_encrypted="", model="test"),
                    memory_context=facts)
    current = sdk.calls[0]["messages"][-1]["content"]
    assert "- " + content in current
    assert "超大" not in current
    assert _request_tokens(sdk.calls[0]["messages"]) <= 128000
    step = stage_payload(phase="Step", user_message="名称", conversation_context={},
                         memory_context=facts, instructions="步骤", stage_data={}, output_contract={})
    assert step["_agent_stage"]["memory"] == ""


def test_actual_planner_action_and_answer_sdk_share_frozen_complete_selection() -> None:
    """真实Planner/Action/answer执行到SDK，重复上下文完整且沿同一冻结选择。"""
    from app.dynamic_tasks.action_proposer import DynamicActionProposer
    from app.dynamic_tasks.planner_service import DynamicTaskPlanner
    from app.dynamic_tasks.planning import PlanStep, SuccessCriterion
    from app.dynamic_tasks.provider_view import build_provider_execution_view
    from app.memory.request_projection import MemoryContext

    fact = MemoryCandidate(id="name", kind="profile", key="name", content="青禾\n\t规则 a  b",
                           metadata={"key": "name"})
    huge = MemoryCandidate(id="huge", kind="fact", key="huge", content="字" * 4001)
    envelope = MemoryRequestEnvelope((fact, huge), "任务",
                                     MemoryProjectionPolicy(max_items=20, max_fact_characters=4000))
    criterion = SuccessCriterion(id="criterion", type="assertion", spec={"required": True})
    plan_json = json.dumps({"goal": "任务", "success_criteria": [criterion.model_dump(mode="json")],
                           "steps": [{"draft_id": "answer", "kind": "answer", "title": "答案"}]})
    sdk = SDK([plan_json])
    DynamicTaskPlanner(llm(sdk)).create_plan(
        goal="任务", success_criteria=(criterion,), capabilities=(),
        memory_context=MemoryContext([fact.provider_payload(), huge.provider_payload()], envelope),
    )
    planner_input = json.loads(sdk.calls[0]["messages"][-1]["content"])
    assert planner_input["memory_context"] == [fact.provider_payload()]
    assert envelope.selected == (fact,)
    snapshot = json.loads(json.dumps(envelope.snapshot()))
    for kind, response in (
        ("tool.read", {"action_kind": "call_tool", "capability_ref": "contract.query",
                       "arguments": {}, "rationale": "读取"}),
        ("answer", {"action_kind": "answer", "arguments": {"markdown": "结论",
                     "criterion_evidence": {}, "pending_questions": []}, "rationale": "完成"}),
    ):
        view = build_provider_execution_view(
            execution_context={"execution_id": "e", "memory_context": [fact.provider_payload()],
                               "goal": {"goal": "任务"}},
            canonical_messages=[{"role": "user", "content": "当前任务"}], model_capabilities={},
        )
        view._memory_envelope = MemoryRequestEnvelope.restore(snapshot, validate=lambda _: True)
        sdk = SDK([json.dumps(response)])
        DynamicActionProposer(llm(sdk)).propose(view=view, step=PlanStep(
            step_key="current", title="当前步骤", kind=kind,
            capability_refs=("contract.query",) if kind == "tool.read" else (),
        ))
        assert memory_values(sdk.calls[0]) == [[fact.provider_payload()], [fact.provider_payload()]]
        assert _request_tokens(sdk.calls[0]["messages"]) <= 128000
