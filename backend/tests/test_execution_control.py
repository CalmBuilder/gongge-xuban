"""
@Time       : 2026/08/04 01:04
@Author     : zhanglp8181
@File       : test_execution_control.py
@CallChain  : pytest → ExecutionControlService → Execution Store/SQLite
@Description: 验证 Attention、命令、signal、Artifact、结果发布和终态闭合契约。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy import update
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db.models import (
    AgentProfile,
    AgentEvent,
    EventOutbox,
    ExecutionCommand,
    ExecutionPlanRevision,
    ExecutionSignal,
    SopInstance,
    SopNodeExecution,
    utc_now,
)
from app.sop_runtime.execution_control import (
    ExecutionControlError,
    ExecutionControlService,
)
from app.sop_runtime.execution_store import (
    SopExecutionConflictError,
    SopExecutionStore,
)


@pytest.fixture
def db() -> Session:
    """创建共享内存 SQLite，使控制服务和 Execution Store 使用真实约束。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _instance(db: Session, *, suffix: str = "one") -> SopInstance:
    """建立可抢占租约的动态 Execution 测试聚合。"""

    db.add(
        AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="Execution control agent",
        )
    )
    instance = SopInstance(
        id=f"sopinst_{suffix}",
        tenant_id="tenant_demo",
        session_id=f"session_{suffix}",
        kind="dynamic_task",
        initiator_user_id="user_owner",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": "test"},
        active_slot_key=f"dynamic:{suffix}",
        current_plan_revision_id="plan_1",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="running",
    )
    db.add(instance)
    db.commit()
    return instance


def test_command_event_signal_and_explicit_outbox_share_transaction(db: Session) -> None:
    """验证命令、事件、signal 及显式外部投递可整体回滚，且不制造伪 event bus。"""

    instance = _instance(db)
    service = ExecutionControlService(db)
    command, created = service.issue_command(
        instance,
        command_id="cmd_1",
        command_type="steer",
        actor_user_id="user_owner",
        expected_execution_revision=instance.revision,
        payload={"instruction": "只读取今年数据"},
    )
    assert created is True
    assert db.exec(select(ExecutionSignal)).one().causation_id == command.id
    event = db.exec(select(AgentEvent)).one()
    assert event.causation_id == command.id
    assert db.exec(select(EventOutbox)).all() == []
    outbox, outbox_created = service.enqueue_event_delivery(
        event,
        destination="webhook",
        destination_ref="configured:webhook:demo",
    )
    replay, replay_created = service.enqueue_event_delivery(
        event,
        destination="webhook",
        destination_ref="configured:webhook:demo",
    )
    assert outbox_created is True
    assert replay_created is False
    assert replay.id == outbox.id
    assert outbox.event_id == event.id

    db.rollback()
    assert db.exec(select(ExecutionCommand)).all() == []
    assert db.exec(select(ExecutionSignal)).all() == []
    assert db.exec(select(AgentEvent)).all() == []
    assert db.exec(select(EventOutbox)).all() == []


def test_add_skill_command_freezes_structured_identity_and_plan_base(db: Session) -> None:
    """验证运行中加 Skill 只接受稳定资源 ID，并冻结当前计划修订供 worker 做 CAS。"""

    instance = _instance(db, suffix="add_skill")
    service = ExecutionControlService(db)
    command, created = service.issue_command(
        instance,
        command_id="add_skill_1",
        command_type="add_skill",
        actor_user_id="user_owner",
        expected_execution_revision=instance.revision,
        payload={"skill_id": "gskill_writing", "ignored": "client-authority"},
    )

    assert created is True
    assert command.payload_json == {"skill_id": "gskill_writing", "trigger": "user"}
    assert command.result_json == {"base_plan_revision_id": "plan_1"}
    signal = db.exec(select(ExecutionSignal)).one()
    assert signal.payload_json["command_type"] == "add_skill"


