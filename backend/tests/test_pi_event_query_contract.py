"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : test_pi_event_query_contract.py
@CallChain  : pytest → replay event query helper/chat API → isolated SQLite ledger
@Description: 验证 PI-2B replay 的高水位、别名、分页、隔离、Unicode 身份和脱离连接语义。
"""

from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, func
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import chat as chat_api
from app.api.chat import (
    _existing_turn_replay,
    _existing_turn_replay_in_snapshot,
    _relay_event_payload,
    _stream_existing_turn,
)
from app.core.agent_loop import AgentLoop
from app.db.models import AgentEvent, ChatSession, Message
from app.db.sqlite_legacy import configure_sqlite_runtime
from app.observability.event_log import EventLog, TurnEventFenceClosed
from app.observability.turn_event_query import (
    TURN_EVENT_PAGE_SIZE,
    all_turn_events,
    latest_received_event,
    partial_turn_events,
    session_high_watermark,
    turn_event_page,
    turn_events_after_cursor,
)
from app.session.session_schema import ChatAttachmentRef, ChatTurnRequest


@pytest.fixture
def query_engine(tmp_path: Path) -> Engine:
    """为事件查询契约建立临时 SQLite 文件库，避免复用应用或真实租户数据。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'pi-event-query.db'}")
    configure_sqlite_runtime(engine)
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _request(
    *,
    tenant_id: str,
    session_id: str,
    message: str,
    client_turn_id: str,
    **updates: Any,
) -> ChatTurnRequest:
    """构造 replay 测试请求，并允许单测覆盖一个不可变输入字段。"""

    return ChatTurnRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        agent_id="agent_pi",
        user_id="user_pi",
        message=message,
        client_turn_id=client_turn_id,
        **updates,
    )


def _seed_turn(
    db: Session,
    *,
    tenant_id: str = "tenant_pi",
    session_id: str = "session_pi",
    message_id: str = "message_pi",
    client_turn_id: str = "client_pi",
    message: str = "查询 PI",
    metadata: dict[str, Any] | None = None,
    events: list[tuple[str, dict[str, Any], datetime]] | None = None,
) -> None:
    """写入一个最小真实 Message/AgentEvent Turn fixture，不改变生产写入口。"""

    db.add(
        ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            user_id="user_pi",
            agent_id="agent_pi",
        )
    )
    db.add(
        Message(
            id=message_id,
            tenant_id=tenant_id,
            session_id=session_id,
            role="user",
            content=message,
            metadata_json={"client_turn_id": client_turn_id, **(metadata or {})},
        )
    )
    if events is None:
        started_at = datetime(2026, 9, 6, 9, 0, 0)
        events = [
            (
                "user_message_received",
                {
                    "message_id": message_id,
                    "client_turn_id": client_turn_id,
                    "channel": "web",
                },
                started_at,
            ),
            (
                "stream_delta",
                {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "完成"},
                started_at + timedelta(microseconds=1),
            ),
            (
                "complete",
                {"turn_id": message_id, "client_turn_id": client_turn_id, "reply": "完成"},
                started_at + timedelta(microseconds=2),
            ),
        ]
    for index, (event_type, payload, created_at) in enumerate(events):
        db.add(
            AgentEvent(
                id=f"{session_id}_event_{index:06d}",
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=event_type,
                payload_json=payload,
                created_at=created_at,
            )
        )
    db.commit()


def test_turn_terminal_record_is_idempotent(query_engine: Engine) -> None:
    """同一聊天 Turn 的多个完成尝试只保留一个终态事件。"""

    with Session(query_engine) as db:
        db.add(
            ChatSession(
                id="session_terminal_once",
                tenant_id="tenant_pi",
                user_id="user_pi",
                agent_id="agent_pi",
            )
        )
        db.commit()
        events = EventLog(db)
        events.bind_turn("message_terminal_once", "client_terminal_once")
        payload = {"reply": "完成"}
        assert events.record_terminal_once(
            "tenant_pi",
            "session_terminal_once",
            "complete",
            payload,
        ) is True
        db.commit()
        assert events.record_terminal_once(
            "tenant_pi",
            "session_terminal_once",
            "complete",
            payload,
        ) is False
        db.commit()
        rows = db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == "tenant_pi",
                AgentEvent.session_id == "session_terminal_once",
                AgentEvent.event_type == "complete",
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].payload_json["turn_id"] == "message_terminal_once"


def test_dynamic_complete_sse_projection_is_idempotent_after_runtime_close(
    query_engine: Engine,
) -> None:
    """领域 Runtime 已写入 complete 后，SSE 重放只能复用该终态而不追加第二条。"""

    with Session(query_engine) as db:
        session_row = ChatSession(
            id="session_dynamic_complete_projection",
            tenant_id="tenant_pi",
            user_id="user_pi",
        )
        db.add(session_row)
        db.add(
            AgentEvent(
                id="event_dynamic_complete_projection",
                tenant_id="tenant_pi",
                session_id=session_row.id,
                event_type="complete",
                payload_json={
                    "turn_id": "message_dynamic_complete_projection",
                    "user_message_id": "message_dynamic_complete_projection",
                    "reply": "领域结果已完成",
                },
            )
        )
        db.commit()

        loop = object.__new__(AgentLoop)
        loop.db = db
        loop.events = EventLog(db)
        projected = loop._stream_event(
            "complete",
            session_row,
            {
                "turn_id": "message_dynamic_complete_projection",
                "user_message_id": "message_dynamic_complete_projection",
                "reply": "领域结果已完成",
            },
            terminal_once=True,
        )
        db.commit()
        rows = db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == "tenant_pi",
                AgentEvent.session_id == session_row.id,
                AgentEvent.event_type == "complete",
            )
        ).all()

    assert projected["event"] == "complete"
    assert len(rows) == 1


