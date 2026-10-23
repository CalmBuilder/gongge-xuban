"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : materials.py
@CallChain  : 本人反馈预览/明确分享/撤回 → 当前目标授权 → 最小不可复活材料
@Description: 分享只承接结构化事实及本人说明，锁定反馈修订而不暴露会话全文。
"""
import hashlib
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.db.models import AgentProfile, ChatSession, FeedbackMaterial, Message, MessageFeedback, User, utc_now
from app.feedback.source import FeedbackSource, feedback_source
from app.security.permissions import can_use_agent_in_chat, ensure_agent_scope_manager


class MaterialPreviewRequest(BaseModel):
    """本人提交预览的目标与最小业务说明；幂等键不赋予分享权限。"""

    tenant_id: str
    target_agent_id: str = Field(min_length=1, max_length=128)
    note: str = Field(default='', max_length=2000)
    request_key: str = Field(min_length=1, max_length=128)


class MaterialShareRequest(BaseModel):
    """只确认服务端已生成快照的修订与摘要，不接受客户端替换材料。"""

    tenant_id: str
    expected_revision: int = Field(ge=1)
    material_checksum: str = Field(min_length=64, max_length=64)


class MaterialWithdrawRequest(BaseModel):
    """撤回指定不可复活分享身份，重放只确认已撤回状态。"""

    tenant_id: str
    expected_revision: int = Field(ge=1)


class MaterialFacts(BaseModel):
    """回执只包含可证明的状态/身份，正文和工具错误不进入共享投影。"""

    rating: str
    user_request: str = '原问题正文未分享'
    assistant_reply: str = '原回复正文未分享'
    operations: list[dict[str, str]] = Field(default_factory=list)
    operations_known: bool = False


class MaterialProjection(BaseModel):
    """区分原执行事实、本人补充说明和模型分类推断。"""

    source: FeedbackSource
    facts: MaterialFacts
    user_note: str
    inference: dict[str, str] | None = None


class FeedbackMaterialRead(BaseModel):
    """授权后才返回材料内容，不附加拥有者会话或模型分析全文。"""

    id: str
    feedback_id: str
    feedback_revision: int
    target_agent_id: str
    status: str
    material_checksum: str
    material: MaterialProjection
    created_at: str
    updated_at: str


def digest(value: object) -> str:
    """稳定摘要绑定服务端材料及原请求参数，不输出原文。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def redact_note(value: str) -> str:
    """清洗已知凭据/PII模式；不宣称正则能识别任意秘密，仍需本人预览确认。"""
    text = re.sub(r'(?im)(password|密码|api[_ -]?key|token|authorization|secret)\s*[:=：]\s*\S+',
                  '[敏感值已隐藏]', value)
    text = re.sub(r'https?://[^\s]+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)',
                  '[联系信息已隐藏]', text)
    return re.sub(r'(?<!\w)[A-Za-z0-9_-]{24,}(?!\w)', '[标记已隐藏]', text).strip()


def lock_feedback(db: Session, tenant_id: str, feedback_id: str) -> MessageFeedback:
    """评分/分享共用真实写屏障；SQLite先写、MySQL锁读当前行，拒绝旧RR快照。"""
    try:
        db.exec(update(MessageFeedback).where(MessageFeedback.id == feedback_id,
                                             MessageFeedback.tenant_id == tenant_id)
                .values(revision=MessageFeedback.revision)
                .execution_options(synchronize_session=False))
        row = db.exec(select(MessageFeedback).where(MessageFeedback.id == feedback_id,
                                                    MessageFeedback.tenant_id == tenant_id)
                      .with_for_update().execution_options(populate_existing=True)).first()
    except OperationalError as error:
        db.rollback()
        raise HTTPException(409, 'FEEDBACK_WRITE_CONFLICT') from error
    if row is None:
        raise HTTPException(404, 'Feedback not found')
    return row


def invalidate_materials(db: Session, feedback_id: str) -> None:
    """评分修改/删除与失效材料在同一短事务提交，永久保留旧身份。"""
    db.exec(update(FeedbackMaterial).where(FeedbackMaterial.feedback_id == feedback_id,
                                          FeedbackMaterial.status.in_(['preview', 'shared']))
            .values(status='invalid', updated_at=utc_now())
            .execution_options(synchronize_session=False))


def fresh_actor(db: Session, actor: User) -> User:
    """读取当前成员状态，不信过时的API依赖对象或客户端角色。"""
    current = db.exec(select(User).where(User.id == actor.id).with_for_update()
                      .execution_options(populate_existing=True)).first()
    if (current is None or current.tenant_id != actor.tenant_id
            or current.membership_status != 'active'):
        raise HTTPException(403, 'FEEDBACK_ACTOR_UNAVAILABLE')
    return current


