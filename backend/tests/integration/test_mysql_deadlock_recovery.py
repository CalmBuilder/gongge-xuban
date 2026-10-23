"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : test_mysql_deadlock_recovery.py
@CallChain  : pytest MySQL → API 幂等读流程 → SQLAlchemy/MySQL 1213 → 单次安全重试
@Description: 复现 MySQL 死锁造成的会话列表和权限目录读取 500。
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pymysql.err import OperationalError as DriverOperationalError
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, create_engine

from app.api import chat as chat_api
from app.api import organization as organization_api
from app.db.models import ChatSession, Tenant, User


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade_isolated_database(database_url: str) -> None:
    """把夹具创建的随机数据库迁移到当前正式 Alembic head。"""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def _mysql_deadlock() -> OperationalError:
    """构造驱动原生错误码 1213，精确模拟可重试的 MySQL 死锁。"""
    return OperationalError(
        "SELECT 1",
        {},
        DriverOperationalError(1213, "Deadlock found when trying to get lock"),
    )


def test_mysql_chat_session_list_recovers_from_one_lifecycle_deadlock(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话列表清理遇到一次 1213 后重读，仍返回持久会话而不产生 500。"""
    _upgrade_isolated_database(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_chat_deadlock_recovery"
    user_id = "user_chat_deadlock_recovery"
    session_id = "session_chat_deadlock_recovery"
    try:
        with Session(engine) as db:
            db.add(Tenant(id=tenant_id, name="会话死锁隔离企业"))
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username="chat_deadlock_recovery",
                    password_hash="synthetic-test-hash",
                )
            )
            db.add(
                ChatSession(
                    id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    title="死锁恢复会话",
                )
            )
            db.commit()
        original = chat_api._cleanup_stale_completed_sessions
        calls = 0

        def fail_once(db: Session, current_tenant_id: str, rows: list[ChatSession]) -> None:
            """只让首次清理抛出原生 1213，后续走正式清理实现。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _mysql_deadlock()
            original(db, current_tenant_id, rows)

        monkeypatch.setattr(chat_api, "_cleanup_stale_completed_sessions", fail_once)
        with Session(engine) as db:
            user = db.get(User, user_id)
            assert user is not None
            result = chat_api.list_chat_sessions(
                tenant_id,
                current_user=user,
                db=db,
            )

        assert calls == 2
        assert [row.id for row in result] == [session_id]
    finally:
        engine.dispose()


def test_mysql_permission_definition_read_recovers_from_catalog_deadlock(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权限目录只读请求遇到一次目录同步 1213 后完整重试并返回正式目录。"""
    _upgrade_isolated_database(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    tenant_id = "tenant_permission_deadlock_recovery"
    try:
        with Session(engine) as db:
            db.add(Tenant(id=tenant_id, name="权限目录死锁隔离企业"))
            user = User(
                id="user_permission_deadlock_recovery",
                tenant_id=tenant_id,
                username="permission_deadlock_recovery",
                password_hash="synthetic-test-hash",
                role="admin",
            )
            db.add(user)
            db.commit()
        monkeypatch.setattr(organization_api, "_ensure_authorization_read", lambda *_: None)
        original = organization_api.ensure_builtin_governance_catalog
        calls = 0

        def fail_once(db: Session, current_tenant_id: str) -> None:
            """只让首次内置目录同步抛出原生 1213，随后走正式同步实现。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _mysql_deadlock()
            original(db, current_tenant_id)

        monkeypatch.setattr(organization_api, "ensure_builtin_governance_catalog", fail_once)
        with Session(engine) as db:
            user = db.get(User, "user_permission_deadlock_recovery")
            assert user is not None
            result = organization_api.list_permission_definitions(
                tenant_id,
                None,
                None,
                "active",
                current_user=user,
                db=db,
            )

        assert calls == 2
        assert result
        assert all(row.status == "active" for row in result)
    finally:
        engine.dispose()
