"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_materials_audit.py
@CallChain  : 本人/管理者真实服务与独立数据库连接 → 分享修订/权限/撤回提交边界
@Description: 独立元模型审核用双方言黑盒反例，禁止旧快照复活或扩大本人会话权限。
"""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.api.chat import delete_message_feedback, upsert_message_feedback
from app.api.feedback import get_feedback_session_detail
from app.db.models import (
    AgentProfile,
    AgentUsage,
    ChatSession,
    FeedbackMaterial,
    Message,
    MessageFeedback,
    Tenant,
    User,
)
from app.feedback.materials import (
    MaterialPreviewRequest,
    MaterialShareRequest,
    MaterialWithdrawRequest,
    get_material,
    list_materials,
    preview_material,
    share_material,
    withdraw_material,
)
from app.session.session_schema import MessageFeedbackRequest
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_materials import seed_feedback

audit_engine = _feedback_engine


def _seed(db: Session) -> None:
    """构造两个合法身份、另一管理范围与合成敏感原文，不预置分享成功。"""
    seed_feedback(db)
    db.add(User(id="fb-manager", tenant_id="fb-t", username="manager", password_hash="synthetic"))
    db.add(User(id="fb-stranger", tenant_id="fb-t", username="stranger", password_hash="synthetic"))
    db.add(Tenant(id="fb-other-t", name="另一隔离租户"))
    db.add(User(id="fb-other-u", tenant_id="fb-other-t", username="other", password_hash="synthetic"))
    db.add(AgentProfile(id="fb-agent", tenant_id="fb-t", name="目标员工",
                        owner_user_id="fb-manager", published_to_gallery=True))
    db.add(AgentProfile(id="fb-other-agent", tenant_id="fb-t", name="另一员工",
                        owner_user_id="fb-stranger", published_to_gallery=True))
    db.add(AgentUsage(tenant_id="fb-t", agent_id="fb-agent", user_id="fb-u"))
    chat = db.get(ChatSession, "fb-s")
    chat.agent_id = "fb-agent"
    db.add(chat)
    message = db.get(Message, "fb-answer")
    message.content = "SYNTHETIC_REPLY_PRIVATE_MARKER"
    db.add(message)
    feedback = db.get(MessageFeedback, "fb-1")
    feedback.analysis_bucket = "skill_issue"
    feedback.analysis_summary = "SYNTHETIC_MODEL_PRIVATE_MARKER"
    feedback.analysis_json = {"secret": "SYNTHETIC_ANALYSIS_PRIVATE_MARKER"}
    db.add(feedback)
    db.commit()


def _preview(db: Session, request_key: str = "audit-preview", note: str = "缺少最终业务确认"):
    """通过真实服务生成本人待确认材料，反馈及目标均由数据库核验。"""
    return preview_material(db, "fb-1", MaterialPreviewRequest(
        tenant_id="fb-t", target_agent_id="fb-agent", note=note, request_key=request_key,
    ), db.get(User, "fb-u"))


def _share(db: Session, material):
    """使用服务端实际预览身份和摘要完成明确分享。"""
    return share_material(db, material.id, MaterialShareRequest(
        tenant_id="fb-t", expected_revision=material.feedback_revision,
        material_checksum=material.material_checksum,
    ), db.get(User, "fb-u"))


def test_owner_preview_manager_share_does_not_grant_session_read(audit_engine: Engine):
    """管理者分享前不可取材，分享后只见投影而原会话仍为本人私有。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db)
        with pytest.raises(HTTPException):
            get_material(db, material.id, db.get(User, "fb-manager"))
        db.rollback()
        shared = _share(db, material)
        visible = get_material(db, material.id, db.get(User, "fb-manager"))
        assert visible.id == shared.id
        assert visible.status == "shared"
        payload = json.dumps(visible.model_dump(), ensure_ascii=False)
        for marker in ["SYNTHETIC_REPLY_PRIVATE_MARKER", "SYNTHETIC_PRIVATE_SLOT",
                       "SYNTHETIC_MODEL_PRIVATE_MARKER", "SYNTHETIC_ANALYSIS_PRIVATE_MARKER"]:
            assert marker not in payload
        assert visible.material.user_note == "缺少最终业务确认"
        assert visible.material.inference["bucket"] == "skill_issue"
        with pytest.raises(HTTPException) as denied:
            get_feedback_session_detail("fb-s", tenant_id="fb-t",
                                        current_user=db.get(User, "fb-manager"), db=db)
        assert denied.value.status_code == 404


