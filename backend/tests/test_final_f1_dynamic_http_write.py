"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f1_dynamic_http_write.py
@CallChain  : 正式pytest → DTA目录/计划 → 原Attention批准 → HTTP Operation/Inbox恢复
@Description: 重现普通DTA无法使用已发布外部写，锁定独立批准、等待及失权零派发。
"""

from __future__ import annotations

import pytest
from datetime import timedelta
import httpx
from sqlalchemy import update
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.db.models import (
    ActionProposalRecord,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ExecutionResult,
    ExecutionSignal,
    Message,
    ModelConfig,
    SopInstance,
    SopOperation,
    SopOperationAttempt,
    SopWorkItem,
    SopWorkItemCandidate,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import (
    DynamicCapabilityCatalog,
    CapabilityAccessDenied,
    ToolReliabilityContract,
    dynamic_model_snapshot_checksum,
    published_tool_snapshot,
    publish_tool_contract,
)
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.external_identity import external_task_callback_token
from app.sop_runtime.external_tasks import (
    apply_pending_external_task_events,
    ingest_external_task_event,
)
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolError, ToolResult
from tests import test_final_f1_external_resume as resume_tests


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def http_write_engine(request, tmp_path):
    """复用F1真实双库夹具的迁移与清理，不复用任何成功业务状态。"""
    yield from resume_tests.resume_engine.__wrapped__(request, tmp_path)


class HttpWriteProposer:
    """按当前冻结步骤给工具参数，最终回答只引用当前已完成回执。"""

    def propose(self, *, view, step: PlanStep) -> CompletedProviderProposal:
        """模拟正式模型提案边界，不写Operation、批准或最终核验状态。"""
        if step.kind == "tool.write":
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref="f1.orders.submit",
                arguments={"order_id": "F1-HTTP-1"},
                rationale="提交本次人工批准的订单",
            )
        else:
            completed = view.execution_context["completed_steps"]
            assert completed[0]["step_key"] == "submit"
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "本次订单的实际回执为 F1-HTTP-RECEIPT。",
                    "criterion_evidence": {"submitted": ["submit"]},
                    "pending_questions": [],
                },
                rationale="只根据当前订单Operation完成结果回答",
            )
        return CompletedProviderProposal(
            response_id=f"f1-http-{step.step_key}", finish_reason="stop", proposal=proposal,
        )


def _resources(db: Session, monkeypatch: pytest.MonkeyPatch):
    """发布合成HTTP工具及原计划；不预置运行节点、批准、派发或回执。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_task_external_write_enabled", True)
    monkeypatch.setattr(settings, "dynamic_task_external_write_tenant_allowlist", "tenant_f1_http")
    monkeypatch.setattr(settings, "dynamic_task_external_write_agent_allowlist", "agent_f1_http")
    for field in ("dynamic_task_alert_signal_backlog_threshold",
                  "dynamic_task_alert_dead_letter_threshold",
                  "dynamic_task_alert_unknown_operation_threshold",
                  "dynamic_task_alert_publication_backlog_threshold",
                  "dynamic_task_alert_waiting_age_seconds"):
        monkeypatch.setattr(settings, field, 100)
    tenant = Tenant(id="tenant_f1_http", name="F1普通DTA隔离租户")
    owner = User(id="owner_f1_http", tenant_id=tenant.id, username="f1-owner",
                 password_hash="synthetic", role="member")
    reviewer = User(id="reviewer_f1_http", tenant_id=tenant.id, username="f1-reviewer",
                    password_hash="synthetic", role="admin")
    agent = AgentProfile(id="agent_f1_http", tenant_id=tenant.id,
                         owner_user_id=owner.id, name="F1普通DTA员工")
    chat = ChatSession(id="chat_f1_http", tenant_id=tenant.id,
                       user_id=owner.id, agent_id=agent.id)
    source = Message(id="source_f1_http", tenant_id=tenant.id, session_id=chat.id,
                     role="user", content="办理订单F1-HTTP-1")
    model = ModelConfig(id="model_f1_http", tenant_id=tenant.id, name="F1控制模型",
                        model="synthetic", api_key_encrypted="synthetic")
    tool = Tool(id="tool_f1_http", tenant_id=tenant.id, name="f1.orders.submit",
                method="POST", url="https://93.184.216.34/orders",
                input_schema={"type": "object", "properties": {"order_id": {"type": "string"}},
                              "required": ["order_id"], "additionalProperties": False},
                output_schema={"type": "object", "properties": {
                    "receipt_id": {"type": "string"}}, "required": ["receipt_id"]})
    publish_tool_contract(tool, ToolReliabilityContract.model_validate({
        "risk_class": "external_write", "side_effect": "external",
        "confirmation_policy": "once", "timeout_policy": "unknown",
        "idempotency": {"mode": "request_key", "remote_scope": "f1-provider"},
        "dynamic_task_enabled": True, "execution_mode": "detached",
        "async_strategy": "provider_task", "status_url": "https://provider.example/{taskId}",
        "max_tracking_seconds": 300, "poll_interval_seconds": 1,
        "model_visibility": {"allowed_paths": ["input.order_id", "output.receipt_id"],
                             "user_display_paths": ["output.receipt_id"]},
    }))
    db.add_all([tenant, owner, reviewer, agent, chat, source, model, tool])
    db.flush()
    ensure_private_resource_binding(db, tenant.id, agent.id, "tool", tool.id, "active")
    snapshot = published_tool_snapshot(tool, agent.id)
    assert snapshot is not None
    plan = NormalizedPlan(
        goal="办理订单F1-HTTP-1",
        success_criteria=(SuccessCriterion(id="submitted", type="assertion",
                                          spec={"required": True}),),
        steps=(PlanStep(step_key="submit", title="提交订单", kind="tool.write",
                        capability_refs=(tool.name,)),
               PlanStep(step_key="answer", title="核验回执", kind="answer",
                        depends_on=("submit",))),
        budget={"max_steps": 4, "max_model_calls": 10, "max_tool_calls": 5},
    )
    instance, _ = SopExecutionStore(db).start_dynamic_instance(
        tenant_id=tenant.id, session_id=chat.id, agent_id=agent.id,
        initiator_user_id=owner.id, plan=plan, source_kind="chat", source_ref=source.id,
        capability_snapshot={"tools": [snapshot.model_dump(mode="json")], "connectors": [],
                             "knowledge": [], "general_skills": [],
                             "model": {"model_config_id": model.id,
                                       "checksum": dynamic_model_snapshot_checksum(model)}},
    )
    db.commit()
    return instance, model, tool


