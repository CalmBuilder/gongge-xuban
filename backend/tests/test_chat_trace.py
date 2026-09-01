"""
@Time       : 2026/08/27
@Author     : zhanglp8181
@File       : test_chat_trace.py
@CallChain  : pytest → chat event projection/API → SQLite test ledger
@Description: 验证会话事件窗口的恢复锚点、边界、终态和租户隔离契约。
"""

from datetime import datetime, timedelta
from threading import Thread
from time import sleep

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import (
    _build_turn_traces,
    _cancel_active_dynamic_execution,
    _events_after_cursor,
    _existing_turn_replay,
    _format_scheduled_task_schedule,
    _message_turn_ids_from_events,
    _normalized_session_event_payload,
    _persist_chat_turn_cancelled,
    _persist_chat_turn_interrupted,
    _relay_event_payload,
    _session_event_history_rows,
    _stream_existing_turn,
    _turn_has_terminal_event,
    SESSION_EVENT_HISTORY_LIMIT,
    SESSION_EVENT_HISTORY_MAX_LIMIT,
    SESSION_EVENT_LATEST_TURN_MAX,
    SESSION_EVENT_TERMINAL_MAX,
    list_chat_session_events,
    list_chat_session_spans,
    message_read,
)
from app.api import chat as chat_api
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    ExecutionCommand,
    KnowledgeConcept,
    Message,
    SopInstance,
    Tenant,
    User,
)
from app.observability.event_log import EventLog
from app.security.auth import get_current_user
from app.session.session_schema import ChatTurnRequest
from app.sop_runtime.execution_store import SopExecutionFencedError, SopExecutionStore


def test_completed_client_turn_replays_persisted_stream_without_new_execution() -> None:
    """同一请求重发只投影原 turn 事件；换正文或 forced Skill 必须冲突。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_replay",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )
    user_message = Message(
        id="msg_replay",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        role="user",
        content="按指南处理",
        metadata_json={
            "client_turn_id": "client_replay",
            "forced_general_skill_id": "skill_a",
            "execution_engine": "dynamic_task",
        },
    )
    db.add(session_row)
    db.add(user_message)
    for index, (event_type, payload) in enumerate(
        (
            (
                "user_message_received",
                {
                    "message_id": user_message.id,
                    "client_turn_id": "client_replay",
                    "message": user_message.content,
                    "channel": "web",
                },
            ),
            (
                "stream_delta",
                {
                    "turn_id": user_message.id,
                    "client_turn_id": "client_replay",
                    "content": "完成",
                },
            ),
            (
                "complete",
                {
                    "turn_id": user_message.id,
                    "client_turn_id": "client_replay",
                    "reply": "完成",
                },
            ),
        )
    ):
        db.add(
            AgentEvent(
                id=f"evt_replay_{index}",
                tenant_id="tenant_demo",
                session_id=session_row.id,
                event_type=event_type,
                payload_json=payload,
            )
        )
    db.commit()
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id=session_row.id,
        agent_id="agent_demo",
        user_id="user_demo",
        message=user_message.content,
        client_turn_id="client_replay",
        channel="web",
        execution_engine="dynamic_task",
        forced_general_skill_id="skill_a",
    )

    with Session(db.get_bind()) as recovered_db:
        replay = _existing_turn_replay(recovered_db, request)
        assert replay is not None
        message_id, rows, cursor, terminal = replay
        assert message_id == user_message.id
        assert terminal is True
        stream = "".join(
            _stream_existing_turn(
                request,
                message_id=message_id,
                initial_rows=rows,
                initial_cursor=cursor,
                terminal=terminal,
            )
        )
        assert "event: stream_delta" in stream
        assert "event: complete" in stream

        with pytest.raises(HTTPException) as mismatch:
            _existing_turn_replay(
                recovered_db,
                request.model_copy(update={"message": "换一个请求"}),
            )
        with pytest.raises(HTTPException) as engine_mismatch:
            _existing_turn_replay(
                recovered_db,
                request.model_copy(update={"execution_engine": "auto"}),
            )
    assert mismatch.value.status_code == 409
    assert engine_mismatch.value.status_code == 409


def test_running_client_turn_reattaches_until_original_worker_persists_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行中重连只尾随同一 turn 的新增事件，并在原 worker 终态后结束。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_reattach",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )
    received = AgentEvent(
        id="evt_reattach_received",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        event_type="user_message_received",
        payload_json={
            "message_id": "msg_reattach",
            "turn_id": "msg_reattach",
            "client_turn_id": "client_reattach",
        },
    )
    db.add(session_row)
    db.add(received)
    db.commit()
    test_engine = db.get_bind()
    monkeypatch.setattr(chat_api, "engine", test_engine)
    monkeypatch.setattr(chat_api, "STREAM_RELAY_POLL_SECONDS", 0.005)
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id=session_row.id,
        agent_id="agent_demo",
        user_id="user_demo",
        message="继续原请求",
        client_turn_id="client_reattach",
    )

    def finish_original_turn() -> None:
        """模拟原 worker 在重连后继续写入正文和终态。"""

        sleep(0.02)
        with Session(test_engine) as worker_db:
            worker_db.add(
                AgentEvent(
                    id="evt_reattach_delta",
                    tenant_id="tenant_demo",
                    session_id=session_row.id,
                    event_type="stream_delta",
                    payload_json={
                        "turn_id": "msg_reattach",
                        "client_turn_id": "client_reattach",
                        "content": "原 worker 完成",
                    },
                )
            )
            worker_db.add(
                AgentEvent(
                    id="evt_reattach_complete",
                    tenant_id="tenant_demo",
                    session_id=session_row.id,
                    event_type="complete",
                    payload_json={
                        "turn_id": "msg_reattach",
                        "client_turn_id": "client_reattach",
                        "reply": "原 worker 完成",
                    },
                )
            )
            worker_db.commit()

    worker = Thread(target=finish_original_turn)
    worker.start()
    stream = "".join(
        _stream_existing_turn(
            request,
            message_id="msg_reattach",
            initial_rows=[received],
            initial_cursor=(received.created_at, received.id),
            terminal=False,
        )
    )
    worker.join(timeout=1)

    assert "原 worker 完成" in stream
    assert "event: complete" in stream


def test_chat_stream_releases_preflight_agent_lock_before_worker_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流式接口把预检事务交给后台 worker 前必须提交，避免请求锁住同一 Agent。"""

    class _Db:
        """记录流式路由是否释放了预检事务。"""

        def __init__(self) -> None:
            """初始化提交计数。"""

            self.commit_count = 0

        def commit(self) -> None:
            """记录一次释放预检锁的提交。"""

            self.commit_count += 1

    class _Thread:
        """不执行 worker、只观察线程启动前的事务状态。"""

        def __init__(self, *, target, daemon: bool) -> None:
            """保存待执行目标并校验线程以 daemon 方式创建。"""

            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            """记录 worker 启动点；测试不真正调用后台模型链路。"""

            assert db.commit_count == 1

    db = _Db()
    user = User(
        id="user_demo",
        tenant_id="tenant_demo",
        username="demo",
        password_hash="hashed",
    )
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_stream_lock",
        agent_id="agent_demo",
        user_id="user_demo",
        message="检查首轮流式执行",
    )
    monkeypatch.setattr(chat_api, "_ensure_request_tenant", lambda *_args: None)
    monkeypatch.setattr(chat_api, "ensure_tenant", lambda *_args: None)
    monkeypatch.setattr(
        chat_api,
        "_ensure_chat_session_available",
        lambda *_args: ChatSession(
            id="session_stream_lock",
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_demo",
        ),
    )
    monkeypatch.setattr(chat_api, "_bind_request_to_session_agent", lambda *_args: request)
    monkeypatch.setattr(chat_api, "_existing_turn_replay", lambda *_args: None)
    monkeypatch.setattr(chat_api, "_latest_event_cursor", lambda *_args: None)
    monkeypatch.setattr(chat_api.threading, "Thread", _Thread)

    response = chat_api.chat_stream(request, current_user=user, db=db)  # type: ignore[arg-type]

    assert response.media_type == "text/event-stream"
    assert db.commit_count == 1


