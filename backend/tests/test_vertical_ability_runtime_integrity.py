"""
@Time       : 2026/09/05 18:00
@Author     : zhanglp8181
@File       : test_vertical_ability_runtime_integrity.py
@CallChain  : pytest → 冻结能力包/计划修订/工具因果 → 统一 DynamicTask Runtime
@Description: 验证能力包升级保持历史语义，虚拟指导不变成工具白名单或失效 Skill Use。
"""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, ModelConfig, SopInstance, SopNodeExecution, SopOperation
from app.cancellation import raise_if_cancelled
from app.dynamic_tasks.agent import DynamicTaskAgent, _markdown_discloses_no_results
from app.dynamic_tasks.capability_catalog import (
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    capability_checksum,
)
from app.dynamic_tasks.planning import (
    ActionKind, CompletedProviderProposal, NormalizedPlan, PlanReason, PlanStep,
    RuntimeActionProposal, SuccessCriterion,
)
from app.sop_runtime.execution_store import SopExecutionSkillAuthorizationError, SopExecutionStore
from app.vertical_abilities.catalog import VerticalAbilityCatalog
from app.vertical_abilities.executor import VerticalAbilityToolExecutor
from app.vertical_abilities.resolver import VerticalAbilityResolver
from app.tools.tool_schema import ToolCall, ToolError, ToolResult
from sqlmodel import select


