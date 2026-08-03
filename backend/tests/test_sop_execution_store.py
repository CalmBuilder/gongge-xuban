"""
@Time       : 2026/07/22 13:05
@Author     : zhanglp8181
@File       : test_sop_execution_store.py
@CallChain  : pytest → SopExecutionStore → SQLite 执行聚合
@Description: 验证执行租约、逻辑动作幂等、unknown 对账、两阶段取消及效果/补偿账本。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ExecutionMutationRejection,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    SopOperationEffect,
)
from app.sop_runtime.contracts import IdempotencyPolicy, IdempotencyScope
from app.sop_runtime.execution_store import (
    SopExecutionConflictError,
    SopExecutionFencedError,
    SopExecutionStore,
)


def _test_session() -> Session:
    """创建包含完整 SQLModel 元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _start(store: SopExecutionStore) -> SopInstance:
    """创建用于节点和操作测试的已启动实例。"""

    instance, created = store.start_instance(
        tenant_id="tenant_demo",
        session_id="session_demo",
        skill_id="skill_expense_quota_query",
        skill_version_id="skillver_quota_200",
        skill_version="2.0.0",
        definition_checksum="a" * 64,
        start_node_id="collect_employee",
    )
    assert created is True
    return instance


def test_instance_and_waiting_node_can_resume_without_losing_attempt() -> None:
    """验证输入等待和恢复复用同一节点 attempt，并保留最终槽位快照。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "collect_employee", input_snapshot={})
            store.wait_for_input(instance, execution, expected_inputs=("employee_id",))
        db.commit()

        with store.owned(instance, worker_id="worker-test"):
            store.resume_waiting_node(instance, execution, slots={"employee_id": "E001"})
            store.complete_node(instance, execution, output={"employee_id": "E001"})
        db.commit()
        executions = db.exec(select(SopNodeExecution)).all()
        instance_status = instance.status
        instance_slots = instance.slots_json
        execution_status = execution.status
        execution_attempt = execution.attempt

    assert instance_status == "running"
    assert instance_slots == {"employee_id": "E001"}
    assert execution_status == "succeeded"
    assert execution_attempt == 1
    assert len(executions) == 1


def test_same_active_version_is_reused_but_different_version_conflicts() -> None:
    """验证活动实例按不可变版本幂等复用，且不会被新版本静默替换。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        first = _start(store)
        reused, created = store.start_instance(
            tenant_id="tenant_demo",
            session_id="session_demo",
            skill_id="skill_expense_quota_query",
            skill_version_id="skillver_quota_200",
            skill_version="2.0.0",
            definition_checksum="a" * 64,
            start_node_id="collect_employee",
        )

        assert reused.id == first.id
        assert created is False

        try:
            store.start_instance(
                tenant_id="tenant_demo",
                session_id="session_demo",
                skill_id="skill_expense_quota_query",
                skill_version_id="skillver_quota_201",
                skill_version="2.0.1",
                definition_checksum="b" * 64,
                start_node_id="collect_employee",
            )
        except SopExecutionConflictError as error:
            assert error.code == "SOP_EXECUTION_CONFLICT"
        else:
            raise AssertionError("不同不可变版本必须与活动实例冲突")


def test_operation_receipt_is_idempotent_and_can_complete_instance() -> None:
    """验证相同工具命令只准备一次，并形成可供流程判断的成功回执。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(
                instance,
                "query_quota",
                input_snapshot={"employee_id": "E001", "month": "2026-07"},
            )
            operation, created = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.quota_query",
                request={"employee_id": "E001", "month": "2026-07"},
            )
            repeated, repeated_created = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.quota_query",
                request={"month": "2026-07", "employee_id": "E001"},
            )
            store.start_operation(operation)
            store.finish_operation(
                operation,
                succeeded=True,
                result={"remaining": 20000.0, "currency": "CNY"},
            )
            store.complete_node(instance, execution, output={"operation_id": operation.id})
            store.complete_instance(instance, slots={"employee_id": "E001"})
        db.commit()
        operations = db.exec(select(SopOperation)).all()
        operation_id = operation.id
        repeated_id = repeated.id
        operation_status = operation.status
        operation_result = operation.result_json
        instance_status = instance.status

    assert created is True
    assert repeated_created is False
    assert repeated_id == operation_id
    assert operation_status == "succeeded"
    assert operation_result["remaining"] == 20000.0
    assert instance_status == "succeeded"
    assert len(operations) == 1


def test_authoritative_mutations_require_execution_lease() -> None:
    """验证节点、Operation 和终态写不能绕过统一 execution 所有权。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)

        with pytest.raises(SopExecutionConflictError, match="execution lease"):
            store.enter_node(instance, "collect_employee", input_snapshot={})


