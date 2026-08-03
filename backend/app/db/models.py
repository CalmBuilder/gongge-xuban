"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : models.py
@CallChain  : API/Seed/Workers → SQLModel Session → models.py → SQLAlchemy Engine
@Description: 定义应用持久化使用的 SQLModel 实体。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import Boolean, Column, Computed, Index, Integer, JSON, String, UniqueConstraint
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
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopinst"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    skill_id: IdentifierString = Field(index=True)
    skill_version_id: IdentifierString = Field(index=True)
    skill_version: VersionString
    definition_checksum: VersionString
    run_number: int = Field(default=1, ge=1)
    status: LabelString = Field(default="created", index=True)
    current_node_id: OptionalIdentifierString = Field(default=None, index=True)
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    context_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    revision: int = Field(default=0, ge=0)
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
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopnode"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_id: IdentifierString = Field(index=True)
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


class SopOperation(SQLModel, table=True):
    """保存外部工具副作用的幂等命令、执行状态和结构化回执。"""

    __tablename__ = "sop_operations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_sop_operation_tenant_idempotency"
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopop"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_execution_id: IdentifierString = Field(index=True)
    operation_name: NameString = Field(index=True)
    idempotency_key: VersionString
    status: LabelString = Field(default="prepared", index=True)
    request_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    external_reference: OptionalIdentifierString = Field(default=None, index=True)
    revision: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    """保存人工节点的候选快照、分配状态、完成策略和最终结构化结果。"""

    __tablename__ = "sop_work_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "node_execution_id",
            name="uq_sop_work_item_node_execution",
        ),
    )

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("sopwork"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    instance_id: IdentifierString = Field(index=True)
    node_execution_id: IdentifierString = Field(index=True)
    skill_version_id: IdentifierString = Field(index=True)
    node_id: IdentifierString = Field(index=True)
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
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_general_skill_tenant_slug"),)

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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    scheduled_for: datetime = Field(index=True)
    status: LabelString = Field(default="queued", index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    result_summary: OptionalMediumTextString = None
    error: OptionalMediumTextString = None
    trace_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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

    id: PrimaryKeyString = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    tenant_id: IdentifierString = Field(index=True)
    session_id: IdentifierString = Field(index=True)
    event_type: LabelString = Field(index=True)
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
