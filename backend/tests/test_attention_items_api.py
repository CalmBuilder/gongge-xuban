"""
@Time       : 2026/08/03 22:55
@Author     : zhanglp8181
@File       : test_attention_items_api.py
@CallChain  : pytest → attention-items API → ExecutionControl/SQLite
@Description: 验证统一 Attention 查询、actor 边界、CAS 首胜和原子恢复 signal。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.attention_items import (
    AttentionResolveRequest,
    get_attention_item,
    list_attention_items,
    resolve_attention_item,
)
from app.api.work_items import list_work_items
from app.db.models import ExecutionSignal, SopInstance, Tenant, User
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


@pytest.fixture
def db() -> Session:
    """建立带真实唯一约束的共享内存数据库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _seed_attention(db: Session):
    """创建动态 Execution、候选人、旁观者和一条 clarification Attention。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    candidate = User(
        id="user_candidate",
        tenant_id="tenant_demo",
        username="candidate",
        password_hash="hash",
    )
    outsider = User(
        id="user_outsider",
        tenant_id="tenant_demo",
        username="outsider",
        password_hash="hash",
    )
    instance = SopInstance(
        id="execution_attention",
        tenant_id="tenant_demo",
        session_id="session_attention",
        kind="dynamic_task",
        active_slot_key="dynamic:session_attention",
        initiator_user_id="user_requester",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": "prepare report"},
        current_plan_revision_id="plan_1",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="waiting",
    )
    db.add(candidate)
    db.add(outsider)
    db.add(instance)
    db.commit()
    store = SopExecutionStore(db)
    control = ExecutionControlService(db, store)
    with store.owned(instance, worker_id="planner"):
        attention, _ = control.offer_attention(
            instance,
            attention_kind="clarification",
            attention_key="step_1:clarification",
            title="补充报告范围",
            payload={"question": "请选择年度"},
            allowed_commands=["answer"],
            candidate_user_ids=[candidate.id],
        )
    db.commit()
    return candidate, outsider, instance, attention


def test_attention_center_lists_all_kinds_but_old_inbox_does_not(db: Session) -> None:
    """验证通用 Attention 出现在统一中心，并由服务端返回真实可用命令。"""

    candidate, _, _, attention = _seed_attention(db)
    page = list_attention_items(
        tenant_id="tenant_demo",
        view="active",
        page=1,
        page_size=20,
        current_user=candidate,
        db=db,
    )
    assert page.total == 1
    assert page.items[0].id == attention.id
    assert page.items[0].kind == "clarification"
    assert page.items[0].available_commands == ["answer"]
    assert list_work_items("tenant_demo", "pending", candidate, db) == []


def test_attention_detail_rejects_unrelated_platform_user(db: Session) -> None:
    """验证同租户旁观者不能读取与自己无关系的 Attention payload。"""

    _, outsider, _, attention = _seed_attention(db)
    with pytest.raises(HTTPException) as caught:
        get_attention_item(attention.id, "tenant_demo", outsider, db)
    assert caught.value.status_code == 403


def test_attention_resolution_first_cas_wins_and_creates_one_signal(db: Session) -> None:
    """验证多端不同 command id 竞争时首个 revision 胜出，且只产生一次恢复 signal。"""

    candidate, _, instance, attention = _seed_attention(db)
    request = AttentionResolveRequest(
        tenant_id="tenant_demo",
        command_id="answer_first",
        command="answer",
        expected_revision=0,
        comment="2026",
    )
    result = resolve_attention_item(
        attention.id,
        request,
        candidate,
        db,
    )
    assert result.status == "completed"
    assert result.resolution["comment"] == "2026"
    signals = db.exec(select(ExecutionSignal)).all()
    assert len(signals) == 1
    assert signals[0].status == "pending"

    instance.status = "cancelled"
    instance.active_slot_key = None
    db.add(instance)
    db.commit()
    replay = resolve_attention_item(attention.id, request, candidate, db)
    assert replay.resolution["comment"] == "2026"
    assert len(db.exec(select(ExecutionSignal)).all()) == 1

    with pytest.raises(HTTPException) as caught:
        resolve_attention_item(
            attention.id,
            AttentionResolveRequest(
                tenant_id="tenant_demo",
                command_id="answer_second",
                command="answer",
                expected_revision=0,
                comment="2025",
            ),
            candidate,
            db,
        )
    assert caught.value.status_code == 409
    assert len(db.exec(select(ExecutionSignal)).all()) == 1
