"""
@Time       : 2026/07/22 17:05
@Author     : zhanglp8181
@File       : demo_sop_versions.py
@CallChain  : seed_demo_data → 演示身份/版本升级 → EmployeeProfile/SkillVersion
@Description: 幂等准备演示员工身份、业务角色和已贯通的确定性 SOP 发布版本。
"""

from __future__ import annotations

from copy import deepcopy

from sqlmodel import Session, select

from app.config import get_settings
from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    sync_branch_from_overall,
)
from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    AgentSkillBranch,
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    Tool,
    User,
    utc_now,
)
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.versioning import write_skill_version
from app.organization.permissions import (
    ensure_builtin_permission_catalog,
    sync_role_permissions,
)


EXPENSE_QUOTA_SKILL_ID = "skill_expense_quota_query"
EXPENSE_QUOTA_IDENTITY_VERSION = "2.3.0"
FINANCE_EXPENSE_SPECIALIST_ROLE = "finance_expense_specialist"
TRAVEL_REIMBURSEMENT_SKILL_ID = "expense_travel_reimbursement"
TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION = "2.1.0"
LEAVE_BALANCE_SKILL_ID = "skill_leave_balance_query"
LEAVE_BALANCE_DETERMINISTIC_VERSION = "2.2.0"
LEAVE_APPLICATION_SKILL_ID = "leave_apply_v1"
LEAVE_APPLICATION_DETERMINISTIC_VERSION = "2.1.0"
OVERTIME_COMPENSATORY_SKILL_ID = "skill_overtime_compensatory_leave"
OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION = "3.1.0"
HR_LEAVE_SPECIALIST_ROLE = "hr_leave_specialist"
HR_CERTIFICATE_SKILL_ID = "skill_hr_cert_issue_001"
HR_CERTIFICATE_DETERMINISTIC_VERSION = "2.0.0"
HR_CERTIFICATE_OPERATOR_ROLE = "hr_certificate_operator"
HR_CERTIFICATE_REVIEWER_ROLE = "hr_certificate_reviewer"
CLAUSE_MODIFICATION_SKILL_ID = "skill_clause_modification"
CLAUSE_MODIFICATION_DETERMINISTIC_VERSION = "2.0.0"
LEGAL_CONTRACT_RESEARCHER_ROLE = "legal_contract_researcher"
CONTRACT_RISK_REVIEW_SKILL_ID = "contract_risk_review"
CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION = "2.1.0"
LEGAL_CONTRACT_RISK_ANALYST_ROLE = "legal_contract_risk_analyst"
LEGAL_CONTRACT_REVIEWER_ROLE = "legal_contract_reviewer"
LEGAL_CONTRACT_REFERENCE_SKILL_IDS = (
    "partner_onboarding_dd",
    "contract_risk_review",
    CLAUSE_MODIFICATION_SKILL_ID,
)
MEETING_ROOM_SKILL_ID = "skill_meeting_room_book"
MEETING_ROOM_DETERMINISTIC_VERSION = "2.0.0"
OFFICE_SUPPLY_SKILL_ID = "skill_office_supply_request"
OFFICE_SUPPLY_DETERMINISTIC_VERSION = "2.0.0"
FAULT_REPORT_SKILL_ID = "fault_report_v1"
FAULT_REPORT_LIFECYCLE_VERSION = "3.1.0"
IT_SUPPORT_ENGINEER_ROLE = "it_support_engineer"
PERMISSION_GRANT_SKILL_ID = "skill_perm_grant_routing_001"
PERMISSION_GRANT_DETERMINISTIC_VERSION = "2.2.0"
IT_ACCESS_OPERATOR_ROLE = "it_access_operator"
IT_ACCESS_APPROVER_ROLE = "it_access_approver"
PARTICIPANT_ACCEPTANCE_ROLE = "process_demo_approver"
PARTICIPANT_ACCEPTANCE_SKILL_ID = "participant_approval_demo"
PARTICIPANT_ACCEPTANCE_VERSION = "1.1.0"
GRAPH_VISUAL_DEMO_SKILL_ID = "skill_graph_visual_demo"
GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION = "2.1.0"
PARTNER_DUE_DILIGENCE_SKILL_ID = "partner_onboarding_dd"
PARTNER_DUE_DILIGENCE_VERSION = "2.3.0"
LEGAL_PARTNER_DUE_DILIGENCE_ANALYST_ROLE = "legal_partner_due_diligence_analyst"
LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE = "legal_partner_due_diligence_reviewer"
SEAL_APPLICATION_SKILL_ID = "seal_application_approval"
SEAL_APPLICATION_DETERMINISTIC_VERSION = "2.0.2"
SEAL_APPLICATION_OPERATOR_ROLE = "admin_seal_operator"
SEAL_APPLICATION_APPROVER_ROLE = "admin_seal_approver"
SEAL_APPLICATION_SENIOR_APPROVER_ROLE = "admin_seal_senior_approver"
EXPENSE_SPECIAL_APPROVAL_SKILL_ID = "expense_over_limit_approval"
EXPENSE_SPECIAL_APPROVAL_VERSION = "2.0.0"
EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION = "2.1.0"
EXPENSE_SPECIAL_APPROVAL_OPERATOR_ROLE = "expense_special_approval_operator"
EXPENSE_DEPARTMENT_APPROVER_ROLE = "expense_department_approver"
EXPENSE_FINANCE_APPROVER_ROLE = "expense_finance_approver"


def ensure_demo_employee_profiles(db: Session) -> None:
    """为内置演示账号补齐稳定工号，且不覆盖管理员已维护的档案。"""

    db.flush()
    for user_id, employee_id, employee_name in (
        ("admin", "E001", "演示管理员"),
        ("user_demo", "E002", "演示员工"),
        ("approver_demo", "E003", "演示审批人"),
        ("it_engineer_demo", "E004", "演示 IT 工程师"),
    ):
        user = db.get(User, user_id)
        if user is None or user.tenant_id != "tenant_demo":
            continue
        existing = db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == user.tenant_id,
                EmployeeProfile.user_id == user.id,
            )
        ).first()
        if existing is not None:
            continue
        conflicting = db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == user.tenant_id,
                EmployeeProfile.employee_id == employee_id,
            )
        ).first()
        if conflicting is not None:
            continue
        db.add(
            EmployeeProfile(
                tenant_id=user.tenant_id,
                user_id=user.id,
                employee_id=employee_id,
                employee_name=employee_name,
                status="active",
                metadata_json={"source": "demo_seed"},
            )
        )
    db.flush()


def ensure_demo_business_role_mappings(db: Session) -> None:
    """为已贯通 SOP 创建最小业务角色，并映射演示员工和对应数字员工。"""

    ensure_builtin_permission_catalog(db, "tenant_demo")
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == FINANCE_EXPENSE_SPECIALIST_ROLE,
        )
    ).first()
    if role is None:
        role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=FINANCE_EXPENSE_SPECIALIST_ROLE,
            name="财务报销专员",
            category="finance",
            permissions_json=[],
            metadata_json={"source": "sop_expense_quota_query"},
        )
        db.add(role)
        db.flush()
    role.category = "finance"
    sync_role_permissions(
        db,
        role=role,
        permission_codes=[
            "expense.quota.read:any",
            "expense.travel_policy.assess",
            "expense.invoice.verify",
            "expense.submit",
            "expense.travel_review.claim",
            "expense.travel_review.complete",
            "expense.travel_review.request_information",
        ],
    )
    admin_profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == "tenant_demo",
            EmployeeProfile.user_id == "admin",
        )
    ).first()
    if admin_profile is not None:
        _ensure_employee_role_assignment(db, admin_profile, role)
    finance_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "财务",
            AgentProfile.status == "active",
        )
    ).first()
    if finance_agent is not None:
        finance_binding = _ensure_agent_role_binding(
            db,
            finance_agent,
            role,
            admin_profile,
            assignment_mode="execute",
        )
        finance_binding.assignment_mode = "execute"
        finance_binding.supervisor_employee_profile_id = (
            admin_profile.id if admin_profile else None
        )
        db.add(finance_binding)
    leave_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == HR_LEAVE_SPECIALIST_ROLE,
        )
    ).first()
    if leave_role is None:
        leave_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=HR_LEAVE_SPECIALIST_ROLE,
            name="HR 假勤专员",
            category="human_resources",
            permissions_json=[],
            metadata_json={"source": LEAVE_BALANCE_SKILL_ID},
        )
        db.add(leave_role)
        db.flush()
    leave_role.category = "human_resources"
    sync_role_permissions(
        db,
        role=leave_role,
        permission_codes=[
            "hr.leave.apply",
            "hr.leave_balance.read:any",
            "hr.leave_review.claim",
            "hr.leave_review.complete",
            "hr.leave_review.request_information",
            "hr.overtime_review.claim",
            "hr.overtime_review.complete",
            "hr.overtime_review.request_information",
        ],
    )
    if admin_profile is not None:
        _ensure_employee_role_assignment(db, admin_profile, leave_role)
    human_resources_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "人事",
            AgentProfile.status == "active",
        )
    ).first()
    if human_resources_agent is not None:
        leave_agent_binding = _ensure_agent_role_binding(
            db,
            human_resources_agent,
            leave_role,
            admin_profile,
            assignment_mode="execute",
        )
        leave_agent_binding.assignment_mode = "execute"
        leave_agent_binding.supervisor_employee_profile_id = (
            admin_profile.id if admin_profile else None
        )
        db.add(leave_agent_binding)
    certificate_operator_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == HR_CERTIFICATE_OPERATOR_ROLE,
        )
    ).first()
    if certificate_operator_role is None:
        certificate_operator_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=HR_CERTIFICATE_OPERATOR_ROLE,
            name="HR 证明开具操作员",
            category="human_resources",
            permissions_json=[],
            metadata_json={"source": HR_CERTIFICATE_SKILL_ID},
        )
        db.add(certificate_operator_role)
        db.flush()
    sync_role_permissions(
        db,
        role=certificate_operator_role,
        permission_codes=["hr.certificate.issue"],
    )
    if human_resources_agent is not None:
        _ensure_agent_role_binding(
            db,
            human_resources_agent,
            certificate_operator_role,
            admin_profile,
            assignment_mode="execute",
        )
    certificate_reviewer_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == HR_CERTIFICATE_REVIEWER_ROLE,
        )
    ).first()
    if certificate_reviewer_role is None:
        certificate_reviewer_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=HR_CERTIFICATE_REVIEWER_ROLE,
            name="HR 证明复核专员",
            category="human_resources",
            permissions_json=[],
            metadata_json={"source": HR_CERTIFICATE_SKILL_ID},
        )
        db.add(certificate_reviewer_role)
        db.flush()
    sync_role_permissions(
        db,
        role=certificate_reviewer_role,
        permission_codes=[
            "hr.certificate_request.approve",
            "hr.certificate_request.reject",
        ],
    )
    certificate_reviewer_profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == "tenant_demo",
            EmployeeProfile.user_id == "approver_demo",
        )
    ).first()
    if certificate_reviewer_profile is not None:
        _ensure_employee_role_assignment(
            db,
            certificate_reviewer_profile,
            certificate_reviewer_role,
        )
    legal_researcher_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == LEGAL_CONTRACT_RESEARCHER_ROLE,
        )
    ).first()
    if legal_researcher_role is None:
        legal_researcher_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=LEGAL_CONTRACT_RESEARCHER_ROLE,
            name="法务合同资料研究员",
            category="legal_compliance",
            permissions_json=[],
            metadata_json={"source": CLAUSE_MODIFICATION_SKILL_ID},
        )
        db.add(legal_researcher_role)
        db.flush()
    sync_role_permissions(
        db,
        role=legal_researcher_role,
        permission_codes=["legal.contract_reference.query"],
    )
    legal_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "法务",
            AgentProfile.status == "active",
        )
    ).first()
    if legal_agent is not None:
        _ensure_agent_role_binding(
            db,
            legal_agent,
            legal_researcher_role,
            admin_profile,
            assignment_mode="execute",
        )
    legal_risk_analyst_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == LEGAL_CONTRACT_RISK_ANALYST_ROLE,
        )
    ).first()
    if legal_risk_analyst_role is None:
        legal_risk_analyst_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=LEGAL_CONTRACT_RISK_ANALYST_ROLE,
            name="法务合同风险分析员",
            category="legal_compliance",
            permissions_json=[],
            metadata_json={"source": CONTRACT_RISK_REVIEW_SKILL_ID},
        )
        db.add(legal_risk_analyst_role)
        db.flush()
    sync_role_permissions(
        db,
        role=legal_risk_analyst_role,
        permission_codes=["legal.contract_risk.assess"],
    )
    if legal_agent is not None:
        _ensure_agent_role_binding(
            db,
            legal_agent,
            legal_risk_analyst_role,
            admin_profile,
            assignment_mode="execute",
        )
    legal_reviewer_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == LEGAL_CONTRACT_REVIEWER_ROLE,
        )
    ).first()
    if legal_reviewer_role is None:
        legal_reviewer_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=LEGAL_CONTRACT_REVIEWER_ROLE,
            name="法务合同复核专员",
            category="legal_compliance",
            permissions_json=[],
            metadata_json={"source": CONTRACT_RISK_REVIEW_SKILL_ID},
        )
        db.add(legal_reviewer_role)
        db.flush()
    sync_role_permissions(
        db,
        role=legal_reviewer_role,
        permission_codes=[
            "legal.contract_review.claim",
            "legal.contract_review.complete",
            "legal.contract_review.request_information",
        ],
    )
    legal_reviewer_profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == "tenant_demo",
            EmployeeProfile.user_id == "approver_demo",
        )
    ).first()
    if legal_reviewer_profile is not None:
        _ensure_employee_role_assignment(db, legal_reviewer_profile, legal_reviewer_role)
    partner_analyst_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == LEGAL_PARTNER_DUE_DILIGENCE_ANALYST_ROLE,
        )
    ).first()
    if partner_analyst_role is None:
        partner_analyst_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=LEGAL_PARTNER_DUE_DILIGENCE_ANALYST_ROLE,
            name="合作方尽调分析员",
            category="legal_compliance",
            permissions_json=[],
            metadata_json={"source": PARTNER_DUE_DILIGENCE_SKILL_ID},
        )
        db.add(partner_analyst_role)
        db.flush()
    sync_role_permissions(
        db,
        role=partner_analyst_role,
        permission_codes=["legal.partner_due_diligence.query"],
    )
    if legal_agent is not None:
        _ensure_agent_role_binding(
            db,
            legal_agent,
            partner_analyst_role,
            admin_profile,
            assignment_mode="execute",
        )
    partner_reviewer_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE,
        )
    ).first()
    if partner_reviewer_role is None:
        partner_reviewer_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE,
            name="合作方尽调复核专员",
            category="legal_compliance",
            permissions_json=[],
            metadata_json={"source": PARTNER_DUE_DILIGENCE_SKILL_ID},
        )
        db.add(partner_reviewer_role)
        db.flush()
    sync_role_permissions(
        db,
        role=partner_reviewer_role,
        permission_codes=[
            "legal.partner_due_diligence.claim",
            "legal.partner_due_diligence.complete",
            "legal.partner_due_diligence.request_information",
        ],
    )
    if legal_reviewer_profile is not None:
        _ensure_employee_role_assignment(
            db,
            legal_reviewer_profile,
            partner_reviewer_role,
        )
    acceptance_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == PARTICIPANT_ACCEPTANCE_ROLE,
        )
    ).first()
    if acceptance_role is None:
        acceptance_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=PARTICIPANT_ACCEPTANCE_ROLE,
            name="流程演示审批人",
            category="cross_functional",
            permissions_json=[],
            metadata_json={"source": PARTICIPANT_ACCEPTANCE_SKILL_ID},
        )
        db.add(acceptance_role)
        db.flush()
    acceptance_role.category = "cross_functional"
    sync_role_permissions(
        db,
        role=acceptance_role,
        permission_codes=["sop.demo.approve", "sop.demo.reject"],
    )
    approver_profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == "tenant_demo",
            EmployeeProfile.user_id == "approver_demo",
        )
    ).first()
    if approver_profile is not None:
        _ensure_employee_role_assignment(db, approver_profile, acceptance_role)
    support_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == IT_SUPPORT_ENGINEER_ROLE,
        )
    ).first()
    if support_role is None:
        support_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=IT_SUPPORT_ENGINEER_ROLE,
            name="IT 支持工程师",
            category="information_technology",
            permissions_json=[],
            metadata_json={"source": FAULT_REPORT_SKILL_ID},
        )
        db.add(support_role)
        db.flush()
    support_role.category = "information_technology"
    sync_role_permissions(
        db,
        role=support_role,
        permission_codes=["it.ticket.claim", "it.ticket.resolve", "it.ticket.escalate"],
    )
    engineer_profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == "tenant_demo",
            EmployeeProfile.user_id == "it_engineer_demo",
        )
    ).first()
    if engineer_profile is not None:
        _ensure_employee_role_assignment(db, engineer_profile, support_role)
    information_technology_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "IT",
            AgentProfile.status == "active",
        )
    ).first()
    if information_technology_agent is not None:
        _ensure_agent_role_binding(
            db,
            information_technology_agent,
            support_role,
            engineer_profile,
        )
    access_operator_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == IT_ACCESS_OPERATOR_ROLE,
        )
    ).first()
    if access_operator_role is None:
        access_operator_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=IT_ACCESS_OPERATOR_ROLE,
            name="IT 权限开通操作员",
            category="information_technology",
            permissions_json=[],
            metadata_json={"source": PERMISSION_GRANT_SKILL_ID},
        )
        db.add(access_operator_role)
        db.flush()
    sync_role_permissions(db, role=access_operator_role, permission_codes=["it.access.grant"])
    if information_technology_agent is not None:
        _ensure_agent_role_binding(
            db,
            information_technology_agent,
            access_operator_role,
            engineer_profile,
            assignment_mode="execute",
        )
    access_approver_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_demo",
            BusinessRole.role_code == IT_ACCESS_APPROVER_ROLE,
        )
    ).first()
    if access_approver_role is None:
        access_approver_role = BusinessRole(
            tenant_id="tenant_demo",
            role_code=IT_ACCESS_APPROVER_ROLE,
            name="IT 高权限审批人",
            category="information_technology",
            permissions_json=[],
            metadata_json={"source": PERMISSION_GRANT_SKILL_ID},
        )
        db.add(access_approver_role)
        db.flush()
    sync_role_permissions(
        db,
        role=access_approver_role,
        permission_codes=["it.access_request.approve", "it.access_request.reject"],
    )
    if approver_profile is not None:
        _ensure_employee_role_assignment(db, approver_profile, access_approver_role)
    _ensure_seal_application_role_mappings(
        db,
        admin_profile=admin_profile,
        approver_profile=approver_profile,
    )
    _ensure_expense_special_approval_role_mappings(
        db,
        admin_profile=admin_profile,
        approver_profile=approver_profile,
    )
    db.flush()


def _ensure_seal_application_role_mappings(
    db: Session,
    *,
    admin_profile: EmployeeProfile | None,
    approver_profile: EmployeeProfile | None,
) -> None:
    """创建用章数字员工操作角色及普通、重要申请的分级真人审批角色。"""

    role_specs = (
        (
            SEAL_APPLICATION_OPERATOR_ROLE,
            "用章申请操作员",
            ["admin.seal_application.create", "admin.seal_application.finalize"],
        ),
        (
            SEAL_APPLICATION_APPROVER_ROLE,
            "用章审批人",
            [
                "admin.seal_application.claim",
                "admin.seal_application.approve",
                "admin.seal_application.reject",
            ],
        ),
        (
            SEAL_APPLICATION_SENIOR_APPROVER_ROLE,
            "重要用章审批人",
            [
                "admin.seal_application.claim",
                "admin.seal_application.approve",
                "admin.seal_application.reject",
            ],
        ),
    )
    roles: dict[str, BusinessRole] = {}
    for role_code, role_name, permissions in role_specs:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == "tenant_demo",
                BusinessRole.role_code == role_code,
            )
        ).first()
        if role is None:
            role = BusinessRole(
                tenant_id="tenant_demo",
                role_code=role_code,
                name=role_name,
                category="administration",
                permissions_json=[],
                metadata_json={"source": SEAL_APPLICATION_SKILL_ID},
            )
            db.add(role)
            db.flush()
        role.category = "administration"
        sync_role_permissions(db, role=role, permission_codes=permissions)
        roles[role_code] = role
    if admin_profile is not None:
        _ensure_employee_role_assignment(
            db,
            admin_profile,
            roles[SEAL_APPLICATION_APPROVER_ROLE],
        )
    if approver_profile is not None:
        _ensure_employee_role_assignment(
            db,
            approver_profile,
            roles[SEAL_APPLICATION_SENIOR_APPROVER_ROLE],
        )
    administration_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "行政",
            AgentProfile.status == "active",
        )
    ).first()
    if administration_agent is not None:
        _ensure_agent_role_binding(
            db,
            administration_agent,
            roles[SEAL_APPLICATION_OPERATOR_ROLE],
            admin_profile,
            assignment_mode="execute",
        )


def _ensure_expense_special_approval_role_mappings(
    db: Session,
    *,
    admin_profile: EmployeeProfile | None,
    approver_profile: EmployeeProfile | None,
) -> None:
    """创建特批数字员工操作角色和部门、财务两级真人审批角色。"""

    role_specs = (
        (
            EXPENSE_SPECIAL_APPROVAL_OPERATOR_ROLE,
            "超标报销特批操作员",
            [
                "expense.special_approval.create",
                "expense.special_approval.finalize",
            ],
        ),
        (
            EXPENSE_DEPARTMENT_APPROVER_ROLE,
            "超标报销部门负责人",
            [
                "expense.special_approval.claim",
                "expense.special_approval.approve",
                "expense.special_approval.reject",
            ],
        ),
        (
            EXPENSE_FINANCE_APPROVER_ROLE,
            "超标报销财务负责人",
            [
                "expense.special_approval.claim",
                "expense.special_approval.approve",
                "expense.special_approval.reject",
            ],
        ),
    )
    roles: dict[str, BusinessRole] = {}
    for role_code, role_name, permissions in role_specs:
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == "tenant_demo",
                BusinessRole.role_code == role_code,
            )
        ).first()
        if role is None:
            role = BusinessRole(
                tenant_id="tenant_demo",
                role_code=role_code,
                name=role_name,
                category="finance",
                permissions_json=[],
                metadata_json={"source": EXPENSE_SPECIAL_APPROVAL_SKILL_ID},
            )
            db.add(role)
            db.flush()
        role.category = "finance"
        sync_role_permissions(db, role=role, permission_codes=permissions)
        roles[role_code] = role
    if admin_profile is not None:
        _ensure_employee_role_assignment(
            db,
            admin_profile,
            roles[EXPENSE_DEPARTMENT_APPROVER_ROLE],
        )
    if approver_profile is not None:
        _ensure_employee_role_assignment(
            db,
            approver_profile,
            roles[EXPENSE_FINANCE_APPROVER_ROLE],
        )
    finance_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "财务",
            AgentProfile.status == "active",
        )
    ).first()
    if finance_agent is not None:
        _ensure_agent_role_binding(
            db,
            finance_agent,
            roles[EXPENSE_SPECIAL_APPROVAL_OPERATOR_ROLE],
            admin_profile,
            assignment_mode="execute",
        )


def ensure_participant_acceptance_skill(db: Session) -> None:
    """幂等发布无工具副作用的参与者验收 SOP，并加入整体资源池。"""

    content = _participant_acceptance_content()
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("组织参与者验收流程包含兼容警告，禁止自动发布")
    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == PARTICIPANT_ACCEPTANCE_SKILL_ID,
        )
    ).first()
    if skill is None:
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id=PARTICIPANT_ACCEPTANCE_SKILL_ID,
            version=PARTICIPANT_ACCEPTANCE_VERSION,
            name="组织参与者验收流程",
            business_domain="organization",
            description="验证业务角色候选、认领、审批和流程恢复的横向闭环。",
            content_json=content,
            status="published",
        )
        db.add(skill)
        db.flush()
        write_skill_version(db, skill, compiled_definition=definition)
    elif _version_tuple(skill.version) < _version_tuple(PARTICIPANT_ACCEPTANCE_VERSION):
        current_version = db.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == skill.tenant_id,
                SkillVersion.skill_id == skill.skill_id,
                SkillVersion.version == skill.version,
            )
        ).first()
        skill.version = PARTICIPANT_ACCEPTANCE_VERSION
        skill.content_json = content
        skill.status = "published"
        db.add(skill)
        write_skill_version(
            db,
            skill,
            compiled_definition=definition,
            derived_from_version_id=current_version.id if current_version else None,
        )
    elif skill.version == PARTICIPANT_ACCEPTANCE_VERSION:
        write_skill_version(db, skill, compiled_definition=definition)
    ensure_open_gallery_binding(
        db,
        "tenant_demo",
        "skill",
        skill.id,
        "active" if skill.status == "published" else "inactive",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    administrative_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "行政",
            AgentProfile.status == "active",
        )
    ).first()
    if administrative_agent is not None:
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            administrative_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed"},
        )
        sync_branch_from_overall(db, "tenant_demo", administrative_agent.id, skill)
    db.flush()


