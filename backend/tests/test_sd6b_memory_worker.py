"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_memory_worker.py
@CallChain  : pytest → durable memory worker → lease/fence → MemoryKeyState/MemoryRecord
@Description: 锁定 SD-6B 可靠领取、实际模型计数和原子结算的行为合同。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentEvent,
    AgentUsage,
    ChatSession,
    MemoryCaptureJob,
    MemoryCaptureScope,
    MemoryKeyState,
    MemoryRecord,
    Message,
    ModelConfig,
    User,
    utc_now,
)
from app.memory.capture import _message_hash, memory_extractor_prompt_hash, memory_model_contract_hash
from app.memory.worker import (
    MemoryCaptureFenceLost,
    claim_memory_capture_job,
    fail_memory_capture_job,
    increment_model_dispatch_count,
    _is_unknown_memory_placeholder,
    process_memory_capture_job,
    renew_memory_capture_lease,
    scan_memory_capture_jobs_once,
    settle_memory_capture_job,
)
from app.memory.jobs import enqueue_memory_capture


def _engine():
    """创建 worker 测试专用 SQLite，禁止访问应用默认数据库。"""

    return create_engine("sqlite://", poolclass=StaticPool)


def _seed_worker_job(
    db: Session,
    *,
    job_id: str = "memjob_worker",
    source_sequence: int = 1,
) -> MemoryCaptureJob:
    """建立带真实来源、scope、模型及当前可执行提取版本的合法领取夹具。"""

    user = User(
        id="user_sd6b_worker",
        tenant_id="tenant_sd6b_worker",
        username="sd6b-worker",
        password_hash="x",
    )
    agent = AgentProfile(
        id="agent_sd6b_worker",
        tenant_id=user.tenant_id,
        name="SD6B Worker Agent",
        owner_user_id=user.id,
        status="active",
    )
    session = ChatSession(
        id="session_sd6b_worker",
        tenant_id=user.tenant_id,
        user_id=user.id,
        agent_id=agent.id,
    )
    model = ModelConfig(
        id="model_sd6b_worker",
        tenant_id=user.tenant_id,
        name="SD6B Worker Model",
        api_key_encrypted="encrypted",
        model="worker-model",
    )
    user_scope = MemoryCaptureScope(
        id="memscope_sd6b_worker_user",
        tenant_id=user.tenant_id,
        user_id=user.id,
        scope_kind="user",
        agent_scope_key="__user__",
        source_sequence=source_sequence,
    )
    agent_scope = MemoryCaptureScope(
        id="memscope_sd6b_worker_agent",
        tenant_id=user.tenant_id,
        user_id=user.id,
        scope_kind="agent",
        agent_scope_key=agent.id,
    )
    source = Message(
        id="message_sd6b_worker",
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="user",
        content="我喜欢青柠茶。",
        metadata_json={
            "memory_capture_source": {
                "schema_version": "sd6b.v1",
                "user_scope_id": user_scope.id,
                "agent_scope_id": agent_scope.id,
                "agent_scope_key": agent.id,
                "source_sequence": source_sequence,
                "user_generation": 0,
                "agent_generation": 0,
                "user_permission_generation": 0,
                "agent_permission_generation": 0,
                "agent_capture_generation": 1,
            }
        },
    )
    assistant = Message(
        id="assistant_sd6b_worker",
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="assistant",
        content="我记住了。",
    )
    db.add_all([user, agent, session, model, user_scope, agent_scope, source, assistant])
    db.flush()
    job = MemoryCaptureJob(
        id=job_id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        agent_id=agent.id,
        agent_scope_key=agent.id,
        scope_id=agent_scope.id,
        source_message_id=source.id,
        source_assistant_message_id=assistant.id,
        source_sequence=source_sequence,
        user_generation=0,
        agent_generation=0,
        user_permission_generation=0,
        agent_permission_generation=0,
        agent_capture_generation=1,
        source_manifest_json={
            "schema_version": "sd6b.v1",
            "source_message_id": source.id,
            "source_message_hash": _message_hash(source),
            "source_assistant_message_id": assistant.id,
            "source_assistant_message_hash": _message_hash(assistant),
            "messages": [
                {"id": source.id, "role": source.role, "hash": _message_hash(source)},
                {"id": assistant.id, "role": assistant.role, "hash": _message_hash(assistant)},
            ],
        },
        source_hash=_message_hash(source),
        model_config_id=model.id,
        model_capture_generation=1,
        model_contract_hash=memory_model_contract_hash(model),
        prompt_hash=memory_extractor_prompt_hash(),
        status="pending",
    )
    db.add(job)
    db.commit()
    return job