def test_stream_end_only_replay_stays_open_until_business_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重放只收到stream_end时不能宣称成功，并在短空闲窗口后安全结束。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_stream_end_replay",
        tenant_id="tenant_demo",
        user_id="user_stream_end_replay",
        agent_id="agent_demo",
    )
    user_message = Message(
        id="msg_stream_end_replay",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        role="user",
        content="只收到流结束",
        metadata_json={"client_turn_id": "client_stream_end_replay"},
    )
    db.add_all(
        [
            session_row,
            user_message,
            User(
                id="user_stream_end_replay",
                tenant_id="tenant_demo",
                username="stream-end-replay",
                password_hash="x",
            ),
            AgentEvent(
                id="evt_stream_end_replay_received",
                tenant_id="tenant_demo",
                session_id=session_row.id,
                event_type="user_message_received",
                payload_json={
                    "message_id": user_message.id,
                    "turn_id": user_message.id,
                    "client_turn_id": "client_stream_end_replay",
                    "channel": "web",
                },
            ),
            AgentEvent(
                id="evt_stream_end_replay_end",
                tenant_id="tenant_demo",
                session_id=session_row.id,
                event_type="stream_end",
                payload_json={
                    "turn_id": user_message.id,
                    "client_turn_id": "client_stream_end_replay",
                },
            ),
        ]
    )
    db.commit()
    monkeypatch.setattr(chat_api, "STREAM_RELAY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(chat_api, "STREAM_RELAY_IDLE_TIMEOUT_SECONDS", 0.01)
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id=session_row.id,
        agent_id="agent_demo",
        user_id="user_stream_end_replay",
        message=user_message.content,
        client_turn_id="client_stream_end_replay",
    )

    replay = _existing_turn_replay(db, request)
    assert replay is not None
    message_id, rows, cursor, terminal = replay
    assert message_id == user_message.id
    assert terminal is False
    stream = "".join(
        _stream_existing_turn(
            request,
            message_id=message_id,
            initial_rows=rows,
            initial_cursor=cursor,
            terminal=terminal,
        )
    )
    assert "event: stream_end" in stream
    assert "event: complete" not in stream


def test_event_log_binds_all_execution_events_to_current_turn() -> None:
    with _test_db() as db:
        events = EventLog(db)
        events.bind_turn("msg_user", "client_turn")

        event = events.record(
            "tenant_demo",
            "session_test",
            "step_agent_result_created",
            {"reply": "请补充退款原因"},
        )

        assert event.payload_json == {
            "reply": "请补充退款原因",
            "turn_id": "msg_user",
            "user_message_id": "msg_user",
            "client_turn_id": "client_turn",
        }


def test_session_spans_endpoint_returns_internal_spans_without_relaying_them() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="hashed",
        )
        db.add(user)
        db.add(
            ChatSession(
                id="session_test",
                tenant_id="tenant_demo",
                user_id=user.id,
            )
        )
        db.add(
            AgentEvent(
                id="evt_span",
                tenant_id="tenant_demo",
                session_id="session_test",
                event_type="llm_call_finished",
                payload_json={
                    "span_id": "span_demo",
                    "operation": "router.scene",
                    "duration_ms": 123.4,
                },
            )
        )
        db.add(
            AgentEvent(
                id="evt_business",
                tenant_id="tenant_demo",
                session_id="session_test",
                event_type="router_decision_created",
                payload_json={"decision": "answer_only"},
            )
        )
        db.commit()

        spans = list_chat_session_spans(
            "session_test",
            tenant_id="tenant_demo",
            current_user=user,
            db=db,
        )
        relayed = _events_after_cursor(db, "tenant_demo", "session_test", None)

    assert len(spans) == 1
    assert spans[0]["operation"] == "router.scene"
    assert spans[0]["duration_ms"] == 123.4
    assert [event.event_type for event in relayed] == ["router_decision_created"]


def test_turn_trace_uses_router_skill_hint_when_events_have_turn_id() -> None:
    started_at = datetime(2026, 6, 5, 6, 35, 4)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="帮我下单a2，实际发货a3",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "帮我下单a2，实际发货a3"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="router_decision_created",
            payload_json={
                "decision": "continue_active",
                "target_skill_id": "skill_purchase_001",
                "target_step_id": "confirm_purchase",
                "user_intent": "下单",
                "reason": "继续购买流程",
                "user_message_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_step_changed",
            payload_json={
                "from_step_id": "confirm_purchase",
                "to_step_id": "end",
                "user_message_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user", "reply": "已完成"},
            created_at=started_at + timedelta(seconds=3),
        ),
    ]

    traces = _build_turn_traces(messages, events, {"skill_purchase_001": "购买商品流程"})

    skill_lines = [
        line
        for line in traces[0]["lines"]
        if line["kind"] == "skill" and "购买商品流程" in line["text"]
    ]
    assert skill_lines
    assert skill_lines[0]["text"] == "推进SOP 购买商品流程"
    assert skill_lines[0]["detail"] == "step end"
    router_line = next(line for line in traces[0]["lines"] if line["id"] == "decision_router")
    assert router_line["icon"] == "judge"
    assert skill_lines[0]["icon"] == "advance"


def test_turn_trace_recovers_persisted_skill_state_for_current_turn() -> None:
    started_at = datetime(2026, 7, 14, 9, 57, 4)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="先查询天气，再购买 a1",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "先查询天气，再购买 a1"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_state",
            payload_json={
                "activeSkillId": "skill_purchase_001",
                "activeStepId": "collect_user_name",
                "currentSkills": [
                    {
                        "skillId": "skill_purchase_001",
                        "name": "购买商品流程",
                        "stepId": "collect_user_name",
                        "state": "active",
                    },
                    {
                        "skillId": "skill_weather_001",
                        "name": "天气查询流程",
                        "stepId": "collect_city",
                        "state": "pending",
                    },
                ],
                "runtimeDecision": "start_new_task",
                "user_message_id": "msg_user",
                "turn_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
    ]

    traces = _build_turn_traces(messages, events, {"skill_purchase_001": "购买商品流程"})

    skill_lines = [line for line in traces[0]["lines"] if line["kind"] == "skill"]
    assert skill_lines[0]["id"] == "skill_state_skill_purchase_001_active_collect_user_name"
    assert skill_lines[0]["text"] == "选择SOP 购买商品流程"
    assert skill_lines[0]["detail"] == "当前步骤 collect_user_name"
    assert skill_lines[1]["id"] == "skill_state_skill_weather_001_pending_collect_city"
    assert skill_lines[1]["text"] == "等待SOP 天气查询流程"