@pytest.mark.parametrize("skill_id", ["", " contains-space", "x" * 129])
def test_add_skill_command_rejects_invalid_skill_identity(db: Session, skill_id: str) -> None:
    """验证客户端不能用空值、非稳定标识或超长文本替代服务端 Skill ID。"""

    instance = _instance(db, suffix=f"invalid_{len(skill_id)}")
    service = ExecutionControlService(db)
    with pytest.raises(ExecutionControlError) as caught:
        service.issue_command(
            instance,
            command_id=f"add_skill_invalid_{len(skill_id)}",
            command_type="add_skill",
            actor_user_id="user_owner",
            expected_execution_revision=instance.revision,
            payload={"skill_id": skill_id},
        )

    assert caught.value.code == "ADD_SKILL_ID_INVALID"
def test_signal_claim_never_grants_execution_ownership(db: Session) -> None:
    """验证取得 signal lease 后仍必须另取 execution lease 才能推进权威状态。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    signal = service.enqueue_signal(
        instance,
        signal_type="timer",
        causation_type="timer",
        causation_id="timer_1",
    )
    service.claim_signal(signal, worker_id="worker_a")
    db.commit()

    with pytest.raises(SopExecutionConflictError):
        service.consume_signal(instance, signal, worker_id="worker_a")

    signal = db.get(ExecutionSignal, signal.id)
    assert signal is not None
    with store.owned(instance, worker_id="worker_a"):
        assert service.consume_signal(instance, signal, worker_id="worker_a") == "consumed"


def test_only_execution_lease_winner_can_consume_distinct_signals(db: Session) -> None:
    """验证不同 signal 可分别认领，但同一 Execution 同时只有一个 worker 能推进。"""

    instance = _instance(db)
    service_a = ExecutionControlService(db)
    first = service_a.enqueue_signal(
        instance,
        signal_type="timer",
        causation_type="timer",
        causation_id="timer_1",
    )
    second = service_a.enqueue_signal(
        instance,
        signal_type="external_event",
        causation_type="event",
        causation_id="event_1",
    )
    service_a.claim_signal(first, worker_id="worker_a")
    service_a.claim_signal(second, worker_id="worker_b")
    db.commit()

    store_a = SopExecutionStore(db)
    owned_service = ExecutionControlService(db, store_a)
    with store_a.owned(instance, worker_id="worker_a"):
        with pytest.raises(SopExecutionConflictError):
            SopExecutionStore(db).claim(instance, worker_id="worker_b")
        assert owned_service.consume_signal(instance, first, worker_id="worker_a") == "consumed"
    assert second.status == "claimed"


def test_signal_owner_can_renew_without_increasing_attempt_count(db: Session) -> None:
    """长外呼续租不得增加处理次数，且旧 owner/过期 owner 不能延长信号。"""

    instance = _instance(db, suffix="signal_renew")
    service = ExecutionControlService(db)
    signal = service.enqueue_signal(
        instance,
        signal_type="timer",
        causation_type="timer",
        causation_id="timer_renew",
    )
    service.claim_signal(signal, worker_id="worker_a", ttl_seconds=30)
    claimed_expires_at = signal.lease_expires_at
    service.renew_signal(signal, worker_id="worker_a", ttl_seconds=60)

    assert signal.attempt_count == 1
    assert signal.lease_expires_at is not None
    assert claimed_expires_at is not None
    assert signal.lease_expires_at > claimed_expires_at
    with pytest.raises(ExecutionControlError) as caught:
        service.renew_signal(signal, worker_id="worker_b", ttl_seconds=60)
    assert caught.value.code == "SIGNAL_FENCED"


def test_cancellation_discards_ordinary_signal_instead_of_resuming(db: Session) -> None:
    """验证取消已登记时普通唤醒只能收敛为 discarded，不能重新推进业务步骤。"""

    instance = _instance(db)
    instance.cancellation_requested_at = utc_now()
    instance.cancellation_requested_by = "user_owner"
    instance.cancellation_reason = "user_requested"
    instance.cancellation_disposition = "requested"
    db.add(instance)
    db.commit()
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    signal = service.enqueue_signal(
        instance,
        signal_type="timer",
        causation_type="timer",
        causation_id="timer_cancelled",
    )
    service.claim_signal(signal, worker_id="worker_a")
    with store.owned_for_cancellation(instance, worker_id="worker_a"):
        assert service.consume_signal(instance, signal, worker_id="worker_a") == "discarded"


def test_cancellation_requested_execution_cannot_be_claimed_again(db: Session) -> None:
    """取消请求已落库后，新的 worker 不能重新取得执行租约。"""

    instance = _instance(db, suffix="claim_after_cancel")
    instance.cancellation_requested_at = utc_now()
    instance.cancellation_requested_by = "user_owner"
    instance.cancellation_reason = "user_requested"
    instance.cancellation_disposition = "requested"
    db.add(instance)
    db.commit()

    with pytest.raises(SopExecutionConflictError):
        SopExecutionStore(db).claim(instance, worker_id="late-worker")


def test_stale_worker_cannot_offer_attention_or_freeze_result(db: Session) -> None:
    """验证旧 worker 租约失效后对 Attention 和 Result 的迟到写均被 fencing 拒绝。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    lease = store.claim(instance, worker_id="worker_a", ttl_seconds=30)
    store._lease = lease  # noqa: SLF001 - 精确模拟作用域中途被更高 fencing token 抢占
    instance.lease_expires_at = utc_now() - timedelta(seconds=1)
    db.add(instance)
    db.commit()

    with pytest.raises(Exception) as attention_error:
        service.offer_attention(
            instance,
            attention_kind="clarification",
            attention_key="step_1:clarification",
            title="补充范围",
            payload={"question": "请选择范围"},
            allowed_commands=["answer"],
            candidate_user_ids=["user_owner"],
        )
    assert getattr(attention_error.value, "code", "") == "SOP_EXECUTION_FENCED"

    with pytest.raises((SopExecutionConflictError, ExecutionControlError)):
        service.freeze_result(
            instance,
            result={"summary": "late"},
            verification={"passed": True},
        )