def ensure_graph_visual_demo_knowledge_version(db: Session) -> None:
    """幂等发布可在真实聊天中验证工具与知识分支的确定性图流程。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == GRAPH_VISUAL_DEMO_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        legacy_definition = compile_legacy_skill_card(skill.content_json)
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=legacy_definition,
        ).version
    content = _graph_visual_demo_knowledge_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("图结构知识回归版本包含兼容警告，禁止自动发布")
    skill.version = GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    hr_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == "人事",
            AgentProfile.status == "active",
        )
    ).first()
    if hr_agent is not None:
        ensure_private_resource_binding(
            db,
            skill.tenant_id,
            hr_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed", "system_seeded": True},
        )
    _sync_seed_agent_branch(db, skill, agent_name="人事")
    db.flush()


def _graph_visual_demo_knowledge_content(
    source: dict[str, object],
) -> dict[str, object]:
    """把旧图演示迁移为只依赖统一工具、知识服务和受限条件的 v5 定义。"""

    content = deepcopy(source)
    content.update(
        {
            "skill_id": GRAPH_VISUAL_DEMO_SKILL_ID,
            "name": "图结构可视化验证流程",
            "version": GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION,
            "execution_mode": "deterministic",
            "business_domain": "demo",
            "description": "验证统一 Runtime 的工具分支、知识分支、持久回执和确定性终态。",
            "trigger_intents": [
                "图结构验证",
                "流程图验证",
                "graph demo",
                "验证知识分支",
            ],
            "user_utterance_examples": [
                "运行图结构验证的知识路径，查询员工年假资格和天数规则",
                "用图结构验证工具路径，查询测试商品价格",
            ],
            "goal": [
                "识别用户要验证的工具或知识路径",
                "执行对应外部能力并持久化回执",
                "按回执状态进入成功或保守终态",
            ],
            "required_info": ["request_type", "request_detail"],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "request_type": {"type": "string"},
                        "request_detail": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "graph_tool_result": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        }
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "graph_knowledge_result": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"outcome": {"type": "string"}},
                                },
                            },
                        }
                    },
                },
            },
            "slot_filling_policy": {
                "enabled": True,
                "multi_slot_per_turn": True,
                "extract_scope": "all_skill_expected_user_info",
                "skip_satisfied_steps": True,
                "target_info": ["request_type", "request_detail"],
            },
            "nodes": [
                {
                    "node_id": "intake_request",
                    "type": "collect_info",
                    "name": "识别验证请求",
                    "instruction": (
                        "收集要验证的路径和具体问题。request_type 只能是 knowledge 或 tool；"
                        "request_detail 保存政策问题或商品名称。"
                    ),
                    "expected_user_info": ["request_type", "request_detail"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "value_aliases": {
                            "request_type": {
                                "knowledge": "knowledge",
                                "知识": "knowledge",
                                "知识路径": "knowledge",
                                "政策": "knowledge",
                                "tool": "tool",
                                "工具": "tool",
                                "工具路径": "tool",
                            }
                        }
                    },
                },
                {
                    "node_id": "classify_path",
                    "type": "decision",
                    "name": "选择验证分支",
                    "instruction": "根据规范化 request_type 进入唯一分支。",
                },
                {
                    "node_id": "query_product_price",
                    "type": "tool_call",
                    "name": "查询商品价格",
                    "instruction": "调用受控商品价格工具验证工具回执路径。",
                    "allowed_actions": ["call_tool:product.price_query"],
                    "metadata": {
                        "operation_input": {"product_name": "slots.request_detail"},
                        "operation_result_key": "graph_tool_result",
                    },
                },
                {
                    "node_id": "read_policy_knowledge",
                    "type": "knowledge_query",
                    "name": "读取企业政策依据",
                    "instruction": "检索当前数字员工可见知识库，返回与问题直接相关的制度依据。",
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {"policy_question": "slots.request_detail"},
                        "operation_result_key": "graph_knowledge_result",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": "直接相关的制度条款、适用条件和来源",
                            "max_chunks": 6,
                            "max_depth": 2,
                        },
                    },
                },
                {
                    "node_id": "reply_tool_success",
                    "type": "response",
                    "name": "工具路径验证成功",
                    "instruction": "依据本次工具回执反馈商品结果，不得编造字段。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reply_knowledge_success",
                    "type": "response",
                    "name": "知识路径验证成功",
                    "instruction": "仅依据本次知识回执回答问题，并给出可核对的来源。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reply_tool_failure",
                    "type": "response",
                    "name": "工具路径保守失败",
                    "instruction": "明确说明工具调用失败，不得伪造商品结果。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reply_knowledge_failure",
                    "type": "response",
                    "name": "知识路径保守失败",
                    "instruction": "明确说明没有形成可用知识回执，建议人工核对。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reply_unsupported_path",
                    "type": "response",
                    "name": "不支持的验证路径",
                    "instruction": "提示当前只支持 knowledge 或 tool 两种验证路径。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "intake_request",
                    "next_node_id": "classify_path",
                },
                {
                    "source_node_id": "classify_path",
                    "next_node_id": "read_policy_knowledge",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.request_type"},
                        "right": {"value": "knowledge"},
                    },
                    "priority": 100,
                    "label": "知识路径",
                },
                {
                    "source_node_id": "classify_path",
                    "next_node_id": "query_product_price",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.request_type"},
                        "right": {"value": "tool"},
                    },
                    "priority": 90,
                    "label": "工具路径",
                },
                {
                    "source_node_id": "classify_path",
                    "next_node_id": "reply_unsupported_path",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "默认阻断",
                },
                {
                    "source_node_id": "read_policy_knowledge",
                    "next_node_id": "reply_knowledge_success",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "node_output.graph_knowledge_result.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "node_output.graph_knowledge_result.data.outcome"
                                    )
                                },
                                "right": {"value": "evidence_found"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "知识回执成功",
                },
                {
                    "source_node_id": "read_policy_knowledge",
                    "next_node_id": "reply_knowledge_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "知识失败默认路径",
                },
                {
                    "source_node_id": "query_product_price",
                    "next_node_id": "reply_tool_success",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.graph_tool_result.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 100,
                    "label": "工具回执成功",
                },
                {
                    "source_node_id": "query_product_price",
                    "next_node_id": "reply_tool_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "工具失败默认路径",
                },
            ],
            "start_node_id": "intake_request",
            "terminal_node_ids": [
                "reply_tool_success",
                "reply_knowledge_success",
                "reply_tool_failure",
                "reply_knowledge_failure",
                "reply_unsupported_path",
            ],
            "response_rules": [
                "模型只负责提取 request_type 和 request_detail，不得跳过 Runtime 节点或自行选择终态。",
                "知识回答只引用本次检索回执；检索失败必须进入保守终态，不得凭模型记忆补写制度。",
                "工具回答只引用本次工具回执；工具失败不得编造价格。",
            ],
        }
    )
    return content


def ensure_partner_due_diligence_version(db: Session) -> None:
    """幂等派生合作方专用尽调、制度检索和高风险真人复核的当前版本。"""

    _ensure_partner_due_diligence_tool(db)
    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == PARTNER_DUE_DILIGENCE_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        PARTNER_DUE_DILIGENCE_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _partner_due_diligence_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("合作方入库尽调确定性版本包含兼容告警，禁止自动发布")
    skill.version = PARTNER_DUE_DILIGENCE_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    legal_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == "法务",
            AgentProfile.status == "active",
        )
    ).first()
    if legal_agent is not None:
        ensure_private_resource_binding(
            db,
            skill.tenant_id,
            legal_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed", "system_seeded": True},
        )
    _sync_seed_agent_branch(db, skill, agent_name="法务")
    db.flush()


def _ensure_partner_due_diligence_tool(db: Session) -> None:
    """幂等创建合作方专用尽调工具，并冻结 SOP 白名单和数字员工授权边界。"""

    base_url = get_settings().normalized_tool_base_url.rstrip("/")
    input_schema = {
        "type": "object",
        "properties": {
            "company_name": {"type": "string", "minLength": 2, "maxLength": 200},
            "unified_social_credit_code": {
                "type": "string",
                "minLength": 18,
                "maxLength": 18,
                "pattern": "^[0-9A-HJ-NP-RT-UWXY]{18}$",
            },
        },
        "required": ["company_name", "unified_social_credit_code"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "check_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["assessed", "not_found", "identity_mismatch"],
            },
            "subject_name": {"type": "string"},
            "unified_social_credit_code": {"type": "string"},
            "subject_status": {
                "type": "string",
                "enum": ["active", "abnormal", "unknown"],
            },
            "credit_code_match": {"type": "boolean"},
            "litigation_count": {"type": "integer"},
            "enforcement_count": {"type": "integer"},
            "blacklisted": {"type": "boolean"},
            "risk_level": {
                "type": "string",
                "enum": ["low", "high", "unknown"],
            },
            "risk_flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "title": {"type": "string"},
                        "severity": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "recommendation": {
                "type": "string",
                "enum": ["pass", "human_review", "insufficient"],
            },
            "requires_human_review": {"type": "boolean"},
            "evidence_as_of": {"type": "string"},
            "evidence_sources": {"type": "array", "items": {"type": "string"}},
            "message": {"type": "string"},
        },
    }
    tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "partner.due_diligence_query",
        )
    ).first()
    payload = {
        "display_name": "合作方入库尽调",
        "description": (
            "按固定虚构主体返回工商、涉诉、执行和演示黑名单回执，不连接真实外部数据库。"
        ),
        "bucket": "法务合规",
        "tool_type": "http",
        "method": "POST",
        "url": f"{base_url}/api/mock/partner/due_diligence_query",
        "headers_json": {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
        "auth_json": {},
        "config_json": {},
        "input_schema": input_schema,
        "output_schema": output_schema,
        "allowed_skills_json": [PARTNER_DUE_DILIGENCE_SKILL_ID],
        "required_permission_code": "legal.partner_due_diligence.query",
        "permission_authorization_mode": "workflow_delegated",
        "enabled": True,
    }
    if tool is None:
        tool = Tool(
            tenant_id="tenant_demo",
            name="partner.due_diligence_query",
            **payload,
        )
    else:
        for field_name, field_value in payload.items():
            setattr(tool, field_name, field_value)
        tool.updated_at = utc_now()
    db.add(tool)
    db.flush()
    ensure_open_gallery_binding(
        db,
        "tenant_demo",
        "tool",
        tool.id,
        metadata_json={"source": PARTNER_DUE_DILIGENCE_SKILL_ID, "system_seeded": True},
    )
    legal_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "法务",
            AgentProfile.status == "active",
        )
    ).first()
    if legal_agent is not None:
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            legal_agent.id,
            "tool",
            tool.id,
            metadata_json={"source": PARTNER_DUE_DILIGENCE_SKILL_ID},
        )


def _partner_due_diligence_content(
    source: dict[str, object],
) -> dict[str, object]:
    """把旧合同检索流程迁移为外部尽调事实、内部制度和真人复核统一图。"""

    content = deepcopy(source)
    content.update(
        {
            "skill_id": PARTNER_DUE_DILIGENCE_SKILL_ID,
            "name": "合作方入库尽调",
            "version": PARTNER_DUE_DILIGENCE_VERSION,
            "execution_mode": "deterministic",
            "business_domain": "采购与供应链合规",
            "description": "核验演示合作方外部事实、读取内部准入制度并形成建议或真人复核。",
            "trigger_intents": [
                "合作方入库尽调",
                "新供应商背景调查",
                "合作方合规核查",
            ],
            "user_utterance_examples": [
                (
                    "请对共格演示科技有限公司做合作方入库尽调，"
                    "统一社会信用代码 91370000MA3D3M001X"
                ),
                (
                    "请核查共格演示风险供应商有限公司，"
                    "统一社会信用代码 91370000MA3R15K01X"
                ),
            ],
            "goal": [
                "核对合作方名称和统一社会信用代码",
                "查询受控工商、涉诉、执行和演示黑名单事实",
                "读取公司合作方准入与反商业贿赂制度",
                "输出演示入库建议或创建法务真人复核任务",
            ],
            "required_info": ["enterprise_full_name", "unified_social_credit_code"],
            "slot_key_aliases": {
                "company_name": "enterprise_full_name",
                "company_full_name": "enterprise_full_name",
                "enterprise_name": "enterprise_full_name",
                "credit_code": "unified_social_credit_code",
                "social_credit_code": "unified_social_credit_code",
                "unified_credit_code": "unified_social_credit_code",
                "uscc": "unified_social_credit_code",
            },
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "enterprise_full_name": {"type": "string"},
                        "unified_social_credit_code": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "partner_due_diligence": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "risk_level": {"type": "string"},
                                        "recommendation": {"type": "string"},
                                    },
                                },
                            },
                        }
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "partner_policy": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"outcome": {"type": "string"}},
                                },
                            },
                        }
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "collect_partner_info",
                    "type": "collect_info",
                    "name": "收集合作方主体信息",
                    "instruction": (
                        "一次性收集企业全称和 18 位统一社会信用代码。不得从简称推测企业全称，"
                        "不得修改或补齐用户提供的信用代码。"
                    ),
                    "expected_user_info": [
                        "enterprise_full_name",
                        "unified_social_credit_code",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "query_partner_due_diligence",
                    "type": "tool_call",
                    "name": "查询合作方演示尽调事实",
                    "instruction": (
                        "只按用户提供的企业全称和统一社会信用代码调用专用尽调工具，"
                        "不得改用合同判例检索或模型常识。"
                    ),
                    "allowed_actions": ["call_tool:partner.due_diligence_query"],
                    "metadata": {
                        "operation_input": {
                            "company_name": "slots.enterprise_full_name",
                            "unified_social_credit_code": (
                                "slots.unified_social_credit_code"
                            ),
                        },
                        "operation_result_key": "partner_due_diligence",
                    },
                },
                {
                    "node_id": "query_partner_policy",
                    "type": "knowledge_query",
                    "name": "读取合作方准入合规制度",
                    "instruction": (
                        "检索当前法务数字员工可见知识库中的合作方准入、反商业贿赂、"
                        "利益冲突和黑名单制度，返回可追溯的内部政策依据。"
                    ),
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {
                            "enterprise_full_name": "slots.enterprise_full_name",
                        },
                        "operation_result_key": "partner_policy",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": (
                                "合作方准入、反商业贿赂、利益冲突或黑名单相关制度条款及来源"
                            ),
                            "max_chunks": 6,
                            "max_depth": 2,
                        },
                    },
                },
                {
                    "node_id": "partner_legal_review",
                    "type": "human_task",
                    "name": "法务复核高风险合作方",
                    "instruction": (
                        "由真实法务复核人认领，核对本次尽调回执、制度依据和补充材料后提交意见。"
                    ),
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [
                                LEGAL_PARTNER_DUE_DILIGENCE_REVIEWER_ROLE
                            ],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["reviewed", "needs_information"],
                            "action_permissions": {
                                "claim": "legal.partner_due_diligence.claim",
                                "outcome:reviewed": (
                                    "legal.partner_due_diligence.complete"
                                ),
                                "outcome:needs_information": (
                                    "legal.partner_due_diligence.request_information"
                                ),
                            },
                            "waiting_message": (
                                "演示尽调命中高风险信号，已创建法务真人复核任务，"
                                "等待具备合作方尽调复核角色的员工认领。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "reviewed",
                                    "label": "提交尽调复核意见",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "合作方尽调真人复核已完成。核查编号：{check_id}；"
                                        "合作方：{subject_name}；复核意见：{comment}。"
                                        "该结果仍是本地演示，不代表真实外部核验或正式准入批准。"
                                    ),
                                },
                                {
                                    "value": "needs_information",
                                    "label": "要求补充合作方材料",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "合作方尽调暂无法完成。核查编号：{check_id}；"
                                        "需要补充：{comment}。当前没有形成准入建议。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "issue_demo_onboarding_recommendation",
                    "type": "response",
                    "name": "出具演示入库建议",
                    "instruction": (
                        "按固定结构反馈主体匹配、存续状态、涉诉数、执行数、黑名单、证据时间、"
                        "内部制度依据和演示入库建议。只能使用本次 partner_due_diligence 与"
                        " partner_policy 回执；必须说明未命中风险不等于真实外部核验或正式批准。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "partner_information_insufficient",
                    "type": "response",
                    "name": "反馈主体信息无法核验",
                    "instruction": (
                        "说明演示数据未找到主体或名称与信用代码不一致，要求核对材料，"
                        "不得生成通过或风险结论。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "partner_due_diligence_failed",
                    "type": "response",
                    "name": "反馈尽调工具失败",
                    "instruction": "说明外部事实查询失败，不得复用历史回执或由模型生成风险结论。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "partner_policy_query_failed",
                    "type": "response",
                    "name": "反馈内部制度检索失败",
                    "instruction": (
                        "说明虽然已取得演示尽调事实，但没有形成可追溯的内部制度依据，"
                        "因此当前不能给出入库建议。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "partner_review_completed",
                    "type": "response",
                    "name": "反馈合作方尽调复核完成",
                    "instruction": "只反馈结构化复核意见和演示边界，不得声称合作方已正式入库。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "partner_review_information_required",
                    "type": "response",
                    "name": "反馈需要补充合作方材料",
                    "instruction": "反馈复核人声明的缺失材料，明确当前没有形成准入建议。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_partner_info",
                    "next_node_id": "query_partner_due_diligence",
                },
                {
                    "source_node_id": "query_partner_due_diligence",
                    "next_node_id": "query_partner_policy",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.partner_due_diligence.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.partner_due_diligence.data.status"
                                    )
                                },
                                "right": {"value": "assessed"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "主体尽调事实已返回",
                },
                {
                    "source_node_id": "query_partner_due_diligence",
                    "next_node_id": "partner_information_insufficient",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.partner_due_diligence.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "any",
                                "args": [
                                    {
                                        "op": "eq",
                                        "left": {
                                            "path": (
                                                "tool_result.partner_due_diligence.data.status"
                                            )
                                        },
                                        "right": {"value": "not_found"},
                                    },
                                    {
                                        "op": "eq",
                                        "left": {
                                            "path": (
                                                "tool_result.partner_due_diligence.data.status"
                                            )
                                        },
                                        "right": {"value": "identity_mismatch"},
                                    },
                                ],
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "主体信息不足或不一致",
                },
                {
                    "source_node_id": "query_partner_due_diligence",
                    "next_node_id": "partner_due_diligence_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "尽调工具失败默认路径",
                },
                {
                    "source_node_id": "query_partner_policy",
                    "next_node_id": "partner_legal_review",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "node_output.partner_policy.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "node_output.partner_policy.data.outcome"
                                },
                                "right": {"value": "evidence_found"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.partner_due_diligence.data.recommendation"
                                    )
                                },
                                "right": {"value": "human_review"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "高风险进入真人复核",
                },
                {
                    "source_node_id": "query_partner_policy",
                    "next_node_id": "issue_demo_onboarding_recommendation",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "node_output.partner_policy.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "node_output.partner_policy.data.outcome"
                                },
                                "right": {"value": "evidence_found"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.partner_due_diligence.data.recommendation"
                                    )
                                },
                                "right": {"value": "pass"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "低风险形成演示入库建议",
                },
                {
                    "source_node_id": "query_partner_policy",
                    "next_node_id": "partner_policy_query_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "内部制度失败默认路径",
                },
                {
                    "source_node_id": "partner_legal_review",
                    "next_node_id": "partner_review_completed",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "reviewed"},
                    },
                    "priority": 100,
                    "label": "真人复核完成",
                },
                {
                    "source_node_id": "partner_legal_review",
                    "next_node_id": "partner_review_information_required",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "要求补充材料",
                },
            ],
            "start_node_id": "collect_partner_info",
            "terminal_node_ids": [
                "issue_demo_onboarding_recommendation",
                "partner_information_insufficient",
                "partner_due_diligence_failed",
                "partner_policy_query_failed",
                "partner_review_completed",
                "partner_review_information_required",
            ],
            "response_rules": [
                "不得调用 contract.archive_query 代替工商、涉诉、执行和黑名单尽调。",
                "外部事实只取本次 partner.due_diligence_query 回执；未知或身份不匹配不得通过。",
                "内部规则只取本次 partner_policy 知识回执；检索失败不得凭模型记忆补写制度。",
                "低风险只形成演示入库建议，不代表真实外部核验、正式批准或已经入库。",
                "高风险必须创建真人工作项；平台管理员和法务数字员工不能代替候选复核人。",
                "人工结果使用 reviewed 或 needs_information，不使用 approve/reject 冒充准入审批。",
            ],
        }
    )
    content["slot_filling_policy"] = {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "target_info": ["enterprise_full_name", "unified_social_credit_code"],
    }
    return content


def _participant_acceptance_content() -> dict[str, object]:
    """构造人工任务到批准、拒绝终态的最小确定性验收定义。"""

    return {
        "skill_id": PARTICIPANT_ACCEPTANCE_SKILL_ID,
        "name": "组织参与者验收流程",
        "version": PARTICIPANT_ACCEPTANCE_VERSION,
        "business_domain": "organization",
        "description": "验证业务角色候选、认领、审批和流程恢复的横向闭环。",
        "execution_mode": "deterministic",
        "trigger_intents": ["发起组织参与者验收", "发起审批演示", "测试审批闭环"],
        "user_utterance_examples": ["请发起组织参与者验收", "帮我测试一次审批闭环"],
        "goal": ["创建角色候选工作项", "由审批人认领并决定", "恢复并结束流程"],
        "required_info": [],
        "condition_schemas": {
            "work_item": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "outcome": {"type": "string"},
                },
            }
        },
        "nodes": [
            {
                "node_id": "participant_review",
                "type": "human_task",
                "name": "流程参与者审批",
                "instruction": "等待具有流程演示审批人角色的真实员工认领并决定。",
                "metadata": {
                    "participant_policy": {
                        "candidate_role_codes": [PARTICIPANT_ACCEPTANCE_ROLE],
                        "completion_mode": "any",
                        "claim_required": True,
                        "exclude_initiator": True,
                        "allowed_outcomes": ["approved", "rejected"],
                        "action_permissions": {
                            "outcome:approved": "sop.demo.approve",
                            "outcome:rejected": "sop.demo.reject",
                        },
                    }
                },
            },
            {
                "node_id": "approved_terminal",
                "type": "terminal",
                "name": "验收通过",
                "instruction": "反馈组织参与者审批闭环已通过。",
            },
            {
                "node_id": "rejected_terminal",
                "type": "terminal",
                "name": "验收拒绝",
                "instruction": "反馈组织参与者审批闭环未通过。",
            },
        ],
        "edges": [
            {
                "source_node_id": "participant_review",
                "next_node_id": "approved_terminal",
                "condition": {
                    "op": "eq",
                    "left": {"path": "work_item.outcome"},
                    "right": {"value": "approved"},
                },
                "priority": 100,
                "label": "同意",
            },
            {
                "source_node_id": "participant_review",
                "next_node_id": "rejected_terminal",
                "condition": {"op": "always"},
                "priority": 0,
                "label": "拒绝",
            },
        ],
        "start_node_id": "participant_review",
        "terminal_node_ids": ["approved_terminal", "rejected_terminal"],
        "interruption_policy": {},
        "response_rules": ["审批结果必须来自结构化工作项，不得由对话模型代替审批人生成。"],
    }


def ensure_expense_quota_identity_version(db: Session) -> None:
    """从现有业务卡派生并幂等发布报销额度身份绑定版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == EXPENSE_QUOTA_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        EXPENSE_QUOTA_IDENTITY_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _expense_quota_identity_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("报销额度身份绑定版本包含兼容警告，禁止自动发布")
    skill.version = EXPENSE_QUOTA_IDENTITY_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_finance_branch(db, skill)
    db.flush()


def ensure_leave_balance_deterministic_version(db: Session) -> None:
    """从现有假期余额卡派生并幂等发布可信身份和类型归一版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == LEAVE_BALANCE_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        LEAVE_BALANCE_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _leave_balance_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("假期余额确定性版本包含兼容警告，禁止自动发布")
    skill.version = LEAVE_BALANCE_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="人事")
    db.flush()


def ensure_leave_application_deterministic_version(db: Session) -> None:
    """从历史请假卡派生政策、余额、确认和提交回执均确定的不可变版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == LEAVE_APPLICATION_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        LEAVE_APPLICATION_DETERMINISTIC_VERSION
    ):
        return
    _ensure_leave_application_tools(db)
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _leave_application_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("请假申请确定性版本包含兼容告警，禁止自动发布")
    skill.version = LEAVE_APPLICATION_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    hr_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == "人事",
            AgentProfile.status == "active",
        )
    ).first()
    if hr_agent is not None:
        ensure_private_resource_binding(
            db,
            skill.tenant_id,
            hr_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed", "system_seeded": True},
        )
    _sync_seed_agent_branch(db, skill, agent_name="人事")
    db.flush()


def ensure_travel_reimbursement_deterministic_version(db: Session) -> None:
    """从历史差旅报销卡派生政策、评估、验票、确认、提交和财务接管版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == TRAVEL_REIMBURSEMENT_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION
    ):
        return
    _ensure_travel_reimbursement_tools(db)
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _travel_reimbursement_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("差旅报销确定性版本包含兼容告警，禁止自动发布")
    skill.version = TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    finance_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == "财务",
            AgentProfile.status == "active",
        )
    ).first()
    if finance_agent is not None:
        ensure_private_resource_binding(
            db,
            skill.tenant_id,
            finance_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed", "system_seeded": True},
        )
        assessment_tool = db.exec(
            select(Tool).where(
                Tool.tenant_id == skill.tenant_id,
                Tool.name == "expense.travel_policy_assess",
            )
        ).first()
        if assessment_tool is not None:
            ensure_private_resource_binding(
                db,
                skill.tenant_id,
                finance_agent.id,
                "tool",
                assessment_tool.id,
                "active",
                metadata_json={"source": "demo_seed", "system_seeded": True},
            )
    _sync_seed_agent_branch(db, skill, agent_name="财务")
    db.flush()


def ensure_seal_application_deterministic_version(db: Session) -> None:
    """从旧用章卡派生申请台账、分级审批、结果回写和本人查询版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == SEAL_APPLICATION_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        SEAL_APPLICATION_DETERMINISTIC_VERSION
    ):
        return
    _ensure_seal_application_tools(db)
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _seal_application_deterministic_content()
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("用章申请确定性版本包含兼容告警，禁止自动发布")
    skill.version = SEAL_APPLICATION_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    _sync_seed_agent_branch(db, skill, agent_name="行政")
    db.flush()


def ensure_expense_special_approval_version(db: Session) -> None:
    """从旧超标卡派生可查询、可审计且支持顺序双级审批的确定性版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == EXPENSE_SPECIAL_APPROVAL_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        EXPENSE_SPECIAL_APPROVAL_VERSION
    ):
        return
    _ensure_expense_special_approval_tools(db)
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _expense_special_approval_content()
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("超标报销特批确定性版本包含兼容告警，禁止自动发布")
    skill.version = EXPENSE_SPECIAL_APPROVAL_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    _sync_seed_agent_branch(db, skill, agent_name="财务")
    db.flush()


def ensure_expense_special_approval_org_scope_version(db: Session) -> None:
    """从已发布 v2 派生部门子树审批版本，同时保留集中财务租户范围。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == EXPENSE_SPECIAL_APPROVAL_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None or current_version.status != "published":
        raise ValueError("超标报销特批组织范围版本必须从已发布版本派生")
    content = _expense_special_approval_content(
        version=EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
        department_scope_resolver="initiator_primary_org_subtree",
    )
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("超标报销特批组织范围版本包含兼容告警，禁止自动发布")
    skill.version = EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    _sync_seed_agent_branch(db, skill, agent_name="财务")
    db.flush()