def test_sd6b_claim_is_bounded_and_fenced_and_actual_dispatches_are_counted() -> None:
    """领取只持有短租约，旧 fence 不能结算，实际派发计数逐次落账。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        claimed = claim_memory_capture_job(db, worker_id="worker_one", job_id=job.id)
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.lease_owner == "worker_one"
        assert claimed.fencing_token == 1
        assert claimed.attempt_count == 1

        increment_model_dispatch_count(db, job_id=job.id, worker_id="worker_one", fencing_token=1)
        increment_model_dispatch_count(db, job_id=job.id, worker_id="worker_one", fencing_token=1)
        db.refresh(job)
        assert job.model_dispatch_count == 2
        with pytest.raises(MemoryCaptureFenceLost):
            increment_model_dispatch_count(
                db,
                job_id=job.id,
                worker_id="stale_worker",
                fencing_token=1,
            )


def test_sd6b_expired_claim_is_recovered_with_new_fence() -> None:
    """进程崩溃留下的过期 running 任务可由新 worker 重新领取并递增 fence。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        claimed = claim_memory_capture_job(db, worker_id="worker_one", job_id=job.id)
        assert claimed is not None
        claimed.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(claimed)
        db.commit()

        recovered = claim_memory_capture_job(db, worker_id="worker_two", job_id=job.id)
        assert recovered is not None
        assert recovered.lease_owner == "worker_two"
        assert recovered.fencing_token == 2
        assert recovered.attempt_count == 2


def test_sd6b_heartbeat_renews_active_lease_without_extending_deadline() -> None:
    """活动 worker 可短续租，旧 fence 或总截止时间不能被续租改变。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_heartbeat")
        claimed = claim_memory_capture_job(db, worker_id="worker_heartbeat", job_id=job.id)
        assert claimed is not None
        deadline = claimed.deadline_at
        previous_lease = claimed.lease_expires_at
        renew_memory_capture_lease(
            db,
            job_id=job.id,
            worker_id="worker_heartbeat",
            fencing_token=claimed.fencing_token,
        )
        db.refresh(claimed)
        assert claimed.lease_expires_at is not None
        assert previous_lease is not None and claimed.lease_expires_at >= previous_lease
        assert claimed.deadline_at == deadline
        with pytest.raises(MemoryCaptureFenceLost):
            renew_memory_capture_lease(
                db,
                job_id=job.id,
                worker_id="old_worker",
                fencing_token=claimed.fencing_token,
            )


def test_sd6b_atomic_settlement_rejects_stale_update_and_preserves_tombstone() -> None:
    """新来源先结算后，旧更新不得复活记忆；删除以 key tombstone 形成屏障。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        claimed = claim_memory_capture_job(db, worker_id="worker_one", job_id=job.id)
        assert claimed is not None
        settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_one",
            fencing_token=claimed.fencing_token,
            delta=[
                {
                    "operation": "upsert",
                    "kind": "fact",
                    "key": "drink",
                    "content": "喜欢青柠茶",
                    "importance": 0.8,
                }
            ],
        )
        record = db.exec(select(MemoryRecord)).one()
        assert record.content == "喜欢青柠茶"

        old_job = db.get(MemoryCaptureJob, job.id)
        assert old_job is not None
        old_job.source_sequence = 0
        old_job.status = "running"
        old_job.lease_owner = "worker_old"
        old_job.fencing_token = claimed.fencing_token + 1
        old_job.lease_expires_at = utc_now() + timedelta(seconds=30)
        db.add(old_job)
        db.commit()
        settle_memory_capture_job(
            db,
            job_id=old_job.id,
            worker_id="worker_old",
            fencing_token=old_job.fencing_token,
            delta=[
                {
                    "operation": "delete",
                    "kind": "fact",
                    "key": "drink",
                }
            ],
        )
        db.refresh(record)
        assert record.content == "喜欢青柠茶"
        state = db.exec(select(MemoryKeyState).where(MemoryKeyState.normalized_key == "drink")).one()
        assert state.tombstoned is False