def owned_feedback(db: Session, feedback: MessageFeedback, actor: User) -> tuple[ChatSession, Message]:
    """仅本人可生成/授权材料，历史反馈不会自动分享给管理员。"""
    chat = db.get(ChatSession, feedback.session_id, populate_existing=True)
    message = db.get(Message, feedback.message_id)
    if (feedback.tenant_id != actor.tenant_id or feedback.user_id != actor.id
            or chat is None or chat.user_id != actor.id or chat.tenant_id != actor.tenant_id
            or message is None or message.tenant_id != actor.tenant_id
            or message.session_id != chat.id or message.role != 'assistant'):
        raise HTTPException(404, 'Feedback not found')
    return chat, message


def material_read(row: FeedbackMaterial, *, body: bool = True) -> FeedbackMaterialRead:
    """只序列化经过授权的服务端投影；撤回回执不继续携带旧材料正文。"""
    projection = row.material_json if body else MaterialProjection(
        source=FeedbackSource(message_id='', missing_reason='材料已撤回'),
        facts=MaterialFacts(rating=''), user_note='').model_dump()
    return FeedbackMaterialRead(id=row.id, feedback_id=row.feedback_id,
        feedback_revision=row.feedback_revision, target_agent_id=row.target_agent_id,
        status=row.status, material_checksum=row.material_checksum, material=projection,
        created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())


def valid_material(db: Session, row: FeedbackMaterial,
                   feedback: MessageFeedback | None = None) -> MessageFeedback:
    """当前反馈修订与不可变材料摘要必须同时成立，撤回身份不能重新激活。"""
    if feedback is None:
        feedback = db.exec(select(MessageFeedback).where(MessageFeedback.id == row.feedback_id,
            MessageFeedback.tenant_id == row.tenant_id).with_for_update()
            .execution_options(populate_existing=True)).first()
    if (row.status not in {'preview', 'shared'} or feedback is None
            or feedback.tenant_id != row.tenant_id or feedback.revision != row.feedback_revision
            or feedback.user_id != row.owner_user_id
            or digest(row.material_json) != row.material_checksum):
        raise HTTPException(409, 'FEEDBACK_MATERIAL_INVALID')
    message = db.exec(select(Message).where(Message.id == feedback.message_id,
        Message.tenant_id == row.tenant_id).with_for_update()
        .execution_options(populate_existing=True)).first()
    saved_source = row.material_json.get('source')
    if (message is None or message.session_id != feedback.session_id
            or message.role != 'assistant'
            or feedback_source(db, message).model_dump() != saved_source):
        raise HTTPException(409, 'FEEDBACK_MATERIAL_INVALID')
    return feedback


def current_material(db: Session, material_id: str) -> FeedbackMaterial:
    """反馈屏障之后用当前锁读取材料，不让MySQL旧RR快照回退已撤回状态。"""
    row = db.exec(select(FeedbackMaterial).where(FeedbackMaterial.id == material_id)
        .with_for_update().execution_options(populate_existing=True)).first()
    if row is None:
        raise HTTPException(404, 'Material not found')
    return row


def preview_material(db: Session, feedback_id: str, request: MaterialPreviewRequest,
                     actor: User) -> FeedbackMaterialRead:
    """本人先预览最小脱敏快照；同幂等键变参冲突，旧失效ID不复活。"""
    actor = fresh_actor(db, actor)
    if request.tenant_id != actor.tenant_id:
        raise HTTPException(403, 'Tenant mismatch')
    agent = db.get(AgentProfile, request.target_agent_id, populate_existing=True,
                   with_for_update=True)
    feedback = lock_feedback(db, actor.tenant_id, feedback_id)
    chat, message = owned_feedback(db, feedback, actor)
    source = feedback_source(db, message)
    target = source.agent_id or chat.agent_id
    if (target != request.target_agent_id or agent is None
            or not can_use_agent_in_chat(db, agent, actor, current=True)):
        raise HTTPException(403, 'FEEDBACK_TARGET_UNAVAILABLE')
    request_hash = digest({'feedback_id': feedback_id, 'revision': feedback.revision,
                           'target': request.target_agent_id, 'note': request.note})
    existing = db.exec(select(FeedbackMaterial).where(FeedbackMaterial.tenant_id == actor.tenant_id,
        FeedbackMaterial.owner_user_id == actor.id,
        FeedbackMaterial.request_key == request.request_key)
        .with_for_update().execution_options(populate_existing=True)).first()
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(409, 'FEEDBACK_REQUEST_CONFLICT')
        valid_material(db, existing)
        db.commit()
        return material_read(existing)
    snapshot = (message.metadata_json or {}).get('feedback_source') or {}
    operations = snapshot.get('operations') if source.status == 'verified' else None
    inference = None
    if feedback.analysis_bucket:
        from app.feedback.service import ALLOWED_BUCKETS

        inference = {'bucket': feedback.analysis_bucket if feedback.analysis_bucket in ALLOWED_BUCKETS else 'unknown',
                     'summary': '模型分类推断，不是执行事实', 'reason': '原分析正文仅本人可见'}
    projection = MaterialProjection(source=source,
        facts=MaterialFacts(rating=feedback.rating,
            operations=[{key: str(op[key]) for key in ['id', 'status', 'tool_id']}
                        for op in (operations or [])], operations_known=isinstance(operations, list)),
        user_note=redact_note(request.note), inference=inference).model_dump()
    row = FeedbackMaterial(tenant_id=actor.tenant_id, feedback_id=feedback.id,
        feedback_revision=feedback.revision, owner_user_id=actor.id,
        target_agent_id=request.target_agent_id, request_key=request.request_key,
        request_hash=request_hash, material_checksum=digest(projection), material_json=projection)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, 'FEEDBACK_REQUEST_CONFLICT') from error
    db.refresh(row)
    return material_read(row)