def test_terminal_guard_requires_closed_children_result_and_publication(db: Session) -> None:
    """逐项证明 succeeded 不得越过 required Step、结果和 required publication。"""

    instance = _instance(db)
    step = SopNodeExecution(
        tenant_id=instance.tenant_id,
        instance_id=instance.id,
        node_id="step_1",
        step_key="step_1",
        plan_revision_id=instance.current_plan_revision_id,
        step_kind="read",
        title="读取数据",
        required=True,
        attempt=1,
        status="running",
    )
    db.add(step)
    db.add(
        ExecutionPlanRevision(
            id="plan_1",
            tenant_id=instance.tenant_id,
            execution_id=instance.id,
            revision_number=1,
            reason="initial",
            status="active",
            plan_json={"steps": [{"step_key": "step_1", "required": True}]},
            checksum="a" * 64,
            capability_snapshot_json={"capabilities": []},
            capability_checksum="b" * 64,
        )
    )
    db.commit()
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)

    with store.owned(instance, worker_id="worker_a"):
        with pytest.raises(ExecutionControlError) as missing_step:
            service.assert_terminal_closure(instance, "succeeded")
        assert "required_steps" in str(missing_step.value)
    db.rollback()

    step.status = "succeeded"
    db.add(step)
    db.commit()
    with store.owned(instance, worker_id="worker_a"):
        with pytest.raises(ExecutionControlError) as missing_result:
            service.assert_terminal_closure(instance, "succeeded")
        assert "verified_result" in str(missing_result.value)
    db.rollback()

    with store.owned(instance, worker_id="worker_a"):
        _, publication, _ = service.freeze_result(
            instance,
            result={"summary": "done"},
            verification={"passed": True, "criteria": ["read_complete"]},
        )
    db.commit()
    with store.owned(instance, worker_id="worker_a"):
        with pytest.raises(ExecutionControlError) as pending_publication:
            service.assert_terminal_closure(instance, "succeeded")
        assert "required_publications" in str(pending_publication.value)
    db.rollback()

    with store.owned(instance, worker_id="worker_a"):
        service.settle_application_publication(
            instance,
            publication,
            message_id="msg_final",
        )
    db.commit()
    with store.owned(instance, worker_id="worker_a"):
        service.assert_terminal_closure(instance, "succeeded")