@pytest.mark.parametrize("unknown_content", ["未知", "口令：未知"])
def test_sd6b_unknown_answer_does_not_overwrite_unmentioned_fact(unknown_content: str) -> None:
    """纯未知占位及字段标签未知都只屏障旧序号，不覆盖已有事实。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_unknown_answer")
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
            id="mem_sd6b_existing_code",
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
            id="mem_sd6b_existing_style",
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

        claimed = claim_memory_capture_job(db, worker_id="worker_unknown_answer", job_id=job.id)
        assert claimed is not None
        result = settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_unknown_answer",
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

        db.refresh(code)
        db.refresh(style)
        assert code.content == "已有口令为R-17"
        assert code.source_sequence == 0
        assert style.content == "偏好详细表格"
        assert result == {"status": "completed", "applied": 1, "discarded": 1}
        code_state = db.exec(
            select(MemoryKeyState).where(MemoryKeyState.normalized_key == "reference_code")
        ).one()
        assert code_state.record_id == code.id
        assert code_state.last_source_sequence == job.source_sequence

        job.status = "running"
        job.lease_owner = "worker_older_source"
        job.fencing_token += 1
        job.source_sequence = 0
        job.lease_expires_at = utc_now() + timedelta(seconds=30)
        db.add(job)
        db.commit()
        older_result = settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_older_source",
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
        assert older_result == {"status": "completed", "applied": 0, "discarded": 1}
        assert code.content == "已有口令为R-17"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("未知", True),
        ("口令：未知", True),
        ("reference_code = 未知。", True),
        ("口令：未知，但需要进一步核实", False),
    ],
)
def test_sd6b_unknown_placeholder_only_discards_a_labeled_scalar(
    content: str, expected: bool,
) -> None:
    """字段标签后只有未知值才视作占位，带有其他内容的完整句子仍保留。"""
    assert _is_unknown_memory_placeholder(content) is expected


def test_sd6b_process_worker_counts_real_model_dispatch_and_settles_delta_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 模型外呼前计数，返回后才一次性写 MemoryRecord 与 job 终态。"""

    calls: list[int] = []

    class FakeMemoryClient:
        """提供一个可观测的无网络 JSON 提取器。"""

        def __init__(self, _model_config, *, dispatch_observer=None, timeout_seconds=None) -> None:
            """记录发送前观察器并验证 worker 的六十秒上限。"""
            assert timeout_seconds <= 60
            self.dispatch_observer = dispatch_observer

        def generate_json(self, _prompt: str, _payload: dict[str, object]) -> dict[str, object]:
            """模拟一次真实模型派发并返回一个结构化事实。"""

            assert self.dispatch_observer is not None
            self.dispatch_observer()
            calls.append(1)
            return {
                "memories": [
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "drink",
                        "content": "喜欢青柠茶",
                        "importance": 0.9,
                    }
                ]
            }

    monkeypatch.setattr("app.memory.service.LLMClient", FakeMemoryClient)
    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        result = process_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_process",
        )

        assert result == {"status": "completed", "applied": 1, "discarded": 0}
        db.refresh(job)
        assert calls == [1]
        assert job.model_dispatch_count == 1
        assert job.status == "completed"
        assert db.exec(select(MemoryRecord)).one().capture_job_id == job.id
        assert [event.event_type for event in db.exec(select(AgentEvent)).all()] == ["memory_saved"]