def _waiting_approval(db: Session, monkeypatch: pytest.MonkeyPatch):
    """真实DTA推进到原一次性批准；发送边界在本函数中必须保持零调用。"""
    instance, model, tool = _resources(db, monkeypatch)
    dynamic = DynamicTaskAgent(db, action_proposer=HttpWriteProposer())
    result = dynamic.run_until_blocked_or_complete(
        execution_id=instance.id, model_config=model, worker_id="f1-http-prepare",
        actor_user_id=instance.initiator_user_id,
    )
    assert result.status == "waiting"
    operation = db.exec(select(SopOperation).where(SopOperation.instance_id == instance.id)).one()
    attention = db.exec(select(SopWorkItem).where(
        SopWorkItem.instance_id == instance.id, SopWorkItem.attention_kind == "tool_approval",
    )).one()
    assert operation.status == "prepared"
    assert attention.payload_json["arguments"] == {"order_id": "F1-HTTP-1"}
    assert [row.user_id for row in db.exec(select(SopWorkItemCandidate).where(
        SopWorkItemCandidate.work_item_id == attention.id,
    )).all()] == ["reviewer_f1_http"]
    return instance, model, tool, dynamic, operation, attention


def _decision(db: Session, instance, attention, command="allow_once") -> ExecutionSignal:
    """通过原控制服务CAS办理，取得原数据库追加的唯一审批Signal。"""
    store = SopExecutionStore(db)
    with store.owned(instance, worker_id="f1-http-review"):
        ExecutionControlService(db, store).resolve_attention(
            instance, attention, actor_user_id="reviewer_f1_http",
            command_id="f1-http-decision", command=command, expected_revision=attention.revision,
        )
    db.commit()
    return db.exec(select(ExecutionSignal).where(
        ExecutionSignal.execution_id == instance.id,
        ExecutionSignal.signal_type == "attention_decided",
    )).one()


def test_published_http_external_write_obeys_existing_high_risk_gate(http_write_engine, monkeypatch):
    """启用已发布HTTP外部写才进入实际目录；撤销原高风险开关立即隐藏。"""
    with Session(http_write_engine) as db:
        instance, _model, tool = _resources(db, monkeypatch)
        catalog = DynamicCapabilityCatalog(db)
        assert [s.name for s in catalog.list_tools(instance.tenant_id, instance.agent_id)] == [tool.name]
        monkeypatch.setattr(get_settings(), "dynamic_task_external_write_enabled", False)
        assert catalog.list_tools(instance.tenant_id, instance.agent_id) == []


