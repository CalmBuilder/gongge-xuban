"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_model_final_audit.py
@CallChain  : 独立统一元模型复核 → 正式反馈/取消领用入口 → 模型迟到与当前授权
@Description: 两真实连接核验分析提交身份和原生RR旧授权，生产代码及既有断言不替换。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.api.agents import remove_chat_agent_usage
from app.api.chat import delete_message_feedback, upsert_message_feedback
from app.api.feedback import reanalyze_feedback
from app.db.models import AgentUsage, FeedbackMaterial, MessageFeedback, User
from app.feedback.materials import (
    MaterialPreviewRequest,
    MaterialWithdrawRequest,
    preview_material,
    withdraw_material,
)
from app.feedback.service import FeedbackAnalysisService
from app.llm.client import LLMClient, LLMError
from app.session.session_schema import MessageFeedbackRequest
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_materials_audit import _preview, _seed, _share

audit_engine = _feedback_engine


@pytest.mark.parametrize("action", ["preview", "share"])
def test_old_usage_snapshot_cannot_authorize_after_formal_cancel(
    audit_engine: Engine, action: str,
):
    """取消领用先提交后，已有RR旧快照的本人不得继续预览或授权材料。"""
    with Session(audit_engine) as creator:
        _seed(creator)
        material = _preview(creator)
    with Session(audit_engine) as stale, Session(audit_engine) as writer:
        old_owner = stale.get(User, "fb-u")
        assert stale.exec(select(AgentUsage).where(
            AgentUsage.user_id == "fb-u", AgentUsage.agent_id == "fb-agent",
        )).first() is not None
        result = remove_chat_agent_usage(
            "fb-agent", tenant_id="fb-t", current_user=writer.get(User, "fb-u"), db=writer,
        )
        assert result["removed"] is True
        with pytest.raises(HTTPException):
            if action == "preview":
                preview_material(stale, "fb-1", MaterialPreviewRequest(
                    tenant_id="fb-t", target_agent_id="fb-agent", request_key="after-cancel",
                ), old_owner)
            else:
                _share(stale, material)


@pytest.mark.parametrize("change", ["delete", "recreate", "reanalyze", "withdraw"])
def test_model_completion_obeys_original_analysis_identity(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch, change: str,
):
    """模型调用期间真实删除、重建、重分析或撤回，旧结果不得复活或重签材料。"""
    monkeypatch.setattr(LLMClient, "__init__", lambda *_: None)
    monkeypatch.setattr("app.api.chat.enqueue_feedback_analysis", lambda *_: None)
    monkeypatch.setattr("app.api.feedback.enqueue_feedback_analysis",
                        lambda *_: SimpleNamespace(id="synthetic-job"))
    with Session(audit_engine) as creator:
        _seed(creator)
        material = _preview(creator)
        _share(creator, material)
        original_hash = material.material_checksum

    def complete_after_change(self, prompt, payload):
        """外呼替身仅编排提交交错，事实与状态使用正式服务持久化。"""
        with Session(audit_engine) as writer:
            owner = writer.get(User, "fb-u")
            if change in {"delete", "recreate"}:
                delete_message_feedback("fb-answer", tenant_id="fb-t",
                                        current_user=owner, db=writer)
                if change == "recreate":
                    upsert_message_feedback("fb-answer", MessageFeedbackRequest(
                        tenant_id="fb-t", rating="down",
                    ), current_user=owner, db=writer)
            elif change == "reanalyze":
                reanalyze_feedback("fb-1", tenant_id="fb-t", current_user=owner, db=writer)
            else:
                withdraw_material(writer, material.id, MaterialWithdrawRequest(
                    tenant_id="fb-t", expected_revision=material.feedback_revision,
                ), owner)
        return {"bucket": "tool_or_system_issue", "summary": "旧模型结果", "reason": "旧结果"}

    monkeypatch.setattr(LLMClient, "generate_json", complete_after_change)
    with Session(audit_engine) as analyzer:
        result = FeedbackAnalysisService(analyzer).analyze_feedback("fb-1")
        assert (result is not None) == (change == "withdraw")
    with Session(audit_engine) as verifier:
        old = verifier.get(MessageFeedback, "fb-1")
        shared = verifier.get(FeedbackMaterial, material.id)
        assert shared.material_checksum == original_hash
        if change in {"delete", "recreate"}:
            assert old is None
            assert shared.status == "invalid"
            rows = verifier.exec(select(MessageFeedback)).all()
            assert len(rows) == (1 if change == "recreate" else 0)
            if rows:
                assert rows[0].id != "fb-1"
                assert rows[0].analysis_status == "pending"
                assert rows[0].analysis_summary is None
        elif change == "reanalyze":
            assert old.analysis_generation == 2
            assert old.revision == 1
            assert old.analysis_status == "pending"
            assert old.analysis_summary is None
            assert shared.status == "shared"
        else:
            assert shared.status == "withdrawn"
            assert old.analysis_status == "analyzed"


def test_retry_does_not_resend_payload_after_rating_changed(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch,
):
    """首调用失败时评分已修改，下一次外呼必须阻断且失败结算不污染新修订。"""
    monkeypatch.setattr(LLMClient, "__init__", lambda *_: None)
    monkeypatch.setattr("app.api.chat.enqueue_feedback_analysis", lambda *_: None)
    monkeypatch.setattr("app.feedback.service.time.sleep", lambda *_: None)
    calls = []

    def fail_after_change(self, prompt, payload):
        """在实际评分提交后返回稳定LLM错误，不使用锁或状态mock。"""
        calls.append(payload["feedback"]["revision"])
        with Session(audit_engine) as writer:
            upsert_message_feedback("fb-answer", MessageFeedbackRequest(
                tenant_id="fb-t", rating="up",
            ), current_user=writer.get(User, "fb-u"), db=writer)
        raise LLMError("synthetic transport failure")

    monkeypatch.setattr(LLMClient, "generate_json", fail_after_change)
    with Session(audit_engine) as analyzer:
        _seed(analyzer)
        assert FeedbackAnalysisService(analyzer).analyze_feedback("fb-1") is None
    assert calls == [1]
    with Session(audit_engine) as verifier:
        row = verifier.get(MessageFeedback, "fb-1")
        assert row.rating == "up"
        assert row.revision == 2
        assert row.analysis_status == "pending"
        assert row.analysis_summary is None
