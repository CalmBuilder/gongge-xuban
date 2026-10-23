"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : edit_context.py
@CallChain  : 普通Skill/员工派生写入与反馈候选 → 当前actor/Tenant短屏障 → 编辑上下文
@Description: 已知反馈编辑关联不能通过删字段、换scope或迟到旧请求丢失。
"""
import secrets

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.db.models import AgentProfile, SkillEditCandidate, Tenant, User
from app.feedback.materials import digest
from app.security.auth import ensure_current_user_tenant


def editor_slot(actor_id: str) -> str:
    """同tenant/actor只有一个当前技能编辑slot，历史行保持独立身份。"""
    return digest({'actor': actor_id, 'kind': 'skill-editor'})


def lock_skill_actor(db: Session, tenant_id: str, actor: User) -> User:
    """先取得真实账号写屏障；认证之后账号消失也必须拒绝过时授权。"""
    ensure_current_user_tenant(tenant_id, actor)
    try:
        db.exec(update(User).where(User.id == actor.id, User.tenant_id == tenant_id)
                .values(auth_revision=User.auth_revision)
                .execution_options(synchronize_session=False))
        current = db.exec(select(User).where(User.id == actor.id, User.tenant_id == tenant_id)
            .with_for_update().execution_options(populate_existing=True)).first()
    except OperationalError as error:
        db.rollback()
        raise HTTPException(409, 'SKILL_AUTHORITY_WRITE_CONFLICT') from error
    if current is None:
        raise HTTPException(403, 'SKILL_ACTOR_UNAVAILABLE')
    if current.membership_status != 'active':
        raise HTTPException(403, 'SKILL_ACTOR_UNAVAILABLE')
    return current


def skill_write_barrier(db: Session, tenant_id: str) -> None:
    """第一把业务锁前复用Tenant短写屏障，SQLite真实写锁/MySQL当前行锁。"""
    try:
        result = db.exec(update(Tenant).where(Tenant.id == tenant_id)
            .values(updated_at=Tenant.updated_at).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(404, 'Tenant not found')
    except OperationalError as error:
        db.rollback()
        raise HTTPException(409, 'SKILL_PUBLICATION_WRITE_CONFLICT') from error


def current_editor(db: Session, tenant_id: str, actor_id: str) -> SkillEditCandidate | None:
    """当前锁读唯一slot，拒绝MySQL旧RR快照重新看见已关闭或替换的上下文。"""
    return db.exec(select(SkillEditCandidate).where(
        SkillEditCandidate.tenant_id == tenant_id,
        SkillEditCandidate.actor_user_id == actor_id,
        SkillEditCandidate.editor_slot_key == editor_slot(actor_id),
    ).with_for_update().execution_options(populate_existing=True)).first()


def lock_scope_agents(db: Session, tenant_id: str, identifiers: list[str]) -> dict[str, AgentProfile]:
    """按真实ID排序取得涉及的员工锁，后续授权仍沿原manager规则。"""
    if not identifiers:
        return {}
    rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id,
        AgentProfile.id.in_(sorted(set(identifiers)))).order_by(AgentProfile.id)
        .with_for_update().execution_options(populate_existing=True)).all()
    return {row.id: row for row in rows}


def start_skill_write(db: Session, tenant_id: str, actor: User,
                      agent_id: str | None, context_identity: str | None = None) -> User:
    """普通实际头写入口统一先锁actor/Tenant；已受管actor只能用新ordinary身份。"""
    actor = lock_skill_actor(db, tenant_id, actor)
    skill_write_barrier(db, tenant_id)
    agents = lock_scope_agents(db, tenant_id, [agent_id] if agent_id else [])
    agent = agents.get(agent_id)
    scope = agent.id if agent and not agent.is_overall else ''
    editor = current_editor(db, tenant_id, actor.id)
    if editor is None:
        return actor
    context = editor.ordinary_context_json or {}
    if (editor.status not in {'closed', 'published'} or not context_identity
            or not secrets.compare_digest(str(context.get('identity') or ''), context_identity)
            or context.get('scope_agent_id') != scope
            or context.get('actor_auth_revision') != actor.auth_revision):
        raise HTTPException(409, 'SKILL_EDITOR_CONTEXT_REQUIRED')
    profile_id = context.get('scope_profile_id')
    profile = db.get(AgentProfile, profile_id, populate_existing=True,
                     with_for_update=True) if profile_id else None
    if profile_id and (profile is None or profile.tenant_id != tenant_id
            or profile.status != 'active'
            or profile.profile_revision != context.get('scope_profile_revision')):
        raise HTTPException(409, 'SKILL_EDITOR_CONTEXT_INVALID')
    return actor
