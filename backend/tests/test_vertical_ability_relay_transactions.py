"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_relay_transactions.py
@CallChain  : pytest → 隔离 SQLite/MySQL → Relay 生产服务 → 独立连接器事务
@Description: 验证短事务轮询、超时与完成竞争、租约回执和用户网站授权边界。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    User,
    VerticalAbilityBrowserRelayCommand,
    VerticalAbilityBrowserRelaySession,
    VerticalAbilityConnection,
    utc_now,
)
from app.vertical_abilities import relay_service as relay
from app.vertical_abilities.connection_service import (
    VerticalAbilityConnectionError,
    verify_user_connection,
)


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def relay_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """仅在随机测试数据库迁移建表，MySQL 保留默认可重复读以暴露旧快照缺陷。"""

    if request.param == "mysql":
        database_url = request.getfixturevalue("mysql_database_url")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = database_url
        alembic_command.upgrade(config, "head")
        engine = create_engine(database_url, isolation_level="REPEATABLE READ", echo=False)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'relay-transactions.db'}")
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def live_pairing(
    relay_engine: Engine, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> tuple[str, str, str, str]:
    """用显式设备令牌及同机来源建立测试连接，可参数化实际拓扑，不读取真实账号。"""

    monkeypatch.setenv("APP_SECRET", "relay-transaction-fixture-secret")
    user = User(
        id="relay-transaction-owner", tenant_id="relay-transaction-tenant",
        username="relay-transaction-owner", role="member", password_hash="unused",
    )
    with Session(relay_engine, expire_on_commit=False) as db:
        db.add(user)
        db.commit()
        pairing, token = relay.create_relay_pairing(
            db, current_user=user, platform_id="xiaohongshu",
            account_label="事务测试账号", expected_revision=0,
            transport=getattr(request, "param", "browser_relay"),
        )
        relay.authenticate_relay_token(db, token, peer_host="127.0.0.1")
        return user.tenant_id, user.id, pairing.id, token


def _wait(db: Session, pairing: tuple[str, str, str, str], **kwargs: object) -> bytes:
    """通过真实入队等待接口执行一个固定小红书读取，允许测试覆盖部分参数。"""

    tenant_id, user_id, session_id, _token = pairing
    parameters: dict[str, object] = {
        "tenant_id": tenant_id, "user_id": user_id, "session_id": session_id,
        "platform_id": "xiaohongshu", "action": "search",
        "positional_args": ["fixture research"], "limit": 3, "timeout_seconds": 5,
    }
    parameters.update(kwargs)
    return relay.enqueue_and_wait_for_relay_command(db, **parameters)


def _complete_from_connector(engine: Engine, token: str) -> None:
    """用另一个真实数据库会话领取租约并提交有界正文，不共享等待端事务。"""

    with Session(engine) as connector_db:
        command = relay.claim_next_relay_command(
            connector_db, pairing_token=token, peer_host="127.0.0.1",
        )
        assert command is not None
        completed = relay.complete_relay_command(
            connector_db, pairing_token=token, command_id=command["command_id"],
            lease_token=command["lease_token"], success=True, output="fixture result",
            peer_host="127.0.0.1",
        )
        assert completed.status == "succeeded"


def test_relay_wait_refreshes_transaction_after_each_poll(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接器在首次读取后提交成功，下一轮必须读取新事务而非可重复读旧快照。"""

    polls = 0

    def finish_after_first_poll(_seconds: float) -> None:
        """在等待间隔提交成功，若仍然等待则立即失败，避免用真实超时洗掉缺陷。"""

        nonlocal polls
        polls += 1
        assert polls == 1, "等待端未看到另一事务已经提交的成功结果"
        _complete_from_connector(relay_engine, live_pairing[3])

    monkeypatch.setattr(relay.time, "sleep", finish_after_first_poll)
    with Session(relay_engine) as db:
        assert _wait(db, live_pairing) == b"fixture result"
    assert polls == 1


@pytest.mark.parametrize("live_pairing", ["local_loopback"], indirect=True)
def test_local_pairing_uses_authorized_queue_in_both_database_dialects(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机模式在完整迁移后的 SQLite/MySQL 都通过设备队列执行，模式不得伪装成远端。"""

    with Session(relay_engine) as db:
        session = db.get(VerticalAbilityBrowserRelaySession, live_pairing[2])
        connection = db.get(VerticalAbilityConnection, session.connection_id)
        assert session.transport == connection.transport == "local_loopback"

    def complete_local(_seconds: float) -> None:
        """通过独立连接器事务领取并回传本机平台数据，等待端不得直接调用全局 OpenCLI。"""

        _complete_from_connector(relay_engine, live_pairing[3])

    monkeypatch.setattr(relay.time, "sleep", complete_local)
    with Session(relay_engine) as db:
        assert _wait(db, live_pairing) == b"fixture result"


def test_relay_timeout_compare_and_set_preserves_concurrent_success(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待端读取未完成状态后成功回执抢先提交，超时更新不能覆盖已经成功的结果。"""

    armed = False
    completed = False
    future = utc_now() + timedelta(minutes=1)

    def prepare_race(_seconds: float) -> None:
        """首次轮询后布置竞争，下一次检查截止时间时插入独立成功事务。"""

        nonlocal armed
        assert not armed and not completed
        armed = True

    def race_clock():
        """在旧行已读取后先提交真实连接器回执，再让等待端发现截止时间已过。"""

        nonlocal armed, completed
        if armed:
            armed = False
            _complete_from_connector(relay_engine, live_pairing[3])
            completed = True
        return future if completed else utc_now()

    monkeypatch.setattr(relay.time, "sleep", prepare_race)
    monkeypatch.setattr(relay, "utc_now", race_clock)
    with Session(relay_engine) as db:
        assert _wait(db, live_pairing) == b"fixture result"
    with Session(relay_engine) as observer:
        command = observer.exec(select(VerticalAbilityBrowserRelayCommand)).one()
        assert command.status == "succeeded"
        assert command.error_code is None


def test_relay_completion_compare_and_set_preserves_concurrent_cancel(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回执读取租约后命令被取消，迟到回执不得将取消覆盖为成功。"""

    original_hash = relay._hash_token
    cancellation_written = False
    with Session(relay_engine) as db:
        row = VerticalAbilityBrowserRelayCommand(
            relay_session_id=live_pairing[2], tenant_id=live_pairing[0], user_id=live_pairing[1],
            platform_id="xiaohongshu", action="search", arguments_json='["fixture"]',
            deadline_at=utc_now() + timedelta(minutes=1),
        )
        db.add(row)
        db.commit()
        command = relay.claim_next_relay_command(db, pairing_token=live_pairing[3])
        assert command is not None

        def cancel_during_lease_check(value: str) -> str:
            """在回执已读到 leased 状态后插入独立取消事务，精确覆盖 CAS 竞争窗口。"""

            nonlocal cancellation_written
            if value == command["lease_token"] and not cancellation_written:
                cancellation_written = True
                with Session(relay_engine) as cancelling:
                    relay._cancel_relay_commands(
                        cancelling, live_pairing[2], now=utc_now(),
                        error_code="VERTICAL_ABILITY_RELAY_COMMAND_CANCELLED",
                        error_message="测试取消。",
                    )
                    cancelling.commit()
            return original_hash(value)

        monkeypatch.setattr(relay, "_hash_token", cancel_during_lease_check)
        result = relay.complete_relay_command(
            db, pairing_token=live_pairing[3], command_id=command["command_id"],
            lease_token=command["lease_token"], success=True, output="late result",
        )
        assert cancellation_written
        assert result.status == "cancelled"
        assert result.result_json is None


def test_relay_cannot_enqueue_another_platform_under_owned_pairing(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权小红书设备不等于授权同设备其他平台，平台不匹配必须在入队前拒绝。"""

    def unexpected_wait(_seconds: float) -> None:
        """任何进入等待的行为都表明错误平台已绕过授权检查。"""

        pytest.fail("其他平台命令不应进入设备队列")

    monkeypatch.setattr(relay.time, "sleep", unexpected_wait)
    with Session(relay_engine) as db:
        with pytest.raises(relay.BrowserRelayError) as rejected:
            _wait(db, live_pairing, platform_id="reddit")
        assert rejected.value.code == "VERTICAL_ABILITY_RELAY_PLATFORM_MISMATCH"
        assert db.exec(select(VerticalAbilityBrowserRelayCommand)).all() == []


def test_relay_heartbeat_cannot_reuse_a_replaced_connection(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
) -> None:
    """连接修订已由新凭据替换时，旧设备令牌心跳不能重新授予访问权。"""

    with Session(relay_engine) as db:
        connection = db.exec(select(VerticalAbilityConnection)).one()
        connection.revision += 1
        db.commit()
    with Session(relay_engine) as db:
        with pytest.raises(relay.BrowserRelayError) as rejected:
            relay.authenticate_relay_token(db, live_pairing[3])
        assert rejected.value.code == "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT"


def test_relay_reconnect_recovers_health_without_reauthorizing_other_accounts(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
) -> None:
    """暂时离线不是授权撤销，同一设备令牌恢复后应重新健康而不必人为解绑重建。"""

    with Session(relay_engine) as db:
        pairing = db.get(VerticalAbilityBrowserRelaySession, live_pairing[2])
        pairing.last_seen_at = utc_now() - timedelta(minutes=2)
        connection = db.exec(select(VerticalAbilityConnection)).one()
        connection_id = connection.id
        user = db.get(User, live_pairing[1])
        db.commit()
        with pytest.raises(VerticalAbilityConnectionError):
            verify_user_connection(
                db, current_user=user, connection_id=connection_id,
                settings=None, resolver=None,
            )
    with Session(relay_engine) as db:
        session = relay.authenticate_relay_token(db, live_pairing[3])
        connection = db.get(VerticalAbilityConnection, connection_id)
        assert session.status == "connected"
        assert connection.status == "connected"
        assert connection.health_status == "healthy"
        assert connection.revision == 1


@pytest.mark.parametrize("relay_engine", ["sqlite"], indirect=True)
def test_relay_claim_cannot_resurrect_concurrent_cancellation(
    relay_engine: Engine, live_pairing: tuple[str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite 不提供行锁，候选读取后被取消时，领取 CAS 不能重新变回 leased。"""

    original_hash = relay._hash_token
    cancellation_written = False
    with Session(relay_engine) as db:
        command = VerticalAbilityBrowserRelayCommand(
            relay_session_id=live_pairing[2], tenant_id=live_pairing[0], user_id=live_pairing[1],
            platform_id="xiaohongshu", action="search", arguments_json='["fixture"]',
            deadline_at=utc_now() + timedelta(minutes=1),
        )
        db.add(command)
        db.commit()

        def cancel_before_lease_write(value: str) -> str:
            """在新租约哈希生成时先提交取消，覆盖 SQLite 缺少行锁时的竞争窗口。"""

            nonlocal cancellation_written
            if value != live_pairing[3] and not cancellation_written:
                cancellation_written = True
                with Session(relay_engine) as cancelling:
                    relay._cancel_relay_commands(
                        cancelling, live_pairing[2], now=utc_now(),
                        error_code="VERTICAL_ABILITY_RELAY_COMMAND_CANCELLED",
                        error_message="测试取消。",
                    )
                    cancelling.commit()
            return original_hash(value)

        monkeypatch.setattr(relay, "_hash_token", cancel_before_lease_write)
        assert relay.claim_next_relay_command(db, pairing_token=live_pairing[3]) is None
        db.expire_all()
        assert db.exec(select(VerticalAbilityBrowserRelayCommand)).one().status == "cancelled"
