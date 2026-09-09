"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : test_pi_event_query_mysql.py
@CallChain  : pytest mysql → Alembic 临时库 → replay event query helper → MySQL 8.4
@Description: 验证 PI-2B JSON 身份、大小写、高水位和 keyset 分页在 MySQL 中等价。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
from math import ceil
from pathlib import Path
from threading import Event, Thread
import json
import statistics
import time
import tracemalloc

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, inspect
from sqlmodel import Session, select

from app.api import chat as chat_api
from app.api.chat import _existing_turn_replay, _existing_turn_replay_in_snapshot, _relay_event_payload
from app.db.models import (
    AgentEvent,
    ExecutionPublication,
    ExecutionResult,
    ExecutionArtifact,
    Message,
    SopInstance,
    SopOperation,
    SopOperationAttempt,
)
from app.observability.event_log import EventLog
from app.observability.turn_event_query import (
    TURN_EVENT_PAGE_SIZE,
    all_turn_events,
    latest_received_event,
    partial_turn_events,
    session_high_watermark,
    turn_identity_filter,
    turn_events_after_cursor,
)
from app.session.session_schema import ChatTurnRequest


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _current_head() -> str:
    """读取当前唯一 Alembic head，避免 MySQL 测试依赖过期修订号。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def _upgrade(url: str, revision: str | None = None) -> None:
    """把隔离 MySQL 测试库升级到指定修订或当前 head，不用 create_all 替代迁移。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, revision or _current_head())


