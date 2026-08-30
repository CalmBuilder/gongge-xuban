"""
@Time       : 2026/08/11 00:20
@Author     : zhanglp8181
@File       : test_dynamic_external_write.py
@CallChain  : pytest → DynamicTaskAgent → tool Approval/Operation/企业微信 adapter stub
@Description: 验证外部写和 destructive-gray 的独立审批、隔离派发、零重发与 unknown 对账闭环。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.connectors.service import ConnectionError
from app.connectors.wecom import WeComCallResult
from app.db.models import (
    ExecutionResult,
    ExecutionSignal,
    AgentProfile,
    ModelConfig,
    SopInstance,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.dynamic_tasks.standing_approvals import StandingApprovalMatch
from app.dynamic_tasks.worker import due_dynamic_task_signals
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
from app.tools.tool_schema import ToolError, ToolResult


class _WriteCatalog:
    """为写路径返回冻结模型和唯一非发起审批人。"""

    def __init__(self, model: ModelConfig) -> None:
        """保存测试模型。"""

        self.model = model

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """返回同租户已预检模型。"""

        assert tenant_id == self.model.tenant_id
        assert model_config_id == self.model.id
        return self.model

    def write_approver_ids(self, tenant_id: str, *, exclude_user_id: str) -> list[str]:
        """返回与发起人分离的固定审批人。"""

        assert tenant_id == "tenant_write"
        assert exclude_user_id == "requester"
        return ["approver"]


class _WriteProposer:
    """生成精确正文的单步 connector write 提案。"""

    def __init__(self, capability_name: str) -> None:
        """冻结本次能力引用。"""

        self.capability_name = capability_name

    def propose(self, *, view, step) -> CompletedProviderProposal:
        """断言当前步骤后返回完整写提案。"""

        assert view.execution_context["execution_id"]
        if step.kind == "answer":
            return CompletedProviderProposal(
                response_id="write-result-1",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.ANSWER,
                    arguments={
                        "markdown": (
                            "企业微信消息已发送；正常回执 delivery_status 为 sent，"
                            "人工对账回执为 manually_confirmed。"
                        ),
                        "criterion_evidence": {"message_sent": ["send_message"]},
                        "pending_questions": [],
                    },
                    rationale="依据已持久化的发送回执形成最终结果",
                ),
            )
        assert step.kind == "tool.write"
        return CompletedProviderProposal(
            response_id="write-proposal-1",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=self.capability_name,
                arguments={"content": "审批后发送的精确消息"},
                rationale="向当前企业微信线程发送已核对内容",
            ),
        )


class _DestructiveCatalog:
    """为 destructive 单测提供已预检模型和实时快照再授权。"""

    def __init__(self, model: ModelConfig) -> None:
        """保存本次隔离运行使用的模型并初始化再授权计数。"""

        self.model = model
        self.reauthorize_calls = 0

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """返回同租户已预检模型，模拟生产目录的模型门禁。"""

        assert tenant_id == self.model.tenant_id
        assert model_config_id == self.model.id
        return self.model

    def reauthorize_tool(self, snapshot, **_kwargs) -> object:
        """记录 dispatch 前的实时再授权并保留当前快照。"""

        self.reauthorize_calls += 1
        return snapshot


class _DestructiveProposer:
    """为 destructive 操作和最终答复返回固定、可校验的 provider 提案。"""

    def __init__(self, capability_name: str, target: str, target_checksum: str) -> None:
        """冻结能力名、目标及其校验指纹，避免测试提案漂移。"""

        self.capability_name = capability_name
        self.target = target
        self.target_checksum = target_checksum
        self.steps: list[str] = []

    def propose(self, *, view, step) -> CompletedProviderProposal:
        """按步骤返回唯一 destructive 调用或带真实证据引用的最终答复。"""

        del view
        self.steps.append(step.step_key)
        if step.kind == "tool.destructive":
            return CompletedProviderProposal(
                response_id="destructive-proposal-1",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL,
                    capability_ref=self.capability_name,
                    arguments={
                        "target": self.target,
                        "target_checksum": self.target_checksum,
                    },
                    rationale="在隔离 disposable fixture 上执行已确认动作",
                ),
            )
        assert step.kind == "answer"
        return CompletedProviderProposal(
            response_id=f"destructive-answer-{len(self.steps)}",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "隔离 disposable fixture 已按批准目标完成处理。",
                    "criterion_evidence": {"effect_applied": ["destroy_fixture"]},
                    "pending_questions": [],
                },
                rationale="依据 destructive Operation 的真实回执形成结果",
            ),
        )


class _DestructiveExecutor:
    """记录 destructive adapter 的单次调用、幂等键和固定目标。"""

    def __init__(self, *, timeout: bool = False) -> None:
        """配置是否返回效果不确定的超时回执。"""

        self.timeout = timeout
        self.calls: list[dict[str, object]] = []

    def execute(self, tenant_id, tool_call, **kwargs):
        """返回成功或 timeout，并保留所有到达 provider 边界的关键事实。"""

        self.calls.append(
            {
                "tenant_id": tenant_id,
                "name": tool_call.name,
                "arguments": dict(tool_call.arguments),
                "remote_idempotency_key": kwargs.get("remote_idempotency_key"),
            }
        )
        if self.timeout:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                error=ToolError(code="TIMEOUT", message="disposable provider timeout"),
            )
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={
                "effect_status": "deleted",
                "target": tool_call.arguments["target"],
                "target_checksum": tool_call.arguments["target_checksum"],
                "destructive_provider": "disposable",
            },
        )


class _WriteConnectionService:
    """记录派发次数并返回可配置的企业微信效果。"""

    def __init__(
        self,
        result: WeComCallResult,
        *,
        validation_error: str | None = None,
    ) -> None:
        """保存 adapter 结果和调用计数。"""

        self.result = result
        self.validation_error = validation_error
        self.validate_calls = 0
        self.send_calls = 0

    def validate_wecom_message_dispatch(self, **kwargs) -> dict[str, object]:
        """返回不含凭据的实时授权证据。"""

        self.validate_calls += 1
        assert kwargs["actor_user_id"] == "approver"
        if self.validation_error is not None:
            error = self.validation_error
            self.validation_error = None
            raise ConnectionError(error)
        return {
            "permission_code": "external_connection.write",
            "profile_id": kwargs["profile_id"],
            "profile_revision": kwargs["expected_profile_revision"],
            "secret_revision": kwargs["expected_secret_revision"],
            "binding_revision": kwargs["expected_binding_revision"],
        }

    def current_wecom_message_dispatch_evidence(self, **kwargs) -> dict[str, object]:
        """模拟修订漂移后的当前授权事实，不执行 adapter。"""

        assert kwargs["actor_user_id"] == "approver"
        return {
            "permission_code": "external_connection.write",
            "profile_id": kwargs["profile_id"],
            "profile_revision": 8,
            "secret_revision": 4,
            "binding_revision": 6,
        }

    def send_wecom_approved_message(self, **kwargs) -> WeComCallResult:
        """记录唯一外呼并返回预设结果。"""

        self.send_calls += 1
        assert kwargs["content"] == "审批后发送的精确消息"
        return self.result


class _CrashAfterDispatchService(_WriteConnectionService):
    """模拟 adapter 已被调用但进程未能保存回执的崩溃窗口。"""

    def send_wecom_approved_message(self, **kwargs) -> WeComCallResult:
        """记录首次外呼后抛出非业务异常，模拟进程突然退出。"""

        super().send_wecom_approved_message(**kwargs)
        raise RuntimeError("simulated process crash after adapter call")


def test_standing_rule_dispatches_without_attention_and_persists_source(monkeypatch) -> None:
    """精确长期规则命中时无需一次性审批，但仍持久化规则来源、审计和恢复信号。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    instance.source_kind = "schedule"
    instance.source_ref = "scheduled-run-write"
    db.add(instance)
    db.commit()
    match = StandingApprovalMatch(
        rule=SimpleNamespace(id="standing-rule-1", revision=3),
        authorization_actor_user_id="approver",
        evidence={
            "authorization_source": "standing_rule",
            "standing_rule_id": "standing-rule-1",
            "standing_rule_revision": 3,
        },
    )
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.match_standing_approval_rule",
        lambda *_args, **_kwargs: match,
    )
    service = _WriteConnectionService(
        WeComCallResult(True, {"message_id": "standing-msg-1", "invalid_user_count": 0})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )

    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="standing-dispatch",
        actor_user_id="requester",
    )

    operation = db.exec(select(SopOperation)).one()
    signal = db.exec(
        select(ExecutionSignal).where(ExecutionSignal.signal_type == "operation_settled")
    ).one()
    assert attention is None
    assert service.validate_calls == service.send_calls == 1
    assert operation.status == "succeeded"
    assert operation.authorization_source_type == "standing_rule"
    assert operation.authorization_source_ref == "standing-rule-1:3"
    assert operation.approval_work_item_id is None
    assert signal.status == "consumed"
    assert db.exec(select(SopWorkItem)).all() == []
    db.close()