def test_stream_success_projection_stops_when_error_terminal_wins(query_engine: Engine) -> None:
    """错误或取消先闭合时，幂等成功投影不得把成功帧返回给客户端。"""

    with Session(query_engine) as db:
        session_row = ChatSession(
            id="session_terminal_race_projection",
            tenant_id="tenant_pi",
            user_id="user_pi",
        )
        db.add(session_row)
        db.add(
            AgentEvent(
                id="event_terminal_race_error",
                tenant_id="tenant_pi",
                session_id=session_row.id,
                event_type="error_occurred",
                payload_json={
                    "turn_id": "message_terminal_race",
                    "user_message_id": "message_terminal_race",
                    "code": "MODEL_ERROR",
                },
            )
        )
        db.commit()

        loop = object.__new__(AgentLoop)
        loop.db = db
        loop.events = EventLog(db)
        with pytest.raises(TurnEventFenceClosed):
            loop._stream_event(
                "complete",
                session_row,
                {
                    "turn_id": "message_terminal_race",
                    "user_message_id": "message_terminal_race",
                    "reply": "不应覆盖错误终态",
                },
                terminal_once=True,
            )


def test_replay_keeps_assistant_projection_open_without_complete(query_engine: Engine) -> None:
    """只有 assistant 投影而没有 complete 时，重复请求仍应进入实时回放等待。"""

    started_at = datetime(2026, 9, 6, 9, 0, 30)
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_projection_only",
            message_id="message_projection_only",
            client_turn_id="client_projection_only",
            message="投影未闭合",
            events=[
                (
                    "user_message_received",
                    {
                        "message_id": "message_projection_only",
                        "client_turn_id": "client_projection_only",
                    },
                    started_at,
                ),
                (
                    "assistant_message_created",
                    {
                        "message_id": "assistant_projection_only",
                        "turn_id": "message_projection_only",
                        "client_turn_id": "client_projection_only",
                        "reply": "处理中",
                    },
                    started_at + timedelta(microseconds=1),
                ),
            ],
        )
        replay = _existing_turn_replay(
            db,
            _request(
                tenant_id="tenant_pi",
                session_id="session_projection_only",
                message="投影未闭合",
                client_turn_id="client_projection_only",
            ),
        )

    assert replay is not None
    assert replay[3] is False
    assert [row.event_type for row in replay[1]] == [
        "user_message_received",
        "assistant_message_created",
    ]


def test_pi_b_p3_aliases_and_spans_are_projected_once(query_engine: Engine) -> None:
    """四种兼容身份键都能命中，但 span 和无身份事件不会串入重放。"""

    started_at = datetime(2026, 9, 6, 9, 1, 0)
    events = [
        (
            "user_message_received",
            {"message_id": "message_alias", "client_turn_id": "client_alias"},
            started_at,
        ),
        (
            "stream_delta",
            {"user_message_id": "message_alias", "content": "正文"},
            started_at + timedelta(microseconds=1),
        ),
        (
            "llm_call_finished",
            {"turn_id": "message_alias", "content": "内部 span"},
            started_at + timedelta(microseconds=2),
        ),
        (
            "complete",
            {"message_id": "message_alias", "reply": "完成"},
            started_at + timedelta(microseconds=3),
        ),
        ("stream_delta", {"content": "无身份"}, started_at + timedelta(microseconds=4)),
    ]
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_alias",
            message_id="message_alias",
            client_turn_id="client_alias",
            message="别名查询",
            events=events,
        )
        replay = _existing_turn_replay(
            db,
            _request(
                tenant_id="tenant_pi",
                session_id="session_alias",
                message="别名查询",
                client_turn_id="client_alias",
            ),
        )

    assert replay is not None
    _message_id, rows, _cursor, terminal = replay
    assert [row.event_type for row in rows] == [
        "user_message_received",
        "stream_delta",
        "complete",
    ]
    assert terminal is True


def test_pi_b_p4_live_cursor_queries_target_and_accepts_iterable_identity(
    query_engine: Engine,
) -> None:
    """实时游标只取目标 Turn，且公共分页 helper 不会耗尽生成器身份。"""

    started_at = datetime(2026, 9, 6, 9, 1, 30)
    events = [
        (
            "user_message_received",
            {"message_id": "message_live", "client_turn_id": "client_live"},
            started_at,
        ),
        (
            "stream_delta",
            {"turn_id": "message_live", "client_turn_id": "client_live", "content": "前"},
            started_at + timedelta(microseconds=1),
        )
    ]
    events.extend(
        (
            "stream_delta",
            {"content": f"无关-{index}"},
            started_at + timedelta(microseconds=index + 2),
        )
        for index in range(TURN_EVENT_PAGE_SIZE + 40)
    )
    events.append(
        (
            "complete",
            {"turn_id": "message_live", "client_turn_id": "client_live"},
            started_at + timedelta(microseconds=TURN_EVENT_PAGE_SIZE + 43),
        )
    )
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_live",
            message_id="message_live",
            client_turn_id="client_live",
            message="实时游标",
            events=events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", "session_live")
        assert high_watermark is not None
        page = turn_event_page(
            db,
            "tenant_pi",
            "session_live",
            (value for value in ("message_live", "client_live")),
            high_watermark,
        )
        after_page = turn_events_after_cursor(
            db,
            "tenant_pi",
            "session_live",
            (value for value in ("client_live",)),
            (events[0][2], "session_live_event_000000"),
        )

    assert [row.event_type for row in page] == ["user_message_received", "stream_delta", "complete"]
    assert [row.event_type for row in after_page] == ["stream_delta", "complete"]


