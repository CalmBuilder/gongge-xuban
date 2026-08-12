"""
@Time       : 2026/08/10 16:25
@Author     : zhanglp8181
@File       : permissions.py
@CallChain  : API/Connector Runtime → tenant/Agent permission predicates → allow or reject
@Description: 集中维护租户管理、Agent 治理和跨入口聊天使用权限。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlmodel import Session, select

from app.agents.identity import agent_is_published, agent_owner_user_id
from app.db import get_session
from app.db.models import AgentProfile, AgentUsage, PublicationRelease, User
from app.security.auth import ensure_current_user_tenant, get_current_user

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
USER_ROLES = {ADMIN_ROLE, MEMBER_ROLE}


def is_admin_user(current_user: User) -> bool:
    return current_user.role == ADMIN_ROLE


def ensure_tenant_admin(tenant_id: str, current_user: User) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage tenant settings")
    return current_user


def require_tenant_admin(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    return ensure_tenant_admin(tenant_id, current_user)


def require_agent_scope_viewer(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return current_user
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        is_admin_user(current_user)
        or row.is_overall
        or agent_owned_by_user(row, current_user)
        or agent_is_published(row)
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Cannot access this staff")


def ensure_open_gallery_admin(tenant_id: str, current_user: User) -> None:
    ensure_tenant_admin(tenant_id, current_user)


def ensure_agent_scope_manager(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile | None:
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return None
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if is_admin_user(current_user):
        return row
    if row.is_overall:
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if agent_owned_by_user(row, current_user):
        return row
    raise HTTPException(status_code=403, detail="Only the creator or administrator can manage this staff")


def agent_owned_by_user(row: AgentProfile, user: User) -> bool:
    """按正式 owner 用户 ID 判断管理责任，兼容尚未回填的历史 metadata。"""

    return agent_owner_user_id(row) == user.id


def can_use_agent_in_chat(
    db: Session,
    row: AgentProfile,
    user: User,
) -> bool:
    """统一判断用户能否在交互入口使用 Agent，防止连接器绕过聊天使用关系。"""

    if row.tenant_id != user.tenant_id or row.status != "active" or row.is_overall:
        return False
    adopted_release_id = (row.metadata_json or {}).get("adopted_release_id")
    if isinstance(adopted_release_id, str):
        release = db.get(PublicationRelease, adopted_release_id)
        if (
            release is None
            or release.tenant_id != row.tenant_id
            or release.resource_type != "agent"
            or release.status == "security_revoked"
        ):
            return False
    if agent_owned_by_user(row, user):
        return True
    if not agent_is_published(row):
        return False
    usage = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == row.tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id == row.id,
        )
    ).first()
    return usage is not None