def _ensure_expense_special_approval_tools(db: Session) -> None:
    """幂等创建特批创建、分步决定和本人查询工具，并绑定财务数字员工。"""

    common_output = {
        "type": "object",
        "properties": {
            "approval_request_id": {"type": "string"},
            "request_type": {
                "type": "string",
                "enum": ["expense_special_approval"],
            },
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "rejected", "expired"],
            },
            "policy_key": {"type": "string"},
            "approval_route": {
                "type": "string",
                "enum": ["department_only", "department_finance"],
            },
            "original_limit": {"type": "number"},
            "claimed_amount": {"type": "number"},
            "over_limit_amount": {"type": "number"},
            "over_limit_ratio": {"type": "number"},
            "current_step": {"type": "integer"},
            "total_steps": {"type": "integer"},
            "revision": {"type": "integer"},
            "message": {"type": "string"},
        },
        "required": [
            "approval_request_id",
            "request_type",
            "status",
            "policy_key",
            "approval_route",
            "original_limit",
            "claimed_amount",
            "over_limit_amount",
            "over_limit_ratio",
            "current_step",
            "total_steps",
            "revision",
            "message",
        ],
    }
    request_input = {
        "type": "object",
        "properties": {
            "approval_request_id": {"type": "string"},
        },
        "required": ["approval_request_id"],
    }
    tool_specs = (
        (
            "expense.special_approval_create",
            "创建超标报销特批",
            {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                    "employee_name": {"type": ["string", "null"]},
                    "expense_category": {"type": "string"},
                    "original_limit": {"type": "number"},
                    "claimed_amount": {"type": "number"},
                    "over_limit_reason": {"type": "string"},
                },
                "required": [
                    "employee_id",
                    "expense_category",
                    "original_limit",
                    "claimed_amount",
                    "over_limit_reason",
                ],
            },
            "expense.special_approval.create",
        ),
        (
            "expense.special_approval_step1_approve",
            "回写部门负责人批准",
            request_input,
            "expense.special_approval.finalize",
        ),
        (
            "expense.special_approval_step1_reject",
            "回写部门负责人驳回",
            request_input,
            "expense.special_approval.finalize",
        ),
        (
            "expense.special_approval_step2_approve",
            "回写财务负责人批准",
            request_input,
            "expense.special_approval.finalize",
        ),
        (
            "expense.special_approval_step2_reject",
            "回写财务负责人驳回",
            request_input,
            "expense.special_approval.finalize",
        ),
        (
            "expense.special_approval_query",
            "查询本人超标报销特批",
            request_input,
            None,
        ),
    )
    finance_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "财务",
            AgentProfile.status == "active",
        )
    ).first()
    for name, display_name, input_schema, permission_code in tool_specs:
        tool = db.exec(
            select(Tool).where(
                Tool.tenant_id == "tenant_demo",
                Tool.name == name,
            )
        ).first()
        if tool is None:
            tool = Tool(
                tenant_id="tenant_demo",
                name=name,
                display_name=display_name,
                description="由统一审批台账执行的受控超标报销特批工具。",
                bucket="财务审批",
                tool_type="builtin",
                method="POST",
                url=f"builtin://{name}",
                headers_json={},
                auth_json={},
                config_json={"handler": name},
                input_schema=input_schema,
                output_schema=common_output,
                allowed_skills_json=[EXPENSE_SPECIAL_APPROVAL_SKILL_ID],
                required_permission_code=permission_code,
                permission_authorization_mode="workflow_delegated",
                enabled=True,
            )
            db.add(tool)
            db.flush()
        else:
            tool.display_name = display_name
            tool.description = "由统一审批台账执行的受控超标报销特批工具。"
            tool.bucket = "财务审批"
            tool.tool_type = "builtin"
            tool.method = "POST"
            tool.url = f"builtin://{name}"
            tool.config_json = {"handler": name}
            tool.input_schema = input_schema
            tool.output_schema = common_output
            tool.allowed_skills_json = [EXPENSE_SPECIAL_APPROVAL_SKILL_ID]
            tool.required_permission_code = permission_code
            tool.permission_authorization_mode = "workflow_delegated"
            tool.enabled = True
            tool.updated_at = utc_now()
            db.add(tool)
        if finance_agent is not None:
            ensure_private_resource_binding(
                db,
                "tenant_demo",
                finance_agent.id,
                "tool",
                tool.id,
                "active",
                metadata_json={"source": "demo_seed", "system_seeded": True},
            )


def _expense_special_approval_content(
    *,
    version: str = EXPENSE_SPECIAL_APPROVAL_VERSION,
    department_scope_resolver: str | None = None,
) -> dict[str, object]:
    """构造指定版本的双级特批图，并可显式收口部门审批参与范围。"""

    receipt_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "data": {
                "type": "object",
                "properties": {
                    "approval_request_id": {"type": "string"},
                    "status": {"type": "string"},
                    "approval_route": {"type": "string"},
                    "current_step": {"type": "integer"},
                    "total_steps": {"type": "integer"},
                },
            },
        },
    }
    human_policy_base = {
        "completion_mode": "any",
        "claim_required": True,
        "exclude_initiator": True,
        "timeout_seconds": 86400,
        "timeout_action": "fail",
        "allowed_outcomes": ["approved", "rejected"],
        "action_permissions": {
            "claim": "expense.special_approval.claim",
            "outcome:approved": "expense.special_approval.approve",
            "outcome:rejected": "expense.special_approval.reject",
        },
        "outcome_options": [
            {
                "value": "approved",
                "label": "批准本级",
                "tone": "success",
                "comment_required": True,
                "completion_message": (
                    "特批申请 {approval_request_id} 本级已批准；"
                    "业务状态：{business_status}；意见：{comment}。"
                ),
            },
            {
                "value": "rejected",
                "label": "驳回申请",
                "tone": "danger",
                "comment_required": True,
                "completion_message": (
                    "特批申请 {approval_request_id} 已驳回；"
                    "业务状态：{business_status}；意见：{comment}。"
                ),
            },
        ],
    }
    request_id_input = {
        "approval_request_id": "tool_result.special_application.data.approval_request_id"
    }
    return {
        "skill_id": EXPENSE_SPECIAL_APPROVAL_SKILL_ID,
        "name": "超标报销特批",
        "version": version,
        "business_domain": "财务报销",
        "description": "按服务端计算的超标比例创建一级或顺序双级特批，并查询权威业务状态。",
        "execution_mode": "deterministic",
        "trigger_intents": [
            "报销超标",
            "差旅超标申请",
            "申请超标特批",
            "查询特批申请",
        ],
        "user_utterance_examples": [
            "标准1000元，实际报销1100元，申请超标特批",
            "标准1000元，实际报销1300元，申请双级特批",
            "查询特批申请 SPECIAL-123456789ABC",
        ],
        "goal": [
            "可信绑定申请人并取得超标制度证据",
            "按原标准和申报额计算超标比例",
            "明确确认后创建特批业务单",
            "按冻结顺序完成部门及必要的财务审批",
            "回写并查询最终业务状态",
        ],
        "required_info": ["request_action"],
        "condition_schemas": {
            "slots": {
                "type": "object",
                "properties": {
                    "request_action": {
                        "type": "string",
                        "enum": ["create", "query"],
                    },
                    "employee_id": {"type": "string"},
                    "employee_name": {"type": "string"},
                    "expense_category": {"type": "string"},
                    "original_limit": {"type": "number"},
                    "claimed_amount": {"type": "number"},
                    "over_limit_reason": {"type": "string"},
                    "approval_request_id": {"type": "string"},
                    "confirmation": {
                        "type": "string",
                        "enum": ["confirmed", "cancelled"],
                    },
                },
            },
            "node_output": {
                "type": "object",
                "properties": {
                    "special_policy": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {"outcome": {"type": "string"}},
                            },
                        },
                    }
                },
            },
            "tool_result": {
                "type": "object",
                "properties": {
                    "special_application": receipt_schema,
                    "department_decision": receipt_schema,
                    "finance_decision": receipt_schema,
                    "special_query": receipt_schema,
                },
            },
            "work_item": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "outcome": {"type": "string"},
                },
            },
        },
        "nodes": [
            {
                "node_id": "collect_special_action",
                "type": "collect_info",
                "name": "识别特批办理动作",
                "instruction": "从本轮明确识别新建超标特批或按申请单号查询。",
                "expected_user_info": ["request_action"],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "value_aliases": {
                        "request_action": {
                            "申请": "create",
                            "特批": "create",
                            "create": "create",
                            "查询": "query",
                            "进度": "query",
                            "状态": "query",
                            "query": "query",
                        }
                    }
                },
            },
            {
                "node_id": "route_special_action",
                "type": "decision",
                "name": "分流申请与查询",
                "instruction": "只按规范动作选择创建或本人查询路径。",
                "allowed_actions": ["continue_flow"],
            },
            {
                "node_id": "collect_special_application",
                "type": "collect_info",
                "name": "收集超标特批事实",
                "instruction": "一次收集费用类型、原标准、实际申报金额和客观超标原因。",
                "expected_user_info": [
                    "employee_id",
                    "employee_name",
                    "expense_category",
                    "original_limit",
                    "claimed_amount",
                    "over_limit_reason",
                ],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "input_bindings": {
                        "employee_id": {
                            "source": "authenticated_employee",
                            "attribute": "employee_id",
                            "allow_override_roles": [],
                        },
                        "employee_name": {
                            "source": "authenticated_employee",
                            "attribute": "employee_name",
                            "allow_override_roles": [],
                        },
                    }
                },
            },
            {
                "node_id": "query_special_policy",
                "type": "knowledge_query",
                "name": "核对超标审批制度",
                "instruction": "检索超标金额相对原标准的比例和对应审批链。",
                "allowed_actions": ["query_knowledge"],
                "metadata": {
                    "knowledge_query": {
                        "query_type": "policy_check",
                        "desired_evidence": (
                            "超标审批流程;超标金额 ≤ 标准的 20%;"
                            "超标金额 > 标准的 20%;部门负责人;财务负责人;来源"
                        ),
                        "max_chunks": 6,
                        "max_depth": 2,
                    },
                    "operation_input": {
                        "expense_category": "slots.expense_category",
                        "original_limit": "slots.original_limit",
                        "claimed_amount": "slots.claimed_amount",
                        "over_limit_reason": "slots.over_limit_reason",
                    },
                    "operation_result_key": "special_policy",
                },
            },
            {
                "node_id": "confirm_special_application",
                "type": "collect_info",
                "name": "确认超标特批申请",
                "instruction": "展示费用、原标准、申报额和原因，等待当前轮明确确认或取消。",
                "expected_user_info": ["confirmation"],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "confirmation_policy": {
                        "slot_name": "confirmation",
                        "phrase_values": {
                            "确认": "confirmed",
                            "确认申请": "confirmed",
                            "确认提交": "confirmed",
                            "confirmed": "confirmed",
                            "取消": "cancelled",
                            "取消申请": "cancelled",
                            "cancelled": "cancelled",
                        },
                        "prompt": (
                            "请核对费用类型、原标准、实际申报金额和超标原因，"
                            "回复“确认申请”或“取消申请”。"
                        ),
                    }
                },
            },
            {
                "node_id": "create_special_application",
                "type": "tool_call",
                "name": "创建超标报销特批",
                "instruction": "仅在制度证据充分且当前轮确认后创建一次特批业务单。",
                "allowed_actions": ["call_tool:expense.special_approval_create"],
                "metadata": {
                    "operation_input": {
                        "employee_id": "slots.employee_id",
                        "employee_name": "slots.employee_name",
                        "expense_category": "slots.expense_category",
                        "original_limit": "slots.original_limit",
                        "claimed_amount": "slots.claimed_amount",
                        "over_limit_reason": "slots.over_limit_reason",
                    },
                    "operation_result_key": "special_application",
                },
            },
            {
                "node_id": "department_special_approval",
                "type": "human_task",
                "name": "部门负责人审批",
                "instruction": "由部门负责人认领并提交本级批准或驳回决定。",
                "metadata": {
                    "participant_policy": {
                        **human_policy_base,
                        "candidate_role_codes": [EXPENSE_DEPARTMENT_APPROVER_ROLE],
                        **(
                            {"participant_scope_resolver": department_scope_resolver}
                            if department_scope_resolver
                            else {}
                        ),
                        "waiting_message": (
                            "超标特批 {approval_request_id} 已创建，"
                            "正在等待部门负责人认领处理。"
                        ),
                    }
                },
            },
            {
                "node_id": "finance_special_approval",
                "type": "human_task",
                "name": "财务负责人审批",
                "instruction": "部门批准后，由财务负责人认领并提交第二级批准或驳回决定。",
                "metadata": {
                    "participant_policy": {
                        **human_policy_base,
                        "candidate_role_codes": [EXPENSE_FINANCE_APPROVER_ROLE],
                        "waiting_message": (
                            "超标特批 {approval_request_id} 已通过部门审批，"
                            "正在等待财务负责人认领处理。"
                        ),
                    }
                },
            },
            *[
                {
                    "node_id": node_id,
                    "type": "tool_call",
                    "name": display_name,
                    "instruction": instruction,
                    "allowed_actions": [f"call_tool:{tool_name}"],
                    "metadata": {
                        "operation_input": request_id_input,
                        "operation_result_key": result_key,
                    },
                }
                for node_id, display_name, instruction, tool_name, result_key in (
                    (
                        "record_department_approve",
                        "回写部门批准",
                        "只依据部门工作项批准事实推进业务单。",
                        "expense.special_approval_step1_approve",
                        "department_decision",
                    ),
                    (
                        "record_department_reject",
                        "回写部门驳回",
                        "只依据部门工作项驳回事实结束业务单。",
                        "expense.special_approval_step1_reject",
                        "department_decision",
                    ),
                    (
                        "record_finance_approve",
                        "回写财务批准",
                        "只依据财务工作项批准事实结束业务单。",
                        "expense.special_approval_step2_approve",
                        "finance_decision",
                    ),
                    (
                        "record_finance_reject",
                        "回写财务驳回",
                        "只依据财务工作项驳回事实结束业务单。",
                        "expense.special_approval_step2_reject",
                        "finance_decision",
                    ),
                )
            ],
            {
                "node_id": "collect_special_query",
                "type": "collect_info",
                "name": "收集特批申请单号",
                "instruction": "要求提供完整 SPECIAL 申请单号。",
                "expected_user_info": ["approval_request_id"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "query_special_application",
                "type": "tool_call",
                "name": "查询本人超标特批",
                "instruction": "按申请单号查询，服务端再次校验原申请人。",
                "allowed_actions": ["call_tool:expense.special_approval_query"],
                "metadata": {
                    "operation_input": {
                        "approval_request_id": "slots.approval_request_id"
                    },
                    "operation_result_key": "special_query",
                },
            },
            *[
                {
                    "node_id": node_id,
                    "type": "response",
                    "name": name,
                    "instruction": instruction,
                    "allowed_actions": ["answer_user"],
                }
                for node_id, name, instruction in (
                    (
                        "special_application_approved",
                        "超标特批已批准",
                        "只按业务台账反馈 approved，不表述为已报销或已打款。",
                    ),
                    (
                        "special_application_rejected",
                        "超标特批已驳回",
                        "只按业务台账反馈 rejected 和申请单号。",
                    ),
                    (
                        "special_query_completed",
                        "超标特批查询完成",
                        "反馈本人申请的单号、状态、当前步骤和审批链。",
                    ),
                    (
                        "special_application_cancelled",
                        "超标特批已取消",
                        "确认没有创建特批业务单或工作项。",
                    ),
                    (
                        "special_policy_unavailable",
                        "超标制度依据不足",
                        "说明未取得充分制度依据，不得创建特批。",
                    ),
                    (
                        "special_application_failed",
                        "超标特批处理失败",
                        "说明技术失败，不得伪装成业务批准或驳回。",
                    ),
                )
            ],
        ],
        "edges": [
            {
                "source_node_id": "collect_special_action",
                "next_node_id": "route_special_action",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "route_special_action",
                "next_node_id": "collect_special_application",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.request_action"},
                    "right": {"value": "create"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "route_special_action",
                "next_node_id": "collect_special_query",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "collect_special_application",
                "next_node_id": "query_special_policy",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "query_special_policy",
                "next_node_id": "confirm_special_application",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "node_output.special_policy.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "node_output.special_policy.data.outcome"
                            },
                            "right": {"value": "evidence_found"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "query_special_policy",
                "next_node_id": "special_policy_unavailable",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "confirm_special_application",
                "next_node_id": "create_special_application",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.confirmation"},
                    "right": {"value": "confirmed"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "confirm_special_application",
                "next_node_id": "special_application_cancelled",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "create_special_application",
                "next_node_id": "department_special_approval",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.special_application.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "tool_result.special_application.data.status"
                            },
                            "right": {"value": "pending"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "create_special_application",
                "next_node_id": "special_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "department_special_approval",
                "next_node_id": "record_department_approve",
                "condition": {
                    "op": "eq",
                    "left": {"path": "work_item.outcome"},
                    "right": {"value": "approved"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "department_special_approval",
                "next_node_id": "record_department_reject",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "record_department_approve",
                "next_node_id": "special_application_approved",
                "condition": {
                    "op": "eq",
                    "left": {
                        "path": "tool_result.department_decision.data.status"
                    },
                    "right": {"value": "approved"},
                },
                "priority": 200,
            },
            {
                "source_node_id": "record_department_approve",
                "next_node_id": "finance_special_approval",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {
                                "path": "tool_result.department_decision.status"
                            },
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "tool_result.department_decision.data.status"
                            },
                            "right": {"value": "pending"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "tool_result.department_decision.data.current_step"
                            },
                            "right": {"value": 2},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "record_department_approve",
                "next_node_id": "special_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "record_department_reject",
                "next_node_id": "special_application_rejected",
                "condition": {
                    "op": "eq",
                    "left": {"path": "tool_result.department_decision.data.status"},
                    "right": {"value": "rejected"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "record_department_reject",
                "next_node_id": "special_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "finance_special_approval",
                "next_node_id": "record_finance_approve",
                "condition": {
                    "op": "eq",
                    "left": {"path": "work_item.outcome"},
                    "right": {"value": "approved"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "finance_special_approval",
                "next_node_id": "record_finance_reject",
                "condition": {"op": "always"},
                "priority": 0,
            },
            *[
                {
                    "source_node_id": source,
                    "next_node_id": target,
                    "condition": {
                        "op": "eq",
                        "left": {"path": f"tool_result.{result_key}.data.status"},
                        "right": {"value": status},
                    },
                    "priority": 100,
                }
                for source, target, result_key, status in (
                    (
                        "record_finance_approve",
                        "special_application_approved",
                        "finance_decision",
                        "approved",
                    ),
                    (
                        "record_finance_reject",
                        "special_application_rejected",
                        "finance_decision",
                        "rejected",
                    ),
                )
            ],
            *[
                {
                    "source_node_id": source,
                    "next_node_id": "special_application_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                }
                for source in ("record_finance_approve", "record_finance_reject")
            ],
            {
                "source_node_id": "collect_special_query",
                "next_node_id": "query_special_application",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "query_special_application",
                "next_node_id": "special_query_completed",
                "condition": {
                    "op": "in",
                    "left": {"path": "tool_result.special_query.data.status"},
                    "right": {
                        "value": ["pending", "approved", "rejected", "expired"]
                    },
                },
                "priority": 100,
            },
            {
                "source_node_id": "query_special_application",
                "next_node_id": "special_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
        ],
        "start_node_id": "collect_special_action",
        "terminal_node_ids": [
            "special_application_approved",
            "special_application_rejected",
            "special_query_completed",
            "special_application_cancelled",
            "special_policy_unavailable",
            "special_application_failed",
        ],
        "interruption_policy": {},
        "response_rules": [
            "申请人身份只取登录员工档案，不接受用户覆盖。",
            "超标比例必须由服务端按（申报额-原标准）/原标准计算，模型不得自报比例。",
            "知识证据不足、取消或工具失败时不得创建特批业务单。",
            "超标比例不超过20%只走部门负责人；超过20%必须先部门后财务顺序审批。",
            "任一级驳回立即结束；第二级不得在第一级批准前办理。",
            "approved 只表示超标特批通过，不表示报销已提交、付款或到账。",
            "查询只允许原申请人读取本人申请。",
        ],
        "slot_filling_policy": {
            "enabled": True,
            "multi_slot_per_turn": True,
            "skip_satisfied_steps": True,
            "extract_scope": "all_skill_expected_user_info",
            "target_info": [
                "request_action",
                "employee_id",
                "employee_name",
                "expense_category",
                "original_limit",
                "claimed_amount",
                "over_limit_reason",
                "approval_request_id",
                "confirmation",
            ],
        },
    }


def _ensure_seal_application_tools(db: Session) -> None:
    """幂等创建用章申请创建、批准、驳回和本人查询内置工具及行政员工绑定。"""

    common_output = {
        "type": "object",
        "properties": {
            "approval_request_id": {"type": "string"},
            "request_type": {"type": "string", "enum": ["seal_application"]},
            "status": {"type": "string", "enum": ["pending", "approved", "rejected"]},
            "policy_key": {"type": "string"},
            "approval_level": {"type": "string", "enum": ["normal", "important"]},
            "current_step": {"type": "integer"},
            "total_steps": {"type": "integer"},
            "document_name": {"type": "string"},
            "revision": {"type": "integer"},
            "message": {"type": "string"},
        },
        "required": [
            "approval_request_id",
            "request_type",
            "status",
            "policy_key",
            "approval_level",
            "current_step",
            "total_steps",
            "document_name",
            "revision",
            "message",
        ],
    }
    tool_specs = (
        (
            "admin.seal_application_create",
            "创建用章审批申请",
            {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                    "employee_name": {"type": ["string", "null"]},
                    "seal_type": {
                        "type": "string",
                        "enum": ["company", "contract", "finance"],
                    },
                    "seal_purpose": {"type": "string"},
                    "document_name": {"type": "string"},
                    "document_type": {
                        "type": "string",
                        "enum": ["ordinary_document", "contract"],
                    },
                    "contract_amount": {"type": ["number", "null"]},
                },
                "required": [
                    "employee_id",
                    "seal_type",
                    "seal_purpose",
                    "document_name",
                    "document_type",
                ],
            },
            "admin.seal_application.create",
        ),
        (
            "admin.seal_application_approve",
            "回写用章审批通过",
            {
                "type": "object",
                "properties": {"approval_request_id": {"type": "string"}},
                "required": ["approval_request_id"],
            },
            "admin.seal_application.finalize",
        ),
        (
            "admin.seal_application_reject",
            "回写用章审批驳回",
            {
                "type": "object",
                "properties": {"approval_request_id": {"type": "string"}},
                "required": ["approval_request_id"],
            },
            "admin.seal_application.finalize",
        ),
        (
            "admin.seal_application_query",
            "查询本人用章申请",
            {
                "type": "object",
                "properties": {"approval_request_id": {"type": "string"}},
                "required": ["approval_request_id"],
            },
            None,
        ),
    )
    administration_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "行政",
            AgentProfile.status == "active",
        )
    ).first()
    for name, display_name, input_schema, permission_code in tool_specs:
        tool = db.exec(
            select(Tool).where(
                Tool.tenant_id == "tenant_demo",
                Tool.name == name,
            )
        ).first()
        if tool is None:
            tool = Tool(
                tenant_id="tenant_demo",
                name=name,
                display_name=display_name,
                description="由统一审批申请台账执行的受控内置工具。",
                bucket="行政审批",
                tool_type="builtin",
                method="POST",
                url=f"builtin://{name}",
                headers_json={},
                auth_json={},
                config_json={"handler": name},
                input_schema=input_schema,
                output_schema=common_output,
                allowed_skills_json=[SEAL_APPLICATION_SKILL_ID],
                required_permission_code=permission_code,
                permission_authorization_mode="workflow_delegated",
                enabled=True,
            )
            db.add(tool)
            db.flush()
        else:
            tool.display_name = display_name
            tool.description = "由统一审批申请台账执行的受控内置工具。"
            tool.bucket = "行政审批"
            tool.tool_type = "builtin"
            tool.method = "POST"
            tool.url = f"builtin://{name}"
            tool.config_json = {"handler": name}
            tool.input_schema = input_schema
            tool.output_schema = common_output
            tool.allowed_skills_json = [SEAL_APPLICATION_SKILL_ID]
            tool.required_permission_code = permission_code
            tool.permission_authorization_mode = "workflow_delegated"
            tool.enabled = True
            tool.updated_at = utc_now()
            db.add(tool)
        if administration_agent is not None:
            ensure_private_resource_binding(
                db,
                "tenant_demo",
                administration_agent.id,
                "tool",
                tool.id,
                "active",
                metadata_json={"source": "demo_seed", "system_seeded": True},
            )


def _seal_application_deterministic_content() -> dict[str, object]:
    """构造用章创建、普通/重要分级审批、权威回写和本人查询图。"""

    work_item_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "outcome": {"type": "string"},
        },
    }
    receipt_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "data": {
                "type": "object",
                "properties": {
                    "approval_request_id": {"type": "string"},
                    "status": {"type": "string"},
                    "approval_level": {"type": "string"},
                },
            },
        },
    }
    human_policy_base = {
        "completion_mode": "any",
        "claim_required": True,
        "exclude_initiator": True,
        "timeout_seconds": 86400,
        "timeout_action": "fail",
        "allowed_outcomes": ["approved", "rejected"],
        "action_permissions": {
            "claim": "admin.seal_application.claim",
            "outcome:approved": "admin.seal_application.approve",
            "outcome:rejected": "admin.seal_application.reject",
        },
        "outcome_options": [
            {
                "value": "approved",
                "label": "批准用章",
                "tone": "success",
                "comment_required": True,
                "completion_message": (
                    "用章申请 {approval_request_id} 已批准；业务状态：{business_status}；"
                    "处理意见：{comment}。"
                ),
            },
            {
                "value": "rejected",
                "label": "驳回用章",
                "tone": "danger",
                "comment_required": True,
                "completion_message": (
                    "用章申请 {approval_request_id} 已驳回；业务状态：{business_status}；"
                    "处理意见：{comment}。"
                ),
            },
        ],
    }
    return {
        "skill_id": SEAL_APPLICATION_SKILL_ID,
        "name": "用章申请审批",
        "version": SEAL_APPLICATION_DETERMINISTIC_VERSION,
        "business_domain": "行政与印章管理",
        "description": "创建可查询的用章申请，由分级真人审批并把权威决定回写业务台账。",
        "execution_mode": "deterministic",
        "trigger_intents": [
            "申请用章",
            "盖章申请",
            "用印审批",
            "合同盖章",
            "查询用章申请",
        ],
        "user_utterance_examples": [
            "我要申请公司公章用于普通证明文件",
            "这份重要合同需要盖合同专用章",
            "查询用章申请 SEAL-123456789ABC",
        ],
        "goal": [
            "可信绑定申请人身份",
            "取得用章制度证据",
            "明确确认后创建申请单",
            "由匹配级别的真人审批",
            "回写并查询最终业务状态",
        ],
        "required_info": ["request_action"],
        "condition_schemas": {
            "slots": {
                "type": "object",
                "properties": {
                    "request_action": {
                        "type": "string",
                        "enum": ["create", "query"],
                    },
                    "employee_id": {"type": "string"},
                    "employee_name": {"type": "string"},
                    "seal_type": {
                        "type": "string",
                        "enum": ["company", "contract", "finance"],
                    },
                    "seal_purpose": {"type": "string"},
                    "document_name": {"type": "string"},
                    "document_type": {
                        "type": "string",
                        "enum": ["ordinary_document", "contract"],
                    },
                    "contract_amount": {"type": "number"},
                    "approval_request_id": {"type": "string"},
                    "confirmation": {
                        "type": "string",
                        "enum": ["confirmed", "cancelled"],
                    },
                },
            },
            "node_output": {
                "type": "object",
                "properties": {
                    "seal_policy": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {
                                    "outcome": {"type": "string"},
                                },
                            },
                        },
                    }
                },
            },
            "tool_result": {
                "type": "object",
                "properties": {
                    "seal_application": receipt_schema,
                    "seal_decision": receipt_schema,
                    "seal_query": receipt_schema,
                },
            },
            "work_item": work_item_schema,
        },
        "nodes": [
            {
                "node_id": "collect_request_action",
                "type": "collect_info",
                "name": "识别用章办理动作",
                "instruction": "从本轮明确识别新建申请或按申请单号查询状态。",
                "expected_user_info": ["request_action"],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "value_aliases": {
                        "request_action": {
                            "申请": "create",
                            "发起": "create",
                            "create": "create",
                            "查询": "query",
                            "进度": "query",
                            "状态": "query",
                            "query": "query",
                        }
                    }
                },
            },
            {
                "node_id": "route_request_action",
                "type": "decision",
                "name": "分流申请与查询",
                "instruction": "只按规范动作选择创建或本人查询路径。",
                "allowed_actions": ["continue_flow"],
            },
            {
                "node_id": "collect_seal_application",
                "type": "collect_info",
                "name": "收集用章申请",
                "instruction": "一次收集印章类型、用途、文件名称和文件类型；申请人身份只取登录档案。",
                "expected_user_info": [
                    "employee_id",
                    "employee_name",
                    "seal_type",
                    "seal_purpose",
                    "document_name",
                    "document_type",
                ],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "input_bindings": {
                        "employee_id": {
                            "source": "authenticated_employee",
                            "attribute": "employee_id",
                            "allow_override_roles": [],
                        },
                        "employee_name": {
                            "source": "authenticated_employee",
                            "attribute": "employee_name",
                            "allow_override_roles": [],
                        },
                    },
                    "value_aliases": {
                        "seal_type": {
                            "company": "company",
                            "公章": "company",
                            "公司公章": "company",
                            "contract": "contract",
                            "合同章": "contract",
                            "合同专用章": "contract",
                            "finance": "finance",
                            "财务章": "finance",
                            "财务专用章": "finance",
                        },
                        "document_type": {
                            "ordinary_document": "ordinary_document",
                            "普通文件": "ordinary_document",
                            "证明文件": "ordinary_document",
                            "普通证明": "ordinary_document",
                            "contract": "contract",
                            "合同": "contract",
                            "重要合同": "contract",
                        },
                    },
                },
            },
            {
                "node_id": "query_seal_policy",
                "type": "knowledge_query",
                "name": "核对用章制度",
                "instruction": "检索当前印章、文件类型和用途对应的审批权限及禁止事项。",
                "allowed_actions": ["query_knowledge"],
                "metadata": {
                    "knowledge_query": {
                        "query_type": "policy_check",
                        "desired_evidence": (
                            "用章类型;审批链;重要合同用章;印章一律不外借;来源"
                        ),
                        "max_chunks": 6,
                        "max_depth": 2,
                    },
                    "operation_input": {
                        "seal_type": "slots.seal_type",
                        "seal_purpose": "slots.seal_purpose",
                        "document_name": "slots.document_name",
                        "document_type": "slots.document_type",
                    },
                    "operation_result_key": "seal_policy",
                },
            },
            {
                "node_id": "confirm_seal_application",
                "type": "collect_info",
                "name": "确认用章申请",
                "instruction": "展示用章申请摘要，等待当前轮明确确认或取消。",
                "expected_user_info": ["confirmation"],
                "allowed_actions": ["ask_user", "continue_flow"],
                "metadata": {
                    "confirmation_policy": {
                        "slot_name": "confirmation",
                        "phrase_values": {
                            "确认": "confirmed",
                            "确认申请": "confirmed",
                            "确认提交": "confirmed",
                            "confirmed": "confirmed",
                            "取消": "cancelled",
                            "取消申请": "cancelled",
                            "cancelled": "cancelled",
                        },
                        "prompt": (
                            "请核对印章类型、用途、文件名称和文件类型，"
                            "回复“确认申请”或“取消申请”。"
                        ),
                    }
                },
            },
            {
                "node_id": "create_seal_application",
                "type": "tool_call",
                "name": "创建用章审批申请",
                "instruction": "仅在制度证据充分且当前轮确认后创建一次审批申请。",
                "allowed_actions": ["call_tool:admin.seal_application_create"],
                "metadata": {
                    "operation_input": {
                        "employee_id": "slots.employee_id",
                        "employee_name": "slots.employee_name",
                        "seal_type": "slots.seal_type",
                        "seal_purpose": "slots.seal_purpose",
                        "document_name": "slots.document_name",
                        "document_type": "slots.document_type",
                        "contract_amount": "slots.contract_amount",
                    },
                    "operation_result_key": "seal_application",
                },
            },
            {
                "node_id": "route_seal_approval_level",
                "type": "decision",
                "name": "按申请台账级别分流",
                "instruction": "只依据创建回执中的 normal/important 选择审批角色。",
                "allowed_actions": ["continue_flow"],
            },
            {
                "node_id": "normal_seal_approval",
                "type": "human_task",
                "name": "普通用章审批",
                "instruction": "由用章审批人认领并提交批准或驳回决定。",
                "metadata": {
                    "participant_policy": {
                        **human_policy_base,
                        "candidate_role_codes": [SEAL_APPLICATION_APPROVER_ROLE],
                        "waiting_message": (
                            "用章申请 {approval_request_id} 已创建，"
                            "正在等待用章审批人认领处理。"
                        ),
                    }
                },
            },
            {
                "node_id": "important_seal_approval",
                "type": "human_task",
                "name": "重要用章审批",
                "instruction": "由重要用章审批人认领并提交批准或驳回决定。",
                "metadata": {
                    "participant_policy": {
                        **human_policy_base,
                        "candidate_role_codes": [SEAL_APPLICATION_SENIOR_APPROVER_ROLE],
                        "waiting_message": (
                            "重要用章申请 {approval_request_id} 已创建，"
                            "正在等待重要用章审批人认领处理。"
                        ),
                    }
                },
            },
            {
                "node_id": "approve_seal_application",
                "type": "tool_call",
                "name": "回写用章批准结果",
                "instruction": "仅依据关联工作项的已批准事实回写业务申请。",
                "allowed_actions": ["call_tool:admin.seal_application_approve"],
                "metadata": {
                    "operation_input": {
                        "approval_request_id": (
                            "tool_result.seal_application.data.approval_request_id"
                        )
                    },
                    "operation_result_key": "seal_decision",
                },
            },
            {
                "node_id": "reject_seal_application",
                "type": "tool_call",
                "name": "回写用章驳回结果",
                "instruction": "仅依据关联工作项的已驳回事实回写业务申请。",
                "allowed_actions": ["call_tool:admin.seal_application_reject"],
                "metadata": {
                    "operation_input": {
                        "approval_request_id": (
                            "tool_result.seal_application.data.approval_request_id"
                        )
                    },
                    "operation_result_key": "seal_decision",
                },
            },
            {
                "node_id": "collect_seal_query",
                "type": "collect_info",
                "name": "收集用章申请单号",
                "instruction": "要求提供完整 SEAL 申请单号，不收集或信任他人员工号。",
                "expected_user_info": ["approval_request_id"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "query_seal_application",
                "type": "tool_call",
                "name": "查询本人用章申请",
                "instruction": "按申请单号查询，工具服务端必须再次校验原申请人。",
                "allowed_actions": ["call_tool:admin.seal_application_query"],
                "metadata": {
                    "operation_input": {
                        "approval_request_id": "slots.approval_request_id"
                    },
                    "operation_result_key": "seal_query",
                },
            },
            {
                "node_id": "seal_application_approved",
                "type": "response",
                "name": "用章申请已批准",
                "instruction": "只按业务台账回执反馈申请单号和 approved，不能表述为已经实际盖章。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "seal_application_rejected",
                "type": "response",
                "name": "用章申请已驳回",
                "instruction": "只按业务台账回执反馈申请单号和 rejected。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "seal_query_completed",
                "type": "response",
                "name": "用章申请查询完成",
                "instruction": "只反馈本人申请的申请单号、当前状态、文件和审批级别。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "seal_application_cancelled",
                "type": "response",
                "name": "用章申请已取消",
                "instruction": "确认没有创建申请单或审批工作项。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "seal_policy_unavailable",
                "type": "response",
                "name": "用章制度依据不足",
                "instruction": "说明未取得充分制度依据，不得创建申请。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "seal_application_failed",
                "type": "response",
                "name": "用章申请处理失败",
                "instruction": "说明技术处理失败，不得伪装成业务批准或驳回。",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {
                "source_node_id": "collect_request_action",
                "next_node_id": "route_request_action",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "route_request_action",
                "next_node_id": "collect_seal_application",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.request_action"},
                    "right": {"value": "create"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "route_request_action",
                "next_node_id": "collect_seal_query",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "collect_seal_application",
                "next_node_id": "query_seal_policy",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "query_seal_policy",
                "next_node_id": "confirm_seal_application",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "node_output.seal_policy.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "node_output.seal_policy.data.outcome"
                            },
                            "right": {"value": "evidence_found"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "query_seal_policy",
                "next_node_id": "seal_policy_unavailable",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "confirm_seal_application",
                "next_node_id": "create_seal_application",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.confirmation"},
                    "right": {"value": "confirmed"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "confirm_seal_application",
                "next_node_id": "seal_application_cancelled",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "create_seal_application",
                "next_node_id": "route_seal_approval_level",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_application.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_application.data.status"},
                            "right": {"value": "pending"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "create_seal_application",
                "next_node_id": "seal_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "route_seal_approval_level",
                "next_node_id": "important_seal_approval",
                "condition": {
                    "op": "eq",
                    "left": {
                        "path": "tool_result.seal_application.data.approval_level"
                    },
                    "right": {"value": "important"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "route_seal_approval_level",
                "next_node_id": "normal_seal_approval",
                "condition": {"op": "always"},
                "priority": 0,
            },
            *[
                {
                    "source_node_id": node_id,
                    "next_node_id": "approve_seal_application",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "approved"},
                    },
                    "priority": 100,
                }
                for node_id in ("normal_seal_approval", "important_seal_approval")
            ],
            *[
                {
                    "source_node_id": node_id,
                    "next_node_id": "reject_seal_application",
                    "condition": {"op": "always"},
                    "priority": 0,
                }
                for node_id in ("normal_seal_approval", "important_seal_approval")
            ],
            {
                "source_node_id": "approve_seal_application",
                "next_node_id": "seal_application_approved",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_decision.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_decision.data.status"},
                            "right": {"value": "approved"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "approve_seal_application",
                "next_node_id": "seal_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "reject_seal_application",
                "next_node_id": "seal_application_rejected",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_decision.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_decision.data.status"},
                            "right": {"value": "rejected"},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "reject_seal_application",
                "next_node_id": "seal_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {
                "source_node_id": "collect_seal_query",
                "next_node_id": "query_seal_application",
                "condition": {"op": "always"},
            },
            {
                "source_node_id": "query_seal_application",
                "next_node_id": "seal_query_completed",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "tool_result.seal_query.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "in",
                            "left": {"path": "tool_result.seal_query.data.status"},
                            "right": {"value": ["pending", "approved", "rejected"]},
                        },
                    ],
                },
                "priority": 100,
            },
            {
                "source_node_id": "query_seal_application",
                "next_node_id": "seal_application_failed",
                "condition": {"op": "always"},
                "priority": 0,
            },
        ],
        "start_node_id": "collect_request_action",
        "terminal_node_ids": [
            "seal_application_approved",
            "seal_application_rejected",
            "seal_query_completed",
            "seal_application_cancelled",
            "seal_policy_unavailable",
            "seal_application_failed",
        ],
        "interruption_policy": {},
        "response_rules": [
            "申请人身份只取登录员工档案，不接受用户覆盖。",
            "知识证据不足、取消或工具失败时不得创建审批申请。",
            "申请创建后必须返回 SEAL 申请单号和 pending，不能说已经批准或已经盖章。",
            "批准或驳回只能来自候选审批人提交的结构化工作项决定。",
            "业务台账回写必须校验创建操作、实例、工作项、申请级别和决定审计。",
            "approved 仅表示允许进入线下用印办理，不表示物理印章已经盖完。",
            "查询只允许原申请人读取本人申请。",
        ],
        "slot_filling_policy": {
            "enabled": True,
            "multi_slot_per_turn": True,
            "extract_scope": "all_skill_expected_user_info",
            "skip_satisfied_steps": True,
            "target_info": [
                "request_action",
                "seal_type",
                "seal_purpose",
                "document_name",
                "document_type",
                "contract_amount",
                "approval_request_id",
            ],
        },
    }


def _ensure_travel_reimbursement_tools(db: Session) -> None:
    """创建差旅评估工具并收紧评估、验票和报销提交的权限及 SOP 白名单。"""

    base_url = get_settings().normalized_tool_base_url.rstrip("/")
    tools = {
        name: db.exec(
            select(Tool).where(
                Tool.tenant_id == "tenant_demo",
                Tool.name == name,
            )
        ).first()
        for name in (
            "expense.travel_policy_assess",
            "invoice.verify",
            "expense.submit",
        )
    }
    if tools["invoice.verify"] is None or tools["expense.submit"] is None:
        raise ValueError("差旅报销所需发票查验或报销提交工具不存在，禁止自动发布")

    assessment_input_schema = {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "destination_city": {"type": "string", "minLength": 2},
            "trip_start_date": {"type": "string", "format": "date"},
            "trip_end_date": {"type": "string", "format": "date"},
            "expense_category": {"type": "string", "enum": ["lodging"]},
            "claimed_amount": {"type": "number", "exclusiveMinimum": 0},
            "trip_scope": {
                "type": "string",
                "enum": ["domestic", "overseas"],
            },
            "trip_approval_status": {
                "type": "string",
                "enum": ["approved", "not_approved"],
            },
            "trip_approval_number": {"type": "string", "minLength": 8},
        },
        "required": [
            "employee_id",
            "destination_city",
            "trip_start_date",
            "trip_end_date",
            "expense_category",
            "claimed_amount",
            "trip_scope",
            "trip_approval_status",
            "trip_approval_number",
        ],
        "additionalProperties": False,
    }
    assessment_output_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "within_limit",
                    "over_limit",
                    "unsupported",
                    "unsupported_employee",
                    "approval_unverified",
                    "late_submission",
                    "invalid_date",
                ],
            },
            "policy_code": {"type": "string"},
            "employee_level": {
                "type": ["string", "null"],
                "enum": ["staff", None],
            },
            "city_tier": {
                "type": ["string", "null"],
                "enum": ["tier_1", "tier_2", "tier_3", None],
            },
            "lodging_nights": {"type": ["integer", "null"]},
            "nightly_limit": {"type": ["number", "null"]},
            "allowance_limit": {"type": ["number", "null"]},
            "claimed_amount": {"type": "number"},
            "over_limit_amount": {"type": "number"},
            "approval_verified": {"type": "boolean"},
            "submission_deadline": {"type": ["string", "null"]},
            "days_since_trip_end": {"type": ["integer", "null"]},
            "message": {"type": "string"},
        },
    }
    assessment_tool = tools["expense.travel_policy_assess"]
    assessment_payload = {
        "display_name": "差旅住宿标准评估",
        "description": "按境内普通员工住宿标准评估固定演示行程，不连接真实财务系统。",
        "bucket": "财务报销",
        "tool_type": "http",
        "method": "POST",
        "url": f"{base_url}/api/mock/expense/travel_policy_assess",
        "headers_json": {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
        "auth_json": {},
        "config_json": {},
        "input_schema": assessment_input_schema,
        "output_schema": assessment_output_schema,
        "allowed_skills_json": [TRAVEL_REIMBURSEMENT_SKILL_ID],
        "required_permission_code": "expense.travel_policy.assess",
        "permission_authorization_mode": "workflow_delegated",
        "enabled": True,
    }
    if assessment_tool is None:
        assessment_tool = Tool(
            tenant_id="tenant_demo",
            name="expense.travel_policy_assess",
            **assessment_payload,
        )
    else:
        for field_name, field_value in assessment_payload.items():
            setattr(assessment_tool, field_name, field_value)
        assessment_tool.updated_at = utc_now()
    db.add(assessment_tool)
    db.flush()
    ensure_open_gallery_binding(
        db,
        "tenant_demo",
        "tool",
        assessment_tool.id,
        metadata_json={"source": TRAVEL_REIMBURSEMENT_SKILL_ID, "system_seeded": True},
    )

    invoice_tool = tools["invoice.verify"]
    assert invoice_tool is not None
    invoice_tool.url = f"{base_url}/api/mock/invoice/verify"
    invoice_tool.input_schema = {
        "type": "object",
        "properties": {
            "invoice_code": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "format": "date"},
            "amount": {"type": "number", "minimum": 0},
            "expected_amount": {"type": "number", "minimum": 0},
            "check_code": {"type": "string"},
            "seller": {"type": "string"},
            "buyer": {"type": "string"},
        },
        "required": ["invoice_code", "invoice_number", "invoice_date", "amount"],
        "additionalProperties": False,
    }
    invoice_tool.output_schema = {
        "type": "object",
        "properties": {
            "authentic": {"type": "boolean"},
            "fields_complete": {"type": "boolean"},
            "amount_matches": {"type": ["boolean", "null"]},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "message": {"type": "string"},
        },
    }
    invoice_tool.allowed_skills_json = sorted(
        {TRAVEL_REIMBURSEMENT_SKILL_ID} | set(invoice_tool.allowed_skills_json or [])
    )
    invoice_tool.required_permission_code = "expense.invoice.verify"
    invoice_tool.permission_authorization_mode = "workflow_delegated"
    invoice_tool.updated_at = utc_now()
    db.add(invoice_tool)

    submit_tool = tools["expense.submit"]
    assert submit_tool is not None
    submit_tool.url = f"{base_url}/api/mock/expense/submit"
    submit_tool.allowed_skills_json = sorted(
        {TRAVEL_REIMBURSEMENT_SKILL_ID} | set(submit_tool.allowed_skills_json or [])
    )
    submit_tool.required_permission_code = "expense.submit"
    submit_tool.permission_authorization_mode = "workflow_delegated"
    submit_tool.updated_at = utc_now()
    db.add(submit_tool)
    db.flush()


