"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_memory_worker_mysql.py
@CallChain  : pytest mysql → Alembic head → MemoryCaptureJob → fence/原子结算
@Description: 在随机隔离 MySQL 验证 SD-6B worker 的领取恢复、来源 CAS 和事件一致性。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentEvent,
    ChatSession,
    MemoryCaptureJob,
    MemoryCaptureScope,
    MemoryKeyState,
    MemoryRecord,
    Message,
    ModelConfig,
    Tenant,
    User,
    utc_now,
)
from app.api.memories import clear_my_memories
from app.memory.capture import _message_hash, memory_extractor_prompt_hash, memory_model_contract_hash
from app.memory.worker import (
    MemoryCaptureFenceLost,
    claim_memory_capture_job,
    settle_memory_capture_job,
)


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把临时 MySQL 数据库升级到当前 Alembic head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def _seed_job(db: Session) -> MemoryCaptureJob:
    """建立符合外键、manifest、scope及可执行提取版本的MySQL合法任务。"""

    tenant = Tenant(id="tenant_sd6b_mysql_worker", name="SD6B MySQL worker")
    user = User(
        id="user_sd6b_mysql_worker",
        tenant_id=tenant.id,
        username="sd6b-mysql-worker",
        password_hash="hash",
    )
    agent = AgentProfile(
        id="agent_sd6b_mysql_worker",
        tenant_id=tenant.id,
        name="SD6B MySQL worker agent",
        owner_user_id=user.id,
        status="active",
    )
    session = ChatSession(
        id="session_sd6b_mysql_worker",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id=agent.id,
    )
    model = ModelConfig(
        id="model_sd6b_mysql_worker",
        tenant_id=tenant.id,
        name="SD6B MySQL worker model",
        api_key_encrypted="encrypted",
        model="mysql-worker-model",
    )
    user_scope = MemoryCaptureScope(
        id="scope_sd6b_mysql_worker_user",
        tenant_id=tenant.id,
        user_id=user.id,
        scope_kind="user",
        agent_scope_key="__user__",
        source_sequence=1,
    )
    agent_scope = MemoryCaptureScope(
        id="scope_sd6b_mysql_worker_agent",
        tenant_id=tenant.id,
        user_id=user.id,
        scope_kind="agent",
        agent_scope_key=agent.id,
    )
    source = Message(
        id="message_sd6b_mysql_worker",
        tenant_id=tenant.id,
        session_id=session.id,
        role="user",
        content="我偏好 MySQL worker 测试。",
    )
    assistant = Message(
        id="assistant_sd6b_mysql_worker",
        tenant_id=tenant.id,
        session_id=session.id,
        role="assistant",
        content="已记录。",
    )
    db.add_all([tenant, user, agent, session, model, user_scope, agent_scope, source, assistant])
    db.flush()
    job = MemoryCaptureJob(
        id="memjob_sd6b_mysql_worker",
        tenant_id=tenant.id,
        user_id=user.id,
        agent_id=agent.id,
        agent_scope_key=agent.id,
        scope_id=agent_scope.id,
        source_message_id=source.id,
        source_assistant_message_id=assistant.id,
        source_sequence=1,
        source_manifest_json={
            "schema_version": "sd6b.v1",
            "source_message_hash": _message_hash(source),
            "source_assistant_message_hash": _message_hash(assistant),
            "messages": [
                {"id": source.id, "role": "user", "hash": _message_hash(source)},
                {"id": assistant.id, "role": "assistant", "hash": _message_hash(assistant)},
            ],
        },
        source_hash="a" * 64,
        model_config_id=model.id,
        model_capture_generation=1,
        model_contract_hash=memory_model_contract_hash(model),
        prompt_hash=memory_extractor_prompt_hash(),
        status="pending",
    )
    db.add(job)
    db.commit()
    return job


