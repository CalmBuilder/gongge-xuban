"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_settlement_regressions.py
@CallChain  : pytest → 独立数据库连接 → 领取/清空/T3 → 记忆读取
@Description: 复现 R2 旧 ORM 持有者和序号零基线删除，并验证实际清空竞争。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import sleep

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.memories import clear_my_memories
from app.db.models import AgentEvent, MemoryCaptureJob, MemoryRecord, Message, Tenant, User, utc_now
from app.memory import worker
from app.memory.service import MemoryService
from tests.test_sd6b_memory_worker import _seed_worker_job
from tests.integration.test_sd6b_memory_worker_mysql import _seed_job, _upgrade


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def isolated(request, tmp_path):
    """使用文件 SQLite 或夹具随机 MySQL，全部变更限定在隔离测试库。"""
    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        _upgrade(url)
        engine = create_engine(url, pool_pre_ping=True)
        seed = _seed_job
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 2})
        SQLModel.metadata.create_all(engine)
        seed = _seed_worker_job
    try:
        with Session(engine, expire_on_commit=False) as db:
            job = seed(db)
            if db.get(Tenant, job.tenant_id) is None:
                db.add(Tenant(id=job.tenant_id, name="隔离租户"))
                db.commit()
            yield_ids = (job.id, job.tenant_id, job.user_id, job.agent_id)
        yield engine, yield_ids
    finally:
        engine.dispose()


@pytest.mark.parametrize("precision", ["equal", "mixed_cache", "mysql_roundtrip"])
def test_same_second_manifest_keeps_own_source_before_assistant(isolated, precision):
    """秒精度下随机 ID 不得漏掉本轮来源，也不得把同秒未来轮次放进 cutoff。"""
    from app.core.agent_loop import AgentLoop
    from app.db.models import ChatSession, ModelConfig
    from app.memory.capture import register_memory_capture_job
    from app.session.session_schema import ChatTurnRequest

    engine, (job_id, tenant_id, user_id, agent_id) = isolated
    with Session(engine, expire_on_commit=False) as db:
        seed = db.get(MemoryCaptureJob, job_id)
        session = db.get(ChatSession, db.get(Message, seed.source_message_id).session_id)
        model = db.get(ModelConfig, seed.model_config_id)
        loop = AgentLoop(db)
        request = ChatTurnRequest(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            session_id=session.id, model_config_id=model.id, message="同秒原轮输入")
        source, _ = loop._append_user_input_message(request, session.id)
        same_second = utc_now().replace(microsecond=0)
        source.created_at = same_second.replace(microsecond=900000) if precision == "mixed_cache" else same_second
        if precision == "mysql_roundtrip":
            source.created_at = same_second + timedelta(seconds=1)
        assistant = Message(id="aaa_same_second_assistant", tenant_id=tenant_id,
            session_id=session.id, role="assistant", content="同秒原轮完成",
            created_at=same_second, metadata_json={"user_message_id": source.id, "turn_id": source.id})
        db.add_all([source, assistant])
        db.commit()
        future_request = request.model_copy(update={"message": "同秒未来轮输入"})
        future, _ = loop._append_user_input_message(future_request, session.id)
        future.created_at = same_second
        future_assistant = Message(id="000_future_assistant", tenant_id=tenant_id,
            session_id=session.id, role="assistant", content="同秒未来完成",
            created_at=same_second, metadata_json={"user_message_id": future.id, "turn_id": future.id})
        db.add_all([future, future_assistant])
        db.commit()
        registration = register_memory_capture_job(db, request, session, model,
            user_message_id=source.id, assistant_message_id=assistant.id)
        assert registration is not None
        db.commit()
        ids = [row["id"] for row in registration.job.source_manifest_json["messages"]]
        assert source.id in ids
        assert ids[-2:] == [source.id, assistant.id]
        assert future.id not in ids and future_assistant.id not in ids
        assert worker._manifest_conversation(db, registration.job)[-2:] == [
            {"role": "user", "content": source.content},
            {"role": "assistant", "content": assistant.content},
        ]