def _travel_reimbursement_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """把历史差旅报销迁移为政策证据、标准评估、验票、确认、提交和财务复核统一图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": TRAVEL_REIMBURSEMENT_SKILL_ID,
            "name": "差旅报销申请",
            "version": TRAVEL_REIMBURSEMENT_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "business_domain": "财务与报销",
            "description": (
                "依据当前可见差旅制度和冻结演示标准，验真本人境内住宿发票并受理报销。"
            ),
            "trigger_intents": ["申请差旅报销", "报销出差住宿费", "提交差旅发票"],
            "user_utterance_examples": [
                (
                    "报销 2026-07-20 到 2026-07-22 在杭州出差的 700 元住宿费，"
                    "事前申请已批准，原因是客户拜访"
                ),
                "我要提交一笔境内差旅住宿报销",
            ],
            "goal": [
                "使用登录账号绑定的可信员工身份",
                "先取得事前申请、提交时限、住宿标准和发票查验的制度证据",
                "由受控规则回执计算住宿晚数、限额和超标金额",
                "仅对未超标且事前批准的申请收集并查验发票",
                "取得当前轮明确确认后提交并返回 EXP 单号",
                "超标、材料或业务校验异常时创建财务复核任务且不自动提交",
            ],
            "required_info": [
                "employee_id",
                "employee_name",
                "trip_scope",
                "destination_city",
                "trip_start_date",
                "trip_end_date",
                "expense_category",
                "claimed_amount",
                "expense_reason",
                "trip_approval_status",
                "trip_approval_number",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "trip_scope": {"type": "string"},
                        "destination_city": {"type": "string"},
                        "trip_start_date": {"type": "string"},
                        "trip_end_date": {"type": "string"},
                        "expense_category": {"type": "string"},
                        "claimed_amount": {"type": "number"},
                        "expense_reason": {"type": "string"},
                        "trip_approval_status": {"type": "string"},
                        "trip_approval_number": {"type": "string"},
                        "invoice_code": {"type": "string"},
                        "invoice_number": {"type": "string"},
                        "invoice_date": {"type": "string"},
                        "invoice_amount": {"type": "number"},
                        "confirmation": {"type": "string"},
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "travel_policy": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "outcome": {
                                            "type": "string",
                                            "enum": [
                                                "evidence_found",
                                                "no_match",
                                                "insufficient",
                                            ],
                                        }
                                    },
                                },
                            },
                        }
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "travel_policy_assessment": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "within_limit",
                                                "over_limit",
                                                "unsupported",
                                                "unsupported_employee",
                                                "approval_unverified",
                                                "late_submission",
                                                "invalid_date",
                                            ],
                                        },
                                        "policy_code": {"type": "string"},
                                        "allowance_limit": {"type": "number"},
                                        "over_limit_amount": {"type": "number"},
                                    },
                                },
                            },
                        },
                        "invoice_receipt": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "authentic": {"type": "boolean"},
                                        "fields_complete": {"type": "boolean"},
                                        "amount_matches": {"type": "boolean"},
                                        "risk_level": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "travel_submission": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "expense_id": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": ["accepted", "pending", "rejected"],
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
            },
            "slot_filling_policy": {
                "enabled": True,
                "multi_slot_per_turn": True,
                "extract_scope": "all_skill_expected_user_info",
                "skip_satisfied_steps": True,
                "target_info": [
                    "employee_id",
                    "employee_name",
                    "trip_scope",
                    "destination_city",
                    "trip_start_date",
                    "trip_end_date",
                    "expense_category",
                    "claimed_amount",
                    "expense_reason",
                    "trip_approval_status",
                    "trip_approval_number",
                    "invoice_code",
                    "invoice_number",
                    "invoice_date",
                    "invoice_amount",
                    "confirmation",
                ],
            },
            "slot_key_aliases": {
                "amount": "claimed_amount",
                "description": "expense_reason",
            },
            "nodes": [
                {
                    "node_id": "collect_travel_request",
                    "type": "collect_info",
                    "name": "收集差旅报销信息",
                    "instruction": (
                        "仅办理本人境内普通员工住宿费；收集目的地、行程日期、金额、事由和事前申请状态。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "employee_name",
                        "trip_scope",
                        "destination_city",
                        "trip_start_date",
                        "trip_end_date",
                        "expense_category",
                        "claimed_amount",
                        "expense_reason",
                        "trip_approval_status",
                        "trip_approval_number",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                            },
                            "employee_name": {
                                "source": "authenticated_employee",
                                "attribute": "employee_name",
                            },
                        },
                        "value_aliases": {
                            "trip_scope": {
                                "境内": "domestic",
                                "国内": "domestic",
                                "domestic": "domestic",
                                "境外": "overseas",
                                "overseas": "overseas",
                            },
                            "expense_category": {
                                "住宿": "lodging",
                                "住宿费": "lodging",
                                "lodging": "lodging",
                            },
                            "trip_approval_status": {
                                "approved": "approved",
                                "not_approved": "not_approved",
                            },
                        },
                    },
                },
                {
                    "node_id": "query_travel_policy",
                    "type": "knowledge_query",
                    "name": "核对差旅报销制度",
                    "instruction": "取得可追溯制度依据后才能进行金额评估。",
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {
                            "destination_city": "slots.destination_city",
                            "trip_start_date": "slots.trip_start_date",
                            "trip_end_date": "slots.trip_end_date",
                            "expense_category": "slots.expense_category",
                        },
                        "operation_result_key": "travel_policy",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": (
                                "事前出差申请、出差结束后14天内提交、普通员工境内住宿标准、"
                                "住宿晚数按行程天数减1、住宿超标审批、发票验真"
                            ),
                            "max_chunks": 8,
                            "max_depth": 3,
                        },
                    },
                },
                {
                    "node_id": "assess_travel_policy",
                    "type": "tool_call",
                    "name": "评估住宿标准",
                    "instruction": "由冻结演示规则计算住宿晚数、限额和超标金额。",
                    "allowed_actions": ["call_tool:expense.travel_policy_assess"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "destination_city": "slots.destination_city",
                            "trip_start_date": "slots.trip_start_date",
                            "trip_end_date": "slots.trip_end_date",
                            "expense_category": "slots.expense_category",
                            "claimed_amount": "slots.claimed_amount",
                            "trip_scope": "slots.trip_scope",
                            "trip_approval_status": "slots.trip_approval_status",
                            "trip_approval_number": "slots.trip_approval_number",
                        },
                        "operation_result_key": "travel_policy_assessment",
                    },
                },
                {
                    "node_id": "collect_travel_documents",
                    "type": "collect_info",
                    "name": "收集出差申请与发票",
                    "instruction": "仅在政策评估通过后收集事前申请编号和完整发票字段。",
                    "expected_user_info": [
                        "invoice_code",
                        "invoice_number",
                        "invoice_date",
                        "invoice_amount",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "verify_travel_invoice",
                    "type": "tool_call",
                    "name": "查验差旅发票",
                    "instruction": "查验发票真伪、字段完整性和金额是否与申报金额一致。",
                    "allowed_actions": ["call_tool:invoice.verify"],
                    "metadata": {
                        "operation_input": {
                            "invoice_code": "slots.invoice_code",
                            "invoice_number": "slots.invoice_number",
                            "invoice_date": "slots.invoice_date",
                            "amount": "slots.invoice_amount",
                            "expected_amount": "slots.claimed_amount",
                        },
                        "operation_result_key": "invoice_receipt",
                    },
                },
                {
                    "node_id": "confirm_travel_submit",
                    "type": "collect_info",
                    "name": "确认提交差旅报销",
                    "instruction": "展示政策限额、发票查验结果和申报信息，只接受当前轮明确确认。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认提交": "confirmed",
                                "取消提交": "cancelled",
                            },
                            "prompt": "请核对差旅、限额和发票信息，并回复“确认提交”或“取消提交”。",
                        }
                    },
                },
                {
                    "node_id": "submit_travel_expense",
                    "type": "tool_call",
                    "name": "提交差旅报销",
                    "instruction": "只提交可信身份和本次已验真的报销字段。",
                    "allowed_actions": ["call_tool:expense.submit"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "category": "slots.expense_category",
                            "amount": "slots.claimed_amount",
                            "invoice_no": "slots.invoice_number",
                            "expense_date": "slots.invoice_date",
                            "description": "slots.expense_reason",
                        },
                        "operation_result_key": "travel_submission",
                    },
                },
                {
                    "node_id": "finance_travel_review",
                    "type": "human_task",
                    "name": "财务复核差旅报销",
                    "instruction": "由财务报销专员核对超标、事前申请、制度证据或发票异常。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [FINANCE_EXPENSE_SPECIALIST_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["reviewed", "needs_information"],
                            "action_permissions": {
                                "claim": "expense.travel_review.claim",
                                "outcome:reviewed": "expense.travel_review.complete",
                                "outcome:needs_information": (
                                    "expense.travel_review.request_information"
                                ),
                            },
                            "waiting_message": (
                                "当前差旅报销无法自动受理，已创建财务复核任务；超标事项不会自动提交。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "reviewed",
                                    "label": "提交复核意见",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "财务已完成差旅报销复核：{comment}。"
                                        "本次复核不会自动生成报销单或超标特批单。"
                                    ),
                                },
                                {
                                    "value": "needs_information",
                                    "label": "要求补充材料",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "财务要求补充差旅材料：{comment}。"
                                        "补充后请重新发起或按财务指引办理。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                *[
                    {
                        "node_id": node_id,
                        "type": "response",
                        "name": name,
                        "instruction": instruction,
                        "allowed_actions": ["answer_user"],
                    }
                    for node_id, name, instruction in (
                        (
                            "travel_expense_accepted",
                            "差旅报销已受理",
                            "返回本次 EXP 单号和 accepted 状态，不得说已付款。",
                        ),
                        (
                            "travel_expense_pending",
                            "差旅报销待处理",
                            "返回本次 EXP 单号和 pending 状态，不得说已批准。",
                        ),
                        (
                            "travel_expense_rejected",
                            "差旅报销被拒绝",
                            "依据 rejected 回执说明未受理。",
                        ),
                        (
                            "travel_expense_failed",
                            "差旅报销提交失败",
                            "说明工具失败且没有形成可确认报销单。",
                        ),
                        (
                            "travel_policy_failed",
                            "差旅标准评估失败",
                            "说明评估工具失败，禁止使用历史结果继续。",
                        ),
                        (
                            "invoice_verification_failed",
                            "发票查验失败",
                            "说明查验工具失败，禁止继续提交。",
                        ),
                        (
                            "travel_submission_cancelled",
                            "差旅报销已取消",
                            "确认没有调用报销提交工具。",
                        ),
                        (
                            "travel_review_completed",
                            "财务复核已完成",
                            "反馈财务意见，并明确未自动生成报销或特批申请。",
                        ),
                        (
                            "travel_review_needs_information",
                            "等待补充差旅材料",
                            "反馈财务要求补充的材料。",
                        ),
                    )
                ],
            ],
            "edges": [
                {"source_node_id": "collect_travel_request", "next_node_id": "query_travel_policy"},
                {
                    "source_node_id": "query_travel_policy",
                    "next_node_id": "assess_travel_policy",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "node_output.travel_policy.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "node_output.travel_policy.data.outcome"},
                                "right": {"value": "evidence_found"},
                            },
                        ],
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "query_travel_policy",
                    "next_node_id": "finance_travel_review",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
                {
                    "source_node_id": "assess_travel_policy",
                    "next_node_id": "collect_travel_documents",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.travel_policy_assessment.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.travel_policy_assessment.data.status"
                                },
                                "right": {"value": "within_limit"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "slots.trip_approval_status"},
                                "right": {"value": "approved"},
                            },
                        ],
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "assess_travel_policy",
                    "next_node_id": "finance_travel_review",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.travel_policy_assessment.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 50,
                },
                {
                    "source_node_id": "assess_travel_policy",
                    "next_node_id": "travel_policy_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
                {
                    "source_node_id": "collect_travel_documents",
                    "next_node_id": "verify_travel_invoice",
                },
                {
                    "source_node_id": "verify_travel_invoice",
                    "next_node_id": "confirm_travel_submit",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.invoice_receipt.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.invoice_receipt.data.authentic"},
                                "right": {"value": True},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.invoice_receipt.data.fields_complete"
                                },
                                "right": {"value": True},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.invoice_receipt.data.amount_matches"
                                },
                                "right": {"value": True},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.invoice_receipt.data.risk_level"},
                                "right": {"value": "low"},
                            },
                        ],
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "verify_travel_invoice",
                    "next_node_id": "finance_travel_review",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.invoice_receipt.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 50,
                },
                {
                    "source_node_id": "verify_travel_invoice",
                    "next_node_id": "invoice_verification_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
                {
                    "source_node_id": "confirm_travel_submit",
                    "next_node_id": "submit_travel_expense",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "confirm_travel_submit",
                    "next_node_id": "travel_submission_cancelled",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
                *[
                    {
                        "source_node_id": "submit_travel_expense",
                        "next_node_id": target,
                        "condition": {
                            "op": "all",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"path": "tool_result.travel_submission.status"},
                                    "right": {"value": "succeeded"},
                                },
                                {
                                    "op": "eq",
                                    "left": {
                                        "path": "tool_result.travel_submission.data.status"
                                    },
                                    "right": {"value": status},
                                },
                            ],
                        },
                        "priority": priority,
                    }
                    for status, target, priority in (
                        ("accepted", "travel_expense_accepted", 100),
                        ("pending", "travel_expense_pending", 90),
                        ("rejected", "travel_expense_rejected", 80),
                    )
                ],
                {
                    "source_node_id": "submit_travel_expense",
                    "next_node_id": "travel_expense_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
                {
                    "source_node_id": "finance_travel_review",
                    "next_node_id": "travel_review_completed",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "reviewed"},
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "finance_travel_review",
                    "next_node_id": "travel_review_needs_information",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
            ],
            "start_node_id": "collect_travel_request",
            "terminal_node_ids": [
                "travel_expense_accepted",
                "travel_expense_pending",
                "travel_expense_rejected",
                "travel_expense_failed",
                "travel_policy_failed",
                "invoice_verification_failed",
                "travel_submission_cancelled",
                "travel_review_completed",
                "travel_review_needs_information",
            ],
            "interruption_policy": {},
            "response_rules": [
                "只有政策证据命中、标准内、事前批准且发票查验通过时才允许确认提交。",
                "住宿限额和超标金额只能来自本次结构化评估回执，不得由模型计算。",
                "未收到当前轮明确确认前禁止调用 expense.submit。",
                "超标、无事前批准或发票业务异常必须创建财务工作项且不得自动提交。",
                "本流程的人工复核不生成超标特批单；多级特批由独立后续 SOP 办理。",
                "EXP 单号和 accepted/pending/rejected 状态只能来自本次提交回执。",
            ],
        }
    )
    return content


def ensure_overtime_compensatory_deterministic_version(db: Session) -> None:
    """从历史加班调休卡派生政策、余额、确认、提交和 HR 接管的不可变版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == OVERTIME_COMPENSATORY_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION
    ):
        return
    _ensure_leave_application_tools(db)
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    if current_version is None:
        current_version = write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(skill.content_json),
        ).version
    content = _overtime_compensatory_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("加班调休确定性版本包含兼容告警，禁止自动发布")
    skill.version = OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id,
    )
    ensure_open_gallery_binding(
        db,
        skill.tenant_id,
        "skill",
        skill.id,
        "active",
        metadata_json={"source": "demo_seed", "system_seeded": True},
    )
    hr_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == "人事",
            AgentProfile.status == "active",
        )
    ).first()
    if hr_agent is not None:
        ensure_private_resource_binding(
            db,
            skill.tenant_id,
            hr_agent.id,
            "skill",
            skill.id,
            "active",
            metadata_json={"source": "demo_seed", "system_seeded": True},
        )
    _sync_seed_agent_branch(db, skill, agent_name="人事")
    db.flush()


