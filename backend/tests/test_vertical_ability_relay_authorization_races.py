"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_relay_authorization_races.py
@CallChain  : pytest → 隔离 SQLite/MySQL → 独立授权/撤销事务 → Relay 状态条件更新
@Description: 验证迟到心跳、首次绑定及状态过期均不能覆盖已提交撤销，失败绑定不残留连接。
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ManagementAuditLog, User, VerticalAbilityBrowserRelaySession, VerticalAbilityConnection, utc_now,
)
from app.vertical_abilities import relay_service as relay
from app.vertical_abilities.connection_service import revoke_user_connection


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def authorization_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """使用不同物理连接重现事务交错；MySQL 仅迁移随机测试库并保持可重复读。"""

    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        engine = create_engine(url, poolclass=NullPool, isolation_level="REPEATABLE READ")
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'authorization.db'}", poolclass=NullPool)
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(params=["browser_relay", "local_loopback"])
def pending_pairing(
    request: pytest.FixtureRequest, authorization_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, str, str]:
    """构造当前普通用户的待确认配对，令牌仅在测试进程中使用。"""

    monkeypatch.setenv("APP_SECRET", "relay-authorization-fixture-secret")
    with Session(authorization_engine) as db:
        owner = User(
            id="authorization-owner", tenant_id="authorization-tenant",
            username="authorization-owner", role="member", password_hash="unused",
        )
        db.add(owner)
        db.commit()
        pairing, token = relay.create_relay_pairing(
            db, current_user=owner, platform_id="xiaohongshu", account_label="事务测试",
            expected_revision=0, transport=request.param,
        )
        return owner.tenant_id, owner.id, pairing.id, token


def _connect(engine: Engine, pairing: tuple[str, str, str, str]) -> None:
    """由真实认证入口确认设备，并把心跳调旧以覆盖断线重连路径。"""

    with Session(engine) as db:
        session = relay.authenticate_relay_token(db, pairing[3], peer_host="127.0.0.1")
        session.last_seen_at = utc_now() - timedelta(minutes=2)
        db.commit()
        with pytest.raises(relay.BrowserRelayError):
            relay.require_live_relay_session(
                db, tenant_id=pairing[0], user_id=pairing[1], session_id=pairing[2],
            )