def test_pi_b_x2_keyset_pages_all_target_events_and_h_excludes_late_rows(
    query_engine: Engine,
) -> None:
    """目标超过一页仍完整读取；固定 H 后追加事件不会污染本次 replay。"""

    started_at = datetime(2026, 9, 6, 9, 2, 0)
    events = [
        (
            "user_message_received",
            {"message_id": "message_long", "client_turn_id": "client_long"},
            started_at,
        )
    ]
    for index in range(TURN_EVENT_PAGE_SIZE + 17):
        events.append(
            (
                "stream_delta",
                {
                    "turn_id": "message_long" if index % 2 == 0 else "",
                    "user_message_id": "message_long" if index % 2 else "",
                    "client_turn_id": "client_long",
                    "content": str(index),
                },
                started_at + timedelta(microseconds=index + 1),
            )
        )
    events.append(
        (
            "complete",
            {"turn_id": "message_long", "client_turn_id": "client_long"},
            started_at + timedelta(microseconds=TURN_EVENT_PAGE_SIZE + 18),
        )
    )
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_long",
            message_id="message_long",
            client_turn_id="client_long",
            message="长 Turn",
            events=events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", "session_long")
        assert high_watermark is not None
        db.add(
            AgentEvent(
                id="session_long_event_late",
                tenant_id="tenant_pi",
                session_id="session_long",
                event_type="complete",
                payload_json={"turn_id": "message_long", "client_turn_id": "client_long"},
                created_at=started_at + timedelta(seconds=1),
            )
        )
        db.commit()
        rows = all_turn_events(
            db,
            "tenant_pi",
            "session_long",
            {"message_long", "client_long"},
            high_watermark,
        )

    assert len(rows) == TURN_EVENT_PAGE_SIZE + 19
    assert rows[0].id == "session_long_event_000000"
    assert rows[-1].event_type == "complete"
    assert all(row.id != "session_long_event_late" for row in rows)


@pytest.mark.parametrize(
    "chunk_count",
    [0, 1, 199, 200, 201, 999, 1000, 1001, 5001],
    ids=lambda value: f"target-chunks-{value}",
)
def test_pi_b_x2_all_target_page_boundaries_are_complete(
    query_engine: Engine,
    chunk_count: int,
) -> None:
    """逐项验证冻结的 K 边界，目标 Turn 跨页读取不能截断或重复。"""

    started_at = datetime(2026, 9, 6, 9, 2, 30)
    session_id = f"session_page_boundary_{chunk_count}"
    message_id = f"message_page_boundary_{chunk_count}"
    client_turn_id = f"client_page_boundary_{chunk_count}"
    events = [
        (
            "user_message_received",
            {"message_id": message_id, "client_turn_id": client_turn_id},
            started_at,
        )
    ]
    events.extend(
        (
            "stream_delta",
            {
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": str(index),
            },
            started_at + timedelta(microseconds=index + 1),
        )
        for index in range(chunk_count)
    )
    events.append(
        (
            "complete",
            {"turn_id": message_id, "client_turn_id": client_turn_id},
            started_at + timedelta(microseconds=chunk_count + 1),
        )
    )

    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="分页边界",
            events=events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        rows = all_turn_events(
            db,
            "tenant_pi",
            session_id,
            {message_id, client_turn_id},
            high_watermark,
        )
        partial_rows = partial_turn_events(
            db,
            "tenant_pi",
            session_id,
            message_id,
        )
        replay = _existing_turn_replay(
            db,
            _request(
                tenant_id="tenant_pi",
                session_id=session_id,
                message="分页边界",
                client_turn_id=client_turn_id,
            ),
        )

    assert len(rows) == chunk_count + 2
    assert len(partial_rows) == chunk_count
    assert [
        (row.payload_json or {}).get("content") for row in partial_rows
    ] == [str(index) for index in range(chunk_count)]
    assert len({row.id for row in rows}) == len(rows)
    assert [(row.created_at, row.id) for row in rows] == sorted(
        (row.created_at, row.id) for row in rows
    )
    assert rows[0].event_type == "user_message_received"
    assert rows[-1].event_type == "complete"
    assert replay is not None
    assert [row.id for row in replay[1]] == [row.id for row in rows]
    assert replay[3] is True


def test_pi_b_x3_same_timestamp_order_and_post_h_tail_are_deterministic(
    query_engine: Engine,
) -> None:
    """同刻事件按 ID 稳定分页；目标恰在 H，H 后提交的尾部事件只由实时游标读取。"""

    started_at = datetime(2026, 9, 6, 9, 2, 40)
    session_id = "session_same_timestamp"
    message_id = "message_same_timestamp"
    client_turn_id = "client_same_timestamp"
    events = [
        (
            "user_message_received",
            {"message_id": message_id, "client_turn_id": client_turn_id},
            started_at,
        ),
        (
            "stream_delta",
            {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "甲"},
            started_at,
        ),
        (
            "stream_delta",
            {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "乙"},
            started_at,
        ),
        (
            "complete",
            {"turn_id": message_id, "client_turn_id": client_turn_id},
            started_at,
        ),
    ]

    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="同刻排序",
            events=events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        first_page = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            page_size=2,
        )
        assert len(first_page) == 2
        second_page = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            after=(first_page[-1].created_at, first_page[-1].id),
            page_size=2,
        )
        initial_ids = [row.id for row in first_page + second_page]
        initial_rows = all_turn_events(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            page_size=2,
        )

    with Session(query_engine) as db:
        db.add(
            AgentEvent(
                id="session_same_timestamp_event_00000-late",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="stream_delta",
                payload_json={
                    "turn_id": message_id,
                    "client_turn_id": client_turn_id,
                    "content": "迟到同刻",
                },
                created_at=started_at,
            )
        )
        db.add(
            AgentEvent(
                id="session_same_timestamp_event_tail",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="complete",
                payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
                created_at=started_at + timedelta(seconds=1),
            )
        )
        db.commit()
        tail_rows = turn_events_after_cursor(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            include_same_timestamp=True,
            exclude_event_ids=set(initial_ids),
        )

    assert initial_ids == [row.id for row in initial_rows]
    assert len(initial_ids) == len(set(initial_ids)) == 4
    assert initial_rows[-1].id == high_watermark[1]
    assert [row.id for row in tail_rows] == [
        "session_same_timestamp_event_00000-late",
        "session_same_timestamp_event_tail",
    ]