def _ensure_leave_application_tools(db: Session) -> None:
    """升级共享 HR 工具契约，并保持余额查询和调休流程的既有白名单。"""

    base_url = get_settings().normalized_tool_base_url.rstrip("/")
    shared_skill_ids = {
        LEAVE_APPLICATION_SKILL_ID,
        LEAVE_BALANCE_SKILL_ID,
        OVERTIME_COMPENSATORY_SKILL_ID,
    }
    balance_tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "hr.balance_query",
        )
    ).first()
    leave_tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "hr.leave_apply",
        )
    ).first()
    if balance_tool is None or leave_tool is None:
        raise ValueError("请假申请所需 HR 工具不存在，禁止自动发布")

    balance_tool.url = f"{base_url}/api/mock/hr/balance_query"
    balance_tool.input_schema = {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "month": {"type": "string"},
            "include_attendance": {"type": "boolean"},
            "leave_type": {
                "type": "string",
                "enum": [
                    "annual",
                    "personal",
                    "sick",
                    "compensatory",
                    "marriage",
                    "maternity",
                    "other",
                ],
            },
            "start_date": {"type": "string", "format": "date"},
            "end_date": {"type": "string", "format": "date"},
            "overtime_date": {"type": "string", "format": "date"},
            "overtime_duration_hours": {"type": "number", "exclusiveMinimum": 0},
            "overtime_day_type": {
                "type": "string",
                "enum": ["workday", "rest_day", "statutory_holiday"],
            },
            "is_pre_approved": {"type": "boolean"},
            "pre_approval_status": {
                "type": "string",
                "enum": ["approved", "not_approved"],
            },
        },
        "required": ["employee_id"],
        "additionalProperties": False,
    }
    balance_tool.output_schema = {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "month": {"type": "string"},
            "leave_balance": {
                "type": "object",
                "properties": {
                    "annual": {"type": "number"},
                    "personal": {"type": "number"},
                    "sick": {"type": "number"},
                    "compensatory": {"type": "number"},
                },
            },
            "attendance": {"type": ["object", "null"]},
            "request_assessment": {
                "type": ["object", "null"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "sufficient",
                            "insufficient",
                            "manual_review",
                            "invalid_date",
                        ],
                    },
                    "leave_type": {"type": "string"},
                    "requested_days": {"type": ["number", "null"]},
                    "available_days": {"type": ["number", "null"]},
                },
            },
            "overtime_policy_assessment": {
                "type": ["object", "null"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "eligible",
                            "preapproval_missing",
                            "workday_minimum_not_met",
                            "statutory_holiday",
                            "invalid_date",
                            "manual_review",
                        ],
                    },
                    "conversion_ratio": {"type": "string", "enum": ["1:1"]},
                    "credit_unit": {"type": "string", "enum": ["hour"]},
                    "credited_hours": {"type": ["number", "null"]},
                },
            },
            "overtime_credit_assessment": {
                "type": ["object", "null"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "sufficient",
                            "insufficient",
                            "manual_review",
                            "invalid_date",
                        ],
                    },
                    "standard_hours_per_day": {"type": "number"},
                    "requested_days": {"type": ["number", "null"]},
                    "requested_hours": {"type": ["number", "null"]},
                    "credited_hours": {"type": ["number", "null"]},
                    "available_hours": {"type": ["number", "null"]},
                },
            },
            "message": {"type": "string"},
        },
    }
    balance_tool.allowed_skills_json = sorted(
        shared_skill_ids | set(balance_tool.allowed_skills_json or [])
    )
    balance_tool.updated_at = utc_now()
    db.add(balance_tool)

    leave_tool.url = f"{base_url}/api/mock/hr/leave_apply"
    leave_tool.allowed_skills_json = sorted(
        {
            LEAVE_APPLICATION_SKILL_ID,
            OVERTIME_COMPENSATORY_SKILL_ID,
        }
        | set(leave_tool.allowed_skills_json or [])
    )
    leave_tool.required_permission_code = "hr.leave.apply"
    leave_tool.permission_authorization_mode = "workflow_delegated"
    leave_tool.updated_at = utc_now()
    db.add(leave_tool)
    db.flush()


