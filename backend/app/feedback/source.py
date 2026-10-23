"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : source.py
@CallChain  : Runtime最终回复 → 精确来源快照 → 本人反馈/授权材料
@Description: 绑定原轮与冻结发布身份，拒绝按当前技能或最新事件猜历史来源。
"""
from typing import Literal

from pydantic import BaseModel
from sqlmodel import Session, select

from app.db.models import ChatSession, Message, SkillVersion, SopInstance, SopOperation


class FeedbackSource(BaseModel):
    """表达可证明的历史执行身份；缺失与普通DTA不伪造SOP发布身份。"""

    status: Literal['verified', 'missing'] = 'missing'
    branch: Literal['sop', 'dynamic_task', 'unknown'] = 'unknown'
    message_id: str
    turn_id: str | None = None
    execution_id: str | None = None
    skill_id: str | None = None
    skill_version_id: str | None = None
    skill_version: str | None = None
    definition_checksum: str | None = None
    agent_id: str | None = None
    missing_reason: str | None = '原消息缺少可证明的执行来源'


def freeze_execution_source(db: Session, chat: ChatSession, turn_id: str,
                            execution_id: str | None) -> dict:
    """在最终回复边界固化本轮真实实例和截至回复观察的回执，不含请求/结果正文。"""
    instance = db.get(SopInstance, execution_id) if execution_id else None
    snapshot = {'policy_version': 'feedback_source_v1', 'turn_id': turn_id,
                'agent_id': chat.agent_id, 'execution_id': None, 'operations': None}
    if (instance is None or instance.tenant_id != chat.tenant_id
            or instance.session_id != chat.id):
        return snapshot
    snapshot.update(execution_id=instance.id, branch=instance.kind,
                    node_id=instance.current_node_id,
                    skill_id=instance.skill_id, skill_version_id=instance.skill_version_id,
                    skill_version=instance.skill_version,
                    definition_checksum=instance.definition_checksum,
                    execution_status=instance.status,
                    operations=[{'id': op.id, 'status': op.status,
                                 'tool_id': op.operation_name, 'node_execution_id': op.node_execution_id}
                                for op in db.exec(select(SopOperation).where(
                                    SopOperation.tenant_id == instance.tenant_id,
                                    SopOperation.instance_id == instance.id,
                                ).order_by(SopOperation.id)).all()])
    return snapshot


def feedback_source(db: Session, message: Message) -> FeedbackSource:
    """验证原回复持久证据与租户/会话/冻结定义，不从会话当前状态补身份。"""
    metadata = message.metadata_json or {}
    turn_id = str(metadata.get('user_message_id') or metadata.get('turn_id') or '')
    result = FeedbackSource(message_id=message.id, turn_id=turn_id or None)
    turn = db.get(Message, turn_id, populate_existing=True, with_for_update=True) if turn_id else None
    if (turn is None or turn.role != 'user' or turn.tenant_id != message.tenant_id
            or turn.session_id != message.session_id):
        return result
    snapshot = metadata.get('feedback_source')
    if isinstance(snapshot, dict) and snapshot.get('policy_version') == 'feedback_source_v1':
        if snapshot.get('turn_id') != turn_id:
            return result
        execution_id = snapshot.get('execution_id')
        result.agent_id = snapshot.get('agent_id')
    else:
        execution_id = metadata.get('execution_id')
        snapshot = None
    instance = db.get(SopInstance, execution_id, populate_existing=True, with_for_update=True) if isinstance(execution_id, str) else None
    if (instance is None or instance.tenant_id != message.tenant_id
            or instance.session_id != message.session_id):
        return result
    if snapshot is None and instance.source_ref != turn_id:
        return result
    if instance.kind == 'dynamic_task':
        return result.model_copy(update={'status': 'verified', 'branch': 'dynamic_task',
                                         'execution_id': instance.id,
                                         'agent_id': instance.agent_id, 'missing_reason': None})
    version = db.get(SkillVersion, instance.skill_version_id, populate_existing=True, with_for_update=True)
    if (version is None or version.tenant_id != message.tenant_id
            or version.skill_id != instance.skill_id
            or version.version != instance.skill_version
            or version.compiled_definition_checksum != instance.definition_checksum):
        return result
    if snapshot is not None and any(snapshot.get(key) != getattr(instance, key) for key in (
            'skill_id', 'skill_version_id', 'skill_version', 'definition_checksum')):
        return result
    return result.model_copy(update={'status': 'verified', 'branch': 'sop',
                                    'execution_id': instance.id, 'skill_id': instance.skill_id,
                                    'skill_version_id': version.id, 'skill_version': version.version,
                                    'definition_checksum': instance.definition_checksum,
                                    'agent_id': instance.agent_id, 'missing_reason': None})
