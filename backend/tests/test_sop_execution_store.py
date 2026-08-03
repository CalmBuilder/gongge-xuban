"""
@Time       : 2026/07/22 13:05
@Author     : zhanglp8181
@File       : test_sop_execution_store.py
@CallChain  : pytest → SopExecutionStore → SQLite 执行聚合
@Description: 验证 SOP 实例恢复、节点等待和工具操作幂等回执的通用持久化语义。
"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import SopInstance, SopNodeExecution, SopOperation
from app.sop_runtime.execution_store import (
    SopExecutionConflictError,
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
        execution = store.enter_node(instance, "collect_employee", input_snapshot={})
        store.wait_for_input(instance, execution, expected_inputs=("employee_id",))
        db.commit()

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