def test_turn_trace_merges_skill_started_with_matching_state_snapshot() -> None:
    started_at = datetime(2026, 7, 15, 13, 44, 11)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="帮我查询本月报销额度",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "帮我查询本月报销额度"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_started",
            payload_json={
                "decision": "start_new_task",
                "to_skill_id": "skill_expense_quota_query",
                "to_step_id": "node_collect_info",
                "turn_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_state",
            payload_json={
                "runtimeDecision": "start_new_task",
                "currentSkills": [
                    {
                        "skillId": "skill_expense_quota_query",
                        "name": "报销额度查询",
                        "stepId": "node_collect_info",
                        "state": "active",
                    }
                ],
                "turn_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=2),
        ),
    ]

    traces = _build_turn_traces(
        messages,
        events,
        {"skill_expense_quota_query": "报销额度查询"},
    )

    skill_lines = [line for line in traces[0]["lines"] if line["kind"] == "skill"]
    assert skill_lines == [
        {
            "id": "skill_state_skill_expense_quota_query_active_node_collect_info",
            "kind": "skill",
            "text": "选择SOP 报销额度查询",
            "detail": "当前步骤 node_collect_info",
            "state": "running",
            "icon": "advance",
        }
    ]


def test_turn_trace_uses_live_stream_ids_for_persisted_status_events() -> None:
    started_at = datetime(2026, 7, 15, 14, 10, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="查询报销额度",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "查询报销额度"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="stream_status",
            payload_json={"phase": "stepping", "text": "正在思考", "turn_id": "msg_user"},
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="stream_status",
            payload_json={"phase": "reflecting", "text": "正在反思", "turn_id": "msg_user"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="stream_status",
            payload_json={"phase": "knowledge", "text": "查询业务资料", "turn_id": "msg_user"},
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="stream_status",
            payload_json={
                "phase": "tool",
                "text": "正在调用工具",
                "tool_name": "expense.quota_query",
                "tool_call_id": "call_1",
                "turn_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=4),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [line["id"] for line in traces[0]["lines"]] == [
        "decision_stepping_main",
        "reflection",
        "knowledge_lookup",
        "tool_call_1",
    ]


def test_turn_trace_merges_knowledge_lifecycle_events_for_same_query() -> None:
    started_at = datetime(2026, 7, 15, 14, 33, 9)
    query = "招待客户的餐费是否计入差旅费报销范围"
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content=query,
            created_at=started_at,
        )
    ]
    common_payload = {"query": {"query": query}, "turn_id": "msg_user"}
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": query},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="knowledge_query_started",
            payload_json=common_payload,
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="knowledge_query_finished",
            payload_json={**common_payload, "selected_concepts": [{"id": "concept_1"}]},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="knowledge_result",
            payload_json={**common_payload, "selected_concepts": [{"id": "concept_1"}]},
            created_at=started_at + timedelta(seconds=3),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    knowledge_lines = [line for line in traces[0]["lines"] if line["kind"] == "knowledge"]
    assert knowledge_lines == [
        {
            "id": f"knowledge_lookup_{query}",
            "kind": "knowledge",
            "phase": "result",
            "text": "读取业务资料",
            "detail": "命中 Wiki 1 个",
            "state": "completed",
            "icon": "advance",
        }
    ]


def test_turn_trace_merges_created_and_relayed_reflection_decisions() -> None:
    started_at = datetime(2026, 7, 15, 14, 37, 5)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="继续处理",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "继续处理"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="reflection_decision_created",
            payload_json={"needs_retry": False, "turn_id": "msg_user"},
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="reflection_decision",
            payload_json={"needs_retry": False, "turn_id": "msg_user"},
            created_at=started_at + timedelta(seconds=2),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    reflection_lines = [line for line in traces[0]["lines"] if line["id"] == "reflection"]
    assert len(reflection_lines) == 1
    assert reflection_lines[0]["text"] == "反思通过"


def test_turn_trace_ignores_noop_skill_step_change() -> None:
    started_at = datetime(2026, 7, 15, 12, 40, 14)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="我的工号是2472063",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "我的工号是2472063"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_step_changed",
            payload_json={
                "decision": "continue_active",
                "from_skill_id": "expense_travel_reimbursement",
                "to_skill_id": "expense_travel_reimbursement",
                "from_step_id": "collect_reimbursement_info",
                "to_step_id": "collect_reimbursement_info",
                "turn_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
    ]

    traces = _build_turn_traces(
        messages,
        events,
        {"expense_travel_reimbursement": "差旅报销申请"},
    )

    assert not any(line["kind"] == "skill" for line in traces[0]["lines"])


def test_message_read_hydrates_knowledge_citation_content_from_concept() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    content = "完整 Content 正文。\n\n第二段继续保留。"
    with Session(engine) as db:
        db.add(
            KnowledgeConcept(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                knowledge_base_version_id="kbv_demo",
                document_id="kdoc_demo",
                concept_id="sources/demo/sections/sec-4",
                concept_type="Source Section",
                title="段落组 1",
                description="不完整 summary",
                content_md=f"---\ntitle: 段落组 1\n---\n{content}",
            )
        )
        db.commit()
        row = Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="assistant",
            content="回答 [1]",
            metadata_json={
                "knowledge_citations": [
                    {
                        "id": "kref_1",
                        "label": "[1]",
                        "kind": "concept",
                        "title": "段落组 1",
                        "concept_id": "sources/demo/sections/sec-4",
                        "summary": "不完整 summary",
                        "excerpt": "不完整 summary",
                    }
                ]
            },
        )

        read = message_read(row, db=db)

    citation = read.metadata["knowledge_citations"][0]
    assert citation["content"] == content
    assert citation["excerpt"] == content
    assert citation["summary"] == "不完整 summary"


def test_message_read_compacts_historical_knowledge_citation_labels() -> None:
    row = Message(
        id="msg_assistant_historical_citations",
        tenant_id="tenant_demo",
        session_id="session_test",
        role="assistant",
        content="先参考排查手册。[1] 区域故障则提交报修。[4]",
        metadata_json={
            "knowledge_citations": [
                {"id": "kref_1", "label": "[1]", "title": "排查手册"},
                {"id": "kref_4", "label": "[4]", "title": "网络故障"},
            ]
        },
    )

    read = message_read(row)

    assert read.content == "先参考排查手册。[1] 区域故障则提交报修。[2]"
    assert [item["label"] for item in read.metadata["knowledge_citations"]] == ["[1]", "[2]"]


def test_turn_trace_does_not_reconstruct_events_from_message_metadata() -> None:
    started_at = datetime(2026, 6, 20, 10, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_citation",
            role="user",
            content="引用规则是什么？",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_citation",
            role="assistant",
            content="回答需要展示知识引用。[1]",
            metadata_json={
                "knowledge_citations": [
                    {
                        "title": "知识引用测试说明 / 引用规则",
                        "source_title": "citation-demo.md",
                    }
                ]
            },
            created_at=started_at + timedelta(seconds=1),
        ),
    ]

    traces = _build_turn_traces(messages, [], {})

    assert traces == []


