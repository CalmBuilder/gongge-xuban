"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_feedback_sdk_dispatch.py
@CallChain  : 正式反馈API/另一连接提交 → FeedbackAnalysisService → LLMClient → SDK
@Description: 覆盖内部JSON修复/429及外层超时的失效派发，保留本人合法重试与CAS。
"""
import json
from types import SimpleNamespace

import httpx
from openai import APITimeoutError, RateLimitError
import pytest
from sqlmodel import Session, select

from app.api.chat import delete_message_feedback, upsert_message_feedback
from app.api.feedback import reanalyze_feedback
from app.db.models import MessageFeedback, User
from app.feedback.materials import MaterialWithdrawRequest, withdraw_material
from app.feedback.service import FeedbackAnalysisService
from app.session.session_schema import MessageFeedbackRequest
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_materials_audit import _preview, _seed, _share

dispatch_engine = _feedback_engine


@pytest.mark.parametrize("transport", ["json", "rate_limit", "timeout"])
@pytest.mark.parametrize("change", ["unchanged", "rating", "delete", "reanalyze", "withdraw"])
def test_committed_identity_checked_at_actual_sdk(dispatch_engine, monkeypatch, transport, change):
    """首次真实SDK期间正式提交身份变化；旧任务不再发送，新任务及分享撤回仍合法。"""
    monkeypatch.setattr("app.api.chat.enqueue_feedback_analysis", lambda *_: None)
    monkeypatch.setattr("app.api.feedback.enqueue_feedback_analysis",
                        lambda *_: SimpleNamespace(id="synthetic-job"))
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda _: "synthetic-only")
    monkeypatch.setattr("app.llm.client._wait_before_provider_retry", lambda *_: None)
    monkeypatch.setattr("app.feedback.service.time.sleep", lambda *_: None)
    with Session(dispatch_engine) as db:
        _seed(db)
        material = _preview(db)
        _share(db, material)
        material_id = material.id
    calls = []
    analysis_sessions = []

    class Provider:
        """仅替换末端供应商传输，不替换分析服务、JSON修复、派发守卫或数据库。"""

        def __init__(self, **kwargs):
            """保留OpenAI兼容client接口，合成配置不连接外部供应商。"""
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            """接受真实LLMClient的timeout/max_retries参数，不改其重试循环。"""
            assert kwargs.get("max_retries", 0) == 0
            return self

        def create(self, **kwargs):
            """观察实际请求并在第一响应前从另一Session正式提交评分/删除/重分析/撤回。"""
            assert all(not session.in_transaction() for session in analysis_sessions)
            sent = json.loads(kwargs["messages"][-1]["content"])
            calls.append(sent["feedback"]["revision"])
            if len(calls) == 1:
                with Session(dispatch_engine) as writer:
                    owner = writer.get(User, "fb-u")
                    if change == "delete":
                        delete_message_feedback("fb-answer", tenant_id="fb-t",
                                                current_user=owner, db=writer)
                    elif change == "reanalyze":
                        reanalyze_feedback("fb-1", tenant_id="fb-t", current_user=owner, db=writer)
                    elif change == "withdraw":
                        withdraw_material(writer, material_id, MaterialWithdrawRequest(
                            tenant_id="fb-t", expected_revision=1), owner)
                    elif change == "rating":
                        upsert_message_feedback("fb-answer", MessageFeedbackRequest(
                            tenant_id="fb-t", rating="up"), current_user=owner, db=writer)
                request = httpx.Request("POST", "http://synthetic.invalid/protocol")
                if transport == "rate_limit":
                    raise RateLimitError("synthetic rate limit", response=httpx.Response(
                        429, request=request), body={"error": {"code": "rate_limit_exceeded"}})
                if transport == "timeout":
                    raise APITimeoutError(request=request)
                text = "{ invalid-json"
            else:
                text = json.dumps({"bucket": "skill_issue", "summary": "合成归因",
                                   "reason": "合成协议说明"})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                   finish_reason="stop")], usage=None, id="synthetic-response")

    monkeypatch.setattr("app.llm.client.OpenAI", Provider)
    with Session(dispatch_engine) as db:
        analysis_sessions.append(db)
        result = FeedbackAnalysisService(db).analyze_feedback("fb-1")
    with Session(dispatch_engine) as db:
        row = db.get(MessageFeedback, "fb-1")
        if change in {"rating", "delete", "reanalyze"}:
            assert result is None
            assert calls == [1], "提交失效身份后不得再次发送旧输入"
            if change == "delete":
                assert row is None
                upsert_message_feedback("fb-answer", MessageFeedbackRequest(
                    tenant_id="fb-t", rating="down"), current_user=db.get(User, "fb-u"), db=db)
                row = db.exec(select(MessageFeedback).where(
                    MessageFeedback.message_id == "fb-answer")).one()
                assert row.id != "fb-1"
            else:
                assert row.analysis_status == "pending" and row.analysis_summary is None
                assert row.analysis_generation == 2 if change == "reanalyze" else row.revision == 2
            next_id = row.id
            db.commit()
            analysis_sessions.append(db)
            assert FeedbackAnalysisService(db).analyze_feedback(next_id).analysis_status == "analyzed"
            assert calls == [1, 2 if change == "rating" else 1]
        else:
            assert result is not None and row.analysis_status == "analyzed"
            assert calls == [1, 1]
