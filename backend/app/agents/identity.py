"""
@Time       : 2026/07/28 21:18
@Author     : zhanglp8181
@File       : identity.py
@CallChain  : Agent API/权限服务 → 正式字段与 legacy metadata 双读 → 列表和对象授权
@Description: 集中解析数字员工所有权、发布、分类和可见范围，避免各调用方产生迁移期双语义。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal

from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    OrganizationUnit,
    PublicationRelease,
    User,
    utc_now,
)

GovernanceForm = Literal[
    "capability_avatar",
    "organization_pending",
    "organization_employee",
    "template",
]


@dataclass(frozen=True, slots=True)
class AgentGovernanceProjection:
    """集中投影 Agent 当前治理形态，并保留可修复的前置条件原因。"""

    form: GovernanceForm
    reasons: tuple[str, ...] = ()
    organization_release_id: str | None = None
    active_role_binding_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """转换为管理端和审计可复用的非敏感治理摘要。"""

        return {
            "governance_form": self.form,
            "governance_reasons": list(self.reasons),
            "organization_release_id": self.organization_release_id,
            "active_role_binding_ids": list(self.active_role_binding_ids),
        }


def agent_owner_user_id(row: AgentProfile) -> str | None:
    """正式 owner 优先；仅在历史字段为空时读取不可变 metadata 用户 ID。"""

    if row.owner_user_id:
        return row.owner_user_id
    candidate = (row.metadata_json or {}).get("owner_user_id")
    return str(candidate).strip() if candidate else None


def agent_is_imported_expert_template(row: AgentProfile) -> bool:
    """识别项目内专家模板，不把技术 owner 误投影为个人能力分身。

    专家导入和精选演示 Agent 都可能为了兼容旧的审计/种子流程写入管理员
    `owner_user_id`。真正的个人能力分身必须经过复制，具有 `source_agent_id`；
    因此模板标记只对没有来源 Agent 的记录生效。
    """

    metadata = row.metadata_json or {}
    if row.source_agent_id:
        return False
    if metadata.get("governance_template") is True:
        return True
    if metadata.get("owner_semantics") == "technical_import_admin":
        return True
    if (
        metadata.get("seed_source") == "public_demo_agent"
        and row.agent_category_code == "professional"
    ):
        return True
    return (
        metadata.get("employee_type") == "expert"
        and metadata.get("expert_source_code") == "agency-agents"
        and bool(metadata.get("import_batch_id"))
    )


def agent_is_published(row: AgentProfile) -> bool:
    """读取正式广场状态，并兼容尚未经过 0024 回填的测试或历史对象。"""

    if row.published_to_gallery is not None:
        return row.published_to_gallery
    return (row.metadata_json or {}).get("published_to_gallery") is True


def agent_category(row: AgentProfile) -> str:
    """返回正式业务分类，兼容明确标记为 expert 的历史数字员工。"""

    metadata = row.metadata_json or {}
    if row.agent_category_code != "assistant":
        return row.agent_category_code
    if metadata.get("employee_type") == "expert":
        return "professional"
    return row.agent_category_code or "assistant"


def agent_visibility_scope(row: AgentProfile) -> str:
    """返回受控可见范围；历史已发布对象按租户可见处理。"""

    if row.visibility_scope == "tenant" or agent_is_published(row):
        return "tenant"
    return "private"


def agent_organization_relationship_snapshot(
    db: Session,
    row: AgentProfile,
) -> dict[str, object]:
    """冻结 Agent 当前责任组织、业务角色和监督者关系，供组织化 CAS 使用。"""

    organization = (
        db.get(OrganizationUnit, row.responsible_org_unit_id)
        if row.responsible_org_unit_id
        else None
    )
    bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == row.tenant_id,
            AgentRoleBinding.agent_id == row.id,
            AgentRoleBinding.status == "active",
        )
    ).all()
    binding_snapshots: list[dict[str, object]] = []
    for binding in bindings:
        role = db.get(BusinessRole, binding.business_role_id)
        supervisor = (
            db.get(EmployeeProfile, binding.supervisor_employee_profile_id)
            if binding.supervisor_employee_profile_id
            else None
        )
        binding_snapshots.append(
            {
                "id": binding.id,
                "business_role_id": binding.business_role_id,
                "role_code": role.role_code if role else None,
                "role_kind": role.role_kind if role else None,
                "role_status": role.status if role else None,
                "assignment_mode": binding.assignment_mode,
                "supervisor_employee_profile_id": binding.supervisor_employee_profile_id,
                "supervisor_status": supervisor.status if supervisor else None,
                "scope_type": binding.scope_type,
                "scope_id": binding.scope_id,
                "include_descendants": binding.include_descendants,
                "status": binding.status,
                "effective_from": (
                    binding.effective_from.isoformat() if binding.effective_from else None
                ),
                "effective_until": (
                    binding.effective_until.isoformat() if binding.effective_until else None
                ),
            }
        )
    binding_snapshots.sort(key=lambda item: (str(item["id"]), str(item["business_role_id"])))
    return {
        "responsible_org_unit": {
            "id": row.responsible_org_unit_id,
            "status": organization.status if organization else None,
        },
        "role_bindings": binding_snapshots,
    }


def agent_organization_relationship_checksum(
    db: Session,
    row: AgentProfile,
) -> str:
    """对组织化关系生成规范 checksum，阻止表单基于旧关系覆盖新配置。"""

    snapshot = agent_organization_relationship_snapshot(db, row)
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def project_agent_governance(
    db: Session,
    row: AgentProfile,
) -> AgentGovernanceProjection:
    """按 owner、组织角色、监督人和 active Release 投影统一 Agent 身份形态。

    该函数只读取当前租户的正式关系事实，不用名称、专家分类或执行模式猜测身份。
    组织员工必须同时具备活动责任组织、有效业务角色、活动监督人、活动 Agent Release
    和活动 Agent；只要缺少任一项，就返回 `organization_pending` 以及可修复原因。
    """

    metadata = row.metadata_json or {}
    if row.is_overall:
        return AgentGovernanceProjection("template", ("overall_template",))

    owner_id = agent_owner_user_id(row)
    owner = db.get(User, owner_id) if owner_id else None
    has_active_owner = bool(
        owner is not None
        and owner.tenant_id == row.tenant_id
        and owner.membership_status == "active"
    )
    active_bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == row.tenant_id,
            AgentRoleBinding.agent_id == row.id,
            AgentRoleBinding.status == "active",
        )
    ).all()
    now = utc_now()
    effective_bindings = [
        binding
        for binding in active_bindings
        if (binding.effective_from is None or binding.effective_from <= now)
        and (binding.effective_until is None or binding.effective_until > now)
    ]
    valid_binding_ids: list[str] = []
    for binding in effective_bindings:
        role = db.get(BusinessRole, binding.business_role_id)
        supervisor = (
            db.get(EmployeeProfile, binding.supervisor_employee_profile_id)
            if binding.supervisor_employee_profile_id
            else None
        )
        if (
            role is not None
            and role.tenant_id == row.tenant_id
            and role.status == "active"
            and role.role_kind == "business"
            and supervisor is not None
            and supervisor.tenant_id == row.tenant_id
            and supervisor.status == "active"
        ):
            valid_binding_ids.append(binding.id)

    organization = (
        db.get(OrganizationUnit, row.responsible_org_unit_id)
        if row.responsible_org_unit_id
        else None
    )
    has_responsible_organization = bool(
        organization is not None
        and organization.tenant_id == row.tenant_id
        and organization.status == "active"
    )
    release = db.exec(
        select(PublicationRelease).where(
            PublicationRelease.tenant_id == row.tenant_id,
            PublicationRelease.resource_type == "agent",
            PublicationRelease.resource_id == row.id,
            PublicationRelease.status == "active",
        )
    ).first()
    reasons: list[str] = []
    if not has_active_owner:
        reasons.append("owner_required")
    if not has_responsible_organization:
        reasons.append("responsible_organization_required")
    if not valid_binding_ids:
        reasons.append("active_role_and_supervisor_required")
    if release is None:
        reasons.append("active_publication_release_required")
    if row.status != "active":
        reasons.append("agent_must_be_active")

    has_organization_facts = bool(
        row.responsible_org_unit_id or active_bindings or release is not None
    )
    if (
        has_active_owner
        and has_responsible_organization
        and valid_binding_ids
        and release is not None
        and row.status == "active"
    ):
        return AgentGovernanceProjection(
            "organization_employee",
            ("active_organization_release",),
            organization_release_id=release.id,
            active_role_binding_ids=tuple(sorted(valid_binding_ids)),
        )
    if has_organization_facts:
        return AgentGovernanceProjection(
            "organization_pending",
            tuple(reasons),
            organization_release_id=release.id if release else None,
            active_role_binding_ids=tuple(sorted(valid_binding_ids)),
        )
    if has_active_owner and not agent_is_imported_expert_template(row):
        return AgentGovernanceProjection("capability_avatar", ("user_owned_private_agent",))
    if metadata.get("employee_type") == "expert" or row.agent_category_code == "professional":
        return AgentGovernanceProjection("template", ("unowned_professional_template",))
    return AgentGovernanceProjection("template", ("unowned_agent_template",))
