"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_dispatch_regressions.py
@CallChain  : 隔离 pytest → 真实 memory worker/LLMClient → 本地 provider spy
@Description: 独立核验记忆提取实际发送上限、外呼事务与撤权前置边界。
"""

import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlmodel import Session, select
from app.db.models import MemoryCaptureJob, MemoryRecord, User, utc_now
from app.memory.worker import (
    process_memory_capture_job,
    claim_memory_capture_job,
    scan_memory_capture_jobs_once,
)
from tests.test_sd6b_settlement_regressions import isolated as _isolated

# 注册共享双库夹具，参数名不遮蔽 import。
isolated = _isolated


def _exercise(monkeypatch, case, isolated):
    """只替换 provider 传输，保留真实提取、JSON修复、observer和结果结算。"""
    engine, (job_id, _, _, _) = isolated
    observations = {
        "case": case,
        "provider_calls": 0,
        "transaction_during_provider": [],
        "client_timeout": None,
    }
    with Session(engine) as db:
        job = db.get(MemoryCaptureJob, job_id)
        if case == "revoked_preflight":
            member = db.get(User, job.user_id)
            member.membership_status = "inactive"
            db.add(member)
            db.commit()

        class Provider:
            """返回受控输出并观察实际SDK发送时数据库事务和超时。"""

            def __init__(self, **kwargs):
                """记录真实LLMClient构造时采用的超时，不建立网络连接。"""
                observations["client_timeout"] = kwargs["timeout"]
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def with_options(self, **_kwargs):
                """保持真实发送入口兼容，不改变来源或observer逻辑。"""
                return self

            def create(self, **_kwargs):
                """前两次可返回非法JSON，第三次返回合法记忆以验证真实修复次数。"""
                observations["provider_calls"] += 1
                observations["transaction_during_provider"].append(db.in_transaction())
                if (
                    case in {"dispatch_cap", "retry_control"} and observations["provider_calls"] < 3
                ) or (case == "repair_control" and observations["provider_calls"] == 1):
                    content = "invalid json"
                else:
                    content = json.dumps(
                        {
                            "memories": [
                                {
                                    "operation": "upsert",
                                    "kind": "fact",
                                    "key": "delivery_rule",
                                    "content": "完整新事实",
                                    "importance": 0.6,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                if case.startswith("invalid_"):
                    value = json.loads(content)
                    bad = dict(value["memories"][0])
                    bad["key"] = "invalid_fact"
                    if case == "invalid_key":
                        bad["key"] = "x" * 129
                    elif case == "invalid_kind":
                        bad["kind"] = "unsupported"
                    elif case == "invalid_importance":
                        bad["importance"] = 2
                    elif case == "invalid_duplicate":
                        bad["key"] = value["memories"][0]["key"]
                        bad["operation"] = "delete"
                    value["memories"].append(bad)
                    content = json.dumps(value)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content), finish_reason="stop"
                        )
                    ],
                    usage=None,
                    id="synthetic-response",
                )

        monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _: "synthetic-probe-only")
        monkeypatch.setattr("app.llm.client.OpenAI", Provider)
        result = process_memory_capture_job(db, job_id=job.id, worker_id="independent_llm_probe")
        db.expire_all()
        current = db.get(MemoryCaptureJob, job_id)
        if case == "retry_control":
            assert current.status == "retry" and current.claim_dispatch_count == 2
            current.next_attempt_at = utc_now() - timedelta(seconds=1)
            db.add(current)
            db.commit()
            result = process_memory_capture_job(db, job_id=job_id, worker_id="retry_worker")
            db.expire_all()
            current = db.get(MemoryCaptureJob, job_id)
        observations.update(
            result=result,
            status=current.status,
            attempt_count=current.attempt_count,
            model_dispatch_count=current.model_dispatch_count,
            stored_memories=len(db.exec(select(MemoryRecord)).all()),
        )
    return observations


@pytest.mark.parametrize("case", [
    "invalid_key", "invalid_kind", "invalid_importance", "invalid_duplicate",
])
def test_real_extraction_rejects_entire_invalid_delta(monkeypatch, case, isolated):
    """真实提取链不可截短键、过滤坏项或夹紧权重后保存响应中的合法半份。"""
    actual = _exercise(monkeypatch, case, isolated)
    assert actual["provider_calls"] == 1
    assert actual["status"] == "failed", actual
    assert actual["stored_memories"] == 0, actual
    engine, (job_id, _, _, _) = isolated
    with Session(engine) as db:
        assert db.get(MemoryCaptureJob, job_id).error_code == "MEMORY_CAPTURE_DELTA_INVALID"


@pytest.mark.parametrize(
    "case",
    [
        "healthy_control",
        "repair_control",
        "retry_control",
        "dispatch_cap",
        "model_transaction",
        "timeout_cap",
        "revoked_preflight",
    ],
)
def test_actual_model_boundaries(monkeypatch, case, isolated):
    """以相同真实worker链逐项断言单claim发送、无事务外呼及撤权后零披露。"""
    actual = _exercise(monkeypatch, case, isolated)
    if case == "healthy_control":
        assert (
            actual["status"] == "completed"
            and actual["provider_calls"] == 1
            and actual["stored_memories"] == 1
        )
    elif case in {"repair_control", "retry_control"}:
        assert actual["status"] == "completed" and actual["stored_memories"] == 1
        assert actual["provider_calls"] == (2 if case == "repair_control" else 3)
        assert actual["attempt_count"] == (1 if case == "repair_control" else 2)
    elif case == "dispatch_cap":
        assert actual["provider_calls"] <= 2, actual
    elif case == "model_transaction":
        assert not any(actual["transaction_during_provider"]), actual
    elif case == "timeout_cap":
        assert actual["client_timeout"] <= 60, actual
    elif case == "revoked_preflight":
        assert actual["provider_calls"] == 0 and actual["stored_memories"] == 0, actual


def test_expired_running_respects_max_claim_attempts(isolated):
    """失联的第三次 claim 不允许接管为第四次，扫描需结算失败。"""
    engine, (job_id, _, _, _) = isolated
    with Session(engine, expire_on_commit=False) as db:
        row = claim_memory_capture_job(db, worker_id="old", job_id=job_id)
        row.attempt_count = row.max_attempts
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        assert claim_memory_capture_job(db, worker_id="new", job_id=job_id) is None
        db.refresh(row)
        assert row.status == "failed" and row.attempt_count == 3


@pytest.mark.parametrize("status", ["pending", "running", "retry"])
def test_scanner_closes_expired_deadline_without_notification(isolated, status):
    """周期扫描无 job_id 时也要将截止任务转为终态，不能永久排除。"""
    engine, (job_id, _, _, _) = isolated
    with Session(engine) as db:
        row = db.get(MemoryCaptureJob, job_id)
        row.status = status
        row.deadline_at = utc_now() - timedelta(seconds=1)
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
    scan_memory_capture_jobs_once(worker_id="scanner", limit=1, database_engine=engine)
    with Session(engine) as db:
        row = db.get(MemoryCaptureJob, job_id)
        assert row.status == "failed"
        assert row.error_code == "MEMORY_CAPTURE_DEADLINE"


def test_three_claims_share_six_dispatch_cap_and_no_fourth_claim(isolated):
    """三次领取分别预留两次，全 job 六次耗尽后原持有者失败且不再重领。"""
    from app.memory.worker import (
        increment_model_dispatch_count,
        fail_memory_capture_job,
        MemoryCaptureFenceLost,
    )

    engine, (job_id, _, _, _) = isolated
    with Session(engine, expire_on_commit=False) as db:
        for attempt in range(1, 4):
            row = claim_memory_capture_job(db, worker_id="count", job_id=job_id)
            assert row.attempt_count == attempt and row.claim_dispatch_count == 0
            fence = row.fencing_token
            for _ in range(2):
                increment_model_dispatch_count(
                    db, job_id=job_id, worker_id="count", fencing_token=fence
                )
            with pytest.raises(MemoryCaptureFenceLost):
                increment_model_dispatch_count(
                    db, job_id=job_id, worker_id="count", fencing_token=fence
                )
            fail_memory_capture_job(
                db, job_id=job_id, worker_id="count", fencing_token=fence, error_code="TEST_RETRY"
            )
            db.refresh(row)
            if attempt < 3:
                row.next_attempt_at = utc_now() - timedelta(seconds=1)
                db.add(row)
                db.commit()
        assert row.status == "failed" and row.model_dispatch_count == 6
        assert claim_memory_capture_job(db, worker_id="fourth", job_id=job_id) is None


def test_active_last_claim_is_not_reaped(isolated):
    """有效第三次租约仍允许当前持有者返回，扫描不能将其当作失联任务。"""
    engine, (job_id, _, _, _) = isolated
    with Session(engine) as db:
        row = claim_memory_capture_job(db, worker_id="active", job_id=job_id)
        row.attempt_count = 3
        db.add(row)
        db.commit()
    assert scan_memory_capture_jobs_once(worker_id="scanner", limit=1, database_engine=engine) == 0
    with Session(engine) as db:
        row = db.get(MemoryCaptureJob, job_id)
        assert row.status == "running" and row.lease_owner == "active"