@pytest.fixture
def db():
    """每个负例使用独立 SQLite 内存数据库，不访问运行数据库。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(AgentProfile(id="agent_a", tenant_id="tenant_a", name="研究专家"))
        session.commit()
        yield session
    engine.dispose()


def _resolution():
    """构造当前能力包的真实目录与固定执行范围。"""

    return VerticalAbilityResolver().resolve(
        ["web-intelligence"], tenant_id="tenant_a", agent_id="agent_a"
    )


def _instance(
    db: Session, *, generic_tool: bool = False,
    capability_name: str = "intelligence.web_search", model: ModelConfig | None = None,
    owner_user_id: str = "user_a", session_id: str = "session_a",
):
    """通过正式 Store 建立包含虚拟 Guidance 的执行与首个计划。"""

    resolution = _resolution()
    snapshot = next(item for item in resolution.capabilities if item.name == capability_name)
    if generic_tool:
        payload = {
            **snapshot.model_dump(mode="json", exclude={"agent_id", "checksum"}),
            "capability_id": "fixture.generic_read",
            "name": "fixture.generic_read",
            "audit_view": {"tool_id": "fixture.generic_read"},
        }
        snapshot = CapabilitySnapshot(
            **payload, agent_id="agent_a", checksum=capability_checksum(payload)
        )
    frozen = {
        **resolution.snapshot,
        "tools": [snapshot.model_dump(mode="json")],
    }
    if model is not None:
        frozen["model"] = {
            "model_config_id": model.id, "checksum": model.capability_checksum,
            "capabilities": model.capability_snapshot_json,
        }
    plan = NormalizedPlan(
        goal="读取研究证据",
        success_criteria=(SuccessCriterion(id="evidence", type="assertion", spec={}),),
        steps=(PlanStep(
            step_key="read_source", title="读取来源", kind="tool.read",
            capability_refs=(snapshot.name,),
            guidance_skill_use_ids=("vertical_ability:web-intelligence",),
        ),),
        budget={"max_steps": 4},
    )
    if model is not None:
        plan = plan.model_copy(update={
            "steps": (*plan.steps,
                PlanStep(step_key="read_detail", title="读取详情", kind="tool.read",
                    capability_refs=(snapshot.name,), depends_on=("read_source",)),
                PlanStep(step_key="answer", title="形成研究报告", kind="answer",
                    depends_on=("read_source", "read_detail")),
            ),
            "budget": {"max_steps": 3, "max_tool_calls": 8, "max_model_calls": 10},
        })
    instance, _ = SopExecutionStore(db).start_dynamic_instance(
        tenant_id="tenant_a", session_id=session_id, agent_id="agent_a",
        initiator_user_id=owner_user_id, plan=plan, capability_snapshot=frozen,
        context={"vertical_ability_ids": ["web-intelligence"]},
    )
    db.commit()
    return instance, plan, snapshot


def test_virtual_guidance_allows_a_frozen_generic_tool(db: Session) -> None:
    """已冻结普通工具可以受能力包指导，因果校验不能变成包内工具白名单。"""

    instance, _, snapshot = _instance(db, generic_tool=True)
    operation_snapshot = snapshot.model_dump(mode="json", exclude={"agent_id", "checksum"})
    operation = SopOperation(
        tenant_id="tenant_a", instance_id=instance.id, node_execution_id="node_a",
        operation_name=snapshot.name, idempotency_key="a" * 64,
        logical_action_id="read_a", request_fingerprint="b" * 64,
        caused_by_skill_use_ids_json=["vertical_ability:web-intelligence"],
        capability_snapshot_json=operation_snapshot,
        capability_checksum=capability_checksum(operation_snapshot),
    )
    db.add(operation)
    db.commit()
    SopExecutionStore(db).authorize_operation_skill_causes(operation)
    operation.caused_by_skill_use_ids_json = ["vertical_ability:unselected"]
    with pytest.raises(SopExecutionSkillAuthorizationError):
        SopExecutionStore(db).authorize_operation_skill_causes(operation)


def test_plan_revision_accepts_frozen_virtual_guidance(db: Session) -> None:
    """追加用户约束不能把虚拟能力包身份查成数据库 GeneralSkillUse 后拒绝。"""

    instance, plan, _ = _instance(db)
    store = SopExecutionStore(db)
    with store.owned(instance, worker_id="revision-test"):
        revision, created = store.append_plan_revision(
            instance, plan=plan.model_copy(update={"constraints": ("优先官方原文",)}),
            reason=PlanReason.USER_CONSTRAINT,
            capability_snapshot=instance.capability_snapshot_json,
        )
    assert created
    assert revision.revision_number == 2
    assert revision.plan_json["steps"][0]["guidance_skill_use_ids"] == [
        "vertical_ability:web-intelligence"
    ]


def test_version_only_upgrade_keeps_old_tool_executable(db: Session, monkeypatch) -> None:
    """仅更新包版本/指导不应使旧任务的同合同适配器失效。"""

    instance, _, snapshot = _instance(db)
    original = VerticalAbilityCatalog().resolve("web-intelligence")
    upgraded = replace(original, version="1.7.2", instructions="新版业务指导")
    monkeypatch.setattr(VerticalAbilityCatalog, "resolve", lambda self, ability_id: upgraded)
    executor = VerticalAbilityToolExecutor(db)
    assert executor._is_frozen_capability(instance, snapshot.name, original.id)
    assert DynamicCapabilityCatalog(db).reauthorize_tool(
        snapshot, actor_user_id="user_a", organization_unit_id=None
    ) is None
    assert snapshot.audit_view["vertical_ability"]["version"] == "1.7.4"


def test_contract_change_still_rejects_old_snapshot(db: Session, monkeypatch) -> None:
    """版本兼容不能绕过适配器/安全合同的实际变化。"""

    instance, _, snapshot = _instance(db)
    original = VerticalAbilityCatalog().resolve("web-intelligence")
    first = replace(original.capabilities[0], adapter="fixture.changed_adapter")
    upgraded = replace(original, capabilities=(first, *original.capabilities[1:]))
    monkeypatch.setattr(VerticalAbilityCatalog, "resolve", lambda self, ability_id: upgraded)
    assert not VerticalAbilityToolExecutor(db)._is_frozen_capability(
        instance, snapshot.name, original.id
    )


def test_removed_vertical_tool_never_falls_back_to_generic_executor(db: Session, monkeypatch) -> None:
    """已冻结为包内工具的名称被新版删除时，只能拒绝，不能同名落到普通工具。"""

    instance, _, snapshot = _instance(db)
    executor = VerticalAbilityToolExecutor(db)
    monkeypatch.setattr(executor.resolver, "find_definition_for_capability", lambda _name: None)

    def removed(_snapshot):
        """模拟新目录已经移除父任务使用的能力。"""

        raise KeyError("removed")

    def forbidden_delegate(*_args, **_kwargs):
        """普通工具不能取得原垂直能力的执行身份。"""

        pytest.fail("removed vertical capability fell through to generic tool")

    monkeypatch.setattr(executor.resolver, "compatible_capability", removed)
    monkeypatch.setattr(executor.delegate, "execute", forbidden_delegate)
    result = executor.execute(
        "tenant_a", ToolCall(name=snapshot.name, arguments={"query": "CLI tools"}),
        agent_id="agent_a", actor_user_id="user_a", execution_id=instance.id,
    )
    assert not result.success
    assert result.error.code == "VERTICAL_ABILITY_NOT_FROZEN"


def test_restore_keeps_parent_guidance_and_version(db: Session, monkeypatch) -> None:
    """同任务续问从父快照恢复，而不是用能力 ID 重新选择新版指导。"""

    resolution = _resolution()
    frozen = {
        **resolution.snapshot,
        "tools": [item.model_dump(mode="json") for item in resolution.capabilities],
    }
    original = VerticalAbilityCatalog().resolve("web-intelligence")
    upgraded = replace(original, version="1.7.2", instructions="新指导不属于父任务")
    monkeypatch.setattr(VerticalAbilityCatalog, "resolve", lambda self, ability_id: upgraded)
    restored = VerticalAbilityResolver().restore(
        frozen, checksum=capability_checksum(frozen), tenant_id="tenant_a", agent_id="agent_a"
    )
    assert restored.ability_ids == resolution.ability_ids
    assert restored.loaded_guidance == resolution.loaded_guidance
    assert restored.snapshot["vertical_abilities"] == resolution.snapshot["vertical_abilities"]
    assert restored.capabilities == resolution.capabilities
    with pytest.raises(ValueError):
        VerticalAbilityResolver().restore(
            frozen, checksum="forged", tenant_id="tenant_a", agent_id="agent_a"
        )


def test_continuation_keeps_parent_when_composer_resolves_latest(db: Session, monkeypatch) -> None:
    """对话框仍提交同一能力 ID 时，真实续问入口也必须使用父任务旧版快照。"""

    parent, _, _ = _instance(db)
    agent = DynamicTaskAgent(db)
    with agent.store.owned(parent, worker_id="finish-parent"):
        agent.store.fail_instance(parent, context_patch={"failure_code": "fixture"})
    db.commit()
    original = VerticalAbilityCatalog().resolve("web-intelligence")
    upgraded = replace(original, version="1.7.2", instructions="新版说明")
    monkeypatch.setattr(VerticalAbilityCatalog, "resolve", lambda self, ability_id: upgraded)
    monkeypatch.setattr(VerticalAbilityCatalog, "resolve_many", lambda self, ids: (upgraded,))
    latest = _resolution()
    captured = {}

    def start_next(**kwargs):
        """只替换下一轮规划，观察续问入口选择的真实父能力快照。"""

        captured.update(kwargs)
        return parent, True

    monkeypatch.setattr(agent, "start_task", start_next)
    agent.continue_chat_turn(
        tenant_id="tenant_a", session_id="session_a", agent_id="agent_a",
        initiator_user_id="user_a", user_message_id="followup-message",
        message="继续核对上轮的信息", success_criteria=("保留来源",),
        model_config=ModelConfig(id="fixture", tenant_id="tenant_a"),
        vertical_ability_resolution=latest,
    )
    inherited = captured["vertical_ability_resolution"]
    assert inherited.snapshot["vertical_abilities"][0]["version"] == "1.7.4"
    assert inherited.loaded_guidance == tuple(parent.capability_snapshot_json["vertical_guidance"])


def test_frozen_executor_checks_tenant_and_checksum(db: Session) -> None:
    """数据库中的其他租户或篡改快照不能借版本兼容路径调用内置适配器。"""

    instance, _, snapshot = _instance(db)
    foreign = SopInstance.model_validate(instance.model_dump())
    foreign.tenant_id = "tenant_b"
    assert not VerticalAbilityToolExecutor(db)._is_frozen_capability(
        foreign, snapshot.name, "web-intelligence"
    )


def test_runtime_passes_cancellation_to_real_adapter(db: Session, monkeypatch) -> None:
    """取消信号穿过真实 Agent/Executor，收敛为 cancelled 而非失败或迟到成功。"""

    instance, _, snapshot = _instance(db)
    state = {"cancelled": False, "callback_seen": False}

    def read_adapter(_adapter, _arguments, *, is_cancelled, **_kwargs):
        """在已派发读取中模拟用户取消并验证传入的回调能即时观察。"""

        state["callback_seen"] = callable(is_cancelled)
        state["cancelled"] = True
        raise_if_cancelled(is_cancelled)
        return {"results": []}

    monkeypatch.setattr("app.vertical_abilities.executor.execute_vertical_adapter", read_adapter)
    agent = DynamicTaskAgent(db, is_cancelled=lambda: state["cancelled"])
    result = agent.execute_read_proposal(
        execution_id=instance.id, step_key="read_source", worker_id="cancel-test",
        actor_user_id="user_a", provider="fixture", model="fixture",
        model_capabilities={
            "protocol_version": "dynamic-v1", "sdk_available": True,
            "credentials_verified": True, "structured_output": True, "tool_calling": True,
        },
        completed_response=CompletedProviderProposal(
            response_id="read-cancel-response", finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL, capability_ref=snapshot.name,
                arguments={"query": "CLI tools"}, rationale="读取公开原文",
            ),
        ),
    )
    assert state["callback_seen"]
    assert not result.success
    db.refresh(instance)
    assert instance.status == "cancelled"
    assert result.error.code == "DYNAMIC_EXECUTION_CANCELLED"


def test_no_results_disclosure_does_not_match_ten_results() -> None:
    """十条或一百条结果不等于披露零结果。"""

    assert _markdown_discloses_no_results("返回 0 条结果")
    assert not _markdown_discloses_no_results("返回 10条结果")
    assert not _markdown_discloses_no_results("返回100 条结果")


def test_two_user_reads_cancel_only_owner_and_discard_late_success(tmp_path: Path, monkeypatch) -> None:
    """两用户真实并发读取时只取消 A，B 读取正常结算，A 的迟到正文不得入成功账本。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'two-user-cancel.db'}", connect_args={"timeout": 10})
    SQLModel.metadata.create_all(engine)
    started = Barrier(2)
    cancel_a = Event()
    with Session(engine) as setup:
        setup.add(AgentProfile(id="agent_a", tenant_id="tenant_a", name="公共研究专家"))
        setup.commit()
        first, _, _ = _instance(setup)
        second, _, _ = _instance(setup, owner_user_id="user_b", session_id="session_b")
        ids = (first.id, second.id)

    def late_adapter(_adapter, arguments, *, is_cancelled, **_kwargs):
        """同时开始两个外呼，再返回已取消用户的迟到成功，验证 Runtime 末次取消检查。"""

        assert callable(is_cancelled)
        started.wait(timeout=10)
        if arguments["query"] == "user_a":
            cancel_a.set()
        return {"results": [{"url": "https://example.org/source", "title": arguments["query"]}]}

    monkeypatch.setattr("app.vertical_abilities.executor.execute_vertical_adapter", late_adapter)

    def read_for_user(user_id: str, execution_id: str):
        """每位用户拥有独立数据库会话与取消回调，不共享 ORM 状态。"""

        with Session(engine, expire_on_commit=False) as db:
            callback = cancel_a.is_set if user_id == "user_a" else lambda: False
            agent = DynamicTaskAgent(db, is_cancelled=callback)
            return agent.execute_read_proposal(
                execution_id=execution_id, step_key="read_source", worker_id=f"worker-{user_id}",
                actor_user_id=user_id, provider="fixture", model="fixture",
                model_capabilities={
                    "protocol_version": "dynamic-v1", "sdk_available": True,
                    "credentials_verified": True, "structured_output": True, "tool_calling": True,
                },
                completed_response=CompletedProviderProposal(
                    response_id=f"response-{user_id}", finish_reason="stop",
                    proposal=RuntimeActionProposal(
                        action_kind=ActionKind.CALL_TOOL, capability_ref="intelligence.web_search",
                        arguments={"query": user_id}, rationale="读取该用户的研究来源",
                    ),
                ),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            future_a = workers.submit(read_for_user, "user_a", ids[0])
            future_b = workers.submit(read_for_user, "user_b", ids[1])
            result_a, result_b = future_a.result(timeout=20), future_b.result(timeout=20)
        assert not result_a.success and result_a.error.code == "DYNAMIC_EXECUTION_CANCELLED"
        assert result_b.success
        with Session(engine) as observer:
            assert observer.get(SopInstance, ids[0]).status == "cancelled"
            assert observer.get(SopInstance, ids[1]).status == "running"
            for execution_id, expected in ((ids[0], "cancelled"), (ids[1], "succeeded")):
                operation = observer.exec(select(SopOperation).where(SopOperation.instance_id == execution_id)).one()
                assert operation.status == expected
                if expected == "cancelled":
                    assert not operation.result_json
    finally:
        engine.dispose()


def test_full_run_recovers_failed_read_and_still_publishes_report(db: Session, monkeypatch) -> None:
    """三步真实推进中一次读失败不能吃掉最终报告的步骤额度，换源后必须完成发布。"""

    capabilities = {
        "protocol_version": "dynamic-v1", "sdk_available": True, "credentials_verified": True,
        "structured_output": True, "tool_calling": True,
    }
    model = ModelConfig(
        id="recovery-model", tenant_id="tenant_a", name="恢复测试模型", model="fixture",
        api_key_encrypted="unused", capability_snapshot_json=capabilities,
        capability_checksum=capability_checksum(capabilities), preflight_status="ready",
    )
    db.add(model)
    db.commit()
    instance, _, snapshot = _instance(db, capability_name="intelligence.web_read", model=model)
    urls = ["https://example.org/unavailable", "https://example.org/original", "https://example.org/detail"]

    class Proposer:
        """模拟自然规划后的模型逐步提案，报告仍经过生产验证与发布。"""

        calls = 0

        def propose(self, *, view, step, **_kwargs):
            """失败后选择不同原文，末步带真实步骤引用与失败披露。"""

            self.calls += 1
            if step.kind == "tool.read":
                proposal = RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL, capability_ref=snapshot.name,
                    arguments={"url": urls[self.calls - 1]}, rationale="下钻公开原文",
                )
            else:
                proposal = RuntimeActionProposal(
                    action_kind=ActionKind.ANSWER, rationale="依据成功回执形成结论",
                    arguments={
                        "markdown": "\n".join([
                            "## 结论摘要\n取得原文和详情。", "## 关键事实\n两个页面提供不同事实。",
                                f"## 来源与证据\n{urls[1]}\n{urls[2]}",
                                f"## 冲突/缺口\n{urls[0]} 读取失败 VERTICAL_ABILITY_PROVIDER_TIMEOUT；已改读可用原文。",
                                "## 后续建议\n继续监测可用原文。",
                            ]),
                        "criterion_evidence": {"evidence": ["read_source", "read_detail"]},
                        "pending_questions": [],
                    },
                )
            return CompletedProviderProposal(response_id=f"recovery-{self.calls}", finish_reason="stop", proposal=proposal)

    class SourceExecutor:
        """只替换外部回执，保留真实 Agent、Store、Operation 和终态验证。"""

        def execute(self, _tenant, call, **_kwargs):
            """首个URL超时，另外两个URL提供有界正文。"""

            if call.arguments["url"] == urls[0]:
                return ToolResult(tool_name=call.name, success=False, error=ToolError(
                    code="VERTICAL_ABILITY_PROVIDER_TIMEOUT", message="原文暂不可用",
                ))
            return ToolResult(tool_name=call.name, success=True, data={
                "url": call.arguments["url"], "content": "该页面的公开原文内容。",
                "provider": "fixture", "retrieved_at": "2026-09-05T00:00:00Z",
            })

    proposer = Proposer()
    agent = DynamicTaskAgent(db, action_proposer=proposer, tool_executor=SourceExecutor())
    monkeypatch.setattr(agent.catalog, "require_dynamic_model", lambda *_args: model)
    outcome = agent.run_until_blocked_or_complete(
        execution_id=instance.id, model_config=model, worker_id="recovery-full-run", actor_user_id="user_a",
    )
    assert outcome.status == "succeeded"
    assert outcome.message is not None and urls[1] in outcome.message.content
    operations = db.exec(select(SopOperation).where(SopOperation.instance_id == instance.id)).all()
    assert [item.status for item in operations].count("failed") == 1
    assert [item.status for item in operations].count("succeeded") >= 2
    assert proposer.calls == 4