def get_material(db: Session, material_id: str, actor: User) -> FeedbackMaterialRead:
    """管理者仅读当前目标内已分享材料，不由此取得他人会话全文权限。"""
    actor = fresh_actor(db, actor)
    row = db.get(FeedbackMaterial, material_id, populate_existing=True)
    if row is None or row.tenant_id != actor.tenant_id:
        raise HTTPException(404, 'Material not found')
    db.get(AgentProfile, row.target_agent_id, populate_existing=True, with_for_update=True)
    if row.owner_user_id != actor.id:
        ensure_agent_scope_manager(db, actor.tenant_id, row.target_agent_id, actor)
    feedback = lock_feedback(db, actor.tenant_id, row.feedback_id)
    row = current_material(db, material_id)
    if row.owner_user_id != actor.id:
        if row.status != 'shared':
            raise HTTPException(404, 'Material not found')
    valid_material(db, row, feedback)
    return material_read(row)


def share_material(db: Session, material_id: str, request: MaterialShareRequest,
                   actor: User) -> FeedbackMaterialRead:
    """最终分享短事务与评分共用反馈锁，严格确认本人预览修订/摘要和原目标。"""
    actor = fresh_actor(db, actor)
    row = db.get(FeedbackMaterial, material_id)
    if (row is None or row.tenant_id != actor.tenant_id or request.tenant_id != actor.tenant_id
            or row.owner_user_id != actor.id):
        raise HTTPException(404, 'Material not found')
    agent = db.get(AgentProfile, row.target_agent_id, populate_existing=True,
                   with_for_update=True)
    feedback = lock_feedback(db, actor.tenant_id, row.feedback_id)
    row = current_material(db, material_id)
    chat, message = owned_feedback(db, feedback, actor)
    valid_material(db, row, feedback)
    if (request.expected_revision != row.feedback_revision
            or request.material_checksum != row.material_checksum
            or (feedback_source(db, message).agent_id or chat.agent_id) != row.target_agent_id
            or agent is None or not can_use_agent_in_chat(db, agent, actor, current=True)):
        raise HTTPException(409, 'FEEDBACK_MATERIAL_INVALID')
    row.status = 'shared'
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return material_read(row)


def withdraw_material(db: Session, material_id: str, request: MaterialWithdrawRequest,
                      actor: User) -> FeedbackMaterialRead:
    """本人撤回永久结束该分享；相同请求重放不返回已失效的内容。"""
    actor = fresh_actor(db, actor)
    row = db.get(FeedbackMaterial, material_id)
    if (row is None or row.tenant_id != actor.tenant_id or request.tenant_id != actor.tenant_id
            or row.owner_user_id != actor.id):
        raise HTTPException(404, 'Material not found')
    lock_feedback(db, actor.tenant_id, row.feedback_id)
    row = current_material(db, material_id)
    if request.expected_revision != row.feedback_revision:
        raise HTTPException(409, 'FEEDBACK_MATERIAL_INVALID')
    row.status = 'withdrawn'
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return material_read(row, body=False)


def list_materials(db: Session, actor: User, agent_id: str, mode: str) -> list[FeedbackMaterialRead]:
    """仅按明确目标列出有效本人或已分享材料，不提供跨员工全量入口。"""
    if mode not in {'owned', 'managed'} or not agent_id.strip():
        raise HTTPException(403, 'FEEDBACK_SCOPE_REQUIRED')
    actor = fresh_actor(db, actor)
    db.get(AgentProfile, agent_id, populate_existing=True, with_for_update=True)
    if mode == 'managed':
        ensure_agent_scope_manager(db, actor.tenant_id, agent_id, actor)
    query = select(FeedbackMaterial).where(FeedbackMaterial.tenant_id == actor.tenant_id,
        FeedbackMaterial.target_agent_id == agent_id,
        FeedbackMaterial.status.in_(['preview', 'shared']))
    query = query.where(FeedbackMaterial.owner_user_id == actor.id) if mode == 'owned' else query.where(
        FeedbackMaterial.status == 'shared')
    result = []
    for row in db.exec(query.order_by(FeedbackMaterial.created_at.desc()).limit(100)).all():
        try:
            feedback = lock_feedback(db, actor.tenant_id, row.feedback_id)
            row = current_material(db, row.id)
            if mode == 'managed' and row.status != 'shared':
                continue
            valid_material(db, row, feedback)
        except HTTPException:
            continue
        result.append(material_read(row))
    return result