def test_standing_rule_revoked_after_capability_snapshot_falls_back_before_dispatch(
    monkeypatch,
) -> None:
    """能力冻结后规则撤销必须转一次性审批，且在新授权前保持零外呼。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    instance.source_kind = "schedule"
    instance.source_ref = "scheduled-run-revoked-standing"
    db.add(instance)
    db.commit()
    matches: list[dict[str, object]] = []

    def revoked_match(*_args, **kwargs):
        """模拟运行时重读发现规则已撤销，并保留调用边界供断言。"""

        matches.append(dict(kwargs))
        return None

    monkeypatch.setattr(
        "app.dynamic_tasks.agent.match_standing_approval_rule",
        revoked_match,
    )
    service = _WriteConnectionService(
        WeComCallResult(True, {"message_id": "must-not-send"})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )

    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="standing-revoked-before-dispatch",
        actor_user_id="requester",
    )

    operation = db.exec(select(SopOperation)).one()
    assert len(matches) == 1
    assert matches[0]["instance"].source_ref == instance.source_ref
    assert attention is not None
    assert attention.attention_kind == "tool_approval"
    assert operation.status == "prepared"
    assert attention.payload_json["operation_id"] == operation.id
    assert operation.authorization_source_type != "standing_rule"
    assert service.validate_calls == service.send_calls == 0
    db.close()


def test_standing_rule_crash_recovery_parks_unknown_without_resend(monkeypatch) -> None:
    """长期授权外呼后崩溃由持久 signal 转人工核对，恢复过程绝不再次调用 adapter。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    instance.source_kind = "schedule"
    instance.source_ref = "scheduled-run-crash"
    db.add(instance)
    db.commit()
    match = StandingApprovalMatch(
        rule=SimpleNamespace(id="standing-rule-crash", revision=1),
        authorization_actor_user_id="approver",
        evidence={
            "authorization_source": "standing_rule",
            "standing_rule_id": "standing-rule-crash",
            "standing_rule_revision": 1,
        },
    )
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.match_standing_approval_rule",
        lambda *_args, **_kwargs: match,
    )
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.has_governance_permission",
        lambda *_args, **_kwargs: True,
    )
    service = _CrashAfterDispatchService(
        WeComCallResult(True, {"message_id": "remote-maybe-applied"})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        agent.advance_next_write_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="standing-crash",
            actor_user_id="requester",
        )

    operation = db.exec(select(SopOperation)).one()
    signal = db.exec(
        select(ExecutionSignal).where(ExecutionSignal.signal_type == "operation_settled")
    ).one()
    assert operation.status == "running"
    assert signal.status == "claimed"
    assert service.send_calls == 1
    signal.lease_expires_at = utc_now() - timedelta(seconds=1)
    db.add(signal)
    db.commit()
    assert [item.id for item in due_dynamic_task_signals(db)] == [signal.id]

    outcome = agent.resume_standing_dispatch_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="standing-recovery",
    )

    db.refresh(operation)
    db.refresh(signal)
    exception = db.exec(
        select(SopWorkItem).where(SopWorkItem.attention_kind == "exception")
    ).one()
    assert outcome.status == "waiting"
    assert operation.status == "unknown"
    assert signal.status == "consumed"
    assert exception.payload_json["error_code"] == "DYNAMIC_STANDING_DISPATCH_INTERRUPTED"
    assert service.send_calls == 1
    db.close()


