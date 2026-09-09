"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_pi_distillation_audit.py
@CallChain  : pytest → 聊天恢复/Execution Store/并行恢复扫描器 → 临时 SQLite
@Description: 从查询成本、状态契约及事务恢复视角验证 pi 蒸馏候选，不改生产机制。
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import _existing_turn_replay
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentEvent,
    AgentProfile,
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    DynamicReadDispatchResult,
    ExecutionSignal,
    Message,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    SopOperationEffect,
    utc_now,
)
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.dynamic_tasks.worker import reconcile_parallel_read_batches
from app.session.session_schema import ChatTurnRequest
from app.sop_runtime.contracts import (
    NONTERMINAL_OPERATION_STATUSES,
    OperationStatus,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.state_machine import transition_operation
from app.tools import ToolExecutor


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[Engine]:
    """仅创建 pytest 临时目录中的 SQLite，不连接应用配置或真实租户库。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'pi-audit.db'}")
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _probe_history(engine: Engine, history_count: int) -> dict[str, int]:
    """记录真实 ORM 实例化行数，并验证目标 Turn 正文、终态及租户隔离。"""

    session_id = f"session_{history_count}"
    message_id = f"message_{history_count}"
    now = utc_now()
    with Session(engine) as db:
        db.add(Message(
            id=message_id, tenant_id="tenant_audit", session_id=session_id,
            role="user", content="审计恢复", metadata_json={"client_turn_id": message_id},
        ))
        entries = [
            ("user_message_received", {"client_turn_id": message_id, "message_id": message_id}),
            ("stream_delta", {"turn_id": message_id, "content": "目标正文"}),
            ("complete", {"turn_id": message_id}),
        ]
        for index in range(history_count):
            entries.append(("stream_delta", {"turn_id": f"old_{index}", "content": "历史"}))
        for index, (kind, payload) in enumerate(entries):
            db.add(AgentEvent(
                tenant_id="tenant_audit", session_id=session_id, event_type=kind,
                payload_json=payload, created_at=now + timedelta(microseconds=index),
            ))
        db.add(AgentEvent(
            tenant_id="tenant_other", session_id=session_id, event_type="stream_delta",
            payload_json={"turn_id": message_id, "content": "不得跨租户读取"},
        ))
        db.commit()

    loaded: list[str] = []

    def count_loaded(row: AgentEvent, _context: object) -> None:
        """只记录本次查询从数据库新加载的事件，不用 SQL 字符串推测行数。"""

        loaded.append(row.id)

    event.listen(AgentEvent, "load", count_loaded)
    try:
        with Session(engine) as db:
            replay = _existing_turn_replay(db, ChatTurnRequest(
                tenant_id="tenant_audit", session_id=session_id,
                client_turn_id=message_id, message="审计恢复", user_id="user_audit",
            ))
            assert replay is not None
            assert replay[0] == message_id and replay[3] is True
            assert len(replay[1]) == 3
            replay_count = len(loaded)
        loaded.clear()
        with Session(engine) as db:
            # 仅测试持久化查询；不初始化无关的模型、工具和业务服务。
            loop = object.__new__(AgentLoop)
            loop.db = db
            assert loop._persisted_turn_delta_parts(
                "tenant_audit", session_id, message_id,
            ) == ["目标正文"]
            partial_count = len(loaded)
    finally:
        event.remove(AgentEvent, "load", count_loaded)
    counts = {"history": history_count, "replay_rows": replay_count, "partial_rows": partial_count}
    print(f"F01 query probe: {counts}")
    return counts


@pytest.mark.parametrize("history_count", [0, 100, 1000, 10_000, 100_000])
def test_pi_b_x1_history_probe_preserves_target_turn(
    audit_engine: Engine,
    history_count: int,
) -> None:
    """在冻结的五档历史量下实测查询加载量，正确性断言独立于优化目标。"""

    counts = _probe_history(audit_engine, history_count)
    assert counts["replay_rows"] == 3
    assert counts["partial_rows"] == 1


def test_pi_b_x1_materialization_does_not_grow_with_history(audit_engine: Engine) -> None:
    """目标 Turn 正文查询只实例化目标事件，不把无关历史加载进进程。"""

    baseline = _probe_history(audit_engine, 0)
    long_history = _probe_history(audit_engine, 2000)
    assert long_history["replay_rows"] == baseline["replay_rows"]
    assert long_history["partial_rows"] == baseline["partial_rows"]


def _seed_operation(
    db: Session,
    status: str,
    *,
    effect_kind: str = "read",
    attempt_statuses: tuple[str, ...] | None = None,
    with_attempt: bool = True,
) -> tuple[SopInstance, SopOperation]:
    """构造数据库可接受的 Operation 状态，并按参数保留完整 Attempt 历史。"""

    if status == "retry_wait" and attempt_statuses is None and with_attempt:
        attempt_statuses = (OperationStatus.RETRY_WAIT.value,)
    if not with_attempt:
        attempt_statuses = ()
    if status == "retry_wait":
        transition_operation(OperationStatus.RUNNING, OperationStatus.RETRY_WAIT, actual_revision=0)
    store = SopExecutionStore(db)
    instance, _ = store.start_instance(
        tenant_id="tenant_audit", session_id="session_audit", skill_id="skill_audit",
        skill_version_id="version_audit", skill_version="1", definition_checksum="a" * 64,
        start_node_id="read_a",
    )
    with store.owned(instance, worker_id="audit-seed"):
        node = store.enter_node(instance, "read_a", input_snapshot={})
    operation = SopOperation(
        tenant_id=instance.tenant_id, instance_id=instance.id, node_execution_id=node.id,
        operation_name="audit.read", idempotency_key="audit.read", logical_action_id="audit.read",
        request_fingerprint="b" * 64, effect_kind=effect_kind, status=status,
    )
    db.add(operation)
    db.flush()
    for attempt_number, attempt_status in enumerate(attempt_statuses or (), start=1):
        started_at = utc_now() if attempt_status == OperationStatus.RUNNING.value else None
        db.add(SopOperationAttempt(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            operation_id=operation.id,
            node_execution_id=node.id,
            attempt_number=attempt_number,
            status=attempt_status,
            retry_at=utc_now() if attempt_status == OperationStatus.RETRY_WAIT.value else None,
            started_at=started_at,
        ))
    db.commit()
    return instance, operation


@pytest.mark.parametrize("status", [
    "prepared", "running", "unknown",
    "retry_wait",
])
def test_declared_nonterminal_operation_blocks_terminal(audit_engine: Engine, status: str) -> None:
    """对照既有活动状态与 retry_wait，检查真实终态查询而非枚举文本。"""

    with Session(audit_engine) as db:
        instance, _ = _seed_operation(db, status)
        blockers = ExecutionControlService(db).terminal_blockers(instance, "cancelled")
        assert "active_operations" in blockers, (status, blockers)


def test_nonterminal_operation_contract_covers_declared_enum(audit_engine: Engine) -> None:
    """活动集合必须由 OperationStatus 完整推导，新增 retry_wait 不能静默漏检。"""

    assert NONTERMINAL_OPERATION_STATUSES == frozenset(
        {
            OperationStatus.PREPARED.value,
            OperationStatus.RUNNING.value,
            OperationStatus.RETRY_WAIT.value,
            OperationStatus.UNKNOWN.value,
        }
    )


@pytest.mark.parametrize("status", [
    "prepared", "running",
    "retry_wait",
])
def test_cancellation_settles_pending_read(audit_engine: Engine, status: str) -> None:
    """运行取消事务并刷新数据库，要求父实例取消时无未结算的待重试纯读。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(db, status)
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel"):
            store.request_cancellation(instance, actor_user_id="user_audit", reason="测试取消")
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        print(f"F03 cancellation: initial={status}, parent={instance.status}, child={operation.status}")
        assert instance.status == "cancelled"
        assert operation.status == "cancelled"
        if status == "retry_wait":
            attempts = db.exec(
                select(SopOperationAttempt).where(
                    SopOperationAttempt.operation_id == operation.id,
                )
            ).all()
            assert [attempt.status for attempt in attempts] == ["cancelled"]
            assert attempts[0].retry_at is None
            assert operation.cancellation_disposition == "cancelled_no_effect"
        if status != "prepared":
            node = db.get(SopNodeExecution, operation.node_execution_id)
            assert node is not None and node.status == "cancelled"


@pytest.mark.parametrize("effect_kind", ["external_write", "destructive"])
def test_retry_wait_external_effect_remains_blocked_without_frozen_evidence(
    audit_engine: Engine,
    effect_kind: str,
) -> None:
    """非 read 且缺少冻结风险证据时保留未决，不能伪造未派发或已取消。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(
            db,
            "retry_wait",
            effect_kind=effect_kind,
        )
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-external"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试取消待重试外部动作",
            )
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        assert settled is False
        assert instance.status == "running"
        assert instance.cancellation_disposition == "awaiting_reconciliation"
        assert operation.status == "retry_wait"
        assert operation.effect_state == "none"
        assert operation.cancellation_disposition == "none"
        attempt = db.exec(
            select(SopOperationAttempt).where(
                SopOperationAttempt.operation_id == operation.id,
            )
        ).one()
        assert attempt.status == "retry_wait"
        assert attempt.retry_at is not None
        assert db.exec(
            select(SopOperationEffect).where(
                SopOperationEffect.operation_id == operation.id,
            )
        ).all() == []


def test_retry_wait_read_without_attempt_is_never_dispatched(
    audit_engine: Engine,
) -> None:
    """没有 Attempt 且没有任何派发字段的历史兼容纯读可写入 not_dispatched。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(
            db,
            "retry_wait",
            with_attempt=False,
        )
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-never"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试未派发纯读",
            )
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        assert settled is True
        assert instance.status == "cancelled"
        assert operation.status == "cancelled"
        assert operation.cancellation_disposition == "not_dispatched"


