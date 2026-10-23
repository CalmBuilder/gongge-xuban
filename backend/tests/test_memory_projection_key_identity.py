"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_memory_projection_key_identity.py
@CallChain  : SQLite/随机MySQL → 原worker claim/fence/settle → 冻结restore/最终SDK
@Description: 锁定历史规范键删除屏障、合法更新冻结及新来源不复活旧选择。
"""

import copy

import pytest
from sqlmodel import Session, select

from app.db.models import MemoryCaptureJob, MemoryCaptureScope, MemoryKeyState, MemoryRecord, Message, Tenant
from app.memory.capture import _message_hash
from app.memory.request_projection import MemoryRequestEnvelope, MemoryRequestPayload
from app.memory.service import MemoryService
from app.memory.worker import claim_memory_capture_job, settle_memory_capture_job
from tests.test_memory_projection_consumers import SDK, llm
from tests.test_memory_projection_disclosure import disclosure_engine as _disclosure_engine
from tests.test_sd6b_memory_worker import _seed_worker_job

key_identity_engine = _disclosure_engine

MARKER = "SYNTHETIC_LEGACY_FACT_A98B"


def next_job(db, original):
    """在同一原scope登记独立真实新来源，沿原worker结算产生新事实。"""
    source = db.get(Message, original.source_message_id)
    assistant = db.get(Message, original.source_assistant_message_id)
    next_source = Message.model_validate({**source.model_dump(), "id": "key-next-source", "content": "新事实"})
    metadata = copy.deepcopy(next_source.metadata_json)
    metadata["memory_capture_source"]["source_sequence"] = 2
    next_source.metadata_json = metadata
    next_assistant = Message.model_validate({**assistant.model_dump(), "id": "key-next-assistant"})
    manifest = {"schema_version": "sd6b.v1", "source_message_id": next_source.id,
        "source_message_hash": _message_hash(next_source),
        "source_assistant_message_id": next_assistant.id,
        "source_assistant_message_hash": _message_hash(next_assistant),
        "messages": [{"id": m.id, "role": m.role, "hash": _message_hash(m)}
                     for m in [next_source, next_assistant]]}
    user_scope = db.exec(select(MemoryCaptureScope).where(
        MemoryCaptureScope.tenant_id == original.tenant_id,
        MemoryCaptureScope.user_id == original.user_id,
        MemoryCaptureScope.scope_kind == "user")).one()
    user_scope.source_sequence = 2
    job = MemoryCaptureJob.model_validate({**original.model_dump(), "id": "key-next-job",
        "source_message_id": next_source.id, "source_assistant_message_id": next_assistant.id,
        "source_sequence": 2, "source_hash": _message_hash(next_source),
        "source_manifest_json": manifest, "status": "pending", "lease_owner": None,
        "lease_expires_at": None, "fencing_token": 0, "attempt_count": 0,
        "finished_at": None, "applied_count": 0})
    db.add_all([next_source, next_assistant, user_scope, job])
    db.commit()
    return job


def settle(db, job, operation, key, content=""):
    """真实领取并携fence结算，禁止用直接删行伪造原反例。"""
    claimed = claim_memory_capture_job(db, worker_id="key-disclosure", job_id=job.id)
    assert claimed is not None
    result = settle_memory_capture_job(db, job_id=job.id, worker_id="key-disclosure",
        fencing_token=claimed.fencing_token,
        delta=[{"operation": operation, "kind": "profile", "key": key, "content": content}])
    assert result["applied"] == 1


@pytest.mark.parametrize("raw_key,canonical", [
    ("preferred_name", "preferred_name"),
    (" Preferred_Name ", "preferred_name"),
    (" DeLiVeRy \t  Rule ", "delivery rule"),
])
def test_worker_delete_and_new_source_never_revive_old_raw_snapshot(
    key_identity_engine, raw_key, canonical,
):
    """原行删掉和规范墓碑成立后，旧raw快照永久拒绝，新来源只供新任务消费。"""
    with Session(key_identity_engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_sd6b_worker", name="隔离"))
        db.commit()
        job = _seed_worker_job(db)
        row = MemoryRecord(id="legacy-key-record", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="profile", content=MARKER, metadata_json={"key": raw_key})
        other = MemoryRecord(id="other-kind", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="preference", content="保留其他kind", metadata_json={"key": raw_key})
        db.add_all([row, other])
        db.commit()
        service = MemoryService(db)
        selected = service.projection_candidates(job.tenant_id, job.user_id, agent_id=job.agent_id)
        candidate = next(c for c in selected if c.id == row.id)
        validate = service.projection_validator(job.tenant_id, job.user_id, agent_id=job.agent_id)
        envelope = MemoryRequestEnvelope((candidate,), "任务", validate=validate)
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", MemoryRequestPayload({"goal": "任务", "memory_context": []}, envelope))
        assert MARKER in str(sdk.calls)
        snapshot = envelope.snapshot()
        snapshot["selected"][0]["key"] = raw_key  # 模拟已存旧raw键，不改写历史审计。
        settle(db, job, "delete", canonical)
        assert db.get(MemoryRecord, row.id) is None
        assert db.get(MemoryRecord, other.id) is not None
        state = db.exec(select(MemoryKeyState).where(MemoryKeyState.normalized_key == canonical)).one()
        assert state.tombstoned
        restored = MemoryRequestEnvelope.restore(snapshot, validate=validate)
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", MemoryRequestPayload({"goal": "任务", "memory_context": []}, restored))
        assert not validate(candidate) and restored.selected == ()
        assert MARKER not in str(sdk.calls)
        new_job = next_job(db, job)
        settle(db, new_job, "upsert", canonical, "合法新来源")
        current = service.projection_candidates(job.tenant_id, job.user_id, agent_id=job.agent_id)
        fresh = next(c for c in current if c.kind == "profile")
        assert fresh.source_sequence == 2 and fresh.id != candidate.id and validate(fresh)
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", MemoryRequestPayload({"goal": "任务", "memory_context": []},
            MemoryRequestEnvelope((fresh,), "任务", validate=validate)))
        assert "合法新来源" in str(sdk.calls)
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", MemoryRequestPayload({"goal": "任务", "memory_context": []},
            MemoryRequestEnvelope.restore(snapshot, validate=validate)))
        assert not validate(candidate) and MARKER not in str(sdk.calls)
        assert validate(next(c for c in current if c.id == other.id))


def test_canonical_dedup_and_same_record_update_keep_original_frozen_content(key_identity_engine):
    """同scope规范键按真实来源序归并；合法原行更新不替换旧计划选择正文。"""
    with Session(key_identity_engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_sd6b_worker", name="隔离"))
        db.commit()
        job = _seed_worker_job(db)
        row = MemoryRecord(id="frozen-legacy", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="profile", content=MARKER, metadata_json={"key": " Preferred_Name "})
        db.add(row)
        db.commit()
        service = MemoryService(db)
        validate = service.projection_validator(job.tenant_id, job.user_id, agent_id=job.agent_id)
        candidates = service.projection_candidates(job.tenant_id, job.user_id, agent_id=job.agent_id)
        envelope = MemoryRequestEnvelope(candidates, "任务", validate=validate)
        llm(SDK(['{"ok":true}'])).generate_json("系统", MemoryRequestPayload(
            {"goal": "任务", "memory_context": []}, envelope))
        snapshot = envelope.snapshot()
        settle(db, job, "upsert", "preferred_name", "当前更新")
        assert db.get(MemoryRecord, row.id).content == "当前更新" and validate(candidates[0])
        sdk = SDK(['{"ok":true}'])
        llm(sdk).generate_json("系统", MemoryRequestPayload({"goal": "任务", "memory_context": []},
            MemoryRequestEnvelope.restore(snapshot, validate=validate)))
        assert MARKER in str(sdk.calls) and "当前更新" not in str(sdk.calls)
        db.add(MemoryRecord(id="same-canonical-later", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="fact", content="后来源", source_sequence=2,
            metadata_json={"key": "  ShArEd \n  Key "}))
        db.add(MemoryRecord(id="same-canonical-old", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="fact", content="旧来源", source_sequence=1,
            metadata_json={"key": "shared key"}))
        db.add(MemoryRecord(id="no-key-legacy", tenant_id=job.tenant_id, user_id=job.user_id,
            agent_id=job.agent_id, kind="fact", content="缺key旧基线"))
        db.commit()
        current = service.projection_candidates(job.tenant_id, job.user_id, agent_id=job.agent_id)
        assert "same-canonical-old" not in {c.id for c in current}
        assert {"same-canonical-later", "no-key-legacy"} <= {c.id for c in current}


def test_canonical_tombstone_remains_scoped_to_tenant_user_agent_and_kind(key_identity_engine):
    """规范同键墓碑只约束其tenant/user/agent/kind，跨范围事实保持完整合法。"""
    from app.db.models import AgentProfile, User

    scopes = [("scope-t", "scope-u", "scope-a", "profile"),
              ("scope-other-t", "scope-other-u", "scope-other-a", "profile"),
              ("scope-t", "scope-other-user", "scope-user-a", "profile"),
              ("scope-t", "scope-u", "scope-other-agent", "profile"),
              ("scope-t", "scope-u", "scope-a", "preference")]
    with Session(key_identity_engine) as db:
        for tenant in sorted({s[0] for s in scopes}):
            db.add(Tenant(id=tenant, name="隔离"))
        db.commit()
        for tenant, user in sorted({(s[0], s[1]) for s in scopes}):
            db.add(User(id=user, tenant_id=tenant, username=user, password_hash="synthetic"))
        db.commit()
        for tenant, user, agent in sorted({s[:3] for s in scopes}):
            db.add(AgentProfile(id=agent, tenant_id=tenant, name=agent, owner_user_id=user))
        db.commit()
        for index, (tenant, user, agent, kind) in enumerate(scopes):
            db.add(MemoryRecord(id=f"scope-memory-{index}", tenant_id=tenant, user_id=user,
                agent_id=agent, kind=kind, content=f"范围{index}完整事实",
                metadata_json={"key": " Preferred_Name "}))
        db.commit()
        service = MemoryService(db)
        candidates = [next(c for c in service.projection_candidates(t, u, agent_id=a)
                           if c.kind == kind) for t, u, a, kind in scopes]
        db.add(MemoryKeyState(tenant_id="scope-t", user_id="scope-u",
            agent_scope_key="scope-a", kind="profile", normalized_key="preferred_name",
            tombstoned=True, last_source_sequence=1))
        db.commit()
        for index, ((tenant, user, agent, _), candidate) in enumerate(zip(scopes, candidates)):
            validate = service.projection_validator(tenant, user, agent_id=agent)
            assert validate(candidate) is (index != 0)
