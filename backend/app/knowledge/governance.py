"""
@Time       : 2026/07/28 23:10
@Author     : zhanglp8181
@File       : governance.py
@CallChain  : Knowledge management API → knowledge governance service → organization facts/SQLModel
@Description: 校验并持久化知识库责任组织、访问组织根、下载策略和乐观锁修订。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseOrgAccess,
    User,
    utc_now,
)
from app.knowledge.schema import KnowledgeBaseOrgAccessInput
from app.organization.governance import (
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.organization.query import resolve_organization_subtree_ids
from app.organization.units import OrganizationUnitError, get_tenant_organization_unit


class KnowledgeGovernanceError(ValueError):
    """表示知识治理命令不满足租户、组织、范围或并发契约。"""


@dataclass(frozen=True, slots=True)
class KnowledgeGovernanceChange:
    """承载经校验的知识治理更新参数。"""

    responsible_org_unit_id: str | None
    access_scope: str
    download_policy: str
    organization_access: tuple[KnowledgeBaseOrgAccessInput, ...]


def validate_knowledge_governance_change(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
    responsible_org_unit_id: str | None,
    access_scope: str,
    download_policy: str,
    organization_access: list[KnowledgeBaseOrgAccessInput],
) -> KnowledgeGovernanceChange:
    """校验治理权限和组织根，并拒绝范围类型与组织明细不一致的请求。"""

    if access_scope not in {"owner", "organization", "tenant"}:
        raise KnowledgeGovernanceError("INVALID_KNOWLEDGE_ACCESS_SCOPE")
    if download_policy not in {"allowed", "restricted"}:
        raise KnowledgeGovernanceError("INVALID_KNOWLEDGE_DOWNLOAD_POLICY")
    normalized_responsible = responsible_org_unit_id.strip() if responsible_org_unit_id else None
    normalized: dict[str, KnowledgeBaseOrgAccessInput] = {}
    for item in organization_access:
        org_unit_id = item.org_unit_id.strip()
        if not org_unit_id:
            raise KnowledgeGovernanceError("KNOWLEDGE_ORGANIZATION_REQUIRED")
        previous = normalized.get(org_unit_id)
        if previous and previous.include_descendants != item.include_descendants:
            raise KnowledgeGovernanceError("DUPLICATE_KNOWLEDGE_ORGANIZATION")
        normalized[org_unit_id] = KnowledgeBaseOrgAccessInput(
            org_unit_id=org_unit_id,
            include_descendants=item.include_descendants,
        )
    if access_scope == "organization" and not normalized:
        raise KnowledgeGovernanceError("KNOWLEDGE_ORGANIZATION_SCOPE_REQUIRED")
    if access_scope != "organization" and normalized:
        raise KnowledgeGovernanceError("KNOWLEDGE_ORGANIZATION_SCOPE_NOT_ALLOWED")

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="knowledge.manage",
    )
    if access_scope == "tenant" and not _has_tenant_knowledge_management(
        db,
        tenant_id=tenant_id,
        user_id=current_user.id,
    ):
        raise KnowledgeGovernanceError("TENANT_KNOWLEDGE_SCOPE_PERMISSION_REQUIRED")

    target_ids = list(normalized)
    if normalized_responsible:
        target_ids.append(normalized_responsible)
    for org_unit_id in sorted(set(target_ids)):
        try:
            unit = get_tenant_organization_unit(db, tenant_id, org_unit_id)
        except OrganizationUnitError as error:
            raise KnowledgeGovernanceError("KNOWLEDGE_ORGANIZATION_NOT_FOUND") from error
        if unit.status != "active":
            raise KnowledgeGovernanceError("KNOWLEDGE_ORGANIZATION_INACTIVE")
        ensure_governance_permission(
            db,
            tenant_id=tenant_id,
            current_user=current_user,
            permission_code="knowledge.manage",
            target_org_unit_id=org_unit_id,
        )
        item = normalized.get(org_unit_id)
        if item is not None:
            resolve_organization_subtree_ids(
                db,
                tenant_id=tenant_id,
                root_org_unit_id=org_unit_id,
                include_descendants=item.include_descendants,
            )
    return KnowledgeGovernanceChange(
        responsible_org_unit_id=normalized_responsible,
        access_scope=access_scope,
        download_policy=download_policy,
        organization_access=tuple(normalized[key] for key in sorted(normalized)),
    )


def ensure_knowledge_governance_manager(
    db: Session,
    *,
    knowledge_base: KnowledgeBase,
    current_user: User,
) -> None:
    """要求操作者覆盖知识库当前全部组织范围；无组织锚点时仅租户级授权可治理。"""

    rows = db.exec(
        select(KnowledgeBaseOrgAccess).where(
            KnowledgeBaseOrgAccess.tenant_id == knowledge_base.tenant_id,
            KnowledgeBaseOrgAccess.knowledge_base_id == knowledge_base.id,
            KnowledgeBaseOrgAccess.status == "active",
        )
    ).all()
    target_ids = {row.org_unit_id for row in rows}
    if knowledge_base.responsible_org_unit_id:
        target_ids.add(knowledge_base.responsible_org_unit_id)
    if not target_ids:
        if not _has_tenant_knowledge_management(
            db,
            tenant_id=knowledge_base.tenant_id,
            user_id=current_user.id,
        ):
            raise KnowledgeGovernanceError("TENANT_KNOWLEDGE_SCOPE_PERMISSION_REQUIRED")
        return
    for org_unit_id in sorted(target_ids):
        ensure_governance_permission(
            db,
            tenant_id=knowledge_base.tenant_id,
            current_user=current_user,
            permission_code="knowledge.manage",
            target_org_unit_id=org_unit_id,
        )


def update_knowledge_base_governance(
    db: Session,
    *,
    knowledge_base: KnowledgeBase,
    expected_revision: int,
    change: KnowledgeGovernanceChange,
) -> KnowledgeBase:
    """按 revision 更新知识治理事实，并保留组织关系历史状态而非物理删除。"""

    if knowledge_base.revision != expected_revision:
        raise KnowledgeGovernanceError("KNOWLEDGE_BASE_REVISION_CONFLICT")
    existing = db.exec(
        select(KnowledgeBaseOrgAccess).where(
            KnowledgeBaseOrgAccess.tenant_id == knowledge_base.tenant_id,
            KnowledgeBaseOrgAccess.knowledge_base_id == knowledge_base.id,
        )
    ).all()
    by_org = {row.org_unit_id: row for row in existing}
    requested = {item.org_unit_id: item for item in change.organization_access}
    now = utc_now()
    for row in existing:
        item = requested.get(row.org_unit_id)
        row.status = "active" if item else "inactive"
        if item:
            row.include_descendants = item.include_descendants
        row.updated_at = now
        db.add(row)
    for org_unit_id, item in requested.items():
        if org_unit_id in by_org:
            continue
        db.add(
            KnowledgeBaseOrgAccess(
                tenant_id=knowledge_base.tenant_id,
                knowledge_base_id=knowledge_base.id,
                org_unit_id=org_unit_id,
                include_descendants=item.include_descendants,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    knowledge_base.responsible_org_unit_id = change.responsible_org_unit_id
    knowledge_base.access_scope = change.access_scope
    knowledge_base.download_policy = change.download_policy
    knowledge_base.revision += 1
    knowledge_base.updated_at = now
    db.add(knowledge_base)
    db.flush()
    return knowledge_base


def active_knowledge_org_access(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_ids: list[str],
) -> dict[str, list[KnowledgeBaseOrgAccess]]:
    """批量返回知识库的活动组织根，避免列表逐行查询。"""

    if not knowledge_base_ids:
        return {}
    rows = db.exec(
        select(KnowledgeBaseOrgAccess)
        .where(
            KnowledgeBaseOrgAccess.tenant_id == tenant_id,
            KnowledgeBaseOrgAccess.knowledge_base_id.in_(knowledge_base_ids),
            KnowledgeBaseOrgAccess.status == "active",
        )
        .order_by(
            KnowledgeBaseOrgAccess.knowledge_base_id,
            KnowledgeBaseOrgAccess.org_unit_id,
        )
    ).all()
    result: dict[str, list[KnowledgeBaseOrgAccess]] = {}
    for row in rows:
        result.setdefault(row.knowledge_base_id, []).append(row)
    return result


def _has_tenant_knowledge_management(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> bool:
    """判断知识管理授权中是否至少存在一个租户级 grant。"""

    return any(
        grant.permission_code == "knowledge.manage"
        and grant.scope.organization_unit_ids is None
        for grant in resolve_permission_grants(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    )