def test_allow_once_dispatches_exactly_once_and_replay_does_not_send(
    monkeypatch,
) -> None:
    """确认前零调用，批准后一次；同一 signal 重放只复用权威事实。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    service = _WriteConnectionService(
        WeComCallResult(
            True,
            {"message_id": "msg-001", "invalid_user_count": 0},
        )
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )

    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-write",
        actor_user_id="requester",
    )
    operation = db.exec(select(SopOperation)).one()

    assert service.send_calls == 0
    assert operation.status == "prepared"
    assert attention.attention_kind == "tool_approval"
    assert attention.exclude_initiator is True
    assert attention.payload_json["content"] == "审批后发送的精确消息"

    signal = _resolve(db, instance, attention, command="allow_once")
    first = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="dispatch-write",
        actor_user_id="approver",
    )
    replay = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="replay-write",
        actor_user_id="approver",
    )

    db.refresh(operation)
    assert first.status == replay.status == "succeeded"
    assert service.validate_calls == 1
    assert service.send_calls == 1
    assert operation.status == "succeeded"
    assert operation.effect_state == "complete"
    assert operation.approval_work_item_id == attention.id
    assert operation.approved_by_user_id == "approver"
    assert operation.dispatched_at is not None
    assert operation.authorization_evidence_json["permission_code"] == (
        "external_connection.write"
    )
    db.close()


def test_external_write_gray_revocation_blocks_prepared_resume(monkeypatch) -> None:
    """外部写已生成待审批后撤回灰度时，恢复也必须拒绝且不触达连接器。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    service = _WriteConnectionService(
        WeComCallResult(True, {"message_id": "must-not-send"})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-revocation",
        actor_user_id="requester",
    )
    signal = _resolve(db, instance, attention, command="allow_once")
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_high_risk_external_write_allows=lambda _tenant, _agent: False,
        ),
    )

    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_EXTERNAL_WRITE_DISABLED"):
        agent.resume_tool_approval_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="resume-after-revocation",
            actor_user_id="approver",
        )

    operation = db.exec(select(SopOperation)).one()
    assert operation.status == "prepared"
    assert service.validate_calls == service.send_calls == 0
    db.close()


