"""
@Time       : 2026/08/10 16:20
@Author     : zhanglp8181
@File       : models.py
@CallChain  : API/Seed/Workers → SQLModel Session → models.py → SQLAlchemy Engine
@Description: 定义应用持久化使用的 SQLModel 实体。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

from app.db.sql_types import (
    IdentifierString,
    LabelString,
    LongTextString,
    MediumTextString,
    NameString,
    OptionalIdentifierString,
    OptionalLabelString,
    OptionalLongTextString,
    OptionalMediumTextString,
    OptionalNameString,
    OptionalPlainTextString,
    OptionalVersionString,
    PasswordHashString,
    PlainTextString,
    PRECISE_DATETIME,
    PrimaryKeyString,
    VersionString,
)


def utc_now() -> datetime:
    """返回去除时区信息的当前 UTC 时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    """生成由指定前缀和截短 UUID 组成的标识符。"""
    return f"{prefix}_{uuid4().hex[:16]}"


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    id: IdentifierString = Field(primary_key=True)
    name: NameString
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("user"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    username: NameString = Field(index=True)
    display_name: OptionalNameString = None
    role: LabelString = Field(default="member", index=True)
    membership_status: LabelString = Field(default="active", index=True)
    member_category_code: IdentifierString = Field(default="employee", index=True)
    joined_at: datetime = Field(default_factory=utc_now)
    left_at: Optional[datetime] = None
    password_hash: PasswordHashString
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EmployeeProfile(SQLModel, table=True):
    """把登录账号映射到租户内唯一的业务员工身份。"""

    __tablename__ = "employee_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_employee_profile_tenant_user"),
        UniqueConstraint("tenant_id", "employee_id", name="uq_employee_profile_tenant_employee"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("employee"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    employee_id: IdentifierString = Field(index=True)
    employee_name: OptionalNameString = None
    department_id: OptionalIdentifierString = Field(default=None, index=True)
    status: LabelString = Field(default="active", index=True)
    join_date: datetime = Field(default_factory=utc_now)
    leave_date: Optional[datetime] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CodeSet(SQLModel, table=True):
    """定义租户内由平台声明、供业务实体引用的稳定码表。"""

    __tablename__ = "code_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "set_code", name="uq_code_set_tenant_code"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("codeset"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    set_code: IdentifierString = Field(index=True)
    name: NameString
    description: OptionalPlainTextString = None
    allow_custom_items: bool = Field(default=False)
    is_system: bool = Field(default=True)
    status: LabelString = Field(default="active", index=True)
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CodeItem(SQLModel, table=True):
    """定义码表内编码不可变、可停用但保留历史显示的码项。"""

    __tablename__ = "code_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "code_set_id",
            "item_code",
            name="uq_code_item_tenant_set_code",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("codeitem"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    code_set_id: IdentifierString = Field(index=True)
    item_code: IdentifierString = Field(index=True)
    name: NameString
    description: OptionalPlainTextString = None
    parent_item_id: OptionalIdentifierString = Field(default=None, index=True)
    sort_order: int = Field(default=0)
    is_builtin: bool = Field(default=False)
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    revision: int = Field(default=0, ge=0)
    created_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    updated_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OrganizationUnit(SQLModel, table=True):
    """定义租户内唯一根节点下的稳定企业组织树。"""

    __tablename__ = "organization_units"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "code",
            name="uq_organization_unit_tenant_code",
        ),
        Index(
            "uq_organization_unit_root_tenant",
            "root_tenant_id",
            unique=True,
        ),
        Index(
            "ix_org_unit_tenant_parent_status_sort",
            "tenant_id",
            "parent_id",
            "status",
            "sort_order",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("orgunit"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    parent_id: OptionalIdentifierString = Field(default=None, index=True)
    code: IdentifierString = Field(index=True)
    name: NameString
    unit_type_code: IdentifierString = Field(index=True)
    tree_path: LongTextString
    depth: int = Field(default=0, ge=0)
    sort_order: int = Field(default=0)
    is_root: bool = Field(default=False, index=True)
    root_tenant_id: OptionalIdentifierString = Field(default=None)
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemberOrgAssignment(SQLModel, table=True):
    """追加保存员工的组织归属任期，活动主归属由领域命令维护唯一。"""

    __tablename__ = "member_org_assignments"
    __table_args__ = (
        Index(
            "ix_member_org_tenant_org_current",
            "tenant_id",
            "org_unit_id",
            "status",
            "effective_until",
            "employee_profile_id",
        ),
        Index(
            "ix_member_org_tenant_member_current",
            "tenant_id",
            "employee_profile_id",
            "status",
            "effective_until",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("memberorg"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    employee_profile_id: IdentifierString = Field(index=True)
    org_unit_id: IdentifierString = Field(index=True)
    assignment_type: LabelString = Field(default="primary", index=True)
    is_primary: bool = Field(default=True, index=True)
    effective_from: datetime = Field(
        default_factory=utc_now, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Position(SQLModel, table=True):
    """定义隶属于活动组织的稳定岗位，岗位与人员任职相互独立。"""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_position_tenant_code"),
        Index(
            "ix_position_tenant_org_status_code",
            "tenant_id",
            "org_unit_id",
            "status",
            "code",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("position"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    org_unit_id: IdentifierString = Field(index=True)
    code: IdentifierString = Field(index=True)
    name: NameString
    position_type_code: IdentifierString = Field(index=True)
    grade_code: OptionalIdentifierString = Field(default=None, index=True)
    reports_to_position_id: OptionalIdentifierString = Field(default=None, index=True)
    headcount_limit: int | None = Field(default=None, ge=1)
    responsibility: OptionalMediumTextString = None
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PositionAssignment(SQLModel, table=True):
    """追加保存员工岗位任职区间，避免调岗时覆盖历史。"""

    __tablename__ = "position_assignments"
    __table_args__ = (
        Index(
            "ix_pos_assign_tenant_position_current",
            "tenant_id",
            "position_id",
            "status",
            "effective_until",
            "employee_profile_id",
        ),
        Index(
            "ix_pos_assign_tenant_member_current",
            "tenant_id",
            "employee_profile_id",
            "status",
            "effective_until",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("posassign"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    employee_profile_id: IdentifierString = Field(index=True)
    position_id: IdentifierString = Field(index=True)
    assignment_type: LabelString = Field(default="primary", index=True)
    is_primary: bool = Field(default=True, index=True)
    effective_from: datetime = Field(
        default_factory=utc_now, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PositionRoleBinding(SQLModel, table=True):
    """把岗位绑定到默认业务角色，成员通过活动岗位任职获得角色。"""

    __tablename__ = "position_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "position_id",
            "business_role_id",
            name="uq_position_role_binding",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("posrole"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    position_id: IdentifierString = Field(index=True)
    business_role_id: IdentifierString = Field(index=True)
    scope_mode: LabelString = Field(default="position_org", index=True)
    granted_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    status: LabelString = Field(default="active", index=True)
    effective_from: datetime | None = Field(
        default_factory=utc_now, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OrganizationLeaderAssignment(SQLModel, table=True):
    """保存组织负责人有效期关系，责任事实不直接产生角色或权限。"""

    __tablename__ = "organization_leader_assignments"
    __table_args__ = (
        Index(
            "ix_org_leader_tenant_org_current",
            "tenant_id",
            "org_unit_id",
            "status",
            "effective_until",
        ),
        Index(
            "ix_org_leader_tenant_member_current",
            "tenant_id",
            "employee_profile_id",
            "status",
            "effective_until",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("orgleader"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    org_unit_id: IdentifierString = Field(index=True)
    employee_profile_id: IdentifierString = Field(index=True)
    position_assignment_id: OptionalIdentifierString = Field(default=None, index=True)
    leader_type_code: IdentifierString = Field(index=True)
    effective_from: datetime = Field(
        default_factory=utc_now, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    status: LabelString = Field(default="active", index=True)
    source_kind: LabelString = Field(default="manual", index=True)
    created_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OrganizationMigrationIssue(SQLModel, table=True):
    """记录旧部门字段无法可靠映射时的待治理事项。"""

    __tablename__ = "organization_migration_issues"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("orgissue"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    employee_profile_id: IdentifierString = Field(index=True)
    source_field: LabelString
    source_value: OptionalPlainTextString = None
    issue_code: LabelString = Field(index=True)
    resolution_status: LabelString = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BusinessRole(SQLModel, table=True):
    """定义租户内稳定的业务或治理角色和可授予能力。"""

    __tablename__ = "business_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "role_code", name="uq_business_role_tenant_code"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("bizrole"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    role_code: IdentifierString = Field(index=True)
    name: NameString
    role_kind: LabelString = Field(default="business", index=True)
    category: LabelString = Field(default="business", index=True)
    permissions_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BusinessRoleCategory(SQLModel, table=True):
    """定义租户内可治理的公司业务角色分类，分类编码创建后保持稳定。"""

    __tablename__ = "business_role_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category_code", name="uq_business_role_category_code"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("rolecat"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    category_code: IdentifierString = Field(index=True)
    name: NameString
    description: OptionalPlainTextString = None
    role_code_prefix: LabelString
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PermissionDefinition(SQLModel, table=True):
    """定义租户内可检索、可停用且编码稳定的原子业务权限点。"""

    __tablename__ = "permission_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "permission_code", name="uq_permission_tenant_code"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("permission"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    permission_code: IdentifierString = Field(index=True)
    name: NameString
    category: LabelString = Field(index=True)
    resource: IdentifierString = Field(index=True)
    action: LabelString = Field(index=True)
    scope: OptionalLabelString = Field(default=None, index=True)
    description: OptionalPlainTextString = None
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BusinessRolePermission(SQLModel, table=True):
    """把公司业务角色映射到权限目录，并保留独立于角色 JSON 缓存的关系事实。"""

    __tablename__ = "business_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "business_role_id",
            "permission_definition_id",
            name="uq_business_role_permission",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("roleperm"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    business_role_id: IdentifierString = Field(index=True)
    permission_definition_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)


class EmployeeRoleAssignment(SQLModel, table=True):
    """记录现实员工在指定组织作用域内承担的业务或治理角色。"""

    __tablename__ = "employee_role_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "employee_profile_id",
            "business_role_id",
            "scope_type",
            "scope_id",
            name="uq_employee_role_assignment_scope",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("emprole"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    employee_profile_id: IdentifierString = Field(index=True)
    business_role_id: IdentifierString = Field(index=True)
    scope_type: LabelString = Field(default="tenant", index=True)
    scope_id: IdentifierString = Field(default="*", index=True)
    include_descendants: bool = Field(default=True, index=True)
    granted_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    status: LabelString = Field(default="active", index=True)
    effective_from: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentRoleBinding(SQLModel, table=True):
    """把数字员工映射到公司业务角色，并声明其执行和监督边界。"""

    __tablename__ = "agent_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_id",
            "business_role_id",
            "scope_type",
            "scope_id",
            name="uq_agent_role_binding_scope",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentrole"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    business_role_id: IdentifierString = Field(index=True)
    assignment_mode: LabelString = Field(default="assist", index=True)
    supervisor_employee_profile_id: OptionalIdentifierString = Field(default=None, index=True)
    scope_type: LabelString = Field(default="tenant", index=True)
    scope_id: IdentifierString = Field(default="*", index=True)
    include_descendants: bool = Field(default=True, index=True)
    granted_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    status: LabelString = Field(default="active", index=True)
    effective_from: datetime | None = Field(
        default_factory=utc_now, index=True, sa_type=PRECISE_DATETIME
    )
    effective_until: datetime | None = Field(
        default=None, index=True, sa_type=PRECISE_DATETIME
    )
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Skill(SQLModel, table=True):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", name="uq_skill_tenant_skill_id"),)

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("skill"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    version: VersionString = "1.0.0"
    name: NameString
    business_domain: OptionalNameString = None
    description: OptionalMediumTextString = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: LabelString = Field(default="draft", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillVersion(SQLModel, table=True):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "skill_id", "version", name="uq_skill_version"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("skillver"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    version: VersionString = Field(index=True)
    name: NameString
    business_domain: OptionalNameString = None
    description: OptionalMediumTextString = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: LabelString = Field(default="draft", index=True)
    content_checksum: OptionalVersionString = Field(default=None, index=True)
    compiled_definition_checksum: OptionalVersionString = None
    meta_model_version: int | None = None
    source_schema_version: int | None = None
    published_at: datetime | None = None
    derived_from_version_id: Optional[str] = Field(default=None, max_length=512, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SopInstance(SQLModel, table=True):
    """绑定不可变技能版本并保存可恢复执行游标的 SOP 实例。"""

    __tablename__ = "sop_instances"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "skill_version_id",
            "run_number",
            name="uq_sop_instance_session_version_run",
        ),
        UniqueConstraint(
            "tenant_id",
            "active_slot_key",
            name="uq_execution_tenant_active_slot",
        ),
        CheckConstraint(
            "kind IN ('sop', 'dynamic_task')",
            name="ck_execution_kind",
        ),
        CheckConstraint(
            "((status IN ('created', 'running', 'waiting') AND active_slot_key IS NOT NULL) OR "
            "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out') "
            "AND active_slot_key IS NULL))",
            name="ck_execution_active_slot",
        ),
        CheckConstraint(
            "kind <> 'sop' OR (skill_id IS NOT NULL AND skill_version_id IS NOT NULL "
            "AND skill_version IS NOT NULL AND definition_checksum IS NOT NULL)",
            name="ck_execution_sop_identity",
        ),
        CheckConstraint(
            "kind <> 'sop' OR current_plan_revision_id IS NULL",
            name="ck_execution_sop_without_dynamic_plan",
        ),
        CheckConstraint(
            "kind <> 'dynamic_task' OR (agent_id IS NOT NULL AND initiator_user_id IS NOT NULL "
            "AND goal_snapshot_json IS NOT NULL AND current_plan_revision_id IS NOT NULL "
            "AND current_plan_checksum IS NOT NULL AND capability_snapshot_json IS NOT NULL)",
            name="ck_execution_dynamic_identity",
        ),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_execution_lease_pair",
        ),
        CheckConstraint("fencing_token >= 0", name="ck_execution_fencing_nonnegative"),
        CheckConstraint(
            "((cancellation_requested_at IS NULL AND cancellation_disposition = 'none') OR "
            "(cancellation_requested_at IS NOT NULL AND cancellation_disposition <> 'none'))",
            name="ck_execution_cancellation_request",
        ),
        CheckConstraint(
            "effect_state IN ('none', 'partial', 'complete', 'unknown')",
            name="ck_execution_effect_state",
        ),
        Index(
            "ix_sop_instances_tenant_lease_expiry",
            "tenant_id",
            "lease_expires_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopinst"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    skill_id: OptionalIdentifierString = Field(default=None, index=True)
    skill_version_id: OptionalIdentifierString = Field(default=None, index=True)
    skill_version: OptionalVersionString = None
    definition_checksum: OptionalVersionString = None
    run_number: int = Field(default=1, ge=1)
    kind: LabelString = Field(default="sop")
    active_slot_key: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    initiator_user_id: OptionalIdentifierString = None
    source_kind: LabelString = Field(default="chat")
    source_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    agent_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    goal_snapshot_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    current_plan_revision_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    current_plan_checksum: OptionalVersionString = Field(default=None, index=True)
    capability_snapshot_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    capability_checksum: OptionalVersionString = Field(default=None, index=True)
    budget_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    terminal_reason_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    current_result_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    status: LabelString = Field(default="created", index=True)
    current_node_id: OptionalIdentifierString = Field(default=None, index=True)
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    context_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    revision: int = Field(default=0, ge=0)
    cancellation_requested_at: datetime | None = None
    cancellation_requested_by: OptionalIdentifierString = None
    cancellation_reason: OptionalMediumTextString = None
    cancellation_disposition: LabelString = Field(default="none")
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_expires_at: datetime | None = None
    lease_acquired_at: datetime | None = None
    lease_heartbeat_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    effect_state: LabelString = Field(default="none")
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SopNodeExecution(SQLModel, table=True):
    """保存 SOP 节点一次不可覆盖的 attempt 及其输入输出快照。"""

    __tablename__ = "sop_node_executions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "instance_id",
            "node_id",
            "attempt",
            name="uq_sop_node_execution_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "instance_id",
            "step_key",
            "attempt",
            name="uq_execution_step_attempt",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopnode"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_id: IdentifierString = Field(index=True)
    step_key: IdentifierString = Field(index=True)
    plan_revision_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    step_kind: LabelString = Field(default="sop_node", index=True)
    title: OptionalNameString = None
    required: bool = Field(default=True)
    superseded_by_step_key: OptionalIdentifierString = Field(default=None, index=True)
    attempt: int = Field(default=1, ge=1)
    status: LabelString = Field(default="scheduled", index=True)
    input_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    revision: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutionPlanRevision(SQLModel, table=True):
    """追加保存动态任务计划修订及其完整能力快照，不覆盖历史计划。"""

    __tablename__ = "execution_plan_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "revision_number",
            name="uq_execution_plan_revision_number",
        ),
        CheckConstraint(
            "status IN ('validated', 'active', 'superseded', 'rejected')",
            name="ck_execution_plan_revision_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execplan"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    revision_number: int = Field(ge=1)
    parent_revision_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    reason: LabelString = Field(default="initial")
    status: LabelString = Field(default="validated", index=True)
    plan_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    checksum: VersionString = Field(index=True)
    capability_snapshot_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    capability_checksum: VersionString = Field(index=True)
    created_by_proposal_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    activated_at: datetime | None = None
    superseded_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ActionProposalRecord(SQLModel, table=True):
    """持久保存完整 provider 响应经服务端验证后的规范动作提案。"""

    __tablename__ = "action_proposal_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "proposal_checksum",
            name="uq_action_proposal_checksum",
        ),
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "provider_response_identity",
            name="uq_action_proposal_provider_response",
        ),
        CheckConstraint(
            "status IN ('validated', 'consumed', 'superseded')",
            name="ck_action_proposal_status",
        ),
        CheckConstraint(
            "NOT (consumed_operation_id IS NOT NULL AND consumed_plan_revision_id IS NOT NULL)",
            name="ck_action_proposal_single_consumption_target",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("proposal"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    plan_revision_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    step_key: IdentifierString = Field(index=True)
    step_attempt: int = Field(ge=1)
    provider: LabelString
    model: NameString
    provider_response_id: str = Field(
        sa_column=Column(String(512), nullable=False),
    )
    provider_response_identity: VersionString = Field(index=True)
    finish_reason: LabelString
    model_capability_snapshot_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    normalized_proposal_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    validation_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    proposal_checksum: VersionString = Field(index=True)
    usage_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: LabelString = Field(default="validated", index=True)
    consumed_operation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    consumed_plan_revision_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    consumed_at: datetime | None = None
    superseded_at: datetime | None = None
    causation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    created_at: datetime = Field(default_factory=utc_now)


class ManagedInputResource(SQLModel, table=True):
    """保存聊天上传资源的服务端身份、内容摘要、提取状态和当前访问边界。"""

    __tablename__ = "managed_input_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            "version",
            name="uq_managed_input_resource_version",
        ),
        CheckConstraint(
            "ingestion_status IN ('uploaded', 'scanning', 'extracting', 'ready', "
            "'quarantined', 'failed', 'revoked')",
            name="ck_managed_input_resource_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("input"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    owner_user_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    agent_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    source_type: LabelString = Field(default="chat_upload")
    source_message_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    version: VersionString
    filename: NameString
    mime_type: NameString
    size_bytes: int = Field(ge=0)
    content_checksum: VersionString = Field(index=True)
    extraction_checksum: OptionalVersionString = Field(default=None, index=True)
    ingestion_status: LabelString = Field(default="uploaded", index=True)
    storage_locator: str = Field(sa_column=Column(String(1000), nullable=False))
    extracted_text: OptionalLongTextString = None
    extraction_metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    acl_revision: int = Field(default=0, ge=0)
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class InputResourceSnapshot(SQLModel, table=True):
    """追加冻结 Execution 输入资源版本和 ACL 证据，正文仍由当前权限解析。"""

    __tablename__ = "input_resource_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "identity_checksum",
            name="uq_execution_input_resource_identity",
        ),
        CheckConstraint(
            "ingestion_status IN ('ready', 'quarantined', 'failed', 'revoked')",
            name="ck_input_resource_snapshot_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("inputsnap"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    source_type: LabelString
    source_resource_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    source_version: VersionString
    source_message_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    filename: NameString
    mime_type: NameString
    size_bytes: int = Field(ge=0)
    content_checksum: VersionString = Field(index=True)
    extraction_checksum: OptionalVersionString = Field(default=None, index=True)
    ingestion_status: LabelString = Field(index=True)
    identity_checksum: VersionString = Field(index=True)
    storage_locator_digest: VersionString
    captured_acl_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionArtifact(SQLModel, table=True):
    """登记由 Execution 产生、可校验下载且不以文件路径授权的交付物。"""

    __tablename__ = "execution_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "artifact_key",
            name="uq_execution_artifact_key",
        ),
        CheckConstraint(
            "status IN ('ready', 'corrupt', 'revoked')",
            name="ck_execution_artifact_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_execution_artifact_size"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("artifact"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    source_node_execution_id: IdentifierString = Field(index=True)
    source_step_key: IdentifierString = Field(index=True)
    artifact_key: IdentifierString = Field(index=True)
    filename: NameString
    mime_type: NameString = Field(index=True)
    size_bytes: int = Field(ge=0)
    content_checksum: VersionString = Field(index=True)
    storage_locator: str = Field(sa_column=Column(String(1000), nullable=False))
    acl_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    lineage_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: LabelString = Field(default="ready", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class ArtifactInputLink(SQLModel, table=True):
    """保存输出 Artifact 到精确输入快照的有方向血缘边。"""

    __tablename__ = "artifact_input_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "artifact_id",
            "input_snapshot_id",
            name="uq_artifact_input_link",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("artifactinput"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    artifact_id: IdentifierString = Field(index=True)
    input_snapshot_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)


class SopOperation(SQLModel, table=True):
    """保存外部工具副作用的幂等命令、执行状态和结构化回执。"""

    __tablename__ = "sop_operations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_sop_operation_tenant_idempotency"
        ),
        UniqueConstraint(
            "tenant_id",
            "logical_action_id",
            name="uq_sop_operation_tenant_logical_action",
        ),
        CheckConstraint(
            "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', 'cancelled')",
            name="ck_sop_operation_status",
        ),
        CheckConstraint(
            "effect_kind IN ('read', 'external_write', 'legacy_unknown')",
            name="ck_sop_operation_effect_kind",
        ),
        CheckConstraint(
            "effect_state IN ('none', 'complete', 'unknown', 'compensated')",
            name="ck_sop_operation_effect_state",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopop"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_execution_id: IdentifierString = Field(index=True)
    operation_name: NameString = Field(index=True)
    idempotency_key: VersionString
    logical_action_id: IdentifierString = Field(index=True)
    request_fingerprint: VersionString
    remote_idempotency_key: OptionalIdentifierString = Field(default=None, index=True)
    idempotency_required: bool = Field(default=True)
    idempotency_scope: LabelString = Field(default="instance")
    idempotency_key_fields_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    effect_kind: LabelString = Field(default="read")
    effect_state: LabelString = Field(default="none")
    cancellation_disposition: LabelString = Field(default="none")
    compensates_operation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    status: LabelString = Field(default="prepared", index=True)
    request_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    capability_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    capability_checksum: OptionalVersionString = Field(default=None, index=True)
    approval_work_item_id: OptionalIdentifierString = Field(default=None, index=True)
    approval_fingerprint: OptionalVersionString = Field(default=None, index=True)
    approved_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    approved_at: datetime | None = None
    authorization_evidence_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    authorization_source_type: LabelString = Field(default="legacy", index=True)
    authorization_source_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    dispatched_at: datetime | None = None
    external_reference: OptionalIdentifierString = Field(default=None, index=True)
    reconciled_at: datetime | None = None
    revision: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SopOperationAttempt(SQLModel, table=True):
    """追加保存逻辑动作每次本地 dispatch attempt，不改变远端命令身份。"""

    __tablename__ = "sop_operation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "node_execution_id",
            name="uq_sop_operation_attempt_execution",
        ),
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "attempt_number",
            name="uq_sop_operation_attempt_number",
        ),
        CheckConstraint(
            "status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown', "
            "'cancelled', 'reused')",
            name="ck_sop_operation_attempt_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopattempt"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    operation_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    node_execution_id: IdentifierString = Field(index=True)
    attempt_number: int = Field(ge=1)
    status: LabelString = Field(default="prepared", index=True)
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SopOperationEffect(SQLModel, table=True):
    """追加保存外部效果事实及对账/补偿 lineage，不覆盖原 Operation 历史。"""

    __tablename__ = "sop_operation_effects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_id",
            "sequence",
            name="uq_sop_operation_effect_sequence",
        ),
        CheckConstraint(
            "effect_state IN ('none', 'complete', 'unknown', 'compensated')",
            name="ck_sop_operation_effect_record_state",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopeffect"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    operation_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    logical_action_id: IdentifierString = Field(index=True)
    sequence: int = Field(ge=1)
    event_type: LabelString = Field(index=True)
    effect_state: LabelString
    external_reference: OptionalIdentifierString = None
    evidence_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    compensation_operation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionMutationRejection(SQLModel, table=True):
    """隔离保存 fencing 拒绝元数据，不记录可能敏感的业务输入输出。"""

    __tablename__ = "execution_mutation_rejections"
    __table_args__ = (
        Index(
            "ix_execution_mutation_rejection_lookup",
            "tenant_id",
            "instance_id",
            "created_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execreject"), primary_key=True)
    tenant_id: IdentifierString
    instance_id: IdentifierString
    worker_id: IdentifierString
    rejected_fencing_token: int = Field(ge=0)
    current_lease_owner: OptionalIdentifierString = None
    current_fencing_token: int = Field(ge=0)
    action: IdentifierString
    reason: LabelString = Field(default="lease_or_fence_mismatch")
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalRequest(SQLModel, table=True):
    """保存跨 SOP 复用的审批申请快照、当前状态和绑定流程版本。"""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "request_number",
            name="uq_approval_request_tenant_number",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("approvalreq"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    request_number: IdentifierString = Field(index=True)
    request_type: LabelString = Field(index=True)
    policy_key: IdentifierString = Field(index=True)
    status: LabelString = Field(default="pending", index=True)
    initiator_user_id: IdentifierString = Field(index=True)
    subject_employee_profile_id: IdentifierString = Field(index=True)
    instance_id: OptionalIdentifierString = Field(default=None, index=True)
    skill_version_id: OptionalIdentifierString = Field(default=None, index=True)
    current_step: int = Field(default=1, ge=1)
    total_steps: int = Field(default=1, ge=1)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    revision: int = Field(default=0, ge=0)
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ApprovalRequestDecision(SQLModel, table=True):
    """追加保存审批申请每一步由权威工作项形成的结构化决定。"""

    __tablename__ = "approval_request_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "request_id",
            "step_number",
            name="uq_approval_request_decision_step",
        ),
        UniqueConstraint(
            "tenant_id",
            "work_item_id",
            name="uq_approval_request_decision_work_item",
        ),
    )

    id: PrimaryKeyString = Field(
        default_factory=lambda: new_id("approvaldecision"),
        primary_key=True,
    )
    tenant_id: IdentifierString = Field(index=True)
    request_id: IdentifierString = Field(index=True)
    step_number: int = Field(ge=1, index=True)
    work_item_id: IdentifierString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    outcome: LabelString = Field(index=True)
    comment: OptionalMediumTextString = None
    created_at: datetime = Field(default_factory=utc_now)


class SopWorkItem(SQLModel, table=True):
    """保存统一 typed Attention，并兼容正式 SOP 人工节点的候选与决定契约。"""

    __tablename__ = "sop_work_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "node_execution_id",
            name="uq_sop_work_item_node_execution",
        ),
        UniqueConstraint(
            "tenant_id",
            "instance_id",
            "attention_identity",
            name="uq_attention_execution_identity",
        ),
        CheckConstraint(
            "attention_kind IN ('sop_human_task', 'clarification', 'plan_approval', "
            "'tool_approval', 'reauth', 'exception', 'publication', 'result_review')",
            name="ck_attention_kind",
        ),
        CheckConstraint(
            "attention_kind <> 'sop_human_task' OR (node_execution_id IS NOT NULL "
            "AND skill_version_id IS NOT NULL AND node_id IS NOT NULL)",
            name="ck_attention_sop_identity",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopwork"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_execution_id: OptionalIdentifierString = Field(default=None, index=True)
    skill_version_id: OptionalIdentifierString = Field(default=None, index=True)
    node_id: OptionalIdentifierString = Field(default=None, index=True)
    attention_kind: LabelString = Field(default="sop_human_task", index=True)
    attention_key: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    attention_identity: VersionString = Field(
        default_factory=lambda: new_id("attention"),
        index=True,
    )
    title: OptionalNameString = None
    source_type: LabelString = Field(default="runtime", index=True)
    source_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    allowed_commands_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    resolution_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    required: bool = True
    status: LabelString = Field(default="offered", index=True)
    owner_user_id: OptionalIdentifierString = Field(default=None, index=True)
    assignee_user_id: OptionalIdentifierString = Field(default=None, index=True)
    initiator_user_id: OptionalIdentifierString = Field(default=None, index=True)
    subject_employee_profile_id: OptionalIdentifierString = Field(default=None, index=True)
    completion_mode: LabelString = Field(default="single", index=True)
    claim_required: bool = False
    required_count: int | None = None
    exclude_initiator: bool = True
    allowed_outcomes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    outcome_options_json: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=True),
    )
    action_permissions_json: dict[str, str] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    candidate_snapshot_json: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )
    participant_scope_snapshot_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON),
    )
    outcome: OptionalLabelString = Field(default=None, index=True)
    comment: OptionalMediumTextString = None
    revision: int = Field(default=0, ge=0)
    expires_at: datetime | None = Field(default=None, index=True)
    timeout_action: LabelString = Field(default="fail", index=True)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    expired_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SopWorkItemCandidate(SQLModel, table=True):
    """保存工作项创建时按用户去重的候选快照及全部来源业务角色。"""

    __tablename__ = "sop_work_item_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "work_item_id",
            "user_id",
            name="uq_sop_work_item_candidate_user",
        ),
    )

    id: PrimaryKeyString = Field(
        default_factory=lambda: new_id("sopcandidate"),
        primary_key=True,
    )
    tenant_id: IdentifierString = Field(index=True)
    work_item_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    employee_profile_id: OptionalIdentifierString = Field(default=None, index=True)
    source_role_codes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    source_types_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class SopWorkItemDecision(SQLModel, table=True):
    """追加保存不同处理人的结构化办理结果，并按工作项和处理人防止重复提交。"""

    __tablename__ = "sop_work_item_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "work_item_id",
            "actor_user_id",
            name="uq_sop_work_item_decision_actor",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_sop_work_item_decision_idempotency",
        ),
    )

    id: PrimaryKeyString = Field(
        default_factory=lambda: new_id("sopdecision"),
        primary_key=True,
    )
    tenant_id: IdentifierString = Field(index=True)
    work_item_id: IdentifierString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    outcome: LabelString = Field(index=True)
    comment: OptionalMediumTextString = None
    idempotency_key: VersionString
    created_at: datetime = Field(default_factory=utc_now)


class SopWorkItemCommandReceipt(SQLModel, table=True):
    """记录认领、释放和完成命令的幂等结果与修订号，支持安全重放。"""

    __tablename__ = "sop_work_item_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "command_id",
            name="uq_sop_work_item_command_receipt",
        ),
    )

    id: PrimaryKeyString = Field(
        default_factory=lambda: new_id("sopcommand"),
        primary_key=True,
    )
    tenant_id: IdentifierString = Field(index=True)
    work_item_id: IdentifierString = Field(index=True)
    command_id: IdentifierString = Field(index=True)
    command_type: LabelString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    aggregate_revision: int = Field(ge=0)
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionCommand(SQLModel, table=True):
    """持久保存 cancel/steer 等统一 Execution 命令及其 CAS 结果。"""

    __tablename__ = "execution_commands"
    __table_args__ = (
        UniqueConstraint("tenant_id", "command_id", name="uq_execution_command_id"),
        CheckConstraint(
            "command_type IN ('cancel', 'steer')",
            name="ck_execution_command_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'applied', 'conflicted', 'rejected')",
            name="ck_execution_command_status",
        ),
        CheckConstraint(
            "expected_execution_revision >= 0 AND "
            "(claimed_fencing_token IS NULL OR claimed_fencing_token >= 0)",
            name="ck_execution_command_revisions",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execcmd"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    command_id: IdentifierString = Field(index=True)
    command_type: LabelString = Field(index=True)
    actor_user_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    source_type: LabelString = Field(default="api", index=True)
    source_message_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    expected_execution_revision: int = Field(ge=0)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    payload_checksum: VersionString = Field(index=True)
    status: LabelString = Field(default="pending", index=True)
    result_plan_revision_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    reason_code: OptionalIdentifierString = Field(default=None, index=True)
    claimed_by: OptionalIdentifierString = Field(default=None, index=True)
    claimed_fencing_token: int | None = Field(default=None, ge=0)
    issued_at: datetime = Field(default_factory=utc_now)
    claimed_at: datetime | None = None
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutionSignal(SQLModel, table=True):
    """保存可租约消费、退避重试和死信的 Execution 唤醒信号。"""

    __tablename__ = "execution_signals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_execution_signal_dedupe"),
        CheckConstraint(
            "signal_type IN ('command', 'attention_decided', 'timer', 'operation_settled', "
            "'external_event', 'publication_retry', 'scheduled_start', 'capacity_retry')",
            name="ck_execution_signal_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'consumed', 'dead_letter', 'discarded')",
            name="ck_execution_signal_status",
        ),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_execution_signal_lease_pair",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_execution_signal_attempts",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execsig"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    signal_type: LabelString = Field(index=True)
    dedupe_key: VersionString = Field(index=True)
    causation_type: LabelString
    causation_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    payload_checksum: VersionString = Field(index=True)
    status: LabelString = Field(default="pending", index=True)
    priority: int = Field(default=0)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=8, ge=1)
    available_at: datetime = Field(default_factory=utc_now, index=True)
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    claimed_at: datetime | None = None
    consumed_at: datetime | None = None
    last_error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DynamicTaskQuotaLease(SQLModel, table=True):
    """用数据库唯一槽位限制跨进程动态 Execution 和工具并发，不保存业务参数。"""

    __tablename__ = "dynamic_task_quota_leases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_ref",
            "slot_number",
            name="uq_dynamic_quota_scope_slot",
        ),
        UniqueConstraint(
            "tenant_id",
            "holder_type",
            "holder_id",
            "scope_type",
            name="uq_dynamic_quota_holder_scope",
        ),
        CheckConstraint(
            "scope_type IN ('tenant', 'agent', 'user', 'tool')",
            name="ck_dynamic_quota_scope_type",
        ),
        CheckConstraint(
            "holder_type IN ('execution', 'operation')",
            name="ck_dynamic_quota_holder_type",
        ),
        CheckConstraint("slot_number >= 0", name="ck_dynamic_quota_slot_nonnegative"),
        Index(
            "ix_dynamic_quota_holder",
            "tenant_id",
            "holder_type",
            "holder_id",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("quota"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    scope_type: LabelString = Field(index=True)
    scope_ref: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    slot_number: int = Field(ge=0)
    holder_type: LabelString = Field(index=True)
    holder_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    acquired_at: datetime = Field(default_factory=utc_now)


class ExecutionResult(SQLModel, table=True):
    """不可变保存一次 Execution 的验证结果，不把生成完成等同于已经发布。"""

    __tablename__ = "execution_results"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "result_revision",
            name="uq_execution_result_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            "checksum",
            name="uq_execution_result_checksum",
        ),
        CheckConstraint(
            "status IN ('verified', 'rejected')",
            name="ck_execution_result_status",
        ),
        CheckConstraint("result_revision >= 1", name="ck_execution_result_revision"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execresult"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    result_revision: int = Field(default=1, ge=1)
    status: LabelString = Field(default="verified", index=True)
    result_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    verification_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    checksum: VersionString = Field(index=True)
    created_by_step_key: OptionalIdentifierString = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionPublication(SQLModel, table=True):
    """保存结果对应用内或外部目标的必需/可选发布状态和唯一业务键。"""

    __tablename__ = "execution_publications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "publication_key", name="uq_execution_publication_key"),
        CheckConstraint(
            "target_type IN ('application', 'external_thread', 'webhook')",
            name="ck_execution_publication_target",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivering', 'settled', 'unknown', 'dead_letter', 'skipped')",
            name="ck_execution_publication_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_execution_publication_attempts"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("execpub"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    execution_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    result_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    publication_key: VersionString = Field(index=True)
    target_type: LabelString = Field(index=True)
    target_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
    )
    required: bool = Field(default=True, index=True)
    status: LabelString = Field(default="pending", index=True)
    operation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    outbox_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    attempt_count: int = Field(default=0, ge=0)
    receipt_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    settled_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EventOutbox(SQLModel, table=True):
    """以 publication key 去重保存领域事件的异步外部投递，不替代领域事实。"""

    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "publication_key", name="uq_event_outbox_key"),
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'dead_letter')",
            name="ck_event_outbox_status",
        ),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_event_outbox_lease_pair",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_event_outbox_attempts",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("outbox"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    event_id: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    publication_key: VersionString = Field(index=True)
    destination: LabelString = Field(index=True)
    payload_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    payload_checksum: VersionString = Field(index=True)
    status: LabelString = Field(default="pending", index=True)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=8, ge=1)
    available_at: datetime = Field(default_factory=utc_now, index=True)
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    last_error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    delivered_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranch(SQLModel, table=True):
    __tablename__ = "agent_skill_branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "skill_id", name="uq_agent_skill_branch"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentbranch"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    source_skill_id: IdentifierString = Field(index=True)
    base_version: VersionString = "1.0.0"
    head_version: VersionString = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: LabelString = Field(default="active", index=True)
    sync_state: LabelString = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranchVersion(SQLModel, table=True):
    __tablename__ = "agent_skill_branch_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "agent_id", "skill_id", "version", name="uq_agent_skill_branch_version"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentbranchver"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    source_skill_id: IdentifierString = Field(index=True)
    version: VersionString = Field(index=True)
    base_version: VersionString = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: LabelString = Field(default="active", index=True)
    sync_state: LabelString = Field(default="diverged", index=True)
    change_summary: OptionalMediumTextString = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralSkill(SQLModel, table=True):
    __tablename__ = "general_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_general_skill_tenant_slug"),
        CheckConstraint(
            "usage_mode IN ('atomic_execution', 'planning_guidance')",
            name="ck_general_skill_usage_mode",
        ),
        CheckConstraint(
            "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery')",
            name="ck_general_skill_visibility_scope",
        ),
        CheckConstraint("row_version >= 1", name="ck_general_skill_row_version"),
        Index(
            "ix_general_skill_owner_visibility_status",
            "tenant_id",
            "owner_user_id",
            "visibility_scope",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("genskill"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    slug: NameString = Field(index=True)
    name: NameString
    description: OptionalMediumTextString = None
    homepage: OptionalPlainTextString = None
    skill_markdown: LongTextString
    skill_files_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: LabelString = Field(default="draft", index=True)
    permissions_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    runtime_config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    usage_mode: LabelString = Field(default="atomic_execution", index=True)
    owner_user_id: OptionalIdentifierString = Field(default=None, index=True)
    visibility_scope: LabelString = Field(default="tenant_gallery", index=True)
    current_published_revision_id: OptionalIdentifierString = Field(default=None, index=True)
    row_version: int = Field(default=1, ge=1)
    planning_guidance_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    planning_guidance_checksum: OptionalVersionString = Field(default=None, index=True)
    planning_guidance_published_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralSkillRevision(SQLModel, table=True):
    """保存通用技能经确认后不可覆盖的规范化修订。"""

    __tablename__ = "general_skill_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "skill_id",
            "revision_number",
            name="uq_general_skill_revision_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "skill_id",
            "content_checksum",
            name="uq_general_skill_revision_checksum",
        ),
        CheckConstraint(
            "status IN ('draft', 'reviewing', 'published', 'rejected', 'superseded', 'revoked')",
            name="ck_general_skill_revision_status",
        ),
        CheckConstraint("revision_number >= 1", name="ck_general_skill_revision_number"),
        CheckConstraint("row_version >= 1", name="ck_general_skill_revision_row_version"),
        Index(
            "ix_general_skill_revision_lookup",
            "tenant_id",
            "skill_id",
            "status",
            "revision_number",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("gsrev"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    revision_number: int = Field(ge=1)
    content_checksum: VersionString = Field(index=True)
    manifest_checksum: VersionString = Field(index=True)
    normalized_skill_markdown: LongTextString
    parsed_metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    resource_manifest_json: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    requested_capabilities_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    source_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: LabelString = Field(default="draft", index=True)
    created_by: IdentifierString = Field(index=True)
    row_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    published_at: datetime | None = None
    revoked_at: datetime | None = None


class GeneralSkillImportJob(SQLModel, table=True):
    """保存一次有身份边界、可恢复且不可原地重试的 Skill 导入作业。"""

    __tablename__ = "general_skill_import_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "idempotency_key",
            "attempt",
            name="uq_general_skill_import_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "parent_job_id",
            "attempt",
            name="uq_general_skill_import_retry_attempt",
        ),
        CheckConstraint(
            "source_kind IN ('upload', 'github', 'skillhub', 'https', 'manual', 'agent_copy')",
            name="ck_general_skill_import_source_kind",
        ),
        CheckConstraint(
            "status IN ('created', 'fetching', 'fetched', 'normalizing', 'normalized', "
            "'analyzing', 'awaiting_approval', 'confirming', 'installed', 'failed', "
            "'cancelled', 'expired')",
            name="ck_general_skill_import_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_general_skill_import_attempt"),
        CheckConstraint("quota_bytes >= 0", name="ck_general_skill_import_quota"),
        CheckConstraint("row_version >= 1", name="ck_general_skill_import_row_version"),
        CheckConstraint("lease_token >= 0", name="ck_general_skill_import_lease_token"),
        Index(
            "ix_general_skill_import_owner_status",
            "tenant_id",
            "owner_user_id",
            "status",
            "expires_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("gsjob"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    owner_user_id: IdentifierString = Field(index=True)
    target_agent_id: IdentifierString = Field(index=True)
    source_kind: LabelString = Field(index=True)
    source_reference_redacted: OptionalPlainTextString = None
    credential_reference: OptionalIdentifierString = None
    raw_checksum: OptionalVersionString = Field(default=None, index=True)
    normalized_checksum: OptionalVersionString = Field(default=None, index=True)
    preview_checksum: OptionalVersionString = Field(default=None, index=True)
    status: LabelString = Field(default="created", index=True)
    attempt: int = Field(default=1, ge=1)
    parent_job_id: OptionalIdentifierString = Field(default=None, index=True)
    idempotency_key: IdentifierString = Field(index=True)
    quota_bytes: int = Field(default=0, ge=0)
    error_code: OptionalLabelString = None
    error_detail_redacted: OptionalMediumTextString = None
    staging_manifest_json: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    preview_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    installed_revision_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    row_version: int = Field(default=1, ge=1)
    worker_id: OptionalIdentifierString = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    lease_token: int = Field(default=0, ge=0)
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    fetched_at: datetime | None = None
    normalized_at: datetime | None = None
    analyzed_at: datetime | None = None
    confirmed_at: datetime | None = None
    terminal_at: datetime | None = None


class GeneralSkillImportQuota(SQLModel, table=True):
    """保存 tenant/user 两级可原子更新的导入并发数与暂存字节计数。"""

    __tablename__ = "general_skill_import_quotas"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_kind",
            "scope_id",
            name="uq_general_skill_import_quota_scope",
        ),
        CheckConstraint(
            "scope_kind IN ('tenant', 'user')",
            name="ck_general_skill_import_quota_scope_kind",
        ),
        CheckConstraint(
            "active_jobs >= 0 AND staged_bytes >= 0 AND row_version >= 1",
            name="ck_general_skill_import_quota_nonnegative",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("gsquota"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    scope_kind: LabelString = Field(index=True)
    scope_id: IdentifierString = Field(index=True)
    active_jobs: int = Field(default=0, ge=0)
    staged_bytes: int = Field(
        default=0,
        ge=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
    )
    row_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralSkillSourceCredential(SQLModel, table=True):
    """保存用户级私有 Skill 来源档案；密文继续由追加式 ConnectionSecret 持有。"""

    __tablename__ = "general_skill_source_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "id",
            name="uq_general_skill_source_credential_owner",
        ),
        CheckConstraint(
            "source_kind IN ('github', 'https')",
            name="ck_general_skill_source_credential_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_general_skill_source_credential_status",
        ),
        CheckConstraint("secret_revision >= 1", name="ck_general_skill_source_secret_revision"),
        CheckConstraint("row_version >= 1", name="ck_general_skill_source_row_version"),
        Index(
            "ix_general_skill_source_credential_owner_status",
            "tenant_id",
            "owner_user_id",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(
        default_factory=lambda: new_id("gssourcecred"),
        primary_key=True,
    )
    tenant_id: IdentifierString
    owner_user_id: IdentifierString
    display_name: NameString
    source_kind: LabelString
    allowed_host: PlainTextString
    secret_reference_id: IdentifierString = Field(index=True)
    secret_revision: int = Field(default=1, ge=1)
    status: LabelString = "active"
    row_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class GeneralSkillDependency(SQLModel, table=True):
    """保存经人工确认、以稳定 Skill 与 Revision 标识表达的不可变依赖边。"""

    __tablename__ = "general_skill_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "parent_revision_id",
            "child_revision_id",
            name="uq_general_skill_dependency_revision_edge",
        ),
        CheckConstraint(
            "dependency_kind IN ('required', 'optional')",
            name="ck_general_skill_dependency_kind",
        ),
        CheckConstraint(
            "source IN ('manifest', 'human_confirmed')",
            name="ck_general_skill_dependency_source",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_general_skill_dependency_status",
        ),
        Index(
            "ix_general_skill_dependency_parent",
            "tenant_id",
            "parent_skill_id",
            "parent_revision_id",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("gsdep"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    parent_skill_id: IdentifierString = Field(index=True)
    parent_revision_id: IdentifierString = Field(index=True)
    child_skill_id: IdentifierString = Field(index=True)
    child_revision_id: IdentifierString = Field(index=True)
    dependency_kind: LabelString
    source: LabelString = Field(default="human_confirmed")
    allow_user_only: bool = False
    edge_checksum: VersionString = Field(index=True)
    status: LabelString = Field(default="active", index=True)
    created_by: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgeBase(SQLModel, table=True):
    """保存知识内容及其独立于数字员工绑定的最小治理事实。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),
        Index(
            "ix_knowledge_base_tenant_owner_status",
            "tenant_id",
            "owner_user_id",
            "status",
        ),
        Index(
            "ix_knowledge_base_tenant_responsible_org",
            "tenant_id",
            "responsible_org_unit_id",
        ),
        Index(
            "ix_knowledge_base_tenant_access_status",
            "tenant_id",
            "access_scope",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kb"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    name: NameString
    description: OptionalMediumTextString = None
    owner_user_id: OptionalIdentifierString = Field(default=None, index=True)
    responsible_org_unit_id: OptionalIdentifierString = Field(default=None, index=True)
    access_scope: LabelString = Field(default="owner", index=True)
    download_policy: LabelString = Field(default="restricted", index=True)
    revision: int = Field(default=1, ge=1)
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseOrgAccess(SQLModel, table=True):
    """保存知识库允许访问的组织根及是否包含后代。"""

    __tablename__ = "knowledge_base_org_access"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "org_unit_id",
            name="uq_knowledge_base_org_access",
        ),
        Index(
            "ix_kb_org_access_tenant_org_status",
            "tenant_id",
            "org_unit_id",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kborg"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    org_unit_id: IdentifierString = Field(index=True)
    include_descendants: bool = Field(default=True)
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseVersion(SQLModel, table=True):
    __tablename__ = "knowledge_base_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "knowledge_base_id", "version", name="uq_knowledge_base_version"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kbver"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    version: VersionString = Field(default="1.0.0", index=True)
    name: NameString
    description: OptionalMediumTextString = None
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentKnowledgeBranch(SQLModel, table=True):
    __tablename__ = "agent_knowledge_branches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "agent_id", "knowledge_base_id", name="uq_agent_knowledge_branch"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentkb"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    base_version: VersionString = "1.0.0"
    head_version: VersionString = "1.0.0"
    status: LabelString = Field(default="active", index=True)
    sync_state: LabelString = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDocument(SQLModel, table=True):
    __tablename__ = "knowledge_documents"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kdoc"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    filename: NameString
    file_type: LabelString = Field(index=True)
    title: OptionalNameString = None
    status: LabelString = Field(default="processing", index=True)
    bucket_count: int = 0
    chunk_count: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: OptionalMediumTextString = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBucket(SQLModel, table=True):
    __tablename__ = "knowledge_buckets"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kbucket"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    document_id: IdentifierString = Field(index=True)
    bucket_key: NameString = Field(index=True)
    title: NameString
    summary: MediumTextString
    token_estimate: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(SQLModel, table=True):
    __tablename__ = "knowledge_chunks"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kchunk"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    document_id: IdentifierString = Field(index=True)
    bucket_id: IdentifierString = Field(index=True)
    chunk_index: int = Field(index=True)
    content: LongTextString
    summary: OptionalMediumTextString = None
    source_ref: OptionalMediumTextString = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeConcept(SQLModel, table=True):
    __tablename__ = "knowledge_concepts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_version_id",
            "concept_id",
            name="uq_knowledge_concept_version_path",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kconcept"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    document_id: OptionalIdentifierString = Field(default=None, index=True)
    concept_id: IdentifierString = Field(index=True)
    concept_type: LabelString = Field(index=True)
    title: NameString
    description: OptionalMediumTextString = None
    content_md: LongTextString
    frontmatter_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    links_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDiscoverySuggestion(SQLModel, table=True):
    __tablename__ = "knowledge_discovery_suggestions"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kdisc"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    document_id: IdentifierString = Field(index=True)
    bucket_id: OptionalIdentifierString = Field(default=None, index=True)
    suggestion_type: LabelString = Field(index=True)
    title: NameString
    status: LabelString = Field(default="pending", index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    reason: OptionalMediumTextString = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeIngestJob(SQLModel, table=True):
    __tablename__ = "knowledge_ingest_jobs"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("kjob"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    knowledge_base_id: IdentifierString = Field(index=True)
    knowledge_base_version_id: OptionalIdentifierString = Field(default=None, index=True)
    document_id: OptionalIdentifierString = Field(default=None, index=True)
    filename: NameString
    status: LabelString = Field(default="queued", index=True)
    stage: LabelString = "queued"
    progress: float = 0.0
    error: OptionalMediumTextString = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class ModelConfig(SQLModel, table=True):
    __tablename__ = "model_configs"
    __table_args__ = (
        Index(
            "uq_model_configs_tenant_default",
            "default_tenant_id",
            unique=True,
        ),
        CheckConstraint(
            "preflight_status IN ('unverified', 'ready', 'failed')",
            name="ck_model_config_preflight_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("model"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    name: NameString
    provider: LabelString = "openai_compatible"
    base_url: OptionalPlainTextString = None
    api_key_encrypted: MediumTextString
    model: NameString
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    capability_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    capability_checksum: OptionalVersionString = Field(default=None, index=True)
    preflight_status: LabelString = Field(default="unverified", index=True)
    preflight_error: OptionalMediumTextString = None
    capability_verified_at: Optional[datetime] = None
    is_default: bool = False
    default_tenant_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(128),
            Computed("CASE WHEN is_default THEN tenant_id ELSE NULL END"),
            nullable=True,
        ),
    )
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PersonaConfig(SQLModel, table=True):
    __tablename__ = "persona_configs"

    tenant_id: IdentifierString = Field(primary_key=True)
    system_prompt: LongTextString
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UIConfig(SQLModel, table=True):
    __tablename__ = "ui_configs"

    tenant_id: IdentifierString = Field(primary_key=True)
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = 1
    agent_loop_max_actions: int = 6
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentProfile(SQLModel, table=True):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agent_profile_tenant_name"),
        Index("ix_agent_profiles_tenant_owner", "tenant_id", "owner_user_id"),
        Index(
            "ix_agent_profiles_tenant_owner_status_updated",
            "tenant_id",
            "owner_user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_agent_profiles_tenant_responsible_org",
            "tenant_id",
            "responsible_org_unit_id",
        ),
        Index(
            "ix_agent_profiles_tenant_gallery_status",
            "tenant_id",
            "published_to_gallery",
            "status",
        ),
        Index(
            "ix_agent_profiles_tenant_gallery_status_category_updated",
            "tenant_id",
            "published_to_gallery",
            "status",
            "agent_category_code",
            "updated_at",
        ),
        Index(
            "ix_agent_profiles_tenant_category_status",
            "tenant_id",
            "agent_category_code",
            "status",
        ),
        Index(
            "ix_agent_profiles_tenant_category_status_updated",
            "tenant_id",
            "agent_category_code",
            "status",
            "updated_at",
        ),
        Index("ix_agent_profiles_tenant_source", "tenant_id", "source_agent_id"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agent"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    name: NameString
    description: OptionalMediumTextString = None
    persona_prompt: OptionalLongTextString = None
    original_name: OptionalNameString = None
    original_description: OptionalMediumTextString = None
    original_persona_prompt: OptionalLongTextString = None
    original_locale: OptionalLabelString = None
    is_overall: bool = Field(default=False, index=True)
    status: LabelString = Field(default="active", index=True)
    owner_user_id: OptionalIdentifierString = None
    responsible_org_unit_id: OptionalIdentifierString = Field(default=None, index=True)
    source_agent_id: OptionalIdentifierString = None
    source_agent_version: OptionalVersionString = None
    profile_revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    published_to_gallery: bool | None = Field(
        default=None,
        sa_column=Column(Boolean, nullable=False, server_default="0"),
    )
    gallery_published_at: Optional[datetime] = None
    gallery_published_by: OptionalIdentifierString = None
    agent_category_code: IdentifierString = Field(
        default="assistant",
        sa_column=Column(String(128), nullable=False, server_default="assistant"),
    )
    visibility_scope: LabelString = Field(
        default="private",
        sa_column=Column(String(64), nullable=False, server_default="private"),
    )
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentUsage(SQLModel, table=True):
    __tablename__ = "agent_usages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "agent_id", name="uq_agent_usage_user_agent"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentuse"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentModelBinding(SQLModel, table=True):
    __tablename__ = "agent_model_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "role", name="uq_agent_model_binding"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentmodel"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    role: LabelString = Field(default="default", index=True)
    model_config_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentResourceBinding(SQLModel, table=True):
    __tablename__ = "agent_resource_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "agent_id", "resource_type", "resource_id", name="uq_agent_resource"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentres"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    resource_type: LabelString = Field(index=True)
    resource_id: IdentifierString = Field(index=True)
    status: LabelString = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    row_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectionSecret(SQLModel, table=True):
    """保存 Connector 凭据密文；业务档案仅持有其不透明引用和修订号。"""

    __tablename__ = "connection_secrets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "reference_id",
            "revision",
            name="uq_connection_secret_revision",
        ),
        CheckConstraint("revision >= 1", name="ck_connection_secret_revision"),
        CheckConstraint(
            "status IN ('active', 'superseded', 'revoked')",
            name="ck_connection_secret_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connsecret"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    reference_id: IdentifierString = Field(index=True)
    encrypted_payload: LongTextString
    revision: int = Field(default=1, ge=1)
    status: LabelString = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class ConnectionProfile(SQLModel, table=True):
    """定义租户内稳定的外部账号身份、授权快照和可观测健康状态。"""

    __tablename__ = "connection_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "account_id",
            name="uq_connection_profile_account",
        ),
        CheckConstraint("revision >= 1", name="ck_connection_profile_revision"),
        CheckConstraint("secret_revision >= 1", name="ck_connection_profile_secret_revision"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'reauth_required')",
            name="ck_connection_profile_status",
        ),
        CheckConstraint(
            "health_status IN ('unverified', 'healthy', 'degraded', 'unhealthy')",
            name="ck_connection_profile_health",
        ),
        Index(
            "ix_connection_profiles_tenant_provider_status",
            "tenant_id",
            "provider",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connprofile"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    account_id: IdentifierString = Field(index=True)
    display_name: NameString
    secret_ref_id: IdentifierString = Field(index=True)
    secret_revision: int = Field(default=1, ge=1)
    callback_configured: bool = Field(default=False, index=True)
    required_scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    granted_scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    tool_allowlist_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: LabelString = Field(default="active", index=True)
    health_status: LabelString = Field(default="unverified", index=True)
    health_error_code: OptionalIdentifierString = None
    rate_limited_until: datetime | None = None
    last_checked_at: datetime | None = None
    last_healthy_at: datetime | None = None
    revision: int = Field(default=1, ge=1)
    created_by_user_id: IdentifierString = Field(index=True)
    updated_by_user_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorInboundEvent(SQLModel, table=True):
    """持久保存已验签的 Connector 入站事件，供异步消费与幂等重放。"""

    __tablename__ = "connector_inbound_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "profile_id",
            "external_event_id",
            name="uq_connector_inbound_external_event",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed', 'dead_letter')",
            name="ck_connector_inbound_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_connector_inbound_attempts"),
        Index(
            "ix_connector_inbound_dispatch",
            "status",
            "available_at",
            "created_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connin"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    external_event_id: str = Field(sa_column=Column(String(255), nullable=False, index=True))
    payload_checksum: VersionString = Field(index=True)
    encrypted_payload: LongTextString
    event_type: LabelString = Field(index=True)
    sender_ref_hash: OptionalVersionString = Field(default=None, index=True)
    status: LabelString = Field(default="pending", index=True)
    attempt_count: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now, index=True)
    last_error_code: OptionalIdentifierString = None
    processed_at: datetime | None = None
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_until: datetime | None = Field(default=None, index=True)
    thread_binding_id: OptionalIdentifierString = Field(default=None, index=True)
    session_id: OptionalIdentifierString = Field(default=None, index=True)
    message_id: OptionalIdentifierString = Field(default=None, index=True)
    execution_id: OptionalIdentifierString = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorPrincipalBinding(SQLModel, table=True):
    """把已验签外部发送者摘要显式映射到同租户活动用户。"""

    __tablename__ = "connector_principal_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "profile_id",
            "sender_ref_hash",
            name="uq_connector_principal_sender",
        ),
        CheckConstraint("revision >= 1", name="ck_connector_principal_revision"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connprincipal"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    sender_ref_hash: VersionString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    enabled: bool = Field(default=True, index=True)
    revision: int = Field(default=1, ge=1)
    created_by_user_id: IdentifierString = Field(index=True)
    updated_by_user_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorInboundRoute(SQLModel, table=True):
    """为一个入站连接档案指定唯一 Agent，禁止运行时猜测绑定。"""

    __tablename__ = "connector_inbound_routes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "profile_id", name="uq_connector_inbound_route_profile"
        ),
        CheckConstraint("revision >= 1", name="ck_connector_inbound_route_revision"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connroute"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    enabled: bool = Field(default=True, index=True)
    revision: int = Field(default=1, ge=1)
    created_by_user_id: IdentifierString = Field(index=True)
    updated_by_user_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorThreadBinding(SQLModel, table=True):
    """关联外部发送者、平台会话和加密回复目标，供恢复与定向回发。"""

    __tablename__ = "connector_thread_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "profile_id",
            "sender_ref_hash",
            "agent_id",
            name="uq_connector_thread_sender_agent",
        ),
        UniqueConstraint("tenant_id", "session_id", name="uq_connector_thread_session"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_connector_thread_status"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connthread"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    sender_ref_hash: VersionString = Field(index=True)
    encrypted_recipient_ref: LongTextString
    user_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    status: LabelString = Field(default="active", index=True)
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_until: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectorOutboundDelivery(SQLModel, table=True):
    """保存普通回答或 Execution publication 的外部回发 outbox 状态。"""

    __tablename__ = "connector_outbound_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_type", "source_ref", name="uq_connector_outbound_source"
        ),
        CheckConstraint(
            "source_type IN ('assistant_message', 'execution_publication')",
            name="ck_connector_outbound_source_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivering', 'settled', 'unknown', 'dead_letter')",
            name="ck_connector_outbound_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_connector_outbound_attempts"),
        Index(
            "ix_connector_outbound_dispatch", "status", "available_at", "created_at"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connout"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    provider: LabelString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    thread_binding_id: IdentifierString = Field(index=True)
    source_type: LabelString = Field(index=True)
    source_ref: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    payload_checksum: VersionString = Field(index=True)
    status: LabelString = Field(default="pending", index=True)
    attempt_count: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now, index=True)
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_until: datetime | None = Field(default=None, index=True)
    receipt_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    settled_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentConnectionBinding(SQLModel, table=True):
    """把明确 Connector 账号绑定给同租户 Agent，并分别收窄 scope 与动作。"""

    __tablename__ = "agent_connection_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_id",
            "profile_id",
            name="uq_agent_connection_binding",
        ),
        Index(
            "ix_agent_connection_bindings_resolve",
            "tenant_id",
            "agent_id",
            "enabled",
        ),
        CheckConstraint("revision >= 1", name="ck_agent_connection_binding_revision"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("agentconn"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    allowed_scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    allowed_actions_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True, index=True)
    revision: int = Field(default=1, ge=1)
    created_by_user_id: IdentifierString = Field(index=True)
    updated_by_user_id: IdentifierString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConnectionCommandReceipt(SQLModel, table=True):
    """保存连接管理命令的安全语义摘要与成功响应，供网络重放返回原始结果。"""

    __tablename__ = "connection_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "command_id",
            name="uq_connection_command_receipt",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("conncmd"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    command_id: IdentifierString = Field(index=True)
    command_type: LabelString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    payload_checksum: VersionString
    resource_type: LabelString
    resource_id: IdentifierString = Field(index=True)
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class ConnectionOAuthState(SQLModel, table=True):
    """保存一次性 Slack OAuth state 的摘要及完成连接命令所需的服务端上下文。"""

    __tablename__ = "connection_oauth_states"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_connection_oauth_state_hash"),
        UniqueConstraint(
            "tenant_id", "command_id", name="uq_connection_oauth_tenant_command"
        ),
        CheckConstraint(
            "flow_type IN ('create', 'reauthorize', 'reauthorize_attention')",
            name="ck_connection_oauth_flow_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'consumed', 'failed')",
            name="ck_connection_oauth_state_status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("connoauth"), primary_key=True)
    state_hash: VersionString = Field(index=True)
    encrypted_state: LongTextString
    tenant_id: IdentifierString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    flow_type: LabelString = Field(index=True)
    profile_id: OptionalIdentifierString = Field(default=None, index=True)
    attention_id: OptionalIdentifierString = Field(default=None, index=True)
    display_name: OptionalNameString = None
    command_id: IdentifierString = Field(index=True)
    expected_profile_revision: int = Field(default=0, ge=0)
    expected_attention_revision: int | None = None
    required_scopes_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: LabelString = Field(default="pending", index=True)
    expires_at: datetime = Field(index=True)
    consumed_at: datetime | None = None
    error_code: OptionalIdentifierString = None
    created_at: datetime = Field(default_factory=utc_now)


class Tool(SQLModel, table=True):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tool_tenant_name"),)

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    name: NameString = Field(index=True)
    display_name: OptionalNameString = None
    description: OptionalMediumTextString = None
    bucket: LabelString = Field(default="未分桶", index=True)
    tool_type: LabelString = Field(default="http", index=True)
    method: LabelString
    url: PlainTextString
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    auth_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    input_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    allowed_skills_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    required_permission_code: OptionalIdentifierString = Field(default=None, index=True)
    permission_authorization_mode: LabelString = Field(default="caller_and_agent", index=True)
    reliability_contract_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    reliability_checksum: OptionalVersionString = Field(default=None, index=True)
    reliability_published_at: Optional[datetime] = None
    mcp_server_id: OptionalIdentifierString = Field(default=None, index=True)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MCPServer(SQLModel, table=True):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_server_tenant_name"),)

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("mcpsrv"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    name: NameString = Field(index=True)
    display_name: OptionalNameString = None
    description: OptionalMediumTextString = None
    bucket: LabelString = Field(default="MCP 工具", index=True)
    # 连接方式：stdio / streamable_http / sse / builtin
    transport: LabelString = Field(default="streamable_http", index=True)
    # streamable_http / sse 使用
    url: OptionalPlainTextString = None
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # stdio 使用
    command: OptionalPlainTextString = None
    args_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    env_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    cwd: OptionalPlainTextString = None
    # 最近一次发现的原始工具定义（预览/审计用）
    discovered_tools_json: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    last_synced_at: Optional[datetime] = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MockOrder(SQLModel, table=True):
    __tablename__ = "mock_orders"

    order_id: PrimaryKeyString = Field(primary_key=True)
    user_id: OptionalIdentifierString = Field(default=None, index=True)
    product_id: OptionalNameString = Field(default=None, index=True)
    sku_id: OptionalNameString = None
    quantity: int = 1
    status: LabelString = Field(default="created", index=True)
    payment_status: OptionalLabelString = None
    order_status: OptionalLabelString = None
    signed_days: int = 0
    refundable: bool = True
    total_amount: float = 0.0
    currency: LabelString = "CNY"
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChatSession(SQLModel, table=True):
    __tablename__ = "sessions"

    id: PrimaryKeyString = Field(primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    user_id: OptionalIdentifierString = Field(default=None, index=True)
    agent_id: OptionalIdentifierString = None
    agent_profile_revision: Optional[int] = None
    capability_snapshot_json: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
    )
    origin: OptionalLabelString = None
    title: OptionalNameString = None
    active_skill_id: OptionalIdentifierString = None
    active_step_id: OptionalIdentifierString = None
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill_stack_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    pending_tasks_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    resume_after_answer_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    awaiting_input_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    knowledge_context_json: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    context_state_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    summary: OptionalMediumTextString = None
    last_agent_question: OptionalMediumTextString = None
    status: LabelString = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HumanHandoffRequest(SQLModel, table=True):
    __tablename__ = "human_handoff_requests"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("handoff"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    agent_id: OptionalIdentifierString = Field(default=None, index=True)
    requester_user_id: OptionalIdentifierString = Field(default=None, index=True)
    assignee_user_id: OptionalIdentifierString = Field(default=None, index=True)
    trigger_skill_id: OptionalIdentifierString = Field(default=None, index=True)
    trigger_step_id: OptionalIdentifierString = Field(default=None, index=True)
    context_summary: OptionalMediumTextString = None
    pending_question: OptionalMediumTextString = None
    status: LabelString = Field(default="pending", index=True)
    human_reply: OptionalMediumTextString = None
    resume_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    answered_at: Optional[datetime] = None


class ScheduledTask(SQLModel, table=True):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        Index(
            "ix_sched_tasks_tenant_agent_status_updated",
            "tenant_id",
            "agent_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_sched_tasks_tenant_agent_creator_status_updated",
            "tenant_id",
            "agent_id",
            "created_by_user_id",
            "status",
            "updated_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sched"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    created_by_user_id: IdentifierString = Field(index=True)
    title: NameString
    prompt: LongTextString
    description: OptionalMediumTextString = None
    schedule_type: LabelString = Field(default="daily", index=True)
    schedule_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    timezone: NameString = Field(default="Asia/Shanghai", index=True)
    rrule: OptionalPlainTextString = None
    status: LabelString = Field(default="active", index=True)
    concurrency_policy: LabelString = Field(default="forbid", index=True)
    misfire_policy: LabelString = Field(default="coalesce", index=True)
    max_runs: Optional[int] = None
    end_at: Optional[datetime] = Field(default=None, index=True)
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    last_run_at: Optional[datetime] = Field(default=None, index=True)
    last_status: OptionalLabelString = Field(default=None, index=True)
    run_count: int = 0
    lease_owner: OptionalIdentifierString = Field(default=None, index=True)
    lease_until: Optional[datetime] = Field(default=None, index=True)
    source_session_id: OptionalIdentifierString = Field(default=None, index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduledTaskRun(SQLModel, table=True):
    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        UniqueConstraint(
            "scheduled_task_id", "scheduled_for", name="uq_scheduled_task_run_due_time"
        ),
        UniqueConstraint(
            "tenant_id", "source_ref", name="uq_scheduled_task_run_source_ref"
        ),
        UniqueConstraint(
            "tenant_id", "execution_id", name="uq_scheduled_task_run_execution"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'skipped')",
            name="ck_scheduled_task_run_status",
        ),
        CheckConstraint(
            "source_kind IN ('schedule', 'manual', 'legacy')",
            name="ck_scheduled_task_run_source_kind",
        ),
        Index(
            "ix_sched_runs_tenant_agent_scheduled",
            "tenant_id",
            "agent_id",
            "scheduled_for",
        ),
        Index(
            "ix_sched_runs_tenant_agent_user_scheduled",
            "tenant_id",
            "agent_id",
            "user_id",
            "scheduled_for",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("schedrun"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    scheduled_task_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    session_id: OptionalIdentifierString = Field(default=None, index=True)
    execution_id: OptionalIdentifierString = Field(default=None, index=True)
    source_kind: LabelString = Field(default="schedule", index=True)
    source_ref: str = Field(sa_column=Column(String(512), nullable=False, index=True))
    source_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_checksum: VersionString = Field(index=True)
    scheduled_for: datetime = Field(index=True)
    status: LabelString = Field(default="queued", index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    result_summary: OptionalMediumTextString = None
    error: OptionalMediumTextString = None
    trace_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StandingApprovalRule(SQLModel, table=True):
    """保存受管调度任务对精确外部目标和受限参数的长期批准规则。"""

    __tablename__ = "standing_approval_rules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "active_scope_key",
            name="uq_standing_rule_active_scope",
        ),
        CheckConstraint("risk_class = 'external_write'", name="ck_standing_rule_risk"),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_standing_rule_status",
        ),
        CheckConstraint("revision >= 1", name="ck_standing_rule_revision"),
        Index(
            "ix_standing_rules_active_lookup",
            "tenant_id",
            "source_schedule_id",
            "agent_id",
            "status",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_standing_rules_target_lookup",
            "tenant_id",
            "tool_id",
            "target_hash",
            "status",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("standing"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: IdentifierString = Field(index=True)
    source_schedule_id: IdentifierString = Field(index=True)
    source_schedule_checksum: VersionString = Field(index=True)
    profile_id: IdentifierString = Field(index=True)
    binding_id: IdentifierString = Field(index=True)
    tool_id: str = Field(sa_column=Column(String(255), nullable=False, index=True))
    tool_snapshot_checksum: VersionString = Field(index=True)
    risk_class: LabelString = Field(default="external_write", index=True)
    target_type: LabelString = Field(index=True)
    canonical_target: str = Field(sa_column=Column(String(512), nullable=False))
    target_hash: VersionString = Field(index=True)
    argument_constraints_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    active_scope_key: OptionalVersionString = Field(default=None, index=True)
    valid_from: datetime = Field(index=True)
    valid_to: datetime = Field(index=True)
    status: LabelString = Field(default="active", index=True)
    revision: int = Field(default=1, ge=1)
    created_by_user_id: IdentifierString = Field(index=True)
    revoked_by_user_id: OptionalIdentifierString = Field(default=None, index=True)
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StandingApprovalCommandReceipt(SQLModel, table=True):
    """保存长期批准创建和撤销命令的幂等语义摘要与结果。"""

    __tablename__ = "standing_approval_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "command_id",
            name="uq_standing_approval_command_receipt",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("standingcmd"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    command_id: IdentifierString = Field(index=True)
    command_type: LabelString = Field(index=True)
    actor_user_id: IdentifierString = Field(index=True)
    payload_checksum: VersionString
    rule_id: IdentifierString = Field(index=True)
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    role: LabelString
    content: LongTextString
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MessageFeedback(SQLModel, table=True):
    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_feedback_message_user"),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("fb"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    message_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    rating: LabelString = Field(index=True)
    analysis_status: LabelString = Field(default="pending", index=True)
    analysis_bucket: OptionalLabelString = Field(default=None, index=True)
    analysis_reason: OptionalMediumTextString = None
    analysis_summary: OptionalMediumTextString = None
    analysis_confidence: Optional[float] = None
    analysis_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    analyzed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillFeedback(SQLModel, table=True):
    __tablename__ = "skill_feedback"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "message_id", "user_id", name="uq_skill_feedback_message_user"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("skillfb"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    skill_version: OptionalVersionString = Field(default=None, index=True)
    step_id: OptionalIdentifierString = Field(default=None, index=True)
    session_id: IdentifierString = Field(index=True)
    message_id: IdentifierString = Field(index=True)
    user_id: IdentifierString = Field(index=True)
    rating: LabelString = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ManagementAuditLog(SQLModel, table=True):
    """保存独立于聊天会话、只追加且已脱敏的管理审计事实。"""

    __tablename__ = "management_audit_logs"
    __table_args__ = (
        Index(
            "ix_management_audit_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_management_audit_tenant_actor_created",
            "tenant_id",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "ix_management_audit_tenant_action_created",
            "tenant_id",
            "action",
            "created_at",
        ),
        Index(
            "ix_management_audit_tenant_resource_created",
            "tenant_id",
            "resource_type",
            "resource_id",
            "created_at",
        ),
        Index(
            "ix_management_audit_tenant_org_created",
            "tenant_id",
            "target_org_unit_id",
            "created_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("audit"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    actor_user_id: OptionalIdentifierString = Field(default=None, index=True)
    actor_type: LabelString = Field(default="user", index=True)
    actor_display_name: OptionalNameString = None
    action: IdentifierString = Field(index=True)
    action_kind: LabelString = Field(index=True)
    outcome: LabelString = Field(index=True)
    resource_type: IdentifierString = Field(index=True)
    resource_id: OptionalIdentifierString = Field(default=None, index=True)
    target_org_unit_id: OptionalIdentifierString = Field(default=None, index=True)
    permission_code: OptionalIdentifierString = Field(default=None, index=True)
    permission_source: OptionalLabelString = None
    request_id: OptionalIdentifierString = Field(default=None, index=True)
    correlation_id: OptionalIdentifierString = Field(default=None, index=True)
    before_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    after_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    detail_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, index=True)


class AgentEvent(SQLModel, table=True):
    __tablename__ = "agent_events"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_agent_event_schema_version"),
        CheckConstraint(
            "aggregate_revision IS NULL OR aggregate_revision >= 0",
            name="ck_agent_event_aggregate_revision",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    event_type: LabelString = Field(index=True)
    schema_version: int = Field(default=1, ge=1)
    aggregate_type: OptionalLabelString = Field(default=None, index=True)
    aggregate_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    aggregate_revision: int | None = Field(default=None, ge=0)
    correlation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    causation_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    payload_checksum: OptionalVersionString = Field(default=None, index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_tenant_agent_user_updated",
            "tenant_id",
            "agent_id",
            "user_id",
            "updated_at",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    agent_id: OptionalIdentifierString = Field(default=None, index=True)
    user_id: IdentifierString = Field(index=True)
    username: OptionalNameString = Field(default=None, index=True)
    session_id: OptionalIdentifierString = Field(default=None, index=True)
    kind: LabelString = Field(default="conversation", index=True)
    content: LongTextString
    importance: float = 0.5
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
