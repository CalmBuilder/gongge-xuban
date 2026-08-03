"""
@Time       : 2026/07/29 09:55
@Author     : zhanglp8181
@File       : sop_migrations.py
@CallChain  : 企业治理前端 → SOP 迁移/依赖覆盖 API → 只读盘点服务
@Description: 以 authorization.read 和租户边界保护 SOP 迁移预检及组织执行覆盖报告。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import User
from app.organization.governance import ensure_governance_permission
from app.security.auth import get_current_user
from app.sop_runtime.migration_inventory import (
    SopDependencyCoverageReport,
    SopMigrationInventory,
    build_sop_dependency_coverage,
    build_sop_migration_inventory,
)


router = APIRouter(
    prefix="/api/sop-migrations",
    tags=["enterprise:sop-migrations"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/preview", response_model=SopMigrationInventory)
def get_sop_migration_preview(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> SopMigrationInventory:
    """返回当前发布 SOP 的只读迁移分类，不创建版本或改写运行实例。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="authorization.read",
    )
    return build_sop_migration_inventory(db, tenant_id)


@router.get("/coverage", response_model=SopDependencyCoverageReport)
def get_sop_dependency_coverage(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> SopDependencyCoverageReport:
    """返回全部发布 SOP 的真人候选和数字员工完整执行路径，不修改业务事实。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="authorization.read",
    )
    return build_sop_dependency_coverage(db, tenant_id)