def test_legacy_write_only_plan_fails_closed_after_effect_without_orphan(
    monkeypatch,
) -> None:
    """历史无 answer 计划完成外部效果后必须失败闭合，不得遗留无唤醒 running 实例。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch, include_answer=False)
    service = _WriteConnectionService(
        WeComCallResult(True, {"message_id": "msg-legacy", "invalid_user_count": 0})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-legacy-write",
        actor_user_id="requester",
    )
    signal = _resolve(db, instance, attention, command="allow_once")

    outcome = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="dispatch-legacy-write",
        actor_user_id="approver",
    )

    operation = db.exec(select(SopOperation)).one()
    result = db.exec(select(ExecutionResult)).one()
    db.refresh(instance)
    db.refresh(signal)
    assert outcome.status == "failed"
    assert service.send_calls == 1
    assert operation.status == "succeeded"
    assert operation.effect_state == "complete"
    assert instance.status == "failed"
    assert instance.effect_state == "complete"
    assert instance.active_slot_key is None
    assert instance.terminal_reason_json["code"] == "DYNAMIC_PLAN_TERMINAL_STEP_MISSING"
    assert signal.status == "consumed"
    assert result.status == "verified"
    assert result.result_json["status"] == "failed"
    db.close()


def test_deny_cancels_prepared_operation_without_adapter_call(monkeypatch) -> None:
    """拒绝一次性批准必须形成确定失败终态且远端调用仍为零。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    service = _WriteConnectionService(WeComCallResult(True, {"message_id": "unexpected"}))
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-deny",
        actor_user_id="requester",
    )
    signal = _resolve(db, instance, attention, command="deny")

    outcome = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="deny-write",
        actor_user_id="approver",
    )

    operation = db.exec(select(SopOperation)).one()
    db.refresh(instance)
    assert outcome.status == "failed"
    assert service.validate_calls == service.send_calls == 0
    assert operation.status == "cancelled"
    assert operation.cancellation_disposition == "not_dispatched"
    assert instance.status == "failed"
    db.close()