def test_turn_trace_keeps_running_routing_status_for_refresh() -> None:
    started_at = datetime(2026, 7, 4, 9, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_running",
            role="user",
            content="你好",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_running",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_running",
            event_type="stream_status",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=100),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] is None
    assert any(
        line["id"] == "decision_router"
        and line["text"] == "判断意图"
        and line["state"] == "running"
        and line["icon"] == "judge"
        for line in traces[0]["lines"]
    )


def test_turn_trace_marks_model_and_intermediate_errors_failed() -> None:
    started_at = datetime(2026, 7, 9, 12, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_error",
            role="user",
            content="总结一下",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_error",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "总结一下"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_error",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "error",
                "code": "LLM_ERROR",
                "message": "upstream timeout",
                "text": "模型调用失败",
            },
            created_at=started_at + timedelta(milliseconds=100),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_error",
            event_type="general_skill_trace",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "plan_failed",
                "message": "模型生成 runner 失败",
                "error": "invalid json",
            },
            created_at=started_at + timedelta(milliseconds=200),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_error",
            event_type="error_occurred",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "code": "LLM_ERROR",
                "message": "upstream timeout",
            },
            created_at=started_at + timedelta(milliseconds=300),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})
    lines = traces[0]["lines"]

    assert traces[0]["completed_at"] == events[-1].created_at.isoformat()
    assert any(
        line["text"] == "模型调用失败"
        and line["state"] == "failed"
        and line["icon"] == "loading"
        and "upstream timeout" in line["detail"]
        for line in lines
    )
    assert any(
        line["text"] == "模型生成 runner 失败"
        and line["state"] == "failed"
        and line["icon"] == "generated"
        and "invalid json" in line["detail"]
        for line in lines
    )


def test_turn_trace_cancel_event_closes_running_status_for_refresh() -> None:
    started_at = datetime(2026, 7, 4, 9, 5, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            role="user",
            content="暂停测试",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "暂停测试"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="stream_status",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=100),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="stream_cancelled",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=300),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] == (started_at + timedelta(milliseconds=300)).isoformat()
    assert all(line["state"] != "running" for line in traces[0]["lines"])
    assert any(
        line["id"] == "generation_stopped"
        and line["text"] == "用户已停止生成"
        and line["state"] == "completed"
        for line in traces[0]["lines"]
    )


def test_scheduled_task_draft_trace_restores_config_stages_for_refresh() -> None:
    started_at = datetime(2026, 7, 7, 16, 50, 0)
    draft = {
        "should_create": True,
        "tenant_id": "tenant_demo",
        "agent_id": "agent_demo",
        "title": "提醒我喝咖啡",
        "prompt": "提醒我喝咖啡",
        "schedule_type": "daily",
        "schedule": {"time": "16:50"},
        "timezone": "Asia/Shanghai",
        "confidence": 0.95,
        "source_session_id": "session_schedule",
    }
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_schedule",
            role="user",
            content="16:50提醒我喝咖啡",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_schedule",
            role="assistant",
            content="我已按你选择的定时项目整理成自动任务草案。",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user", "scheduled_task_draft": draft},
            created_at=started_at + timedelta(milliseconds=500),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "16:50提醒我喝咖啡"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "scheduled_task_intent",
                "text": "识别定时任务需求",
            },
            created_at=started_at + timedelta(milliseconds=100),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "scheduled_task_parse",
                "text": "解析执行计划",
            },
            created_at=started_at + timedelta(milliseconds=200),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="scheduled_task_draft_created",
            payload_json={**draft, "turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=300),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant", "turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=500),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] == (started_at + timedelta(milliseconds=500)).isoformat()
    assert [line["id"] for line in traces[0]["lines"]] == [
        "scheduled_task_intent",
        "scheduled_task_parse",
        "scheduled_task_draft",
    ]
    assert all(line["state"] == "completed" for line in traces[0]["lines"])
    assert traces[0]["lines"][1]["detail"] == "计划：每天 16:50"
    assert "提醒我喝咖啡" in traces[0]["lines"][2]["detail"]


def test_scheduled_task_schedule_formatter_preserves_fallbacks() -> None:
    assert (
        _format_scheduled_task_schedule("weekly", {"time": "18:30", "weekdays": ["1", "x", 6, 7, -1]})
        == "每周 周二、周日 18:30"
    )
    assert _format_scheduled_task_schedule("monthly", {"day_of_month": 21}) == "每月 21 号 09:00"
    assert _format_scheduled_task_schedule("unknown", {"time": "08:15"}) == "每天 08:15"


def test_cancel_endpoint_persists_terminal_trace_for_client_turn_id() -> None:
    db = _test_db()
    started_at = datetime(2026, 7, 4, 9, 5, 0)
    session_row = ChatSession(id="session_cancel_endpoint", tenant_id="tenant_demo", user_id="user_demo")
    db.add(session_row)
    db.add(
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id=session_row.id,
            role="user",
            content="暂停测试",
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={
                "message_id": "msg_user",
                "client_turn_id": "turn_local_1",
                "message": "暂停测试",
            },
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "routing",
                "text": "正在判断用户意图",
            },
            created_at=started_at + timedelta(milliseconds=100),
        )
    )
    db.commit()

    assert _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_1", "user_demo")
    db.commit()
    assert _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_1", "user_demo")

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == session_row.id)
        .order_by(AgentEvent.created_at)
    ).all()
    cancel_events = [event for event in events if event.event_type == "stream_cancelled"]
    assert len(cancel_events) == 1
    assert cancel_events[0].payload_json["turn_id"] == "msg_user"
    assert cancel_events[0].payload_json["user_message_id"] == "msg_user"
    assert cancel_events[0].payload_json["client_turn_id"] == "turn_local_1"

    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == session_row.id)
        .order_by(Message.created_at)
    ).all()
    assistant_messages = [message for message in messages if message.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "已停止生成"
    assert assistant_messages[0].metadata_json["turn_id"] == "msg_user"
    assert assistant_messages[0].metadata_json["user_message_id"] == "msg_user"
    assert assistant_messages[0].metadata_json["client_turn_id"] == "turn_local_1"
    traces = _build_turn_traces(messages, events, {})
    assert traces[0]["completed_at"] == cancel_events[0].created_at.isoformat()
    assert all(line["state"] != "running" for line in traces[0]["lines"])
    assert any(
        line["id"] == "generation_stopped"
        and line["text"] == "用户已停止生成"
        and line["state"] == "completed"
        for line in traces[0]["lines"]
    )


def test_cancel_endpoint_rejects_unknown_turn_alias_before_user_event_is_visible() -> None:
    """未知 client_turn_id 不得预先写入取消事实，避免污染未来同别名 Turn。"""

    db = _test_db()
    session_row = ChatSession(id="session_cancel_before_event", tenant_id="tenant_demo", user_id="user_demo")
    db.add(session_row)
    db.commit()

    assert not _persist_chat_turn_cancelled(
        db, "tenant_demo", session_row, "turn_local_pending", "user_demo"
    )
    db.commit()
    assert not _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_pending", "user_demo")

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == session_row.id)
        .order_by(AgentEvent.created_at)
    ).all()
    cancel_events = [event for event in events if event.event_type == "stream_cancelled"]
    assert cancel_events == []
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == session_row.id)
        .order_by(Message.created_at)
    ).all()
    assert [message.role for message in messages] == []