def test_foreground_agent_completion_and_capture_share_lock_order(isolated):
    """真实前台完成先锁 Agent 时，后台 T3 不得反向锁 session 使正常回复死锁失败。"""
    from app.core.agent_loop import AgentLoop
    from app.db.models import ChatSession

    engine, (job_id, tenant_id, _, _) = isolated
    held = Event()
    with Session(engine, expire_on_commit=False) as db:
        job = worker.claim_memory_capture_job(db, worker_id="lock-order", job_id=job_id)
        fence = job.fencing_token
        session_id = db.get(Message, job.source_message_id).session_id

    def foreground():
        """调用实际前台最终写权限锁，再提交会话投影，全部在隔离数据库内。"""
        with Session(engine) as db:
            session = db.get(ChatSession, session_id)
            AgentLoop(db)._assert_agent_available_for_final_write(session, tenant_id)
            held.set()
            sleep(0.3)
            session.title = "并发前台回复已完成"
            db.add(session)
            db.commit()

    def capture():
        """后台按实际 T3 原子结算；foreground 尚持有 Agent 时不得反向锁会话。"""
        assert held.wait(3)
        with Session(engine) as db:
            return worker.settle_memory_capture_job(db, job_id=job_id,
                worker_id="lock-order", fencing_token=fence,
                delta=[{"kind": "fact", "key": "concurrent", "content": "正常并发事实"}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        front = pool.submit(foreground)
        back = pool.submit(capture)
        front.result(timeout=10)
        assert back.result(timeout=10)["status"] == "completed"
    with Session(engine) as db:
        assert db.get(ChatSession, session_id).title == "并发前台回复已完成"


def test_t1_manifest_reads_committed_source_beyond_old_snapshot(isolated):
    """T1 的旧读取快照不得漏掉另一连接已提交、并已精确锁定的真实原轮来源。"""
    from app.core.agent_loop import AgentLoop
    from app.db.models import ChatSession, ModelConfig
    from app.memory.capture import register_memory_capture_job
    from app.session.session_schema import ChatTurnRequest

    engine, (job_id, tenant_id, user_id, agent_id) = isolated
    with Session(engine, expire_on_commit=False) as stale:
        seed = stale.get(MemoryCaptureJob, job_id)
        session_id = stale.get(Message, seed.source_message_id).session_id
        model = stale.get(ModelConfig, seed.model_config_id)
        request = ChatTurnRequest(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            session_id=session_id, model_config_id=model.id, message="旧快照之后已提交的输入")
        with Session(engine) as writer:
            source, _ = AgentLoop(writer)._append_user_input_message(request, session_id)
            source_id = source.id
            writer.commit()
        source = stale.exec(select(Message).where(Message.id == source_id)
            .with_for_update().execution_options(populate_existing=True)).one()
        assistant = Message(id="current_read_t1_assistant", tenant_id=tenant_id,
            session_id=session_id, role="assistant", content="原事务完成结果",
            metadata_json={"turn_id": source.id, "user_message_id": source.id})
        stale.add(assistant)
        stale.flush()
        session = stale.get(ChatSession, session_id)
        registered = register_memory_capture_job(stale, request, session, model,
            user_message_id=source.id, assistant_message_id=assistant.id)
        assert registered is not None
        ids = [row["id"] for row in registered.job.source_manifest_json["messages"]]
        assert ids[-2:] == [source.id, assistant.id]
        stale.commit()


@pytest.mark.parametrize("operation", ["success", "failure", "renew", "dispatch", "discard"])
def test_cached_old_owner_rejects_every_write(isolated, operation):
    """第二连接接管后旧 identity map 不能结算、错误结算、续租或发送计数。"""
    engine, (job_id, _, _, _) = isolated
    with Session(engine, expire_on_commit=False) as stale:
        old = worker.claim_memory_capture_job(stale, worker_id="old", job_id=job_id)
        fence = old.fencing_token
        with Session(engine, expire_on_commit=False) as other:
            row = other.get(MemoryCaptureJob, job_id)
            row.lease_expires_at = utc_now() - timedelta(seconds=1)
            other.add(row)
            other.commit()
            new = worker.claim_memory_capture_job(other, worker_id="new", job_id=job_id)
            new_fence = new.fencing_token
        kwargs = dict(job_id=job_id, worker_id="old", fencing_token=fence)
        with pytest.raises(worker.MemoryCaptureFenceLost):
            if operation in {"success", "discard"}:
                worker.settle_memory_capture_job(
                    stale,
                    delta=[]
                    if operation == "discard"
                    else [{"kind": "fact", "key": "stale", "content": "迟到事实"}],
                    **kwargs,
                )
            elif operation == "failure":
                worker.fail_memory_capture_job(stale, error_code="OLD_FAILURE", **kwargs)
            elif operation == "renew":
                worker.renew_memory_capture_lease(stale, **kwargs)
            else:
                worker.increment_model_dispatch_count(stale, **kwargs)
        with Session(engine) as check:
            current = check.get(MemoryCaptureJob, job_id)
            assert (current.status, current.lease_owner, current.fencing_token) == (
                "running",
                "new",
                new_fence,
            )
            assert check.exec(select(MemoryRecord)).all() == []
            assert check.exec(select(AgentEvent)).all() == []


@pytest.mark.parametrize("legacy_agent", ["column", "metadata", "session"])
def test_legacy_delete_removes_only_same_effective_scope(isolated, legacy_agent):
    """序号零旧事实沿 SD2 有效归属删除，同键其他用户的内容必须保留。"""
    engine, (job_id, tenant_id, user_id, agent_id) = isolated
    with Session(engine, expire_on_commit=False) as db:
        job = db.get(MemoryCaptureJob, job_id)
        session_id = db.get(Message, job.source_message_id).session_id
        legacy = MemoryRecord(
            id="baseline",
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id if legacy_agent == "column" else None,
            session_id=session_id,
            kind="fact",
            content="旧事实",
            metadata_json={
                "source": "model_memory_extractor",
                "key": " Delivery Rule ",
                **({"agent_id": agent_id} if legacy_agent == "metadata" else {}),
            },
        )
        other = MemoryRecord(
            id="other",
            tenant_id=tenant_id,
            user_id="other",
            agent_id=agent_id,
            kind="fact",
            content="其他用户事实",
            metadata_json={"key": "delivery rule"},
        )
        db.add_all([legacy, other])
        db.commit()
        claimed = worker.claim_memory_capture_job(db, worker_id="delete", job_id=job_id)
        result = worker.settle_memory_capture_job(
            db,
            job_id=job_id,
            worker_id="delete",
            fencing_token=claimed.fencing_token,
            delta=[{"operation": "delete", "kind": "fact", "key": "delivery rule"}],
        )
        assert result["applied"] == 1
        assert MemoryService(db).context_memories(tenant_id, user_id, agent_id=agent_id) == []
        assert db.get(MemoryRecord, "other") is not None


@pytest.mark.parametrize("clear_first", [True, False])
def test_clear_and_t3_serialize_with_real_api(isolated, monkeypatch, clear_first):
    """实际两连接证明清空先提交拒绝旧 T3，T3 先取写权时清空等待后删除。"""
    engine, (job_id, tenant_id, user_id, agent_id) = isolated
    with Session(engine, expire_on_commit=False) as db:
        claimed = worker.claim_memory_capture_job(db, worker_id="late", job_id=job_id)
        fence = claimed.fencing_token
    entered, clear_started = Event(), Event()
    original = worker._current_contract_matches

    def checked(db, job):
        """在真实授权检查后启动清空线程，制造清空与首个正文写入交错。"""
        result = original(db, job)
        entered.set()
        assert clear_started.wait(2)
        return result

    def clear():
        """使用第二连接调用本人清空 API，不修改时钟或 clear 代数。"""
        if not clear_first:
            assert entered.wait(5)
        clear_started.set()
        with Session(engine) as db:
            return clear_my_memories(tenant_id, agent_id, db.get(User, user_id), db)

    if clear_first:
        clear()
    else:
        monkeypatch.setattr(worker, "_current_contract_matches", checked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(clear) if not clear_first else None
        with Session(engine) as db:
            result = worker.settle_memory_capture_job(
                db,
                job_id=job_id,
                worker_id="late",
                fencing_token=fence,
                delta=[{"kind": "fact", "key": "race", "content": "旧事实"}],
            )
        if future:
            future.result(timeout=8)
    with Session(engine) as check:
        assert check.exec(select(MemoryRecord)).all() == []
        saved = check.exec(select(AgentEvent).where(AgentEvent.event_type == "memory_saved")).all()
        assert len(saved) == (0 if clear_first else 1)
        assert result["status"] == ("discarded" if clear_first else "completed")


def test_new_source_after_clear_can_save_and_old_source_stays_discarded(isolated):
    """清空前旧来源失效，清空后真实输入沿新代数登记并正常保存。"""
    from app.core.agent_loop import AgentLoop
    from app.db.models import ModelConfig
    from app.memory.capture import register_memory_capture_job
    from app.session.session_schema import ChatTurnRequest

    engine, (job_id, tenant_id, user_id, agent_id) = isolated
    with Session(engine, expire_on_commit=False) as db:
        old = worker.claim_memory_capture_job(db, worker_id="old", job_id=job_id)
        fence = old.fencing_token
        session_id = db.get(Message, old.source_message_id).session_id
        model_id = old.model_config_id
        clear_my_memories(tenant_id, agent_id, db.get(User, user_id), db)
        result = worker.settle_memory_capture_job(
            db,
            job_id=job_id,
            worker_id="old",
            fencing_token=fence,
            delta=[{"kind": "fact", "key": "fresh", "content": "旧事实"}],
        )
        assert result["status"] == "discarded"
        request = ChatTurnRequest(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            model_config_id=model_id,
            message="清空后的新事实",
        )
        loop = AgentLoop(db)
        source, _ = loop._append_user_input_message(request, session_id)
        db.flush()
        assistant = Message(
            id="fresh_assistant",
            tenant_id=tenant_id,
            session_id=session_id,
            role="assistant",
            content="新的回复",
            metadata_json={"turn_id": source.id},
        )
        db.add(assistant)
        db.flush()
        session = loop._get_or_create_session(request)
        registered = register_memory_capture_job(
            db,
            request,
            session,
            db.get(ModelConfig, model_id),
            user_message_id=source.id,
            assistant_message_id=assistant.id,
        )
        assert registered is not None
        db.commit()
        fresh = worker.claim_memory_capture_job(db, worker_id="new", job_id=registered.job.id)
        result = worker.settle_memory_capture_job(
            db,
            job_id=fresh.id,
            worker_id="new",
            fencing_token=fresh.fencing_token,
            delta=[{"kind": "fact", "key": "fresh", "content": "新事实"}],
        )
        assert result["status"] == "completed"
        assert [
            row.content
            for row in MemoryService(db).context_memories(tenant_id, user_id, agent_id=agent_id)
        ] == ["新事实"]


def test_history_cleanup_refreshes_lifecycle_before_terminal_projection(isolated):
    """历史旧快照须等待前台生命周期提交，不能覆盖新步骤或重复追加技能完成事件。"""
    from app.api.chat import _cleanup_stale_completed_sessions, _lock_chat_session_lifecycle
    from app.db.models import ChatSession, Skill

    engine, (job_id, tenant_id, _, _) = isolated
    with Session(engine) as db:
        session_id = db.get(Message, db.get(MemoryCaptureJob, job_id).source_message_id).session_id
        session = db.get(ChatSession, session_id)
        session.active_skill_id = "cleanup_probe"
        session.active_step_id = "end"
        db.add(Skill(tenant_id=tenant_id, skill_id="cleanup_probe", name="隔离收尾流程",
            status="published", content_json={"terminal_node_ids": ["end"],
                "nodes": [{"node_id": "end", "allowed_actions": ["answer_user"]}]}))
        db.add(session)
        db.commit()
    with Session(engine, expire_on_commit=False) as stale:
        cached = stale.get(ChatSession, session_id)
        held = Event()

        def advance():
            """前台在现有生命周期锁内推进步骤，给历史旧读取制造真实提交竞争。"""
            with Session(engine) as db:
                current = db.get(ChatSession, session_id)
                assert _lock_chat_session_lifecycle(db, tenant_id, current)
                held.set()
                sleep(0.3)
                current.active_step_id = "continue"
                current.slots_json = {"fresh": "保留新步骤输入"}
                db.add(current)
                db.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(advance)
            assert held.wait(3)
            if engine.dialect.name == "sqlite":
                pending.result(timeout=5)
            _cleanup_stale_completed_sessions(stale, tenant_id, [cached])
            pending.result(timeout=5)
        stale.rollback()
    with Session(engine) as db:
        current = db.get(ChatSession, session_id)
        assert current.active_step_id == "continue"
        assert current.slots_json == {"fresh": "保留新步骤输入"}
        assert db.exec(select(AgentEvent).where(
            AgentEvent.event_type == "skill_completed")).all() == []