def test_unknown_effect_parks_exception_and_manual_reconcile_never_resends(
    monkeypatch,
) -> None:
    """发送超时进入 unknown，人工确认效果后继续且不产生第二次外呼。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.has_governance_permission",
        lambda _db, *, user_id, **_kwargs: user_id == "approver",
    )
    service = _WriteConnectionService(
        WeComCallResult(False, {}, error_code="WECOM_DELIVERY_UNKNOWN")
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    approval = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-unknown",
        actor_user_id="requester",
    )
    approval_signal = _resolve(db, instance, approval, command="allow_once")

    blocked = agent.resume_tool_approval_signal(
        signal_id=approval_signal.id,
        model_config=model,
        worker_id="dispatch-unknown",
        actor_user_id="approver",
    )

    operation = db.exec(select(SopOperation)).one()
    exception = db.exec(
        select(SopWorkItem).where(SopWorkItem.attention_kind == "exception")
    ).one()
    assert blocked.status == "waiting"
    assert operation.status == "unknown"
    assert service.send_calls == 1
    reconcile_signal = _resolve(
        db,
        instance,
        exception,
        command="confirm_applied",
        comment="已在企业微信客户端核对到唯一消息",
    )

    outcome = agent.resume_write_reconciliation_signal(
        signal_id=reconcile_signal.id,
        model_config=model,
        worker_id="reconcile-write",
        actor_user_id="approver",
    )

    db.refresh(operation)
    assert outcome.status == "succeeded"
    assert operation.status == "succeeded"
    assert operation.effect_state == "complete"
    assert operation.cancellation_disposition == "reconciled"
    assert service.send_calls == 1
    db.close()


def test_revision_drift_requires_fresh_approval_before_any_dispatch(monkeypatch) -> None:
    """批准后配置修订变化必须产生新审批，旧批准不得用于外呼。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    service = _WriteConnectionService(
        WeComCallResult(True, {"message_id": "msg-refreshed"}),
        validation_error="CONNECTION_APPROVAL_REVISION_CHANGED",
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    original = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-revision-drift",
        actor_user_id="requester",
    )
    original_signal = _resolve(db, instance, original, command="allow_once")

    waiting = agent.resume_tool_approval_signal(
        signal_id=original_signal.id,
        model_config=model,
        worker_id="detect-revision-drift",
        actor_user_id="approver",
    )

    approvals = db.exec(
        select(SopWorkItem)
        .where(SopWorkItem.attention_kind == "tool_approval")
        .order_by(SopWorkItem.created_at)
    ).all()
    assert waiting.status == "waiting"
    assert len(approvals) == 2
    replacement = approvals[-1]
    assert replacement.node_execution_id is None
    assert replacement.payload_json["previous_attention_id"] == original.id
    assert replacement.payload_json["profile_revision"] == 8
    assert service.send_calls == 0

    replacement_signal = _resolve(db, instance, replacement, command="allow_once")
    outcome = agent.resume_tool_approval_signal(
        signal_id=replacement_signal.id,
        model_config=model,
        worker_id="dispatch-refreshed",
        actor_user_id="approver",
    )

    operation = db.exec(select(SopOperation)).one()
    assert outcome.status == "succeeded"
    assert operation.approval_work_item_id == replacement.id
    assert service.send_calls == 1
    db.close()


def test_crash_after_dispatch_recovers_to_unknown_without_resend(monkeypatch) -> None:
    """派发窗口崩溃后由原 signal 租约恢复为 unknown，禁止第二次 adapter 调用。"""

    db, instance, model, snapshot = _write_runtime(monkeypatch)
    monkeypatch.setattr(
        "app.dynamic_tasks.agent.has_governance_permission",
        lambda _db, *, user_id, **_kwargs: user_id == "approver",
    )
    service = _CrashAfterDispatchService(
        WeComCallResult(True, {"message_id": "receipt-lost"})
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_WriteCatalog(model),
        action_proposer=_WriteProposer(snapshot.name),
        connection_service=service,
    )
    attention = agent.advance_next_write_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="prepare-crash-window",
        actor_user_id="requester",
    )
    signal = _resolve(db, instance, attention, command="allow_once")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        agent.resume_tool_approval_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="crashing-dispatch",
            actor_user_id="approver",
        )

    operation = db.exec(select(SopOperation)).one()
    db.refresh(signal)
    assert operation.status == "running"
    assert signal.status == "claimed"
    assert service.send_calls == 1

    signal.lease_expires_at = utc_now() - timedelta(seconds=1)
    db.add(signal)
    db.commit()

    recovered = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="recover-crash-window",
        actor_user_id="approver",
    )

    db.refresh(operation)
    db.refresh(signal)
    exception = db.exec(
        select(SopWorkItem).where(SopWorkItem.attention_kind == "exception")
    ).one()
    assert recovered.status == "waiting"
    assert operation.status == "unknown"
    assert signal.status == "consumed"
    assert exception.payload_json["error_code"] == "DYNAMIC_WRITE_DISPATCH_INTERRUPTED"
    assert service.send_calls == 1
    db.close()


def test_destructive_default_off_has_zero_operation_or_provider_calls(monkeypatch) -> None:
    """默认 destructive 关闭时在动作入场处稳定拒绝，不能创建 Operation 或调用 provider。"""

    db, instance, model, snapshot, settings = _destructive_runtime(monkeypatch, allowed=False)
    executor = _DestructiveExecutor()
    agent = DynamicTaskAgent(
        db,
        catalog=_DestructiveCatalog(model),
        action_proposer=_DestructiveProposer(
            snapshot.name,
            snapshot.contract["canonical_target"],
            snapshot.contract["target_checksum"],
        ),
        tool_executor=executor,
    )

    assert settings.dynamic_task_high_risk_destructive_allows("tenant_destructive", "agent-destructive") is False
    with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_DESTRUCTIVE_DISABLED"):
        agent.advance_next_local_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="destructive-default-off",
            actor_user_id="requester",
            step_kind="tool.destructive",
        )

    assert executor.calls == []
    assert db.exec(select(SopOperation)).all() == []