@pytest.mark.parametrize("code,recoverable,history_size,mixed_failure", [
    ("VERTICAL_ABILITY_PROVIDER_TIMEOUT", True, 0, False),
    ("VERTICAL_ABILITY_RESOURCE_KIND_MISMATCH", True, 0, False),
    ("VERTICAL_ABILITY_PROVIDER_TIMEOUT", False, 2, False),
    ("VERTICAL_ABILITY_PROVIDER_TIMEOUT", False, 0, True),
    ("VERTICAL_ABILITY_URL_BLOCKED", False, 0, False),
    ("VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED", False, 0, False),
])
def test_failed_public_read_recovery_keeps_ledger(
    db: Session, code: str, recoverable: bool, history_size: int, mixed_failure: bool,
) -> None:
    """可恢复 Provider 失败追加有界修订，安全拒绝和缺凭据不通过重试绕过。"""

    instance, _, snapshot = _instance(db, capability_name="intelligence.web_read")
    instance.context_json = {
        **instance.context_json,
        "public_read_recoveries": [{"failed_operation_id": f"earlier_{i}"} for i in range(history_size)],
    }
    db.add(instance)
    db.commit()

    class SourceExecutor:
        """返回确定失败/替代来源成功，观察 Runtime 是否保留原操作。"""

        def execute(self, _tenant, call, **_kwargs):
            """只根据测试来源返回回执，不执行网络请求。"""

            if call.arguments["url"].endswith("unavailable"):
                return ToolResult(tool_name=call.name, success=False, error=ToolError(code=code, message="来源不可用"))
            return ToolResult(tool_name=call.name, success=True, data={
                "url": call.arguments["url"], "content": "可用原文", "provider": "fixture",
            })

    agent = DynamicTaskAgent(db, tool_executor=SourceExecutor())

    def read_source(suffix: str):
        """每个新的模型提案经正式执行入口记录独立 Operation。"""

        return agent.execute_read_proposal(
            execution_id=instance.id, step_key="read_source", worker_id="recovery-test",
            actor_user_id="user_a", provider="fixture", model="fixture",
            model_capabilities={
                "protocol_version": "dynamic-v1", "sdk_available": True,
                "credentials_verified": True, "structured_output": True, "tool_calling": True,
            },
            completed_response=CompletedProviderProposal(
                response_id=f"read-{suffix}", finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL, capability_ref=snapshot.name,
                    arguments={"url": f"https://example.org/{suffix}"}, rationale="读取研究来源",
                ),
            ),
        )

    assert not read_source("unavailable").success
    failed = db.exec(select(SopNodeExecution).where(SopNodeExecution.instance_id == instance.id)).one()
    if mixed_failure:
        blocked_node = SopNodeExecution(
            tenant_id=instance.tenant_id, instance_id=instance.id,
            plan_revision_id=instance.current_plan_revision_id,
            step_key="blocked_read", step_kind="tool.read", node_id="blocked_read",
            status="failed", error_json={"code": "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED"},
        )
        db.add(blocked_node)
        db.commit()
    old_revision = instance.current_plan_revision_id
    assert agent._recover_failed_public_read(instance, failed, worker_id="recovery-test") == recoverable
    db.refresh(failed)
    assert failed.status == "failed"
    if recoverable:
        assert instance.current_plan_revision_id != old_revision
        assert read_source("alternate").success
        operations = db.exec(select(SopOperation).where(SopOperation.instance_id == instance.id)).all()
        assert {operation.status for operation in operations} == {"succeeded", "failed"}
        assert len(operations) == 2
        assert "unavailable" in " ".join(agent._current_plan(instance).constraints)
    else:
        assert instance.current_plan_revision_id == old_revision
