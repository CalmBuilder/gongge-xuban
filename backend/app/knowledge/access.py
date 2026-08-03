"""
@Time       : 2026/07/29 00:20
@Author     : zhanglp8181
@File       : access.py
@CallChain  : Knowledge API/Agent Loop → knowledge access resolver → user/org/agent/knowledge facts
@Description: 统一计算活动成员、数字员工绑定、知识治理范围和请求限定的交集及拒绝原因。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.agents.branching import (
    ensure_knowledge_base_version,
    get_agent,
    visible_knowledge_base_ids,
    visible_knowledge_base_versions,
)
from app.db.models import (
    EmployeeProfile,
    KnowledgeBase,
    KnowledgeBaseOrgAccess,
    KnowledgeBaseVersion,
    MemberOrgAssignment,
    OrganizationUnit,
    User,
)
from app.organization.query import (
    current_assignment_predicates,
    resolve_organization_subtree_ids,
)
from app.organization.units import OrganizationUnitError


@dataclass(frozen=True, slots=True)
class KnowledgeAccessDecision:
    """描述单个知识库经过成员、Agent、请求与下载策略求交后的确定结果。"""

    knowledge_base_id: str
    allowed: bool
    member_scope_reason: str
    agent_allowed: bool
    requested: bool
    download_allowed: bool
    denial_reason: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeAccessResolution:
    """承载一次知识访问解析的允许集合、组织事实和逐库解释。"""

    tenant_id: str
    user_id: str
    agent_id: str | None
    member_org_unit_ids: tuple[str, ...]
    allowed_knowledge_base_ids: tuple[str, ...]
    decisions: tuple[KnowledgeAccessDecision, ...]

    def decision_for(self, knowledge_base_id: str) -> KnowledgeAccessDecision | None:
        """按知识库 ID 返回解释，未知资源保持无结果以避免伪造存在性。"""

        return next(
            (
                decision
                for decision in self.decisions
                if decision.knowledge_base_id == knowledge_base_id
            ),
            None,
        )


def resolve_knowledge_access(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    agent_id: str | None = None,
    requested_knowledge_base_ids: list[str] | tuple[str, ...] | None = None,
    require_download: bool = False,
    include_inactive: bool = False,
) -> KnowledgeAccessResolution:
    """按成员范围、Agent 绑定和请求限定求交；任一事实异常或缺失均默认拒绝。"""

    requested_ids = _normalized_ids(requested_knowledge_base_ids)
    requested_set = set(requested_ids) if requested_knowledge_base_ids is not None else None
    member_active = (
        current_user.tenant_id == tenant_id
        and current_user.membership_status == "active"
    )
    member_org_ids = (
        _active_member_org_unit_ids(db, tenant_id=tenant_id, user_id=current_user.id)
        if member_active
        else ()
    )
    member_org_set = set(member_org_ids)
    rows = db.exec(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status != "deleted"
            if include_inactive
            else KnowledgeBase.status == "active",
        )
        .order_by(KnowledgeBase.id)
    ).all()
    org_access = _active_org_access_by_knowledge_base(
        db,
        tenant_id=tenant_id,
        knowledge_base_ids=[row.id for row in rows],
    )
    agent_allowed_ids = _agent_allowed_knowledge_base_ids(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        include_inactive=include_inactive,
    )
    decisions: list[KnowledgeAccessDecision] = []
    for row in rows:
        requested = requested_set is None or row.id in requested_set
        member_allowed, member_reason = _member_scope_decision(
            db,
            row=row,
            current_user=current_user,
            member_active=member_active,
            member_org_unit_ids=member_org_set,
            org_access=org_access.get(row.id, ()),
        )
        agent_allowed = agent_allowed_ids is None or row.id in agent_allowed_ids
        download_allowed = row.download_policy == "allowed"
        denial_reason = _denial_reason(
            member_allowed=member_allowed,
            member_reason=member_reason,
            agent_allowed=agent_allowed,
            requested=requested,
            require_download=require_download,
            download_allowed=download_allowed,
        )
        decisions.append(
            KnowledgeAccessDecision(
                knowledge_base_id=row.id,
                allowed=denial_reason is None,
                member_scope_reason=member_reason,
                agent_allowed=agent_allowed,
                requested=requested,
                download_allowed=download_allowed,
                denial_reason=denial_reason,
            )
        )
    return KnowledgeAccessResolution(
        tenant_id=tenant_id,
        user_id=current_user.id,
        agent_id=agent_id,
        member_org_unit_ids=member_org_ids,
        allowed_knowledge_base_ids=tuple(
            decision.knowledge_base_id for decision in decisions if decision.allowed
        ),
        decisions=tuple(decisions),
    )


def ensure_knowledge_content_access(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    knowledge_base_id: str,
    agent_id: str | None = None,
    require_download: bool = False,
) -> KnowledgeAccessDecision:
    """返回单库允许解释；不存在或未命中任一交集时统一抛出不可见错误。"""

    resolution = resolve_knowledge_access(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        agent_id=agent_id,
        requested_knowledge_base_ids=[knowledge_base_id],
        require_download=require_download,
    )
    decision = resolution.decision_for(knowledge_base_id)
    if decision is None or not decision.allowed:
        raise KnowledgeAccessDenied(
            knowledge_base_id=knowledge_base_id,
            reason=decision.denial_reason if decision else "knowledge_not_found",
        )
    return decision


class KnowledgeAccessDenied(PermissionError):
    """表示知识资源不存在或没有同时命中成员、Agent、请求和下载范围。"""

    def __init__(self, *, knowledge_base_id: str, reason: str | None):
        """保留稳定拒绝代码供 API 防枚举映射和后续审计使用。"""

        self.knowledge_base_id = knowledge_base_id
        self.reason = reason or "knowledge_access_denied"
        super().__init__(self.reason)


def accessible_knowledge_base_versions(
    db: Session,
    *,
    resolution: KnowledgeAccessResolution,
) -> dict[str, KnowledgeBaseVersion]:
    """把解析允许集映射到 Agent 当前分支版本；无 Agent 时使用知识库当前版本。"""

    allowed_ids = set(resolution.allowed_knowledge_base_ids)
    if not allowed_ids:
        return {}
    if resolution.agent_id:
        versions = visible_knowledge_base_versions(
            db,
            resolution.tenant_id,
            resolution.agent_id,
        )
        return {
            knowledge_base_id: version
            for knowledge_base_id, version in versions.items()
            if knowledge_base_id in allowed_ids
        }
    rows = db.exec(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == resolution.tenant_id,
            KnowledgeBase.id.in_(allowed_ids),
            KnowledgeBase.status == "active",
        )
    ).all()
    return {
        row.id: ensure_knowledge_base_version(db, row)
        for row in rows
    }


def _active_member_org_unit_ids(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> tuple[str, ...]:
    """返回活动员工档案当前有效且组织节点仍活动的全部组织归属。"""

    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == user_id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if profile is None:
        return ()
    rows = db.exec(
        select(MemberOrgAssignment.org_unit_id)
        .join(
            OrganizationUnit,
            OrganizationUnit.id == MemberOrgAssignment.org_unit_id,
        )
        .where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.employee_profile_id == profile.id,
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.status == "active",
            *current_assignment_predicates(),
        )
        .order_by(MemberOrgAssignment.org_unit_id)
    ).all()
    return tuple(dict.fromkeys(rows))


def _active_org_access_by_knowledge_base(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
) -> dict[str, tuple[KnowledgeBaseOrgAccess, ...]]:
    """批量读取活动组织根，避免逐知识库查询访问关系。"""

    if not knowledge_base_ids:
        return {}
    rows = db.exec(
        select(KnowledgeBaseOrgAccess)
        .join(
            OrganizationUnit,
            OrganizationUnit.id == KnowledgeBaseOrgAccess.org_unit_id,
        )
        .where(
            KnowledgeBaseOrgAccess.tenant_id == tenant_id,
            KnowledgeBaseOrgAccess.knowledge_base_id.in_(knowledge_base_ids),
            KnowledgeBaseOrgAccess.status == "active",
            OrganizationUnit.tenant_id == tenant_id,
            OrganizationUnit.status == "active",
        )
        .order_by(
            KnowledgeBaseOrgAccess.knowledge_base_id,
            KnowledgeBaseOrgAccess.org_unit_id,
        )
    ).all()
    grouped: dict[str, list[KnowledgeBaseOrgAccess]] = {}
    for row in rows:
        grouped.setdefault(row.knowledge_base_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _agent_allowed_knowledge_base_ids(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str | None,
    include_inactive: bool,
) -> set[str] | None:
    """返回数字员工既有分支/绑定允许集；未指定 Agent 时不增加约束。"""

    if agent_id is None:
        return None
    agent = get_agent(db, tenant_id, agent_id)
    if agent is None or agent.status != "active":
        return set()
    return set(
        visible_knowledge_base_ids(
            db,
            tenant_id,
            agent.id,
            include_inactive=include_inactive,
        )
    )


def _member_scope_decision(
    db: Session,
    *,
    row: KnowledgeBase,
    current_user: User,
    member_active: bool,
    member_org_unit_ids: set[str],
    org_access: tuple[KnowledgeBaseOrgAccess, ...],
) -> tuple[bool, str]:
    """解释成员是否命中 owner、tenant 或组织范围，不把治理权限当正文权限。"""

    if not member_active:
        return False, "member_inactive"
    if row.access_scope == "owner":
        return row.owner_user_id == current_user.id, (
            "owner" if row.owner_user_id == current_user.id else "owner_mismatch"
        )
    if row.access_scope == "tenant":
        return True, "tenant"
    if row.access_scope != "organization":
        return False, "invalid_access_scope"
    for access in org_access:
        try:
            allowed_org_ids = resolve_organization_subtree_ids(
                db,
                tenant_id=row.tenant_id,
                root_org_unit_id=access.org_unit_id,
                include_descendants=access.include_descendants,
            )
        except OrganizationUnitError:
            continue
        if member_org_unit_ids.intersection(allowed_org_ids):
            return True, f"organization:{access.org_unit_id}"
    return False, "organization_mismatch"


def _denial_reason(
    *,
    member_allowed: bool,
    member_reason: str,
    agent_allowed: bool,
    requested: bool,
    require_download: bool,
    download_allowed: bool,
) -> str | None:
    """按固定优先级返回拒绝原因，确保解释和测试不受查询顺序影响。"""

    if not requested:
        return "outside_request_scope"
    if not member_allowed:
        return member_reason
    if not agent_allowed:
        return "agent_unbound"
    if require_download and not download_allowed:
        return "download_restricted"
    return None


def _normalized_ids(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """清理请求 ID 并保持首次出现顺序，避免空字符串扩大查询。"""

    if values is None:
        return ()
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
