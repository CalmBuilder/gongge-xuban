"""
@Time       : 2026/07/29 01:45
@Author     : zhanglp8181
@File       : management_audit.py
@CallChain  : 审计管理页 → FastAPI → audit.read 治理范围 → ManagementAuditLog
@Description: 提供按租户和真实组织授权过滤的管理审计分页与详情只读 API。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.audit.service import query_management_audits
from app.db import get_session
from app.db.models import ManagementAuditLog, User
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/management-audit", tags=["management-audit"])


class ManagementAuditRead(BaseModel):
    """返回不含密钥和业务正文的管理审计事实。"""

    id: str
    tenant_id: str
    actor_user_id: str | None
    actor_type: str
    actor_display_name: str | None
    action: str
    action_kind: str
    outcome: str
    resource_type: str
    resource_id: str | None
    target_org_unit_id: str | None
    permission_code: str | None
    permission_source: str | None
    request_id: str | None
    correlation_id: str | None
    before: dict[str, Any]
    after: dict[str, Any]
    detail: dict[str, Any]
    created_at: datetime


class ManagementAuditPage(BaseModel):
    """返回审计总数、页码、页大小和当前页记录。"""

    items: list[ManagementAuditRead]
    total: int
    page: int
    page_size: int


@router.get("/logs", response_model=ManagementAuditPage)
def list_management_audit_logs(
    tenant_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor_user_id: str | None = Query(None),
    action: str | None = Query(None),
    action_kind: Literal["create", "update", "delete", "read", "execute"] | None = Query(None),
    outcome: Literal["success", "denied", "failure"] | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ManagementAuditPage:
    """按当前审计员的租户或组织子树范围查询脱敏管理审计。"""

    allowed_organization_ids = _audit_scope(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
    )
    rows, total = query_management_audits(
        db,
        tenant_id=tenant_id,
        allowed_organization_ids=allowed_organization_ids,
        page=page,
        page_size=page_size,
        actor_user_id=actor_user_id,
        action=action,
        action_kind=action_kind,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        created_after=created_after,
        created_before=created_before,
    )
    return ManagementAuditPage(
        items=[_audit_read(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/logs/{audit_id}", response_model=ManagementAuditRead)
def get_management_audit_log(
    audit_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ManagementAuditRead:
    """读取单条脱敏审计，并对直接 URL 重新执行组织范围判断。"""

    allowed_organization_ids = _audit_scope(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
    )
    row = db.get(ManagementAuditLog, audit_id)
    if (
        row is None
        or row.tenant_id != tenant_id
        or (
            allowed_organization_ids is not None
            and row.target_org_unit_id not in allowed_organization_ids
        )
    ):
        raise HTTPException(status_code=404, detail="Management audit log not found")
    return _audit_read(row)


def _audit_scope(
    db: Session,
    *,
    tenant_id: str,
    current_user: User,
) -> frozenset[str] | None:
    """校验 audit.read 并返回 None 租户全范围或唯一子树展开后的组织集合。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="audit.read",
    )
    grants = resolve_permission_grants(
        db,
        tenant_id=tenant_id,
        user_id=current_user.id,
    )
    return authorized_organization_ids(grants, permission_code="audit.read")


def _audit_read(row: ManagementAuditLog) -> ManagementAuditRead:
    """把数据库 JSON 字段映射为稳定且不暴露内部列名的只读契约。"""

    return ManagementAuditRead(
        id=row.id,
        tenant_id=row.tenant_id,
        actor_user_id=row.actor_user_id,
        actor_type=row.actor_type,
        actor_display_name=row.actor_display_name,
        action=row.action,
        action_kind=row.action_kind,
        outcome=row.outcome,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        target_org_unit_id=row.target_org_unit_id,
        permission_code=row.permission_code,
        permission_source=row.permission_source,
        request_id=row.request_id,
        correlation_id=row.correlation_id,
        before=dict(row.before_json or {}),
        after=dict(row.after_json or {}),
        detail=dict(row.detail_json or {}),
        created_at=row.created_at,
    )