def test_result_and_publication_are_idempotent_but_immutable(db: Session) -> None:
    """验证相同结果重放复用，结果或 publication receipt 漂移则稳定拒绝。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    with store.owned(instance, worker_id="worker_a"):
        result, publication, created = service.freeze_result(
            instance,
            result={"summary": "done"},
            verification={"passed": True},
        )
    db.commit()
    with store.owned(instance, worker_id="worker_a"):
        replay, replay_publication, replay_created = service.freeze_result(
            instance,
            result={"summary": "done"},
            verification={"passed": True},
        )
        service.settle_application_publication(
            instance,
            publication,
            message_id="msg_final",
        )
    assert replay.id == result.id
    assert replay_publication.id == publication.id
    assert replay_created is False

    with store.owned(instance, worker_id="worker_a"):
        with pytest.raises(ExecutionControlError) as conflict:
            service.freeze_result(
                instance,
                result={"summary": "changed"},
                verification={"passed": True},
            )
        assert conflict.value.code == "RESULT_ALREADY_FROZEN"
        with pytest.raises(ExecutionControlError) as receipt_conflict:
            service.settle_application_publication(
                instance,
                publication,
                message_id="msg_other",
            )
        assert receipt_conflict.value.code == "PUBLICATION_RECEIPT_CONFLICT"


def test_outbox_crash_reclaims_same_publication_key_and_late_ack_is_fenced(db: Session) -> None:
    """验证投递者崩溃后只重领同一 outbox，旧 worker 不能迟到确认或制造第二条通知。"""

    instance = _instance(db)
    service = ExecutionControlService(db)
    service.issue_command(
        instance,
        command_id="steer_outbox",
        command_type="steer",
        actor_user_id="user_owner",
        expected_execution_revision=instance.revision,
        payload={"instruction": "keep idempotent"},
    )
    event = db.exec(select(AgentEvent)).one()
    outbox, _ = service.enqueue_event_delivery(
        event,
        destination="external_thread",
        destination_ref="thread:contract-review",
    )
    publication_key = outbox.publication_key
    service.claim_outbox(outbox, worker_id="outbox_a", ttl_seconds=30)
    db.commit()

    db.exec(
        update(EventOutbox)
        .where(EventOutbox.id == outbox.id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    db.commit()
    db.refresh(outbox)
    service.claim_outbox(outbox, worker_id="outbox_b", ttl_seconds=30)
    assert outbox.attempt_count == 2
    assert outbox.publication_key == publication_key
    assert len(db.exec(select(EventOutbox)).all()) == 1

    with pytest.raises(ExecutionControlError) as late_ack:
        service.acknowledge_outbox(outbox, worker_id="outbox_a")
    assert late_ack.value.code == "OUTBOX_FENCED"
    service.acknowledge_outbox(outbox, worker_id="outbox_b")
    assert outbox.status == "delivered"


def test_failed_verification_freezes_rejected_result_and_blocks_success(db: Session) -> None:
    """验证“生成了结果”不等于通过成功标准，rejected 结果不能写 succeeded。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    with store.owned(instance, worker_id="worker_a"):
        result, publication, _ = service.freeze_result(
            instance,
            result={"summary": "incomplete"},
            verification={"passed": False, "missing": ["source_b"]},
        )
        service.settle_application_publication(
            instance,
            publication,
            message_id="msg_rejected",
        )
    assert result.status == "rejected"
    db.commit()

    with store.owned(instance, worker_id="worker_a"):
        with pytest.raises(ExecutionControlError) as blocked:
            service.assert_terminal_closure(instance, "succeeded")
        assert "verified_result" in str(blocked.value)


def test_dynamic_terminal_guard_rejects_required_plan_step_never_created(db: Session) -> None:
    """验证动态计划声明的 required Step 即使从未建行，也不能被顶层结果绕过。"""

    instance = _instance(db, suffix="missing_step")
    revision = ExecutionPlanRevision(
        id="plan_1",
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        revision_number=1,
        reason="initial",
        status="active",
        plan_json={
            "steps": [
                {"step_key": "required_missing", "required": True},
            ],
            "expected_artifacts": [
                {"artifact_key": "required_brief", "required": True},
            ],
        },
        checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        capability_checksum="b" * 64,
    )
    db.add(revision)
    db.flush()
    store = SopExecutionStore(db)
    control = ExecutionControlService(db, store)
    with store.owned(instance, worker_id="worker_missing_step"):
        _result, publication, _ = control.freeze_result(
            instance,
            result={"summary": "不能跳步"},
            verification={"passed": True},
        )
        control.settle_application_publication(
            instance,
            publication,
            message_id="message_missing_step",
        )
        with pytest.raises(ExecutionControlError) as blocked:
            control.assert_terminal_closure(instance, "succeeded")

    assert "missing_required_steps" in str(blocked.value)
    assert "missing_required_artifacts" in str(blocked.value)