def test_pi_b_x3_stream_relay_emits_unseen_same_timestamp_late_event_once(
    query_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 relay 轮询同刻低 ID 迟到事件时只补发一次，并在终态后停止。"""

    started_at = datetime(2026, 9, 6, 9, 2, 50)
    session_id = "session_relay_same_timestamp"
    message_id = "message_relay_same_timestamp"
    client_turn_id = "client_relay_same_timestamp"
    with Session(query_engine, expire_on_commit=False) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="relay 同刻排序",
            events=[
                (
                    "user_message_received",
                    {"message_id": message_id, "client_turn_id": client_turn_id},
                    started_at,
                ),
                (
                    "stream_delta",
                    {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "前"},
                    started_at,
                ),
            ],
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        initial_rows = all_turn_events(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
        )
        db.add(
            AgentEvent(
                id="session_relay_same_timestamp_event_00000-late",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="stream_delta",
                payload_json={
                    "turn_id": message_id,
                    "client_turn_id": client_turn_id,
                    "content": "迟到",
                },
                created_at=started_at,
            )
        )
        db.add(
            AgentEvent(
                id="session_relay_same_timestamp_event_tail",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="complete",
                payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
                created_at=started_at + timedelta(seconds=1),
            )
        )
        db.commit()

    monkeypatch.setattr(chat_api, "engine", query_engine)
    frames = list(
        chat_api._stream_existing_turn(
            _request(
                tenant_id="tenant_pi",
                session_id=session_id,
                message="relay 同刻排序",
                client_turn_id=client_turn_id,
            ),
            message_id=message_id,
            initial_rows=initial_rows,
            initial_cursor=high_watermark,
            terminal=False,
        )
    )

    assert [frame.split("event: ", 1)[1].split("\n", 1)[0] for frame in frames] == [
        "user_message_received",
        "stream_delta",
        "stream_delta",
        "complete",
    ]
    assert sum("迟到" in frame for frame in frames) == 1
    assert sum("event_tail" in frame for frame in frames) == 1


def test_pi_b_x3_stream_relay_emits_late_event_before_cursor_timestamp(
    query_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn 内已知首事件之后，早于当前游标的迟到提交也必须被 relay 补发。"""

    started_at = datetime(2026, 9, 6, 9, 2, 52)
    session_id = "session_relay_older_timestamp"
    message_id = "message_relay_older_timestamp"
    client_turn_id = "client_relay_older_timestamp"
    with Session(query_engine, expire_on_commit=False) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="relay 早时间戳迟到",
            events=[
                (
                    "user_message_received",
                    {"message_id": message_id, "client_turn_id": client_turn_id},
                    started_at,
                ),
                (
                    "stream_delta",
                    {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "前缀"},
                    started_at + timedelta(seconds=1),
                ),
            ],
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        initial_rows = all_turn_events(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
        )
        db.add_all(
            [
                AgentEvent(
                    id="session_relay_older_timestamp_event_late",
                    tenant_id="tenant_pi",
                    session_id=session_id,
                    event_type="stream_delta",
                    payload_json={
                        "turn_id": message_id,
                        "client_turn_id": client_turn_id,
                        "content": "早时间戳迟到",
                    },
                    created_at=started_at + timedelta(milliseconds=500),
                ),
                AgentEvent(
                    id="session_relay_older_timestamp_event_tail",
                    tenant_id="tenant_pi",
                    session_id=session_id,
                    event_type="complete",
                    payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
                    created_at=started_at + timedelta(seconds=2),
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(chat_api, "engine", query_engine)
    monkeypatch.setattr(chat_api, "STREAM_RELAY_POLL_SECONDS", 0.001)
    frames = list(
        chat_api._stream_existing_turn(
            _request(
                tenant_id="tenant_pi",
                session_id=session_id,
                message="relay 早时间戳迟到",
                client_turn_id=client_turn_id,
            ),
            message_id=message_id,
            initial_rows=initial_rows,
            initial_cursor=high_watermark,
            terminal=False,
        )
    )

    assert [frame.split("event: ", 1)[1].split("\n", 1)[0] for frame in frames] == [
        "user_message_received",
        "stream_delta",
        "stream_delta",
        "complete",
    ]
    assert sum("早时间戳迟到" in frame for frame in frames) == 1


def test_pi_b_x3_paging_midflight_append_respects_h_and_live_overlap(
    query_engine: Engine,
) -> None:
    """分页进行中追加事件时，固定 H 的 replay 与实时重叠读取各自只返回应见事实。"""

    started_at = datetime(2026, 9, 6, 9, 2, 55)
    session_id = "session_midflight_append"
    message_id = "message_midflight_append"
    client_turn_id = "client_midflight_append"
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="分页中追加",
            events=[
                (
                    "user_message_received",
                    {"message_id": message_id, "client_turn_id": client_turn_id},
                    started_at,
                ),
                (
                    "stream_delta",
                    {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "甲"},
                    started_at,
                ),
                (
                    "stream_delta",
                    {"turn_id": message_id, "client_turn_id": client_turn_id, "content": "乙"},
                    started_at,
                ),
                (
                    "complete",
                    {"turn_id": message_id, "client_turn_id": client_turn_id},
                    started_at,
                ),
            ],
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        first_page = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            page_size=2,
        )
        assert [row.id for row in first_page] == [
            "session_midflight_append_event_000000",
            "session_midflight_append_event_000001",
        ]
        db.add(
            AgentEvent(
                id="session_midflight_append_event_000000-late",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="stream_delta",
                payload_json={
                    "turn_id": message_id,
                    "client_turn_id": client_turn_id,
                    "content": "迟到同刻",
                },
                created_at=started_at,
            )
        )
        db.add(
            AgentEvent(
                id="session_midflight_append_event_tail",
                tenant_id="tenant_pi",
                session_id=session_id,
                event_type="complete",
                payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
                created_at=started_at + timedelta(seconds=1),
            )
        )
        db.commit()
        second_page = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            after=(first_page[-1].created_at, first_page[-1].id),
            page_size=2,
        )
        third_page = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            after=(second_page[-1].created_at, second_page[-1].id),
            page_size=2,
        )
        bounded_h_rows = turn_event_page(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            (started_at, "session_midflight_append_event_000000"),
            page_size=20,
        )
        strict_rows = turn_events_after_cursor(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
        )
        overlap_rows = turn_events_after_cursor(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
            include_same_timestamp=True,
            exclude_event_ids={row.id for row in first_page + second_page},
        )

    replay_rows = first_page + second_page + third_page
    assert [row.id for row in replay_rows] == [
        "session_midflight_append_event_000000",
        "session_midflight_append_event_000001",
        "session_midflight_append_event_000002",
        "session_midflight_append_event_000003",
    ]
    assert [row.id for row in bounded_h_rows] == [
        "session_midflight_append_event_000000",
    ]
    assert [row.id for row in strict_rows] == ["session_midflight_append_event_tail"]
    assert [row.id for row in overlap_rows] == [
        "session_midflight_append_event_000000-late",
        "session_midflight_append_event_tail",
    ]


def test_pi_b_x4_one_mib_utf8_frames_replay_without_duplication(query_engine: Engine) -> None:
    """1 MiB 中文、emoji、换行经独立快照和 SSE 重放十轮时保持不变。"""

    target_bytes = 1024 * 1024
    pattern = "中文🙂\n开发者工具\r\n"
    body = ""
    while len(body.encode("utf-8")) + len(pattern.encode("utf-8")) <= target_bytes:
        body += pattern
    body += "a" * (target_bytes - len(body.encode("utf-8")))
    assert len(body.encode("utf-8")) == target_bytes

    started_at = datetime(2026, 9, 6, 9, 3, 0)
    session_id = "session_one_mib"
    message_id = "message_one_mib"
    client_turn_id = "client_one_mib"
    frame_size = 137
    frames = [body[index:index + frame_size] for index in range(0, len(body), frame_size)]
    events = [
        (
            "user_message_received",
            {"message_id": message_id, "client_turn_id": client_turn_id},
            started_at,
        )
    ]
    events.extend(
        (
            "stream_delta",
            {"turn_id": message_id, "client_turn_id": client_turn_id, "content": frame},
            started_at + timedelta(microseconds=index + 1),
        )
        for index, frame in enumerate(frames)
    )
    events.append(
        (
            "complete",
            {"turn_id": message_id, "client_turn_id": client_turn_id},
            started_at + timedelta(microseconds=len(frames) + 1),
        )
    )

    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message="1 MiB 跨帧正文",
            events=events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        before_event_count = db.exec(select(func.count()).select_from(AgentEvent)).one()
        before_message_count = db.exec(select(func.count()).select_from(Message)).one()
        expected_event_count = len(events)
        expected_ids: list[str] | None = None
        expected_hash = sha256(body.encode("utf-8")).hexdigest()
        for _observer in range(2):
            for _reconnect in range(10):
                replay = _existing_turn_replay_in_snapshot(
                    db,
                    _request(
                        tenant_id="tenant_pi",
                        session_id=session_id,
                        message="1 MiB 跨帧正文",
                        client_turn_id=client_turn_id,
                    ),
                )
                assert replay is not None
                replay_message_id, rows, replay_cursor, replay_terminal = replay
                assert replay_message_id == message_id
                assert replay_cursor == high_watermark
                assert replay_terminal is True
                stream = list(
                    _stream_existing_turn(
                        _request(
                            tenant_id="tenant_pi",
                            session_id=session_id,
                            message="1 MiB 跨帧正文",
                            client_turn_id=client_turn_id,
                        ),
                        message_id=replay_message_id,
                        initial_rows=rows,
                        initial_cursor=replay_cursor,
                        terminal=replay_terminal,
                    )
                )
                ids = [
                    frame.split("id: ", 1)[1].split("\n", 1)[0]
                    for frame in stream
                    if frame.startswith("id: ")
                ]
                if expected_ids is None:
                    expected_ids = ids
                assert ids == expected_ids
                assert len(ids) == len(set(ids))
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
                assert sha256(replayed_body.encode("utf-8")).hexdigest() == expected_hash
                assert len(replayed_body.encode("utf-8")) == target_bytes
                assert len(stream) == expected_event_count

    assert expected_ids is not None
    assert len(expected_ids) == expected_event_count
    with Session(query_engine) as db:
        assert db.exec(select(func.count()).select_from(AgentEvent)).one() == before_event_count
        assert db.exec(select(func.count()).select_from(Message)).one() == before_message_count


def test_pi_b_x4_live_relay_streams_one_mib_tail_without_new_execution(
    query_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行中重连先消费 1 MiB 前缀、再接收原 worker 尾部时不启动第二次执行。"""

    target_bytes = 1024 * 1024
    pattern = "中文🙂\n开发者工具\r\n"
    body = ""
    while len(body.encode("utf-8")) + len(pattern.encode("utf-8")) <= target_bytes:
        body += pattern
    body += "a" * (target_bytes - len(body.encode("utf-8")))
    assert len(body.encode("utf-8")) == target_bytes

    started_at = datetime(2026, 9, 6, 9, 3, 30)
    session_id = "session_one_mib_live"
    message_id = "message_one_mib_live"
    client_turn_id = "client_one_mib_live"
    frame_size = 8191
    body_frames = [body[index:index + frame_size] for index in range(0, len(body), frame_size)]
    initial_events = [
        (
            "user_message_received",
            {"message_id": message_id, "client_turn_id": client_turn_id},
            started_at,
        ),
        (
            "stream_delta",
            {
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": body_frames[0],
            },
            started_at + timedelta(microseconds=1),
        ),
    ]
    tail_events = [
        (
            "stream_delta",
            {
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": frame,
            },
            started_at + timedelta(microseconds=index + 2),
        )
        for index, frame in enumerate(body_frames[1:])
    ]
    tail_events.append(
        (
            "complete",
            {"turn_id": message_id, "client_turn_id": client_turn_id},
            started_at + timedelta(microseconds=len(body_frames) + 1),
        )
    )
    request = _request(
        tenant_id="tenant_pi",
        session_id=session_id,
        message="1 MiB 运行中重连",
        client_turn_id=client_turn_id,
    )
    writer_done = Event()
    writer_errors: list[BaseException] = []

    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=message_id,
            client_turn_id=client_turn_id,
            message=request.message,
            events=initial_events,
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        initial_rows = all_turn_events(
            db,
            "tenant_pi",
            session_id,
            (message_id, client_turn_id),
            high_watermark,
        )

    def append_original_worker_tail() -> None:
        """模拟原 worker 在重连观察者消费前缀后提交剩余正文和终态。"""

        try:
            with Session(query_engine) as db:
                for index, (event_type, payload, created_at) in enumerate(tail_events, start=2):
                    db.add(
                        AgentEvent(
                            id=f"{session_id}_event_{index:06d}",
                            tenant_id="tenant_pi",
                            session_id=session_id,
                            event_type=event_type,
                            payload_json=payload,
                            created_at=created_at,
                        )
                    )
                db.commit()
        except BaseException as exc:  # pragma: no cover - 把线程异常传回主断言
            writer_errors.append(exc)
        finally:
            writer_done.set()

    monkeypatch.setattr(chat_api, "engine", query_engine)
    monkeypatch.setattr(chat_api, "STREAM_RELAY_POLL_SECONDS", 0.001)
    stream = _stream_existing_turn(
        request,
        message_id=message_id,
        initial_rows=initial_rows,
        initial_cursor=high_watermark,
        terminal=False,
    )
    initial_frames = [next(stream) for _row in initial_rows]
    worker = Thread(target=append_original_worker_tail)
    worker.start()
    assert writer_done.wait(timeout=5)
    tail_frames = list(stream)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert not writer_errors
    all_frames = initial_frames + tail_frames
    event_ids = [
        frame.split("id: ", 1)[1].split("\n", 1)[0]
        for frame in all_frames
        if frame.startswith("id: ")
    ]
    expected_ids = [
        f"{session_id}_event_{index:06d}"
        for index in range(len(initial_events) + len(tail_events))
    ]
    replayed_body = "".join(
        str(
            json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0]).get("content")
            or ""
        )
        for frame in all_frames
        if "event: stream_delta\n" in frame
    )
    assert event_ids == expected_ids
    assert len(event_ids) == len(set(event_ids))
    assert sha256(replayed_body.encode("utf-8")).hexdigest() == sha256(body.encode("utf-8")).hexdigest()
    assert len(replayed_body.encode("utf-8")) == target_bytes
    with Session(query_engine) as db:
        assert db.exec(select(func.count()).select_from(Message)).one() == 1
        assert db.exec(select(func.count()).select_from(AgentEvent)).one() == len(expected_ids)


def test_pi_b_n1_replay_keeps_tenant_and_session_boundaries(query_engine: Engine) -> None:
    """相同 client ID 出现在其他租户或会话时，当前请求只能返回未找到。"""

    with Session(query_engine) as db:
        _seed_turn(
            db,
            tenant_id="tenant_other",
            session_id="session_other",
            message_id="message_other",
            client_turn_id="client_shared",
            message="其他域",
        )
        assert _existing_turn_replay(
            db,
            _request(
                tenant_id="tenant_pi",
                session_id="session_other",
                message="其他域",
                client_turn_id="client_shared",
            ),
        ) is None
        assert _existing_turn_replay(
            db,
            _request(
                tenant_id="tenant_other",
                session_id="session_missing",
                message="其他域",
                client_turn_id="client_shared",
            ),
        ) is None


@pytest.mark.parametrize(
    "padding",
    [" ", "\t", "\r", "\n", "\u00a0", "\u3000"],
    ids=["space", "tab", "cr", "lf", "nbsp", "ideographic-space"],
)
def test_pi_b_g1_sql_identity_matches_python_strip_for_supported_whitespace(
    query_engine: Engine,
    padding: str,
) -> None:
    """SQLite JSON 身份条件与现行 Python strip oracle 对六类边界空白保持一致。"""

    session_id = f"session_whitespace_{ord(padding[-1]):x}"
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=f"message_{session_id}",
            client_turn_id="client_whitespace",
            message="空白身份",
            events=[
                (
                    "user_message_received",
                    {
                        "message_id": f"message_{session_id}",
                        "client_turn_id": f"{padding}client_whitespace{padding}",
                    },
                    datetime(2026, 9, 6, 9, 3, 0),
                )
            ],
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        received = latest_received_event(
            db,
            "tenant_pi",
            session_id,
            "client_whitespace",
            high_watermark,
        )

    assert received is not None


@pytest.mark.parametrize(
    "stored_value,requested_value",
    [
        ("Target", "target"),
        ("e\u0301", "é"),
        (None, "target"),
        ("", "target"),
        (False, "False"),
        ([], "[]"),
        ({}, "{}"),
    ],
    ids=["case", "composed-unicode", "null", "empty", "boolean", "array", "object"],
)
def test_pi_b_g1_sql_identity_rejects_non_equivalent_json_values(
    query_engine: Engine,
    stored_value: Any,
    requested_value: str,
) -> None:
    """大小写、组合 Unicode 和非字符串 JSON 值不能被 SQL 候选误串为另一个 Turn。"""

    session_id = f"session_identity_{abs(hash((repr(stored_value), requested_value)))}"
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id=session_id,
            message_id=f"message_{session_id}",
            client_turn_id="different_client",
            message="身份负例",
            events=[
                (
                    "user_message_received",
                    {"message_id": f"message_{session_id}", "client_turn_id": stored_value},
                    datetime(2026, 9, 6, 9, 4, 0),
                )
            ],
        )
        high_watermark = session_high_watermark(db, "tenant_pi", session_id)
        assert high_watermark is not None
        received = latest_received_event(
            db,
            "tenant_pi",
            session_id,
            requested_value,
            high_watermark,
        )

    assert received is None


def test_pi_b_g1_latest_malformed_json_does_not_hide_older_valid_received(
    query_engine: Engine,
) -> None:
    """最新非字符串身份不是 SQL 候选时，不能遮蔽 H 内更早的合法接收事件。"""

    started_at = datetime(2026, 9, 6, 9, 4, 30)
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_received_candidates",
            message_id="message_received_candidates",
            client_turn_id="client_received_candidates",
            message="接收候选",
            events=[
                (
                    "user_message_received",
                    {
                        "message_id": "message_received_candidates",
                        "client_turn_id": " client_received_candidates ",
                    },
                    started_at,
                ),
                (
                    "user_message_received",
                    {
                        "message_id": "message_received_candidates",
                        "client_turn_id": 123,
                    },
                    started_at + timedelta(microseconds=1),
                ),
            ],
        )
        high_watermark = session_high_watermark(
            db,
            "tenant_pi",
            "session_received_candidates",
        )
        assert high_watermark is not None
        received = latest_received_event(
            db,
            "tenant_pi",
            "session_received_candidates",
            "client_received_candidates",
            high_watermark,
        )

    assert received is not None
    assert received.id == "session_received_candidates_event_000000"


def test_pi_b_snapshot_rows_are_usable_after_read_connection_closes(query_engine: Engine) -> None:
    """replay 返回的事件在独立只读连接关闭后仍可完整序列化为 SSE。"""

    with Session(query_engine) as db:
        _seed_turn(db)
        replay = _existing_turn_replay_in_snapshot(
            db,
            _request(
                tenant_id="tenant_pi",
                session_id="session_pi",
                message="查询 PI",
                client_turn_id="client_pi",
            ),
        )

    assert replay is not None
    _message_id, rows, _cursor, terminal = replay
    assert terminal is True
    assert [event_name for event_name, _payload in (_relay_event_payload(row) for row in rows)] == [
        "user_message_received",
        "stream_delta",
        "complete",
    ]


def test_pi_b_g0_sqlite_full_snapshot_replay_excludes_post_h_event(
    query_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite 完整 replay 在 H 与目标查询之间遇到提交时仍保持快照边界。"""

    writer_can_commit = Event()
    writer_committed = Event()
    writer_errors: list[BaseException] = []
    started_at = datetime(2026, 9, 6, 9, 8, 0)
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_snapshot",
            message_id="message_snapshot",
            client_turn_id="client_snapshot",
            message="snapshot replay",
            events=[
                (
                    "user_message_received",
                    {"message_id": "message_snapshot", "client_turn_id": "client_snapshot"},
                    started_at,
                )
            ],
        )

    def writer_two() -> None:
        """在 replay 固定 H 后通过真实 EventLog 提交迟到终态。"""

        try:
            writer_can_commit.wait(timeout=5)
            with Session(query_engine) as db:
                EventLog(db).record(
                    "tenant_pi",
                    "session_snapshot",
                    "complete",
                    {"turn_id": "message_snapshot", "client_turn_id": "client_snapshot"},
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
    with Session(query_engine) as caller_db:
        replay = _existing_turn_replay_in_snapshot(
            caller_db,
            _request(
                tenant_id="tenant_pi",
                session_id="session_snapshot",
                message="snapshot replay",
                client_turn_id="client_snapshot",
            ),
        )
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert replay is not None
    assert [row.event_type for row in replay[1]] == ["user_message_received"]
    assert replay[3] is False


def test_pi_b_n2_replay_preserves_immutable_model_and_mode_checks(query_engine: Engine) -> None:
    """相同 client ID 改模型或交互模式仍返回 409，而不是重放错误 Turn。"""

    with Session(query_engine) as db:
        _seed_turn(
            db,
            metadata={"model_config_id": "model_a", "interaction_mode": "scheduled_task"},
        )
        request = _request(
            tenant_id="tenant_pi",
            session_id="session_pi",
            message="查询 PI",
            client_turn_id="client_pi",
            model_config_id="model_a",
            interaction_mode="scheduled_task",
        )
        assert _existing_turn_replay(db, request) is not None
        with pytest.raises(HTTPException) as mismatch:
            _existing_turn_replay(db, request.model_copy(update={"model_config_id": "model_b"}))

    assert mismatch.value.status_code == 409


def test_pi_b_n2_all_immutable_inputs_keep_409_and_do_not_append_rows(
    query_engine: Engine,
) -> None:
    """正文、渠道、运行身份、Skill、能力和附件任一变化都拒绝且不写新事实。"""

    attachment = ChatAttachmentRef(resource_id="resource_pi", resource_version="v1")
    started_at = datetime(2026, 9, 6, 9, 6, 30)
    events = [
        (
            "user_message_received",
            {
                "message_id": "message_immutable",
                "client_turn_id": "client_immutable",
                "channel": "api",
                "user_id": "user_pi",
            },
            started_at,
        ),
        (
            "complete",
            {"turn_id": "message_immutable", "client_turn_id": "client_immutable"},
            started_at + timedelta(microseconds=1),
        ),
    ]
    metadata = {
        "execution_engine": "dynamic_task",
        "model_config_id": "model_immutable_a",
        "interaction_mode": "scheduled_task",
        "forced_general_skill_id": "skill_immutable_a",
        "forced_general_skill_ids": ["skill_immutable_a", "skill_immutable_b"],
        "ability_ids": ["ability_immutable_a", "ability_immutable_b"],
        "attachments": [{"resource_id": attachment.resource_id, "resource_version": attachment.resource_version}],
    }
    request = _request(
        tenant_id="tenant_pi",
        session_id="session_immutable",
        message="不可变输入",
        client_turn_id="client_immutable",
        channel="api",
        execution_engine="dynamic_task",
        model_config_id="model_immutable_a",
        interaction_mode="scheduled_task",
        forced_general_skill_id="skill_immutable_a",
        forced_general_skill_ids=["skill_immutable_a", "skill_immutable_b"],
        ability_ids=["ability_immutable_a", "ability_immutable_b"],
        attachments=[attachment],
    )
    mutations = {
        "message": {"message": "正文变更"},
        "user": {"user_id": "user_other"},
        "channel": {"channel": "web"},
        "engine": {"execution_engine": "auto"},
        "model": {"model_config_id": "model_immutable_b"},
        "mode": {"interaction_mode": "normal"},
        "skill": {"forced_general_skill_id": "skill_immutable_b"},
        "skills": {"forced_general_skill_ids": ["skill_immutable_b"]},
        "abilities": {"ability_ids": ["ability_immutable_b"]},
        "attachments": {"attachments": []},
    }

    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_immutable",
            message_id="message_immutable",
            client_turn_id="client_immutable",
            message="不可变输入",
            metadata=metadata,
            events=events,
        )
        assert _existing_turn_replay(db, request) is not None
        message_count = len(db.exec(select(Message)).all())
        event_count = len(db.exec(select(AgentEvent)).all())
        for mutation in mutations.values():
            with pytest.raises(HTTPException) as mismatch:
                _existing_turn_replay(db, request.model_copy(update=mutation))
            assert mismatch.value.status_code == 409
        assert len(db.exec(select(Message)).all()) == message_count
        assert len(db.exec(select(AgentEvent)).all()) == event_count


def test_pi_b_p2_partial_keeps_priority_and_recovers_padded_identity(query_engine: Engine) -> None:
    """partial 兼容空白身份，但 turn_id 仅空白时仍优先于 user_message_id 不回退。"""

    started_at = datetime(2026, 9, 6, 9, 6, 0)
    with Session(query_engine) as db:
        _seed_turn(
            db,
            session_id="session_partial_identity",
            message_id="message_partial_identity",
            client_turn_id="client_partial_identity",
            message="partial 身份",
            events=[
                (
                    "stream_delta",
                    {"turn_id": " message_partial_identity ", "content": "前"},
                    started_at,
                ),
                (
                    "stream_delta",
                    {
                        "turn_id": "   ",
                        "user_message_id": "message_partial_identity",
                        "content": "错误回退",
                    },
                    started_at + timedelta(microseconds=1),
                ),
                (
                    "stream_delta",
                    {"turn_id": "", "user_message_id": " message_partial_identity ", "content": "后"},
                    started_at + timedelta(microseconds=2),
                ),
            ],
        )
        loop = object.__new__(AgentLoop)
        loop.db = db
        assert loop._persisted_turn_delta_parts(
            "tenant_pi", "session_partial_identity", "message_partial_identity"
        ) == ["前", "后"]


def test_pi_b_g0_normal_committed_producer_after_h_is_not_in_snapshot(
    query_engine: Engine,
) -> None:
    """三连接模拟正常 EventLog 写入，证明 R 消费 H 后只接收 H 之后的新游标。"""

    with Session(query_engine) as writer_one:
        event_log = EventLog(writer_one)
        event_log.record(
            "tenant_g0",
            "session_g0",
            "user_message_received",
            {"message_id": "message_g0", "client_turn_id": "client_g0"},
        )
        writer_one.commit()

    writer_can_commit = Event()
    writer_committed = Event()
    writer_error: list[BaseException] = []

    def writer_two() -> None:
        """在 R 固定 H 后，通过真实 EventLog 独立提交迟到业务事件。"""

        try:
            writer_can_commit.wait(timeout=5)
            with Session(query_engine) as writer_db:
                EventLog(writer_db).record(
                    "tenant_g0",
                    "session_g0",
                    "complete",
                    {"turn_id": "message_g0", "client_turn_id": "client_g0"},
                )
                writer_db.commit()
                writer_committed.set()
        except BaseException as exc:  # pragma: no cover - 只用于把线程失败传回主断言
            writer_error.append(exc)
            writer_committed.set()

    worker = Thread(target=writer_two)
    worker.start()
    with query_engine.connect() as reader_connection:
        with reader_connection.begin():
            with Session(bind=reader_connection) as reader_db:
                high_watermark = session_high_watermark(reader_db, "tenant_g0", "session_g0")
                assert high_watermark is not None
                writer_can_commit.set()
                assert writer_committed.wait(timeout=5)
                assert not writer_error
                rows = all_turn_events(
                    reader_db,
                    "tenant_g0",
                    "session_g0",
                    {"message_g0", "client_g0"},
                    high_watermark,
                )
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert [row.event_type for row in rows] == ["user_message_received"]