def test_expired_lease_fences_late_node_operation_and_terminal_writes(tmp_path) -> None:
    """验证新 worker 抢占后旧 token 的节点、Operation 与终态迟到写均被隔离审计。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'execution-fencing.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as seed_db:
        instance = _start(SopExecutionStore(seed_db))
        seed_db.commit()
        instance_id = instance.id

    with Session(engine) as worker_a_db:
        instance_a = worker_a_db.get(SopInstance, instance_id)
        assert instance_a is not None
        store_a = SopExecutionStore(worker_a_db)
        with store_a.owned(instance_a, worker_id="worker-a", ttl_seconds=30) as lease_a:
            execution_a = store_a.enter_node(instance_a, "query_quota", input_snapshot={})
            operation_a, _ = store_a.prepare_operation(
                instance_a,
                execution_a,
                operation_name="expense.quota_query",
                request={"employee_id": "E001"},
            )
            operation_id = operation_a.id
            execution_id = execution_a.id
            worker_a_db.commit()

            with Session(engine) as expiry_db:
                expiry_db.exec(
                    update(SopInstance)
                    .where(SopInstance.id == instance_id)
                    .values(lease_expires_at=datetime(2000, 1, 1))
                )
                expiry_db.commit()

            with Session(engine) as worker_b_db:
                instance_b = worker_b_db.get(SopInstance, instance_id)
                assert instance_b is not None
                store_b = SopExecutionStore(worker_b_db)
                with store_b.owned(instance_b, worker_id="worker-b") as lease_b:
                    assert lease_b.fencing_token > lease_a.fencing_token
                worker_b_db.commit()

            rejected_actions: list[str] = []
            instance_a.context_json = {"late_worker_payload": True}
            for action, mutation in (
                ("lease.renew", lambda: store_a.renew(lease_a)),
                (
                    "node.complete",
                    lambda: store_a.complete_node(
                        instance_a,
                        execution_a,
                        output={"late": True},
                    ),
                ),
                ("operation.start", lambda: store_a.start_operation(operation_a)),
                ("instance.complete", lambda: store_a.complete_instance(instance_a)),
            ):
                with pytest.raises(SopExecutionFencedError) as caught:
                    mutation()
                assert caught.value.action == action
                rejected_actions.append(action)
            worker_a_db.rollback()

    with Session(engine) as verify_db:
        persisted_operation = verify_db.get(SopOperation, operation_id)
        persisted_execution = verify_db.get(SopNodeExecution, execution_id)
        persisted_instance = verify_db.get(SopInstance, instance_id)
        rejections = verify_db.exec(
            select(ExecutionMutationRejection).order_by(ExecutionMutationRejection.created_at)
        ).all()

    assert rejected_actions == [
        "lease.renew",
        "node.complete",
        "operation.start",
        "instance.complete",
    ]
    assert persisted_operation is not None and persisted_operation.status == "prepared"
    assert persisted_execution is not None and persisted_execution.status == "running"
    assert persisted_instance is not None and persisted_instance.status == "running"
    assert persisted_instance.context_json == {}
    assert [item.action for item in rejections] == rejected_actions
    assert all(item.rejected_fencing_token < item.current_fencing_token for item in rejections)


def test_execution_kind_condition_allows_dynamic_identity_but_rejects_invalid_sop() -> None:
    """验证通用 Execution 可无 SOP 身份，而 kind=sop 仍由数据库强制完整绑定。"""

    with _test_session() as db:
        dynamic = SopInstance(
            tenant_id="tenant_demo",
            session_id="session_dynamic",
            kind="dynamic_task",
            active_slot_key="foreground:session_dynamic",
            source_kind="api",
            source_ref="request-1",
            status="running",
            current_node_id="planning",
        )
        db.add(dynamic)
        db.commit()
        assert db.get(SopInstance, dynamic.id) is not None

        invalid_sop = SopInstance(
            tenant_id="tenant_demo",
            session_id="session_invalid_sop",
            kind="sop",
            active_slot_key="foreground:session_invalid_sop",
            status="running",
        )
        db.add(invalid_sop)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_renew_and_release_require_current_fencing_token() -> None:
    """验证续租保持 token，释放只影响当前 owner/token，伪造旧 token 无法解锁。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        lease = store.claim(instance, worker_id="worker-a", ttl_seconds=10)
        renewed = store.renew(lease, ttl_seconds=60)

        assert renewed.fencing_token == lease.fencing_token
        assert renewed.expires_at > lease.expires_at
        assert store.release(renewed) is True
        assert store.release(renewed) is False