def ensure_meeting_room_deterministic_version(db: Session) -> None:
    """从现有会议室卡派生并幂等发布明确确认后才预订的确定性版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == MEETING_ROOM_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        MEETING_ROOM_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _meeting_room_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("会议室预订确定性版本包含兼容警告，禁止自动发布")
    skill.version = MEETING_ROOM_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="行政")
    db.flush()


def _meeting_room_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """保留会议室业务说明并替换为可信身份、明确确认和回执分支图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": MEETING_ROOM_SKILL_ID,
            "name": "会议室预订",
            "version": MEETING_ROOM_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": [
                "employee_id",
                "date",
                "start_time",
                "end_time",
                "attendees",
                "confirmation",
            ],
            "start_node_id": "node_collect_booking_details",
            "terminal_node_ids": [
                "node_booking_success",
                "node_booking_unavailable",
                "node_booking_failure",
                "node_booking_cancelled",
                "node_confirmation_invalid",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "date": {"type": "string"},
                        "start_time": {"type": "string"},
                        "end_time": {"type": "string"},
                        "attendees": {"type": "integer"},
                        "equipment": {"type": "array", "items": {"type": "string"}},
                        "room_preference": {"type": "string"},
                        "topic": {"type": "string"},
                        "confirmation": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "room_booking": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"status": {"type": "string"}},
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_booking_details",
                    "type": "collect_info",
                    "name": "收集会议室需求",
                    "instruction": (
                        "默认使用登录账号绑定的本人工号，收集绝对日期、开始时间、结束时间和"
                        "参会人数；人数用于匹配容量，因此作为业务必填项。可选收集设备、房间偏好和主题。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "date",
                        "start_time",
                        "end_time",
                        "attendees",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [],
                            }
                        }
                    },
                },
                {
                    "node_id": "node_confirm_booking",
                    "type": "collect_info",
                    "name": "明确确认预订",
                    "instruction": "展示日期、时间和人数，等待用户明确确认或取消。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认": "confirmed",
                                "确认预订": "confirmed",
                                "确认提交": "confirmed",
                                "是的，确认预订": "confirmed",
                                "yes": "confirmed",
                                "confirmed": "confirmed",
                                "取消": "cancelled",
                                "取消预订": "cancelled",
                                "不预订了": "cancelled",
                                "不要了": "cancelled",
                                "cancel": "cancelled",
                            },
                            "prompt": (
                                "会议室需求已收集，但尚未提交。请回复“确认预订”后系统才会"
                                "实际调用预订工具；如不再需要，请回复“取消预订”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_call_room_booking",
                    "type": "tool_call",
                    "name": "提交会议室预订",
                    "instruction": "只在当前轮收到明确确认后调用一次会议室预订工具。",
                    "allowed_actions": ["call_tool:admin.room_book"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "date": "slots.date",
                            "start_time": "slots.start_time",
                            "end_time": "slots.end_time",
                            "attendees": "slots.attendees",
                            "equipment": "slots.equipment",
                            "room_preference": "slots.room_preference",
                            "topic": "slots.topic",
                        },
                        "operation_result_key": "room_booking",
                    },
                },
                {
                    "node_id": "node_booking_success",
                    "type": "response",
                    "name": "反馈预订成功",
                    "instruction": (
                        "只依据工具回执反馈预订单号、会议室、位置、日期和时间段；"
                        "没有提醒工具，不得声称已设置会议提醒。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_booking_unavailable",
                    "type": "response",
                    "name": "反馈不可用和备选",
                    "instruction": "明确说明未完成预订，并只展示工具回执中的备选会议室。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_booking_failure",
                    "type": "response",
                    "name": "反馈预订失败",
                    "instruction": "说明预订工具失败并建议稍后重试，不得虚构预订单号。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_booking_cancelled",
                    "type": "response",
                    "name": "反馈用户取消",
                    "instruction": "明确说明本次预订已取消且没有调用预订工具。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_confirmation_invalid",
                    "type": "response",
                    "name": "阻断异常确认",
                    "instruction": "确认状态异常，未调用预订工具，请用户重新发起。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_booking_details",
                    "next_node_id": "node_confirm_booking",
                },
                {
                    "source_node_id": "node_confirm_booking",
                    "next_node_id": "node_call_room_booking",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "明确确认",
                },
                {
                    "source_node_id": "node_confirm_booking",
                    "next_node_id": "node_booking_cancelled",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "cancelled"},
                    },
                    "priority": 90,
                    "label": "取消预订",
                },
                {
                    "source_node_id": "node_confirm_booking",
                    "next_node_id": "node_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常确认默认阻断",
                },
                {
                    "source_node_id": "node_call_room_booking",
                    "next_node_id": "node_booking_success",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.room_booking.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.room_booking.data.status"},
                                "right": {"value": "booked"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "预订成功",
                },
                {
                    "source_node_id": "node_call_room_booking",
                    "next_node_id": "node_booking_unavailable",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.room_booking.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "in",
                                "left": {"path": "tool_result.room_booking.data.status"},
                                "right": {"value": ["waitlist", "unavailable"]},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "不可用或候补",
                },
                {
                    "source_node_id": "node_call_room_booking",
                    "next_node_id": "node_booking_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "失败默认路径",
                },
            ],
            "response_rules": [
                "未收到当前轮明确确认前禁止调用 admin.room_book。",
                "预订工具最多调用一次，最终结果只能来自本次结构化回执。",
                "不得声称已设置当前工具未提供的会议提醒。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "employee_id",
        "date",
        "start_time",
        "end_time",
        "attendees",
        "equipment",
        "room_preference",
        "topic",
        "confirmation",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_permission_grant_deterministic_version(db: Session) -> None:
    """从旧权限分流卡派生普通自动开通与高权限人工审批版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == PERMISSION_GRANT_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        PERMISSION_GRANT_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _permission_grant_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("权限开通确定性版本包含兼容告警，禁止自动发布")
    skill.version = PERMISSION_GRANT_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="IT")
    _protect_permission_grant_tool(db)
    db.flush()


def _protect_permission_grant_tool(db: Session) -> None:
    """把权限开通工具绑定到已发布 SOP 流程委托与数字员工执行权限。"""

    tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "it.grant_permission",
        )
    ).first()
    if tool is None:
        raise ValueError("权限开通 SOP 缺少 it.grant_permission 工具")
    tool.allowed_skills_json = [PERMISSION_GRANT_SKILL_ID]
    tool.required_permission_code = "it.access.grant"
    tool.permission_authorization_mode = "workflow_delegated"
    tool.updated_at = utc_now()
    db.add(tool)


def _permission_grant_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """构造可编译的可信身份、确认、受限分流、人工审批和工具回执图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": PERMISSION_GRANT_SKILL_ID,
            "name": "权限开通工单分流",
            "version": PERMISSION_GRANT_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": [
                "employee_id",
                "system",
                "permission",
                "access_level",
                "confirmation",
            ],
            "start_node_id": "node_collect_access_request",
            "terminal_node_ids": [
                "node_access_granted",
                "node_access_pending",
                "node_access_rejected",
                "node_tool_failure",
                "node_request_cancelled",
                "node_confirmation_invalid",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "system": {"type": "string"},
                        "permission": {"type": "string"},
                        "access_level": {"type": "string"},
                        "reason": {"type": "string"},
                        "duration": {"type": "string"},
                        "confirmation": {"type": "string"},
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "permission_grant": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"status": {"type": "string"}},
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_access_request",
                    "type": "collect_info",
                    "name": "收集权限申请",
                    "instruction": (
                        "默认使用登录账号绑定的本人工号。收集目标系统、权限名称"
                        "和访问级别；访问级别只能是 read、write 或 admin。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "system",
                        "permission",
                        "access_level",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [],
                            }
                        },
                        "value_aliases": {
                            "access_level": {
                                "只读": "read",
                                "查看": "read",
                                "read": "read",
                                "读写": "write",
                                "编辑": "write",
                                "write": "write",
                                "管理员": "admin",
                                "管理": "admin",
                                "admin": "admin",
                            }
                        },
                    },
                },
                {
                    "node_id": "node_confirm_access_request",
                    "type": "collect_info",
                    "name": "确认权限申请",
                    "instruction": "展示系统、权限和访问级别，等待申请人明确确认或取消。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认": "confirmed",
                                "确认申请": "confirmed",
                                "确认开通": "confirmed",
                                "确认提交": "confirmed",
                                "yes": "confirmed",
                                "confirmed": "confirmed",
                                "取消": "cancelled",
                                "取消申请": "cancelled",
                                "不申请了": "cancelled",
                                "cancel": "cancelled",
                            },
                            "prompt": (
                                "权限申请已收集，但尚未开通或提交审批。请核对系统、"
                                "权限和访问级别后回复“确认申请”；如不再需要请回复“取消申请”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_route_access_level",
                    "type": "decision",
                    "name": "按受限级别分流",
                    "instruction": "read 自动开通；write 和 admin 必须进入高权限人工审批。",
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "node_high_access_review",
                    "type": "human_task",
                    "name": "审批高权限申请",
                    "instruction": "由具备 IT 高权限审批角色的真实员工认领并提交意见。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [IT_ACCESS_APPROVER_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["approved", "rejected"],
                            "action_permissions": {
                                "outcome:approved": "it.access_request.approve",
                                "outcome:rejected": "it.access_request.reject",
                            },
                            "waiting_message": "高权限申请已提交，正在等待 IT 高权限审批人认领处理。",
                            "outcome_options": [
                                {
                                    "value": "approved",
                                    "label": "批准并继续开通",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "高权限申请已批准；权限系统处理状态：{business_status}；"
                                        "授权单号：{grant_id}；"
                                        "处理意见：{comment}。"
                                    ),
                                },
                                {
                                    "value": "rejected",
                                    "label": "拒绝申请",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": "高权限申请未通过，处理意见：{comment}。",
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "node_call_permission_grant",
                    "type": "tool_call",
                    "name": "执行权限开通",
                    "instruction": "只能在普通 read 分支或高权限审批通过分支调用一次工具。",
                    "allowed_actions": ["call_tool:it.grant_permission"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "system": "slots.system",
                            "permission": "slots.permission",
                            "access_level": "slots.access_level",
                            "reason": "slots.reason",
                            "duration": "slots.duration",
                        },
                        "operation_result_key": "permission_grant",
                    },
                },
                {
                    "node_id": "node_access_granted",
                    "type": "response",
                    "name": "反馈开通成功",
                    "instruction": "只依据工具回执反馈授权单号、系统、权限、状态和生效时间。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_access_pending",
                    "type": "response",
                    "name": "反馈外部待处理",
                    "instruction": "明确反馈 pending 和授权单号，不得声称已开通。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_access_rejected",
                    "type": "response",
                    "name": "反馈审批或外部拒绝",
                    "instruction": "明确说明申请未通过且未执行权限开通。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_tool_failure",
                    "type": "response",
                    "name": "反馈开通失败",
                    "instruction": "明确说明工具失败，不得虚构授权单号或承诺稍后自动完成。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_request_cancelled",
                    "type": "response",
                    "name": "反馈申请取消",
                    "instruction": "明确说明本次申请已取消，没有创建工作项也没有调用工具。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_confirmation_invalid",
                    "type": "response",
                    "name": "阻断异常确认",
                    "instruction": "确认状态异常，未提交审批或开通，请用户重新发起。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_access_request",
                    "next_node_id": "node_confirm_access_request",
                },
                {
                    "source_node_id": "node_confirm_access_request",
                    "next_node_id": "node_route_access_level",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "明确确认",
                },
                {
                    "source_node_id": "node_confirm_access_request",
                    "next_node_id": "node_request_cancelled",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "cancelled"},
                    },
                    "priority": 90,
                    "label": "取消",
                },
                {
                    "source_node_id": "node_confirm_access_request",
                    "next_node_id": "node_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常默认阻断",
                },
                {
                    "source_node_id": "node_route_access_level",
                    "next_node_id": "node_call_permission_grant",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.access_level"},
                        "right": {"value": "read"},
                    },
                    "priority": 100,
                    "label": "普通只读",
                },
                {
                    "source_node_id": "node_route_access_level",
                    "next_node_id": "node_high_access_review",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "高权限",
                },
                {
                    "source_node_id": "node_high_access_review",
                    "next_node_id": "node_call_permission_grant",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "approved"},
                    },
                    "priority": 100,
                    "label": "审批通过后受控开通",
                },
                {
                    "source_node_id": "node_high_access_review",
                    "next_node_id": "node_access_rejected",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "审批拒绝",
                },
                {
                    "source_node_id": "node_call_permission_grant",
                    "next_node_id": "node_access_granted",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.data.status"},
                                "right": {"value": "granted"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "已开通",
                },
                {
                    "source_node_id": "node_call_permission_grant",
                    "next_node_id": "node_access_pending",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.data.status"},
                                "right": {"value": "pending"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "外部待处理",
                },
                {
                    "source_node_id": "node_call_permission_grant",
                    "next_node_id": "node_access_rejected",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.permission_grant.data.status"},
                                "right": {"value": "rejected"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "外部拒绝",
                },
                {
                    "source_node_id": "node_call_permission_grant",
                    "next_node_id": "node_tool_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "失败默认路径",
                },
            ],
            "response_rules": [
                "未收到当前轮明确确认前，禁止创建工作项或调用 it.grant_permission。",
                "read 为自动开通；write/admin 审批通过后由 Runtime 恢复并受控调用同一开通工具。",
                "工具只能由 IT 数字员工以 workflow_delegated 受控执行，最终状态只来自结构化回执。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "employee_id",
        "system",
        "permission",
        "access_level",
        "reason",
        "duration",
        "confirmation",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_hr_certificate_deterministic_version(db: Session) -> None:
    """从旧证明卡派生可信本人、受限分类、人工复核和回执驱动的确定性版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == HR_CERTIFICATE_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        HR_CERTIFICATE_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _hr_certificate_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("在职证明确定性版本包含兼容告警，禁止自动发布")
    skill.version = HR_CERTIFICATE_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="人事")
    _protect_hr_certificate_tool(db)
    db.flush()


def _protect_hr_certificate_tool(db: Session) -> None:
    """把证明开具工具绑定到唯一 SOP，并要求人事数字员工以流程委托受控执行。"""

    tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "hr.cert_issue",
        )
    ).first()
    if tool is None:
        raise ValueError("在职证明 SOP 缺少 hr.cert_issue 工具")
    tool.allowed_skills_json = [HR_CERTIFICATE_SKILL_ID]
    tool.required_permission_code = "hr.certificate.issue"
    tool.permission_authorization_mode = "workflow_delegated"
    tool.updated_at = utc_now()
    db.add(tool)


def _hr_certificate_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """构造常规自动开具、特殊人工复核和证明系统业务回执的统一确定性图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": HR_CERTIFICATE_SKILL_ID,
            "name": "在职证明开具",
            "version": HR_CERTIFICATE_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": [
                "employee_id",
                "employee_name",
                "cert_type",
                "purpose",
                "purpose_category",
                "language",
                "confirmation",
            ],
            "start_node_id": "node_collect_certificate_request",
            "terminal_node_ids": [
                "node_certificate_issued",
                "node_certificate_pending",
                "node_certificate_rejected",
                "node_certificate_failure",
                "node_certificate_cancelled",
                "node_confirmation_invalid",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "cert_type": {"type": "string"},
                        "purpose": {"type": "string"},
                        "purpose_category": {"type": "string"},
                        "language": {"type": "string"},
                        "include_income": {"type": "boolean"},
                        "confirmation": {"type": "string"},
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "certificate_issue": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "cert_id": {"type": "string"},
                                        "download_url": {"type": "string"},
                                    },
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_certificate_request",
                    "type": "collect_info",
                    "name": "收集证明申请",
                    "instruction": (
                        "从登录员工档案取得本人工号和姓名。收集证明类型、具体用途、用途分类和语言；"
                        "证明类型只能是 employment、income、employment_income，用途分类只能是 "
                        "routine、visa、loan、other_sensitive，语言只能是 zh 或 en。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "employee_name",
                        "cert_type",
                        "purpose",
                        "purpose_category",
                        "language",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [],
                            },
                            "employee_name": {
                                "source": "authenticated_employee",
                                "attribute": "employee_name",
                                "allow_override_roles": [],
                            },
                        },
                        "value_aliases": {
                            "cert_type": {
                                "在职证明": "employment",
                                "employment": "employment",
                                "收入证明": "income",
                                "income": "income",
                                "在职收入证明": "employment_income",
                                "在职及收入证明": "employment_income",
                                "employment_income": "employment_income",
                            },
                            "purpose_category": {
                                "普通业务": "routine",
                                "常规用途": "routine",
                                "租房": "routine",
                                "routine": "routine",
                                "签证": "visa",
                                "visa": "visa",
                                "贷款": "loan",
                                "loan": "loan",
                                "其他敏感用途": "other_sensitive",
                                "other_sensitive": "other_sensitive",
                            },
                            "language": {
                                "中文": "zh",
                                "zh": "zh",
                                "英文": "en",
                                "英语": "en",
                                "en": "en",
                            },
                        },
                    },
                },
                {
                    "node_id": "node_confirm_certificate_request",
                    "type": "collect_info",
                    "name": "确认证明申请",
                    "instruction": "展示本人身份、证明类型、用途和语言，等待申请人明确确认或取消。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认": "confirmed",
                                "确认开具": "confirmed",
                                "确认申请": "confirmed",
                                "确认提交": "confirmed",
                                "yes": "confirmed",
                                "confirmed": "confirmed",
                                "取消": "cancelled",
                                "取消申请": "cancelled",
                                "不需要了": "cancelled",
                                "cancel": "cancelled",
                            },
                            "prompt": (
                                "证明信息已收集，但尚未开具或提交复核。请核对证明类型、用途和语言后"
                                "回复“确认开具”；如不再需要请回复“取消申请”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_route_certificate_policy",
                    "type": "decision",
                    "name": "按证明政策分流",
                    "instruction": (
                        "普通在职证明直接开具；收入类、签证、贷款和其他敏感用途必须人工复核。"
                    ),
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "node_special_certificate_review",
                    "type": "human_task",
                    "name": "复核特殊证明申请",
                    "instruction": "由具备 HR 证明复核角色的真实员工认领并提交结构化决定。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [HR_CERTIFICATE_REVIEWER_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["approved", "rejected"],
                            "action_permissions": {
                                "outcome:approved": "hr.certificate_request.approve",
                                "outcome:rejected": "hr.certificate_request.reject",
                            },
                            "waiting_message": (
                                "特殊用途或收入证明申请已提交，正在等待 HR 证明复核专员认领处理。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "approved",
                                    "label": "批准并继续开具",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "证明复核已批准；证明系统处理状态：{business_status}；"
                                        "证明编号：{cert_id}；处理意见：{comment}。"
                                    ),
                                },
                                {
                                    "value": "rejected",
                                    "label": "拒绝申请",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "证明申请未通过复核，未执行开具。处理意见：{comment}。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "node_call_certificate_issue",
                    "type": "tool_call",
                    "name": "执行证明开具",
                    "instruction": "只在普通路径或特殊证明复核批准后调用一次证明开具工具。",
                    "allowed_actions": ["call_tool:hr.cert_issue"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "cert_type": "slots.cert_type",
                            "purpose": "slots.purpose",
                            "language": "slots.language",
                            "include_income": "slots.include_income",
                        },
                        "operation_result_key": "certificate_issue",
                    },
                },
                {
                    "node_id": "node_certificate_issued",
                    "type": "response",
                    "name": "反馈证明已开具",
                    "instruction": (
                        "只依据 issued 回执反馈证明编号、类型和演示下载地址，并说明不是生产证明。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_certificate_pending",
                    "type": "response",
                    "name": "反馈证明待处理",
                    "instruction": "明确反馈 pending 和证明编号，不得描述成已经开具。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_certificate_rejected",
                    "type": "response",
                    "name": "反馈证明申请拒绝",
                    "instruction": "明确说明申请未通过且未生成可用证明。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_certificate_failure",
                    "type": "response",
                    "name": "反馈证明开具失败",
                    "instruction": "明确说明工具失败，不得虚构 CERT 编号或下载地址。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_certificate_cancelled",
                    "type": "response",
                    "name": "反馈证明申请取消",
                    "instruction": "说明本次申请已取消，没有工作项或证明工具调用。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_confirmation_invalid",
                    "type": "response",
                    "name": "阻断异常确认",
                    "instruction": "确认状态异常，未提交复核或开具，请用户重新发起。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_certificate_request",
                    "next_node_id": "node_confirm_certificate_request",
                },
                {
                    "source_node_id": "node_confirm_certificate_request",
                    "next_node_id": "node_route_certificate_policy",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "明确确认",
                },
                {
                    "source_node_id": "node_confirm_certificate_request",
                    "next_node_id": "node_certificate_cancelled",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "cancelled"},
                    },
                    "priority": 90,
                    "label": "取消",
                },
                {
                    "source_node_id": "node_confirm_certificate_request",
                    "next_node_id": "node_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常默认阻断",
                },
                {
                    "source_node_id": "node_route_certificate_policy",
                    "next_node_id": "node_special_certificate_review",
                    "condition": {
                        "op": "any",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "slots.cert_type"},
                                "right": {"value": "income"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "slots.cert_type"},
                                "right": {"value": "employment_income"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "slots.purpose_category"},
                                "right": {"value": "visa"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "slots.purpose_category"},
                                "right": {"value": "loan"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "slots.purpose_category"},
                                "right": {"value": "other_sensitive"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "特殊证明人工复核",
                },
                {
                    "source_node_id": "node_route_certificate_policy",
                    "next_node_id": "node_call_certificate_issue",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "普通在职证明",
                },
                {
                    "source_node_id": "node_special_certificate_review",
                    "next_node_id": "node_call_certificate_issue",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "approved"},
                    },
                    "priority": 100,
                    "label": "复核批准后开具",
                },
                {
                    "source_node_id": "node_special_certificate_review",
                    "next_node_id": "node_certificate_rejected",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "复核拒绝",
                },
                {
                    "source_node_id": "node_call_certificate_issue",
                    "next_node_id": "node_certificate_issued",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.data.status"},
                                "right": {"value": "issued"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "已开具",
                },
                {
                    "source_node_id": "node_call_certificate_issue",
                    "next_node_id": "node_certificate_pending",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.data.status"},
                                "right": {"value": "pending"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "外部待处理",
                },
                {
                    "source_node_id": "node_call_certificate_issue",
                    "next_node_id": "node_certificate_rejected",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.certificate_issue.data.status"},
                                "right": {"value": "rejected"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "外部拒绝",
                },
                {
                    "source_node_id": "node_call_certificate_issue",
                    "next_node_id": "node_certificate_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "失败默认路径",
                },
            ],
            "response_rules": [
                "未收到当前轮明确确认前，禁止创建工作项或调用 hr.cert_issue。",
                "普通在职证明可自动开具；收入类和敏感用途必须由 HR 证明复核专员批准。",
                "审批通过不等于证明已开具，只有 hr.cert_issue 返回 issued 才能反馈 CERT 编号和下载地址。",
                "所有下载地址仅用于演示，不得描述为生产人事证明。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "employee_id",
        "employee_name",
        "cert_type",
        "purpose",
        "purpose_category",
        "language",
        "include_income",
        "confirmation",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_clause_modification_deterministic_version(db: Session) -> None:
    """从旧条款卡派生受限输入、相关资料检索和证据边界明确的确定性版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == CLAUSE_MODIFICATION_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        CLAUSE_MODIFICATION_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _clause_modification_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("条款修改建议确定性版本包含兼容告警，禁止自动发布")
    skill.version = CLAUSE_MODIFICATION_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="法务")
    _protect_contract_reference_tool(db)
    db.flush()


def _protect_contract_reference_tool(db: Session) -> None:
    """把共享合同资料工具限制在三个法务 SOP，并要求法务数字员工受控检索。"""

    tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "contract.archive_query",
        )
    ).first()
    if tool is None:
        raise ValueError("条款修改建议 SOP 缺少 contract.archive_query 工具")
    tool.display_name = "合同参考资料检索"
    tool.description = "按关键词检索演示合同、条款和复盘资料，不提供正式法律结论。"
    tool.allowed_skills_json = list(LEGAL_CONTRACT_REFERENCE_SKILL_IDS)
    tool.required_permission_code = "legal.contract_reference.query"
    tool.permission_authorization_mode = "workflow_delegated"
    tool.updated_at = utc_now()
    db.add(tool)


def _clause_modification_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """构造条款输入、相关资料检索和有依据/无匹配/失败三终态的确定性图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": CLAUSE_MODIFICATION_SKILL_ID,
            "name": "条款修改建议",
            "version": CLAUSE_MODIFICATION_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": [
                "contract_type",
                "clause_content",
                "modification_request",
            ],
            "start_node_id": "node_collect_clause_request",
            "terminal_node_ids": [
                "node_clause_suggestion_with_reference",
                "node_clause_suggestion_without_reference",
                "node_clause_suggestion_failure",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "contract_type": {"type": "string"},
                        "clause_content": {"type": "string"},
                        "modification_request": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "contract_reference": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "total": {"type": "integer"},
                                    },
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_clause_request",
                    "type": "collect_info",
                    "name": "收集条款修改需求",
                    "instruction": (
                        "收集合同类型、待修改条款完整原文和修改目标。合同类型只能归一为 "
                        "software_procurement、service、sales 或 other；不得把用户未提供的交易背景"
                        "、管辖法、金额或谈判立场当成事实。"
                    ),
                    "expected_user_info": [
                        "contract_type",
                        "clause_content",
                        "modification_request",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "value_aliases": {
                            "contract_type": {
                                "软件采购合同": "software_procurement",
                                "软件采购": "software_procurement",
                                "software_procurement": "software_procurement",
                                "服务合同": "service",
                                "服务协议": "service",
                                "service": "service",
                                "销售合同": "sales",
                                "买卖合同": "sales",
                                "sales": "sales",
                                "其他合同": "other",
                                "other": "other",
                            }
                        }
                    },
                },
                {
                    "node_id": "node_query_clause_reference",
                    "type": "tool_call",
                    "name": "检索合同参考资料",
                    "instruction": (
                        "只以用户提供的条款原文检索演示合同资料，不得把检索结果描述为现行法律结论。"
                    ),
                    "allowed_actions": ["call_tool:contract.archive_query"],
                    "metadata": {
                        "operation_input": {"query": "slots.clause_content"},
                        "operation_result_key": "contract_reference",
                    },
                },
                {
                    "node_id": "node_clause_suggestion_with_reference",
                    "type": "response",
                    "name": "输出有参考依据的修改建议",
                    "instruction": (
                        "按固定结构输出：一、原条款；二、建议条款；三、修改理由；四、参考资料；"
                        "五、风险与复核提示。建议条款必须回应 modification_request；参考资料只能引用本次"
                        " contract_reference 回执实际返回的 title 与 citation，不得编造法律条文、案号、"
                        "金额、责任上限或对方立场。明确说明资料和建议仅用于演示与谈判起草，正式签署前"
                        "需由法务结合完整合同、适用法律和交易背景复核。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_clause_suggestion_without_reference",
                    "type": "response",
                    "name": "反馈无匹配参考资料",
                    "instruction": (
                        "明确说明演示资料库没有匹配记录；可以整理用户修改目标和待补信息，但不得使用"
                        "所谓内置法律知识生成确定性条款或编造引用。提示补充合同全文、适用法律、交易金额"
                        "和责任分配后交由正式法务复核。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_clause_suggestion_failure",
                    "type": "response",
                    "name": "反馈参考资料检索失败",
                    "instruction": (
                        "明确说明参考资料检索失败，本次未形成有依据的修改条款；不得复用历史检索结果或"
                        "虚构引用，建议稍后重新检索或交由正式法务处理。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_clause_request",
                    "next_node_id": "node_query_clause_reference",
                },
                {
                    "source_node_id": "node_query_clause_reference",
                    "next_node_id": "node_clause_suggestion_with_reference",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_reference.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "gt",
                                "left": {"path": "tool_result.contract_reference.data.total"},
                                "right": {"value": 0},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "找到相关演示资料",
                },
                {
                    "source_node_id": "node_query_clause_reference",
                    "next_node_id": "node_clause_suggestion_without_reference",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_reference.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_reference.data.total"},
                                "right": {"value": 0},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "没有匹配资料",
                },
                {
                    "source_node_id": "node_query_clause_reference",
                    "next_node_id": "node_clause_suggestion_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "检索失败默认路径",
                },
            ],
            "response_rules": [
                "本技能只输出起草建议，不修改合同系统，不产生审批或签署副作用，因此无需确认节点。",
                "不得把演示合同资料、示例案号或模型知识描述为现行法律、公司制度或正式法务意见。",
                "检索有结果时只引用本次结构化回执；无结果或失败时必须明确能力边界，不得伪造依据。",
                "正式签署前必须由法务结合完整合同、交易背景和适用法律复核。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "contract_type",
        "clause_content",
        "modification_request",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_contract_risk_review_deterministic_version(db: Session) -> None:
    """发布结构化初筛、低风险报告和高风险真人复核的确定性合同审查版本。"""

    _ensure_contract_risk_assessment_tool(db)
    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == CONTRACT_RISK_REVIEW_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _contract_risk_review_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("合同风险审查确定性版本包含兼容告警，禁止自动发布")
    skill.version = CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="法务")
    db.flush()


def _ensure_contract_risk_assessment_tool(db: Session) -> None:
    """幂等创建合同风险初筛工具，并绑定法务数字员工和唯一风险审查 SOP。"""

    base_url = get_settings().normalized_tool_base_url.rstrip("/")
    input_schema = {
        "type": "object",
        "properties": {
            "contract_type": {
                "type": "string",
                "enum": ["software_procurement", "service", "sales", "other"],
            },
            "contract_content": {"type": "string", "minLength": 20},
            "review_scope": {
                "type": "string",
                "enum": ["key_clauses", "full_text"],
                "default": "key_clauses",
            },
        },
        "required": ["contract_type", "contract_content"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "assessment_id": {"type": "string"},
            "status": {"type": "string", "enum": ["assessed", "insufficient"]},
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high", "unknown"],
            },
            "risk_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "title": {"type": "string"},
                        "severity": {"type": "string"},
                        "evidence": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            },
            "reference_codes": {"type": "array", "items": {"type": "string"}},
            "requires_human_review": {"type": "boolean"},
            "message": {"type": "string"},
            "assessed_at": {"type": "string"},
        },
    }
    tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_demo",
            Tool.name == "contract.risk_assess",
        )
    ).first()
    payload = {
        "display_name": "合同风险初筛",
        "description": "按显式演示规则返回结构化合同风险信号，不提供正式法律结论。",
        "bucket": "法务合规",
        "tool_type": "http",
        "method": "POST",
        "url": f"{base_url}/api/mock/contract/risk_assess",
        "headers_json": {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
        "auth_json": {},
        "config_json": {},
        "input_schema": input_schema,
        "output_schema": output_schema,
        "allowed_skills_json": [CONTRACT_RISK_REVIEW_SKILL_ID],
        "required_permission_code": "legal.contract_risk.assess",
        "permission_authorization_mode": "workflow_delegated",
        "enabled": True,
    }
    if tool is None:
        tool = Tool(tenant_id="tenant_demo", name="contract.risk_assess", **payload)
    else:
        for field_name, field_value in payload.items():
            setattr(tool, field_name, field_value)
        tool.updated_at = utc_now()
    db.add(tool)
    db.flush()
    ensure_open_gallery_binding(
        db,
        "tenant_demo",
        "tool",
        tool.id,
        metadata_json={"source": CONTRACT_RISK_REVIEW_SKILL_ID, "system_seeded": True},
    )
    legal_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "法务",
            AgentProfile.status == "active",
        )
    ).first()
    if legal_agent is not None:
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            legal_agent.id,
            "tool",
            tool.id,
            metadata_json={"source": CONTRACT_RISK_REVIEW_SKILL_ID},
        )


def _contract_risk_review_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """构造合同风险初筛、低风险报告和高风险法务人工复核的统一确定性图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": CONTRACT_RISK_REVIEW_SKILL_ID,
            "name": "合同条款风险审查",
            "version": CONTRACT_RISK_REVIEW_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": ["contract_type", "contract_content"],
            "start_node_id": "node_collect_contract_review",
            "terminal_node_ids": [
                "node_contract_risk_report",
                "node_high_risk_review_completed",
                "node_review_information_required",
                "node_contract_assessment_insufficient",
                "node_contract_assessment_failure",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "contract_type": {"type": "string"},
                        "contract_content": {"type": "string"},
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "contract_risk": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "risk_level": {"type": "string"},
                                    },
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_contract_review",
                    "type": "collect_info",
                    "name": "收集合同审查材料",
                    "instruction": (
                        "收集合同类型和需要审查的合同全文或关键条款原文。用户提供关键条款时必须将"
                        "原文完整写入 contract_content，不得另建 contract_clause，也不得强制索要合同全文。"
                        "合同类型只能归一为 "
                        "software_procurement、service、sales 或 other；不得假定未提供的完整合同、"
                        "适用法律、金额或交易背景。"
                    ),
                    "expected_user_info": ["contract_type", "contract_content"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "value_aliases": {
                            "contract_type": {
                                "软件采购合同": "software_procurement",
                                "软件采购": "software_procurement",
                                "software_procurement": "software_procurement",
                                "服务合同": "service",
                                "服务协议": "service",
                                "service": "service",
                                "销售合同": "sales",
                                "买卖合同": "sales",
                                "sales": "sales",
                                "其他合同": "other",
                                "other": "other",
                            }
                        }
                    },
                },
                {
                    "node_id": "node_assess_contract_risk",
                    "type": "tool_call",
                    "name": "执行合同风险初筛",
                    "instruction": "只按用户提供的合同类型和文本执行演示初筛，不生成正式法律结论。",
                    "allowed_actions": ["call_tool:contract.risk_assess"],
                    "metadata": {
                        "operation_input": {
                            "contract_type": "slots.contract_type",
                            "contract_content": "slots.contract_content",
                        },
                        "operation_result_key": "contract_risk",
                    },
                },
                {
                    "node_id": "node_high_risk_legal_review",
                    "type": "human_task",
                    "name": "法务人工复核高风险合同",
                    "instruction": "由真实法务复核人认领，基于初筛回执和合同原文提交专业意见。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [LEGAL_CONTRACT_REVIEWER_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["reviewed", "needs_information"],
                            "action_permissions": {
                                "claim": "legal.contract_review.claim",
                                "outcome:reviewed": "legal.contract_review.complete",
                                "outcome:needs_information": (
                                    "legal.contract_review.request_information"
                                ),
                            },
                            "waiting_message": (
                                "演示初筛命中高风险信号，已创建法务人工复核任务，"
                                "等待具备合同复核角色的员工认领。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "reviewed",
                                    "label": "提交复核意见",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "法务人工复核已完成。初筛编号：{assessment_id}；"
                                        "初筛风险等级：{risk_level}；复核意见：{comment}。"
                                        "该意见仅用于本次演示，正式签署前仍需结合完整合同复核。"
                                    ),
                                },
                                {
                                    "value": "needs_information",
                                    "label": "要求补充材料",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "法务复核暂无法完成。初筛编号：{assessment_id}；"
                                        "需要补充：{comment}。当前未形成正式复核结论。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "node_contract_risk_report",
                    "type": "response",
                    "name": "输出合同风险初筛报告",
                    "instruction": (
                        "按固定结构输出初筛编号、风险等级、逐项风险信号、文本证据、修改建议、"
                        "演示参考编号和能力边界。只使用 contract_risk 本次回执，不得编造法律条文、"
                        "判例或公司制度；明确未命中高风险不等于正式法律审查通过。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_high_risk_review_completed",
                    "type": "response",
                    "name": "反馈法务人工复核完成",
                    "instruction": "只反馈结构化人工复核意见和演示边界，不得声称合同已批准或已签署。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_review_information_required",
                    "type": "response",
                    "name": "反馈需要补充合同材料",
                    "instruction": "反馈复核人声明的缺失材料，明确当前没有形成复核结论。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_contract_assessment_insufficient",
                    "type": "response",
                    "name": "反馈合同材料不足",
                    "instruction": "说明结构化初筛返回 insufficient，并要求补充完整条款，不得猜测风险等级。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_contract_assessment_failure",
                    "type": "response",
                    "name": "反馈合同初筛失败",
                    "instruction": "说明初筛工具失败，不得复用历史风险报告或虚构法务结论。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_contract_review",
                    "next_node_id": "node_assess_contract_risk",
                },
                {
                    "source_node_id": "node_assess_contract_risk",
                    "next_node_id": "node_high_risk_legal_review",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.data.status"},
                                "right": {"value": "assessed"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.data.risk_level"},
                                "right": {"value": "high"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "高风险进入法务人工复核",
                },
                {
                    "source_node_id": "node_assess_contract_risk",
                    "next_node_id": "node_contract_risk_report",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.data.status"},
                                "right": {"value": "assessed"},
                            },
                            {
                                "op": "any",
                                "args": [
                                    {
                                        "op": "eq",
                                        "left": {
                                            "path": "tool_result.contract_risk.data.risk_level"
                                        },
                                        "right": {"value": "low"},
                                    },
                                    {
                                        "op": "eq",
                                        "left": {
                                            "path": "tool_result.contract_risk.data.risk_level"
                                        },
                                        "right": {"value": "medium"},
                                    },
                                ],
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "低中风险输出初筛报告",
                },
                {
                    "source_node_id": "node_assess_contract_risk",
                    "next_node_id": "node_contract_assessment_insufficient",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.contract_risk.data.status"},
                                "right": {"value": "insufficient"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "材料不足",
                },
                {
                    "source_node_id": "node_assess_contract_risk",
                    "next_node_id": "node_contract_assessment_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "初筛失败默认路径",
                },
                {
                    "source_node_id": "node_high_risk_legal_review",
                    "next_node_id": "node_high_risk_review_completed",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "reviewed"},
                    },
                    "priority": 100,
                    "label": "人工复核完成",
                },
                {
                    "source_node_id": "node_high_risk_legal_review",
                    "next_node_id": "node_review_information_required",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "要求补充材料",
                },
            ],
            "response_rules": [
                "支持合同全文或关键条款审查；关键条款原文统一保存为 contract_content，不重复索要全文。",
                "风险初筛只使用 contract.risk_assess 结构化回执，不执行当前尚未放行的知识节点。",
                "低风险只表示演示规则未命中高风险信号，不等于正式审查通过。",
                "高风险必须创建 legal_contract_reviewer 工作项；申请人、平台管理员和法务数字员工不能代替真人复核。",
                "人工结果是 reviewed 或 needs_information，不使用 approve/reject 冒充专业审查。",
                "任何路径都不得声称合同已批准、已修改、已签署或符合全部适用法律。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = ["contract_type", "contract_content"]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_office_supply_deterministic_version(db: Session) -> None:
    """从现有用品申领卡派生并幂等发布确认后登记的确定性版本。"""

    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == OFFICE_SUPPLY_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        OFFICE_SUPPLY_DETERMINISTIC_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _office_supply_deterministic_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("办公用品申领确定性版本包含兼容警告，禁止自动发布")
    skill.version = OFFICE_SUPPLY_DETERMINISTIC_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="行政")
    db.flush()


def _office_supply_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """保留用品申领说明并替换为明确确认和业务回执驱动的确定性图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": OFFICE_SUPPLY_SKILL_ID,
            "name": "办公用品申领",
            "version": OFFICE_SUPPLY_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": ["employee_id", "items", "confirmation"],
            "start_node_id": "node_collect_supply_request",
            "terminal_node_ids": [
                "node_supply_approved",
                "node_supply_partial",
                "node_supply_pending",
                "node_supply_rejected",
                "node_supply_failure",
                "node_supply_cancelled",
                "node_supply_confirmation_invalid",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "department": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                    "unit": {"type": "string"},
                                },
                            },
                        },
                        "reason": {"type": "string"},
                        "needed_by": {"type": "string"},
                        "confirmation": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "supply_request": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"status": {"type": "string"}},
                                },
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_supply_request",
                    "type": "collect_info",
                    "name": "收集用品申领信息",
                    "instruction": (
                        "默认使用登录账号绑定的本人工号。收集物品名称和正整数数量，支持一次"
                        "提取多项；单位、事由和期望领取日期可选。不得由模型判断是否贵重或已获批准。"
                    ),
                    "expected_user_info": ["employee_id", "items"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [],
                            }
                        }
                    },
                },
                {
                    "node_id": "node_confirm_supply_request",
                    "type": "collect_info",
                    "name": "明确确认用品申领",
                    "instruction": "展示物品名称和数量，等待用户明确确认或取消。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认": "confirmed",
                                "确认申领": "confirmed",
                                "确认提交": "confirmed",
                                "是的，确认申领": "confirmed",
                                "yes": "confirmed",
                                "confirmed": "confirmed",
                                "取消": "cancelled",
                                "取消申领": "cancelled",
                                "不申请了": "cancelled",
                                "不要了": "cancelled",
                                "cancel": "cancelled",
                            },
                            "prompt": (
                                "办公用品清单已收集，但尚未提交。请核对名称和数量后回复"
                                "“确认申领”；如不再需要，请回复“取消申领”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_call_supply_request",
                    "type": "tool_call",
                    "name": "提交用品申领",
                    "instruction": "只在当前轮收到明确确认后调用一次用品申领工具。",
                    "allowed_actions": ["call_tool:admin.supply_request"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "department": "slots.department",
                            "items": "slots.items",
                            "reason": "slots.reason",
                            "needed_by": "slots.needed_by",
                        },
                        "operation_result_key": "supply_request",
                    },
                },
                {
                    "node_id": "node_supply_approved",
                    "type": "response",
                    "name": "反馈用品批准",
                    "instruction": (
                        "只依据本次工具回执反馈申领单号、各物品申请/批准数量、领取地点和提交时间。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_partial",
                    "type": "response",
                    "name": "反馈部分批准",
                    "instruction": "明确逐项反馈申请数量、批准数量和备注，不得把部分批准说成全部通过。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_pending",
                    "type": "response",
                    "name": "反馈待审批",
                    "instruction": (
                        "反馈申领单号和 pending 状态，只能说明外部用品系统已受理待审批；"
                        "当前没有审批结果，不得声称已批准或已分配审批人。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_rejected",
                    "type": "response",
                    "name": "反馈用品拒绝",
                    "instruction": "只依据工具回执说明拒绝状态和原因，不得建议已获批准。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_failure",
                    "type": "response",
                    "name": "反馈提交失败",
                    "instruction": "说明用品申领工具失败并建议稍后重试，不得虚构 SUP 申领单号。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_cancelled",
                    "type": "response",
                    "name": "反馈用户取消",
                    "instruction": "明确说明本次用品申领已取消且没有调用登记工具。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_supply_confirmation_invalid",
                    "type": "response",
                    "name": "阻断异常确认",
                    "instruction": "确认状态异常，未调用用品申领工具，请用户重新发起。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_supply_request",
                    "next_node_id": "node_confirm_supply_request",
                },
                {
                    "source_node_id": "node_confirm_supply_request",
                    "next_node_id": "node_call_supply_request",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "明确确认",
                },
                {
                    "source_node_id": "node_confirm_supply_request",
                    "next_node_id": "node_supply_cancelled",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "cancelled"},
                    },
                    "priority": 90,
                    "label": "取消申领",
                },
                {
                    "source_node_id": "node_confirm_supply_request",
                    "next_node_id": "node_supply_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常确认默认阻断",
                },
                {
                    "source_node_id": "node_call_supply_request",
                    "next_node_id": "node_supply_approved",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.data.status"},
                                "right": {"value": "approved"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "全部批准",
                },
                {
                    "source_node_id": "node_call_supply_request",
                    "next_node_id": "node_supply_partial",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.data.status"},
                                "right": {"value": "partial"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "部分批准",
                },
                {
                    "source_node_id": "node_call_supply_request",
                    "next_node_id": "node_supply_pending",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.data.status"},
                                "right": {"value": "pending"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "外部系统待审批",
                },
                {
                    "source_node_id": "node_call_supply_request",
                    "next_node_id": "node_supply_rejected",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.supply_request.data.status"},
                                "right": {"value": "rejected"},
                            },
                        ],
                    },
                    "priority": 70,
                    "label": "外部系统拒绝",
                },
                {
                    "source_node_id": "node_call_supply_request",
                    "next_node_id": "node_supply_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "失败默认路径",
                },
            ],
            "response_rules": [
                "未收到当前轮明确确认前禁止调用 admin.supply_request。",
                "工具最多调用一次，业务结果只能来自本次 status 和 approved_items 回执。",
                "不得由模型判断贵重/超额或虚构主管审批；pending 只表示外部系统已受理待审批。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "employee_id",
        "employee_name",
        "department",
        "items",
        "reason",
        "needed_by",
        "confirmation",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def ensure_fault_report_lifecycle_version(db: Session) -> None:
    """幂等准备工单工具并发布工程师处理、报修人验收和关闭的完整版本。"""

    _ensure_fault_report_tools(db)
    skill = db.exec(
        select(Skill).where(
            Skill.tenant_id == "tenant_demo",
            Skill.skill_id == FAULT_REPORT_SKILL_ID,
        )
    ).first()
    if skill is None or _version_tuple(skill.version) >= _version_tuple(
        FAULT_REPORT_LIFECYCLE_VERSION
    ):
        return
    current_version = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
        )
    ).first()
    content = _fault_report_lifecycle_content(skill.content_json)
    definition = compile_legacy_skill_card(content)
    if definition.diagnostics:
        raise ValueError("故障报修生命周期版本包含兼容警告，禁止自动发布")
    skill.version = FAULT_REPORT_LIFECYCLE_VERSION
    skill.content_json = content
    skill.status = "published"
    db.add(skill)
    write_skill_version(
        db,
        skill,
        compiled_definition=definition,
        derived_from_version_id=current_version.id if current_version else None,
    )
    _sync_seed_agent_branch(db, skill, agent_name="IT")
    db.flush()