def test_pi_b_g1_mysql_json_identity_and_keyset(mysql_database_url: str) -> None:
    """MySQL 8.4 使用二进制身份比较，目标跨页且不读取 H 之后事件。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    started_at = datetime(2026, 9, 6, 9, 5, 0)
    try:
        created_at_column = next(
            column
            for column in inspect(engine).get_columns("agent_events")
            if column["name"] == "created_at"
        )
        assert getattr(created_at_column["type"], "fsp", None) == 6
        with Session(engine) as db:
            db.add(
                Message(
                    id="mysql_message_pi",
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    role="user",
                    content="MySQL replay",
                    metadata_json={"client_turn_id": "client_mysql_pi"},
                )
            )
            db.add(
                AgentEvent(
                    id="mysql_event_received",
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    event_type="user_message_received",
                    payload_json={
                        "message_id": "mysql_message_pi",
                        "client_turn_id": "\u3000client_mysql_pi\u3000",
                    },
                    created_at=started_at,
                )
            )
            for index in range(TURN_EVENT_PAGE_SIZE + 5):
                db.add(
                    AgentEvent(
                        id=f"mysql_event_delta_{index:03d}",
                        tenant_id="tenant_mysql_pi",
                        session_id="session_mysql_pi",
                        event_type="stream_delta",
                        payload_json={
                            "user_message_id": "mysql_message_pi",
                            "client_turn_id": "client_mysql_pi",
                            "content": str(index),
                        },
                        created_at=started_at + timedelta(microseconds=index + 1),
                    )
                )
            db.add(
                AgentEvent(
                    id="mysql_event_complete",
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    event_type="complete",
                    payload_json={
                        "message_id": "mysql_message_pi",
                        "client_turn_id": "client_mysql_pi",
                    },
                    created_at=started_at + timedelta(microseconds=TURN_EVENT_PAGE_SIZE + 6),
                )
            )
            db.add(
                AgentEvent(
                    id="mysql_event_span",
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    event_type="llm_call_finished",
                    payload_json={"turn_id": "mysql_message_pi"},
                    created_at=started_at + timedelta(microseconds=TURN_EVENT_PAGE_SIZE + 7),
                )
            )
            db.commit()

            high_watermark = session_high_watermark(db, "tenant_mysql_pi", "session_mysql_pi")
            assert high_watermark is not None
            assert latest_received_event(
                db,
                "tenant_mysql_pi",
                "session_mysql_pi",
                "client_mysql_pi",
                high_watermark,
            ) is not None
            assert latest_received_event(
                db,
                "tenant_mysql_pi",
                "session_mysql_pi",
                "CLIENT_MYSQL_PI",
                high_watermark,
            ) is None
            replay = _existing_turn_replay(
                db,
                ChatTurnRequest(
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    user_id="user_mysql_pi",
                    message="MySQL replay",
                    client_turn_id="client_mysql_pi",
                ),
            )
            assert replay is not None
            assert len(replay[1]) == TURN_EVENT_PAGE_SIZE + 7
            assert replay[3] is True
            snapshot_replay = _existing_turn_replay_in_snapshot(
                db,
                ChatTurnRequest(
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    user_id="user_mysql_pi",
                    message="MySQL replay",
                    client_turn_id="client_mysql_pi",
                ),
            )
            assert snapshot_replay is not None
            assert len(snapshot_replay[1]) == TURN_EVENT_PAGE_SIZE + 7
            assert _relay_event_payload(snapshot_replay[1][-1])[0] == "complete"

            for index, padding in enumerate(
                (" ", "\t", "\r", "\n", "\u00a0", "\u3000", "\x1c", "\x1d", "\x1e", "\x1f")
            ):
                identity_session = f"session_mysql_identity_{index}"
                identity_message = f"message_mysql_identity_{index}"
                identity = f"client_mysql_identity_{index}"
                db.add(
                    Message(
                        id=identity_message,
                        tenant_id="tenant_mysql_pi",
                        session_id=identity_session,
                        role="user",
                        content="identity",
                        metadata_json={"client_turn_id": identity},
                    )
                )
                db.add(
                    AgentEvent(
                        id=f"mysql_event_identity_{index}",
                        tenant_id="tenant_mysql_pi",
                        session_id=identity_session,
                        event_type="user_message_received",
                        payload_json={
                            "message_id": identity_message,
                            "client_turn_id": f"{padding}{identity}{padding}",
                        },
                        created_at=started_at,
                    )
                )
            db.commit()
            for index in range(10):
                identity_session = f"session_mysql_identity_{index}"
                identity = f"client_mysql_identity_{index}"
                identity_h = session_high_watermark(db, "tenant_mysql_pi", identity_session)
                assert identity_h is not None
                assert latest_received_event(
                    db,
                    "tenant_mysql_pi",
                    identity_session,
                    identity,
                    identity_h,
                ) is not None

            db.add(
                AgentEvent(
                    id="mysql_event_late",
                    tenant_id="tenant_mysql_pi",
                    session_id="session_mysql_pi",
                    event_type="complete",
                    payload_json={
                        "turn_id": "mysql_message_pi",
                        "client_turn_id": "client_mysql_pi",
                    },
                    created_at=started_at + timedelta(seconds=1),
                )
            )
            db.commit()
            assert all(
                row.id != "mysql_event_late"
                for row in all_turn_events(
                    db,
                    "tenant_mysql_pi",
                    "session_mysql_pi",
                    {"mysql_message_pi", "client_mysql_pi"},
                    high_watermark,
                )
            )
    finally:
        engine.dispose()


def test_pi_b_g1_mysql_precision_migration_preserves_existing_event_times(
    mysql_database_url: str,
) -> None:
    """从当前业务库前一版升级时保留旧秒级值，并让新事件获得微秒排序精度。"""

    _upgrade(mysql_database_url, "20260904_0081")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    old_time = datetime(2026, 9, 6, 9, 10, 0, 123456)
    try:
        with Session(engine) as db:
            db.add(
                AgentEvent(
                    id="mysql_precision_existing",
                    tenant_id="tenant_mysql_precision",
                    session_id="session_mysql_precision",
                    event_type="stream_delta",
                    payload_json={"turn_id": "message_mysql_precision", "content": "old"},
                    created_at=old_time,
                )
            )
            db.commit()
            stored_old_time = db.get(AgentEvent, "mysql_precision_existing").created_at
        assert stored_old_time == old_time.replace(microsecond=0)
    finally:
        engine.dispose()

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    new_time = datetime(2026, 9, 6, 9, 10, 1, 654321)
    try:
        created_at_column = next(
            column
            for column in inspect(engine).get_columns("agent_events")
            if column["name"] == "created_at"
        )
        assert getattr(created_at_column["type"], "fsp", None) == 6
        with Session(engine) as db:
            db.add(
                AgentEvent(
                    id="mysql_precision_new",
                    tenant_id="tenant_mysql_precision",
                    session_id="session_mysql_precision",
                    event_type="stream_delta",
                    payload_json={"turn_id": "message_mysql_precision", "content": "new"},
                    created_at=new_time,
                )
            )
            db.commit()
            stored_new_time = db.get(AgentEvent, "mysql_precision_new").created_at
        assert stored_new_time == new_time
    finally:
        engine.dispose()


def test_pi_b_g1_mysql_json_identity_rejects_non_equivalent_values(
    mysql_database_url: str,
) -> None:
    """MySQL 二进制身份条件与 Python oracle 一致拒绝组合 Unicode 和非字符串值。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    cases = (
        ("Target", "target"),
        ("e\u0301", "é"),
        (None, "target"),
        ("", "target"),
        (False, "False"),
        ([], "[]"),
        ({}, "{}"),
    )
    try:
        with Session(engine) as db:
            for index, (stored_value, _requested_value) in enumerate(cases):
                db.add(
                    AgentEvent(
                        id=f"mysql_identity_event_{index}",
                        tenant_id="tenant_mysql_identity",
                        session_id=f"session_mysql_identity_{index}",
                        event_type="user_message_received",
                        payload_json={
                            "message_id": f"message_mysql_identity_{index}",
                            "client_turn_id": stored_value,
                        },
                        created_at=datetime(2026, 9, 6, 9, 11, index),
                    )
                )
            db.commit()
            for index, (_stored_value, requested_value) in enumerate(cases):
                high_watermark = session_high_watermark(
                    db,
                    "tenant_mysql_identity",
                    f"session_mysql_identity_{index}",
                )
                assert high_watermark is not None
                assert latest_received_event(
                    db,
                    "tenant_mysql_identity",
                    f"session_mysql_identity_{index}",
                    requested_value,
                    high_watermark,
                ) is None
    finally:
        engine.dispose()