def test_signal_and_outbox_exhaust_attempt_budget_into_dead_letter(db: Session) -> None:
    """验证 signal 与 outbox 达到重试预算后停止热循环并保留错误证据。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    signal = service.enqueue_signal(
        instance,
        signal_type="external_event",
        causation_type="test",
        causation_id="dead_signal",
        max_attempts=1,
    )
    service.claim_signal(signal, worker_id="signal_worker")
    with store.owned(instance, worker_id="execution_worker"):
        assert service.retry_signal(
            instance,
            signal,
            worker_id="signal_worker",
            error={"code": "PERMANENT"},
        ) == "dead_letter"

    db.refresh(instance)
    service.issue_command(
        instance,
        command_id="outbox_dead",
        command_type="steer",
        actor_user_id="user_owner",
        expected_execution_revision=instance.revision,
        payload={"instruction": "用于触发命令 outbox 的有效约束"},
    )
    event = db.exec(select(AgentEvent)).one()
    outbox, _ = service.enqueue_event_delivery(
        event,
        destination="webhook",
        destination_ref="configured:webhook:dead-letter",
    )
    outbox.max_attempts = 1
    db.add(outbox)
    db.flush()
    service.claim_outbox(outbox, worker_id="outbox_worker")
    assert service.retry_outbox(
        outbox,
        worker_id="outbox_worker",
        error={"code": "DESTINATION_REJECTED"},
    ) == "dead_letter"
    assert outbox.last_error_json["code"] == "DESTINATION_REJECTED"


def test_claimed_signal_can_be_settled_after_execution_already_terminal(db: Session) -> None:
    """恢复逻辑先终结 Execution 时，迟到 signal 仍可凭自身租约收敛且不能被重放。"""

    instance = _instance(db, suffix="terminal_signal")
    service = ExecutionControlService(db)
    signal = service.enqueue_signal(
        instance,
        signal_type="external_event",
        causation_type="test",
        causation_id="terminal_signal",
    )
    service.claim_signal(signal, worker_id="signal_worker")
    instance.status = "failed"
    instance.active_slot_key = None
    instance.completed_at = utc_now()
    db.add(instance)
    db.flush()

    assert service.settle_claimed_signal_for_terminal_execution(
        instance,
        signal,
        worker_id="signal_worker",
        error={"code": "DYNAMIC_RUNTIME_BUDGET_EXCEEDED"},
    ) == "discarded"
    assert signal.lease_owner is None
    assert signal.consumed_at is not None
    assert signal.last_error_json["code"] == "DYNAMIC_RUNTIME_BUDGET_EXCEEDED"


def test_cancellation_closes_active_attention_and_discards_all_resume_signals(db: Session) -> None:
    """验证取消在终态前同时处置 Attention 和普通唤醒，不留下可恢复孤儿。"""

    instance = _instance(db)
    store = SopExecutionStore(db)
    service = ExecutionControlService(db, store)
    with store.owned(instance, worker_id="planner"):
        attention, _ = service.offer_attention(
            instance,
            attention_kind="clarification",
            attention_key="step_cancel:question",
            title="等待输入",
            payload={"question": "continue?"},
            allowed_commands=["answer"],
            candidate_user_ids=["user_owner"],
        )
        db.commit()
    db.refresh(instance)
    command, _ = service.issue_command(
        instance,
        command_id="cancel_with_attention",
        command_type="cancel",
        actor_user_id="user_owner",
        expected_execution_revision=instance.revision,
        payload={"reason": "stop_waiting"},
    )
    with store.owned(instance, worker_id="cancel_worker"):
        assert service.apply_cancel_command(
            instance,
            command,
            worker_id="cancel_worker",
        ) is True
    assert instance.status == "cancelled"
    assert attention.status == "cancelled"
    assert attention.resolution_json["command"] == "cancel_execution"
    assert {
        signal.status for signal in db.exec(select(ExecutionSignal)).all()
    } == {"discarded"}