def test_chat_cancel_bridges_to_active_dynamic_execution() -> None:
    """聊天停止必须复用统一 Execution 取消状态机，而非仅终止 SSE 展示。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_dynamic_cancel",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )
    instance = SopInstance(
        id="execution_dynamic_cancel",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        kind="dynamic_task",
        active_slot_key="dynamic:session_dynamic_cancel",
        initiator_user_id="user_demo",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": "执行复杂任务"},
        current_plan_revision_id="plan_dynamic_cancel",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="running",
        source_ref="msg_dynamic",
    )
    db.add(
        AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="Chat trace agent",
            status="active",
        )
    )
    db.add(session_row)
    db.add(instance)
    db.commit()

    assert _cancel_active_dynamic_execution(db, session_row, "msg_dynamic", "user_demo")
    db.commit()
    db.refresh(instance)

    assert instance.status == "cancelled"
    command = db.exec(
        select(ExecutionCommand).where(ExecutionCommand.execution_id == instance.id)
    ).one()
    assert command.command_type == "cancel"
    assert command.status == "applied"
    assert command.source_type == "chat"


def test_chat_cancel_preempts_model_worker_lease_and_fences_late_write() -> None:
    """用户停止必须抢占长模型租约，旧worker续租或落结果时只能得到fenced。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_dynamic_cancel_preempt",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )
    instance = SopInstance(
        id="execution_dynamic_cancel_preempt",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        kind="dynamic_task",
        active_slot_key="dynamic:session_dynamic_cancel_preempt",
        initiator_user_id="user_demo",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": "阻塞中的视觉复核"},
        current_plan_revision_id="plan_dynamic_cancel_preempt",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="running",
        source_ref="msg_dynamic_preempt",
    )
    db.add(
        AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="Chat trace preempt agent",
            status="active",
        )
    )
    db.add(session_row)
    db.add(instance)
    db.commit()
    old_lease = SopExecutionStore(db).claim(
        instance,
        worker_id="model-worker",
        ttl_seconds=300,
    )
    db.commit()

    assert _cancel_active_dynamic_execution(
        db,
        session_row,
        "msg_dynamic_preempt",
        "user_demo",
    )
    db.commit()
    db.refresh(instance)

    assert instance.status == "cancelled"
    with pytest.raises(SopExecutionFencedError):
        SopExecutionStore(db).renew(old_lease, ttl_seconds=300)


def test_chat_cancel_does_not_stop_an_unrelated_dynamic_execution() -> None:
    """普通 Turn 的停止只能收口自身，不能按会话猜测并误杀旧动态任务。"""

    db = _test_db()
    session_row = ChatSession(
        id="session_unrelated_cancel", tenant_id="tenant_demo", user_id="user_demo"
    )
    instance = SopInstance(
        id="execution_old_turn",
        tenant_id="tenant_demo",
        session_id=session_row.id,
        kind="dynamic_task",
        active_slot_key="dynamic:old-turn",
        initiator_user_id="user_demo",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": "旧任务"},
        current_plan_revision_id="plan_old_turn",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="waiting",
        source_ref="msg_old_turn",
    )
    db.add(session_row)
    db.add(instance)
    db.commit()

    assert not _cancel_active_dynamic_execution(db, session_row, "msg_new_turn", "user_demo")
    db.refresh(instance)
    assert instance.status == "waiting"


def test_cancelled_turn_preserves_persisted_partial_reply() -> None:
    """取消后的 assistant 消息保留已发送正文，而不被停止占位文案覆盖。"""

    db = _test_db()
    started_at = datetime(2026, 7, 4, 9, 8, 0)
    session_row = ChatSession(
        id="session_cancel_partial", tenant_id="tenant_demo", user_id="user_demo"
    )
    db.add(session_row)
    db.add(
        Message(
            id="msg_partial_user",
            tenant_id="tenant_demo",
            session_id=session_row.id,
            role="user",
            content="请生成长回复",
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={
                "message_id": "msg_partial_user",
                "client_turn_id": "client_partial",
            },
            created_at=started_at,
        )
    )
    for index, content in enumerate(("这是", "已经发送的", "部分回复。"), start=1):
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "msg_partial_user", "content": content},
                created_at=started_at + timedelta(milliseconds=index),
            )
        )
    db.commit()

    assert _persist_chat_turn_cancelled(
        db, "tenant_demo", session_row, "client_partial", "user_demo"
    )
    db.commit()

    assistant = db.exec(
        select(Message).where(
            Message.session_id == session_row.id, Message.role == "assistant"
        )
    ).one()
    assert assistant.content == "这是已经发送的部分回复。"
    assert assistant.metadata_json == {
        "turn_id": "msg_partial_user",
        "user_message_id": "msg_partial_user",
        "client_turn_id": "client_partial",
        "status": "cancelled",
        "partial": True,
        "terminal_reason": "user_cancelled",
        "last_seq": 3,
    }


def test_stream_interrupted_persists_terminal_trace_and_message() -> None:
    db = _test_db()
    started_at = datetime(2026, 7, 4, 9, 7, 0)
    session_row = ChatSession(id="session_interrupted", tenant_id="tenant_demo", user_id="user_demo")
    db.add(session_row)
    db.add(
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id=session_row.id,
            role="user",
            content="你是谁",
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={
                "message_id": "msg_user",
                "client_turn_id": "turn_interrupted",
                "message": "你是谁",
            },
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "responding",
                "text": "正在生成回复",
            },
            created_at=started_at + timedelta(milliseconds=100),
        )
    )
    db.commit()

    assert _persist_chat_turn_interrupted(db, "tenant_demo", session_row, "turn_interrupted", "GeneratorExit")
    db.commit()
    assert not _persist_chat_turn_interrupted(db, "tenant_demo", session_row, "turn_interrupted", "GeneratorExit")

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == session_row.id)
        .order_by(AgentEvent.created_at)
    ).all()
    interrupted_events = [event for event in events if event.event_type == "stream_interrupted"]
    assert len(interrupted_events) == 1
    assert interrupted_events[0].payload_json["turn_id"] == "msg_user"
    assert interrupted_events[0].payload_json["client_turn_id"] == "turn_interrupted"

    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == session_row.id)
        .order_by(Message.created_at)
    ).all()
    assistant_messages = [message for message in messages if message.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].metadata_json["status"] == "interrupted"

    traces = _build_turn_traces(messages, events, {})
    assert traces[0]["completed_at"] == interrupted_events[0].created_at.isoformat()
    assert all(line["state"] != "running" for line in traces[0]["lines"])
    assert any(
        line["id"] == "generation_interrupted"
        and line["text"] == "响应生成中断"
        and line["state"] == "failed"
        for line in traces[0]["lines"]
    )