def test_destructive_gray_requires_exact_target_and_dispatches_once_with_remote_idempotency(
    monkeypatch,
) -> None:
    """destructive-gray 命中后仍需逐次批准，且 provider 只收到固定目标和稳定幂等键。"""

    db, instance, model, snapshot, _settings = _destructive_runtime(monkeypatch, allowed=True)
    executor = _DestructiveExecutor()
    catalog = _DestructiveCatalog(model)
    agent = DynamicTaskAgent(
        db,
        catalog=catalog,
        action_proposer=_DestructiveProposer(
            snapshot.name,
            snapshot.contract["canonical_target"],
            snapshot.contract["target_checksum"],
        ),
        tool_executor=executor,
    )

    attention = agent.advance_next_local_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="destructive-prepare",
        actor_user_id="requester",
        step_kind="tool.destructive",
    )
    operation = db.exec(select(SopOperation)).one()
    assert attention.attention_kind == "tool_approval"
    assert operation.effect_kind == "destructive"
    assert operation.status == "prepared"
    assert operation.idempotency_scope == "instance"
    assert operation.remote_idempotency_key
    assert attention.payload_json["canonical_target"] == snapshot.contract["canonical_target"]
    assert executor.calls == []

    signal = _resolve(db, instance, attention, command="allow_once")
    first = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="destructive-dispatch",
        actor_user_id="approver",
    )
    replay = agent.resume_tool_approval_signal(
        signal_id=signal.id,
        model_config=model,
        worker_id="destructive-replay",
        actor_user_id="approver",
    )

    db.refresh(operation)
    assert first.status == replay.status == "succeeded"
    assert operation.status == "succeeded"
    assert operation.effect_state == "complete"
    assert len(executor.calls) == 1
    assert executor.calls[0]["remote_idempotency_key"] == operation.remote_idempotency_key
    assert executor.calls[0]["arguments"] == {
        "target": snapshot.contract["canonical_target"],
        "target_checksum": snapshot.contract["target_checksum"],
    }
    assert catalog.reauthorize_calls == 1


def test_destructive_timeout_parks_unknown_and_reconcile_never_resends(monkeypatch) -> None:
    """destructive provider 超时必须进入人工对账，确认后继续且绝不二次派发。"""

    db, instance, model, snapshot, _settings = _destructive_runtime(monkeypatch, allowed=True)
    executor = _DestructiveExecutor(timeout=True)
    proposer = _DestructiveProposer(
        snapshot.name,
        snapshot.contract["canonical_target"],
        snapshot.contract["target_checksum"],
    )
    agent = DynamicTaskAgent(
        db,
        catalog=_DestructiveCatalog(model),
        action_proposer=proposer,
        tool_executor=executor,
    )
    approval = agent.advance_next_local_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="destructive-unknown-prepare",
        actor_user_id="requester",
        step_kind="tool.destructive",
    )
    approval_signal = _resolve(db, instance, approval, command="allow_once")
    blocked = agent.resume_tool_approval_signal(
        signal_id=approval_signal.id,
        model_config=model,
        worker_id="destructive-unknown-dispatch",
        actor_user_id="approver",
    )

    operation = db.exec(select(SopOperation)).one()
    exception = db.exec(
        select(SopWorkItem).where(SopWorkItem.attention_kind == "exception")
    ).one()
    assert blocked.status == "waiting"
    assert operation.status == "unknown"
    assert exception.payload_json["effect_kind"] == "destructive"
    assert len(executor.calls) == 1

    reconcile_signal = _resolve(
        db,
        instance,
        exception,
        command="confirm_applied",
        comment="隔离 provider 状态已核对",
    )
    outcome = agent.resume_write_reconciliation_signal(
        signal_id=reconcile_signal.id,
        model_config=model,
        worker_id="destructive-reconcile",
        actor_user_id="approver",
    )

    db.refresh(operation)
    assert outcome.status == "succeeded"
    assert operation.status == "succeeded"
    assert operation.effect_state == "complete"
    assert operation.cancellation_disposition == "reconciled"
    assert len(executor.calls) == 1


