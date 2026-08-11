"""
@Time       : 2026/08/12
@Author     : zhanglp8181
@File       : start_fullstack_server.py
@CallChain  : Playwright fullstack 配置 → 临时 SQLite → FastAPI 单端口应用
@Description: 启动隔离真实全栈服务，并准备登录、流程、动态任务、Artifact 和分页浏览器数据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


FRONTEND_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = FRONTEND_DIR.parent / "backend"
E2E_PORT = 5148
E2E_SECRET = "fullstack-e2e-isolated-secret-at-least-32-bytes"
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
            "DYNAMIC_TASK_EXECUTION_ENABLED": "true",
            "DYNAMIC_TASK_TENANT_ALLOWLIST": "*",
            "DYNAMIC_TASK_AGENT_ALLOWLIST": "*",
            "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD": "100",
            "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD": "1",
            "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD": "1",
            "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD": "10",
            "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS": "3600",
            "DYNAMIC_TASK_MAX_ACTIVE_PER_TENANT": "16",
            "DYNAMIC_TASK_MAX_ACTIVE_PER_AGENT": "8",
            "DYNAMIC_TASK_MAX_ACTIVE_PER_USER": "4",
            "DYNAMIC_TASK_MAX_ACTIVE_PER_TOOL": "4",
            "DYNAMIC_TASK_MANAGED_WORKSPACE_ENABLED": "true",
            "DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT": str(
                E2E_RUNTIME_DIR / "managed-workspaces"
            ),
            "GENERAL_SKILL_IMPORT_V2_ENABLED": "true",
            "GENERAL_SKILL_IMPORT_ASYNC_ENABLED": "true",
            "GENERAL_SKILL_IMPORT_WORKER_POLL_SECONDS": "0.2",
            "GENERAL_SKILL_IMPORT_WORKER_LEASE_SECONDS": "300",
            "GENERAL_SKILL_OBJECT_STORE_PATH": str(E2E_RUNTIME_DIR / "general-skill-objects"),
            "GENERAL_SKILL_RESOLVER_V2_ENABLED": "true",
            "GENERAL_SKILL_DYNAMIC_GUIDANCE_ENABLED": "true",
            "GONGGE_XUBAN_DATA_DIR": str(E2E_RUNTIME_DIR / "user-data"),
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
                owner_user_id="admin",
                metadata_json={"owner_user_id": "admin", "owner_username": "admin"},
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_member_employee",
                tenant_id="tenant_demo",
                name="E2E 成员数字员工",
                status="active",
                owner_user_id="member_e2e",
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
                owner_user_id="admin",
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
                owner_user_id="member_e2e",
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
            employee_name="E2E Member",
        )
        other_profile = EmployeeProfile(
            id="profile_e2e_other_member",
            tenant_id="tenant_demo",
            user_id="other_member_e2e",
            employee_id="E2E-OTHER-MEMBER",
            employee_name="E2E Other Member",
        )
        member_two_profile = EmployeeProfile(
            id="profile_e2e_member_two",
            tenant_id="tenant_demo",
            user_id="member_two_e2e",
            employee_id="E2E-MEMBER-TWO",
            employee_name="E2E Member Two",
        )
        finance_profile = EmployeeProfile(
            id="profile_e2e_finance",
            tenant_id="tenant_demo",
            user_id="finance_e2e",
            employee_id="E2E-FINANCE",
            employee_name="E2E Finance",
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
        admin_profile = db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == "tenant_demo",
                EmployeeProfile.user_id == "admin",
            )
        ).one()
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
            employee_profile_id=admin_profile.id,
            org_unit_id=root.id,
            assignment_type="primary",
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
            active_slot_key="foreground:session_e2e_admin_process",
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
            step_key="administrative_review",
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
            active_slot_key="foreground:session_e2e_legacy_tenant_process",
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
            step_key="legacy_review",
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
            active_slot_key=f"foreground:{expense_session.id}",
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
            step_key="create_special_application",
            status="succeeded",
        )
        department_execution = SopNodeExecution(
            id="execution_e2e_expense_department",
            tenant_id="tenant_demo",
            instance_id=expense_instance.id,
            node_id="department_special_approval",
            step_key="department_special_approval",
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
                logical_action_id="e2e-expense-special-create",
                request_fingerprint=(
                    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
                ),
                effect_kind="external_write",
                effect_state="complete",
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


def seed_dynamic_task_browser_fixtures() -> None:
    """准备普通消息、可信 Artifact 和可办理澄清的生产同形动态 Execution。"""

    from sqlmodel import Session

    from app.db import engine
    from app.db.models import (
        ChatSession,
        ExecutionPlanRevision,
        ExecutionPublication,
        ExecutionResult,
        Message,
        SopInstance,
        SopNodeExecution,
        SopWorkItem,
        SopWorkItemCandidate,
        utc_now,
    )
    from app.dynamic_tasks.artifacts import ArtifactService
    from app.dynamic_tasks.capability_catalog import capability_checksum
    from app.dynamic_tasks.planning import canonical_checksum
    from app.sop_runtime.execution_control import attention_identity

    tenant_id = "tenant_demo"
    capability = {"model": {"id": "model_e2e_dynamic", "status": "ready"}}
    capability_digest = capability_checksum(capability)
    now = utc_now()
    artifact_content = "# 浏览器续约风险简报\n\n合同证据、风险项和处理建议均已核验。"
    artifact_plan = {
        "goal": "生成浏览器续约风险简报",
        "success_criteria": ["风险简报可下载且输入证据完整"],
        "steps": [{
            "step_key": "answer",
            "title": "生成风险简报",
            "kind": "answer",
            "required": True,
            "depends_on": [],
        }],
        "expected_artifacts": [{
            "artifact_key": "renewal_risk_brief",
            "filename": "浏览器续约风险简报.md",
            "mime_type": "text/markdown",
            "content_source": "result.markdown",
            "required": True,
        }],
        "budget": {"max_model_calls": 6, "max_steps": 4},
    }
    artifact_checksum = canonical_checksum(artifact_plan)

    with Session(engine, expire_on_commit=False) as db:
        session = ChatSession(
            id="session_e2e_dynamic_artifact",
            tenant_id=tenant_id,
            user_id="admin",
            agent_id="agent_e2e_employee",
            agent_profile_revision=1,
            title="浏览器动态任务与交付物",
            summary="续约风险简报已完成",
            status="active",
        )
        instance = SopInstance(
            id="execution_e2e_dynamic_artifact",
            tenant_id=tenant_id,
            session_id=session.id,
            kind="dynamic_task",
            initiator_user_id="admin",
            agent_id="agent_e2e_employee",
            goal_snapshot_json={"goal": artifact_plan["goal"]},
            current_plan_revision_id="plan_e2e_dynamic_artifact",
            current_plan_checksum=artifact_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            budget_snapshot_json=dict(artifact_plan["budget"]),
            context_json={"dynamic_budget_usage": {"model_calls": 2}},
            current_result_id="result_e2e_dynamic_artifact",
            status="succeeded",
            revision=8,
            started_at=now,
            completed_at=now,
        )
        plan = ExecutionPlanRevision(
            id=instance.current_plan_revision_id,
            tenant_id=tenant_id,
            execution_id=instance.id,
            revision_number=1,
            reason="initial",
            status="active",
            plan_json=artifact_plan,
            checksum=artifact_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            activated_at=now,
        )
        node = SopNodeExecution(
            id="node_e2e_dynamic_artifact",
            tenant_id=tenant_id,
            instance_id=instance.id,
            node_id="answer",
            step_key="answer",
            plan_revision_id=plan.id,
            step_kind="answer",
            title="生成风险简报",
            status="succeeded",
            started_at=now,
            completed_at=now,
        )
        result_payload = {
            "markdown": artifact_content,
            "criterion_evidence": {"criterion_01": ["answer"]},
            "pending_questions": [],
        }
        result = ExecutionResult(
            id=instance.current_result_id,
            tenant_id=tenant_id,
            execution_id=instance.id,
            status="verified",
            result_json=result_payload,
            verification_json={"passed": True},
            checksum=canonical_checksum(result_payload),
            created_by_step_key="answer",
        )
        message = Message(
            id="message_e2e_dynamic_artifact",
            tenant_id=tenant_id,
            session_id=session.id,
            role="assistant",
            content=artifact_content,
            metadata_json={"execution_id": instance.id, "result_id": result.id},
        )
        db.add(session)
        db.add(Message(
            id="message_e2e_ordinary_answer",
            tenant_id=tenant_id,
            session_id=session.id,
            role="assistant",
            content="普通问答仍可正常展示，不会创建新的动态执行。",
        ))
        db.add(instance)
        db.add(plan)
        db.add(node)
        db.add(result)
        db.add(message)
        db.flush()
        artifact, _ = ArtifactService(db).register(
            instance=instance,
            source_node=node,
            artifact_key="renewal_risk_brief",
            filename="浏览器续约风险简报.md",
            mime_type="text/markdown",
            data=artifact_content.encode("utf-8"),
        )
        result.verification_json = {"passed": True, "artifact_ids": [artifact.id]}
        message.metadata_json = {
            "execution_id": instance.id,
            "result_id": result.id,
            "artifact_ids": [artifact.id],
        }
        db.add(result)
        db.add(message)
        db.add(ExecutionPublication(
            id="publication_e2e_dynamic_artifact",
            tenant_id=tenant_id,
            execution_id=instance.id,
            result_id=result.id,
            publication_key=canonical_checksum({
                "execution_id": instance.id,
                "target_type": "application",
            }),
            target_type="application",
            target_ref=session.id,
            required=True,
            status="settled",
            receipt_json={"message_id": message.id},
            settled_at=now,
        ))

        waiting_plan = {
            "goal": "补充合作方后继续合同核验",
            "success_criteria": ["明确合作方并完成核验"],
            "steps": [
                {
                    "step_key": "clarify_partner",
                    "title": "确认合作方",
                    "kind": "clarification",
                    "required": True,
                    "depends_on": [],
                },
                {
                    "step_key": "answer",
                    "title": "完成合同核验",
                    "kind": "answer",
                    "required": True,
                    "depends_on": ["clarify_partner"],
                },
            ],
            "expected_artifacts": [],
            "budget": {"max_model_calls": 6, "max_steps": 4},
        }
        waiting_checksum = canonical_checksum(waiting_plan)
        waiting = SopInstance(
            id="execution_e2e_dynamic_attention",
            tenant_id=tenant_id,
            session_id="session_e2e_dynamic_attention",
            kind="dynamic_task",
            active_slot_key="dynamic:e2e-attention",
            initiator_user_id="admin",
            agent_id="agent_e2e_employee",
            goal_snapshot_json={"goal": waiting_plan["goal"]},
            current_plan_revision_id="plan_e2e_dynamic_attention",
            current_plan_checksum=waiting_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            budget_snapshot_json=dict(waiting_plan["budget"]),
            status="waiting",
            revision=4,
            started_at=now,
        )
        waiting_node = SopNodeExecution(
            id="node_e2e_dynamic_attention",
            tenant_id=tenant_id,
            instance_id=waiting.id,
            node_id="clarify_partner",
            step_key="clarify_partner",
            plan_revision_id=waiting.current_plan_revision_id,
            step_kind="clarification",
            title="确认合作方",
            status="waiting",
            started_at=now,
        )
        attention = SopWorkItem(
            id="attention_e2e_dynamic_clarification",
            tenant_id=tenant_id,
            instance_id=waiting.id,
            node_execution_id=waiting_node.id,
            attention_kind="clarification",
            attention_key="clarify_partner",
            attention_identity=attention_identity(
                tenant_id=tenant_id,
                execution_id=waiting.id,
                attention_key="clarify_partner",
            ),
            title="确认需要核验的合作方",
            payload_json={
                "question": "请选择需要核验的合作方",
                "options": ["星海科技", "云帆数据"],
            },
            allowed_commands_json=["answer", "cancel"],
            allowed_outcomes_json=["answer", "cancel"],
            status="offered",
            initiator_user_id="admin",
            exclude_initiator=False,
        )
        db.add(ChatSession(
            id=waiting.session_id,
            tenant_id=tenant_id,
            user_id="admin",
            agent_id="agent_e2e_employee",
            title="浏览器动态澄清",
            status="waiting",
        ))
        db.add(waiting)
        db.add(ExecutionPlanRevision(
            id=waiting.current_plan_revision_id,
            tenant_id=tenant_id,
            execution_id=waiting.id,
            revision_number=1,
            reason="initial",
            status="active",
            plan_json=waiting_plan,
            checksum=waiting_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            activated_at=now,
        ))
        db.add(waiting_node)
        db.add(attention)
        db.add(SopWorkItemCandidate(
            tenant_id=tenant_id,
            work_item_id=attention.id,
            user_id="admin",
            source_role_codes_json=["initiator"],
            source_types_json=["execution_initiator"],
        ))
        db.commit()


def seed_managed_workspace_browser_fixture() -> None:
    """创建真实 Git 演示仓库并把四个固定受管工具只绑定给成员数字员工。"""

    from sqlmodel import Session

    from app.agents.branching import ensure_private_resource_binding
    from app.db import engine
    from app.db.models import Tool
    from app.dynamic_tasks.capability_catalog import (
        ToolReliabilityContract,
        publish_tool_contract,
    )

    repo = E2E_RUNTIME_DIR / "managed-workspaces" / "tenant_demo" / "refund-demo"
    repo.mkdir(parents=True)
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "robot@example.invalid"),
        ("config", "user.name", "E2E Workspace Robot"),
    ):
        subprocess.run(["git", "-C", str(repo), *argv], check=True)
    (repo / "refund.py").write_text("STATUS = 'pending'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_refund.py").write_text(
        "import unittest\n"
        "from refund import STATUS\n\n"
        "class RefundApprovalTest(unittest.TestCase):\n"
        "    def test_high_refund_requires_approval(self):\n"
        "        self.assertEqual(STATUS, 'approval_required')\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "refund.py", "tests/test_refund.py"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "baseline"], check=True
    )
    image = (
        "python@sha256:"
        "9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4"
    )
    definitions = (
        (
            "workspace.refund.read",
            "read_file",
            "read",
            {"path": {"type": "string"}},
            {"content": {"type": "string"}, "sha256": {"type": "string"}},
            ["input.path", "output.content", "output.sha256"],
        ),
        (
            "workspace.refund.apply",
            "apply_file",
            "local_write",
            {
                "path": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "content": {"type": "string"},
            },
            {"sha256": {"type": "string"}, "branch": {"type": "string"}},
            [
                "input.path",
                "input.expected_sha256",
                "input.content",
                "output.sha256",
                "output.branch",
            ],
        ),
        (
            "workspace.refund.check",
            "run_check",
            "execute",
            {"profile": {"type": "string"}},
            {
                "profile": {"type": "string"},
                "passed": {"type": "boolean"},
                "exit_code": {"type": "integer"},
            },
            ["input.profile", "output.profile", "output.passed", "output.exit_code"],
        ),
        (
            "workspace.refund.commit",
            "commit",
            "local_write",
            {
                "message": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            {"commit_sha": {"type": "string"}, "branch": {"type": "string"}},
            ["input.message", "input.paths", "output.commit_sha", "output.branch"],
        ),
    )
    with Session(engine) as db:
        for name, handler, risk, input_properties, output_properties, paths in definitions:
            config: dict[str, object] = {
                "workspace_id": "refund-demo",
                "base_ref": "main",
                "handler": handler,
            }
            if handler == "run_check":
                config["check_profiles"] = {
                    "backend-unit": {
                        "image": image,
                        "argv": [
                            "python",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        "timeout_seconds": 60,
                    }
                }
            tool = Tool(
                tenant_id="tenant_demo",
                name=name,
                display_name=name,
                tool_type="managed_workspace",
                method="POST",
                url="",
                config_json=config,
                input_schema={
                    "type": "object",
                    "properties": input_properties,
                    "required": list(input_properties),
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "properties": output_properties},
            )
            publish_tool_contract(
                tool,
                ToolReliabilityContract.model_validate(
                    {
                        "risk_class": risk,
                        "side_effect": "none" if risk == "read" else "local",
                        "confirmation_policy": "none" if risk == "read" else "once",
                        "idempotency": {"mode": "none"},
                        "reconcile": {"supported": False},
                        "model_visibility": {
                            "allowed_paths": paths,
                            "user_display_paths": [
                                path for path in paths if path.startswith("output.")
                            ],
                            "audit_only_paths": [],
                        },
                        "timeout_policy": "failed",
                        "dynamic_task_enabled": True,
                    }
                ),
            )
            db.add(tool)
            db.flush()
            ensure_private_resource_binding(
                db,
                "tenant_demo",
                "agent_e2e_member_employee",
                "tool",
                tool.id,
                "active",
            )
        db.commit()


def seed_schedule_dynamic_model() -> None:
    """为 Schedule 全栈回归配置通过 dynamic-v1 预检的隔离默认模型。"""

    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ModelConfig
    from app.dynamic_tasks.capability_catalog import capability_checksum
    from app.security.encryption import encrypt_secret

    capabilities = {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "structured_output": True,
        "tool_calling": True,
    }
    with Session(engine) as db:
        model = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == "tenant_demo",
                ModelConfig.is_default == True,  # noqa: E712
            )
        ).first()
        if model is None:
            model = ModelConfig(
                id="model_e2e_schedule_dynamic",
                tenant_id="tenant_demo",
                name="E2E Schedule Dynamic Model",
                provider="openai_compatible",
                model="e2e-schedule-model",
                is_default=True,
                enabled=True,
            )
        model.api_key_encrypted = encrypt_secret("e2e-schedule-model-key")
        model.capability_snapshot_json = capabilities
        model.capability_checksum = capability_checksum(capabilities)
        model.preflight_status = "ready"
        model.enabled = True
        db.add(model)
        db.commit()


def install_schedule_llm_override() -> None:
    """仅替换隔离 provider 响应，保留 Router、AgentLoop、Planner、Signal 和 Runtime 真链路。"""

    from app.llm.client import LLMClient
    from app.llm.stage_protocol import STAGE_PROTOCOL_KEY

    original_generate_text = LLMClient.generate_text

    def deterministic_json(
        client: LLMClient,
        system_prompt: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        """按正式阶段协议返回可预测结构，禁止测试直接调用内部 Agent。"""

        stage = user_payload.get(STAGE_PROTOCOL_KEY, {})
        phase = str(stage.get("phase") or "") if isinstance(stage, dict) else ""
        if phase == "Router":
            return {
                "decision": "answer_only",
                "confidence": 0.99,
                "general_intent": "生成需要确认范围的合同巡检结果",
                "reason": "没有匹配正式 SOP，交由非 SOP 能力仲裁",
            }
        if phase == "Router / General Skill Selector":
            if "S3-AUTO" in str(user_payload.get("user_message") or ""):
                return {
                    "use_general_skill": True,
                    "selected_slug": "s3-browser-auto",
                    "use_knowledge": False,
                    "knowledge_query": None,
                    "knowledge_mode": "disabled",
                    "confidence": 0.99,
                    "reason": "自动目录中存在精确匹配的已审核 Skill",
                }
            return {
                "use_general_skill": False,
                "selected_slug": None,
                "use_knowledge": False,
                "knowledge_query": None,
                "knowledge_mode": "disabled",
                "confidence": 0.99,
                "reason": "该任务需要持久执行而非原子技能",
            }
        if phase == "Router / Dynamic Task Shadow":
            goal = str(user_payload.get("user_message") or "生成合同巡检结果")
            if "S3" in goal or "本轮选定的指南" in goal:
                return {
                    "mode": "answer",
                    "goal": None,
                    "success_criteria": [],
                    "requires_durable_execution": False,
                    "requires_artifact": False,
                    "capability_hints": [],
                    "clarification": None,
                    "execution_intent": "none",
                    "confidence": 0.99,
                    "reason": "S3 验证是单轮或跨轮对话 Skill，不需要持久动态执行",
                }
            return {
                "mode": "dynamic_task",
                "goal": goal,
                "success_criteria": ["确认合同范围后生成巡检结果"],
                "requires_durable_execution": True,
                "requires_artifact": False,
                "capability_hints": [],
                "clarification": None,
                "execution_intent": "new_task",
                "confidence": 0.99,
                "reason": "任务需要跨轮等待用户确认",
            }
        if "动态任务指导选择器" in system_prompt:
            names = {
                str(item.get("slug") or "")
                for item in user_payload.get("skill_catalog", [])
                if isinstance(item, dict)
            }
            goal = str(user_payload.get("goal") or "")
            if "S4代码" in goal and "s4-code-guidance" in names:
                selected = ["s4-code-guidance"]
            elif "S4动态" in goal and "s4-dynamic-guidance" in names:
                selected = ["s4-dynamic-guidance"]
            else:
                selected = []
            if "S4-DYNAMIC-FULL-GUIDANCE" in str(user_payload):
                raise RuntimeError("S4 selector received full Skill instructions")
            return {
                "selected_skill_names": selected,
                "reason": "仅为明确的 S4 动态任务选择诊断指导",
            }
        if "受控动态任务规划器" in system_prompt:
            loaded_guidance = user_payload.get("loaded_guidance", [])
            if "S4-CODE-FULL-GUIDANCE" in str(loaded_guidance):
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                required = {
                    "workspace.refund.read",
                    "workspace.refund.apply",
                    "workspace.refund.check",
                    "workspace.refund.commit",
                }
                if not required <= capability_names:
                    raise RuntimeError("S4 code planner did not receive governed workspace tools")
                return {
                    "goal": str(user_payload.get("goal") or "完成 S4 代码交付"),
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["必须先读、审批写入、隔离回归、审批提交"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "read",
                            "title": "读取退款实现",
                            "kind": "tool.read",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["workspace.refund.read"],
                            "guidance_skill_refs": ["s4-code-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "apply",
                            "title": "写入退款审批补丁",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["read"],
                            "capability_refs": ["workspace.refund.apply"],
                            "guidance_skill_refs": ["s4-code-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "check",
                            "title": "运行退款回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["apply"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": ["s4-code-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "commit",
                            "title": "提交一次性任务分支",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["check"],
                            "capability_refs": ["workspace.refund.commit"],
                            "guidance_skill_refs": ["s4-code-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成代码交付报告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["commit"],
                            "capability_refs": [],
                            "guidance_skill_refs": ["s4-code-guidance"],
                            "expected_output_schema": {},
                        },
                    ],
                }
            if isinstance(loaded_guidance, list) and loaded_guidance:
                if "S4-DYNAMIC-FULL-GUIDANCE" not in str(loaded_guidance):
                    raise RuntimeError("S4 planner did not receive fixed Skill instructions")
                knowledge_names = [
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict) and item.get("name") == "knowledge.search"
                ]
                if knowledge_names != ["knowledge.search"]:
                    raise RuntimeError("S4 planner did not receive governed knowledge capability")
                return {
                    "goal": str(user_payload.get("goal") or "完成 S4 动态核验"),
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["必须按固定 Skill 先确认范围再检索证据"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "clarify_scope",
                            "title": "确认诊断范围",
                            "kind": "clarification",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": ["s4-dynamic-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "search_evidence",
                            "title": "检索固定知识证据",
                            "kind": "knowledge",
                            "required": True,
                            "depends_on": ["clarify_scope"],
                            "capability_refs": ["knowledge.search"],
                            "guidance_skill_refs": ["s4-dynamic-guidance"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成可审计结论",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["search_evidence"],
                            "capability_refs": [],
                            "guidance_skill_refs": ["s4-dynamic-guidance"],
                            "expected_output_schema": {},
                        },
                    ],
                }
            return {
                "goal": str(user_payload.get("goal") or "生成合同巡检结果"),
                "success_criteria": user_payload.get("success_criteria", []),
                "constraints": [],
                "assumptions": [],
                "steps": [
                    {
                        "draft_id": "clarify_scope",
                        "title": "确认合同范围",
                        "kind": "clarification",
                        "required": True,
                        "depends_on": [],
                        "capability_refs": [],
                        "guidance_skill_refs": [],
                        "expected_output_schema": {},
                    },
                    {
                        "draft_id": "answer",
                        "title": "生成巡检结果",
                        "kind": "answer",
                        "required": True,
                        "depends_on": ["clarify_scope"],
                        "capability_refs": [],
                        "guidance_skill_refs": [],
                        "expected_output_schema": {},
                    },
                ],
            }
        if "受控单步动作提议器" in system_prompt:
            current_step = user_payload.get("current_step", {})
            step_kind = str(current_step.get("kind") or "") if isinstance(current_step, dict) else ""
            step_title = str(current_step.get("title") or "") if isinstance(current_step, dict) else ""
            is_s4_code = "S4-CODE-FULL-GUIDANCE" in str(user_payload)
            is_s4 = "S4-DYNAMIC-FULL-GUIDANCE" in str(user_payload)
            if is_s4_code:
                client._last_completed_response_metadata = {
                    "response_id": (
                        "e2e-s4-code-"
                        f"{step_kind}-{hashlib.sha256(step_title.encode()).hexdigest()[:12]}"
                    ),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                }
                if step_kind == "tool.read":
                    capability_ref = "workspace.refund.read"
                    arguments = {"path": "refund.py"}
                elif step_kind == "tool.execute":
                    capability_ref = "workspace.refund.check"
                    arguments = {"profile": "backend-unit"}
                elif step_kind == "tool.write" and "提交" in step_title:
                    capability_ref = "workspace.refund.commit"
                    arguments = {
                        "message": "feat: require high refund approval",
                        "paths": ["refund.py"],
                    }
                elif step_kind == "tool.write":
                    capability_ref = "workspace.refund.apply"
                    arguments = {
                        "path": "refund.py",
                        "expected_sha256": hashlib.sha256(
                            b"STATUS = 'pending'\n"
                        ).hexdigest(),
                        "content": "STATUS = 'approval_required'\n",
                    }
                else:
                    execution_view = user_payload.get("provider_execution_view", {})
                    execution_context = (
                        execution_view.get("execution_context", {})
                        if isinstance(execution_view, dict)
                        else {}
                    )
                    completed = [
                        str(item.get("step_key") or "")
                        for item in execution_context.get("completed_steps", [])
                        if isinstance(item, dict) and item.get("step_key")
                    ]
                    criteria = [
                        str(item.get("id") or "")
                        for item in execution_context.get("success_criteria", [])
                        if isinstance(item, dict) and item.get("id")
                    ]
                    return {
                        "action_kind": "answer",
                        "arguments": {
                            "markdown": (
                                "S4-CODE-DELIVERY-SUCCESS：补丁已审批写入，固定容器回归通过，"
                                "并在一次性任务分支形成提交。"
                            ),
                            "criterion_evidence": {
                                criterion: completed for criterion in criteria
                            },
                            "pending_questions": [],
                        },
                        "capability_ref": None,
                        "expected_output_schema": {},
                        "rationale": "依据真实工作区 Operation 回执形成交付报告",
                    }
                return {
                    "action_kind": "call_tool",
                    "arguments": arguments,
                    "capability_ref": capability_ref,
                    "expected_output_schema": {},
                    "rationale": "按固定代码交付 Skill 调用受管能力",
                }
            if is_s4 and step_kind == "knowledge":
                client._last_completed_response_metadata = {
                    "response_id": "e2e-s4-knowledge-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                }
                return {
                    "action_kind": "query_knowledge",
                    "arguments": {"query": "S4 动态任务固定知识证据"},
                    "capability_ref": "knowledge.search",
                    "expected_output_schema": {},
                    "rationale": "按固定 Skill 纪律检索证据",
                }
            if is_s4 and step_kind == "answer":
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                completed = [
                    str(item.get("step_key") or "")
                    for item in execution_context.get("completed_steps", [])
                    if isinstance(item, dict) and item.get("step_key")
                ]
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                client._last_completed_response_metadata = {
                    "response_id": "e2e-s4-answer-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 14, "output_tokens": 12},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": "S4-DYNAMIC-GUIDED-SUCCESS：已确认范围、检索知识并形成可审计结论。",
                        "criterion_evidence": {criterion: completed for criterion in criteria},
                        "pending_questions": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "按固定 Skill 和真实检索证据完成任务",
                }
            client._last_completed_response_metadata = {
                "response_id": "e2e-schedule-clarification-response",
                "finish_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
            return {
                "action_kind": "wait_input",
                "arguments": {
                    "question": "请选择本次需要巡检的合同范围",
                    "options": ["未来30天到期", "未来90天到期"],
                },
                "capability_ref": None,
                "expected_output_schema": {},
                "rationale": "先确认范围再生成可核验结果",
            }
        raise RuntimeError(f"Unhandled E2E model stage: {phase or system_prompt[:40]}")

    LLMClient.generate_json = deterministic_json

    def deterministic_text(
        client: LLMClient,
        system_prompt: str,
        user_payload: dict[str, object] | str,
        response_format: dict[str, str] | None = None,
    ) -> str:
        """只为真实加载 Skill 的回复固定供应商输出，其余场景保持原链路。"""

        if isinstance(user_payload, dict):
            context = user_payload.get("conversation_context")
            loaded = context.get("loaded_general_skills") if isinstance(context, dict) else None
            if isinstance(loaded, list) and loaded:
                combined_instructions = "\n".join(
                    str(item.get("instructions") or "")
                    for item in loaded
                    if isinstance(item, dict)
                )
                if (
                    "S3-COMBO-PARENT" in combined_instructions
                    and "S3-COMBO-CHILD" in combined_instructions
                ):
                    history = json.dumps(context, ensure_ascii=False)
                    if "S3 组合任务第二轮" in str(user_payload.get("user_message") or ""):
                        if "S3-COMBO-FIRST" not in history:
                            raise RuntimeError("S3 combo prior turn was absent from conversation context")
                        return "S3-COMBO-MEMORY：已沿用上一轮 CASE-2026-0813 并再次消费父子固定修订。"
                    return "S3-COMBO-FIRST：CASE-2026-0813 已同时消费父子固定修订。"
                first = loaded[0] if isinstance(loaded[0], dict) else {}
                instructions = str(first.get("instructions") or "")
                if "S3-AUTO-GUIDED" in instructions:
                    return "S3-AUTO-GUIDED：模型从有预算目录自动选择并消费了固定修订。"
                if "只返回 S3-GUIDED-SUCCESS" not in instructions:
                    raise RuntimeError("S3 guidance was not loaded from the fixed revision")
                return "S3-GUIDED-SUCCESS：已按固定修订的售后核验指南完成本轮处理。"
        return original_generate_text(client, system_prompt, user_payload, response_format)

    LLMClient.generate_text = deterministic_text


def seed_connection_browser_fixtures() -> None:
    """准备 Slack 控制面、企业微信消息接入，以及浏览器可办理的 reauth Attention。"""

    import json

    from sqlmodel import Session

    from app.connectors.service import ConnectionService
    from app.connectors.slack import SlackCallResult
    from app.connectors.wecom import WeComCallResult
    from app.db import engine
    from app.db.models import ConnectorInboundEvent
    from app.security.encryption import encrypt_secret
    from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
    from app.sop_runtime.execution_control import ExecutionControlService
    from app.sop_runtime.execution_store import SopExecutionStore

    class SeedSlack:
        """只在夹具构建阶段返回两个稳定 workspace 身份。"""

        def __init__(self) -> None:
            """按建档顺序准备管理与重授权账号。"""

            self.team_ids = iter(("T-E2E-MANAGE", "T-E2E-REAUTH"))

        def auth_test(self, _token: str) -> SlackCallResult:
            """返回下一个测试 workspace 及只读 scope。"""

            return SlackCallResult(
                True,
                {"ok": True, "team_id": next(self.team_ids)},
                granted_scopes=frozenset({"channels:read"}),
            )

        def conversations_info(self, _token: str, *, channel_id: str) -> SlackCallResult:
            """夹具构建不执行频道读取。"""

            return SlackCallResult(True, {"ok": True, "channel": {"id": channel_id}})

    class SeedWeCom:
        """为消息接入浏览器夹具返回稳定自建应用身份。"""

        def application_info(self, **_credentials: str) -> WeComCallResult:
            """返回启用应用和最小只读 scope。"""

            return WeComCallResult(
                True,
                {
                    "agent_id": "1000002",
                    "name": "E2E 企业微信消息",
                    "description": "消息接入浏览器回归",
                    "enabled": True,
                    "home_url": "",
                },
                granted_scopes=frozenset({"application:read"}),
            )

        def invalidate_credentials(self, **_credentials: str) -> None:
            """夹具没有进程 token 缓存。"""

    with Session(engine, expire_on_commit=False) as db:
        service = ConnectionService(db, slack=SeedSlack(), wecom=SeedWeCom())
        manage_profile = service.create_slack_profile(
            tenant_id="tenant_demo",
            display_name="E2E 管理工作区",
            token="xoxb-e2e-manage-seed",
            required_scopes={"channels:read"},
            actor_user_id="admin",
        )
        service.bind_agent(
            tenant_id="tenant_demo",
            profile_id=manage_profile.id,
            agent_id="agent_e2e_employee",
            allowed_scopes={"channels:read"},
            expected_profile_revision=manage_profile.revision,
            actor_user_id="admin",
        )
        wecom_profile = service.create_wecom_profile(
            tenant_id="tenant_demo",
            display_name="E2E 企业微信消息",
            corp_id="ww-e2e-corp",
            agent_id="1000002",
            corp_secret="e2e-wecom-secret",
            callback_token="e2e-callback-token",
            callback_encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            actor_user_id="admin",
        )
        service.bind_agent(
            tenant_id="tenant_demo",
            profile_id=wecom_profile.id,
            agent_id="agent_e2e_employee",
            allowed_scopes={"application:read"},
            expected_profile_revision=wecom_profile.revision,
            actor_user_id="admin",
        )
        plaintext = (
            "<xml><ToUserName>ww-e2e-corp</ToUserName>"
            "<FromUserName>e2e-external-user</FromUserName>"
            "<MsgType>text</MsgType><Content>浏览器消息接入测试</Content>"
            "<AgentID>1000002</AgentID><MsgId>e2e-inbound-message</MsgId></xml>"
        )
        db.add(
            ConnectorInboundEvent(
                id="connin_e2e_pending",
                tenant_id="tenant_demo",
                provider="wecom",
                profile_id=wecom_profile.id,
                external_event_id="e2e-inbound-message",
                payload_checksum=hashlib.sha256(plaintext.encode()).hexdigest(),
                encrypted_payload=encrypt_secret(plaintext),
                event_type="text",
                sender_ref_hash=hashlib.sha256(
                    "ww-e2e-corp\0e2e-external-user".encode()
                ).hexdigest(),
            )
        )
        reauth_profile = service.create_slack_profile(
            tenant_id="tenant_demo",
            display_name="E2E 待重授权工作区",
            token="xoxb-e2e-expired-seed",
            required_scopes={"channels:read"},
            actor_user_id="admin",
        )
        reauth_profile.status = "reauth_required"
        reauth_profile.health_status = "unhealthy"
        reauth_profile.health_error_code = "CONNECTION_TOKEN_EXPIRED"
        reauth_profile.revision += 1
        db.add(reauth_profile)
        db.commit()

        plan = NormalizedPlan(
            goal="浏览器恢复 Slack 只读任务",
            success_criteria=(
                SuccessCriterion(id="channel", type="assertion", spec={"required": True}),
            ),
            steps=(
                PlanStep(
                    step_key="read_channel",
                    title="读取 Slack 频道",
                    kind="tool.read",
                    capability_refs=(f"slack.channel_info@{reauth_profile.id}",),
                ),
            ),
            budget={"max_steps": 2},
        )
        store = SopExecutionStore(db)
        instance = store.start_dynamic_instance(
            tenant_id="tenant_demo",
            session_id="session_e2e_connection_reauth",
            agent_id="agent_e2e_employee",
            initiator_user_id="admin",
            plan=plan,
            capability_snapshot={
                "tools": [],
                "connectors": [
                    {
                        "name": f"slack.channel_info@{reauth_profile.id}",
                        "capability_id": reauth_profile.id,
                    }
                ],
            },
        )[0]
        with store.owned(instance, worker_id="e2e_prepare_reauth"):
            node = store.enter_node(
                instance,
                "read_channel",
                step_key="read_channel",
                plan_revision_id=instance.current_plan_revision_id,
                step_kind="tool.read",
                title="读取 Slack 频道",
            )
            attention, _ = ExecutionControlService(db, store).offer_attention(
                instance,
                attention_kind="reauth",
                attention_key="read_channel:reauth:e2e",
                title="重新授权 E2E 待重授权工作区",
                payload={
                    "provider": "slack",
                    "profile_id": reauth_profile.id,
                    "account_id": reauth_profile.account_id,
                    "secret_revision": reauth_profile.secret_revision,
                    "profile_revision": reauth_profile.revision,
                    "operation_id": "operation_e2e_connection_reauth",
                    "reason_code": "CONNECTION_TOKEN_EXPIRED",
                },
                allowed_commands=["reauthorize"],
                candidate_user_ids=["admin"],
                node_execution=node,
            )
            store.wait_for_work_item(instance, node, work_item_id=attention.id)
        db.commit()
        assert json.dumps(attention.payload_json).find("xoxb-") == -1


def install_connection_service_override() -> None:
    """仅为隔离全栈进程注入可预测 Slack 边界，生产应用和租户请求均不能选择该地址。"""

    from fastapi import Depends
    from sqlmodel import Session

    from app.api.connection_profiles import get_connection_service
    from app.connectors.service import ConnectionService
    from app.connectors.slack import SlackCallResult
    from app.db import get_session
    from app.main import app

    class BrowserSlack:
        """模拟同一待重授权 workspace 的验证与安全只读响应。"""

        def auth_test(self, token: str) -> SlackCallResult:
            """按测试 token 明确返回账号，避免浏览器回归访问公网。"""

            if "reauth" in token:
                team_id = "T-E2E-REAUTH"
            elif "create" in token:
                team_id = "T-E2E-CREATED"
            else:
                team_id = "T-E2E-MANAGE"
            return SlackCallResult(
                True,
                {"ok": True, "team_id": team_id},
                granted_scopes=frozenset({"channels:read"}),
            )

        def conversations_info(self, _token: str, *, channel_id: str) -> SlackCallResult:
            """返回固定频道身份，供管理面授权读取验收。"""

            return SlackCallResult(
                True,
                {"ok": True, "channel": {"id": channel_id, "name": "contracts"}},
            )

    def override_service(db: Session = Depends(get_session)) -> ConnectionService:
        """让每个 HTTP 请求继续使用真实事务，只替换 provider adapter。"""

        return ConnectionService(db, slack=BrowserSlack())

    app.dependency_overrides[get_connection_service] = override_service


def install_general_skill_remote_fetcher_override() -> None:
    """只替换外部 GitHub 下载响应，保留真实 API、解析、对象存储、事务和绑定链。"""

    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    from app.api.general_skill_imports import get_general_skill_remote_fetcher
    from app.general_skills import worker as general_skill_worker
    from app.general_skills.remote_source import RemoteFetchResult
    from app.main import app

    class BrowserRemoteFetcher:
        """按 GitHub 或 SkillHub 契约返回固定 Skill 包，不替换内部导入链。"""

        def fetch(
            self,
            source_url: str,
            *,
            allowed_hosts: frozenset[str] | None = None,
            authorization: str | None = None,
            authorization_hosts: frozenset[str] | None = None,
        ) -> RemoteFetchResult:
            """校验来源专属 URL/allowlist 后生成确定性 provider 响应。"""

            payload = BytesIO()
            if source_url.startswith(
                "https://wry-manatee-359.convex.site/api/v1/download?slug="
            ):
                if allowed_hosts != frozenset({"wry-manatee-359.convex.site"}):
                    raise RuntimeError("missing E2E SkillHub host allowlist")
                with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
                    archive.writestr(
                        "SKILL.md",
                        "---\n"
                        "name: skillhub-browser-helper\n"
                        "description: Verify the reviewed SkillHub adapter.\n"
                        "---\n# SkillHub browser helper\n",
                    )
                return RemoteFetchResult(source_url, payload.getvalue(), 0)
            if not source_url.startswith("https://github.com/mattpocock/skills/archive/"):
                raise RuntimeError("unexpected E2E remote archive URL")
            if not allowed_hosts or "codeload.github.com" not in allowed_hosts:
                raise RuntimeError("missing E2E GitHub redirect allowlist")
            if authorization is not None:
                if authorization != "Bearer e2e-private-github-token":
                    raise RuntimeError("unexpected E2E private source authorization")
                if authorization_hosts != frozenset({"github.com"}):
                    raise RuntimeError("private source authorization escaped its host binding")
            time.sleep(0.8)
            with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
                archive.writestr(
                    "skills-main/skills/engineering/implement/SKILL.md",
                    "---\n"
                    "name: implement\n"
                    'description: "Implement a piece of work based on a spec or set of tickets."\n'
                    "disable-model-invocation: true\n"
                    "---\n"
                    "Implement the work described by the user in the spec or tickets.\n\n"
                    "Use /tdd where possible, at pre-agreed seams.\n\n"
                    "Once done, use /code-review to review the work.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/tdd/SKILL.md",
                    "---\n"
                    "name: tdd\n"
                    "description: Test-driven development. Use when the user wants to build "
                    'features or fix bugs test-first, mentions "red-green-refactor", or wants '
                    "integration tests.\n"
                    "---\n# Test-Driven Development\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/code-review/SKILL.md",
                    "---\n"
                    "name: code-review\n"
                    "description: Review changes along the Standards and Spec axes.\n"
                    "---\n# Code Review\n",
                )
            return RemoteFetchResult(source_url, payload.getvalue(), 0)

    app.dependency_overrides[get_general_skill_remote_fetcher] = BrowserRemoteFetcher
    general_skill_worker.get_remote_fetcher = BrowserRemoteFetcher


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
        SopInstance,
        SopNodeExecution,
        SopWorkItem,
    )
    from app.db.models import utc_now

    now = utc_now()
    agent_id = "agent_e2e_employee"
    with Session(engine) as db:
        pagination_session = ChatSession(
            id="session_e2e_work_item_pagination",
            tenant_id="tenant_demo",
            user_id="admin",
            agent_id=agent_id,
            title="浏览器流程任务分页",
            status="waiting",
        )
        pagination_instance = SopInstance(
            id="instance_e2e_work_item_pagination",
            tenant_id="tenant_demo",
            session_id=pagination_session.id,
            skill_id="expense_over_limit_approval",
            skill_version_id="skillver_e2e_expense_org_scope",
            skill_version="2.1.0",
            definition_checksum="f" * 64,
            active_slot_key=f"foreground:{pagination_session.id}",
            initiator_user_id="requestor_e2e",
            status="waiting",
            current_node_id="浏览器分页节点_20",
        )
        db.add(pagination_session)
        db.add(pagination_instance)
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
                    source_kind="legacy",
                    source_ref=f"legacy:schedrun_e2e_page_{index:02d}",
                    source_snapshot_json={},
                    source_checksum=f"legacy-e2e-{index:02d}",
                    scheduled_for=scheduled_for,
                    status="succeeded",
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
            node_execution_id = f"execution_e2e_page_{index:02d}"
            node_id = f"浏览器分页节点_{index:02d}"
            db.add(
                SopNodeExecution(
                    id=node_execution_id,
                    tenant_id="tenant_demo",
                    instance_id=pagination_instance.id,
                    node_id=node_id,
                    step_key=node_id,
                    status="waiting",
                )
            )
            db.add(
                SopWorkItem(
                    id=f"sopwork_e2e_page_{index:02d}",
                    tenant_id="tenant_demo",
                    instance_id=pagination_instance.id,
                    node_execution_id=node_execution_id,
                    skill_version_id="skillver_e2e_expense_org_scope",
                    node_id=node_id,
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
    seed_managed_workspace_browser_fixture()
    seed_schedule_dynamic_model()
    seed_dynamic_task_browser_fixtures()
    seed_connection_browser_fixtures()
    seed_pagination_browser_fixtures()
    seed_large_organization_browser_fixture()
    install_connection_service_override()
    install_general_skill_remote_fetcher_override()
    install_schedule_llm_override()

    import uvicorn
    from single_port_app import app

    uvicorn.run(app, host="127.0.0.1", port=E2E_PORT, log_level="warning")


if __name__ == "__main__":
    main()