@pytest.mark.parametrize("actor_id", ["fb-stranger", "fb-other-u"])
def test_unrelated_actor_cannot_preview_share_or_read(audit_engine: Engine, actor_id: str):
    """同租户另一员工管理者及跨租户身份均不能借指定来源取得材料。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db)
        _share(db, material)
        actor = db.get(User, actor_id)
        with pytest.raises(HTTPException):
            preview_material(db, "fb-1", MaterialPreviewRequest(
                tenant_id="fb-t", target_agent_id="fb-agent", request_key="hostile",
            ), actor)
        db.rollback()
        with pytest.raises(HTTPException):
            _ = get_material(db, material.id, actor)
        db.rollback()
        with pytest.raises(HTTPException):
            share_material(db, material.id, MaterialShareRequest(
                tenant_id="fb-t", expected_revision=material.feedback_revision,
                material_checksum=material.material_checksum,
            ), actor)


def test_note_sensitive_patterns_are_removed_from_confirmed_projection(audit_engine: Engine):
    """已知密码、凭据、邮箱、手机号及URL只在本人预览中呈现清洗结果。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db, note=(
            "password=SyntheticPassword42 API_KEY:SyntheticKey42 "
            "电话13800138000 邮箱synthetic@example.invalid "
            "链接https://example.invalid/private"
        ))
        _share(db, material)
        visible = get_material(db, material.id, db.get(User, "fb-manager"))
        body = json.dumps(visible.model_dump(), ensure_ascii=False)
        for value in ["SyntheticPassword42", "SyntheticKey42", "13800138000",
                      "synthetic@example.invalid", "https://example.invalid/private"]:
            assert value not in body
        assert "已隐藏" in visible.material.user_note


def test_preview_share_replays_are_stable_and_changed_parameters_conflict(audit_engine: Engine):
    """同请求返回同材料，同幂等键变说明冲突；伪造修订或摘要不能分享。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db)
        replay = _preview(db)
        assert replay.id == material.id
        assert replay.material_checksum == material.material_checksum
        with pytest.raises(HTTPException) as conflict:
            _preview(db, note="变参请求")
        assert conflict.value.status_code == 409
        db.rollback()
        for revision, checksum in [(material.feedback_revision + 1, material.material_checksum),
                                   (material.feedback_revision, "0" * 64)]:
            with pytest.raises(HTTPException) as invalid:
                share_material(db, material.id, MaterialShareRequest(
                    tenant_id="fb-t", expected_revision=revision, material_checksum=checksum,
                ), db.get(User, "fb-u"))
            assert invalid.value.status_code == 409
            db.rollback()
        shared = _share(db, material)
        repeated = _share(db, material)
        assert shared.id == repeated.id
        assert shared.material_checksum == repeated.material_checksum


@pytest.mark.parametrize("change", ["rating", "delete"])
def test_feedback_write_invalidates_materials_in_real_commit(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch, change: str,
):
    """另一连接从正式评分修改/删除入口提交后，材料读写与列表同步拒绝。"""
    monkeypatch.setattr("app.api.chat.enqueue_feedback_analysis", lambda *_: None)
    with Session(audit_engine) as creator:
        _seed(creator)
        material = _preview(creator)
        _share(creator, material)
    with Session(audit_engine) as writer:
        owner = writer.get(User, "fb-u")
        if change == "rating":
            upsert_message_feedback("fb-answer", MessageFeedbackRequest(
                tenant_id="fb-t", rating="up",
            ), current_user=owner, db=writer)
        else:
            delete_message_feedback("fb-answer", tenant_id="fb-t", current_user=owner, db=writer)
    with Session(audit_engine) as reader:
        persisted = reader.get(FeedbackMaterial, material.id)
        assert persisted.status == "invalid"
        assert list_materials(reader, reader.get(User, "fb-manager"), "fb-agent", "managed") == []
        with pytest.raises(HTTPException):
            get_material(reader, material.id, reader.get(User, "fb-manager"))
        reader.rollback()
        with pytest.raises(HTTPException):
            _share(reader, material)


def test_withdrawn_identity_never_revives_and_new_preview_uses_new_identity(audit_engine: Engine):
    """撤回重放只返回无正文终态，旧ID与旧幂等键不复活，新分享另建身份。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db)
        _share(db, material)
        request = MaterialWithdrawRequest(tenant_id="fb-t", expected_revision=material.feedback_revision)
        withdrawn = withdraw_material(db, material.id, request, db.get(User, "fb-u"))
        again = withdraw_material(db, material.id, request, db.get(User, "fb-u"))
        assert withdrawn.status == again.status == "withdrawn"
        assert withdrawn.material.user_note == again.material.user_note == ""
        with pytest.raises(HTTPException):
            _share(db, material)
        db.rollback()
        with pytest.raises(HTTPException):
            _preview(db)
        db.rollback()
        fresh = _preview(db, request_key="fresh-preview")
        assert fresh.id != material.id
        assert _share(db, fresh).status == "shared"
        assert db.get(FeedbackMaterial, material.id, populate_existing=True).status == "withdrawn"