def test_unexpired_lease_blocks_competing_worker_and_ignores_worker_clock(
    tmp_path,
    monkeypatch,
) -> None:
    """验证未过期 owner 排他，且极端进程时钟偏移不能改变数据库租约裁决。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'database-time.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as seed_db:
        instance = _start(SopExecutionStore(seed_db))
        seed_db.commit()
        instance_id = instance.id

    monkeypatch.setattr(
        "app.sop_runtime.execution_store.utc_now",
        lambda: datetime(2200, 1, 1),
    )
    with Session(engine) as worker_a_db:
        instance_a = worker_a_db.get(SopInstance, instance_id)
        assert instance_a is not None
        store_a = SopExecutionStore(worker_a_db)
        lease_a = store_a.claim(instance_a, worker_id="clock-skewed-a", ttl_seconds=30)
        worker_a_db.commit()
        assert lease_a.expires_at.year < 2200

        with Session(engine) as worker_b_db:
            instance_b = worker_b_db.get(SopInstance, instance_id)
            assert instance_b is not None
            with pytest.raises(SopExecutionConflictError, match="其他 worker"):
                SopExecutionStore(worker_b_db).claim(
                    instance_b,
                    worker_id="worker-b",
                    ttl_seconds=30,
                )

        assert store_a.release(lease_a) is True
        worker_a_db.commit()


def test_concurrent_active_slot_creation_keeps_one_execution(tmp_path) -> None:
    """验证 SQLite 双 worker 并发启动时数据库活动槽最多保留一个实例。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'active-slot.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    barrier = Barrier(2)

    def start_from_worker(version_id: str) -> tuple[str, bool] | None:
        """让独立事务同时尝试占用同一 tenant/session 活动槽。"""

        with Session(engine) as db:
            barrier.wait()
            try:
                instance, created = SopExecutionStore(db).start_instance(
                    tenant_id="tenant_demo",
                    session_id="session_shared",
                    skill_id="skill_demo",
                    skill_version_id=version_id,
                    skill_version="1.0.0",
                    definition_checksum="a" * 64,
                    start_node_id="start",
                )
                db.commit()
                return instance.id, created
            except (IntegrityError, OperationalError, SopExecutionConflictError):
                db.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start_from_worker, ("version-a", "version-b")))

    with Session(engine) as verify_db:
        active = verify_db.exec(
            select(SopInstance).where(SopInstance.active_slot_key.is_not(None))
        ).all()

    assert len(active) == 1
    assert sum(result is not None for result in results) >= 1


def test_new_step_attempt_reuses_logical_action_and_remote_key() -> None:
    """验证节点重试只新增本地 attempt，不产生第二个 Operation 或远端幂等键。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            first_execution = store.enter_node(instance, "submit", input_snapshot={})
            first, created = store.prepare_operation(
                instance,
                first_execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-1", "amount": 100},
                logical_action_id="action-submit-expense",
                effect_kind="external_write",
            )
            store.fail_node(instance, first_execution, error={"code": "RETRY"})
            second_execution = store.enter_node(instance, "submit", input_snapshot={})
            second, second_created = store.prepare_operation(
                instance,
                second_execution,
                operation_name="expense.submit",
                request={"amount": 100, "request_id": "REQ-1"},
                logical_action_id="action-submit-expense",
                effect_kind="external_write",
            )
        db.commit()
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == first.id)
        ).all()

    assert created is True
    assert second_created is False
    assert second.id == first.id
    assert second.remote_idempotency_key == first.remote_idempotency_key
    assert len(attempts) == 2


def test_same_logical_action_with_changed_request_is_rejected() -> None:
    """验证同一 logical action 的规范请求变化会在任何工具调用前形成冲突。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-1", "amount": 100},
                logical_action_id="action-submit-expense",
                effect_kind="external_write",
            )
            with pytest.raises(SopExecutionConflictError, match="fingerprint"):
                store.prepare_operation(
                    instance,
                    execution,
                    operation_name="expense.submit",
                    request={"request_id": "REQ-1", "amount": 101},
                    logical_action_id="action-submit-expense",
                    effect_kind="external_write",
                )