def test_sd6b_generation_revoke_discards_late_result_and_delete_creates_tombstone() -> None:
    """清空/撤权代数变化丢弃迟到结果；合法更新中的删除保留 key tombstone。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        claimed = claim_memory_capture_job(db, worker_id="worker_generation", job_id=job.id)
        assert claimed is not None
        scope = db.exec(
            select(MemoryCaptureScope).where(
                MemoryCaptureScope.scope_kind == "user",
                MemoryCaptureScope.agent_scope_key == "__user__",
            )
        ).one()
        scope.clear_generation += 1
        db.add(scope)
        db.commit()

        discarded = settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_generation",
            fencing_token=claimed.fencing_token,
            delta=[
                {
                    "operation": "upsert",
                    "kind": "fact",
                    "key": "drink",
                    "content": "不应落库",
                }
            ],
        )
        assert discarded["status"] == "discarded"
        assert db.exec(select(MemoryRecord)).all() == []

        job.status = "running"
        job.lease_owner = "worker_generation"
        job.fencing_token += 1
        job.source_sequence = 2
        job.user_generation = scope.clear_generation
        job.lease_expires_at = utc_now() + timedelta(seconds=30)
        scope.source_sequence = 2
        db.add_all([job, scope])
        db.commit()
        # 以新代数重新建立事实，随后同一 fence 的删除必须留下 tombstone。
        settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_generation",
            fencing_token=job.fencing_token,
            delta=[
                {
                    "operation": "upsert",
                    "kind": "fact",
                    "key": "drink",
                    "content": "合法事实",
                }
            ],
        )
        db.refresh(job)
        job.status = "running"
        job.lease_owner = "worker_generation"
        job.fencing_token += 1
        job.source_sequence = 3
        job.lease_expires_at = utc_now() + timedelta(seconds=30)
        scope.source_sequence = 3
        db.add_all([job, scope])
        db.commit()
        settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_generation",
            fencing_token=job.fencing_token,
            delta=[{"operation": "delete", "kind": "fact", "key": "drink"}],
        )
        assert db.exec(select(MemoryRecord)).all() == []
        assert db.exec(select(MemoryKeyState)).one().tombstoned is True


def test_sd6b_retry_wait_and_deadline_are_claim_boundaries() -> None:
    """重试等待和总截止时间都必须阻断扫描，不能因重复扫描提前外呼。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db)
        claimed = claim_memory_capture_job(db, worker_id="worker_retry", job_id=job.id)
        assert claimed is not None
        status = fail_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_retry",
            fencing_token=claimed.fencing_token,
            error_code="TEMPORARY_PROVIDER",
        )
        assert status == "retry"
        assert claim_memory_capture_job(db, worker_id="worker_retry_again", job_id=job.id) is None

        row = db.get(MemoryCaptureJob, job.id)
        assert row is not None
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
        row.deadline_at = utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        assert claim_memory_capture_job(db, worker_id="worker_after_deadline", job_id=job.id) is None
        db.refresh(row)
        assert row.status == "failed"


def test_sd6b_disabled_model_and_inactive_member_cannot_settle_late_result() -> None:
    """模型禁用或主体暂停后，迟到 delta 必须被丢弃且不产生正文。"""

    for mutation in ("model", "user"):
        engine = _engine()
        SQLModel.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as db:
            job = _seed_worker_job(db, job_id=f"memjob_{mutation}")
            claimed = claim_memory_capture_job(db, worker_id=f"worker_{mutation}", job_id=job.id)
            assert claimed is not None
            if mutation == "model":
                model = db.get(ModelConfig, job.model_config_id)
                assert model is not None
                model.enabled = False
                model.memory_capture_generation += 1
                db.add(model)
            else:
                user = db.get(User, job.user_id)
                assert user is not None
                user.membership_status = "suspended"
                db.add(user)
            db.commit()

            result = settle_memory_capture_job(
                db,
                job_id=job.id,
                worker_id=f"worker_{mutation}",
                fencing_token=claimed.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "late",
                        "content": "不应保存",
                    }
                ],
            )
            assert result["status"] == "discarded"
            assert db.exec(select(MemoryRecord)).all() == []


