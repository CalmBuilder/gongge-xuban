"""
@Time       : 2026/08/29 18:10
@Author     : zhanglp8181
@File       : organizationization.py
@CallChain  : Agent 组织化向导 → 原子配置服务 → AgentProfile/AgentRoleBinding/命令回执/审计
@Description: 在同一事务内配置责任组织、业务角色和监督者，并用版本与关系 checksum 防止覆盖并发变更。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json

from sqlmodel import Session, select

from app.agents.identity import agent_organization_relationship_checksum
from app.agents.schema import AgentOrganizationizationRequest
from app.audit.service import append_user_management_audit
from app.db.models import (
    AgentOrganizationizationCommand,
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    OrganizationUnit,
    User,
    utc_now,
)
from app.organization.governance import ensure_governance_permission, validate_role_assignment_scope


class OrganizationizationError(RuntimeError):
    """表示组织化原子配置的权限、版本或关系校验失败。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存稳定错误码和 HTTP 建议状态。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OrganizationizationApplyResult:
    """承载组织化命令提交后的最小可重放结果。"""

    command_id: str
    result_status: str
    agent_id: str
    profile_revision: int
    relationship_checksum: str
    active_role_binding_id: str


def apply_agent_organizationization(
    db: Session,
    *,
    agent_id: str,
    request: AgentOrganizationizationRequest,
    actor: User,
) -> OrganizationizationApplyResult:
    """在一个事务中写入组织化关系，保证重复命令、旧版本和部分失败可控。"""

    if actor.tenant_id != request.tenant_id:
        raise OrganizationizationError("TENANT_MISMATCH", "组织化请求租户不匹配", 403)
    normalized_org_id = request.responsible_org_unit_id.strip()
    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=actor,
        permission_code="agent.manage",
        target_org_unit_id=normalized_org_id,
    )
    request_checksum = _request_checksum(agent_id, request)
    previous = db.exec(
        select(AgentOrganizationizationCommand).where(
            AgentOrganizationizationCommand.tenant_id == request.tenant_id,
            AgentOrganizationizationCommand.agent_id == agent_id,
            AgentOrganizationizationCommand.command_id == request.command_id,
        )
    ).first()
    if previous is not None:
        if previous.request_checksum != request_checksum:
            raise OrganizationizationError(
                "ORGANIZATIONIZATION_IDEMPOTENCY_CONFLICT",
                "command_id 已用于另一份组织化配置",
            )
        if previous.status != "committed":
            raise OrganizationizationError(
                previous.error_code or "ORGANIZATIONIZATION_FAILED",
                "组织化命令上一次执行失败，请使用新的 command_id 重试",
            )
        return _result_from_command(previous)

    agent = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.id == agent_id,
            AgentProfile.tenant_id == request.tenant_id,
        )
        .with_for_update()
    ).first()
    if agent is None or agent.is_overall:
        raise OrganizationizationError("AGENT_NOT_FOUND", "组织化目标数字员工不存在", 404)
    if agent.status != "active":
        raise OrganizationizationError("AGENT_NOT_ACTIVE", "只有活动 Agent 才能组织化")
    if agent.profile_revision != request.expected_profile_revision:
        raise OrganizationizationError("AGENT_PROFILE_STALE", "数字员工资料已变化，请重新预览")
    current_relationship_checksum = agent_organization_relationship_checksum(db, agent)
    if current_relationship_checksum != request.expected_relationship_checksum:
        raise OrganizationizationError("AGENT_RELATIONSHIP_STALE", "组织关系已变化，请重新预览")

    organization = db.get(OrganizationUnit, normalized_org_id)
    if (
        organization is None
        or organization.tenant_id != request.tenant_id
        or organization.status != "active"
    ):
        raise OrganizationizationError(
            "RESPONSIBLE_ORGANIZATION_INVALID",
            "责任组织不存在、已停用或不属于当前企业",
            422,
        )
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == request.tenant_id,
            BusinessRole.role_code == request.role_code.strip(),
            BusinessRole.role_kind == "business",
            BusinessRole.status == "active",
        )
    ).first()
    if role is None:
        raise OrganizationizationError("BUSINESS_ROLE_INVALID", "活动业务角色不存在", 422)
    supervisor = db.get(EmployeeProfile, request.supervisor_employee_profile_id.strip())
    supervisor_user = db.get(User, supervisor.user_id) if supervisor else None
    if (
        supervisor is None
        or supervisor.tenant_id != request.tenant_id
        or supervisor.status != "active"
        or supervisor_user is None
        or supervisor_user.tenant_id != request.tenant_id
        or supervisor_user.membership_status != "active"
    ):
        raise OrganizationizationError("SUPERVISOR_INVALID", "监督者必须是当前租户活动员工", 422)
    try:
        validate_role_assignment_scope(
            db,
            tenant_id=request.tenant_id,
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            include_descendants=request.include_descendants,
        )
    except ValueError as error:
        raise OrganizationizationError("ROLE_SCOPE_INVALID", "数字员工工作范围无效", 422) from error
    effective_from, effective_until = _validated_effective_range(
        request.effective_from,
        request.effective_until,
    )
    existing = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == request.tenant_id,
            AgentRoleBinding.agent_id == agent.id,
            AgentRoleBinding.business_role_id == role.id,
            AgentRoleBinding.scope_type == request.scope_type,
            AgentRoleBinding.scope_id == request.scope_id,
        )
    ).first()
    before_agent = {
        "responsible_org_unit_id": agent.responsible_org_unit_id,
        "profile_revision": agent.profile_revision,
    }
    before_binding = _binding_snapshot(existing) if existing else {}
    now = utc_now()
    binding = existing or AgentRoleBinding(
        tenant_id=request.tenant_id,
        agent_id=agent.id,
        business_role_id=role.id,
        scope_type=request.scope_type,
        scope_id=request.scope_id,
    )
    changed = (
        agent.responsible_org_unit_id != organization.id
        or binding.assignment_mode != request.assignment_mode
        or binding.supervisor_employee_profile_id != supervisor.id
        or binding.include_descendants != request.include_descendants
        or binding.status != "active"
        or binding.effective_from != effective_from
        or binding.effective_until != effective_until
    )
    agent.responsible_org_unit_id = organization.id
    binding.assignment_mode = request.assignment_mode
    binding.supervisor_employee_profile_id = supervisor.id
    binding.include_descendants = request.include_descendants
    binding.granted_by_user_id = actor.id
    binding.status = "active"
    binding.effective_from = effective_from or now
    binding.effective_until = effective_until
    binding.metadata_json = {
        **(binding.metadata_json or {}),
        "source": f"agent_organizationization:{actor.id}",
        "command_id": request.command_id,
    }
    binding.updated_at = now
    agent.updated_at = now
    if changed:
        agent.profile_revision = max(int(agent.profile_revision or 1), 1) + 1
    db.add(agent)
    db.add(binding)
    db.flush()
    relationship_checksum = agent_organization_relationship_checksum(db, agent)
    result_status = "configured" if changed else "unchanged"
    command = AgentOrganizationizationCommand(
        tenant_id=request.tenant_id,
        agent_id=agent.id,
        command_id=request.command_id,
        request_checksum=request_checksum,
        expected_profile_revision=request.expected_profile_revision,
        expected_relationship_checksum=request.expected_relationship_checksum,
        active_role_binding_id=binding.id,
        status="committed",
        result_json={
            "result_status": result_status,
            "agent_id": agent.id,
            "profile_revision": agent.profile_revision,
            "relationship_checksum": relationship_checksum,
            "active_role_binding_id": binding.id,
        },
        created_at=now,
        updated_at=now,
    )
    db.add(command)
    append_user_management_audit(
        db,
        current_user=actor,
        tenant_id=request.tenant_id,
        permission_code="agent.manage",
        action="agent.organizationization.configure",
        action_kind="update" if existing else "create",
        outcome="success",
        resource_type="agent_profile",
        resource_id=agent.id,
        target_org_unit_id=organization.id,
        before={**before_agent, "binding": before_binding},
        after={
            "responsible_org_unit_id": agent.responsible_org_unit_id,
            "profile_revision": agent.profile_revision,
            "binding": _binding_snapshot(binding),
            "relationship_checksum": relationship_checksum,
        },
        detail={"command_id": request.command_id, "result_status": result_status},
    )
    db.commit()
    db.refresh(command)
    return _result_from_command(command)