def test_retry_wait_read_with_valid_snapshot_is_safe_read(
    audit_engine: Engine,
) -> None:
    """存在完整冻结 read 契约时仍按 SAFE_READ 取消，不依赖当前工具目录。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(db, "retry_wait")
        snapshot = {"contract": {"risk_class": "read", "side_effect": "none"}}
        operation.capability_snapshot_json = snapshot
        operation.capability_checksum = capability_checksum(snapshot)
        db.add(operation)
        db.commit()
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-snapshot"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试冻结纯读",
            )
        db.commit()
        db.refresh(operation)
        assert settled is True
        assert operation.status == "cancelled"
        assert operation.cancellation_disposition == "cancelled_no_effect"


@pytest.mark.parametrize("snapshot", [[], ["invalid"], "invalid"])
def test_retry_wait_read_with_invalid_snapshot_shape_remains_blocked(
    audit_engine: Engine,
    snapshot: object,
) -> None:
    """快照存在但不是非空对象时必须阻塞，不能按历史无快照纯读兼容。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(db, "retry_wait")
        operation.capability_snapshot_json = snapshot  # type: ignore[assignment]
        operation.capability_checksum = "invalid-snapshot-shape"
        db.add(operation)
        db.commit()
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-invalid-shape"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试非法快照形态",
            )
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        assert settled is False
        assert instance.status == "running"
        assert operation.status == "retry_wait"


