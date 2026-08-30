"""
@Time       : 2026/08/29 21:05
@Author     : zhanglp8181
@File       : test_agent_identity.py
@CallChain  : Agent 管理/Skill 绑定 API → project_agent_governance → 组织关系事实
@Description: 验证能力分身、待组织化 Agent、组织数字员工和模板的集中形态投影。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.identity import (
    agent_organization_relationship_checksum,
    project_agent_governance,
)
from app.api.agents import (
    list_agent_organizationization_options,
    page_managed_agents,
    preview_agent_organizationization,
)
from app.agents.organizationization import (
    OrganizationizationError,
    apply_agent_organizationization,
)
from app.agents.schema import AgentOrganizationizationRequest
from app.db.models import (
    AgentOrganizationizationCommand,
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    OrganizationUnit,
    PublicationRelease,
    Tenant,
    User,
)


def _identity_db() -> Session:
    """创建包含个人、组织和模板投影所需正式关系表的隔离会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_identity", name="Identity tenant"))
    db.add_all(
        [
            User(
                id="identity_admin",
                tenant_id="tenant_identity",
                username="admin",
                role="admin",
                password_hash="unused",
            ),
            User(
                id="identity_owner",
                tenant_id="tenant_identity",
                username="owner",
                role="member",
                password_hash="unused",
            ),
            User(
                id="identity_supervisor_user",
                tenant_id="tenant_identity",
                username="supervisor",
                role="member",
                password_hash="unused",
            ),
        ]
    )
    db.add(
        OrganizationUnit(
            id="identity_org",
            tenant_id="tenant_identity",
            code="identity-org",
            name="Identity organization",
            unit_type_code="department",
            tree_path="/identity-org",
            is_root=True,
            root_tenant_id="tenant_identity",
        )
    )
    db.add(
        BusinessRole(
            id="identity_role",
            tenant_id="tenant_identity",
            role_code="identity.operator",
            name="Identity operator",
        )
    )
    db.add(
        EmployeeProfile(
            id="identity_supervisor_profile",
            tenant_id="tenant_identity",
            user_id="identity_supervisor_user",
            employee_id="IDENTITY-SUPERVISOR",
            employee_name="Identity supervisor",
        )
    )
    db.add_all(
        [
            AgentProfile(
                id="identity_avatar",
                tenant_id="tenant_identity",
                name="能力分身",
                owner_user_id="identity_owner",
            ),
            AgentProfile(
                id="identity_template",
                tenant_id="tenant_identity",
                name="模板",
                owner_user_id=None,
                agent_category_code="professional",
            ),
            AgentProfile(
                id="identity_imported_template",
                tenant_id="tenant_identity",
                name="导入专家模板",
                owner_user_id="identity_supervisor_user",
                agent_category_code="professional",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "import_batch_id": "expert-batch-1",
                    "owner_semantics": "technical_import_admin",
                },
            ),
            AgentProfile(
                id="identity_pending",
                tenant_id="tenant_identity",
                name="待组织化",
                owner_user_id="identity_owner",
                responsible_org_unit_id="identity_org",
            ),
            AgentProfile(
                id="identity_employee",
                tenant_id="tenant_identity",
                name="组织员工",
                owner_user_id="identity_owner",
                responsible_org_unit_id="identity_org",
            ),
        ]
    )
    db.add(
        AgentRoleBinding(
            id="identity_role_binding",
            tenant_id="tenant_identity",
            agent_id="identity_employee",
            business_role_id="identity_role",
            supervisor_employee_profile_id="identity_supervisor_profile",
            assignment_mode="execute",
        )
    )
    db.add(
        PublicationRelease(
            id="identity_release",
            tenant_id="tenant_identity",
            approved_request_id="identity_request",
            resource_type="agent",
            resource_id="identity_employee",
            snapshot_kind="agent",
            snapshot_id="identity_snapshot",
            snapshot_checksum="a" * 64,
        )
    )
    db.commit()
    return db


def test_agent_governance_projection_distinguishes_four_forms() -> None:
    """验证形态由正式 owner、组织关系和 Release 共同决定，而不是由名称/分类决定。"""

    db = _identity_db()

    assert project_agent_governance(db, db.get(AgentProfile, "identity_avatar")).form == "capability_avatar"
    assert project_agent_governance(db, db.get(AgentProfile, "identity_template")).form == "template"
    assert project_agent_governance(
        db,
        db.get(AgentProfile, "identity_imported_template"),
    ).form == "template"
    pending = project_agent_governance(db, db.get(AgentProfile, "identity_pending"))
    assert pending.form == "organization_pending"
    assert "active_role_and_supervisor_required" in pending.reasons
    employee = project_agent_governance(db, db.get(AgentProfile, "identity_employee"))
    assert employee.form == "organization_employee"
    assert employee.organization_release_id == "identity_release"
    assert employee.active_role_binding_ids == ("identity_role_binding",)

    employee_preview = preview_agent_organizationization(
        agent_id="identity_employee",
        tenant_id="tenant_identity",
        db=db,
        current_user=db.get(User, "identity_owner"),
    )
    assert employee_preview.active_role_code == "identity.operator"
    assert employee_preview.active_supervisor_employee_profile_id == "identity_supervisor_profile"


