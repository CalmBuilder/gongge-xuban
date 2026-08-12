"""
@Time       : 2026/08/14 02:10
@Author     : zhanglp8181
@File       : test_general_skill_worker.py
@CallChain  : Skill maintenance worker → interrupted chat Use reconciler → Use/AgentEvent
@Description: 验证普通对话进程崩溃后遗留的 Skill Use 可被持久维护任务幂等收敛。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import AgentEvent, GeneralSkillUse, utc_now
from app.general_skills.worker import _reconcile_interrupted_chat_uses


def test_reconciler_fails_stale_chat_use_and_records_one_terminal_event() -> None:
    """无 Execution 的陈旧 active Use 必须失败，重复维护不产生重复终态事件。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    now = utc_now()
    with Session(engine) as db:
        use = GeneralSkillUse(
            id="gsuse_interrupted",
            tenant_id="tenant_worker",
            session_id="session_worker",
            turn_id="turn_worker",
            agent_id="agent_worker",
            user_id="user_worker",
            skill_id="skill_worker",
            revision_id="revision_worker",
            content_checksum="a" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="b" * 64,
            loaded_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        )
        db.add(use)
        db.commit()

        assert _reconcile_interrupted_chat_uses(db, now=now) == 1
        assert _reconcile_interrupted_chat_uses(db, now=now) == 0
        db.refresh(use)
        assert use.status == "failed"
        assert use.invalidation_reason == "GENERAL_SKILL_CONSUMPTION_INTERRUPTED"
        events = db.exec(
            select(AgentEvent).where(AgentEvent.aggregate_id == use.id)
        ).all()
        assert [(event.event_type, event.payload_json["code"]) for event in events] == [
            ("skill_use_failed", "GENERAL_SKILL_CONSUMPTION_INTERRUPTED")
        ]


def test_reconciler_preserves_dynamic_and_recent_chat_uses() -> None:
    """维护任务不能终结仍在合理模型窗口内的对话 Use 或归属于 Execution 的 Use。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    now = utc_now()
    with Session(engine) as db:
        rows = [
            GeneralSkillUse(
                id="gsuse_recent",
                tenant_id="tenant_worker",
                session_id="session_worker",
                turn_id="turn_recent",
                agent_id="agent_worker",
                user_id="user_worker",
                skill_id="skill_worker",
                revision_id="revision_worker",
                content_checksum="c" * 64,
                selection_mode="forced",
                status="active",
                idempotency_key="d" * 64,
                updated_at=now,
            ),
            GeneralSkillUse(
                id="gsuse_dynamic_old",
                tenant_id="tenant_worker",
                session_id="session_worker",
                turn_id="turn_dynamic",
                execution_id="execution_worker",
                agent_id="agent_worker",
                user_id="user_worker",
                skill_id="skill_worker",
                revision_id="revision_worker",
                content_checksum="e" * 64,
                selection_mode="forced",
                status="active",
                idempotency_key="f" * 64,
                updated_at=now - timedelta(hours=2),
            ),
        ]
        db.add_all(rows)
        db.commit()

        assert _reconcile_interrupted_chat_uses(db, now=now) == 0
        for row in rows:
            db.refresh(row)
            assert row.status == "active"


def test_reconciler_respects_complete_model_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后一次合法模型尝试仍在进行时不得误杀，越过完整窗口后才幂等收敛。"""

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_api_timeout_seconds", 600.0)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    now = utc_now()
    with Session(engine) as db:
        within_window = GeneralSkillUse(
            id="gsuse_last_legal_attempt",
            tenant_id="tenant_worker",
            session_id="session_worker",
            turn_id="turn_last_legal_attempt",
            agent_id="agent_worker",
            user_id="user_worker",
            skill_id="skill_worker",
            revision_id="revision_worker",
            content_checksum="1" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="2" * 64,
            updated_at=now - timedelta(seconds=1_900),
        )
        beyond_window = GeneralSkillUse(
            id="gsuse_beyond_retry_window",
            tenant_id="tenant_worker",
            session_id="session_worker",
            turn_id="turn_beyond_retry_window",
            agent_id="agent_worker",
            user_id="user_worker",
            skill_id="skill_worker",
            revision_id="revision_worker",
            content_checksum="3" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="4" * 64,
            updated_at=now - timedelta(seconds=1_921),
        )
        db.add_all([within_window, beyond_window])
        db.commit()

        assert _reconcile_interrupted_chat_uses(db, now=now) == 1
        db.refresh(within_window)
        db.refresh(beyond_window)
        assert within_window.status == "active"
        assert beyond_window.status == "failed"
