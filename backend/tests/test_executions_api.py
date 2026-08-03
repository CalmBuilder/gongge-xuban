"""
@Time       : 2026/08/03 23:05
@Author     : zhanglp8181
@File       : test_executions_api.py
@CallChain  : pytest → executions API → ExecutionControl/TerminalClosureGuard
@Description: 验证 Execution 命令授权、取消闭环、steer 等待状态和结果发布查询。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.executions import (
    ExecutionCommandRequest,
    get_execution_result,
    issue_execution_command,
)
from app.db.models import ExecutionCommand, SopInstance, Tenant, User


@pytest.fixture
def db() -> Session:
    """建立隔离 SQLite 以执行真实命令、租约和终态约束。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _seed_execution(db: Session, *, suffix: str = "cancel"):
    """创建发起人、旁观者和一个无副作用动态 Execution。"""

    if db.get(Tenant, "tenant_demo") is None:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                password_hash="hash",
            )
        )
        db.add(
            User(
                id="user_outsider",
                tenant_id="tenant_demo",
                username="outsider",
                password_hash="hash",
            )
        )
    instance = SopInstance(
        id=f"execution_{suffix}",
        tenant_id="tenant_demo",
        session_id=f"session_{suffix}",
        kind="dynamic_task",
        active_slot_key=f"dynamic:{suffix}",
        initiator_user_id="user_owner",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": suffix},
        current_plan_revision_id="plan_1",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="running",
    )
    db.add(instance)
    db.commit()
    return db.get(User, "user_owner"), db.get(User, "user_outsider"), instance


def test_cancel_command_reaches_terminal_result_and_publication(db: Session) -> None:
    """验证 cancel 不是只写请求，而是关闭 signal 并产出一次可查询终态结果。"""

    owner, _, instance = _seed_execution(db)
    assert owner is not None
    command = issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="cancel_1",
            command_type="cancel",
            expected_revision=instance.revision,
            payload={"reason": "user_requested"},
        ),
        owner,
        db,
    )
    assert command.status == "applied"
    db.refresh(instance)
    assert instance.status == "cancelled"
    result = get_execution_result(instance.id, "tenant_demo", owner, db)
    assert result.result["status"] == "cancelled"
    assert result.publications[0]["required"] is True
    assert result.publications[0]["status"] == "settled"


def test_steer_stays_pending_and_idempotency_conflict_is_rejected(db: Session) -> None:
    """验证 B1.2 前 steer 明确保持 pending，且同 command id 不得改写 payload。"""

    owner, _, instance = _seed_execution(db, suffix="steer")
    assert owner is not None
    request = ExecutionCommandRequest(
        tenant_id="tenant_demo",
        command_id="steer_1",
        command_type="steer",
        expected_revision=instance.revision,
        payload={"instruction": "仅看今年"},
    )
    first = issue_execution_command(instance.id, request, owner, db)
    assert first.status == "pending"
    replay = issue_execution_command(instance.id, request, owner, db)
    assert replay.status == "pending"

    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            request.model_copy(update={"payload": {"instruction": "改看去年"}}),
            owner,
            db,
        )
    assert caught.value.status_code == 409


def test_unrelated_user_cannot_cancel_execution(db: Session) -> None:
    """验证平台同租户身份本身不授予 Execution 管理能力。"""

    _, outsider, instance = _seed_execution(db, suffix="forbidden")
    assert outsider is not None
    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            ExecutionCommandRequest(
                tenant_id="tenant_demo",
                command_id="cancel_forbidden",
                command_type="cancel",
                expected_revision=instance.revision,
            ),
            outsider,
            db,
        )
    assert caught.value.status_code == 403


def test_cancel_disposes_pending_steer_before_terminal(db: Session) -> None:
    """验证取消会明确拒绝尚未消费的 steer，不会被旧约束永久阻塞或在终态后应用。"""

    owner, _, instance = _seed_execution(db, suffix="steer_then_cancel")
    assert owner is not None
    issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="steer_before_cancel",
            command_type="steer",
            expected_revision=instance.revision,
            payload={"instruction": "change scope"},
        ),
        owner,
        db,
    )
    cancel = issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="cancel_after_steer",
            command_type="cancel",
            expected_revision=instance.revision,
            payload={"reason": "stop"},
        ),
        owner,
        db,
    )
    assert cancel.status == "applied"
    steer = db.exec(
        select(ExecutionCommand).where(
            ExecutionCommand.command_id == "steer_before_cancel"
        )
    ).one()
    assert steer.status == "rejected"
    assert steer.reason_code == "EXECUTION_CANCELLED"