def test_http_signal_worker_releases_committed_lease_when_waiting(
    http_write_engine, monkeypatch,
):
    """真实worker等待返回后关闭Session，另一Session能续接原实例且外部写不重发。"""
    from app.dynamic_tasks.worker import process_dynamic_task_signal

    calls = []

    def execute(self, tenant_id, tool_call, **kwargs):
        """仅替换远端受理响应；原worker和租约提交必须自行完成。"""
        calls.append(kwargs["remote_idempotency_key"])
        return ToolResult(tool_name=tool_call.name, success=False, disposition="accepted",
                          data={"provider_task_id": "F1-HTTP-WORKER-TASK",
                                "provider_status": "accepted"})

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(http_write_engine) as db:
        instance, _model, _tool, _dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        signal = _decision(db, instance, attention)
        execution_id, signal_id, operation_id = instance.id, signal.id, operation.id
        outcome = process_dynamic_task_signal(db, signal)
        assert outcome is not None and outcome.status == "waiting"
        # 不添加调用方commit，保持真实后台worker关闭Session的路径。
    with Session(http_write_engine) as reader:
        from app.db.models import SopInstance

        waiting = reader.get(SopInstance, execution_id)
        assert waiting.status == "waiting"
        assert waiting.lease_owner is None
        assert reader.get(ExecutionSignal, signal_id).status == "consumed"
        assert reader.get(SopOperation, operation_id).status == "waiting_external"
        with SopExecutionStore(reader).owned(waiting, worker_id="f1-http-legal-next-turn"):
            assert waiting.current_plan_revision_id
        reader.commit()
    assert len(calls) == 1


def test_http_approved_once_waits_and_original_dynamic_consumer_verifies_result(
    http_write_engine, monkeypatch,
):
    """原审批后仅派发一次，受理不成功；可信回执恢复同计划并真实核验成果。"""
    calls = []

    def execute(self, tenant_id, tool_call, **kwargs):
        """只替换HTTP效果边界，读取已经提交的批准与原派发身份。"""
        operation = self.db.exec(select(SopOperation).where(
            SopOperation.instance_id == kwargs["execution_id"],
        )).one()
        assert operation.status == "running"
        assert operation.approved_by_user_id == "reviewer_f1_http"
        assert operation.request_json == tool_call.arguments == {"order_id": "F1-HTTP-1"}
        calls.append(kwargs["remote_idempotency_key"])
        return ToolResult(tool_name=tool_call.name, success=False, disposition="accepted",
                          data={"provider_task_id": "F1-HTTP-TASK", "provider_status": "accepted"})

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(http_write_engine) as db:
        instance, model, _tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        assert calls == []
        plan_id, action_id, operation_id = (instance.current_plan_revision_id,
                                            db.exec(select(ActionProposalRecord).where(
                                                ActionProposalRecord.consumed_operation_id == operation.id,
                                            )).one().id, operation.id)
        signal = _decision(db, instance, attention)
        outcome = dynamic.resume_tool_approval_signal(
            signal_id=signal.id, model_config=model, worker_id="f1-http-dispatch",
            actor_user_id="reviewer_f1_http",
        )
        db.commit()
        assert outcome.status == "waiting"
        assert operation.status == "waiting_external"
        assert signal.status == "consumed"
        assert len(calls) == 1
        assert db.exec(select(ExecutionResult)).all() == []
        repeated = dynamic.resume_tool_approval_signal(
            signal_id=signal.id, model_config=model, worker_id="f1-http-repeat",
            actor_user_id="reviewer_f1_http",
        )
        assert repeated.status == "waiting"
        assert len(calls) == 1
        assert operation.id == operation_id
        assert db.get(ActionProposalRecord, action_id).consumed_operation_id == operation.id
        assert instance.current_plan_revision_id == plan_id
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        ingest_external_task_event(
            db, tenant_id=instance.tenant_id, operation_id=operation.id,
            provider_task_id="F1-HTTP-TASK", provider_event_id="F1-HTTP-COMPLETED",
            provider_status="completed", observation_revision=2,
            payload={"result": {"receipt_id": "F1-HTTP-RECEIPT"}},
            callback_token=external_task_callback_token(operation.remote_idempotency_key),
        )
        db.commit()
        assert apply_pending_external_task_events(db) == 1
        external = db.exec(select(ExecutionSignal).where(
            ExecutionSignal.signal_type == "external_event",
            ExecutionSignal.execution_id == instance.id,
        )).one()
        completed = dynamic.resume_external_task_signal(
            signal_id=external.id, model_config=model, worker_id="f1-http-resume",
        )
        assert completed.status == instance.status == "succeeded"
        assert external.status == "consumed"
        assert len(calls) == 1
        result = db.exec(select(ExecutionResult)).one()
        assert result.verification_json["passed"] is True
        assert result.result_json["criterion_evidence"] == {"submitted": ["submit"]}