def _destructive_runtime(
    monkeypatch,
    *,
    allowed: bool,
) -> tuple[Session, SopInstance, ModelConfig, CapabilitySnapshot, SimpleNamespace]:
    """创建带独立 destructive gate、逐次审批人和隔离目标的内存动态运行时。"""

    settings = SimpleNamespace(
        dynamic_task_explore_enabled=False,
        dynamic_task_managed_workspace_enabled=True,
        general_skill_agent_proposal_enabled=True,
        dynamic_task_high_risk_destructive_allows=lambda _tenant, _agent: allowed,
    )
    monkeypatch.setattr("app.dynamic_tasks.agent.get_settings", lambda: settings)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    model_facts = {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "tool_calling": True,
        "structured_output": True,
    }
    model = ModelConfig(
        id="model-destructive",
        tenant_id="tenant_destructive",
        name="Destructive isolation model",
        api_key_encrypted="encrypted",
        model="model-demo",
        capability_snapshot_json=model_facts,
        capability_checksum=capability_checksum(model_facts),
        preflight_status="ready",
    )
    target = "disposable://fixture/object-1"
    target_checksum = capability_checksum(target)
    snapshot_payload = {
        "capability_type": "tool",
        "capability_id": "tool-disposable-delete",
        "tenant_id": "tenant_destructive",
        "name": "disposable.fixture_delete",
        "contract": {
            "risk_class": "destructive",
            "side_effect": "external",
            "confirmation_policy": "always",
            "timeout_policy": "unknown",
            "dynamic_task_enabled": False,
            "destructive_dynamic_task_enabled": True,
            "idempotency": {
                "mode": "request_key",
                "argument": None,
                "remote_scope": "disposable-fixture",
            },
            "reconcile": {
                "supported": True,
                "tool_name": "disposable.fixture_status",
                "reference_source": "output.operation_id",
                "terminal_status_mapping": {
                    "deleted": "complete",
                    "already_deleted": "complete",
                    "pending": "unknown",
                },
            },
            "canonical_target": target,
            "target_checksum": target_checksum,
            "destructive_provider": "disposable",
        },
        "model_view": {
            "name": "disposable.fixture_delete",
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "target_checksum": {"type": "string"},
                },
                "required": ["target", "target_checksum"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
        "user_view": {"target": "隔离 disposable fixture"},
        "audit_view": {"provider": "disposable", "fixture": True},
    }
    snapshot = CapabilitySnapshot(
        **snapshot_payload,
        agent_id="agent-destructive",
        checksum=capability_checksum(snapshot_payload),
    )
    plan = NormalizedPlan(
        goal="在隔离 provider 上处理 fixture",
        success_criteria=(
            SuccessCriterion(id="effect_applied", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="destroy_fixture",
                title="处理隔离 fixture",
                kind="tool.destructive",
                capability_refs=(snapshot.name,),
            ),
            PlanStep(
                step_key="answer_result",
                title="确认隔离处理结果",
                kind="answer",
                depends_on=("destroy_fixture",),
            ),
        ),
        budget={"max_steps": 3, "max_tool_calls": 1, "max_model_calls": 4},
    )
    db.add_all(
        [
            Tenant(id="tenant_destructive", name="Destructive tenant"),
            User(
                id="requester",
                tenant_id="tenant_destructive",
                username="requester",
                role="member",
                password_hash="x",
                membership_status="active",
            ),
            User(
                id="approver",
                tenant_id="tenant_destructive",
                username="approver",
                role="admin",
                password_hash="x",
                membership_status="active",
            ),
            AgentProfile(
                id="agent-destructive",
                tenant_id="tenant_destructive",
                name="Destructive isolation Agent",
                status="active",
                owner_user_id="requester",
            ),
            model,
        ]
    )
    db.flush()
    instance = SopExecutionStore(db).start_dynamic_instance(
        tenant_id="tenant_destructive",
        session_id="session-destructive",
        agent_id="agent-destructive",
        initiator_user_id="requester",
        plan=plan,
        capability_snapshot={
            "tools": [snapshot.model_dump(mode="json")],
            "connectors": [],
            "general_skills": [],
            "knowledge": [],
            "model": {
                "model_config_id": model.id,
                "capabilities": model_facts,
                "checksum": model.capability_checksum,
            },
        },
    )[0]
    instance.context_json = {
        "dynamic_budget_usage": {"model_calls": 0, "tool_calls": 0},
    }
    db.add(instance)
    db.commit()
    return db, instance, model, snapshot, settings


def _write_runtime(
    monkeypatch,
    *,
    include_answer: bool = True,
) -> tuple[Session, SopInstance, ModelConfig, CapabilitySnapshot]:
    """创建含冻结线程写能力、发起人与独立审批人的内存 Runtime。"""

    monkeypatch.setattr(
        "app.dynamic_tasks.agent.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_external_write_enabled=True,
            dynamic_task_high_risk_external_write_allows=lambda _tenant, _agent: True,
        ),
    )
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    model_facts = {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "tool_calling": True,
        "structured_output": True,
    }
    model = ModelConfig(
        id="model-write",
        tenant_id="tenant_write",
        name="Write model",
        api_key_encrypted="encrypted",
        model="model-demo",
        capability_snapshot_json=model_facts,
        capability_checksum=capability_checksum(model_facts),
        preflight_status="ready",
    )
    db.add_all(
        [
            Tenant(id="tenant_write", name="Write tenant"),
            User(
                id="requester",
                tenant_id="tenant_write",
                username="requester",
                password_hash="x",
            ),
            User(
                id="approver",
                tenant_id="tenant_write",
                username="approver",
                password_hash="x",
            ),
            AgentProfile(
                id="agent-write",
                tenant_id="tenant_write",
                name="写入测试 Agent",
                is_overall=False,
            ),
            model,
        ]
    )
    db.flush()
    snapshot = _write_snapshot()
    plan_steps = [
        PlanStep(
            step_key="send_message",
            title="发送企业微信消息",
            kind="tool.write",
            capability_refs=(snapshot.name,),
        )
    ]
    if include_answer:
        plan_steps.append(
            PlanStep(
                step_key="answer_result",
                title="确认发送结果",
                kind="answer",
                depends_on=("send_message",),
            )
        )
    plan = NormalizedPlan(
        goal="向当前企业微信会话发送核验结果",
        success_criteria=(
            SuccessCriterion(
                id="message_sent",
                type="assertion",
                spec={"required": True},
            ),
        ),
        steps=tuple(plan_steps),
        budget={"max_steps": 2, "max_tool_calls": 2, "max_model_calls": 4},
    )
    instance = SopExecutionStore(db).start_dynamic_instance(
        tenant_id="tenant_write",
        session_id="session-write",
        agent_id="agent-write",
        initiator_user_id="requester",
        plan=plan,
        capability_snapshot={
            "tools": [],
            "connectors": [snapshot.model_dump(mode="json")],
            "knowledge": [],
            "general_skills": [],
            "model": {
                "model_config_id": model.id,
                "capabilities": model_facts,
                "checksum": model.capability_checksum,
            },
        },
        source_kind="chat",
        source_ref="message-write",
    )[0]
    instance.context_json = {
        "dynamic_budget_usage": {"model_calls": 0, "tool_calls": 0}
    }
    db.add(instance)
    db.commit()
    return db, instance, model, snapshot