def _request_checksum(agent_id: str, request: AgentOrganizationizationRequest) -> str:
    """对组织化命令的业务输入生成稳定摘要，不记录监督者姓名或敏感内容。"""

    payload = {"agent_id": agent_id, **request.model_dump(mode="json")}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _result_from_command(command: AgentOrganizationizationCommand) -> OrganizationizationApplyResult:
    """把持久回执转换为可重放的领域结果。"""

    result = command.result_json or {}
    return OrganizationizationApplyResult(
        command_id=command.command_id,
        result_status=str(result.get("result_status") or "configured"),
        agent_id=str(result.get("agent_id") or command.agent_id),
        profile_revision=int(result.get("profile_revision") or 1),
        relationship_checksum=str(result.get("relationship_checksum") or ""),
        active_role_binding_id=str(
            result.get("active_role_binding_id") or command.active_role_binding_id or ""
        ),
    )


def _binding_snapshot(binding: AgentRoleBinding | None) -> dict[str, object]:
    """返回角色绑定的非敏感版本快照，供审计对比。"""

    if binding is None:
        return {}
    return {
        "id": binding.id,
        "business_role_id": binding.business_role_id,
        "assignment_mode": binding.assignment_mode,
        "supervisor_employee_profile_id": binding.supervisor_employee_profile_id,
        "scope_type": binding.scope_type,
        "scope_id": binding.scope_id,
        "include_descendants": binding.include_descendants,
        "status": binding.status,
        "effective_from": binding.effective_from.isoformat() if binding.effective_from else None,
        "effective_until": binding.effective_until.isoformat() if binding.effective_until else None,
    }


def _validated_effective_range(
    effective_from: datetime | None,
    effective_until: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """把有效期转换为 UTC 并拒绝结束早于开始的组织化配置。"""

    normalized_from = _naive_utc(effective_from)
    normalized_until = _naive_utc(effective_until)
    if normalized_from and normalized_until and normalized_until <= normalized_from:
        raise OrganizationizationError("EFFECTIVE_RANGE_INVALID", "有效期结束时间必须晚于开始时间", 422)
    return normalized_from, normalized_until


def _naive_utc(value: datetime | None) -> datetime | None:
    """把带时区时间规范化为项目数据库使用的无时区 UTC。"""

    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