def _ensure_fault_report_tools(db: Session) -> None:
    """幂等创建关闭和重开工具，并绑定到 IT 数字员工和故障 SOP。"""

    base_url = get_settings().normalized_tool_base_url.rstrip("/")
    tool_definitions = (
        (
            "it.ticket_close",
            "IT工单关闭",
            "/api/mock/it/ticket_close",
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "requester_employee_id": {"type": "string"},
                },
                "required": ["ticket_id", "requester_employee_id"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["closed"]},
                    "closed_by_employee_id": {"type": "string"},
                    "message": {"type": "string"},
                    "closed_at": {"type": "string"},
                },
            },
        ),
        (
            "it.ticket_reopen",
            "IT工单重开",
            "/api/mock/it/ticket_reopen",
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "requester_employee_id": {"type": "string"},
                },
                "required": ["ticket_id", "requester_employee_id"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["reopened"]},
                    "queue": {"type": "string"},
                    "message": {"type": "string"},
                    "reopened_at": {"type": "string"},
                },
            },
        ),
    )
    information_technology_agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == "tenant_demo",
            AgentProfile.name == "IT",
            AgentProfile.status == "active",
        )
    ).first()
    for name, display_name, path, input_schema, output_schema in tool_definitions:
        tool = db.exec(
            select(Tool).where(Tool.tenant_id == "tenant_demo", Tool.name == name)
        ).first()
        payload = {
            "display_name": display_name,
            "description": f"{display_name}的受控演示接口。",
            "bucket": "IT 服务",
            "tool_type": "http",
            "method": "POST",
            "url": f"{base_url}{path}",
            "headers_json": {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
            "auth_json": {},
            "config_json": {},
            "input_schema": input_schema,
            "output_schema": output_schema,
            "allowed_skills_json": [FAULT_REPORT_SKILL_ID],
            "enabled": True,
        }
        if tool is None:
            tool = Tool(tenant_id="tenant_demo", name=name, **payload)
        else:
            for field_name, field_value in payload.items():
                setattr(tool, field_name, field_value)
            tool.updated_at = utc_now()
        db.add(tool)
        db.flush()
        ensure_open_gallery_binding(
            db,
            "tenant_demo",
            "tool",
            tool.id,
            metadata_json={"source": FAULT_REPORT_SKILL_ID, "system_seeded": True},
        )
        if information_technology_agent is not None:
            ensure_private_resource_binding(
                db,
                "tenant_demo",
                information_technology_agent.id,
                "tool",
                tool.id,
                metadata_json={"source": FAULT_REPORT_SKILL_ID},
            )


def _fault_report_lifecycle_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """把报修定义升级为可信提交、工程师办理、报修人验收和关闭的完整图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": FAULT_REPORT_SKILL_ID,
            "name": "故障报修受理",
            "version": FAULT_REPORT_LIFECYCLE_VERSION,
            "execution_mode": "deterministic",
            "required_info": [
                "employee_id",
                "category",
                "title",
                "description",
                "confirmation",
                "resolution_confirmation",
            ],
            "start_node_id": "node_collect_fault_report",
            "terminal_node_ids": [
                "node_ticket_closed",
                "node_ticket_reopened",
                "node_ticket_escalated",
                "node_ticket_failure",
                "node_ticket_cancelled",
                "node_ticket_confirmation_invalid",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "category": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "contact": {"type": "string"},
                        "confirmation": {"type": "string"},
                        "resolution_confirmation": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "ticket_create": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "ticket_id": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "ticket_close": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"status": {"type": "string"}},
                                },
                            },
                        },
                        "ticket_reopen": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {"status": {"type": "string"}},
                                },
                            },
                        },
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_fault_report",
                    "type": "collect_info",
                    "name": "收集报修信息",
                    "instruction": (
                        "默认使用登录账号绑定的本人工号。根据用户原文生成简洁工单标题和完整问题描述，"
                        "描述必须保留故障现象和影响范围。将类别规范为 hardware、software、network、"
                        "account 或 other；联系方式可选。不要自行承诺优先级、处理人或 SLA。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "category",
                        "title",
                        "description",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [],
                            }
                        },
                        "value_aliases": {
                            "category": {
                                "硬件": "hardware",
                                "hardware": "hardware",
                                "软件": "software",
                                "software": "software",
                                "网络": "network",
                                "vpn": "network",
                                "network": "network",
                                "账号": "account",
                                "账户": "account",
                                "account": "account",
                                "其他": "other",
                                "other": "other",
                            }
                        },
                    },
                },
                {
                    "node_id": "node_confirm_ticket_create",
                    "type": "collect_info",
                    "name": "明确确认创建工单",
                    "instruction": "展示工单类别、标题和问题描述，等待用户明确确认或取消。",
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认": "confirmed",
                                "确认报修": "confirmed",
                                "确认建单": "confirmed",
                                "确认提交": "confirmed",
                                "是的，确认报修": "confirmed",
                                "yes": "confirmed",
                                "confirmed": "confirmed",
                                "取消": "cancelled",
                                "取消报修": "cancelled",
                                "不报修了": "cancelled",
                                "不要了": "cancelled",
                                "cancel": "cancelled",
                            },
                            "prompt": (
                                "报修信息已收集，但尚未创建工单。请核对故障现象和影响范围后回复"
                                "“确认报修”；如不再需要，请回复“取消报修”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_call_ticket_create",
                    "type": "tool_call",
                    "name": "创建 IT 工单",
                    "instruction": "只在当前轮收到明确确认后调用一次 IT 工单登记工具。",
                    "allowed_actions": ["call_tool:it.ticket_create"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "category": "slots.category",
                            "title": "slots.title",
                            "description": "slots.description",
                            "contact": "slots.contact",
                        },
                        "operation_result_key": "ticket_create",
                    },
                },
                {
                    "node_id": "node_engineer_resolution",
                    "type": "human_task",
                    "name": "IT 工程师处理故障",
                    "instruction": "由真实 IT 支持工程师认领，完成维修后填写解决说明。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [IT_SUPPORT_ENGINEER_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "action_permissions": {
                                "claim": "it.ticket.claim",
                                "outcome:resolved": "it.ticket.resolve",
                                "outcome:escalated": "it.ticket.escalate",
                            },
                            "waiting_message": (
                                "工单已创建，正在等待具备 IT 支持工程师角色的员工认领处理。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "resolved",
                                    "label": "标记已解决",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "IT 工程师已提交解决结果。处理说明：{comment}。"
                                        "请确认故障是否已经恢复；恢复后回复“确认已恢复”，"
                                        "仍有问题请回复“仍未解决”。"
                                    ),
                                },
                                {
                                    "value": "escalated",
                                    "label": "升级处理",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "IT 工程师已将工单升级处理。说明：{comment}。当前工单尚未关闭。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "node_confirm_resolution",
                    "type": "collect_info",
                    "name": "报修人验收维修结果",
                    "instruction": "只允许原会话报修人确认恢复或反馈仍未解决。",
                    "expected_user_info": ["resolution_confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "resolution_confirmation",
                            "phrase_values": {
                                "确认已恢复": "confirmed",
                                "已经恢复": "confirmed",
                                "已恢复": "confirmed",
                                "可以关闭": "confirmed",
                                "确认关闭": "confirmed",
                                "仍未解决": "not_resolved",
                                "还没好": "not_resolved",
                                "没有恢复": "not_resolved",
                            },
                            "prompt": (
                                "工程师已提交解决结果。故障恢复后请回复“确认已恢复”；"
                                "如仍有问题请回复“仍未解决”。"
                            ),
                        }
                    },
                },
                {
                    "node_id": "node_call_ticket_close",
                    "type": "tool_call",
                    "name": "关闭 IT 工单",
                    "instruction": "只在原报修人当前轮明确确认恢复后关闭工单。",
                    "allowed_actions": ["call_tool:it.ticket_close"],
                    "metadata": {
                        "operation_input": {
                            "ticket_id": "tool_result.ticket_create.data.ticket_id",
                            "requester_employee_id": "slots.employee_id",
                        },
                        "operation_result_key": "ticket_close",
                    },
                },
                {
                    "node_id": "node_call_ticket_reopen",
                    "type": "tool_call",
                    "name": "重新打开 IT 工单",
                    "instruction": "报修人明确反馈仍未解决时重新打开工单。",
                    "allowed_actions": ["call_tool:it.ticket_reopen"],
                    "metadata": {
                        "operation_input": {
                            "ticket_id": "tool_result.ticket_create.data.ticket_id",
                            "requester_employee_id": "slots.employee_id",
                        },
                        "operation_result_key": "ticket_reopen",
                    },
                },
                {
                    "node_id": "node_ticket_closed",
                    "type": "terminal",
                    "name": "工单已关闭",
                    "instruction": "反馈本次工单号、报修人验收事实和 closed 回执。",
                },
                {
                    "node_id": "node_ticket_reopened",
                    "type": "terminal",
                    "name": "工单已重开",
                    "instruction": "反馈工单已重开且尚未闭环，不得描述为已解决。",
                },
                {
                    "node_id": "node_ticket_escalated",
                    "type": "terminal",
                    "name": "工单已升级",
                    "instruction": "反馈工程师已升级处理且工单尚未关闭。",
                },
                {
                    "node_id": "node_ticket_failure",
                    "type": "response",
                    "name": "反馈工单创建失败",
                    "instruction": "说明建单工具失败并建议稍后重试，不得虚构 TICKET 工单号。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_ticket_cancelled",
                    "type": "response",
                    "name": "反馈用户取消报修",
                    "instruction": "明确说明本次报修已取消且没有调用工单工具。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_ticket_confirmation_invalid",
                    "type": "response",
                    "name": "阻断异常确认",
                    "instruction": "确认状态异常，未调用建单工具，请用户重新发起。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_fault_report",
                    "next_node_id": "node_confirm_ticket_create",
                },
                {
                    "source_node_id": "node_confirm_ticket_create",
                    "next_node_id": "node_call_ticket_create",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "明确确认",
                },
                {
                    "source_node_id": "node_confirm_ticket_create",
                    "next_node_id": "node_ticket_cancelled",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "cancelled"},
                    },
                    "priority": 90,
                    "label": "取消报修",
                },
                {
                    "source_node_id": "node_confirm_ticket_create",
                    "next_node_id": "node_ticket_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常确认默认阻断",
                },
                {
                    "source_node_id": "node_call_ticket_create",
                    "next_node_id": "node_engineer_resolution",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.ticket_create.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "in",
                                "left": {"path": "tool_result.ticket_create.data.status"},
                                "right": {"value": ["created", "assigned", "pending"]},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "工单进入工程师队列",
                },
                {
                    "source_node_id": "node_call_ticket_create",
                    "next_node_id": "node_ticket_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "失败默认路径",
                },
                {
                    "source_node_id": "node_engineer_resolution",
                    "next_node_id": "node_confirm_resolution",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "resolved"},
                    },
                    "priority": 100,
                    "label": "工程师标记已解决",
                },
                {
                    "source_node_id": "node_engineer_resolution",
                    "next_node_id": "node_ticket_escalated",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "升级处理",
                },
                {
                    "source_node_id": "node_confirm_resolution",
                    "next_node_id": "node_call_ticket_close",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.resolution_confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "报修人确认恢复",
                },
                {
                    "source_node_id": "node_confirm_resolution",
                    "next_node_id": "node_call_ticket_reopen",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.resolution_confirmation"},
                        "right": {"value": "not_resolved"},
                    },
                    "priority": 90,
                    "label": "报修人反馈未恢复",
                },
                {
                    "source_node_id": "node_confirm_resolution",
                    "next_node_id": "node_ticket_confirmation_invalid",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "异常验收默认阻断",
                },
                {
                    "source_node_id": "node_call_ticket_close",
                    "next_node_id": "node_ticket_closed",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.ticket_close.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.ticket_close.data.status"},
                                "right": {"value": "closed"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "工单关闭成功",
                },
                {
                    "source_node_id": "node_call_ticket_close",
                    "next_node_id": "node_ticket_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "关闭失败",
                },
                {
                    "source_node_id": "node_call_ticket_reopen",
                    "next_node_id": "node_ticket_reopened",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.ticket_reopen.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.ticket_reopen.data.status"},
                                "right": {"value": "reopened"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "工单重开成功",
                },
                {
                    "source_node_id": "node_call_ticket_reopen",
                    "next_node_id": "node_ticket_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "重开失败",
                },
            ],
            "response_rules": [
                "未收到当前轮明确确认前禁止调用 it.ticket_create。",
                "创建、关闭或重开工具在各自节点最多调用一次，工单号和状态只能来自本次回执。",
                "工程师处理结果必须来自结构化工作项，禁止用 approved/rejected 代替维修结果。",
                "只有原报修人当前轮明确确认恢复后才能关闭工单。",
                "当前确定性 Runtime 未执行知识检索，不得声称已自动排障或无法访问知识库。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = [
        "employee_id",
        "employee_name",
        "category",
        "title",
        "description",
        "contact",
        "confirmation",
        "resolution_confirmation",
    ]
    content["slot_filling_policy"] = slot_policy
    return content


def _overtime_compensatory_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """把历史加班调休流程迁移为政策、资格、余额、确认、提交和接管统一图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": OVERTIME_COMPENSATORY_SKILL_ID,
            "name": "加班调休申请",
            "version": OVERTIME_COMPENSATORY_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "business_domain": "人事与员工服务",
            "description": (
                "依据当前可见加班制度和 HR 已入账调休余额，办理本人调休申请或转 HR 核对。"
            ),
            "trigger_intents": ["申请加班调休", "用加班额度调休", "办理补休"],
            "user_utterance_examples": [
                (
                    "我 2026-07-25 休息日加班 4 小时，已事前审批，"
                    "想在 2026-07-30 调休一天，原因是版本发布"
                ),
                "工作日加班 3 小时已经审批，想申请下周一调休",
            ],
            "goal": [
                "使用登录账号绑定的可信员工身份",
                "核对日期类型、事前审批、1:1 小时折算及适用范围",
                "由 HR 回执计算计划调休天数并校验已入账余额",
                "取得当前轮明确确认后提交调休申请",
                "政策或资格异常时创建 HR 假勤专员接管任务",
            ],
            "required_info": [
                "employee_id",
                "employee_name",
                "leave_type",
                "overtime_date",
                "overtime_hours",
                "overtime_type",
                "pre_approval_status",
                "reason",
                "planned_start_date",
                "planned_end_date",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "leave_type": {"type": "string"},
                        "overtime_date": {"type": "string"},
                        "overtime_hours": {"type": "number"},
                        "overtime_type": {"type": "string"},
                        "pre_approval_status": {"type": "string"},
                        "reason": {"type": "string"},
                        "planned_start_date": {"type": "string"},
                        "planned_end_date": {"type": "string"},
                        "confirmation": {"type": "string"},
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "overtime_policy": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "outcome": {
                                            "type": "string",
                                            "enum": [
                                                "evidence_found",
                                                "no_match",
                                                "insufficient",
                                            ],
                                        }
                                    },
                                },
                            },
                        }
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "overtime_balance": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "overtime_policy_assessment": {
                                            "type": "object",
                                            "properties": {
                                                "status": {
                                                    "type": "string",
                                                    "enum": [
                                                        "eligible",
                                                        "preapproval_missing",
                                                        "workday_minimum_not_met",
                                                        "statutory_holiday",
                                                        "invalid_date",
                                                        "manual_review",
                                                    ],
                                                },
                                                "conversion_ratio": {"type": "string"},
                                                "credit_unit": {"type": "string"},
                                                "credited_hours": {"type": "number"},
                                            },
                                        },
                                        "request_assessment": {
                                            "type": "object",
                                            "properties": {
                                                "status": {
                                                    "type": "string",
                                                    "enum": [
                                                        "sufficient",
                                                        "insufficient",
                                                        "manual_review",
                                                        "invalid_date",
                                                    ],
                                                },
                                                "requested_days": {"type": "number"},
                                                "available_days": {"type": "number"},
                                            },
                                        },
                                        "overtime_credit_assessment": {
                                            "type": "object",
                                            "properties": {
                                                "status": {
                                                    "type": "string",
                                                    "enum": [
                                                        "sufficient",
                                                        "insufficient",
                                                        "manual_review",
                                                        "invalid_date",
                                                    ],
                                                },
                                                "standard_hours_per_day": {"type": "number"},
                                                "requested_hours": {"type": "number"},
                                                "credited_hours": {"type": "number"},
                                                "available_hours": {"type": "number"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                        "compensatory_application": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": ["approved", "pending", "rejected"],
                                        },
                                        "application_id": {"type": "string"},
                                        "approver": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
            },
            "slot_filling_policy": {
                "enabled": True,
                "multi_slot_per_turn": True,
                "extract_scope": "all_skill_expected_user_info",
                "skip_satisfied_steps": True,
                "target_info": [
                    "employee_id",
                    "employee_name",
                    "leave_type",
                    "overtime_date",
                    "overtime_hours",
                    "overtime_type",
                    "pre_approval_status",
                    "reason",
                    "planned_start_date",
                    "planned_end_date",
                    "confirmation",
                ],
            },
            "slot_key_aliases": {
                "is_pre_approved": "pre_approval_status",
                "overtime_duration": "overtime_hours",
                "overtime_duration_hours": "overtime_hours",
                "overtime_day_type": "overtime_type",
                "overtime_reason": "reason",
            },
            "nodes": [
                {
                    "node_id": "collect_overtime_information",
                    "type": "collect_info",
                    "name": "收集本人加班与计划调休信息",
                    "instruction": (
                        "身份只取登录账号绑定档案；收集加班日期、小时数、日期类型、"
                        "事前审批、事由和计划调休起止日期。假种必须为调休。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "employee_name",
                        "leave_type",
                        "overtime_date",
                        "overtime_hours",
                        "overtime_type",
                        "pre_approval_status",
                        "reason",
                        "planned_start_date",
                        "planned_end_date",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                            },
                            "employee_name": {
                                "source": "authenticated_employee",
                                "attribute": "employee_name",
                            },
                        },
                        "value_aliases": {
                            "leave_type": {
                                "调休": "compensatory",
                                "补休": "compensatory",
                                "compensatory": "compensatory",
                            },
                            "overtime_type": {
                                "工作日": "workday",
                                "workday": "workday",
                                "休息日": "rest_day",
                                "周末": "rest_day",
                                "rest_day": "rest_day",
                                "法定节假日": "statutory_holiday",
                                "statutory_holiday": "statutory_holiday",
                            },
                            "pre_approval_status": {
                                "approved": "approved",
                                "not_approved": "not_approved",
                            },
                        },
                    },
                },
                {
                    "node_id": "check_overtime_policy",
                    "type": "knowledge_query",
                    "name": "核对加班调休制度",
                    "instruction": (
                        "检索当前数字员工可见知识库中的事前审批、日期类型、"
                        "1:1 折算、有效期和最小使用单位，取得可追溯依据后才能继续。"
                    ),
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {
                            "overtime_date": "slots.overtime_date",
                            "overtime_duration_hours": "slots.overtime_hours",
                            "overtime_day_type": "slots.overtime_type",
                            "pre_approval_status": "slots.pre_approval_status",
                        },
                        "operation_result_key": "overtime_policy",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": (
                                "工作日加班2小时以上、休息日加班、法定节假日不可调休、"
                                "事前审批、按 1:1 折算、调休最小使用单位0.5天"
                            ),
                            "max_chunks": 8,
                            "max_depth": 3,
                        },
                    },
                },
                {
                    "node_id": "assess_overtime_and_balance",
                    "type": "tool_call",
                    "name": "核对加班资格与调休余额",
                    "instruction": (
                        "由 HR 受控回执判断加班政策资格，并计算计划调休含首尾自然日和已入账余额。"
                    ),
                    "allowed_actions": ["call_tool:hr.balance_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "leave_type": "slots.leave_type",
                            "start_date": "slots.planned_start_date",
                            "end_date": "slots.planned_end_date",
                            "overtime_date": "slots.overtime_date",
                            "overtime_duration_hours": "slots.overtime_hours",
                            "overtime_day_type": "slots.overtime_type",
                            "pre_approval_status": "slots.pre_approval_status",
                        },
                        "operation_result_key": "overtime_balance",
                    },
                },
                {
                    "node_id": "confirm_compensatory_submit",
                    "type": "collect_info",
                    "name": "确认提交调休申请",
                    "instruction": (
                        "展示加班事实、1:1 小时回执、计划调休日期、HR 计算天数及待审批状态，"
                        "仅接受当前轮明确确认。"
                    ),
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认提交": "confirmed",
                                "取消提交": "cancelled",
                            },
                            "prompt": "请核对加班与调休信息，并回复“确认提交”或“取消提交”。",
                        }
                    },
                },
                {
                    "node_id": "submit_compensatory_application",
                    "type": "tool_call",
                    "name": "提交调休申请",
                    "instruction": "只使用可信身份、计划日期和本次 HR 回执计算天数提交。",
                    "allowed_actions": ["call_tool:hr.leave_apply"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "leave_type": "slots.leave_type",
                            "start_date": "slots.planned_start_date",
                            "end_date": "slots.planned_end_date",
                            "days": (
                                "tool_result.overtime_balance.data."
                                "request_assessment.requested_days"
                            ),
                            "reason": "slots.reason",
                        },
                        "operation_result_key": "compensatory_application",
                    },
                },
                {
                    "node_id": "hr_overtime_review",
                    "type": "human_task",
                    "name": "HR 核对加班调休资格",
                    "instruction": (
                        "由真实 HR 假勤专员认领，核对审批、考勤、日期类型、制度证据和调休额度。"
                    ),
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [HR_LEAVE_SPECIALIST_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["reviewed", "needs_information"],
                            "action_permissions": {
                                "claim": "hr.overtime_review.claim",
                                "outcome:reviewed": "hr.overtime_review.complete",
                                "outcome:needs_information": (
                                    "hr.overtime_review.request_information"
                                ),
                            },
                            "waiting_message": (
                                "当前加班调休事项无法自动确认，已创建 HR 假勤专员核对任务。"
                            ),
                            "outcome_options": [
                                {
                                    "value": "reviewed",
                                    "label": "提交核对意见",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "HR 已完成加班调休资格核对：{comment}。"
                                        "本次人工核对不会自动生成请假申请。"
                                    ),
                                },
                                {
                                    "value": "needs_information",
                                    "label": "要求补充材料",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": (
                                        "HR 要求补充加班调休材料：{comment}。"
                                        "补充后请重新发起或按 HR 指引办理。"
                                    ),
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "compensatory_submitted_pending",
                    "type": "response",
                    "name": "调休申请已提交待审批",
                    "instruction": "返回本次 LEAVE 单号、pending 状态和审批环节，不得说已批准。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "compensatory_submitted_approved",
                    "type": "response",
                    "name": "调休申请已批准",
                    "instruction": "仅当本次回执明确 approved 时反馈批准和申请单号。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "compensatory_submission_rejected",
                    "type": "response",
                    "name": "调休申请被系统拒绝",
                    "instruction": "依据 rejected 回执说明未受理，不得编造申请成功。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "compensatory_submission_failed",
                    "type": "response",
                    "name": "调休提交失败",
                    "instruction": "明确工具失败且没有形成可确认申请单。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "overtime_assessment_failed",
                    "type": "response",
                    "name": "加班资格与余额查询失败",
                    "instruction": "说明 HR 查询失败，禁止使用历史回执继续提交。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "compensatory_submission_cancelled",
                    "type": "response",
                    "name": "调休提交已取消",
                    "instruction": "确认没有调用提交工具，也没有生成申请单。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "overtime_review_completed",
                    "type": "response",
                    "name": "HR 核对已完成",
                    "instruction": "反馈人工核对意见，并明确未自动生成调休申请。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "overtime_review_needs_information",
                    "type": "response",
                    "name": "等待补充加班材料",
                    "instruction": "反馈 HR 要求补充的材料和后续办理方式。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_overtime_information",
                    "next_node_id": "check_overtime_policy",
                },
                {
                    "source_node_id": "check_overtime_policy",
                    "next_node_id": "assess_overtime_and_balance",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "node_output.overtime_policy.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "node_output.overtime_policy.data.outcome"
                                },
                                "right": {"value": "evidence_found"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "取得加班调休政策证据",
                },
                {
                    "source_node_id": "check_overtime_policy",
                    "next_node_id": "hr_overtime_review",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "政策证据不足转 HR",
                },
                {
                    "source_node_id": "assess_overtime_and_balance",
                    "next_node_id": "confirm_compensatory_submit",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.overtime_balance.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.overtime_balance.data."
                                        "overtime_policy_assessment.status"
                                    )
                                },
                                "right": {"value": "eligible"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.overtime_balance.data."
                                        "request_assessment.status"
                                    )
                                },
                                "right": {"value": "sufficient"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.overtime_balance.data."
                                        "overtime_credit_assessment.status"
                                    )
                                },
                                "right": {"value": "sufficient"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "资格和已入账余额均满足",
                },
                {
                    "source_node_id": "assess_overtime_and_balance",
                    "next_node_id": "hr_overtime_review",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.overtime_balance.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 50,
                    "label": "业务资格异常转 HR",
                },
                {
                    "source_node_id": "assess_overtime_and_balance",
                    "next_node_id": "overtime_assessment_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "查询失败默认路径",
                },
                {
                    "source_node_id": "confirm_compensatory_submit",
                    "next_node_id": "submit_compensatory_application",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "当前轮确认提交",
                },
                {
                    "source_node_id": "confirm_compensatory_submit",
                    "next_node_id": "compensatory_submission_cancelled",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "取消或未确认默认路径",
                },
                {
                    "source_node_id": "submit_compensatory_application",
                    "next_node_id": "compensatory_submitted_pending",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.compensatory_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.compensatory_application.data.status"
                                    )
                                },
                                "right": {"value": "pending"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "提交成功待审批",
                },
                {
                    "source_node_id": "submit_compensatory_application",
                    "next_node_id": "compensatory_submitted_approved",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.compensatory_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.compensatory_application.data.status"
                                    )
                                },
                                "right": {"value": "approved"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "提交并即时批准",
                },
                {
                    "source_node_id": "submit_compensatory_application",
                    "next_node_id": "compensatory_submission_rejected",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.compensatory_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.compensatory_application.data.status"
                                    )
                                },
                                "right": {"value": "rejected"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "业务拒绝",
                },
                {
                    "source_node_id": "submit_compensatory_application",
                    "next_node_id": "compensatory_submission_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "提交失败默认路径",
                },
                {
                    "source_node_id": "hr_overtime_review",
                    "next_node_id": "overtime_review_completed",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "reviewed"},
                    },
                    "priority": 100,
                    "label": "HR 完成核对",
                },
                {
                    "source_node_id": "hr_overtime_review",
                    "next_node_id": "overtime_review_needs_information",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "要求补充材料",
                },
            ],
            "start_node_id": "collect_overtime_information",
            "terminal_node_ids": [
                "compensatory_submitted_pending",
                "compensatory_submitted_approved",
                "compensatory_submission_rejected",
                "compensatory_submission_failed",
                "overtime_assessment_failed",
                "compensatory_submission_cancelled",
                "overtime_review_completed",
                "overtime_review_needs_information",
            ],
            "interruption_policy": {},
            "response_rules": [
                "只有知识证据命中、HR 资格 eligible 且已入账调休余额充分时才允许确认提交。",
                "1:1 只按小时记录；现有制度未定义日工时，禁止把加班小时自行换算为调休天数。",
                "调休天数只能来自本次 HR 回执对计划起止日期的计算，不得由模型计算。",
                "未收到当前轮明确确认前禁止调用 hr.leave_apply。",
                "政策或业务资格异常必须创建 HR 工作项；传输失败不得伪装成业务接管。",
                "申请单号和 pending/approved/rejected 状态只能来自本次提交回执。",
            ],
        }
    )
    return content


