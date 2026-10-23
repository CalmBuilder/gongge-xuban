"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f1_plain_memory_completion.py
@CallChain  : 原AgentLoop流式普通回答 → 原complete事务 → MemoryCaptureJob/唤醒读回
@Description: 复现两个普通回答早返回漏入队，锁定精确同轮来源和终态失败零残留。
"""

from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.core.non_sop_capability import NonSopCapabilityRouter
from app.db.models import AgentEvent, MemoryCaptureJob, Message, Skill
from app.general_skills.schema import GeneralSkillSelection
from app.observability.event_log import TurnEventFenceClosed
from app.session.session_schema import ChatTurnRequest, RouterDecision
from tests import test_final_f1_external_resume as resume_tests
from tests.test_sd6b_memory_capture import _seed_turn


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def completion_engine(request, tmp_path):
    """SQLite独立文件或真实Alembic随机原生MySQL，沿已有F1自有清理边界。"""
    yield from resume_tests.resume_engine.__wrapped__(request, tmp_path)


@pytest.mark.parametrize("catalog_available", [False, True])
@pytest.mark.parametrize("reject_completion", [False, True])
def test_plain_stream_registers_exact_memory_job_with_canonical_completion(
    completion_engine, monkeypatch, catalog_available, reject_completion,
):
    """两条普通回答沿原完成事务入队；终态提交被拒绝时job与唤醒均为零。"""
    wakeups = []

    def wake(job_id):
        """通知发出时用第二真实Session确认原job及同轮complete均已提交。"""
        with Session(completion_engine) as check:
            job = check.get(MemoryCaptureJob, job_id)
            assert job is not None
            events = check.exec(select(AgentEvent).where(
                AgentEvent.event_type == "complete",
                AgentEvent.session_id == "session_sd6b_capture",
            )).all()
            assert len(events) == 1
            assert events[0].payload_json["user_message_id"] == job.source_message_id
        wakeups.append(job_id)

    monkeypatch.setattr("app.core.agent_loop.wake_memory_capture_job", wake)
    with Session(completion_engine, expire_on_commit=False) as db:
        loop, session, model, _initial = _seed_turn(db)
        db.commit()
        resource = Skill(tenant_id=session.tenant_id, skill_id="f1_plain_available",
                         name="可用场景资源", status="published", content_json={})
        monkeypatch.setattr(loop, "_get_request_model", lambda *_a, **_k: model)
        monkeypatch.setattr(loop, "_list_published_skills", lambda *_a, **_k:
                            [resource] if catalog_available else [])
        loop.router = SimpleNamespace(decide=lambda *_a, **_k: RouterDecision(
            decision="answer_only", reason="明确普通偏好咨询，不推进已有场景"))
        loop.general_skill_selector = SimpleNamespace(decide=lambda *_a, **_k:
                                                       GeneralSkillSelection())
        loop.non_sop_capability_router = NonSopCapabilityRouter(shadow_enabled=False)
        monkeypatch.setattr(loop, "_generate_reply_stream_segment", lambda *_a, **_k:
                            iter(["稳定服务偏好已接收。", "本轮不办理业务。"] ))
        monkeypatch.setattr(loop, "_pace_stream", lambda: None)
        original_event = loop._stream_event

        def stream_event(name, *args, **kwargs):
            """仅在原canonical终态提交边界拒绝，不替换登记或消息持久化逻辑。"""
            if reject_completion and name == "complete":
                raise TurnEventFenceClosed("F1_COMPLETION_ALREADY_CLOSED")
            return original_event(name, *args, **kwargs)

        monkeypatch.setattr(loop, "_stream_event", stream_event)
        request = ChatTurnRequest(tenant_id=session.tenant_id, session_id=session.id,
                                 agent_id=session.agent_id, user_id=session.user_id,
                                 client_turn_id="f1-plain-source",
                                 message="请记住我要求保留完整原始回执，勿开始业务。")
        frames = list(loop.handle_turn_stream(request))
        if not reject_completion:
            assert any(frame["event"] == "complete" for frame in frames)
    with Session(completion_engine) as check:
        jobs = check.exec(select(MemoryCaptureJob)).all()
        if reject_completion:
            assert jobs == []
            assert wakeups == []
            return
        assert len(jobs) == 1
        job = jobs[0]
        source = check.get(Message, job.source_message_id)
        assistant = check.get(Message, job.source_assistant_message_id)
        assert source is not None and source.role == "user" and source.content == request.message
        assert assistant is not None and assistant.role == "assistant"
        assert assistant.metadata_json["user_message_id"] == source.id
        assert job.status == "pending" and job.model_dispatch_count == 0
        assert job.model_config_id == model.id and job.model_contract_hash
        assert job.source_manifest_json["source_message_id"] == source.id
        assert job.source_manifest_json["source_assistant_message_id"] == assistant.id
        assert wakeups == [job.id]
