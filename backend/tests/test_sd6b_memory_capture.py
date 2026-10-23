"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_memory_capture.py
@CallChain  : pytest → AgentLoop/记忆捕获注册 → MemoryCaptureScope/MemoryCaptureJob
@Description: 锁定 SD-6B 可信来源、原完成事务登记和重放幂等的行为合同。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop, PreparedTurn
from app.memory.capture import memory_source_anchor_is_current, source_anchor_from_message
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureJob,
    MemoryCaptureScope,
    Message,
    ModelConfig,
    User,
)
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult


def _engine():
    """创建当前测试专用的内存 SQLite，避免触碰应用默认数据库。"""

    return create_engine("sqlite://", poolclass=StaticPool)


def _seed_turn(db: Session) -> tuple[AgentLoop, ChatSession, ModelConfig, Message]:
    """建立包含用户、活动 Agent、会话和模型配置的真实记忆登记前置。"""

    user = User(
        id="user_sd6b_capture",
        tenant_id="tenant_sd6b_capture",
        username="sd6b-capture",
        password_hash="x",
    )
    agent = AgentProfile(
        id="agent_sd6b_capture",
        tenant_id=user.tenant_id,
        name="SD6B Capture Agent",
        owner_user_id=user.id,
        status="active",
    )
    session = ChatSession(
        id="session_sd6b_capture",
        tenant_id=user.tenant_id,
        user_id=user.id,
        agent_id=agent.id,
    )
    model = ModelConfig(
        id="model_sd6b_capture",
        tenant_id=user.tenant_id,
        name="SD6B Capture Model",
        is_default=True,
        api_key_encrypted="encrypted",
        model="capture-model",
    )
    db.add_all([user, agent, session, model])
    db.commit()
    loop = AgentLoop(db)
    request = ChatTurnRequest(
        tenant_id=user.tenant_id,
        session_id=session.id,
        agent_id=agent.id,
        user_id=user.id,
        client_turn_id="client_sd6b_capture",
        message="请记住我喜欢青柠茶。",
    )
    user_message, _ = loop._append_user_input_message(request, session.id)
    db.flush()
    return loop, session, model, user_message


def test_sd6b_persistent_capture_contract_is_not_hidden_in_metadata() -> None:
    """持久来源、任务和后续按键状态必须有原生表字段。"""

    assert {
        "source_sequence",
        "clear_generation",
        "permission_generation",
        "row_version",
    }.issubset(MemoryCaptureScope.__table__.columns.keys())
    assert {
        "source_message_id",
        "source_assistant_message_id",
        "source_sequence",
        "user_generation",
        "agent_generation",
        "model_config_id",
        "model_capture_generation",
        "model_contract_hash",
        "source_manifest_json",
        "source_hash",
        "status",
    }.issubset(MemoryCaptureJob.__table__.columns.keys())