def test_http_send_releases_transaction_and_accepts_early_inbox_callback(http_write_engine, monkeypatch):
    """真实ToolExecutor外呼边界不持数据库事务，另一Session的早回调可先可靠落盘。"""
    with Session(http_write_engine) as db:
        instance, model, _tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        tenant_id, operation_id = instance.tenant_id, operation.id
        signal = _decision(db, instance, attention)

        class ProviderClient:
            """只替换最末HTTP传输，保留原鉴权、派发守卫及受理解析。"""

            def __init__(self, **kwargs):
                """接受生产HTTP客户端配置但不访问远端。"""

            def __enter__(self):
                """返回本次受控传输对象。"""
                return self

            def __exit__(self, *args):
                """结束单次受控网络请求。"""

            def request(self, method, url, **kwargs):
                """在HTTP受理返回之前，由独立事务写可信早回调。"""
                assert db.in_transaction() is False
                with Session(http_write_engine) as callback_db:
                    ingest_external_task_event(
                        callback_db, tenant_id=tenant_id, operation_id=operation_id,
                        provider_task_id="F1-EARLY-TASK", provider_event_id="F1-EARLY-COMPLETE",
                        provider_status="completed", observation_revision=2,
                        payload={"result": {"receipt_id": "F1-HTTP-RECEIPT"}},
                        callback_token=kwargs["headers"]["X-Gongge-Xuban-External-Task-Token"],
                    )
                    callback_db.commit()
                return httpx.Response(202, json={"taskId": "F1-EARLY-TASK", "status": "accepted"},
                                      request=httpx.Request(method, url))

        monkeypatch.setattr("app.tools.tool_executor.httpx.Client", ProviderClient)
        outcome = dynamic.resume_tool_approval_signal(
            signal_id=signal.id, model_config=model, worker_id="f1-http-early",
            actor_user_id="reviewer_f1_http",
        )
        db.commit()
        assert outcome.status == "waiting"
        assert operation.status == "waiting_external"
        assert apply_pending_external_task_events(db) == 1
        db.refresh(operation)
        assert operation.status == "succeeded"


@pytest.mark.parametrize("barrier", [
    "deny", "gate", "binding", "approver", "arguments", "operation_arguments",
])
def test_http_approval_denial_or_changed_authority_never_dispatches(
    http_write_engine, monkeypatch, barrier,
):
    """拒绝、撤权及批准材料漂移不能转换为合法外部派发。"""
    calls = []

    def execute(self, *args, **kwargs):
        """记录失效合同是否错误进入任何网络派发边界。"""
        calls.append(1)
        raise AssertionError("失效批准不得发出工具请求")

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(http_write_engine) as db:
        instance, model, tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        signal = _decision(db, instance, attention, "deny" if barrier == "deny" else "allow_once")
        if barrier == "gate":
            monkeypatch.setattr(get_settings(), "dynamic_task_external_write_enabled", False)
        elif barrier == "binding":
            binding = db.exec(select(AgentResourceBinding).where(
                AgentResourceBinding.resource_id == tool.id,
            )).one()
            binding.status = "inactive"
            db.add(binding)
        elif barrier == "approver":
            reviewer = db.get(User, "reviewer_f1_http")
            reviewer.role = "member"
            db.add(reviewer)
        elif barrier == "arguments":
            attention.payload_json = {**attention.payload_json,
                                      "arguments": {"order_id": "UNAPPROVED"}}
            db.add(attention)
        elif barrier == "operation_arguments":
            operation.request_json = {"order_id": "UNAPPROVED"}
            db.add(operation)
        db.commit()
        if barrier == "deny":
            outcome = dynamic.resume_tool_approval_signal(
                signal_id=signal.id, model_config=model, worker_id="f1-http-denied",
                actor_user_id="reviewer_f1_http",
            )
            assert outcome.status == "failed"
            assert operation.status == "cancelled"
        else:
            with pytest.raises((DynamicTaskAgentError, CapabilityAccessDenied)):
                dynamic.resume_tool_approval_signal(
                    signal_id=signal.id, model_config=model, worker_id="f1-http-revoked",
                    actor_user_id="reviewer_f1_http",
                )
        assert calls == []