def test_retry_wait_read_with_effect_evidence_remains_blocked(
    audit_engine: Engine,
) -> None:
    """read Effect 即使 state 为 none，也不能携带外部引用、补偿关系或证据。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(db, "retry_wait")
        db.add(
            SopOperationEffect(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                operation_id=operation.id,
                logical_action_id=operation.logical_action_id,
                sequence=1,
                event_type="synthetic_read_effect",
                effect_state="none",
                external_reference="remote-effect-id",
                evidence_json={"observed": True},
                compensation_operation_id="unexpected-compensation",
            )
        )
        db.commit()
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-effect-evidence"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试读效果证据",
            )
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        assert settled is False
        assert instance.status == "running"
        assert operation.status == "retry_wait"


def test_retry_wait_cancellation_keeps_all_active_attempts_and_history(
    audit_engine: Engine,
) -> None:
    """取消 retry_wait 只结算全部活动 Attempt，历史成功/失败/复用记录不可改写。"""

    statuses = ("prepared", "running", "retry_wait", "succeeded", "failed", "cancelled", "reused")
    with Session(audit_engine) as db:
        instance, operation = _seed_operation(
            db,
            "retry_wait",
            attempt_statuses=statuses,
        )
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-attempts"):
            assert store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试全部 Attempt",
            ) is True
        db.commit()
        attempts = db.exec(
            select(SopOperationAttempt)
            .where(SopOperationAttempt.operation_id == operation.id)
            .order_by(SopOperationAttempt.attempt_number)
        ).all()
        assert [attempt.status for attempt in attempts] == [
            "cancelled", "cancelled", "cancelled", "succeeded", "failed", "cancelled", "reused",
        ]
        assert all(attempt.retry_at is None for attempt in attempts[:3])


def test_retry_wait_cancellation_discards_active_parallel_items(
    audit_engine: Engine,
) -> None:
    """安全 retry_wait 取消在同一事务 CAS 丢弃活动并行项，阻断迟到 inbox 回执。"""

    instance_id, _batch_id = _seed_inbox(audit_engine)
    with Session(audit_engine) as db:
        operations = db.exec(
            select(SopOperation).where(SopOperation.instance_id == instance_id)
        ).all()
        for operation in operations:
            operation.status = "retry_wait"
            db.add(operation)
            attempt = db.exec(
                select(SopOperationAttempt).where(
                    SopOperationAttempt.operation_id == operation.id,
                )
            ).one()
            attempt.status = "retry_wait"
            attempt.retry_at = utc_now()
            db.add(attempt)
        db.commit()
        instance = db.get(SopInstance, instance_id)
        assert instance is not None
        store = SopExecutionStore(db)
        with store.owned_for_cancellation(instance, worker_id="audit-cancel-parallel-retry"):
            assert store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试安全 retry_wait 并行取消",
            ) is True
        db.commit()
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(SopOperationAttempt)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(SopNodeExecution)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(DynamicReadDispatchItem)).all()} == {
            "discarded"
        }


def test_retry_wait_with_damaged_snapshot_remains_blocked(
    audit_engine: Engine,
) -> None:
    """冻结快照 checksum 损坏时不进入任何自动取消安全分支。"""

    with Session(audit_engine) as db:
        instance, operation = _seed_operation(db, "retry_wait")
        operation.capability_snapshot_json = {
            "contract": {"risk_class": "read", "side_effect": "none"},
        }
        operation.capability_checksum = "not-the-real-checksum"
        db.add(operation)
        db.commit()
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="audit-cancel-damaged"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_audit",
                reason="测试损坏快照",
            )
        db.commit()
        db.refresh(instance)
        db.refresh(operation)
        assert settled is False
        assert instance.status == "running"
        assert operation.status == "retry_wait"
        assert operation.capability_checksum == "not-the-real-checksum"


def _seed_inbox(engine: Engine) -> tuple[str, str]:
    """保存两项已派发纯读，回执按 B、A 到达，结算仍须遵守计划 A、B。"""

    with Session(engine, expire_on_commit=False) as db:
        db.add(AgentProfile(id="agent_audit", tenant_id="tenant_audit", name="测试员工"))
        store = SopExecutionStore(db)
        instance, _ = store.start_dynamic_instance(
            tenant_id="tenant_audit", session_id="session_audit", agent_id="agent_audit",
            initiator_user_id="user_audit",
            plan=NormalizedPlan(
                goal="验证回执恢复", success_criteria=(
                    SuccessCriterion(id="done", type="assertion", spec={"required": True}),
                ),
                steps=tuple(PlanStep(
                    step_key=key, title=key, kind="tool.read", capability_refs=(key,),
                ) for key in ("read_a", "read_b")),
                budget={"max_steps": 4, "max_tool_calls": 4, "max_model_calls": 4},
            ),
            capability_snapshot={"tools": [{"name": key} for key in ("read_a", "read_b")]},
        )
        batch = DynamicReadDispatchBatch(
            tenant_id=instance.tenant_id, execution_id=instance.id,
            plan_revision_id=str(instance.current_plan_revision_id), wave_checksum="d" * 64,
            ordered_step_keys_json=["read_a", "read_b"], status="dispatched", parallelism=2,
            deadline_at=utc_now() + timedelta(minutes=5),
        )
        db.add(batch)
        with store.owned(instance, worker_id="audit-seed"):
            for position, key in enumerate(batch.ordered_step_keys_json):
                node = store.enter_node(
                    instance, key, step_key=key, plan_revision_id=instance.current_plan_revision_id,
                    step_kind="tool.read", title=key,
                )
                operation = SopOperation(
                    tenant_id=instance.tenant_id, instance_id=instance.id, node_execution_id=node.id,
                    operation_name=key, idempotency_key=key, logical_action_id=key,
                    effect_kind="read", status="running", request_fingerprint="b" * 64,
                    capability_checksum="c" * 64, revision=1,
                )
                db.add(operation)
                db.flush()
                db.add(SopOperationAttempt(
                    tenant_id=instance.tenant_id, instance_id=instance.id, operation_id=operation.id,
                    node_execution_id=node.id, attempt_number=1, dispatch_token=f"token_{key}",
                    status="running", started_at=utc_now(),
                ))
                db.add(DynamicReadDispatchItem(
                    tenant_id=instance.tenant_id, batch_id=batch.id, execution_id=instance.id,
                    plan_revision_id=str(instance.current_plan_revision_id), position=position,
                    step_key=key, node_execution_id=node.id, operation_id=operation.id,
                    operation_revision_at_start=1, dispatch_token=f"token_{key}",
                    capability_checksum="c" * 64, request_fingerprint="b" * 64, status="dispatched",
                ))
        for key in ("read_b", "read_a"):
            db.add(DynamicReadDispatchResult(
                tenant_id=instance.tenant_id, dispatch_token=f"token_{key}", status="succeeded",
                result_json={"value": key}, started_at=utc_now(), finished_at=utc_now(),
            ))
        db.commit()
        return instance.id, batch.id


def test_inbox_recovery_survives_rollback_reopen_without_duplicate_attempts(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在恢复提交前注入故障，回滚并重开连接；验证原 token、顺序及单次后续唤醒。"""

    instance_id, batch_id = _seed_inbox(audit_engine)

    def forbid_dispatch(*_args: object, **_kwargs: object) -> None:
        """已有 inbox 的恢复不得再次进入工具派发入口，更不允许测试触达外部工具。"""

        raise AssertionError("AUDIT_UNEXPECTED_TOOL_DISPATCH")

    monkeypatch.setattr(ToolExecutor, "execute", forbid_dispatch)

    def fail_before_signal(*_args: object, **_kwargs: object) -> None:
        """模拟 Operation/Node 已 flush、恢复信号尚未提交时的执行中断。"""

        raise RuntimeError("AUDIT_BEFORE_RECOVERY_COMMIT")

    with Session(audit_engine) as db, monkeypatch.context() as fault:
        fault.setattr(ExecutionControlService, "enqueue_signal", fail_before_signal)
        with pytest.raises(RuntimeError, match="AUDIT_BEFORE_RECOVERY_COMMIT"):
            reconcile_parallel_read_batches(db)
        db.rollback()
    audit_engine.dispose()

    with Session(audit_engine) as db:
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"running"}
        assert db.exec(select(ExecutionSignal)).all() == []
        original_attempts = [
            (row.id, row.dispatch_token, row.attempt_number)
            for row in db.exec(select(SopOperationAttempt).order_by(SopOperationAttempt.id)).all()
        ]
        order: list[str] = []
        original_complete = SopExecutionStore.complete_node

        def record_complete(
            store: SopExecutionStore, instance: SopInstance, node: SopNodeExecution,
            **kwargs: object,
        ) -> object:
            """记录真实结算顺序，并继续调用原实现而非替代状态机。"""

            order.append(node.step_key)
            return original_complete(store, instance, node, **kwargs)

        monkeypatch.setattr(SopExecutionStore, "complete_node", record_complete)
        assert reconcile_parallel_read_batches(db) == 1
        assert order == ["read_a", "read_b"]
        assert reconcile_parallel_read_batches(db) == 0
        assert [
            (row.id, row.dispatch_token, row.attempt_number)
            for row in db.exec(select(SopOperationAttempt).order_by(SopOperationAttempt.id)).all()
        ] == original_attempts
        assert {row.status for row in db.exec(select(SopOperationAttempt)).all()} == {"succeeded"}
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"succeeded"}
        assert [row.output_json for row in db.exec(
            select(SopNodeExecution).order_by(SopNodeExecution.step_key),
        ).all()] == [{"value": "read_a"}, {"value": "read_b"}]
        assert db.get(DynamicReadDispatchBatch, batch_id).status == "succeeded"
        # 纯读波次结束不等于业务成功；实例等待原 signal 链路继续。
        assert db.get(SopInstance, instance_id).status == "running"
        assert len(db.exec(select(DynamicReadDispatchResult)).all()) == 2
        assert len(db.exec(select(ExecutionSignal)).all()) == 1