def test_old_transaction_snapshot_cannot_resurrect_committed_withdrawal(audit_engine: Engine):
    """两独立连接先建旧RR快照再提交撤回，旧连接分享不能覆盖终态。"""
    with Session(audit_engine) as creator:
        _seed(creator)
        material = _preview(creator)
        _share(creator, material)
    with Session(audit_engine) as stale, Session(audit_engine) as writer:
        assert stale.get(FeedbackMaterial, material.id).status == "shared"
        stale_owner = stale.get(User, "fb-u")
        withdraw_material(writer, material.id, MaterialWithdrawRequest(
            tenant_id="fb-t", expected_revision=material.feedback_revision,
        ), writer.get(User, "fb-u"))
        with pytest.raises(HTTPException):
            share_material(stale, material.id, MaterialShareRequest(
                tenant_id="fb-t", expected_revision=material.feedback_revision,
                material_checksum=material.material_checksum,
            ), stale_owner)
        stale.rollback()
    with Session(audit_engine) as verifier:
        assert verifier.get(FeedbackMaterial, material.id).status == "withdrawn"


def test_old_transaction_actor_snapshot_cannot_read_after_membership_revoked(audit_engine: Engine):
    """旧连接已读合法管理者后另一连接撤销成员资格，材料不得再外发。"""
    with Session(audit_engine) as creator:
        _seed(creator)
        material = _preview(creator)
        _share(creator, material)
    with Session(audit_engine) as stale, Session(audit_engine) as writer:
        manager = stale.get(User, "fb-manager")
        assert manager.membership_status == "active"
        current = writer.get(User, "fb-manager")
        current.membership_status = "inactive"
        writer.add(current)
        writer.commit()
        with pytest.raises(HTTPException) as denied:
            get_material(stale, material.id, manager)
        assert denied.value.status_code == 403


def test_invalid_list_mode_cannot_skip_manager_check(audit_engine: Engine):
    """服务边界拒绝非法读取模式，不能落入已分享查询绕过目标管理权限。"""
    with Session(audit_engine) as db:
        _seed(db)
        material = _preview(db)
        _share(db, material)
        with pytest.raises(HTTPException):
            list_materials(db, db.get(User, "fb-stranger"), "fb-agent", "unexpected")


@pytest.mark.parametrize("target", ["", "   ", "fb-other-agent"])
def test_wrong_or_empty_target_cannot_receive_source_material(audit_engine: Engine, target: str):
    """空scope和另一员工不能接收来源反馈；输入或领域边界必须拒绝。"""
    from pydantic import ValidationError

    with Session(audit_engine) as db:
        _seed(db)
        with pytest.raises((HTTPException, ValidationError)):
            request = MaterialPreviewRequest(tenant_id="fb-t", target_agent_id=target,
                                             request_key="wrong-target")
            preview_material(db, "fb-1", request, db.get(User, "fb-u"))