def _write_snapshot() -> CapabilitySnapshot:
    """构造不包含外部 UserID 或凭据的固定线程写快照。"""

    name = "wecom.message_send@profile-write"
    payload = {
        "capability_type": "connector",
        "capability_id": "profile-write",
        "tenant_id": "tenant_write",
        "name": name,
        "contract": {
            "risk_class": "external_write",
            "side_effect": "external",
            "confirmation_policy": "once",
            "canonical_target": "wecom_thread:thread-write",
            "target_checksum": capability_checksum("thread-write"),
            "profile_revision": 7,
            "secret_revision": 3,
            "binding_revision": 5,
            "required_result_evidence_paths": ["delivery_status"],
        },
        "model_view": {
            "name": name,
            "input_schema": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "delivery_status": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["delivery_status", "message_id"],
                "additionalProperties": False,
            },
        },
        "user_view": {"target": "当前企业微信会话"},
        "audit_view": {
            "binding_id": "binding-write",
            "thread_binding_id": "thread-write",
        },
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent-write",
        checksum=capability_checksum(payload),
    )


def _resolve(
    db: Session,
    instance: SopInstance,
    attention: SopWorkItem,
    *,
    command: str,
    comment: str | None = None,
) -> ExecutionSignal:
    """以真实 Attention CAS 服务生成持久恢复 signal。"""

    control = ExecutionControlService(db)
    with control.store.owned(instance, worker_id=f"resolve-{command}"):
        control.resolve_attention(
            instance,
            attention,
            actor_user_id="approver",
            command_id=f"command-{attention.id}-{command}",
            command=command,
            expected_revision=attention.revision,
            comment=comment,
        )
    db.commit()
    signals = db.exec(
        select(ExecutionSignal).where(
            ExecutionSignal.causation_type == "attention_resolution"
        )
    ).all()
    return next(
        signal
        for signal in signals
        if signal.payload_json.get("attention_id") == attention.id
    )