def test_parallel_parent_terminal_recovery_uses_store_transitions(
    audit_engine: Engine,
) -> None:
    """父实例已终态时，并行恢复仍经 Store 结算 Operation/Attempt/Node。"""

    instance_id, batch_id = _seed_inbox(audit_engine)
    with Session(audit_engine) as db:
        instance = db.get(SopInstance, instance_id)
        assert instance is not None
        instance.status = "cancelled"
        instance.active_slot_key = None
        db.add(instance)
        db.commit()

    with Session(audit_engine) as db:
        assert reconcile_parallel_read_batches(db) == 1
        db.commit()
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(SopOperationAttempt)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(SopNodeExecution)).all()} == {"cancelled"}
        assert {row.status for row in db.exec(select(DynamicReadDispatchItem)).all()} == {"discarded"}
        assert db.get(DynamicReadDispatchBatch, batch_id).status == "cancelled"
        assert reconcile_parallel_read_batches(db) == 0


def test_parallel_recovery_claim_is_revision_fenced(
    audit_engine: Engine,
) -> None:
    """两个恢复会话竞争同一波次时只有一个能取得 revision CAS 写入权。"""

    _instance_id, batch_id = _seed_inbox(audit_engine)
    with Session(audit_engine) as first, Session(audit_engine) as second:
        first_batch = first.get(DynamicReadDispatchBatch, batch_id)
        second_batch = second.get(DynamicReadDispatchBatch, batch_id)
        assert first_batch is not None and second_batch is not None
        assert SopExecutionStore(first).claim_parallel_read_batch(first_batch) is True
        first.commit()
        assert SopExecutionStore(second).claim_parallel_read_batch(second_batch) is False
        second.rollback()

    with Session(audit_engine) as db:
        batch = db.get(DynamicReadDispatchBatch, batch_id)
        assert batch is not None and batch.status == "settling" and batch.revision == 1


