from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.feedback import get_feedback_session_detail, get_feedback_summary, list_feedback_sessions
from app.api.sessions import get_session_detail, get_session_overview, list_sessions, page_sessions
from app.db.models import AgentEvent, ChatSession, Message, MessageFeedback, Tenant, User


def test_enterprise_sessions_are_limited_to_current_user_even_for_admin() -> None:
    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)

        rows = list_sessions("tenant_demo", agent_id=None, current_user=admin, db=db)

        assert [row["id"] for row in rows] == ["session_admin"]

        with pytest.raises(HTTPException) as exc_info:
            get_session_detail("session_other", "tenant_demo", current_user=admin, db=db)

        assert exc_info.value.status_code == 404


def test_enterprise_feedback_is_limited_to_current_user_even_for_admin() -> None:
    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)
        _seed_feedback_rows(db)

        summary = get_feedback_summary("tenant_demo", agent_id=None, limit=1000, current_user=admin, db=db)
        down_rows = list_feedback_sessions("tenant_demo", "down", agent_id=None, limit=200, current_user=admin, db=db)
        detail = get_feedback_session_detail("session_admin", "tenant_demo", current_user=admin, db=db)

        assert summary["down_count"] == 1
        assert [row["session_id"] for row in down_rows] == ["session_admin"]
        assert detail["session"]["id"] == "session_admin"

        with pytest.raises(HTTPException) as exc_info:
            get_feedback_session_detail("session_other", "tenant_demo", current_user=admin, db=db)

        assert exc_info.value.status_code == 404


def test_enterprise_session_page_filters_feedback_before_counting_and_paging() -> None:
    """验证会话分页先应用本人、员工和反馈条件，再返回准确总数。"""

    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)
        _seed_feedback_rows(db)
        db.add(
            ChatSession(
                id="session_admin_unrated",
                tenant_id="tenant_demo",
                user_id=admin.id,
                agent_id="agent_shared",
                title="未评价会话",
            )
        )
        db.commit()

        down_page = page_sessions(
            tenant_id="tenant_demo",
            agent_id="agent_shared",
            feedback_filter="down",
            page=1,
            page_size=1,
            current_user=admin,
            db=db,
        )
        unrated_page = page_sessions(
            tenant_id="tenant_demo",
            agent_id="agent_shared",
            feedback_filter="unrated",
            page=1,
            page_size=10,
            current_user=admin,
            db=db,
        )

        assert down_page.total == 1
        assert down_page.session_total == 2
        assert down_page.items[0]["id"] == "session_admin"
        assert down_page.items[0]["down_feedback"]["primary_bucket"] == "model_issue"
        assert [item["id"] for item in unrated_page.items] == ["session_admin_unrated"]


def test_enterprise_session_overview_returns_count_and_only_latest_session() -> None:
    """验证员工档案概览无需读取全部会话即可获得总数和最近一条。"""

    with _test_session() as db:
        admin, _other = _seed_session_privacy_rows(db)
        db.add(
            ChatSession(
                id="session_admin_latest",
                tenant_id="tenant_demo",
                user_id=admin.id,
                agent_id="agent_latest",
                title="最近会话",
            )
        )
        db.commit()

        overview = get_session_overview(
            tenant_id="tenant_demo",
            agent_id="agent_latest",
            current_user=admin,
            db=db,
        )

        assert overview.total == 1
        assert overview.latest is not None
        assert overview.latest["id"] == "session_admin_latest"


def _seed_session_privacy_rows(db: Session) -> tuple[User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    admin = User(id="admin_user", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x")
    other = User(id="other_user", tenant_id="tenant_demo", username="other", password_hash="x")
    db.add(admin)
    db.add(other)
    db.add(
        ChatSession(
            id="session_admin",
            tenant_id="tenant_demo",
            user_id=admin.id,
            agent_id="agent_shared",
            title="管理员自己的会话",
        )
    )
    db.add(
        ChatSession(
            id="session_other",
            tenant_id="tenant_demo",
            user_id=other.id,
            agent_id="agent_shared",
            title="其他用户的会话",
        )
    )
    db.add(Message(id="msg_admin_user", tenant_id="tenant_demo", session_id="session_admin", role="user", content="admin question"))
    db.add(
        Message(
            id="msg_admin_assistant",
            tenant_id="tenant_demo",
            session_id="session_admin",
            role="assistant",
            content="admin answer",
        )
    )
    db.add(Message(id="msg_other_user", tenant_id="tenant_demo", session_id="session_other", role="user", content="other question"))
    db.add(
        Message(
            id="msg_other_assistant",
            tenant_id="tenant_demo",
            session_id="session_other",
            role="assistant",
            content="other answer",
        )
    )
    db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_admin", event_type="assistant_message_created", payload_json={}))
    db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_other", event_type="assistant_message_created", payload_json={}))
    db.commit()
    return admin, other


def _seed_feedback_rows(db: Session) -> None:
    db.add(
        MessageFeedback(
            id="feedback_admin",
            tenant_id="tenant_demo",
            session_id="session_admin",
            message_id="msg_admin_assistant",
            user_id="admin_user",
            rating="down",
            analysis_bucket="model_issue",
        )
    )
    db.add(
        MessageFeedback(
            id="feedback_other",
            tenant_id="tenant_demo",
            session_id="session_other",
            message_id="msg_other_assistant",
            user_id="other_user",
            rating="down",
            analysis_bucket="skill_issue",
        )
    )
    db.commit()


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