@pytest.mark.parametrize("change", ["revoke_pairing", "revoke_connection", "replace_revision"])
def test_late_reconnect_cannot_override_committed_authorization_change(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """绑定检查后独立提交撤销或修订替换，迟到重连须拒绝且旧令牌持续无效。"""

    _connect(authorization_engine, pending_pairing)
    original = relay._require_relay_connection_binding
    changed = False

    def change_after_binding(db: Session, session: VerticalAbilityBrowserRelaySession):
        """保留真实绑定检查，只在写回之前让另一物理连接提交授权变化。"""

        nonlocal changed
        connection = original(db, session)
        if not changed:
            changed = True
            with Session(authorization_engine) as other:
                assert other.connection().connection.dbapi_connection is not (
                    db.connection().connection.dbapi_connection
                )
                if change == "revoke_pairing":
                    relay.revoke_relay_session(
                        other, tenant_id=pending_pairing[0], user_id=pending_pairing[1],
                        session_id=pending_pairing[2],
                    )
                elif change == "revoke_connection":
                    revoke_user_connection(
                        other, current_user=other.get(User, pending_pairing[1]),
                        connection_id=connection.id,
                    )
                else:
                    row = other.get(VerticalAbilityConnection, connection.id)
                    row.revision += 1
                    other.commit()
        return connection

    monkeypatch.setattr(relay, "_require_relay_connection_binding", change_after_binding)
    with Session(authorization_engine) as db:
        with pytest.raises(relay.BrowserRelayError) as rejected:
            relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")
        assert rejected.value.code in {
            "VERTICAL_ABILITY_RELAY_TOKEN_INVALID", "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT",
        }
    assert changed
    with Session(authorization_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pending_pairing[2])
        assert session.status == ("disconnected" if change == "replace_revision" else "revoked")
        with pytest.raises(relay.BrowserRelayError):
            relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")


def test_failed_initial_attachment_rolls_back_connection_and_pairing(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次连接在绑定后失败时，连接和会话关联必须同事务回滚，不能留下已授权假象。"""

    original = relay._attach_relay_connection

    def fail_after_attachment(db: Session, session: VerticalAbilityBrowserRelaySession):
        """真实执行绑定后模拟后续校验失败，不替换绑定本身的业务行为。"""

        original(db, session)
        raise relay.BrowserRelayError("VERTICAL_ABILITY_RELAY_TOKEN_INVALID", "测试拒绝")

    monkeypatch.setattr(relay, "_attach_relay_connection", fail_after_attachment)
    with Session(authorization_engine) as db:
        with pytest.raises(relay.BrowserRelayError):
            relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")
    with Session(authorization_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pending_pairing[2])
        assert session.status == "pending"
        assert session.connection_id is None
        assert db.exec(select(VerticalAbilityConnection)).all() == []
        assert db.exec(select(ManagementAuditLog)).all() == []


def test_stale_offline_detection_cannot_replace_revocation(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳陈旧检查与用户撤销交错，失活标记不能把 revoked 改成可重连的 disconnected。"""

    with Session(authorization_engine) as db:
        session = relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")
        session.last_seen_at = utc_now() - timedelta(minutes=2)
        db.commit()
    original = relay.relay_session_is_live

    def revoke_after_liveness_check(session: VerticalAbilityBrowserRelaySession) -> bool:
        """在已算出失活但未写回时，通过独立事务完成用户撤销。"""

        result = original(session)
        assert result is False
        with Session(authorization_engine) as other:
            relay.revoke_relay_session(
                other, tenant_id=pending_pairing[0], user_id=pending_pairing[1],
                session_id=pending_pairing[2],
            )
        return result

    monkeypatch.setattr(relay, "relay_session_is_live", revoke_after_liveness_check)
    with Session(authorization_engine) as db:
        with pytest.raises(relay.BrowserRelayError):
            relay.require_live_relay_session(
                db, tenant_id=pending_pairing[0], user_id=pending_pairing[1],
                session_id=pending_pairing[2],
            )
    with Session(authorization_engine) as db:
        assert db.get(VerticalAbilityBrowserRelaySession, pending_pairing[2]).status == "revoked"


def test_stale_offline_detection_preserves_a_new_heartbeat(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失活检查依据已被新心跳替代时不标记掉线，而应结束旧快照后返回最新在线状态。"""

    with Session(authorization_engine) as db:
        session = relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")
        session.last_seen_at = utc_now() - timedelta(minutes=2)
        db.commit()
    original = relay.relay_session_is_live
    renewed = False

    def heartbeat_after_check(session: VerticalAbilityBrowserRelaySession) -> bool:
        """第一次失活判定后提交新心跳，后续重读保持真实判断函数。"""

        nonlocal renewed
        result = original(session)
        if not renewed:
            assert result is False
            renewed = True
            with Session(authorization_engine) as other:
                relay.authenticate_relay_token(other, pending_pairing[3], peer_host="127.0.0.1")
        return result

    monkeypatch.setattr(relay, "relay_session_is_live", heartbeat_after_check)
    with Session(authorization_engine) as db:
        session = relay.require_live_relay_session(
            db, tenant_id=pending_pairing[0], user_id=pending_pairing[1], session_id=pending_pairing[2],
        )
        assert session.status == "connected"
        assert original(session)


def test_expiration_cannot_override_a_committed_revocation(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期判定读取旧 pending 状态后用户撤销，过期写回不得覆盖撤销终态。"""

    with Session(authorization_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, pending_pairing[2])
        session.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    original_clock = relay.utc_now
    revoked = False

    def revoke_before_expiration_check():
        """在首次期限比较时提交撤销；内部撤销的时钟读取不再重复注入。"""

        nonlocal revoked
        if not revoked:
            revoked = True
            with Session(authorization_engine) as other:
                relay.revoke_relay_session(
                    other, tenant_id=pending_pairing[0], user_id=pending_pairing[1],
                    session_id=pending_pairing[2],
                )
        return original_clock()

    monkeypatch.setattr(relay, "utc_now", revoke_before_expiration_check)
    with Session(authorization_engine) as db:
        session = relay.get_user_relay_session(
            db, tenant_id=pending_pairing[0], user_id=pending_pairing[1], session_id=pending_pairing[2],
        )
        assert session.status == "revoked"
    with Session(authorization_engine) as db:
        assert db.get(VerticalAbilityBrowserRelaySession, pending_pairing[2]).status == "revoked"


def test_explicit_repair_after_revocation_keeps_monotonic_connection_revision(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
) -> None:
    """用户主动重新配对可恢复使用，旧令牌仍失效，账号修订只能递增而不能重置。"""

    _connect(authorization_engine, pending_pairing)
    with Session(authorization_engine) as db:
        owner = db.get(User, pending_pairing[1])
        connection = db.exec(select(VerticalAbilityConnection)).one()
        transport = connection.transport
        revoked = revoke_user_connection(db, current_user=owner, connection_id=connection.id)
        revoked_revision = revoked.revision
        pairing, token = relay.create_relay_pairing(
            db, current_user=owner, platform_id="xiaohongshu", account_label="事务测试",
            expected_revision=0, transport=transport,
        )
        paired = relay.authenticate_relay_token(db, token, peer_host="127.0.0.1")
        db.refresh(connection)
        assert paired.id == pairing.id
        assert paired.status == "connected"
        assert connection.revision == revoked_revision + 1
        assert paired.expected_revision == connection.revision - 1
    with Session(authorization_engine) as db:
        assert relay.authenticate_relay_token(db, token, peer_host="127.0.0.1").status == "connected"
        with pytest.raises(relay.BrowserRelayError):
            relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")


def test_two_initial_heartbeats_create_only_one_binding(
    authorization_engine: Engine, pending_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个独立连接同时确认同一配对时最多创建一次绑定，冲突结构化且下一次心跳可重试。"""

    barrier = Barrier(2, timeout=10)
    original = relay._attach_relay_connection

    def attach_together(db: Session, session: VerticalAbilityBrowserRelaySession):
        """让两个真实事务均读到 pending 后再竞争连接唯一键和会话 CAS。"""

        barrier.wait()
        return original(db, session)

    def heartbeat() -> str:
        """独立数据库会话只接受成功或稳定冲突错误，不吞掉数据库异常。"""

        with Session(authorization_engine) as db:
            try:
                relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1")
                return "connected"
            except relay.BrowserRelayError as exc:
                return exc.code

    with monkeypatch.context() as patched:
        patched.setattr(relay, "_attach_relay_connection", attach_together)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(heartbeat) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
    assert "connected" in results
    assert set(results) <= {"connected", "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT", "VERTICAL_ABILITY_RELAY_TOKEN_INVALID"}
    with Session(authorization_engine) as db:
        rows = db.exec(select(VerticalAbilityConnection)).all()
        assert len(rows) == 1
        assert rows[0].revision == 1
        assert len(db.exec(select(ManagementAuditLog)).all()) == 1
        assert relay.authenticate_relay_token(db, pending_pairing[3], peer_host="127.0.0.1").status == "connected"