def test_memory_source_anchor_rejects_slots_after_clear_generation_changes() -> None:
    """SOP 恢复使用的来源锚点一旦清空代数变化就必须失效。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        _, session, _, user_message = _seed_turn(db)
        anchor = source_anchor_from_message(user_message)
        assert anchor is not None
        assert memory_source_anchor_is_current(
            db,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            anchor=anchor,
        )

        scope = db.exec(
            select(MemoryCaptureScope).where(
                MemoryCaptureScope.tenant_id == session.tenant_id,
                MemoryCaptureScope.user_id == session.user_id,
                MemoryCaptureScope.scope_kind == "user",
            )
        ).one()
        scope.clear_generation += 1
        db.add(scope)
        db.commit()

        assert not memory_source_anchor_is_current(
            db,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            anchor=anchor,
        )


def test_sd6b_original_completion_registers_once_and_reuses_same_source_after_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一原始用户消息的完成重放只保留一条持久任务，并在提交后才唤醒。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    wakeups: list[str] = []
    monkeypatch.setattr("app.core.agent_loop.wake_memory_capture_job", wakeups.append)

    with Session(engine, expire_on_commit=False) as db:
        loop, session, model, user_message = _seed_turn(db)
        assistant = Message(
            id="assistant_sd6b_capture",
            tenant_id=session.tenant_id,
            session_id=session.id,
            role="assistant",
            content="好的，我记住了。",
            metadata_json={"turn_id": user_message.id, "user_message_id": user_message.id},
        )
        db.add(assistant)
        db.flush()

        first = loop._enqueue_memory_capture(
            ChatTurnRequest(
                tenant_id=session.tenant_id,
                session_id=session.id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                client_turn_id="client_sd6b_capture",
                message=user_message.content,
            ),
            session,
            StepAgentResult(),
            None,
            model,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            commit=False,
        )
        assert wakeups == []
        db.commit()
        loop._flush_memory_capture_wakeups()

        second = loop._enqueue_memory_capture(
            ChatTurnRequest(
                tenant_id=session.tenant_id,
                session_id=session.id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                client_turn_id="client_sd6b_capture-replayed",
                message="客户端重放不能改变原始来源。",
            ),
            session,
            StepAgentResult(),
            None,
            model,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            commit=False,
        )
        db.commit()
        loop._flush_memory_capture_wakeups()

        jobs = db.exec(select(MemoryCaptureJob)).all()
        assert len(jobs) == 1
        assert first == second == [{"job_id": jobs[0].id, "job_name": "memory.capture_turn"}]
        assert jobs[0].source_message_id == user_message.id
        assert jobs[0].source_assistant_message_id == assistant.id
        assert jobs[0].source_sequence == 1
        assert len(wakeups) == 1


def test_sd6b_sync_agent_loop_registers_capture_in_the_same_completion_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 AgentLoop 的 complete 与记忆任务必须由同一次提交产生。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    wakeups: list[str] = []
    monkeypatch.setattr("app.core.agent_loop.wake_memory_capture_job", wakeups.append)
    with Session(engine, expire_on_commit=False) as db:
        loop, session, model, user_message = _seed_turn(db)
        request = ChatTurnRequest(
            tenant_id=session.tenant_id,
            session_id=session.id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            message=user_message.content,
            client_turn_id="sync-source",
        )
        loop._prepare_turn = lambda _request: PreparedTurn(
            chat_session=session,
            model_config=model,
            active_skill=None,
            router_decision=RouterDecision(decision="answer_only"),
            step_result=StepAgentResult(action="reply", reply="同步完成"),
            tool_result=None,
            memory_context=[],
            conversation_context={},
            reply_override="同步完成",
            user_message_id=user_message.id,
        )

        response = loop.handle_turn(request)

        assert response.reply == "同步完成"
        job = db.exec(select(MemoryCaptureJob)).one()
        assert job.source_message_id == user_message.id
        assert db.exec(
            select(MemoryCaptureJob).where(MemoryCaptureJob.status == "pending")
        ).one().id == job.id
        assert len(wakeups) == 1


def test_sd6b_replayed_input_reuses_source_message_without_advancing_sequence() -> None:
    """同一冻结输入重放必须复用原消息和来源序号，而不是再次产生 T0。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        loop, session, _model, first_message = _seed_turn(db)
        persisted_time_context = dict((first_message.metadata_json or {}).get("time_context") or {})
        request = ChatTurnRequest(
            tenant_id=session.tenant_id,
            session_id=session.id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            message="客户端重放的正文不能替换原消息。",
        )

        replayed, _ = loop._append_user_input_message(
            request,
            session.id,
            time_context=persisted_time_context,
        )

        assert replayed.id == first_message.id
        user_scope = db.exec(
            select(MemoryCaptureScope).where(MemoryCaptureScope.scope_kind == "user")
        ).one()
        assert user_scope.source_sequence == 1
        assert len(db.exec(select(Message).where(Message.role == "user")).all()) == 1


def test_sd6b_stream_finalization_registers_before_complete_event_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE 的 stream_end、complete 和记忆任务在同一最终事务中落账后再唤醒。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    wakeups: list[str] = []
    monkeypatch.setattr("app.core.agent_loop.wake_memory_capture_job", wakeups.append)
    with Session(engine, expire_on_commit=False) as db:
        loop, session, model, user_message = _seed_turn(db)
        loop.events.bind_turn(user_message.id, "stream-source")
        assistant_reply = loop._finalize_turn(
            session,
            session.tenant_id,
            "流式完成",
            StepAgentResult(action="reply", reply="流式完成"),
            user_message.content,
            user_message_id=user_message.id,
            client_turn_id="stream-source",
        )
        refs = loop._enqueue_memory_capture(
            ChatTurnRequest(
                tenant_id=session.tenant_id,
                session_id=session.id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                message=user_message.content,
                client_turn_id="stream-source",
            ),
            session,
            StepAgentResult(action="reply", reply=assistant_reply),
            None,
            model,
            user_message_id=user_message.id,
            commit=False,
        )
        loop._stream_event(
            "stream_end",
            session,
            {"turn_id": user_message.id, "user_message_id": user_message.id},
            commit=False,
        )
        loop._stream_event(
            "complete",
            session,
            {"turn_id": user_message.id, "user_message_id": user_message.id},
            terminal_once=True,
        )
        loop._wake_memory_capture_refs(refs)

        assert db.exec(select(MemoryCaptureJob)).one().source_message_id == user_message.id
        assert len(wakeups) == 1


