"""
@Time       : 2026/08/11 22:45
@Author     : zhanglp8181
@File       : standing_approval_rules.py
@CallChain  : 管理端 → FastAPI → Standing Approval service → SQLModel/审计
@Description: 提供租户隔离的长期批准创建、查询和 CAS 撤销接口，不提供原地扩权更新。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import StandingApprovalRule, User
from app.dynamic_tasks.standing_approvals import (
    STANDING_APPROVAL_PERMISSION_CODE,
    StandingApprovalError,
    create_standing_approval_rule,
    list_standing_approval_candidates,
    revoke_standing_approval_rule,
)
from app.organization.governance import ensure_governance_permission
from app.security.auth import get_current_user


router = APIRouter(
    prefix="/api/standing-approval-rules",
    tags=["standing-approval-rules"],
)


class StandingApprovalCreateRequest(BaseModel):
    """接收幂等命令和资源身份，目标与工具快照始终由服务端规范化。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    source_schedule_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    thread_binding_id: str = Field(min_length=1, max_length=128)
    tool_action: Literal["wecom.message_send"]
    argument_constraints: dict[str, object]
    valid_from: datetime
    valid_to: datetime


class StandingApprovalRevokeRequest(BaseModel):
    """以 command id 与 revision CAS 撤销既有规则。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class StandingApprovalRuleRead(BaseModel):
    """返回管理面需要的规则身份、约束、有效期和撤销事实。"""

    id: str
    tenant_id: str
    agent_id: str
    source_schedule_id: str
    source_schedule_checksum: str
    profile_id: str
    binding_id: str
    tool_id: str
    tool_snapshot_checksum: str
    risk_class: str
    target_type: str
    canonical_target: str
    target_hash: str
    argument_constraints: dict[str, object]
    valid_from: datetime
    valid_to: datetime
    status: str
    revision: int
    created_by_user_id: str
    revoked_by_user_id: str | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StandingApprovalCandidateRead(BaseModel):
    """返回可授权的内部会话标签和校验摘要，不暴露外部接收者原始标识。"""

    thread_binding_id: str
    profile_id: str
    profile_display_name: str
    target_label: str
    tool_snapshot_checksum: str
    target_hash: str


@router.post("", response_model=StandingApprovalRuleRead)
def create_rule(
    request: StandingApprovalCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> StandingApprovalRuleRead:
    """创建精确规则；相同 command 重放返回原结果，不允许模型或 Agent 调用。"""

    try:
        rule = create_standing_approval_rule(
            db,
            tenant_id=request.tenant_id,
            command_id=request.command_id,
            current_user=current_user,
            agent_id=request.agent_id,
            source_schedule_id=request.source_schedule_id,
            profile_id=request.profile_id,
            thread_binding_id=request.thread_binding_id,
            tool_action=request.tool_action,
            argument_constraints=request.argument_constraints,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
        )
    except StandingApprovalError as exc:
        db.rollback()
        raise _rule_error(exc) from exc
    return _rule_read(rule)


@router.get("", response_model=list[StandingApprovalRuleRead])
def list_rules(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    source_schedule_id: str | None = Query(None),
    status: Literal["active", "revoked"] | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[StandingApprovalRuleRead]:
    """只向拥有专门治理权限的当前租户真人列出脱敏规则。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code=STANDING_APPROVAL_PERMISSION_CODE,
    )
    conditions = [StandingApprovalRule.tenant_id == tenant_id]
    if agent_id:
        conditions.append(StandingApprovalRule.agent_id == agent_id)
    if source_schedule_id:
        conditions.append(StandingApprovalRule.source_schedule_id == source_schedule_id)
    if status:
        conditions.append(StandingApprovalRule.status == status)
    rows = db.exec(
        select(StandingApprovalRule)
        .where(*conditions)
        .order_by(StandingApprovalRule.created_at.desc(), StandingApprovalRule.id.desc())
    ).all()
    return [_rule_read(rule) for rule in rows]


@router.get("/candidates", response_model=list[StandingApprovalCandidateRead])
def list_candidates(
    tenant_id: str = Query(...),
    source_schedule_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[StandingApprovalCandidateRead]:
    """列出当前用户可为指定调度选择的精确企业微信会话。"""

    try:
        rows = list_standing_approval_candidates(
            db,
            tenant_id=tenant_id,
            source_schedule_id=source_schedule_id,
            current_user=current_user,
        )
    except StandingApprovalError as exc:
        raise _rule_error(exc) from exc
    return [
        StandingApprovalCandidateRead(
            thread_binding_id=row.thread_binding_id,
            profile_id=row.profile_id,
            profile_display_name=row.profile_display_name,
            target_label=row.target_label,
            tool_snapshot_checksum=row.tool_snapshot_checksum,
            target_hash=row.target_hash,
        )
        for row in rows
    ]


@router.post("/{rule_id}/revoke", response_model=StandingApprovalRuleRead)
def revoke_rule(
    rule_id: str,
    request: StandingApprovalRevokeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> StandingApprovalRuleRead:
    """撤销活动规则；规则一旦撤销只能新建，不能 PATCH 恢复或扩大范围。"""

    try:
        rule = revoke_standing_approval_rule(
            db,
            tenant_id=request.tenant_id,
            rule_id=rule_id,
            command_id=request.command_id,
            expected_revision=request.expected_revision,
            current_user=current_user,
        )
    except StandingApprovalError as exc:
        db.rollback()
        raise _rule_error(exc) from exc
    return _rule_read(rule)


def _rule_read(rule: StandingApprovalRule) -> StandingApprovalRuleRead:
    """把数据库规则投影为不含外部接收者密文和凭据的管理响应。"""

    return StandingApprovalRuleRead(
        id=rule.id,
        tenant_id=rule.tenant_id,
        agent_id=rule.agent_id,
        source_schedule_id=rule.source_schedule_id,
        source_schedule_checksum=rule.source_schedule_checksum,
        profile_id=rule.profile_id,
        binding_id=rule.binding_id,
        tool_id=rule.tool_id,
        tool_snapshot_checksum=rule.tool_snapshot_checksum,
        risk_class=rule.risk_class,
        target_type=rule.target_type,
        canonical_target=rule.canonical_target,
        target_hash=rule.target_hash,
        argument_constraints=dict(rule.argument_constraints_json or {}),
        valid_from=rule.valid_from,
        valid_to=rule.valid_to,
        status=rule.status,
        revision=rule.revision,
        created_by_user_id=rule.created_by_user_id,
        revoked_by_user_id=rule.revoked_by_user_id,
        revoked_at=rule.revoked_at,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _rule_error(error: StandingApprovalError) -> HTTPException:
    """将稳定领域错误映射为不泄露跨租户资源存在性的 HTTP 响应。"""

    if error.code in {
        "STANDING_APPROVAL_MANAGER_DENIED",
        "STANDING_APPROVAL_RESOURCE_SCOPE_DENIED",
    }:
        status_code = 403
    elif error.code == "STANDING_APPROVAL_NOT_FOUND":
        status_code = 404
    elif error.code in {
        "STANDING_APPROVAL_REVISION_CONFLICT",
        "STANDING_APPROVAL_COMMAND_CONFLICT",
        "STANDING_APPROVAL_ACTIVE_DUPLICATE",
        "STANDING_APPROVAL_NOT_ACTIVE",
    }:
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail={"code": error.code})
