"""
@Time       : 2026/07/27
@Author     : zhanglp8181
@File       : start_fullstack_server.py
@CallChain  : Playwright fullstack 配置 → 临时 SQLite → FastAPI 单端口应用
@Description: 启动隔离的真实前后端服务，并准备登录、知识、流程和分页浏览器回归数据。
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


FRONTEND_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = FRONTEND_DIR.parent / "backend"
E2E_PORT = 5148
E2E_SECRET = "fullstack-e2e-secret"
E2E_RUNTIME_DIR = Path(tempfile.gettempdir()) / "gongge-fullstack-e2e-current"


def legacy_password_hash(password: str) -> str:
    """复现升级前基于应用密钥生成固定盐的密码哈希。"""
    salt = hashlib.sha256(E2E_SECRET.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    )
    encoded = base64.urlsafe_b64encode(digest).decode("utf-8")
    return f"pbkdf2_sha256${salt}${encoded}"


def configure_environment(database_path: Path) -> None:
    os.environ.update(
        {
            "APP_ENV": "test",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(E2E_PORT),
            "APP_SECRET": E2E_SECRET,
            "AUTO_RESTART": "false",
            "DATABASE_URL": f"sqlite:///{database_path}",
            "PUBLIC_MOCK_API_KEY": "fullstack-e2e-public-mock-key",
        }
    )
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))


def seed_e2e_fixtures() -> None:
    """初始化 E2E 租户、双账号、数字员工、知识建议和可认领流程任务。"""
    from sqlmodel import Session, select

    from app.agents.branching import ensure_agent_private_knowledge_branch
    from app.db import engine, init_db
    from app.db.models import (
        AgentProfile,
        BusinessRole,
        ChatSession,
        EmployeeProfile,
        EmployeeRoleAssignment,
        KnowledgeBase,
        KnowledgeBaseVersion,
        KnowledgeDiscoverySuggestion,
        KnowledgeIngestJob,
        SopInstance,
        SopNodeExecution,
        SopOperation,
        Skill,
        SkillVersion,
        Tenant,
        User,
    )
    from app.approvals import ApprovalRequestService
    from app.db.demo_sop_versions import (
        EXPENSE_DEPARTMENT_APPROVER_ROLE,
        EXPENSE_FINANCE_APPROVER_ROLE,
        EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
    )
    from app.db.seed import seed_demo_data
    from app.organization.assignments import assign_member_to_organization
    from app.organization.units import (
        create_organization_unit,
        ensure_organization_foundation,
    )
    from app.sop_runtime.contracts import CompletionMode, WorkItemCompletionPolicy
    from app.sop_runtime.definition import (
        HumanTaskConfig,
        HumanTaskKind,
        ParticipantScopeResolver,
    )
    from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
    from app.sop_runtime.work_items import SopWorkItemService

    init_db()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="E2E Enterprise"))
        db.add(
            User(
                id="admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="E2E Administrator",
                role="admin",
                password_hash=legacy_password_hash("admin"),
            )
        )
        db.commit()
        seed_demo_data(db)

        db.add(
            User(
                id="member_e2e",
                tenant_id="tenant_demo",
                username="member",
                display_name="E2E Member",
                role="member",
                password_hash=legacy_password_hash("member"),
            )
        )
        db.add(
            User(
                id="other_member_e2e",
                tenant_id="tenant_demo",
                username="other-member",
                display_name="E2E Other Member",
                role="member",
                password_hash=legacy_password_hash("other-member"),
            )
        )
        db.add(
            User(
                id="member_two_e2e",
                tenant_id="tenant_demo",
                username="member-two",
                display_name="E2E Second Member",
                role="member",
                password_hash=legacy_password_hash("member-two"),
            )
        )
        db.add(
            User(
                id="finance_e2e",
                tenant_id="tenant_demo",
                username="finance",
                display_name="E2E Finance",
                role="member",
                password_hash=legacy_password_hash("finance"),
            )
        )
        db.add(
            User(
                id="requestor_e2e",
                tenant_id="tenant_demo",
                username="requestor",
                display_name="E2E Requestor",
                role="member",
                password_hash=legacy_password_hash("requestor"),
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_employee",
                tenant_id="tenant_demo",
                name="E2E 数字员工",
                status="active",
                metadata_json={"owner_user_id": "admin", "owner_username": "admin"},
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_member_employee",
                tenant_id="tenant_demo",
                name="E2E 成员数字员工",
                status="active",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_gallery",
                tenant_id="tenant_demo",
                name="E2E 企业广场员工",
                status="active",
                owner_user_id="admin",
                published_to_gallery=True,
                gallery_published_by="admin",
                agent_category_code="assistant",
                visibility_scope="tenant",
                metadata_json={
                    "owner_user_id": "admin",
                    "owner_username": "admin",
                    "published_to_gallery": True,
                },
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_e2e",
                tenant_id="tenant_demo",
                name="E2E Knowledge Base",
                metadata_json={
                    "current_version": "1.0.0",
                    "owner_agent_id": "agent_e2e_employee",
                    "created_from_agent": True,
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_kb_e2e_1_0_0",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                version="1.0.0",
                name="E2E Knowledge Base",
            )
        )
        db.add(
            KnowledgeBase(
                id="kb_e2e_member",
                tenant_id="tenant_demo",
                name="E2E Member Knowledge Base",
                metadata_json={
                    "current_version": "1.0.0",
                    "owner_agent_id": "agent_e2e_member_employee",
                    "created_from_agent": True,
                },
            )
        )
        db.add(
            KnowledgeBaseVersion(
                id="kbver_kb_e2e_member_1_0_0",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                version="1.0.0",
                name="E2E Member Knowledge Base",
            )
        )
        db.add(
            KnowledgeIngestJob(
                id="kjob_e2e_succeeded",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                filename="e2e-knowledge.md",
                status="succeeded",
                stage="done",
                progress=1,
            )
        )
        db.add(
            KnowledgeIngestJob(
                id="kjob_e2e_member_succeeded",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                document_id="kdoc_e2e_member",
                filename="e2e-member-knowledge.md",
                status="succeeded",
                stage="done",
                progress=1,
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_invalid",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="Invalid E2E skill",
                payload_json={
                    "skill_id": "invalid_e2e_skill",
                    "name": "Invalid E2E skill",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_handled",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="Handled E2E skill",
                status="confirmed",
                payload_json={
                    "skill_id": "handled_e2e_skill",
                    "name": "Handled E2E skill",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_ui",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="浏览器确认技能",
                payload_json={
                    "skill_id": "browser_confirmed_skill",
                    "name": "浏览器确认技能",
                    "nodes": [
                        {
                            "node_id": "finish",
                            "name": "完成",
                            "instruction": "完成浏览器确认回归。",
                        }
                    ],
                    "start_node_id": "finish",
                    "terminal_node_ids": ["finish"],
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_tool_ui",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="tool",
                title="浏览器确认工具",
                payload_json={
                    "name": "browser.confirmed.tool",
                    "display_name": "浏览器确认工具",
                    "method": "POST",
                    "url": "/api/mock/browser-confirmed-tool",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_reject_skill",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="浏览器拒绝技能",
                payload_json={
                    "skill_id": "browser_rejected_skill",
                    "name": "浏览器拒绝技能",
                    "nodes": [
                        {
                            "node_id": "finish",
                            "name": "完成",
                            "instruction": "该建议应被拒绝。",
                        }
                    ],
                    "start_node_id": "finish",
                    "terminal_node_ids": ["finish"],
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_reject_tool",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="tool",
                title="浏览器拒绝工具",
                payload_json={
                    "name": "browser.rejected.tool",
                    "display_name": "浏览器拒绝工具",
                    "method": "POST",
                    "url": "/api/mock/browser-rejected-tool",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_double_click",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="tool",
                title="浏览器防重复工具",
                payload_json={
                    "name": "browser.single.submit.tool",
                    "display_name": "浏览器防重复工具",
                    "method": "POST",
                    "url": "/api/mock/browser-single-submit-tool",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_retry_tool",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="tool",
                title="浏览器重试工具",
                payload_json={
                    "name": "browser.retry.tool",
                    "display_name": "浏览器重试工具",
                    "method": "POST",
                    "url": "/api/mock/browser-retry-tool",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_concurrent",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="浏览器并发确认技能",
                payload_json={
                    "skill_id": "browser_concurrent_skill",
                    "name": "浏览器并发确认技能",
                    "nodes": [
                        {
                            "node_id": "finish",
                            "name": "完成",
                            "instruction": "并发请求只能创建一次。",
                        }
                    ],
                    "start_node_id": "finish",
                    "terminal_node_ids": ["finish"],
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_forbidden_confirm",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="skill",
                title="成员不可确认技能",
                payload_json={
                    "skill_id": "member_forbidden_skill",
                    "name": "成员不可确认技能",
                    "nodes": [
                        {
                            "node_id": "finish",
                            "name": "完成",
                            "instruction": "普通成员不应创建该资源。",
                        }
                    ],
                    "start_node_id": "finish",
                    "terminal_node_ids": ["finish"],
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_forbidden_reject",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e",
                knowledge_base_version_id="kbver_kb_e2e_1_0_0",
                document_id="kdoc_e2e",
                suggestion_type="tool",
                title="成员不可拒绝工具",
                payload_json={
                    "name": "member.forbidden.tool",
                    "display_name": "成员不可拒绝工具",
                    "method": "POST",
                    "url": "/api/mock/member-forbidden-tool",
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_member_confirm",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                document_id="kdoc_e2e_member",
                suggestion_type="skill",
                title="成员确认自己的技能",
                payload_json={
                    "skill_id": "member_owned_skill",
                    "name": "成员确认自己的技能",
                    "nodes": [
                        {
                            "node_id": "finish",
                            "name": "完成",
                            "instruction": "员工所有者可以确认该建议。",
                        }
                    ],
                    "start_node_id": "finish",
                    "terminal_node_ids": ["finish"],
                },
            )
        )
        db.add(
            KnowledgeDiscoverySuggestion(
                id="kdisc_e2e_member_reject",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                document_id="kdoc_e2e_member",
                suggestion_type="tool",
                title="成员拒绝自己的工具",
                payload_json={
                    "name": "member.owned.rejected.tool",
                    "display_name": "成员拒绝自己的工具",
                    "method": "POST",
                    "url": "/api/mock/member-owned-rejected-tool",
                },
            )
        )
        knowledge_base = db.get(KnowledgeBase, "kb_e2e")
        if knowledge_base is None:
            raise RuntimeError("E2E knowledge base fixture was not created")
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            "agent_e2e_employee",
            knowledge_base,
        )
        member_knowledge_base = db.get(KnowledgeBase, "kb_e2e_member")
        if member_knowledge_base is None:
            raise RuntimeError("Member E2E knowledge base fixture was not created")
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            "agent_e2e_member_employee",
            member_knowledge_base,
        )
        db.commit()

        role = BusinessRole(
            id="role_e2e_admin_process",
            tenant_id="tenant_demo",
            role_code="e2e.admin.process",
            name="E2E 行政流程处理人",
        )
        profile = EmployeeProfile(
            id="profile_e2e_member",
            tenant_id="tenant_demo",
            user_id="member_e2e",
            employee_id="E2E-MEMBER",
        )
        other_profile = EmployeeProfile(
            id="profile_e2e_other_member",
            tenant_id="tenant_demo",
            user_id="other_member_e2e",
            employee_id="E2E-OTHER-MEMBER",
        )
        member_two_profile = EmployeeProfile(
            id="profile_e2e_member_two",
            tenant_id="tenant_demo",
            user_id="member_two_e2e",
            employee_id="E2E-MEMBER-TWO",
        )
        finance_profile = EmployeeProfile(
            id="profile_e2e_finance",
            tenant_id="tenant_demo",
            user_id="finance_e2e",
            employee_id="E2E-FINANCE",
        )
        requestor_profile = EmployeeProfile(
            id="profile_e2e_requestor",
            tenant_id="tenant_demo",
            user_id="requestor_e2e",
            employee_id="E2E-REQUESTOR",
            employee_name="E2E Requestor",
        )
        db.add(role)
        db.add(profile)
        db.add(other_profile)
        db.add(member_two_profile)
        db.add(finance_profile)
        db.add(requestor_profile)
        db.flush()
        root = ensure_organization_foundation(db, "tenant_demo")
        scoped_branch = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="E2E_SCOPED_BRANCH",
            name="E2E 授权分部",
            unit_type_code="department",
        )
        scoped_child = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=scoped_branch.id,
            code="E2E_SCOPED_CHILD",
            name="E2E 授权分部下级",
            unit_type_code="department",
        )
        sibling_branch = create_organization_unit(
            db,
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="E2E_SIBLING_BRANCH",
            name="E2E 兄弟分部",
            unit_type_code="department",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=profile.id,
            org_unit_id=root.id,
            assignment_type="primary",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=other_profile.id,
            org_unit_id=root.id,
            assignment_type="primary",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=member_two_profile.id,
            org_unit_id=scoped_child.id,
            assignment_type="primary",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=finance_profile.id,
            org_unit_id=sibling_branch.id,
            assignment_type="primary",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=requestor_profile.id,
            org_unit_id=scoped_branch.id,
            assignment_type="primary",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=profile.id,
            org_unit_id=scoped_branch.id,
            assignment_type="concurrent",
        )
        assign_member_to_organization(
            db,
            tenant_id="tenant_demo",
            employee_profile_id=other_profile.id,
            org_unit_id=sibling_branch.id,
            assignment_type="concurrent",
        )
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=profile.id,
                business_role_id=role.id,
            )
        )
        expense_roles = {
            expense_role.role_code: expense_role
            for expense_role in db.exec(
                select(BusinessRole).where(
                    BusinessRole.role_code.in_(
                        (
                            EXPENSE_DEPARTMENT_APPROVER_ROLE,
                            EXPENSE_FINANCE_APPROVER_ROLE,
                        )
                    )
                )
            ).all()
        }
        for employee_profile_id in (
            profile.id,
            member_two_profile.id,
            other_profile.id,
        ):
            db.add(
                EmployeeRoleAssignment(
                    tenant_id="tenant_demo",
                    employee_profile_id=employee_profile_id,
                    business_role_id=expense_roles[EXPENSE_DEPARTMENT_APPROVER_ROLE].id,
                )
            )
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=finance_profile.id,
                business_role_id=expense_roles[EXPENSE_FINANCE_APPROVER_ROLE].id,
            )
        )
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=other_profile.id,
                business_role_id=role.id,
            )
        )
        instance = SopInstance(
            id="instance_e2e_admin_process",
            tenant_id="tenant_demo",
            session_id="session_e2e_admin_process",
            skill_id="m0_admin_process",
            skill_version_id="version_e2e_admin_process",
            skill_version="1.0.0",
            definition_checksum="e" * 64,
            status="running",
            current_node_id="administrative_review",
        )
        execution = SopNodeExecution(
            id="execution_e2e_admin_process",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_id="administrative_review",
            status="running",
        )
        db.add(instance)
        db.add(execution)
        db.commit()
        SopWorkItemService(db).offer(
            instance,
            execution,
            HumanTaskConfig(
                kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                capability="human.structured_work_item",
                candidate_role_codes=(role.role_code,),
                participant_scope_resolver=ParticipantScopeResolver.EXPLICIT_ORG,
                participant_scope_org_unit_id=scoped_branch.id,
                completion_policy=WorkItemCompletionPolicy(
                    mode=CompletionMode.ANY,
                    claim_required=True,
                ),
            ),
            initiator_user_id="requestor_e2e",
        )
        legacy_content = {
            "skill_id": "m0_legacy_tenant_process",
            "name": "M0 租户级兼容任务",
            "version": "1.0.0",
            "execution_mode": "deterministic",
            "condition_schemas": {
                "work_item": {
                    "type": "object",
                    "properties": {"outcome": {"type": "string"}},
                }
            },
            "nodes": [
                {
                    "node_id": "legacy_review",
                    "type": "human_task",
                    "name": "租户级兼容审批",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": [role.role_code],
                            "completion_mode": "any",
                            "claim_required": True,
                            "allowed_outcomes": ["approved", "rejected"],
                        }
                    },
                },
                {"node_id": "approved", "type": "terminal", "name": "审批通过"},
                {"node_id": "rejected", "type": "terminal", "name": "审批拒绝"},
            ],
            "edges": [
                {
                    "source_node_id": "legacy_review",
                    "next_node_id": "approved",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "approved"},
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "legacy_review",
                    "next_node_id": "rejected",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
            ],
            "start_node_id": "legacy_review",
            "terminal_node_ids": ["approved", "rejected"],
        }
        legacy_definition = compile_legacy_skill_card(legacy_content)
        legacy_version = SkillVersion(
            id="version_e2e_legacy_tenant_process",
            tenant_id="tenant_demo",
            skill_id="m0_legacy_tenant_process",
            version="1.0.0",
            name="M0 租户级兼容任务",
            content_json=legacy_content,
            status="published",
            compiled_definition_checksum=legacy_definition.checksum,
        )
        legacy_session = ChatSession(
            id="session_e2e_legacy_tenant_process",
            tenant_id="tenant_demo",
            user_id="requestor_e2e",
            active_skill_id="m0_legacy_tenant_process",
            active_step_id="legacy_review",
        )
        legacy_instance = SopInstance(
            id="instance_e2e_legacy_tenant_process",
            tenant_id="tenant_demo",
            session_id="session_e2e_legacy_tenant_process",
            skill_id="m0_legacy_tenant_process",
            skill_version_id="version_e2e_legacy_tenant_process",
            skill_version="1.0.0",
            definition_checksum=legacy_definition.checksum,
            status="running",
            current_node_id="legacy_review",
        )
        legacy_execution = SopNodeExecution(
            id="execution_e2e_legacy_tenant_process",
            tenant_id="tenant_demo",
            instance_id=legacy_instance.id,
            node_id="legacy_review",
            status="running",
        )
        db.add(legacy_version)
        db.add(legacy_session)
        db.add(legacy_instance)
        db.add(legacy_execution)
        db.flush()
        SopWorkItemService(db).offer(
            legacy_instance,
            legacy_execution,
            HumanTaskConfig(
                kind=HumanTaskKind.STRUCTURED_WORK_ITEM,
                capability="human.structured_work_item",
                candidate_role_codes=(role.role_code,),
                completion_policy=WorkItemCompletionPolicy(
                    mode=CompletionMode.ANY,
                    claim_required=True,
                ),
            ),
            initiator_user_id="requestor_e2e",
        )
        expense_skill = db.exec(
            select(Skill).where(Skill.skill_id == "expense_over_limit_approval")
        ).one()
        expense_version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == expense_skill.skill_id,
                SkillVersion.version == EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
            )
        ).one()
        expense_definition = compile_legacy_skill_card(expense_version.content_json)
        expense_agent = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "财务",
            )
        ).one()
        expense_request = ApprovalRequestService(db).create_expense_special_approval(
            tenant_id="tenant_demo",
            actor_user_id="requestor_e2e",
            payload={
                "employee_id": requestor_profile.employee_id,
                "employee_name": requestor_profile.employee_name,
                "expense_category": "差旅住宿",
                "original_limit": 1000,
                "claimed_amount": 1300,
                "over_limit_reason": "M3-C 真实浏览器组织范围验证",
            },
        )
        expense_session = ChatSession(
            id="session_e2e_expense_org_scope",
            tenant_id="tenant_demo",
            user_id="requestor_e2e",
            agent_id=expense_agent.id,
            active_skill_id=expense_skill.skill_id,
            active_step_id="department_special_approval",
        )
        expense_instance = SopInstance(
            id="instance_e2e_expense_org_scope",
            tenant_id="tenant_demo",
            session_id=expense_session.id,
            skill_id=expense_skill.skill_id,
            skill_version_id=expense_version.id,
            skill_version=expense_version.version,
            definition_checksum=expense_definition.checksum,
            status="waiting",
            current_node_id="department_special_approval",
            context_json={
                "tool_results": {
                    "special_application": {
                        "status": "succeeded",
                        "data": expense_request,
                    }
                }
            },
        )
        create_execution = SopNodeExecution(
            id="execution_e2e_expense_create",
            tenant_id="tenant_demo",
            instance_id=expense_instance.id,
            node_id="create_special_application",
            status="succeeded",
        )
        department_execution = SopNodeExecution(
            id="execution_e2e_expense_department",
            tenant_id="tenant_demo",
            instance_id=expense_instance.id,
            node_id="department_special_approval",
            status="waiting",
        )
        db.add(expense_session)
        db.add(expense_instance)
        db.add(create_execution)
        db.add(department_execution)
        db.flush()
        db.add(
            SopOperation(
                id="operation_e2e_expense_create",
                tenant_id="tenant_demo",
                instance_id=expense_instance.id,
                node_execution_id=create_execution.id,
                operation_name="expense.special_approval_create",
                idempotency_key="e2e-expense-special-create",
                status="succeeded",
                result_json=expense_request,
            )
        )
        department_node = next(
            node
            for node in expense_definition.nodes
            if node.node_id == "department_special_approval"
        )
        SopWorkItemService(db).offer(
            expense_instance,
            department_execution,
            department_node.config,
            initiator_user_id="requestor_e2e",
        )
        db.commit()


def seed_large_organization_browser_fixture() -> None:
    """写入隔离的大组织夹具，并把一个匿名账号设置为浏览器管理员。"""

    from sqlmodel import Session

    from app.db import engine
    from app.db.models import User
    from tests.organization_large_fixture import seed_large_organization_fixture

    with Session(engine) as db:
        seed_large_organization_fixture(db)
        admin = db.get(User, "user_scale_00000")
        if admin is None:
            raise RuntimeError(
                "Large organization browser administrator was not created"
            )
        admin.role = "admin"
        admin.password_hash = legacy_password_hash("scale-admin")
        db.add(admin)
        member = db.get(User, "user_scale_00001")
        if member is None:
            raise RuntimeError("Large organization browser member was not created")
        member.password_hash = legacy_password_hash("scale-member")
        db.add(member)
        db.commit()


def seed_pagination_browser_fixtures() -> None:
    """为员工广场、任务箱、档案日志、记忆和定时任务写入跨页数据。"""

    from datetime import timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.db.models import (
        AgentProfile,
        ChatSession,
        MemoryRecord,
        ScheduledTask,
        ScheduledTaskRun,
        SopWorkItem,
    )
    from app.db.models import utc_now

    now = utc_now()
    agent_id = "agent_e2e_employee"
    with Session(engine) as db:
        for index in range(13):
            db.add(
                AgentProfile(
                    id=f"agent_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    name=f"浏览器分页员工 {index:02d}",
                    owner_user_id="admin",
                    status="active",
                    updated_at=now + timedelta(seconds=index),
                    metadata_json={"owner_user_id": "admin", "owner_username": "admin"},
                )
            )
        for index in range(11):
            task_id = f"sched_e2e_page_{index:02d}"
            scheduled_for = now + timedelta(minutes=index)
            db.add(
                ScheduledTask(
                    id=task_id,
                    tenant_id="tenant_demo",
                    agent_id=agent_id,
                    created_by_user_id="admin",
                    title=f"浏览器分页定时任务 {index:02d}",
                    prompt="执行分页回归任务",
                    schedule_json={"hour": 9, "minute": 0},
                    next_run_at=scheduled_for,
                    updated_at=scheduled_for,
                )
            )
            db.add(
                ScheduledTaskRun(
                    id=f"schedrun_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    scheduled_task_id=task_id,
                    agent_id=agent_id,
                    user_id="admin",
                    scheduled_for=scheduled_for,
                    status="completed",
                    result_summary=f"浏览器分页执行结果 {index:02d}",
                )
            )
            db.add(
                ChatSession(
                    id=f"session_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    user_id="admin",
                    agent_id=agent_id,
                    title=f"浏览器分页对话 {index:02d}",
                    updated_at=now + timedelta(minutes=index),
                )
            )
            db.add(
                MemoryRecord(
                    id=f"memory_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    agent_id=agent_id,
                    user_id=f"memory_user_{index:02d}",
                    username=f"分页记忆用户 {index:02d}",
                    kind="fact",
                    content=f"浏览器分页记忆内容 {index:02d}",
                    updated_at=now + timedelta(minutes=index),
                )
            )
        for index in range(21):
            db.add(
                SopWorkItem(
                    id=f"sopwork_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    instance_id="instance_e2e_expense_org_scope",
                    node_execution_id=f"execution_e2e_page_{index:02d}",
                    skill_version_id="skillver_e2e_expense_org_scope",
                    node_id=f"浏览器分页节点_{index:02d}",
                    status="offered",
                    owner_user_id="admin",
                    initiator_user_id="requestor_e2e",
                    created_at=now + timedelta(minutes=index),
                    updated_at=now + timedelta(minutes=index),
                )
            )
        db.commit()


def main() -> None:
    """启动临时全栈服务，并确保普通与大组织浏览器夹具都可用。"""

    shutil.rmtree(E2E_RUNTIME_DIR, ignore_errors=True)
    E2E_RUNTIME_DIR.mkdir(mode=0o700)
    configure_environment(E2E_RUNTIME_DIR / "e2e.sqlite3")
    seed_e2e_fixtures()
    seed_pagination_browser_fixtures()
    seed_large_organization_browser_fixture()

    import uvicorn
    from single_port_app import app

    uvicorn.run(app, host="127.0.0.1", port=E2E_PORT, log_level="warning")


if __name__ == "__main__":
    main()
