"""
@Time       : 2026/08/10 23:35
@Author     : zhanglp8181
@File       : dynamic_task_operations.py
@CallChain  : 运维管理端 → FastAPI → DynamicTaskOperationsService → 统一 Runtime 权威表
@Description: 向租户全域审计管理员提供脱敏动态任务运行快照和阈值告警状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User
from app.dynamic_tasks.operations import (
    DynamicTaskAlertThresholds,
    DynamicTaskOperationsService,
)
from app.dynamic_tasks.quotas import quota_limits_from_settings
from app.organization.governance import (
    authorized_organization_ids,
    ensure_governance_permission,
    resolve_permission_grants,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/dynamic-task-operations", tags=["dynamic-task-operations"])


class OperationalAlertRead(BaseModel):
    """返回单项阈值是否配置、当前值和触发状态。"""

    code: str
    severity: Literal["warning", "critical"]
    current: int
    threshold: int | None
    enabled: bool
    triggered: bool


class DynamicTaskOperationalSnapshotRead(BaseModel):
    """返回单租户脱敏聚合，不包含 prompt、工具参数、凭据或业务结果。"""

    tenant_id: str
    observed_at: datetime
    thresholds_configured: bool
    quota_limits_configured: bool
    runtime_capacity_limits_configured: bool
    runtime_capacity_available: bool
    base_execution_available: bool
    base_execution_reason: str
    high_risk_external_write_available: bool
    high_risk_external_write_reason: str
    high_risk_destructive_available: bool
    high_risk_destructive_reason: str
    quota_limits: dict[str, int]
    quota_leases: dict[str, int]
    executions: dict[str, int]
    signals: dict[str, int]
    operations: dict[str, int]
    publications: dict[str, int]
    attentions: dict[str, int]
    oldest_waiting_age_seconds: int
    alerts: list[OperationalAlertRead]


@router.get("/snapshot", response_model=DynamicTaskOperationalSnapshotRead)
def get_dynamic_task_operational_snapshot(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> DynamicTaskOperationalSnapshotRead:
    """仅允许拥有租户全域 audit.read 的人员读取动态任务聚合运行事实。"""

    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="audit.read",
    )
    grants = resolve_permission_grants(db, tenant_id=tenant_id, user_id=current_user.id)
    if authorized_organization_ids(grants, permission_code="audit.read") is not None:
        raise HTTPException(status_code=403, detail="TENANT_WIDE_AUDIT_SCOPE_REQUIRED")
    settings = get_settings()
    snapshot = DynamicTaskOperationsService(db).snapshot(
        tenant_id=tenant_id,
        thresholds=DynamicTaskAlertThresholds(
            signal_backlog=settings.dynamic_task_alert_signal_backlog_threshold,
            dead_letters=settings.dynamic_task_alert_dead_letter_threshold,
            unknown_operations=settings.dynamic_task_alert_unknown_operation_threshold,
            publication_backlog=settings.dynamic_task_alert_publication_backlog_threshold,
            waiting_age_seconds=settings.dynamic_task_alert_waiting_age_seconds,
        ),
        quota_limits=quota_limits_from_settings(settings),
    )
    return DynamicTaskOperationalSnapshotRead.model_validate(snapshot)