def test_parallel_recovery_final_cas_rolls_back_child_facts(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """batch 最终 CAS 失败时，子 Operation/Node/Attempt 不能被 worker 单独提交。"""

    instance_id, batch_id = _seed_inbox(audit_engine)
    def reject_final_cas(
        store: SopExecutionStore,
        batch: DynamicReadDispatchBatch,
        *,
        status: str,
    ) -> bool:
        """模拟另一个权威写入抢先改变 batch，验证恢复保存点整体回滚。"""

        del store, batch, status
        return False

    monkeypatch.setattr(SopExecutionStore, "finalize_parallel_read_batch", reject_final_cas)
    with Session(audit_engine) as db:
        assert reconcile_parallel_read_batches(db) == 0

    with Session(audit_engine) as db:
        assert db.get(SopInstance, instance_id).status == "running"
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"running"}
        assert {row.status for row in db.exec(select(SopOperationAttempt)).all()} == {"running"}
        assert {row.status for row in db.exec(select(SopNodeExecution)).all()} == {"running"}
        assert {row.status for row in db.exec(select(DynamicReadDispatchItem)).all()} == {
            "dispatched"
        }
        assert db.get(DynamicReadDispatchBatch, batch_id).status == "dispatched"
        assert db.exec(select(ExecutionSignal)).all() == []