def test_business_idempotency_policy_uses_only_declared_fields() -> None:
    """验证业务级远端键忽略非声明字段，同时完整请求 fingerprint 仍各自可审计。"""

    policy = IdempotencyPolicy(
        scope=IdempotencyScope.BUSINESS,
        key_fields=("request_id",),
    )
    first = SopExecutionStore.remote_idempotency_key(
        tenant_id="tenant_demo",
        instance_id="instance-a",
        logical_action_id="action-a",
        operation_name="expense.submit",
        request={"request_id": "REQ-1", "trace": "first"},
        policy=policy,
    )
    second = SopExecutionStore.remote_idempotency_key(
        tenant_id="tenant_demo",
        instance_id="instance-b",
        logical_action_id="action-b",
        operation_name="expense.submit",
        request={"request_id": "REQ-1", "trace": "second"},
        policy=policy,
    )

    assert first == second


def test_same_logical_action_rejects_idempotency_required_policy_drift() -> None:
    """验证同一逻辑动作不能从可选远端幂等静默漂移为必需策略。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-1"},
                logical_action_id="action-policy-drift",
                idempotency_policy=IdempotencyPolicy(required=False),
                effect_kind="external_write",
            )
            with pytest.raises(SopExecutionConflictError, match="策略"):
                store.prepare_operation(
                    instance,
                    execution,
                    operation_name="expense.submit",
                    request={"request_id": "REQ-1"},
                    logical_action_id="action-policy-drift",
                    idempotency_policy=IdempotencyPolicy(required=True),
                    effect_kind="external_write",
                )


def test_operation_freezes_capability_snapshot_and_rejects_revision_drift() -> None:
    """验证 Operation 保存规范能力快照/checksum，同一逻辑动作不得换契约。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        first_snapshot = {
            "capability_type": "tool",
            "capability_id": "tool_submit",
            "contract": {"risk_class": "external_write", "timeout_policy": "unknown"},
        }
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            operation, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-1"},
                logical_action_id="action-capability-snapshot",
                effect_kind="external_write",
                capability_snapshot=first_snapshot,
            )
            assert operation.capability_snapshot_json == first_snapshot
            assert operation.capability_checksum is not None

            with pytest.raises(SopExecutionConflictError, match="策略或效果契约"):
                store.prepare_operation(
                    instance,
                    execution,
                    operation_name="expense.submit",
                    request={"request_id": "REQ-1"},
                    logical_action_id="action-capability-snapshot",
                    effect_kind="external_write",
                    capability_snapshot={
                        **first_snapshot,
                        "contract": {
                            "risk_class": "external_write",
                            "timeout_policy": "failed",
                        },
                    },
                )


@pytest.mark.parametrize(
    "payload",
    (
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": {"not", "json"}},
        {1: "non-string-key"},
    ),
)
def test_non_json_values_are_rejected_before_fingerprint(payload) -> None:  # noqa: ANN001
    """验证 NaN、Infinity、set 和非字符串键不会被隐式字符串化进命令摘要。"""

    with pytest.raises(ValueError, match="JSON"):
        SopExecutionStore.request_fingerprint(payload)