def test_sd6b_capture_registration_rolls_back_with_original_completion_transaction() -> None:
    """原完成事务回滚时，任务和入队事件都不可见。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        loop, session, model, user_message = _seed_turn(db)
        assistant = Message(
            id="assistant_sd6b_rollback",
            tenant_id=session.tenant_id,
            session_id=session.id,
            role="assistant",
            content="回滚的回复。",
            metadata_json={"turn_id": user_message.id},
        )
        db.add(assistant)
        db.flush()
        result = loop._enqueue_memory_capture(
            ChatTurnRequest(
                tenant_id=session.tenant_id,
                session_id=session.id,
                agent_id=session.agent_id,
                user_id=session.user_id,
                message=user_message.content,
            ),
            session,
            StepAgentResult(),
            None,
            model,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            commit=False,
        )
        assert result
        db.rollback()
        assert db.exec(select(MemoryCaptureJob)).all() == []
        assert db.exec(select(MemoryCaptureScope)).all() == []


def test_sd6b_registration_never_falls_back_to_latest_message_or_wrong_turn() -> None:
    """错轮或缺失冻结来源不能借用会话最新消息创建记忆任务。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        loop, session, model, first_user = _seed_turn(db)
        second_user = Message(
            id="user_sd6b_other_turn",
            tenant_id=session.tenant_id,
            session_id=session.id,
            role="user",
            content="另一轮输入。",
            metadata_json={"client_turn_id": "other-turn"},
        )
        assistant = Message(
            id="assistant_sd6b_other_turn",
            tenant_id=session.tenant_id,
            session_id=session.id,
            role="assistant",
            content="另一轮回复。",
            metadata_json={"turn_id": second_user.id},
        )
        db.add_all([second_user, assistant])
        db.flush()
        request = ChatTurnRequest(
            tenant_id=session.tenant_id,
            session_id=session.id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            message="缺失来源身份的重放。",
        )

        assert loop._enqueue_memory_capture(
            request,
            session,
            StepAgentResult(),
            None,
            model,
            assistant_message_id=assistant.id,
            commit=False,
        ) == []
        db.commit()
        assert db.exec(select(MemoryCaptureJob)).all() == []
        assert first_user.id != second_user.id


def test_sd6b_migration_is_chained_after_sd5() -> None:
    """SD-6B 的首个迁移必须接在 SD-5 0086 之后。"""

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20261001_0087_memory_capture_contract.py"
    )
    assert migration.exists()
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "20261001_0087"' in source
    assert 'down_revision: str | None = "20260928_0086"' in source


def test_sd6b_migration_upgrades_legacy_sqlite_without_create_all(tmp_path) -> None:
    """历史 SQLite 只经 Alembic 升级即可获得全部持久合同。"""

    database_url = f"sqlite:///{tmp_path / 'sd6b-migration.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260928_0086')"))
        connection.execute(
            text(
                "CREATE TABLE model_configs (id VARCHAR(512) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_profiles (id VARCHAR(512) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE memories (id VARCHAR(512) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL, user_id VARCHAR(128) NOT NULL, "
                "content TEXT NOT NULL)"
            )
        )

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20261001_0087")

    with engine.begin() as connection:
        tables = set(inspect(engine).get_table_names())
        assert {
            "memory_capture_scopes",
            "memory_capture_jobs",
            "memory_key_states",
        }.issubset(tables)
        assert "memory_capture_generation" in {
            item["name"] for item in inspect(engine).get_columns("model_configs")
        }
        assert {
            "user_generation",
            "agent_generation",
            "user_permission_generation",
            "agent_permission_generation",
        }.issubset(
            {item["name"] for item in inspect(engine).get_columns("memory_key_states")}
        )
        connection.execute(
            text(
                "INSERT INTO memory_capture_scopes "
                "(id, tenant_id, user_id, scope_kind, agent_scope_key, created_at, updated_at) "
                "VALUES ('scope_one', 'tenant', 'user', 'user', '__user__', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(Exception):  # noqa: B017 - 方言无关地锁定唯一约束。
            connection.execute(
                text(
                    "INSERT INTO memory_capture_scopes "
                    "(id, tenant_id, user_id, scope_kind, agent_scope_key, created_at, updated_at) "
                    "VALUES ('scope_two', 'tenant', 'user', 'user', '__user__', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )

    command.upgrade(config, "20261001_0089")
    with engine.begin() as connection:
        assert {
            "agent_scope_key",
            "source_sequence",
            "capture_job_id",
            "source_hash",
            "user_generation",
            "agent_generation",
            "user_permission_generation",
            "agent_permission_generation",
        }.issubset({item["name"] for item in inspect(engine).get_columns("memories")})

    with engine.begin() as connection:
        assert "claim_dispatch_count" in {
            item["name"] for item in inspect(engine).get_columns("memory_capture_jobs")
        }
    command.downgrade(config, "20260928_0086")


def test_existing_sqlite_desktop_restarts_with_claim_budget(tmp_path) -> None:
    """桌面旧 SD-6B 表经原初始化两次仍保留来源，只增加计数和固定截止。"""
    from app.db.sqlite_legacy import initialize_sqlite_database

    engine = create_engine(f"sqlite:///{tmp_path / 'desktop.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE memory_capture_jobs DROP COLUMN claim_dispatch_count")
    initialize_sqlite_database(engine)
    initialize_sqlite_database(engine)
    assert "claim_dispatch_count" in {
        column["name"] for column in inspect(engine).get_columns("memory_capture_jobs")
    }
    engine.dispose()