def test_mysql_sd6b_claim_recovery_and_atomic_result_event(mysql_database_url: str) -> None:
    """MySQL 新连接可接管过期任务，旧 fence 不能覆盖已提交正文和事件。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine, expire_on_commit=False) as db:
            job = _seed_job(db)
            first = claim_memory_capture_job(db, worker_id="mysql_worker_one", job_id=job.id)
            assert first is not None
            first.lease_expires_at = utc_now() - timedelta(seconds=1)
            db.add(first)
            db.commit()

        with Session(engine, expire_on_commit=False) as db:
            recovered = claim_memory_capture_job(
                db,
                worker_id="mysql_worker_two",
                job_id="memjob_sd6b_mysql_worker",
            )
            assert recovered is not None
            result = settle_memory_capture_job(
                db,
                job_id=recovered.id,
                worker_id="mysql_worker_two",
                fencing_token=recovered.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "storage",
                        "content": "MySQL 原子结算",
                    }
                ],
            )
            assert result == {"status": "completed", "applied": 1, "discarded": 0}
            assert db.exec(select(MemoryRecord)).one().content == "MySQL 原子结算"
            assert [event.event_type for event in db.exec(select(AgentEvent)).all()] == [
                "memory_saved"
            ]
            assert db.exec(select(MemoryKeyState)).one().tombstoned is False

            with pytest.raises(MemoryCaptureFenceLost):
                settle_memory_capture_job(
                    db,
                    job_id=recovered.id,
                    worker_id="mysql_worker_one",
                    fencing_token=first.fencing_token,
                    delta=[
                        {
                            "operation": "upsert",
                            "kind": "fact",
                            "key": "storage",
                            "content": "旧 worker 不得覆盖",
                        }
                    ],
                )
            cleared = clear_my_memories(
                "tenant_sd6b_mysql_worker",
                "agent_sd6b_mysql_worker",
                db.get(User, "user_sd6b_mysql_worker"),
                db,
            )
            assert cleared == {"deleted": 1}
            assert db.exec(select(MemoryRecord)).all() == []
            cleared_state = db.exec(select(MemoryKeyState)).one()
            assert cleared_state.tombstoned is True
    finally:
        engine.dispose()


def test_mysql_sd6b_revoked_agent_discards_late_result(mysql_database_url: str) -> None:
    """MySQL 撤销 Agent 后，旧 worker 的结果不能写正文或冒充成功。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine, expire_on_commit=False) as db:
            job = _seed_job(db)
            claimed = claim_memory_capture_job(db, worker_id="mysql_revoke_worker", job_id=job.id)
            assert claimed is not None
            agent = db.get(AgentProfile, job.agent_id)
            assert agent is not None
            agent.status = "inactive"
            agent.memory_capture_generation += 1
            db.add(agent)
            db.commit()
            result = settle_memory_capture_job(
                db,
                job_id=job.id,
                worker_id="mysql_revoke_worker",
                fencing_token=claimed.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "revoked",
                        "content": "不应写入",
                    }
                ],
            )
            assert result["status"] == "discarded"
            assert db.exec(select(MemoryRecord)).all() == []
            assert db.exec(select(MemoryCaptureJob)).one().status == "discarded"
    finally:
        engine.dispose()


@pytest.mark.parametrize("unknown_content", ["未知", "口令：未知"])
def test_mysql_sd6b_unknown_placeholder_preserves_fact_and_applies_correction(
    mysql_database_url: str, unknown_content: str,
) -> None:
    """MySQL 纯未知及字段标签未知均保持旧正文、接收更正并屏障旧来源。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine, expire_on_commit=False) as db:
            job = _seed_job(db)
            source = db.get(Message, job.source_message_id)
            assistant = db.get(Message, job.source_assistant_message_id)
            assert source is not None and assistant is not None
            source.content = "请将答复格式更正为详细表格。另一个字段未选择，请如实说未知。"
            assistant.content = "答复格式已更正；未选择字段的答案未知。"
            job.source_hash = _message_hash(source)
            manifest = dict(job.source_manifest_json)
            manifest["source_message_hash"] = _message_hash(source)
            manifest["source_assistant_message_hash"] = _message_hash(assistant)
            manifest["messages"] = [
                {
                    **row,
                    "hash": _message_hash(source if row["id"] == source.id else assistant),
                }
                for row in manifest["messages"]
            ]
            job.source_manifest_json = manifest
            code = MemoryRecord(
                id="mem_sd6b_mysql_existing_code",
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                agent_scope_key=job.agent_scope_key,
                user_id=job.user_id,
                session_id=source.session_id,
                kind="fact",
                content="已有口令为R-17",
                metadata_json={"key": "reference_code"},
                source_sequence=0,
            )
            style = MemoryRecord(
                id="mem_sd6b_mysql_existing_style",
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                agent_scope_key=job.agent_scope_key,
                user_id=job.user_id,
                session_id=source.session_id,
                kind="preference",
                content="偏好简短要点",
                metadata_json={"key": "communication_style"},
                source_sequence=0,
            )
            db.add_all([code, style, job, source, assistant])
            db.commit()

            claimed = claim_memory_capture_job(db, worker_id="mysql_unknown_answer", job_id=job.id)
            assert claimed is not None
            result = settle_memory_capture_job(
                db,
                job_id=job.id,
                worker_id="mysql_unknown_answer",
                fencing_token=claimed.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "reference_code",
                        "content": unknown_content,
                        "importance": 0.6,
                    },
                    {
                        "operation": "upsert",
                        "kind": "preference",
                        "key": "communication_style",
                        "content": "偏好详细表格",
                        "importance": 0.8,
                    },
                ],
            )
            assert result == {"status": "completed", "applied": 1, "discarded": 1}
            db.refresh(code)
            db.refresh(style)
            assert code.content == "已有口令为R-17"
            assert code.source_sequence == 0
            assert style.content == "偏好详细表格"
            state = db.exec(
                select(MemoryKeyState).where(MemoryKeyState.normalized_key == "reference_code")
            ).one()
            assert state.record_id == code.id
            assert state.last_source_sequence == job.source_sequence

            job.status = "running"
            job.lease_owner = "mysql_older_source"
            job.fencing_token += 1
            job.source_sequence = 0
            job.lease_expires_at = utc_now() + timedelta(seconds=30)
            db.add(job)
            db.commit()
            older = settle_memory_capture_job(
                db,
                job_id=job.id,
                worker_id="mysql_older_source",
                fencing_token=job.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "reference_code",
                        "content": "较旧来源的口令",
                        "importance": 0.6,
                    }
                ],
            )
            db.refresh(code)
            assert older == {"status": "completed", "applied": 0, "discarded": 1}
            assert code.content == "已有口令为R-17"
    finally:
        engine.dispose()