def _leave_application_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """把历史请假流程迁移为知识证据、自然日余额、确认和受理回执统一图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": LEAVE_APPLICATION_SKILL_ID,
            "name": "请假申请办理",
            "version": LEAVE_APPLICATION_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "business_domain": "人事与员工服务",
            "description": "依据当前可见制度、自然日余额和当轮明确确认提交本人请假申请。",
            "trigger_intents": ["申请请假", "办理年假", "发起请假申请"],
            "user_utterance_examples": [
                "申请 2026-07-27 到 2026-07-28 两天年假，处理家庭事务",
                "帮我发起下周一到周二的年假申请",
            ],
            "goal": [
                "使用登录账号绑定的可信员工身份",
                "检索当前数字员工可见的请假制度依据",
                "由 HR 回执计算含首尾自然日并校验余额",
                "取得当前轮明确确认后提交申请",
                "返回申请单号和真实待审批状态",
            ],
            "required_info": [
                "employee_id",
                "employee_name",
                "leave_type",
                "start_date",
                "end_date",
                "reason",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "leave_type": {"type": "string"},
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                        "reason": {"type": "string"},
                        "confirmation": {"type": "string"},
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "leave_policy": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "outcome": {
                                            "type": "string",
                                            "enum": [
                                                "evidence_found",
                                                "no_match",
                                                "insufficient",
                                            ],
                                        }
                                    },
                                },
                            },
                        }
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "leave_balance": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "request_assessment": {
                                            "type": "object",
                                            "properties": {
                                                "status": {
                                                    "type": "string",
                                                    "enum": [
                                                        "sufficient",
                                                        "insufficient",
                                                        "manual_review",
                                                        "invalid_date",
                                                    ],
                                                },
                                                "requested_days": {"type": "number"},
                                                "available_days": {"type": "number"},
                                            },
                                        }
                                    },
                                },
                            },
                        },
                        "leave_application": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": ["approved", "pending", "rejected"],
                                        },
                                        "application_id": {"type": "string"},
                                        "approver": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                },
            },
            "slot_filling_policy": {
                "enabled": True,
                "multi_slot_per_turn": True,
                "extract_scope": "all_skill_expected_user_info",
                "skip_satisfied_steps": True,
                "target_info": [
                    "employee_id",
                    "employee_name",
                    "leave_type",
                    "start_date",
                    "end_date",
                    "reason",
                    "confirmation",
                ],
            },
            "nodes": [
                {
                    "node_id": "collect_leave_information",
                    "type": "collect_info",
                    "name": "收集本人请假信息",
                    "instruction": (
                        "员工身份只能取当前登录账号绑定档案。收集假种、ISO 起止日期和事由；"
                        "不得自行计算或写入请假天数。"
                    ),
                    "expected_user_info": [
                        "employee_id",
                        "employee_name",
                        "leave_type",
                        "start_date",
                        "end_date",
                        "reason",
                    ],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                            },
                            "employee_name": {
                                "source": "authenticated_employee",
                                "attribute": "employee_name",
                            },
                        },
                        "value_aliases": {
                            "leave_type": {
                                "年假": "annual",
                                "annual": "annual",
                                "事假": "personal",
                                "personal": "personal",
                                "病假": "sick",
                                "sick": "sick",
                                "调休": "compensatory",
                                "补休": "compensatory",
                                "compensatory": "compensatory",
                                "婚假": "marriage",
                                "marriage": "marriage",
                                "产假": "maternity",
                                "maternity": "maternity",
                                "其他": "other",
                                "other": "other",
                            }
                        },
                    },
                },
                {
                    "node_id": "check_leave_policy",
                    "type": "knowledge_query",
                    "name": "核对请假制度依据",
                    "instruction": (
                        "检索当前假种、起止日期和事由直接相关的公司制度；"
                        "必须取得可引用依据才能继续。"
                    ),
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {
                            "leave_type": "slots.leave_type",
                            "start_date": "slots.start_date",
                            "end_date": "slots.end_date",
                            "reason": "slots.reason",
                        },
                        "operation_result_key": "leave_policy",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": "年假申请时限",
                            "max_chunks": 8,
                            "max_depth": 3,
                        },
                    },
                },
                {
                    "node_id": "query_leave_balance",
                    "type": "tool_call",
                    "name": "计算自然日并核对余额",
                    "instruction": (
                        "调用 HR 查询工具，由受控回执计算含首尾自然日并返回余额充分性。"
                    ),
                    "allowed_actions": ["call_tool:hr.balance_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "leave_type": "slots.leave_type",
                            "start_date": "slots.start_date",
                            "end_date": "slots.end_date",
                        },
                        "operation_result_key": "leave_balance",
                    },
                },
                {
                    "node_id": "select_automatic_leave_path",
                    "type": "decision",
                    "name": "选择自动办理范围",
                    "instruction": (
                        "当前版本只自动提交已取得政策证据且余额充分的年假；"
                        "其他假种保守要求 HR 人工核对材料和规则。"
                    ),
                },
                {
                    "node_id": "confirm_leave_submit",
                    "type": "collect_info",
                    "name": "确认提交请假申请",
                    "instruction": (
                        "展示假种、起止日期、HR 回执计算天数、事由和待直属主管审批状态，"
                        "仅接受当前轮明确确认。"
                    ),
                    "expected_user_info": ["confirmation"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "confirmation_policy": {
                            "slot_name": "confirmation",
                            "phrase_values": {
                                "确认提交": "confirmed",
                                "取消提交": "cancelled",
                            },
                            "prompt": "请核对请假信息，并回复“确认提交”或“取消提交”。",
                        }
                    },
                },
                {
                    "node_id": "submit_leave_application",
                    "type": "tool_call",
                    "name": "提交请假申请",
                    "instruction": "只使用可信身份、已收集信息和本次 HR 余额回执中的计算天数提交。",
                    "allowed_actions": ["call_tool:hr.leave_apply"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "employee_name": "slots.employee_name",
                            "leave_type": "slots.leave_type",
                            "start_date": "slots.start_date",
                            "end_date": "slots.end_date",
                            "days": (
                                "tool_result.leave_balance.data."
                                "request_assessment.requested_days"
                            ),
                            "reason": "slots.reason",
                        },
                        "operation_result_key": "leave_application",
                    },
                },
                {
                    "node_id": "leave_submitted_pending",
                    "type": "response",
                    "name": "请假申请已提交待审批",
                    "instruction": (
                        "返回本次 LEAVE 申请单号、pending 状态和审批环节；"
                        "必须明确已提交待审批，不得说已批准。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_submitted_approved",
                    "type": "response",
                    "name": "请假申请已批准",
                    "instruction": "仅当本次工具回执明确为 approved 时反馈批准及申请单号。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_submission_rejected",
                    "type": "response",
                    "name": "请假申请被系统拒绝",
                    "instruction": "依据本次 rejected 回执说明未受理，不得编造申请成功。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_submission_failed",
                    "type": "response",
                    "name": "请假提交失败",
                    "instruction": "明确说明工具调用失败且没有形成可确认的申请单。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_policy_unavailable",
                    "type": "response",
                    "name": "请假政策依据不足",
                    "instruction": "说明未取得足够制度依据，建议联系 HR 核对，不得继续提交。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "hr_leave_review",
                    "type": "human_task",
                    "name": "HR 核对请假事项",
                    "instruction": "由 HR 假勤专员核对余额不足、特殊假种、日期或材料要求。",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [HR_LEAVE_SPECIALIST_ROLE],
                            "completion_mode": "any",
                            "claim_required": True,
                            "exclude_initiator": True,
                            "timeout_seconds": 86400,
                            "timeout_action": "fail",
                            "allowed_outcomes": ["reviewed", "needs_information"],
                            "action_permissions": {
                                "claim": "hr.leave_review.claim",
                                "outcome:reviewed": "hr.leave_review.complete",
                                "outcome:needs_information": (
                                    "hr.leave_review.request_information"
                                ),
                            },
                            "waiting_message": "当前请假事项无法自动受理，已创建 HR 假勤专员核对任务。",
                            "outcome_options": [
                                {
                                    "value": "reviewed",
                                    "label": "提交核对意见",
                                    "tone": "success",
                                    "comment_required": True,
                                    "completion_message": (
                                        "HR 已完成请假事项核对：{comment}。"
                                        "本次人工核对不会自动生成请假申请。"
                                    ),
                                },
                                {
                                    "value": "needs_information",
                                    "label": "要求补充材料",
                                    "tone": "danger",
                                    "comment_required": True,
                                    "completion_message": "HR 要求补充请假材料：{comment}。",
                                },
                            ],
                        }
                    },
                },
                {
                    "node_id": "leave_balance_failed",
                    "type": "response",
                    "name": "请假余额查询失败",
                    "instruction": "说明 HR 查询失败，禁止使用历史余额继续提交。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_review_completed",
                    "type": "response",
                    "name": "HR 请假核对已完成",
                    "instruction": "反馈 HR 核对意见，并明确本次未自动生成请假申请。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_review_needs_information",
                    "type": "response",
                    "name": "等待补充请假材料",
                    "instruction": "反馈 HR 要求补充的请假材料和后续办理方式。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "leave_submission_cancelled",
                    "type": "response",
                    "name": "请假提交已取消",
                    "instruction": "确认本次没有调用提交工具，也没有生成申请单。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_leave_information",
                    "next_node_id": "select_automatic_leave_path",
                },
                {
                    "source_node_id": "select_automatic_leave_path",
                    "next_node_id": "check_leave_policy",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.leave_type"},
                        "right": {"value": "annual"},
                    },
                    "priority": 100,
                    "label": "年假自动受理",
                },
                {
                    "source_node_id": "select_automatic_leave_path",
                    "next_node_id": "hr_leave_review",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "其他假种默认人工核对",
                },
                {
                    "source_node_id": "check_leave_policy",
                    "next_node_id": "query_leave_balance",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "node_output.leave_policy.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": "node_output.leave_policy.data.outcome"
                                },
                                "right": {"value": "evidence_found"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "取得政策证据",
                },
                {
                    "source_node_id": "check_leave_policy",
                    "next_node_id": "leave_policy_unavailable",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "政策证据不足默认路径",
                },
                {
                    "source_node_id": "query_leave_balance",
                    "next_node_id": "confirm_leave_submit",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.leave_balance.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.leave_balance.data."
                                        "request_assessment.status"
                                    )
                                },
                                "right": {"value": "sufficient"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "余额充分",
                },
                {
                    "source_node_id": "query_leave_balance",
                    "next_node_id": "hr_leave_review",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.leave_balance.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.leave_balance.data."
                                        "request_assessment.status"
                                    )
                                },
                                "right": {"value": "insufficient"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "余额不足",
                },
                {
                    "source_node_id": "query_leave_balance",
                    "next_node_id": "hr_leave_review",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {"path": "tool_result.leave_balance.status"},
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "in",
                                "left": {
                                    "path": (
                                        "tool_result.leave_balance.data."
                                        "request_assessment.status"
                                    )
                                },
                                "right": {
                                    "value": ["manual_review", "invalid_date"]
                                },
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "日期或假种需人工核对",
                },
                {
                    "source_node_id": "query_leave_balance",
                    "next_node_id": "leave_balance_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "余额查询失败默认路径",
                },
                {
                    "source_node_id": "confirm_leave_submit",
                    "next_node_id": "submit_leave_application",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "slots.confirmation"},
                        "right": {"value": "confirmed"},
                    },
                    "priority": 100,
                    "label": "当前轮确认提交",
                },
                {
                    "source_node_id": "confirm_leave_submit",
                    "next_node_id": "leave_submission_cancelled",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "取消或未确认默认路径",
                },
                {
                    "source_node_id": "submit_leave_application",
                    "next_node_id": "leave_submitted_pending",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.leave_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.leave_application.data.status"
                                    )
                                },
                                "right": {"value": "pending"},
                            },
                        ],
                    },
                    "priority": 100,
                    "label": "提交成功待审批",
                },
                {
                    "source_node_id": "submit_leave_application",
                    "next_node_id": "leave_submitted_approved",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.leave_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.leave_application.data.status"
                                    )
                                },
                                "right": {"value": "approved"},
                            },
                        ],
                    },
                    "priority": 90,
                    "label": "提交并即时批准",
                },
                {
                    "source_node_id": "submit_leave_application",
                    "next_node_id": "leave_submission_rejected",
                    "condition": {
                        "op": "all",
                        "args": [
                            {
                                "op": "eq",
                                "left": {
                                    "path": "tool_result.leave_application.status"
                                },
                                "right": {"value": "succeeded"},
                            },
                            {
                                "op": "eq",
                                "left": {
                                    "path": (
                                        "tool_result.leave_application.data.status"
                                    )
                                },
                                "right": {"value": "rejected"},
                            },
                        ],
                    },
                    "priority": 80,
                    "label": "业务拒绝",
                },
                {
                    "source_node_id": "submit_leave_application",
                    "next_node_id": "leave_submission_failed",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "提交失败默认路径",
                },
                {
                    "source_node_id": "hr_leave_review",
                    "next_node_id": "leave_review_completed",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "reviewed"},
                    },
                    "priority": 100,
                    "label": "HR 完成核对",
                },
                {
                    "source_node_id": "hr_leave_review",
                    "next_node_id": "leave_review_needs_information",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "要求补充材料",
                },
            ],
            "start_node_id": "collect_leave_information",
            "terminal_node_ids": [
                "leave_submitted_pending",
                "leave_submitted_approved",
                "leave_submission_rejected",
                "leave_submission_failed",
                "leave_policy_unavailable",
                "leave_balance_failed",
                "leave_review_completed",
                "leave_review_needs_information",
                "leave_submission_cancelled",
            ],
            "interruption_policy": {},
            "response_rules": [
                "政策只使用本次 leave_policy 回执；no_match、insufficient 或技术失败均不得继续。",
                "天数只使用本次 HR 回执计算的含首尾自然日，不接受模型或历史消息自行计算。",
                "当前自动提交范围仅为政策证据充分且余额充分的本人年假申请。",
                "提交前必须取得当前轮明确确认；取消或模糊回复不得产生写操作。",
                "pending 只能表述为已提交待审批；没有 LEAVE 单号不得声称提交成功。",
            ],
        }
    )
    return content


def _leave_balance_deterministic_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """保留业务说明并替换为假期类型白名单归一的确定性查询图。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": LEAVE_BALANCE_SKILL_ID,
            "name": "假期余额查询",
            "version": LEAVE_BALANCE_DETERMINISTIC_VERSION,
            "execution_mode": "deterministic",
            "required_info": ["employee_id", "leave_type"],
            "start_node_id": "node_collect_leave_query",
            "terminal_node_ids": [
                "node_response_leave_balance",
                "node_response_leave_failure",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "leave_type": {"type": "string"},
                        "month": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "hr_balance_query": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {"type": "object"},
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_leave_query",
                    "type": "collect_info",
                    "name": "解析员工身份与假期类型",
                    "instruction": (
                        "默认使用登录账号绑定的员工身份；只有 HR 假勤专员明确提供目标工号时"
                        "才允许代查。必须识别年假、调休、病假或事假，月份可选。"
                    ),
                    "expected_user_info": ["employee_id", "leave_type"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [HR_LEAVE_SPECIALIST_ROLE],
                                "required_override_permission": "hr.leave_balance.read:any",
                            }
                        },
                        "value_aliases": {
                            "leave_type": {
                                "年假": "annual",
                                "annual": "annual",
                                "调休": "compensatory",
                                "补休": "compensatory",
                                "compensatory": "compensatory",
                                "病假": "sick",
                                "sick": "sick",
                                "事假": "personal",
                                "personal": "personal",
                            }
                        },
                    },
                },
                {
                    "node_id": "node_call_leave_balance",
                    "type": "tool_call",
                    "name": "查询假期余额",
                    "instruction": "按可信员工身份调用假期考勤查询工具。",
                    "allowed_actions": ["call_tool:hr.balance_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "month": "slots.month",
                        },
                        "operation_result_key": "hr_balance_query",
                    },
                },
                {
                    "node_id": "node_response_leave_balance",
                    "type": "response",
                    "name": "反馈指定假期余额",
                    "instruction": (
                        "只依据工具回执和规范 leave_type 回答对应余额：annual=年假、"
                        "compensatory=调休、sick=病假、personal=事假；明确单位为天。"
                        "工具没有有效期字段，不得承诺或编造有效期。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_response_leave_failure",
                    "type": "response",
                    "name": "反馈假期查询失败",
                    "instruction": (
                        "明确说明假期余额暂时查询失败并建议联系 HR，不得虚构余额或有效期。"
                    ),
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_leave_query",
                    "next_node_id": "node_call_leave_balance",
                },
                {
                    "source_node_id": "node_call_leave_balance",
                    "next_node_id": "node_response_leave_balance",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.hr_balance_query.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 100,
                    "label": "查询成功",
                },
                {
                    "source_node_id": "node_call_leave_balance",
                    "next_node_id": "node_response_leave_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "查询失败默认路径",
                },
            ],
            "response_rules": [
                "只回答工具回执中存在的余额字段，禁止补充工具没有返回的有效期。",
                "工具失败时明确建议联系 HR，不得把历史会话结果当作本次查询结果。",
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = ["employee_id", "leave_type", "month"]
    content["slot_filling_policy"] = slot_policy
    return content


def _expense_quota_identity_content(
    source_content: dict[str, object],
) -> dict[str, object]:
    """保留业务说明并替换为确定性图和可信员工身份输入契约。"""

    content = deepcopy(source_content)
    content.update(
        {
            "skill_id": EXPENSE_QUOTA_SKILL_ID,
            "name": "报销额度查询",
            "version": EXPENSE_QUOTA_IDENTITY_VERSION,
            "execution_mode": "deterministic",
            "required_info": ["employee_id"],
            "start_node_id": "node_collect_info",
            "terminal_node_ids": [
                "node_response_success",
                "node_response_failure",
            ],
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "month": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "expense_quota_query": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "data": {"type": "object"},
                            },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "node_collect_info",
                    "type": "collect_info",
                    "name": "解析查询员工身份",
                    "instruction": (
                        "默认使用登录账号绑定的员工身份；只有获得代办权限并明确提供目标工号时"
                        "才允许查询他人。月份可选，未提供时查询当前月份。"
                    ),
                    "expected_user_info": ["employee_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "input_bindings": {
                            "employee_id": {
                                "source": "authenticated_employee",
                                "attribute": "employee_id",
                                "allow_override_roles": [FINANCE_EXPENSE_SPECIALIST_ROLE],
                                "required_override_permission": "expense.quota.read:any",
                            }
                        }
                    },
                },
                {
                    "node_id": "node_call_quota_query",
                    "type": "tool_call",
                    "name": "查询报销额度",
                    "instruction": "按结构化参数调用报销额度查询工具。",
                    "allowed_actions": ["call_tool:expense.quota_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "month": "slots.month",
                        },
                        "operation_result_key": "expense_quota_query",
                    },
                },
                {
                    "node_id": "node_response_success",
                    "type": "response",
                    "name": "反馈额度结果",
                    "instruction": "清晰反馈月份、总额度、已使用额度、剩余额度和币种。",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "node_response_failure",
                    "type": "response",
                    "name": "反馈查询失败",
                    "instruction": "说明查询失败原因并建议用户稍后重试，不得虚构额度。",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "node_collect_info",
                    "next_node_id": "node_call_quota_query",
                },
                {
                    "source_node_id": "node_call_quota_query",
                    "next_node_id": "node_response_success",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.expense_quota_query.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 100,
                    "label": "查询成功",
                },
                {
                    "source_node_id": "node_call_quota_query",
                    "next_node_id": "node_response_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                    "label": "查询失败默认路径",
                },
            ],
        }
    )
    slot_policy = dict(content.get("slot_filling_policy") or {})
    slot_policy["target_info"] = ["employee_id", "month"]
    content["slot_filling_policy"] = slot_policy
    return content


def _ensure_employee_role_assignment(
    db: Session, profile: EmployeeProfile, role: BusinessRole
) -> None:
    """幂等授予演示员工租户级业务角色，不修改平台账号角色。"""

    assignment = db.exec(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.tenant_id == profile.tenant_id,
            EmployeeRoleAssignment.employee_profile_id == profile.id,
            EmployeeRoleAssignment.business_role_id == role.id,
            EmployeeRoleAssignment.scope_type == "tenant",
            EmployeeRoleAssignment.scope_id == "*",
        )
    ).first()
    if assignment is None:
        db.add(
            EmployeeRoleAssignment(
                tenant_id=profile.tenant_id,
                employee_profile_id=profile.id,
                business_role_id=role.id,
                scope_type="tenant",
                scope_id="*",
                status="active",
                effective_from=utc_now(),
                metadata_json={"source": "demo_seed"},
            )
        )


def _sync_seed_finance_branch(db: Session, skill: Skill) -> None:
    """仅同步未产生私有改写的财务员工分支，保留用户自定义分支。"""

    _sync_seed_agent_branch(db, skill, agent_name="财务")


def _sync_seed_agent_branch(db: Session, skill: Skill, *, agent_name: str) -> None:
    """同步指定内置数字员工的未改写分支，同时保留用户已经产生的私有版本。"""

    agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.name == agent_name,
            AgentProfile.status == "active",
        )
    ).first()
    if agent is None:
        return
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == skill.tenant_id,
            AgentSkillBranch.agent_id == agent.id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    if branch is not None and branch.sync_state != "synced":
        return
    sync_branch_from_overall(db, skill.tenant_id, agent.id, skill)


def _ensure_agent_role_binding(
    db: Session,
    agent: AgentProfile,
    role: BusinessRole,
    supervisor: EmployeeProfile | None,
    *,
    assignment_mode: str = "assist",
) -> AgentRoleBinding:
    """幂等取得数字员工的公司业务角色绑定，供调用方显式维护执行模式。"""

    binding = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == agent.tenant_id,
            AgentRoleBinding.agent_id == agent.id,
            AgentRoleBinding.business_role_id == role.id,
            AgentRoleBinding.scope_type == "tenant",
            AgentRoleBinding.scope_id == "*",
        )
    ).first()
    if binding is None:
        binding = AgentRoleBinding(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            business_role_id=role.id,
            assignment_mode=assignment_mode,
            supervisor_employee_profile_id=supervisor.id if supervisor else None,
            scope_type="tenant",
            scope_id="*",
            status="active",
            metadata_json={"source": role.metadata_json.get("source", role.role_code)},
        )
        db.add(binding)
    return binding


def _version_tuple(version: str) -> tuple[int, int, int]:
    """把三段式版本转为可比较元组，非标准版本按零版本保守处理。"""

    parts = version.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return int(parts[0]), int(parts[1]), int(parts[2])
    return 0, 0, 0