def test_cancel_prepared_operation_and_reconcile_running_write() -> None:
    """验证 prepared 零调用取消，running 外部写进入 unknown 并在对账后才取消实例。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            prepared, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.draft",
                request={"request_id": "REQ-DRAFT"},
                logical_action_id="action-draft",
                effect_kind="external_write",
            )
            store.cancel_prepared_operation(prepared)
            running, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-1"},
                logical_action_id="action-submit",
                effect_kind="external_write",
            )
            store.start_operation(running)
            settled = store.request_cancellation(
                instance,
                actor_user_id="user_demo",
                reason="用户撤回",
            )
            assert settled is False
            assert instance.status == "running"
            assert instance.cancellation_disposition == "awaiting_reconciliation"
            assert running.status == "unknown"
            with pytest.raises(SopExecutionConflictError, match="running"):
                store.finish_operation(
                    running,
                    succeeded=False,
                    error={"code": "LATE_FAILURE"},
                )
            with pytest.raises(SopExecutionConflictError, match="效果证据"):
                store.reconcile_operation(
                    instance,
                    running,
                    succeeded=True,
                    result={"id": "unexpected"},
                    effect_confirmed=False,
                )
            settled = store.reconcile_operation(
                instance,
                running,
                succeeded=False,
                result={},
                error={"code": "REMOTE_NOT_APPLIED"},
                effect_confirmed=False,
            )
        db.commit()
        prepared_status = prepared.status
        instance_status = instance.status
        active_slot_key = instance.active_slot_key

    assert prepared_status == "cancelled"
    assert settled is True
    assert instance_status == "cancelled"
    assert active_slot_key is None


def test_effect_state_reports_partial_and_unknown_external_effects() -> None:
    """验证多个外部动作的成功、失败与未知结果形成正交 execution effect_state。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "multi_write", input_snapshot={})
            succeeded, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="system_a.create",
                request={"id": "A"},
                logical_action_id="action-a",
                effect_kind="external_write",
            )
            store.start_operation(succeeded)
            store.finish_operation(succeeded, succeeded=True, result={"id": "A-1"})
            failed, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="system_b.notify",
                request={"id": "B"},
                logical_action_id="action-b",
                effect_kind="external_write",
            )
            store.start_operation(failed)
            store.finish_operation(
                failed,
                succeeded=False,
                error={"code": "HTTP_ERROR"},
            )
            assert store.aggregate_effect_state(instance) == "partial"
            unknown, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="system_c.update",
                request={"id": "C"},
                logical_action_id="action-c",
                effect_kind="external_write",
            )
            store.start_operation(unknown)
            store.mark_operation_unknown(unknown, error={"code": "TIMEOUT"})

    assert instance.effect_state == "unknown"


def test_stale_running_external_write_becomes_unknown_after_worker_crash() -> None:
    """验证同步 worker 超时失联后由数据库时间判定 unknown，原逻辑动作不再允许重发。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            operation, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-STALE"},
                logical_action_id="action-stale",
                effect_kind="external_write",
            )
            store.start_operation(operation)
            operation.started_at = datetime(2000, 1, 1)
            assert store.mark_stale_running_operation_unknown(
                operation,
                timeout_seconds=30,
            ) is True
            assert store.mark_stale_running_operation_unknown(
                operation,
                timeout_seconds=30,
            ) is False

    assert operation.status == "unknown"
    assert operation.effect_state == "unknown"


def test_compensation_is_a_new_managed_action_with_auditable_lineage() -> None:
    """验证补偿不是回滚原记录，而是新的受管逻辑动作并在双方效果账本保留 lineage。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance = _start(store)
        with store.owned(instance, worker_id="worker-test"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            original, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.submit",
                request={"request_id": "REQ-COMP"},
                logical_action_id="action-original",
                effect_kind="external_write",
            )
            store.start_operation(original)
            store.finish_operation(original, succeeded=True, result={"id": "REMOTE-1"})
            compensation, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="expense.cancel",
                request={"remote_id": "REMOTE-1"},
                logical_action_id="action-compensation",
                effect_kind="external_write",
                compensates_operation_id=original.id,
            )
            store.start_operation(compensation)
            store.finish_operation(compensation, succeeded=True, result={"cancelled": True})
        db.commit()
        effects = db.exec(
            select(SopOperationEffect).order_by(
                SopOperationEffect.operation_id,
                SopOperationEffect.sequence,
            )
        ).all()
        original_state = original.effect_state
        compensation_state = compensation.effect_state
        aggregate = instance.effect_state

    assert original.id != compensation.id
    assert original_state == "compensated"
    assert compensation_state == "complete"
    assert aggregate == "complete"
    assert any(
        effect.operation_id == original.id
        and effect.event_type == "compensated"
        and effect.compensation_operation_id == compensation.id
        for effect in effects
    )
