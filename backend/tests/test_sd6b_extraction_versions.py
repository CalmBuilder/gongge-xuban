"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_extraction_versions.py
@CallChain  : 双库 pytest → 真实 T0/T1 → worker/MemoryService/LLMClient → SDK → T3
@Description: 观察实际发送、JSON修复及结算，验证冻结提取版本与执行合同一致。
"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.core.agent_loop import AgentLoop
from app.db.models import AgentEvent, MemoryCaptureJob, MemoryKeyState, MemoryRecord
from app.memory import capture, service, worker
from tests.test_sd6b_settlement_regressions import isolated as _isolated
from tests.test_sd6b_source_regressions import _assistant, _seed


isolated = _isolated
NEW_MARKER = "合成第二版本提取规则。"


@pytest.mark.parametrize("case", [
    "normal", "pending_prompt", "unsupported_schema", "empty_schema", "empty_hash",
    "damaged_hash", "missing_prompt", "empty_prompt", "invalid_encoding",
    "after_dispatch", "repair_snapshot", "check_then_change", "new_source",
    "same_source_replay", "t3_mismatch", "repair_job_changed",
])
def test_extraction_version_matches_actual_execution(monkeypatch, tmp_path, isolated, case):
    """保持真实双库来源、授权与发送链，只在临时资源/SDK边界制造版本变化。"""

    engine, _ = isolated
    original = service.PROMPT_PATH.read_bytes()
    resources = tmp_path / "resources"
    prompt = resources / "app/llm/prompts/memory_extractor_prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_bytes(original)
    monkeypatch.setattr(capture.paths, "resource_dir", lambda: resources)
    monkeypatch.setattr(service, "PROMPT_PATH", prompt)
    observations = {"case": case, "dialect": engine.dialect.name, "calls": 0,
        "original_sent": [], "new_sent": [], "transactions": [], "snapshot_reads": 0}

    with Session(engine, expire_on_commit=False) as db:
        request, _, session, model, source = _seed(db)
        assistant = _assistant(db, session, source, suffix="version")
        registration = capture.register_memory_capture_job(db, request, session, model,
            user_message_id=source.id, assistant_message_id=assistant.id)
        assert registration is not None
        db.commit()
        job_id = registration.job.id
        frozen_hash = registration.job.prompt_hash
        declared_schema = registration.job.extractor_schema_version
        assert frozen_hash == hashlib.sha256(original).hexdigest()
        if case in {"unsupported_schema", "empty_schema", "empty_hash", "damaged_hash"}:
            row = registration.job
            if case == "unsupported_schema":
                row.extractor_schema_version = "sd6b.unsupported-v999"
            elif case == "empty_schema":
                row.extractor_schema_version = ""
            else:
                row.prompt_hash = "" if case == "empty_hash" else "damaged-version"
            frozen_hash, declared_schema = row.prompt_hash, row.extractor_schema_version
            db.add(row)
            db.commit()
        if case in {"pending_prompt", "new_source", "same_source_replay"}:
            prompt.write_bytes(original + ("\n" + NEW_MARKER + "\n").encode())
        elif case == "missing_prompt":
            prompt.unlink()
        elif case == "empty_prompt":
            prompt.write_bytes(b"")
        elif case == "invalid_encoding":
            prompt.write_bytes(b"\xff\xfe")
        if case == "same_source_replay":
            replay = capture.register_memory_capture_job(db, request, session, model,
                user_message_id=source.id, assistant_message_id=assistant.id)
            assert replay is not None and replay.job.id == job_id and not replay.created
            assert replay.job.prompt_hash == frozen_hash
            db.commit()
        if case == "new_source":
            request = request.model_copy(update={"message": "新来源采用新版本规则。"})
            source, _ = AgentLoop(db)._append_user_input_message(request, session.id)
            assistant = _assistant(db, session, source, suffix="new_version")
            fresh = capture.register_memory_capture_job(db, request, session, model,
                user_message_id=source.id, assistant_message_id=assistant.id)
            assert fresh is not None and fresh.created and fresh.job.id != job_id
            assert fresh.job.prompt_hash != frozen_hash
            job_id, frozen_hash = fresh.job.id, fresh.job.prompt_hash
            db.commit()
        source_id = source.id

    read_bytes = Path.read_bytes
    if case == "check_then_change":
        def read_and_change(path):
            """第一次可信读取后立即改变文件，下一层不能重读变化后的正文。"""
            value = read_bytes(path)
            if path == prompt:
                observations["snapshot_reads"] += 1
                prompt.write_bytes(original + ("\n" + NEW_MARKER).encode())
            return value
        monkeypatch.setattr(Path, "read_bytes", read_and_change)

    with Session(engine) as db:
        class Provider:
            """替换SDK传输，直接观察最终system消息和外呼期间事务。"""

            def __init__(self, **_kwargs):
                """创建仅用于合成响应的SDK接口，不建立供应商连接。"""
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def with_options(self, **_kwargs):
                """保持实际LLMClient超时配置入口与原SDK一致。"""
                return self

            def create(self, **kwargs):
                """首次发送后可改变资源或持久版本，后续修复仍需遵守原合同。"""
                observations["calls"] += 1
                system = next(item["content"] for item in kwargs["messages"]
                    if item["role"] == "system")
                observations["original_sent"].append(original.decode().strip() in system)
                observations["new_sent"].append(NEW_MARKER in system)
                observations["transactions"].append(db.in_transaction())
                if case in {"after_dispatch", "repair_snapshot"}:
                    prompt.write_bytes(original + ("\n" + NEW_MARKER).encode())
                if observations["calls"] == 1 and case in {"t3_mismatch", "repair_job_changed"}:
                    with Session(engine) as writer:
                        row = writer.get(MemoryCaptureJob, job_id)
                        row.prompt_hash = "f" * 64
                        writer.add(row)
                        writer.commit()
                content = ("invalid json" if observations["calls"] == 1
                    and case in {"repair_snapshot", "repair_job_changed"} else json.dumps({
                        "memories": [{"operation": "upsert", "kind": "fact", "key": "version_fact",
                            "content": "受控版本事实", "importance": 0.6}]}))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=content, tool_calls=None), finish_reason="stop")], usage=None,
                    id="synthetic-version-response", model="probe-model")

        monkeypatch.setattr("app.llm.client.OpenAI", Provider)
        result = worker.process_memory_capture_job(db, job_id=job_id, worker_id="version-worker")
        db.expire_all()
        job = db.get(MemoryCaptureJob, job_id)
        records = db.exec(select(MemoryRecord).where(MemoryRecord.capture_job_id == job_id)).all()
        keys = db.exec(select(MemoryKeyState).where(MemoryKeyState.last_job_id == job_id)).all()
        events = [row for row in db.exec(select(AgentEvent)).all()
            if (row.payload_json or {}).get("job_id") == job_id]
        observations.update(result=result, status=job.status, terminal_reason=job.terminal_reason,
            dispatch_count=job.model_dispatch_count, claim_dispatch_count=job.claim_dispatch_count,
            record_count=len(records), key_count=len(keys), schema=job.extractor_schema_version,
            prompt_hash_unchanged=job.prompt_hash == frozen_hash,
            event_types=[row.event_type for row in events])
        rejected = case in {"pending_prompt", "unsupported_schema", "empty_schema", "empty_hash",
            "damaged_hash", "missing_prompt", "empty_prompt", "invalid_encoding",
            "same_source_replay", "t3_mismatch", "repair_job_changed"}
        expected_calls = (1 if case in {"t3_mismatch", "repair_job_changed"} else 0) if rejected else (
            2 if case == "repair_snapshot" else 1)
        output = os.environ.get("SD6B_R5_OBSERVATIONS")
        if output:
            with Path(output).open("a") as handle:
                handle.write(json.dumps(observations, ensure_ascii=False) + "\n")
        assert observations["calls"] == expected_calls, observations
        assert job.model_dispatch_count == job.claim_dispatch_count == expected_calls
        assert not any(observations["transactions"])
        assert job.extractor_schema_version == declared_schema
        if case not in {"t3_mismatch", "repair_job_changed"}:
            assert job.prompt_hash == frozen_hash
        if rejected:
            assert job.status == "discarded" and job.terminal_reason == "capture_contract_changed"
            assert not records and not keys and "memory_saved" not in observations["event_types"]
            assert "memory_error" in observations["event_types"]
        else:
            assert job.status == "completed" and job.applied_count == len(records) == len(keys) == 1
            assert "memory_saved" in observations["event_types"]
            assert all(observations["original_sent"])
            assert any(observations["new_sent"]) == (case == "new_source")
        if case == "check_then_change":
            assert observations["snapshot_reads"] == 1
        same_source = db.exec(select(MemoryCaptureJob).where(
            MemoryCaptureJob.source_message_id == source_id)).all()
        assert len(same_source) == 1