def test_sd6b_agent_usage_revocation_blocks_non_owner_settlement() -> None:
    """共享 Agent 的使用关系撤销后，worker 不能绕过现有聊天授权谓词。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_usage")
        agent = db.get(AgentProfile, job.agent_id)
        user = db.get(User, job.user_id)
        assert agent is not None and user is not None
        agent.owner_user_id = "another_owner"
        db.add(agent)
        usage = AgentUsage(
            tenant_id=job.tenant_id,
            user_id=user.id,
            agent_id=agent.id,
        )
        db.add(usage)
        db.commit()
        claimed = claim_memory_capture_job(db, worker_id="worker_usage", job_id=job.id)
        assert claimed is not None
        db.delete(usage)
        db.commit()

        result = settle_memory_capture_job(
            db,
            job_id=job.id,
            worker_id="worker_usage",
            fencing_token=claimed.fencing_token,
            delta=[
                {
                    "operation": "upsert",
                    "kind": "fact",
                    "key": "late",
                    "content": "不应保存",
                }
            ],
        )
        assert result["status"] == "discarded"
        assert db.exec(select(MemoryRecord)).all() == []


def test_sd6b_invalid_delta_is_failed_without_partial_key_state() -> None:
    """同批合法项和非法项不能部分落库，任务应停在安全失败终态。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_invalid_delta")
        claimed = claim_memory_capture_job(db, worker_id="worker_invalid", job_id=job.id)
        assert claimed is not None
        with pytest.raises(ValueError):
            settle_memory_capture_job(
                db,
                job_id=job.id,
                worker_id="worker_invalid",
                fencing_token=claimed.fencing_token,
                delta=[
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "valid",
                        "content": "不能部分保存",
                    },
                    {"operation": "upsert", "kind": "unknown", "key": "bad", "content": "坏"},
                ],
            )
        row = db.get(MemoryCaptureJob, job.id)
        assert row is not None and row.status == "failed"
        assert db.exec(select(MemoryRecord)).all() == []
        assert db.exec(select(MemoryKeyState)).all() == []


def test_sd6b_dispatch_count_is_hard_bounded_and_old_queue_write_is_closed() -> None:
    """实际 provider 预留次数有硬上限，旧内存队列入口不能重新写旁路。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_dispatch_limit")
        claimed = claim_memory_capture_job(db, worker_id="worker_limit", job_id=job.id)
        assert claimed is not None
        for _ in range(2):
            increment_model_dispatch_count(
                db,
                job_id=job.id,
                worker_id="worker_limit",
                fencing_token=claimed.fencing_token,
            )
        with pytest.raises(MemoryCaptureFenceLost):
            increment_model_dispatch_count(
                db,
                job_id=job.id,
                worker_id="worker_limit",
                fencing_token=claimed.fencing_token,
            )
    with pytest.raises(RuntimeError):
        enqueue_memory_capture({"job_id": "legacy"})


def test_sd6b_new_process_scan_recovers_expired_job_without_memory_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知丢失或进程重启后，持久扫描仍能按新 owner 恢复并只处理一次。"""

    class FakeMemoryClient:
        """提供受控模型结果，验证恢复扫描确实经过持久 worker。"""

        def __init__(self, _model_config, *, dispatch_observer=None, timeout_seconds=None) -> None:
            """记录发送前观察器并验证 worker 的六十秒上限。"""
            assert timeout_seconds <= 60
            self.dispatch_observer = dispatch_observer

        def generate_json(self, _prompt: str, _payload: dict[str, object]) -> dict[str, object]:
            """模拟恢复进程的一次真实提取请求。"""

            assert self.dispatch_observer is not None
            self.dispatch_observer()
            return {
                "memories": [
                    {
                        "operation": "upsert",
                        "kind": "fact",
                        "key": "restart",
                        "content": "恢复后只写一次",
                    }
                ]
            }

    monkeypatch.setattr("app.memory.service.LLMClient", FakeMemoryClient)
    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        job = _seed_worker_job(db, job_id="memjob_restart")
        claimed = claim_memory_capture_job(db, worker_id="dead_process", job_id=job.id)
        assert claimed is not None
        claimed.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(claimed)
        db.commit()

    assert (
        scan_memory_capture_jobs_once(
            worker_id="new_process",
            limit=1,
            database_engine=engine,
        )
        == 1
    )
    with Session(engine) as db:
        restored = db.get(MemoryCaptureJob, "memjob_restart")
        assert restored is not None and restored.status == "completed"
        assert restored.attempt_count == 2
        assert len(db.exec(select(MemoryRecord)).all()) == 1