def test_pi_b_g1_mysql_live_overlap_reads_same_timestamp_late_id(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL 实时 relay 能补读同刻低 ID 迟到事件，并排除已发送事件。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    started_at = datetime(2026, 9, 6, 9, 12, 0)
    session_id = "session_mysql_live_overlap"
    message_id = "message_mysql_live_overlap"
    client_turn_id = "client_mysql_live_overlap"
    try:
        with Session(engine) as db:
            db.add_all(
                [
                    AgentEvent(
                        id="mysql_live_event_000001",
                        tenant_id="tenant_mysql_live",
                        session_id=session_id,
                        event_type="user_message_received",
                        payload_json={
                            "message_id": message_id,
                            "client_turn_id": client_turn_id,
                        },
                        created_at=started_at,
                    ),
                    AgentEvent(
                        id="mysql_live_event_000002",
                        tenant_id="tenant_mysql_live",
                        session_id=session_id,
                        event_type="stream_delta",
                        payload_json={
                            "turn_id": message_id,
                            "client_turn_id": client_turn_id,
                            "content": "前",
                        },
                        created_at=started_at + timedelta(seconds=1),
                    ),
                ]
            )
            db.commit()
            high_watermark = session_high_watermark(
                db,
                "tenant_mysql_live",
                session_id,
            )
            assert high_watermark is not None
            initial_rows = all_turn_events(
                db,
                "tenant_mysql_live",
                session_id,
                (message_id, client_turn_id),
                high_watermark,
            )
        with Session(engine) as writer_db:
            writer_db.add_all(
                [
                    AgentEvent(
                        id="mysql_live_event_000000-older",
                        tenant_id="tenant_mysql_live",
                        session_id=session_id,
                        event_type="stream_delta",
                        payload_json={
                            "turn_id": message_id,
                            "client_turn_id": client_turn_id,
                            "content": "早于游标迟到",
                        },
                        created_at=started_at + timedelta(milliseconds=500),
                    ),
                    AgentEvent(
                        id="mysql_live_event_000000-late",
                        tenant_id="tenant_mysql_live",
                        session_id=session_id,
                        event_type="stream_delta",
                        payload_json={
                            "turn_id": message_id,
                            "client_turn_id": client_turn_id,
                            "content": "迟到",
                        },
                        created_at=started_at,
                    ),
                    AgentEvent(
                        id="mysql_live_event_tail",
                        tenant_id="tenant_mysql_live",
                        session_id=session_id,
                        event_type="complete",
                        payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
                        created_at=started_at + timedelta(seconds=2),
                    ),
                ]
            )
            writer_db.commit()
        with Session(engine) as db:
            strict_rows = turn_events_after_cursor(
                db,
                "tenant_mysql_live",
                session_id,
                (message_id, client_turn_id),
                high_watermark,
            )
            rows = turn_events_after_cursor(
                db,
                "tenant_mysql_live",
                session_id,
                (message_id, client_turn_id),
                high_watermark,
                include_same_timestamp=True,
                exclude_event_ids={row.id for row in initial_rows},
            )
            overlap_rows = turn_events_after_cursor(
                db,
                "tenant_mysql_live",
                session_id,
                (message_id, client_turn_id),
                high_watermark,
                include_same_timestamp=True,
                overlap_since=started_at,
                exclude_event_ids={row.id for row in initial_rows},
            )
        monkeypatch.setattr(chat_api, "engine", engine)
        relay_frames = list(
            chat_api._stream_existing_turn(
                ChatTurnRequest(
                    tenant_id="tenant_mysql_live",
                    session_id=session_id,
                    agent_id="agent_mysql_live",
                    user_id="user_mysql_live",
                    message="MySQL relay overlap",
                    client_turn_id=client_turn_id,
                ),
                message_id=message_id,
                initial_rows=initial_rows,
                initial_cursor=high_watermark,
                terminal=False,
            )
        )
    finally:
        engine.dispose()

    assert [row.id for row in rows] == [
        "mysql_live_event_tail",
    ]
    assert [row.id for row in overlap_rows] == [
        "mysql_live_event_000000-late",
        "mysql_live_event_000000-older",
        "mysql_live_event_tail",
    ]
    assert [row.id for row in strict_rows] == ["mysql_live_event_tail"]
    assert [
        frame.split("event: ", 1)[1].split("\n", 1)[0]
        for frame in relay_frames
    ] == [
        "user_message_received",
        "stream_delta",
        "stream_delta",
        "stream_delta",
        "complete",
    ]
    assert sum('"content": "迟到"' in frame for frame in relay_frames) == 1
    assert sum("早于游标迟到" in frame for frame in relay_frames) == 1


@pytest.mark.parametrize(
    "chunk_count",
    [0, 1, 199, 200, 201, 999, 1000, 1001, 5001],
    ids=lambda value: f"mysql-target-chunks-{value}",
)
def test_pi_b_x2_mysql_all_target_page_boundaries_are_complete(
    mysql_database_url: str,
    chunk_count: int,
) -> None:
    """MySQL 8.4 逐项验证 K 边界的 replay 与 partial keyset 分页完整性。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    started_at = datetime(2026, 9, 6, 9, 13, 0)
    tenant_id = "tenant_mysql_page_boundary"
    session_id = f"session_mysql_page_boundary_{chunk_count}"
    message_id = f"message_mysql_page_boundary_{chunk_count}"
    client_turn_id = f"client_mysql_page_boundary_{chunk_count}"
    events = [
        AgentEvent(
            id=f"mysql_page_{chunk_count}_received",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="user_message_received",
            payload_json={"message_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at,
        )
    ]
    events.extend(
        AgentEvent(
            id=f"mysql_page_{chunk_count}_delta_{index:04d}",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="stream_delta",
            payload_json={
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": str(index),
            },
            created_at=started_at + timedelta(microseconds=index + 1),
        )
        for index in range(chunk_count)
    )
    events.append(
        AgentEvent(
            id=f"mysql_page_{chunk_count}_complete",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="complete",
            payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at + timedelta(microseconds=chunk_count + 1),
        )
    )
    try:
        with Session(engine) as db:
            db.add_all(events)
            db.add(
                Message(
                    id=message_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    role="user",
                    content="MySQL 分页边界",
                    metadata_json={"client_turn_id": client_turn_id},
                )
            )
            db.commit()
            high_watermark = session_high_watermark(db, tenant_id, session_id)
            assert high_watermark is not None
            rows = all_turn_events(
                db,
                tenant_id,
                session_id,
                (message_id, client_turn_id),
                high_watermark,
            )
            partial_rows = partial_turn_events(db, tenant_id, session_id, message_id)
            replay_request = ChatTurnRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                agent_id="agent_mysql_page_boundary",
                user_id="user_mysql_page_boundary",
                message="MySQL 分页边界",
                client_turn_id=client_turn_id,
            )
            replay = _existing_turn_replay(db, replay_request)
            snapshot_replay = _existing_turn_replay_in_snapshot(db, replay_request)
    finally:
        engine.dispose()

    assert len(rows) == chunk_count + 2
    assert len(partial_rows) == chunk_count
    assert len({row.id for row in rows}) == len(rows)
    assert [
        (row.created_at, row.id) for row in rows
    ] == sorted((row.created_at, row.id) for row in rows)
    assert [
        (row.payload_json or {}).get("content") for row in partial_rows
    ] == [str(index) for index in range(chunk_count)]
    assert rows[0].event_type == "user_message_received"
    assert rows[-1].event_type == "complete"
    assert replay is not None
    assert snapshot_replay is not None
    assert [row.id for row in replay[1]] == [row.id for row in rows]
    assert [row.id for row in snapshot_replay[1]] == [row.id for row in rows]
    assert replay[3] is True
    assert snapshot_replay[3] is True


def test_pi_b_g0_mysql_eventlog_snapshot_excludes_post_h_commit(mysql_database_url: str) -> None:
    """MySQL 独立读事务在真实 EventLog 提交后仍只返回固定 H 内的事件。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    writer_can_commit = Event()
    writer_committed = Event()
    writer_errors: list[BaseException] = []
    try:
        with Session(engine) as writer_db:
            EventLog(writer_db).record(
                "tenant_mysql_g0",
                "session_mysql_g0",
                "user_message_received",
                {"message_id": "message_mysql_g0", "client_turn_id": "client_mysql_g0"},
            )
            writer_db.commit()

        def writer_two() -> None:
            """在读事务固定 H 后，使用独立 EventLog 提交终态事件。"""

            try:
                writer_can_commit.wait(timeout=5)
                # 让事件创建时间明确晚于读事务已经取得的 H，避免把生产时序退化成手工同刻时间。
                time.sleep(0.01)
                with Session(engine) as writer_db:
                    EventLog(writer_db).record(
                        "tenant_mysql_g0",
                        "session_mysql_g0",
                        "complete",
                        {"turn_id": "message_mysql_g0", "client_turn_id": "client_mysql_g0"},
                    )
                    writer_db.commit()
                    writer_committed.set()
            except BaseException as exc:  # pragma: no cover - 把线程异常传回主断言
                writer_errors.append(exc)
                writer_committed.set()

        worker = Thread(target=writer_two)
        worker.start()
        reader_connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        try:
            with reader_connection.begin():
                with Session(bind=reader_connection, expire_on_commit=False) as reader_db:
                    high_watermark = session_high_watermark(
                        reader_db,
                        "tenant_mysql_g0",
                        "session_mysql_g0",
                    )
                    assert high_watermark is not None
                    writer_can_commit.set()
                    assert writer_committed.wait(timeout=5)
                    assert not writer_errors
                    rows = all_turn_events(
                        reader_db,
                        "tenant_mysql_g0",
                        "session_mysql_g0",
                        {"message_mysql_g0", "client_mysql_g0"},
                        high_watermark,
                    )
        finally:
            reader_connection.close()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert [row.event_type for row in rows] == ["user_message_received"]
    finally:
        engine.dispose()


def test_pi_b_g0_mysql_full_snapshot_replay_excludes_post_h_event(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 replay 快照在 H 与目标查询之间遇到提交时仍不读到迟到终态。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    writer_can_commit = Event()
    writer_committed = Event()
    writer_errors: list[BaseException] = []
    try:
        started_at = datetime(2026, 9, 6, 9, 9, 0)
        with Session(engine) as db:
            db.add(
                Message(
                    id="mysql_snapshot_message",
                    tenant_id="tenant_mysql_snapshot",
                    session_id="session_mysql_snapshot",
                    role="user",
                    content="snapshot replay",
                    metadata_json={"client_turn_id": "client_mysql_snapshot"},
                )
            )
            db.add(
                AgentEvent(
                    id="mysql_snapshot_received",
                    tenant_id="tenant_mysql_snapshot",
                    session_id="session_mysql_snapshot",
                    event_type="user_message_received",
                    payload_json={
                        "message_id": "mysql_snapshot_message",
                        "client_turn_id": "client_mysql_snapshot",
                    },
                    created_at=started_at,
                )
            )
            db.commit()

        def writer_two() -> None:
            """在 replay 固定 H 后通过真实 EventLog 提交迟到终态。"""

            try:
                writer_can_commit.wait(timeout=5)
                time.sleep(0.01)
                with Session(engine) as db:
                    EventLog(db).record(
                        "tenant_mysql_snapshot",
                        "session_mysql_snapshot",
                        "complete",
                        {
                            "turn_id": "mysql_snapshot_message",
                            "client_turn_id": "client_mysql_snapshot",
                        },
                    )
                    db.commit()
                    writer_committed.set()
            except BaseException as exc:  # pragma: no cover - 把线程异常传回主断言
                writer_errors.append(exc)
                writer_committed.set()

        original_all_turn_events = chat_api.all_turn_events

        def gated_all_turn_events(*args, **kwargs):
            """在目标事件查询前释放独立写事务，验证快照边界而非时间猜测。"""

            writer_can_commit.set()
            assert writer_committed.wait(timeout=5)
            assert not writer_errors
            return original_all_turn_events(*args, **kwargs)

        monkeypatch.setattr(chat_api, "all_turn_events", gated_all_turn_events)
        worker = Thread(target=writer_two)
        worker.start()
        with Session(engine) as caller_db:
            replay = _existing_turn_replay_in_snapshot(
                caller_db,
                ChatTurnRequest(
                    tenant_id="tenant_mysql_snapshot",
                    session_id="session_mysql_snapshot",
                    user_id="user_mysql_snapshot",
                    message="snapshot replay",
                    client_turn_id="client_mysql_snapshot",
                ),
            )
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert replay is not None
        assert [row.event_type for row in replay[1]] == ["user_message_received"]
        assert replay[3] is False
    finally:
        engine.dispose()


def test_pi_b_x1_mysql_history_scale_records_query_costs(mysql_database_url: str) -> None:
    """MySQL 8.4 五档历史量验证目标查询物化量并记录旧宽查询成本。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    history_counts = (0, 100, 1_000, 10_000, 100_000)
    target_chunk_count = 5
    tenant_id = "tenant_mysql_b_x1"
    started_at = datetime(2026, 9, 6, 9, 20, 0)
    try:
        fixtures: dict[int, tuple[str, str, str]] = {}
        with Session(engine) as db:
            for history_count in history_counts:
                session_id = f"session_mysql_b_x1_{history_count}"
                message_id = f"message_mysql_b_x1_{history_count}"
                client_turn_id = f"client_mysql_b_x1_{history_count}"
                fixtures[history_count] = (session_id, message_id, client_turn_id)
                db.add(
                    Message(
                        id=message_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        role="user",
                        content="MySQL B-X1 benchmark",
                        metadata_json={"client_turn_id": client_turn_id},
                    )
                )
                events = [
                    AgentEvent(
                        id=f"{session_id}_received",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type="user_message_received",
                        payload_json={
                            "message_id": message_id,
                            "client_turn_id": client_turn_id,
                        },
                        created_at=started_at,
                    )
                ]
                events.extend(
                    AgentEvent(
                        id=f"{session_id}_delta_{index:03d}",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type="stream_delta",
                        payload_json={
                            "turn_id": message_id,
                            "client_turn_id": client_turn_id,
                            "content": f"目标-{index}",
                        },
                        created_at=started_at + timedelta(microseconds=index + 1),
                    )
                    for index in range(target_chunk_count)
                )
                events.append(
                    AgentEvent(
                        id=f"{session_id}_complete",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type="complete",
                        payload_json={
                            "turn_id": message_id,
                            "client_turn_id": client_turn_id,
                        },
                        created_at=started_at + timedelta(microseconds=target_chunk_count + 1),
                    )
                )
                events.extend(
                    AgentEvent(
                        id=f"{session_id}_unrelated_{index:06d}",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type="stream_delta",
                        payload_json={
                            "turn_id": f"unrelated_{index}",
                            "content": "历史",
                        },
                        created_at=started_at
                        + timedelta(microseconds=target_chunk_count + index + 2),
                    )
                    for index in range(history_count)
                )
                db.add_all(events)
            db.commit()

        def measure(history_count: int, operation: str) -> dict[str, float | int]:
            """用新 Session 测量一次目标或旧宽查询的实际物化和内存成本。"""

            session_id, message_id, client_turn_id = fixtures[history_count]
            loaded: list[str] = []

            def on_load(row: AgentEvent, _context: object) -> None:
                """记录本次查询真正实例化的 AgentEvent。"""

                loaded.append(row.id)

            event.listen(AgentEvent, "load", on_load)
            tracemalloc.start()
            try:
                started = time.perf_counter()
                with Session(engine) as db:
                    if operation == "replay":
                        high_watermark = session_high_watermark(db, tenant_id, session_id)
                        assert high_watermark is not None
                        received = latest_received_event(
                            db,
                            tenant_id,
                            session_id,
                            client_turn_id,
                            high_watermark,
                        )
                        assert received is not None
                        rows = all_turn_events(
                            db,
                            tenant_id,
                            session_id,
                            (message_id, client_turn_id),
                            high_watermark,
                        )
                        returned_rows = len(rows)
                    elif operation == "partial":
                        rows = partial_turn_events(db, tenant_id, session_id, message_id)
                        returned_rows = len(rows)
                    elif operation == "legacy":
                        rows = db.exec(
                            select(AgentEvent)
                            .where(
                                AgentEvent.tenant_id == tenant_id,
                                AgentEvent.session_id == session_id,
                            )
                            .order_by(AgentEvent.created_at, AgentEvent.id)
                        ).all()
                        returned_rows = len(rows)
                    else:
                        raise AssertionError(f"unsupported operation: {operation}")
                elapsed_ms = (time.perf_counter() - started) * 1000
                _current, peak_bytes = tracemalloc.get_traced_memory()
                return {
                    "elapsed_ms": round(elapsed_ms, 3),
                    "loaded_agent_events": len(loaded),
                    "returned_rows": returned_rows,
                    "peak_tracemalloc_kib": round(peak_bytes / 1024, 2),
                }
            finally:
                tracemalloc.stop()
                event.remove(AgentEvent, "load", on_load)

        def percentile(values: list[float], fraction: float) -> float:
            """按上取整最近邻计算小样本 p95。"""

            ordered = sorted(values)
            index = min(len(ordered) - 1, max(0, ceil(fraction * len(ordered)) - 1))
            return ordered[index]

        results: dict[int, dict[str, object]] = {}
        for history_count in history_counts:
            for operation in ("replay", "partial", "legacy"):
                for _ in range(3):
                    measure(history_count, operation)
            summaries: dict[str, dict[str, object]] = {}
            for operation in ("replay", "partial", "legacy"):
                samples = [measure(history_count, operation) for _ in range(10)]
                summaries[operation] = {
                    "median_ms": round(statistics.median(float(row["elapsed_ms"]) for row in samples), 3),
                    "p95_ms": round(
                        percentile([float(row["elapsed_ms"]) for row in samples], 0.95),
                        3,
                    ),
                    "loaded_agent_events": sorted({row["loaded_agent_events"] for row in samples}),
                    "returned_rows": sorted({row["returned_rows"] for row in samples}),
                    "peak_tracemalloc_kib_max": round(
                        max(float(row["peak_tracemalloc_kib"]) for row in samples),
                        2,
                    ),
                }
            with Session(engine) as db:
                session_id, message_id, client_turn_id = fixtures[history_count]
                candidate_rows_before_identity = int(
                    db.exec(
                        select(func.count())
                        .select_from(AgentEvent)
                        .where(
                            AgentEvent.tenant_id == tenant_id,
                            AgentEvent.session_id == session_id,
                            AgentEvent.event_type.notin_(
                                {"llm_call_started", "llm_call_finished", "llm_call_failed"}
                            ),
                        )
                    ).one()
                )
                statement = (
                    select(AgentEvent)
                    .where(
                        AgentEvent.tenant_id == tenant_id,
                        AgentEvent.session_id == session_id,
                        AgentEvent.event_type.notin_(
                            {"llm_call_started", "llm_call_finished", "llm_call_failed"}
                        ),
                        turn_identity_filter(db, (message_id, client_turn_id)),
                    )
                    .order_by(AgentEvent.created_at, AgentEvent.id)
                    .limit(TURN_EVENT_PAGE_SIZE)
                )
                compiled = statement.compile(engine, compile_kwargs={"literal_binds": True})
            with engine.connect() as connection:
                explain_text = str(
                    connection.exec_driver_sql(f"EXPLAIN FORMAT=JSON {compiled}").scalar()
                )
            results[history_count] = {
                "history_count": history_count,
                "target_chunk_count": target_chunk_count,
                "warmups": 3,
                "samples": 10,
                "summary": summaries,
                "candidate_rows_before_identity": candidate_rows_before_identity,
                "explain_format_json": json.loads(explain_text),
            }
            assert candidate_rows_before_identity == history_count + target_chunk_count + 2
            assert summaries["replay"]["loaded_agent_events"] == [target_chunk_count + 2]
            assert summaries["replay"]["returned_rows"] == [target_chunk_count + 2]
            assert summaries["partial"]["loaded_agent_events"] == [target_chunk_count]
            assert summaries["partial"]["returned_rows"] == [target_chunk_count]
            assert summaries["legacy"]["loaded_agent_events"] == [
                history_count + target_chunk_count + 2
            ]
            assert summaries["legacy"]["returned_rows"] == [
                history_count + target_chunk_count + 2
            ]
        print(json.dumps({"benchmark": "PI-2B-B-X1-MySQL", "results": results}, ensure_ascii=False))
    finally:
        engine.dispose()


def test_pi_b_x4_mysql_one_mib_snapshot_replay_has_no_new_facts(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL 8.4 独立快照经两个观察者各十次 SSE 重放 1 MiB 正文且不新增执行事实。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    monkeypatch.setattr(chat_api, "engine", engine)
    target_bytes = 1024 * 1024
    pattern = "中文🙂\n开发者工具\r\n"
    body = ""
    while len(body.encode("utf-8")) + len(pattern.encode("utf-8")) <= target_bytes:
        body += pattern
    body += "a" * (target_bytes - len(body.encode("utf-8")))
    assert len(body.encode("utf-8")) == target_bytes

    tenant_id = "tenant_mysql_b_x4"
    session_id = "session_mysql_b_x4"
    message_id = "message_mysql_b_x4"
    client_turn_id = "client_mysql_b_x4"
    started_at = datetime(2026, 9, 6, 9, 25, 0)
    frame_size = 8191
    body_frames = [body[index:index + frame_size] for index in range(0, len(body), frame_size)]
    events = [
        AgentEvent(
            id="mysql_b_x4_received",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="user_message_received",
            payload_json={"message_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at,
        )
    ]
    events.extend(
        AgentEvent(
            id=f"mysql_b_x4_delta_{index:04d}",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="stream_delta",
            payload_json={
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": frame,
            },
            created_at=started_at + timedelta(microseconds=index + 1),
        )
        for index, frame in enumerate(body_frames)
    )
    events.append(
        AgentEvent(
            id="mysql_b_x4_complete",
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="complete",
            payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at + timedelta(microseconds=len(body_frames) + 1),
        )
    )
    expected_ids = [event_row.id for event_row in events]
    request = ChatTurnRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        agent_id="agent_mysql_b_x4",
        user_id="user_mysql_b_x4",
        message="MySQL B-X4 1 MiB",
        client_turn_id=client_turn_id,
    )
    fact_models = (
        AgentEvent,
        Message,
        SopInstance,
        SopOperation,
        SopOperationAttempt,
        ExecutionResult,
        ExecutionPublication,
        ExecutionArtifact,
    )

    def fact_counts() -> dict[str, int]:
        """读取 replay 前后的持久事实数量，不读取正文或执行载荷。"""

        with Session(engine) as db:
            return {
                model.__name__: int(db.exec(select(func.count()).select_from(model)).one())
                for model in fact_models
            }

    try:
        with Session(engine) as db:
            db.add(
                Message(
                    id=message_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    role="user",
                    content=request.message,
                    metadata_json={"client_turn_id": client_turn_id},
                )
            )
            db.add_all(events)
            db.commit()
        before_counts = fact_counts()
        expected_hash = sha256(body.encode("utf-8")).hexdigest()

        def replay_once() -> tuple[list[str], str]:
            """通过独立读 Session 执行一次完整生产 replay/SSE 投影。"""

            with Session(engine) as db:
                replay = _existing_turn_replay_in_snapshot(db, request)
            assert replay is not None
            replay_message_id, rows, cursor, terminal = replay
            assert replay_message_id == message_id
            assert cursor is not None
            assert terminal is True
            stream = list(
                chat_api._stream_existing_turn(
                    request,
                    message_id=replay_message_id,
                    initial_rows=rows,
                    initial_cursor=cursor,
                    terminal=terminal,
                )
            )
            ids = [
                frame.split("id: ", 1)[1].split("\n", 1)[0]
                for frame in stream
                if frame.startswith("id: ")
            ]
            replayed_body = "".join(
                str(
                    json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0]).get(
                        "content"
                    )
                    or ""
                )
                for frame in stream
                if "event: stream_delta\n" in frame
            )
            assert ids == expected_ids
            assert len(stream) == len(events)
            assert sha256(replayed_body.encode("utf-8")).hexdigest() == expected_hash
            assert len(replayed_body.encode("utf-8")) == target_bytes
            return ids, replayed_body

        def observe_ten_times(_observer: int) -> list[tuple[list[str], str]]:
            """让一个独立观察者连续十次重新建立快照并重放同一 Turn。"""

            return [replay_once() for _ in range(10)]

        with ThreadPoolExecutor(max_workers=2) as observers:
            observed = list(observers.map(observe_ten_times, range(2)))
        assert all(result[0] == expected_ids for observer in observed for result in observer)
        assert fact_counts() == before_counts
    finally:
        engine.dispose()
