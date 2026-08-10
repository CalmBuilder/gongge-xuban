"""
@Time       : 2026/08/11 00:05
@Author     : zhanglp8181
@File       : test_dynamic_task_quotas.py
@CallChain  : pytest → DynamicTaskQuotaService → database unique slots
@Description: 验证四级配额配置、跨 holder 原子槽位竞争、幂等复用和确定终态释放。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import DynamicTaskQuotaLease, SopInstance, SopOperation
from app.dynamic_tasks.quotas import (
    DynamicTaskQuotaError,
    DynamicTaskQuotaLimits,
    DynamicTaskQuotaService,
)


def test_execution_quota_acquires_three_scopes_atomically_and_releases() -> None:
    """验证 tenant 槽耗尽时第二个 Execution 不会遗留 Agent/用户半套租约。"""

    with _session() as db:
        first = _instance("execution_a", agent_id="agent_a", user_id="user_a")
        second = _instance("execution_b", agent_id="agent_b", user_id="user_b")
        db.add(first)
        db.add(second)
        db.commit()
        service = DynamicTaskQuotaService(db)
        limits = DynamicTaskQuotaLimits(tenant=1, agent=1, user=1, tool=1)

        service.acquire_execution(first, limits=limits)
        service.acquire_execution(first, limits=limits)
        db.commit()
        assert len(_leases(db, first.id)) == 3

        with pytest.raises(DynamicTaskQuotaError) as exhausted:
            service.acquire_execution(second, limits=limits)
        assert exhausted.value.code == "DYNAMIC_TASK_TENANT_QUOTA_EXCEEDED"
        assert _leases(db, second.id) == []

        service.release_execution(first)
        service.acquire_execution(second, limits=limits)
        db.commit()
        assert len(_leases(db, second.id)) == 3


def test_tool_quota_holds_unknown_work_until_explicit_release() -> None:
    """验证同名工具跨 Execution 竞争唯一槽，释放前不会因重试扩大 provider 并发。"""

    with _session() as db:
        first = _operation("operation_a", "execution_a")
        second = _operation("operation_b", "execution_b")
        db.add(first)
        db.add(second)
        db.commit()
        service = DynamicTaskQuotaService(db)

        service.acquire_tool_operation(first, limit=1)
        service.acquire_tool_operation(first, limit=1)
        db.commit()
        with pytest.raises(DynamicTaskQuotaError) as exhausted:
            service.acquire_tool_operation(second, limit=1)
        assert exhausted.value.code == "DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED"

        service.release_tool_operation(first)
        service.acquire_tool_operation(second, limit=1)
        db.commit()
        assert len(_leases(db, second.id)) == 1


def test_quota_rejects_unconfigured_limits_without_writing_rows() -> None:
    """验证默认零上限是发布未就绪而不是无限配额。"""

    with _session() as db:
        instance = _instance("execution_a", agent_id="agent_a", user_id="user_a")
        db.add(instance)
        db.commit()

        with pytest.raises(DynamicTaskQuotaError) as unconfigured:
            DynamicTaskQuotaService(db).acquire_execution(
                instance,
                limits=DynamicTaskQuotaLimits(),
            )

        assert unconfigured.value.code == "DYNAMIC_TASK_QUOTA_NOT_CONFIGURED"
        assert db.exec(select(DynamicTaskQuotaLease)).all() == []


def test_quota_slot_is_enforced_across_independent_database_sessions(tmp_path) -> None:
    """验证独立连接看到同一唯一槽，释放提交后另一个进程等价会话才能取得。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'quota-cross-session.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as first_db, Session(engine) as second_db:
        first = _instance("execution_a", agent_id="agent_a", user_id="user_a")
        second = _instance("execution_b", agent_id="agent_b", user_id="user_b")
        first_db.add(first)
        first_db.add(second)
        first_db.commit()
        second = second_db.get(SopInstance, second.id)
        assert second is not None
        limits = DynamicTaskQuotaLimits(tenant=1, agent=1, user=1, tool=1)

        DynamicTaskQuotaService(first_db).acquire_execution(first, limits=limits)
        first_db.commit()
        with pytest.raises(DynamicTaskQuotaError) as exhausted:
            DynamicTaskQuotaService(second_db).acquire_execution(second, limits=limits)
        assert exhausted.value.code == "DYNAMIC_TASK_TENANT_QUOTA_EXCEEDED"
        second_db.rollback()

        DynamicTaskQuotaService(first_db).release_execution(first)
        first_db.commit()
        second = second_db.get(SopInstance, "execution_b")
        assert second is not None
        DynamicTaskQuotaService(second_db).acquire_execution(second, limits=limits)
        second_db.commit()
        assert len(_leases(second_db, second.id)) == 3


def _session() -> Session:
    """创建启用完整约束的独占 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _instance(instance_id: str, *, agent_id: str, user_id: str) -> SopInstance:
    """创建满足动态身份约束的活动 Execution。"""

    return SopInstance(
        id=instance_id,
        tenant_id="tenant_a",
        session_id=f"session_{instance_id}",
        kind="dynamic_task",
        active_slot_key=f"foreground:session_{instance_id}",
        initiator_user_id=user_id,
        agent_id=agent_id,
        goal_snapshot_json={"goal": "配额测试", "success_criteria": []},
        current_plan_revision_id=f"plan_{instance_id}",
        current_plan_checksum=f"plan-checksum-{instance_id}",
        capability_snapshot_json={"model": {"id": "model"}},
        capability_checksum=f"capability-checksum-{instance_id}",
        current_node_id="step_a",
        status="running",
    )


def _operation(operation_id: str, instance_id: str) -> SopOperation:
    """创建同名工具的 prepared Operation，模拟 dispatch 前槽位竞争。"""

    return SopOperation(
        id=operation_id,
        tenant_id="tenant_a",
        instance_id=instance_id,
        node_execution_id=f"step_{operation_id}",
        operation_name="crm.read",
        idempotency_key=f"idempotency-{operation_id}",
        logical_action_id=f"logical-{operation_id}",
        request_fingerprint=f"fingerprint-{operation_id}",
        status="prepared",
        effect_kind="read",
    )


def _leases(db: Session, holder_id: str) -> list[DynamicTaskQuotaLease]:
    """读取指定 holder 的全部活动槽位，供原子性断言。"""

    return db.exec(
        select(DynamicTaskQuotaLease).where(DynamicTaskQuotaLease.holder_id == holder_id)
    ).all()