def test_parallel_recovery_invalid_child_chain_fails_closed(
    audit_engine: Engine,
) -> None:
    """batch 与子事实关联链损坏时不凭 item CAS 丢弃，也不提交部分恢复。"""

    _instance_id, batch_id = _seed_inbox(audit_engine)
    with Session(audit_engine) as db:
        item = db.exec(select(DynamicReadDispatchItem)).first()
        assert item is not None
        item.node_execution_id = "node-from-another-execution"
        db.add(item)
        db.commit()

    with Session(audit_engine) as db:
        assert reconcile_parallel_read_batches(db) == 0
        batch = db.get(DynamicReadDispatchBatch, batch_id)
        assert batch is not None and batch.status == "dispatched"
        assert {row.status for row in db.exec(select(DynamicReadDispatchItem)).all()} == {
            "dispatched"
        }
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"running"}
        assert db.exec(select(ExecutionSignal)).all() == []


def test_parallel_recovery_leaves_retry_wait_explicitly_pending(
    audit_engine: Engine,
) -> None:
    """并行恢复遇到 retry_wait 时只保留波次，不伪造失败或激活第二套重试。"""

    instance_id, batch_id = _seed_inbox(audit_engine)
    with Session(audit_engine) as db:
        operations = db.exec(
            select(SopOperation).where(SopOperation.instance_id == instance_id)
        ).all()
        for operation in operations:
            operation.status = "retry_wait"
            db.add(operation)
            attempt = db.exec(
                select(SopOperationAttempt).where(
                    SopOperationAttempt.operation_id == operation.id,
                )
            ).one()
            attempt.status = "retry_wait"
            attempt.retry_at = utc_now()
            db.add(attempt)
        db.commit()

    with Session(audit_engine) as db:
        assert reconcile_parallel_read_batches(db) == 0
        db.commit()
        instance = db.get(SopInstance, instance_id)
        batch = db.get(DynamicReadDispatchBatch, batch_id)
        assert instance is not None and instance.status == "running"
        assert batch is not None and batch.status == "dispatched"
        assert {row.status for row in db.exec(select(SopOperation)).all()} == {"retry_wait"}
        assert {row.status for row in db.exec(select(DynamicReadDispatchItem)).all()} == {
            "dispatched"
        }