def test_organizationization_options_follow_governance_boundary() -> None:
    """验证组织化向导只返回当前租户活动组织、业务角色和监督员工。"""

    db = _identity_db()
    admin = db.get(User, "identity_admin")
    assert admin is not None

    options = list_agent_organizationization_options(
        tenant_id="tenant_identity",
        db=db,
        current_user=admin,
    )
    assert [(item.id, item.name) for item in options.organizations] == [
        ("identity_org", "Identity organization"),
    ]
    assert [(item.role_code, item.name) for item in options.roles] == [
        ("identity.operator", "Identity operator"),
    ]
    assert [(item.id, item.employee_id) for item in options.supervisors] == [
        ("identity_supervisor_profile", "IDENTITY-SUPERVISOR"),
    ]


def test_management_page_exposes_capability_and_organization_views() -> None:
    """验证管理页两个身份分栏分别返回个人能力分身和组织化前置状态。"""

    db = _identity_db()
    owner = db.get(User, "identity_owner")
    assert owner is not None

    capability_page = page_managed_agents(
        tenant_id="tenant_identity",
        view="capability",
        q=None,
        expert_source=None,
        expert_department=None,
        expert_direction=None,
        page=1,
        page_size=12,
        db=db,
        current_user=owner,
    )
    assert {item.id for item in capability_page.items} == {
        "identity_avatar",
        "identity_pending",
    }
    assert {item.governance_form for item in capability_page.items} == {
        "capability_avatar",
        "organization_pending",
    }
    assert capability_page.governance_counts["capability_avatar"] == 1

    organization_page = page_managed_agents(
        tenant_id="tenant_identity",
        view="organization",
        q=None,
        expert_source=None,
        expert_department=None,
        expert_direction=None,
        page=1,
        page_size=12,
        db=db,
        current_user=owner,
    )
    assert {item.id for item in organization_page.items} == {
        "identity_pending",
        "identity_employee",
    }
    assert {item.governance_form for item in organization_page.items} == {
        "organization_pending",
        "organization_employee",
    }
    assert organization_page.total == 2


def test_organizationization_preview_exposes_missing_facts_without_mutation() -> None:
    """验证组织化预览只汇总事实，不把能力分身静默改成组织员工。"""

    db = _identity_db()
    owner = db.get(User, "identity_owner")
    assert owner is not None

    preview = preview_agent_organizationization(
        agent_id="identity_pending",
        tenant_id="tenant_identity",
        db=db,
        current_user=owner,
    )
    assert preview.governance_form == "organization_pending"
    assert preview.can_submit is False
    assert {
        item.code for item in preview.requirements if not item.satisfied
    } == {
        "active_role_and_supervisor_required",
        "active_publication_release_required",
    }
    assert db.get(AgentProfile, "identity_pending").responsible_org_unit_id == "identity_org"


def test_organizationization_config_is_atomic_cas_and_idempotent() -> None:
    """验证责任组织、角色和监督者一次提交，旧预览与重复命令都不会产生半成品。"""

    db = _identity_db()
    admin = db.get(User, "identity_admin")
    pending = db.get(AgentProfile, "identity_pending")
    assert admin is not None and pending is not None
    request = AgentOrganizationizationRequest(
        tenant_id="tenant_identity",
        command_id="identity-organizationize-1",
        expected_profile_revision=pending.profile_revision,
        expected_relationship_checksum=agent_organization_relationship_checksum(db, pending),
        responsible_org_unit_id="identity_org",
        role_code="identity.operator",
        supervisor_employee_profile_id="identity_supervisor_profile",
        assignment_mode="execute",
    )

    result = apply_agent_organizationization(
        db,
        agent_id=pending.id,
        request=request,
        actor=admin,
    )
    assert result.result_status == "configured"
    assert result.active_role_binding_id
    configured = db.get(AgentProfile, pending.id)
    assert configured is not None
    assert configured.responsible_org_unit_id == "identity_org"
    assert configured.profile_revision == request.expected_profile_revision + 1
    assert len(db.exec(select(AgentRoleBinding).where(AgentRoleBinding.agent_id == pending.id)).all()) == 1

    replay = apply_agent_organizationization(
        db,
        agent_id=pending.id,
        request=request,
        actor=admin,
    )
    assert replay == result
    assert db.exec(
        select(AgentOrganizationizationCommand).where(
            AgentOrganizationizationCommand.command_id == request.command_id
        )
    ).one().result_json["result_status"] == "configured"


def test_organizationization_rejects_stale_relationship_without_writing() -> None:
    """验证组织关系在预览后变化时拒绝提交，并保持 Agent 与绑定原状。"""

    db = _identity_db()
    admin = db.get(User, "identity_admin")
    pending = db.get(AgentProfile, "identity_pending")
    assert admin is not None and pending is not None
    stale_checksum = agent_organization_relationship_checksum(db, pending)
    pending.responsible_org_unit_id = None
    db.add(pending)
    db.commit()
    request = AgentOrganizationizationRequest(
        tenant_id="tenant_identity",
        command_id="identity-organizationize-stale",
        expected_profile_revision=pending.profile_revision,
        expected_relationship_checksum=stale_checksum,
        responsible_org_unit_id="identity_org",
        role_code="identity.operator",
        supervisor_employee_profile_id="identity_supervisor_profile",
    )

    try:
        apply_agent_organizationization(db, agent_id=pending.id, request=request, actor=admin)
    except OrganizationizationError as error:
        assert error.code == "AGENT_RELATIONSHIP_STALE"
    else:
        raise AssertionError("stale organizationization should be rejected")
    assert db.get(AgentProfile, pending.id).responsible_org_unit_id is None
    assert db.exec(select(AgentRoleBinding).where(AgentRoleBinding.agent_id == pending.id)).all() == []
