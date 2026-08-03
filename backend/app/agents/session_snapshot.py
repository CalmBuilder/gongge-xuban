"""
@Time       : 2026/07/28 21:46
@Author     : zhanglp8181
@File       : session_snapshot.py
@CallChain  : Chat API/Agent Loop/定时任务 → 数字员工版本锚点 → ChatSession
@Description: 生成不含凭据和资源内容的会话能力快照，并统一写入创建时版本与来源。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.db.models import AgentProfile, AgentResourceBinding, ChatSession


def agent_capability_snapshot(db: Session, row: AgentProfile) -> dict[str, object]:
    """生成只包含活动资源标识的快照，排除绑定 metadata、凭据和资源正文。"""

    bindings = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == row.tenant_id,
            AgentResourceBinding.agent_id == row.id,
            AgentResourceBinding.status == "active",
        )
        .order_by(
            AgentResourceBinding.resource_type,
            AgentResourceBinding.resource_id,
        )
    ).all()
    return {
        "agent_id": row.id,
        "profile_revision": row.profile_revision,
        "resources": [
            {
                "resource_type": binding.resource_type,
                "resource_id": binding.resource_id,
                "status": binding.status,
            }
            for binding in bindings
        ],
    }


def anchor_chat_session(
    db: Session,
    chat_session: ChatSession,
    agent: AgentProfile,
    *,
    origin: str,
) -> None:
    """仅在缺失时写入会话创建事实，确保后续员工变更不会改写历史锚点。"""

    chat_session.agent_id = chat_session.agent_id or agent.id
    if chat_session.agent_profile_revision is None:
        chat_session.agent_profile_revision = agent.profile_revision
    if chat_session.capability_snapshot_json is None:
        chat_session.capability_snapshot_json = agent_capability_snapshot(db, agent)
    if chat_session.origin is None:
        chat_session.origin = origin