def test_relay_event_payload_maps_persisted_router_and_status_events() -> None:
    status_event = AgentEvent(
        id="evt_status",
        tenant_id="tenant_demo",
        session_id="session_relay",
        event_type="stream_status",
        payload_json={"turn_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
        created_at=datetime(2026, 7, 4, 9, 9, 0),
    )
    router_event = AgentEvent(
        id="evt_router",
        tenant_id="tenant_demo",
        session_id="session_relay",
        event_type="router_decision_created",
        payload_json={"turn_id": "msg_user", "decision": "answer_only"},
        created_at=datetime(2026, 7, 4, 9, 9, 1),
    )

    status_name, status_payload = _relay_event_payload(status_event)
    router_name, router_payload = _relay_event_payload(router_event)

    assert status_name == "status"
    assert status_payload["sessionId"] == "session_relay"
    assert status_payload["phase"] == "routing"
    assert router_name == "router_decision"
    assert router_payload["decision"] == "answer_only"


def test_event_projection_keeps_persisted_envelope_authoritative() -> None:
    """事件业务payload不能伪造事件身份、类型或创建时间，避免破坏恢复排序。"""

    row = AgentEvent(
        id="evt_authoritative",
        tenant_id="tenant_demo",
        session_id="session_authoritative",
        event_type="stream_end",
        payload_json={
            "id": "spoofed-id",
            "event": "complete",
            "type": "complete",
            "event_type": "complete",
            "created_at": "2099-01-01T00:00:00",
            "content": "正文",
        },
        created_at=datetime(2026, 7, 4, 9, 9, 0),
    )

    projected = _normalized_session_event_payload(row)
    event_name, relay_payload = _relay_event_payload(row)

    assert projected["id"] == row.id
    assert projected["event"] == "stream_end"
    assert projected["type"] == "stream_end"
    assert projected["event_type"] == "stream_end"
    assert projected["created_at"] == row.created_at.isoformat()
    assert event_name == "stream_end"
    assert relay_payload["kind"] == "stream_end"
    assert relay_payload["sessionId"] == row.session_id
    assert relay_payload["timestamp"] == row.created_at.isoformat()


def test_session_event_window_preserves_latest_turn_after_old_history() -> None:
    """历史事件达到窗口上限时，最新运行Turn仍完整可见而不只返回旧前缀。"""

    db = _test_db()
    tenant = Tenant(id="tenant_latest", name="Latest")
    user = User(id="user_latest", tenant_id=tenant.id, username="latest", password_hash="x")
    session_row = ChatSession(
        id="session_latest_turn",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    started_at = datetime(2026, 8, 27, 12, 0, 0)
    for index in range(100):
        db.add(
            AgentEvent(
                id=f"evt_old_{index:03d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "old_turn", "content": "o"},
                created_at=started_at + timedelta(microseconds=index),
            )
    )
    latest_started_at = started_at + timedelta(seconds=1)
    db.add(
        AgentEvent(
            id="evt_000_same_timestamp",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="stream_delta",
            payload_json={"turn_id": "old_turn", "content": "old"},
            created_at=latest_started_at,
        )
    )
    db.add(
        AgentEvent(
            id="evt_latest_received",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={"message_id": "latest_turn", "turn_id": "latest_turn"},
            created_at=latest_started_at,
        )
    )
    for index in range(120):
        db.add(
            AgentEvent(
                id=f"evt_latest_{index:03d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "latest_turn", "content": "n"},
                created_at=latest_started_at + timedelta(microseconds=index + 1),
            )
        )
    db.commit()

    rows = _session_event_history_rows(db, tenant.id, session_row.id, limit=100)

    assert len(rows) == 221
    assert rows[0].id == "evt_old_000"
    assert rows[-1].id == "evt_latest_119"
    assert sum(row.event_type == "stream_delta" for row in rows if row.id.startswith("evt_latest_")) == 120


def test_session_event_window_filters_late_other_turn_and_bounds_latest_extension() -> None:
    """最新Turn按身份筛选且最多保留5000条扩展，避免并发迟到事件或超长响应失控。"""

    assert SESSION_EVENT_LATEST_TURN_MAX == 5000
    db = _test_db()
    tenant = Tenant(id="tenant_latest_bound", name="Latest bound")
    user = User(
        id="user_latest_bound",
        tenant_id=tenant.id,
        username="latest-bound",
        password_hash="x",
    )
    session_row = ChatSession(
        id="session_latest_bound",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    started_at = datetime(2026, 8, 27, 12, 0, 0)
    for index in range(100):
        db.add(
            AgentEvent(
                id=f"evt_bound_old_{index:03d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "old_turn", "content": "o"},
                created_at=started_at + timedelta(microseconds=index),
            )
        )
    latest_started_at = started_at + timedelta(seconds=1)
    db.add(
        AgentEvent(
            id="evt_bound_user",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={"message_id": "bound_turn", "turn_id": "bound_turn"},
            created_at=latest_started_at,
        )
    )
    for index in range(5105):
        db.add(
            AgentEvent(
                id=f"evt_bound_latest_{index:04d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "bound_turn", "content": "n"},
                created_at=latest_started_at + timedelta(microseconds=index + 1),
            )
        )
    db.add(
        AgentEvent(
            id="evt_bound_late_other",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="stream_delta",
            payload_json={"turn_id": "old_turn", "content": "late"},
            created_at=latest_started_at + timedelta(seconds=1),
        )
    )
    db.add(
        AgentEvent(
            id="evt_bound_complete",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="complete",
            payload_json={"turn_id": "bound_turn", "reply": "完成"},
            created_at=latest_started_at + timedelta(seconds=2),
        )
    )
    db.commit()

    rows = _session_event_history_rows(db, tenant.id, session_row.id, limit=100)
    latest_rows = [
        row
        for row in rows
        if row.id == "evt_bound_user" or row.id.startswith("evt_bound_latest_")
    ]

    # complete 占用一个最新 Turn 扩展槽位，终态本身仍由 terminal anchor 保留。
    assert len(latest_rows) == SESSION_EVENT_LATEST_TURN_MAX - 1
    assert latest_rows[0].id == "evt_bound_user"
    assert rows[-1].id == "evt_bound_complete"
    assert "evt_bound_late_other" not in {row.id for row in rows}
    assert all(
        (row.payload_json or {}).get("turn_id") != "old_turn"
        for row in rows
        if row.created_at >= latest_started_at
    )


def test_stream_end_is_not_success_terminal_for_interruption_guard() -> None:
    """仅有stream_end而没有complete时仍允许写入中断终态，避免假装成功。"""

    db = _test_db()
    tenant = Tenant(id="tenant_stream_end", name="Stream end")
    user = User(id="user_stream_end", tenant_id=tenant.id, username="stream-end", password_hash="x")
    session_row = ChatSession(
        id="session_stream_end",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    db.add(
        AgentEvent(
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="stream_end",
            payload_json={"turn_id": "turn_stream_end"},
        )
    )
    db.commit()

    assert not _turn_has_terminal_event(db, tenant.id, session_row.id, "turn_stream_end")


def test_session_event_window_preserves_tenant_and_session_boundaries() -> None:
    """事件窗口查询拒绝跨租户或非会话所有者读取，避免补回逻辑扩大可见范围。"""

    db = _test_db()
    tenant_a = Tenant(id="tenant_events_a", name="Events A")
    tenant_b = Tenant(id="tenant_events_b", name="Events B")
    user_a = User(id="user_events_a", tenant_id=tenant_a.id, username="events-a", password_hash="x")
    user_b = User(id="user_events_b", tenant_id=tenant_b.id, username="events-b", password_hash="x")
    session_row = ChatSession(
        id="session_events_a",
        tenant_id=tenant_a.id,
        user_id=user_a.id,
        agent_id="agent_demo",
    )
    db.add_all(
        [
            tenant_a,
            tenant_b,
            user_a,
            user_b,
            session_row,
            AgentEvent(
                id="evt_events_a",
                tenant_id=tenant_a.id,
                session_id=session_row.id,
                event_type="complete",
                payload_json={"turn_id": "turn_a", "reply": "仅租户A可见"},
            ),
        ]
    )
    db.commit()

    with pytest.raises(HTTPException) as tenant_error:
        list_chat_session_events(
            session_row.id,
            tenant_id=tenant_b.id,
            current_user=user_b,
            db=db,
        )
    assert tenant_error.value.status_code == 404

    with pytest.raises(HTTPException) as user_error:
        list_chat_session_events(
            session_row.id,
            tenant_id=tenant_a.id,
            current_user=user_b,
            db=db,
        )
    assert user_error.value.status_code == 403


def test_turn_trace_without_terminal_event_stays_open_for_refresh_recovery() -> None:
    started_at = datetime(2026, 7, 4, 9, 6, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_refresh",
            role="user",
            content="你是谁",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_refresh",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你是谁"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_refresh",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "routing",
                "text": "正在判断用户意图",
            },
            created_at=started_at + timedelta(milliseconds=100),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] is None
    assert any(line["id"] == "decision_router" and line["state"] == "running" for line in traces[0]["lines"])
    assert all(line["id"] != "generation_stopped" for line in traces[0]["lines"])


def test_turn_trace_ignores_trace_events_without_turn_id() -> None:
    started_at = datetime(2026, 7, 4, 9, 8, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            role="user",
            content="北京今天天气如何",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            role="assistant",
            content="北京今天晴朗。",
            created_at=started_at + timedelta(seconds=50),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "北京今天天气如何"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="router_decision_created",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "decision": "answer_only",
                "user_intent": "查询天气",
                "reason": "实时信息查询",
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_selected",
            payload_json={
                "skill_slug": "maomao-weather",
                "skill_name": "weather",
                "reason": "匹配天气查询能力",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="tool_result",
            payload_json={
                "toolName": "weather",
                "rawToolName": "maomao-weather",
                "success": True,
                "content": {"tool_name": "maomao-weather", "success": True, "data": {"found": True}},
            },
            created_at=started_at + timedelta(seconds=4),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_trace",
            payload_json={
                "skill_slug": "maomao-weather",
                "phase": "planning",
                "message": "正在根据 SKILL.md 生成 runner",
            },
            created_at=started_at + timedelta(seconds=4),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_trace",
            payload_json={
                "skill_slug": "maomao-weather",
                "phase": "reflection_reviewed",
                "message": "已完成运行结果校验",
                "review": {"reason": "结果可用"},
            },
            created_at=started_at + timedelta(seconds=5),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_run_finished",
            payload_json={"skill_slug": "maomao-weather", "success": True},
            created_at=started_at + timedelta(seconds=6),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant",
                "user_message_id": "msg_user",
                "reply": "北京今天晴朗。",
            },
            created_at=started_at + timedelta(seconds=50),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    texts = [line["text"] for line in traces[0]["lines"]]
    assert traces[0]["turn_id"] == "msg_user"
    assert "选择通用技能 weather" not in texts
    assert "调用工具 weather" not in texts
    assert "正在根据 SKILL.md 生成 runner" not in texts
    assert "已完成运行结果校验" not in texts
    assert "通用技能运行完成" not in texts


def test_turn_trace_restores_stream_tool_and_skill_events_with_turn_id() -> None:
    started_at = datetime(2026, 7, 4, 9, 9, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            role="user",
            content="北京今天天气如何",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            role="assistant",
            content="北京今天晴朗。",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(seconds=50),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "北京今天天气如何"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="general_skill_trace",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "skill_slug": "maomao-weather",
                "phase": "planning",
                "message": "正在根据 SKILL.md 生成 runner",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="tool_result",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "toolName": "weather",
                "rawToolName": "maomao-weather",
                "success": True,
                "content": {"tool_name": "maomao-weather", "success": True, "data": {"found": True}},
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="agent_loop_completed",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "iteration": 1,
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant",
                "user_message_id": "msg_user",
                "reply": "北京今天晴朗。",
            },
            created_at=started_at + timedelta(seconds=50),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    texts = [line["text"] for line in traces[0]["lines"]]
    assert "正在根据 SKILL.md 生成 runner" in texts
    assert "调用工具 weather" in texts
    assert "重新分析执行动作" in texts


def test_turn_trace_uses_message_id_for_repeated_user_text() -> None:
    started_at = datetime(2026, 7, 3, 10, 0, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="assistant",
            content="你好！",
            created_at=started_at + timedelta(seconds=2),
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="user",
            content="你好",
            created_at=started_at + timedelta(seconds=10),
        ),
        Message(
            id="msg_assistant_second",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="assistant",
            content="请问有什么可以帮您？",
            created_at=started_at + timedelta(seconds=12),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user_first", "reply": "你好！"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=10),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_second",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第二轮问候",
            },
            created_at=started_at + timedelta(seconds=11),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user_second", "reply": "请问有什么可以帮您？"},
            created_at=started_at + timedelta(seconds=12),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["msg_user_first", "msg_user_second"]
    assert traces[1]["user_message_id"] == "msg_user_second"
    assert any(line["text"] == "判断意图 问候" and line["detail"] == "第二轮问候" for line in traces[1]["lines"])


def test_turn_trace_keeps_late_trace_events_after_assistant_event() -> None:
    started_at = datetime(2026, 7, 6, 10, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            role="assistant",
            content="你好！",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(seconds=2),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="stream_status",
            payload_json={"user_message_id": "msg_user", "turn_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=200),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant", "user_message_id": "msg_user", "reply": "你好！"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user",
                "turn_id": "msg_user",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "晚到的意图明细也要保留",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="step_result",
            payload_json={
                "user_message_id": "msg_user",
                "turn_id": "msg_user",
                "reply": "直接回复问候",
            },
            created_at=started_at + timedelta(seconds=4),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert len(traces) == 1
    assert traces[0]["completed_at"] == (started_at + timedelta(seconds=2)).isoformat()
    assert any(
        line["text"] == "判断意图 问候" and line["detail"] == "晚到的意图明细也要保留"
        for line in traces[0]["lines"]
    )
    assert any(line["text"] == "完成步骤判断" and line["detail"] == "直接回复问候" for line in traces[0]["lines"])
    assert all(line["state"] != "running" for line in traces[0]["lines"])


def test_turn_trace_does_not_merge_interleaved_repeated_turns() -> None:
    started_at = datetime(2026, 7, 3, 10, 30, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="assistant",
            content="我是第一个回答。",
            created_at=started_at + timedelta(seconds=12),
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="user",
            content="你好",
            created_at=started_at + timedelta(seconds=2),
        ),
        Message(
            id="msg_assistant_second",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="assistant",
            content="我是第二个回答。",
            created_at=started_at + timedelta(seconds=14),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_first",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第一轮问候",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_second",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第二轮问候",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_first",
                "user_message_id": "msg_user_first",
                "reply": "我是第一个回答。",
            },
            created_at=started_at + timedelta(seconds=12),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_second",
                "user_message_id": "msg_user_second",
                "reply": "我是第二个回答。",
            },
            created_at=started_at + timedelta(seconds=14),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["msg_user_first", "msg_user_second"]
    assert traces[0]["completed_at"] == (started_at + timedelta(seconds=12)).isoformat()
    assert traces[1]["completed_at"] == (started_at + timedelta(seconds=14)).isoformat()
    first_details = [line.get("detail") for line in traces[0]["lines"]]
    second_details = [line.get("detail") for line in traces[1]["lines"]]
    assert "第一轮问候" in first_details
    assert "第二轮问候" not in first_details
    assert "第二轮问候" in second_details
    assert "第一轮问候" not in second_details


def test_turn_trace_without_message_id_does_not_bind_user_messages() -> None:
    started_at = datetime(2026, 7, 3, 11, 0, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            role="user",
            content="第一句",
            created_at=started_at,
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            role="user",
            content="第二句",
            created_at=started_at + timedelta(seconds=10),
        ),
    ]
    events = [
        AgentEvent(
            id="evt_user_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="user_message_received",
            payload_json={"message": "第二句"},
            created_at=started_at,
        ),
        AgentEvent(
            id="evt_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="assistant_message_created",
            payload_json={"reply": "收到"},
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            id="evt_user_second",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="user_message_received",
            payload_json={"message": "第二句"},
            created_at=started_at + timedelta(seconds=10),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["evt_user_first", "evt_user_second"]
    assert [trace["user_message_id"] for trace in traces] == [None, None]


def test_message_turn_ids_from_events_use_ids_not_message_text() -> None:
    started_at = datetime(2026, 7, 3, 12, 0, 0)
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_first",
                "user_message_id": "msg_user_first",
                "reply": "你好！",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=10),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_second",
                "turn_id": "msg_user_second",
                "reply": "请问有什么可以帮您？",
            },
            created_at=started_at + timedelta(seconds=11),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message": "你好"},
            created_at=started_at + timedelta(seconds=20),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant_without_user_id", "reply": "旧事件不应猜测归属"},
            created_at=started_at + timedelta(seconds=21),
        ),
    ]

    assert _message_turn_ids_from_events(events) == {
        "msg_user_first": "msg_user_first",
        "msg_assistant_first": "msg_user_first",
        "msg_user_second": "msg_user_second",
        "msg_assistant_second": "msg_user_second",
    }


def test_message_read_uses_metadata_turn_id_when_event_mapping_is_missing() -> None:
    row = Message(
        id="msg_assistant",
        tenant_id="tenant_demo",
        session_id="session_repeat",
        role="assistant",
        content="你好",
        metadata_json={"turn_id": "msg_user"},
        created_at=datetime(2026, 7, 4, 12, 0, 0),
    )

    assert message_read(row).turn_id == "msg_user"


def test_session_event_window_keeps_terminal_event_after_long_stream() -> None:
    """长流超过历史窗口时仍返回 complete，避免刷新端把已完成 Turn 判成运行中。"""

    assert SESSION_EVENT_HISTORY_LIMIT == 1024
    assert SESSION_EVENT_HISTORY_MAX_LIMIT == 5000
    db = _test_db()
    tenant = Tenant(id="tenant_demo", name="Demo")
    user = User(id="user_demo", tenant_id=tenant.id, username="demo", password_hash="x")
    session_row = ChatSession(
        id="session_long_stream",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    started_at = datetime(2026, 8, 27, 12, 0, 0)
    for index in range(1024):
        db.add(
            AgentEvent(
                id=f"evt_long_{index:03d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "msg_long", "content": "x"},
                created_at=started_at + timedelta(microseconds=index),
            )
        )
    db.add(
        AgentEvent(
            id="evt_long_complete",
            tenant_id=tenant.id,
            session_id=session_row.id,
            event_type="complete",
            payload_json={"turn_id": "msg_long", "reply": "完成"},
            created_at=started_at + timedelta(microseconds=1025),
        )
    )
    db.commit()

    rows = _session_event_history_rows(db, tenant.id, session_row.id, limit=500)
    assert len(rows) == 501
    assert rows[-1].event_type == "complete"
    default_rows = _session_event_history_rows(db, tenant.id, session_row.id)
    assert len(default_rows) == 1025
    assert default_rows[-1].event_type == "complete"
    projected = list_chat_session_events(
        session_row.id,
        tenant_id=tenant.id,
        current_user=user,
        db=db,
        limit=500,
    )
    assert projected[-1]["event_type"] == "complete"


def test_session_event_window_bounds_terminal_anchor_query() -> None:
    """终态锚点补回在 SQL 查询层最多读取1024条，避免历史终态无限膨胀。"""

    assert SESSION_EVENT_TERMINAL_MAX == 1024
    db = _test_db()
    tenant = Tenant(id="tenant_terminal_bound", name="Terminal bound")
    user = User(
        id="user_terminal_bound",
        tenant_id=tenant.id,
        username="terminal-bound",
        password_hash="x",
    )
    session_row = ChatSession(
        id="session_terminal_bound",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    started_at = datetime(2026, 8, 27, 12, 0, 0)
    for index in range(1024):
        db.add(
            AgentEvent(
                id=f"evt_terminal_history_{index:04d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="stream_delta",
                payload_json={"turn_id": "old_turn", "content": "history"},
                created_at=started_at + timedelta(microseconds=index),
            )
        )
    terminal_started_at = started_at + timedelta(seconds=1)
    for index in range(1100):
        db.add(
            AgentEvent(
                id=f"evt_terminal_{index:04d}",
                tenant_id=tenant.id,
                session_id=session_row.id,
                event_type="complete",
                payload_json={"turn_id": f"turn_{index:04d}"},
                created_at=terminal_started_at + timedelta(microseconds=index),
            )
        )
    db.commit()

    rows = _session_event_history_rows(db, tenant.id, session_row.id)

    terminal_rows = [row for row in rows if row.event_type == "complete"]
    assert len(terminal_rows) == SESSION_EVENT_TERMINAL_MAX
    assert terminal_rows[0].id == "evt_terminal_0076"
    assert terminal_rows[-1].id == "evt_terminal_1099"


def test_session_event_route_rejects_limit_above_public_bound() -> None:
    """HTTP 事件窗口只接受不超过5000的请求，避免客户端绕过资源契约。"""

    db = _test_db()
    tenant = Tenant(id="tenant_route_bound", name="Route bound")
    user = User(
        id="user_route_bound",
        tenant_id=tenant.id,
        username="route-bound",
        password_hash="x",
    )
    session_row = ChatSession(
        id="session_route_bound",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id="agent_demo",
    )
    db.add_all([tenant, user, session_row])
    db.commit()

    def override_session() -> Session:
        """为路由测试注入隔离的 SQLite 会话。"""

        return db

    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            f"/api/chat/sessions/{session_row.id}/events",
            params={"tenant_id": tenant.id, "limit": SESSION_EVENT_HISTORY_MAX_LIMIT + 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    route = next(
        item
        for item in chat_api.router.routes
        if isinstance(item, APIRoute) and item.path == "/api/chat/sessions/{session_id}/events"
    )
    limit_parameter = next(item for item in route.dependant.query_params if item.name == "limit")
    assert any(getattr(metadata, "le", None) == SESSION_EVENT_HISTORY_MAX_LIMIT for metadata in limit_parameter.field_info.metadata)


def _test_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