@pytest.mark.parametrize("outcome", ["timeout", "unknown_without_error"])
def test_http_timeout_is_unknown_and_never_replayed(http_write_engine, monkeypatch, outcome):
    """已发外部写超时只能对账，不能沿本地幂等重试再次提交。"""
    calls = []

    def execute(self, _tenant, tool_call, **kwargs):
        """模拟请求已发出后没有取得响应，不证明远端无效果。"""
        calls.append(kwargs["remote_idempotency_key"])
        return ToolResult(
            tool_name=tool_call.name, success=False,
            disposition="unknown" if outcome == "unknown_without_error" else "completed",
            error=(None if outcome == "unknown_without_error"
                   else ToolError(code="TIMEOUT", message="sent then timed out")),
        )

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(http_write_engine) as db:
        instance, model, _tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        signal = _decision(db, instance, attention)
        outcome = dynamic.resume_tool_approval_signal(
            signal_id=signal.id, model_config=model, worker_id="f1-http-timeout",
            actor_user_id="reviewer_f1_http",
        )
        db.commit()
        assert outcome.status == "waiting"
        assert operation.status == "unknown"
        assert signal.status == "consumed"
        assert len(calls) == 1
        exception = db.exec(select(SopWorkItem).where(
            SopWorkItem.attention_kind == "exception",
        )).one()
        assert "企业微信" not in exception.title
        with pytest.raises(DynamicTaskAgentError):
            dynamic.advance_next_write_step(
                execution_id=instance.id, model_config=model,
                worker_id="f1-http-no-replay", actor_user_id=instance.initiator_user_id,
            )
        assert len(calls) == 1


def test_http_running_dispatch_interruption_requires_reconciliation(http_write_engine, monkeypatch):
    """外呼边界进程中断留下原running身份，过期Signal恢复只对账且不重派。"""
    calls = []

    def interrupted(self, _tenant, tool_call, **kwargs):
        """模拟已进入发送边界后进程中断，不能推断远端无效果。"""
        calls.append(kwargs["remote_idempotency_key"])
        raise KeyboardInterrupt("F1 controlled process interruption")

    monkeypatch.setattr(ToolExecutor, "execute", interrupted)
    with Session(http_write_engine) as db:
        instance, model, _tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        signal = _decision(db, instance, attention)
        with pytest.raises(KeyboardInterrupt):
            dynamic.resume_tool_approval_signal(
                signal_id=signal.id, model_config=model, worker_id="f1-http-crashed",
                actor_user_id="reviewer_f1_http",
            )
        db.commit()
        assert operation.status == "running"
        assert len(calls) == 1
        signal.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(signal)
        db.commit()
        recovered = DynamicTaskAgent(db, action_proposer=HttpWriteProposer()).resume_tool_approval_signal(
            signal_id=signal.id, model_config=model, worker_id="f1-http-recovered",
            actor_user_id="reviewer_f1_http",
        )
        db.commit()
        assert recovered.status == "waiting"
        assert operation.status == "unknown"
        assert signal.status == "consumed"
        assert len(calls) == 1


@pytest.mark.parametrize("barrier", ["archived", "fenced"])
def test_http_running_commit_gap_rechecks_original_owner_and_lifecycle(
    http_write_engine, monkeypatch, barrier,
):
    """批准派发身份提交后由独立Session归档或换fence，旧worker下一外呼为零。"""
    calls = []

    def execute(self, *args, **kwargs):
        """记录提交竞争是否绕过原Runtime的发送守卫。"""
        calls.append(1)
        raise AssertionError("过期worker不可发送")

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(http_write_engine) as db:
        instance, model, _tool, dynamic, operation, attention = _waiting_approval(db, monkeypatch)
        signal = _decision(db, instance, attention)
        original_commit = db.commit
        switched = False

        def commit():
            """精确等待running身份提交，再用第二真实事务修改生命周期或fence。"""
            nonlocal switched
            original_commit()
            with Session(http_write_engine) as other:
                current = other.get(SopOperation, operation.id)
                if not switched and current.status == "running":
                    switched = True
                    if barrier == "archived":
                        other.exec(update(AgentProfile).where(AgentProfile.id == instance.agent_id)
                                   .values(status="archived"))
                    else:
                        other.exec(update(SopInstance).where(SopInstance.id == instance.id)
                                   .values(fencing_token=SopInstance.fencing_token + 1))
                    other.commit()

        monkeypatch.setattr(db, "commit", commit)
        with pytest.raises(ValueError):
            dynamic.resume_tool_approval_signal(
                signal_id=signal.id, model_config=model, worker_id="f1-http-old-worker",
                actor_user_id="reviewer_f1_http",
            )
        assert switched is True
        assert calls == []
