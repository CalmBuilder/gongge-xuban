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
E2E_PORT = int(os.environ.get("FULLSTACK_E2E_PORT", "5148"))
E2E_SECRET = "fullstack-e2e-isolated-secret-at-least-32-bytes"
E2E_RUNTIME_DIR = Path(
    os.environ.get(
        "FULLSTACK_E2E_RUNTIME_DIR",
        str(Path(tempfile.gettempdir()) / "gongge-fullstack-e2e-current"),
    )
)
REFUND_APPROVAL_MIGRATION = (
    BACKEND_DIR / "tests" / "fixtures" / "refund_approval_mysql.sql"
).read_text(encoding="utf-8")

REFUND_BACKEND_BASELINE = """\
HIGH_AMOUNT_THRESHOLD = 10000

def request_refund(amount):
    return {"amount": amount, "status": "ready"}
"""
REFUND_BACKEND_PATCHED = """\
HIGH_AMOUNT_THRESHOLD = 10000

def request_refund(amount, audit):
    status = "pending_approval" if amount > HIGH_AMOUNT_THRESHOLD else "ready"
    audit.append({"event": "refund_requested", "amount": amount, "status": status})
    return {"amount": amount, "status": status}

def approve_refund(refund, audit):
    if refund["status"] != "pending_approval":
        raise ValueError("refund is not awaiting approval")
    refund["status"] = "approved"
    audit.append({"event": "refund_approved", "amount": refund["amount"]})
    return refund
"""
REFUND_FRONTEND_BASELINE = """\
export function availableRefundActions(status) {
  return status === 'ready' ? ['refund'] : [];
}
"""
REFUND_FRONTEND_PATCHED = """\
export function availableRefundActions(status) {
  if (status === 'pending_approval') return ['view_approval'];
  if (status === 'approved' || status === 'ready') return ['refund'];
  return [];
}
"""
REFUND_SKILL_SETUP = """\
## Agent skills

### Issue tracker

Issues use repository-local Markdown under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Domain docs

This repository uses one root `CONTEXT.md` and `docs/adr/`.
"""
REFUND_ISSUE_TRACKER = """\
# Issue tracker

Use one Markdown file per ticket under `.scratch/<feature>/issues/`.
Record blocking ticket numbers explicitly and keep status `ready-for-agent` until implementation starts.
"""
REFUND_DOMAIN_GUIDE = """\
# Domain documentation

Read the root `CONTEXT.md` and relevant records under `docs/adr/` before planning or implementation.
"""
REFUND_CONTEXT = """\
# Refund approval domain

- Refund request: a request to return a captured payment.
- Approval: an auditable decision required when amount exceeds 10000.
- Refund execution: the irreversible payment action, permitted only for ready or approved requests.
- Rejection: a terminal approval decision that forbids refund execution.

Invariants: high-amount requests enter `pending_approval`; only an approver may transition them to
`approved`; every request and decision appends an audit event.
"""
REFUND_ADR = """\
# ADR 0001: Gate high-amount refund execution

Status: accepted

Refund request and refund execution remain separate actions. Amounts above 10000 create a pending
approval record. Approval is explicit and auditable; rejection never exposes the refund action.
"""
REFUND_SPEC = """\
# High-amount refund approval specification

## Problem Statement
High-value refunds can currently execute without an independent approval decision.

## Solution
Requests above 10000 enter `pending_approval`; approval changes the state to `approved`, after which
the refund action becomes available. Request and approval transitions are audited.

## User Stories
1. As an operator, I can request a high-value refund and see that approval is pending.
2. As an approver, I can approve a pending refund and create an audit event.
3. As an auditor, I can distinguish request and approval events.

## Implementation Decisions
Add a portable approval table, backend state transitions, and matching frontend actions.

## Testing Decisions
Verify public behavior through backend state/audit tests, frontend action tests, and SQLite/MySQL
migration contract tests.

## Out of Scope
Payment-provider settlement and production notification delivery.
"""
REFUND_TICKETS = {
    ".scratch/high-refund/issues/01-request-approval.md": """\
# 01 — Request high-amount approval

**What to build:** A complete schema-to-API-to-UI path that puts high refunds in pending approval.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] Portable approval migration
- [ ] Request state and audit regression
""",
    ".scratch/high-refund/issues/02-approve-and-refund.md": """\
# 02 — Approve and expose refund action

**What to build:** Approve a pending request and expose refund only after approval.

**Blocked by:** 01 — Request high-amount approval

**Status:** ready-for-agent

- [ ] Approval transition and audit event
- [ ] Frontend pending/approved actions
""",
}
REFUND_REVIEW_CHECK = """\
from pathlib import Path
import json

backend = Path("backend/refunds.py").read_text(encoding="utf-8")
frontend = Path("frontend/refund-state.mjs").read_text(encoding="utf-8")
migration = Path("migrations/0002_high_refund_approval.sql").read_text(encoding="utf-8")
standards_issues = []
if any(marker in backend + frontend for marker in ("[DEBUG-", "TODO", "FIXME")):
    standards_issues.append("temporary instrumentation remains")
if "BIGINT PRIMARY KEY" not in migration or "VARCHAR(32)" not in migration:
    standards_issues.append("migration is not SQLite/MySQL portable")
spec_issues = []
for required in (
    "pending_approval",
    "refund_requested",
    "refund_approved",
    "approve_refund",
):
    if required not in backend:
        spec_issues.append("backend missing " + required)
for required in ("pending_approval", "view_approval", "approved"):
    if required not in frontend:
        spec_issues.append("frontend missing " + required)
for required in ("refund_approvals", "amount", "status", "created_at"):
    if required not in migration:
        spec_issues.append("migration missing " + required)
result = {
    "standards": {"status": "passed" if not standards_issues else "failed", "issues": standards_issues},
    "spec": {"status": "passed" if not spec_issues else "failed", "issues": spec_issues},
    "unresolved_risks": [],
}
print(json.dumps(result, ensure_ascii=False))
if standards_issues or spec_issues:
    raise SystemExit(1)
"""
DIAGNOSIS_MEMORY_BASELINE = """\
def build_memory_context(preferences):
    return []
"""
DIAGNOSIS_MEMORY_PATCHED = """\
def build_memory_context(preferences):
    return [item for item in preferences if item.strip()]
"""
DIAGNOSIS_TEST = """\
import unittest

from app.memory_route import build_memory_context


class MemoryContextRegressionTest(unittest.TestCase):
    def test_no_sop_route_retains_agent_preference(self):
        actual = build_memory_context(["称呼用户为张工"])
        self.assertEqual(actual, ["称呼用户为张工"], "remembered preference missing")
"""
DIAGNOSIS_CLEAN_CHECK = """\
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path.cwd()))
suite = unittest.defaultTestLoader.discover("tests")
if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
    raise SystemExit("regression suite failed")
matches = []
for path in Path("app").rglob("*.py"):
    if "[DEBUG-" in path.read_text(encoding="utf-8"):
        matches.append(str(path))
if matches:
    raise SystemExit("debug instrumentation remains: " + ",".join(matches))
print("GREEN_AND_CLEAN")
"""


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
        KnowledgeBucket,
        KnowledgeChunk,
        KnowledgeDiscoverySuggestion,
        KnowledgeDocument,
        KnowledgeIngestJob,
        MemoryRecord,
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
                id="agent_e2e_diagnosis",
                tenant_id="tenant_demo",
                name="E2E 疑难故障诊断分身",
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
                id="agent_e2e_delivery",
                tenant_id="tenant_demo",
                name="E2E 研发交付数字员工",
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
            KnowledgeDocument(
                id="kdoc_e2e_member",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                filename="e2e-member-knowledge.md",
                file_type="markdown",
                title="Agent Loop 记忆投影手册",
                status="ready",
                bucket_count=1,
                chunk_count=1,
            )
        )
        db.add(
            KnowledgeBucket(
                id="kbucket_e2e_member_agent_loop",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                document_id="kdoc_e2e_member",
                bucket_key="agent-loop-memory-context",
                title="Agent Loop 无 SOP 记忆投影契约",
                summary="无 SOP 路径仍须保留当前数字员工的 memory_context。",
                token_estimate=40,
            )
        )
        db.add(
            KnowledgeChunk(
                id="kchunk_e2e_member_agent_loop",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_e2e_member",
                knowledge_base_version_id="kbver_kb_e2e_member_1_0_0",
                document_id="kdoc_e2e_member",
                bucket_id="kbucket_e2e_member_agent_loop",
                chunk_index=0,
                content=(
                    "Agent Loop 在无 SOP 普通问答和动态任务分流时，必须把当前数字员工隔离召回的 "
                    "memory_context 原样传给能力路由和 DynamicTaskAgent，禁止替换为空数组。"
                ),
                summary="无 SOP 动态任务必须消费 Agent 隔离记忆。",
                source_ref="e2e-member-knowledge.md#agent-loop-memory-context",
            )
        )
        db.add(
            MemoryRecord(
                id="memory_e2e_diagnosis_preference",
                tenant_id="tenant_demo",
                user_id="member_e2e",
                username="member",
                agent_id="agent_e2e_diagnosis",
                kind="preference",
                content="诊断时先给出可复现命令，并称呼我为张工。",
                metadata_json={
                    "agent_id": "agent_e2e_diagnosis",
                    "key": "diagnosis_style",
                },
            )
        )
        db.add(
            MemoryRecord(
                id="memory_e2e_other_agent_preference",
                tenant_id="tenant_demo",
                user_id="member_e2e",
                username="member",
                agent_id="agent_e2e_member_employee",
                kind="preference",
                content="购物售后助手只展示物流摘要。",
                metadata_json={
                    "agent_id": "agent_e2e_member_employee",
                    "key": "after_sales_style",
                },
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
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            "agent_e2e_diagnosis",
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
    """创建含后端、迁移、前端和回归的售后仓库，并发布固定受管工具。"""

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
    (repo / "backend").mkdir()
    (repo / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "backend" / "refunds.py").write_text(
        REFUND_BACKEND_BASELINE, encoding="utf-8"
    )
    (repo / "frontend").mkdir()
    (repo / "frontend" / "refund-state.mjs").write_text(
        REFUND_FRONTEND_BASELINE, encoding="utf-8"
    )
    (repo / "frontend" / "refund-state.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { availableRefundActions } from './refund-state.mjs';\n\n"
        "test('pending approval only exposes approval detail', () => {\n"
        "  assert.deepEqual(availableRefundActions('pending_approval'), ['view_approval']);\n"
        "});\n"
        "test('approved refund exposes refund action', () => {\n"
        "  assert.deepEqual(availableRefundActions('approved'), ['refund']);\n"
        "});\n",
        encoding="utf-8",
    )
    (repo / "checks").mkdir()
    (repo / "checks" / "two_axis_review.py").write_text(
        REFUND_REVIEW_CHECK,
        encoding="utf-8",
    )
    (repo / "migrations").mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_refund.py").write_text(
        "import sqlite3\n"
        "import unittest\n"
        "from pathlib import Path\n\n"
        "from backend.refunds import approve_refund, request_refund\n\n"
        "class RefundApprovalTest(unittest.TestCase):\n"
        "    def test_high_refund_requires_approval(self):\n"
        "        audit = []\n"
        "        refund = request_refund(20000, audit)\n"
        "        self.assertEqual(refund['status'], 'pending_approval')\n"
        "        approve_refund(refund, audit)\n"
        "        self.assertEqual(refund['status'], 'approved')\n"
        "        self.assertEqual([item['event'] for item in audit], "
        "['refund_requested', 'refund_approved'])\n\n"
        "    def test_sqlite_migration_is_runnable(self):\n"
        "        sql = Path('migrations/0002_high_refund_approval.sql').read_text()\n"
        "        db = sqlite3.connect(':memory:')\n"
        "        db.executescript(sql)\n"
        "        columns = [row[1] for row in db.execute('PRAGMA table_info(refund_approvals)')]\n"
        "        self.assertEqual(columns, ['id', 'amount', 'status', 'created_at'])\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "baseline"], check=True
    )
    diagnosis_repo = (
        E2E_RUNTIME_DIR / "managed-workspaces" / "tenant_demo" / "memory-diagnosis-demo"
    )
    diagnosis_repo.mkdir(parents=True)
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "robot@example.invalid"),
        ("config", "user.name", "E2E Diagnosis Robot"),
    ):
        subprocess.run(["git", "-C", str(diagnosis_repo), *argv], check=True)
    (diagnosis_repo / "app").mkdir()
    (diagnosis_repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (diagnosis_repo / "app" / "memory_route.py").write_text(
        DIAGNOSIS_MEMORY_BASELINE,
        encoding="utf-8",
    )
    (diagnosis_repo / "tests").mkdir()
    (diagnosis_repo / "checks").mkdir()
    (diagnosis_repo / "checks" / "no_debug.py").write_text(
        DIAGNOSIS_CLEAN_CHECK,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(diagnosis_repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(diagnosis_repo), "commit", "-m", "baseline"],
        check=True,
    )
    image = (
        "python@sha256:"
        "9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4"
    )
    node_image = (
        "node@sha256:"
        "1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b"
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
            "workspace.refund.apply-set",
            "apply_files",
            "local_write",
            {
                "changes": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            {
                "files": {"type": "array"},
                "changed_count": {"type": "integer"},
                "branch": {"type": "string"},
            },
            [
                "input.changes",
                "output.files",
                "output.changed_count",
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
                    "backend-red": {
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
                        "expected_exit_codes": [1],
                        "required_output_substrings": ["cannot import name 'approve_refund'"],
                    },
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
                        "required_output_substrings": ["OK"],
                    },
                    "frontend-unit": {
                        "image": node_image,
                        "argv": ["node", "--test", "frontend/refund-state.test.mjs"],
                        "timeout_seconds": 60,
                        "required_output_substrings": ["pass 2"],
                    },
                    "two-axis-review": {
                        "image": image,
                        "argv": ["python", "checks/two_axis_review.py"],
                        "timeout_seconds": 60,
                        "required_output_substrings": [
                            '"standards": {"status": "passed"',
                            '"spec": {"status": "passed"',
                            '"unresolved_risks": []',
                        ],
                    },
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
            ensure_private_resource_binding(
                db,
                "tenant_demo",
                "agent_e2e_delivery",
                "tool",
                tool.id,
                "active",
            )
        diagnosis_definitions = (
            (
                "workspace.memory.read",
                "read_file",
                "read",
                {"path": {"type": "string"}},
                {"content": {"type": "string"}, "sha256": {"type": "string"}},
                ["input.path", "output.content", "output.sha256"],
            ),
            (
                "workspace.memory.apply-set",
                "apply_files",
                "local_write",
                {"changes": {"type": "array", "items": {"type": "object"}}},
                {
                    "files": {"type": "array"},
                    "changed_count": {"type": "integer"},
                    "branch": {"type": "string"},
                },
                ["input.changes", "output.files", "output.changed_count", "output.branch"],
            ),
            (
                "workspace.memory.check",
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
                "workspace.memory.commit",
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
        for name, handler, risk, input_properties, output_properties, paths in (
            diagnosis_definitions
        ):
            config = {
                "workspace_id": "memory-diagnosis-demo",
                "base_ref": "main",
                "handler": handler,
            }
            if handler == "run_check":
                config["check_profiles"] = {
                    "diagnosis-red": {
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
                        "expected_exit_codes": [1],
                        "required_output_substrings": ["remembered preference missing"],
                    },
                    "diagnosis-green-clean": {
                        "image": image,
                        "argv": ["python", "checks/no_debug.py"],
                        "timeout_seconds": 60,
                        "required_output_substrings": ["GREEN_AND_CLEAN"],
                    },
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
                "agent_e2e_diagnosis",
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
            if "S4代码" in goal and {"implement", "tdd", "code-review"} <= names:
                selected = ["implement", "tdd", "code-review"]
            elif "S4诊断" in goal and {
                "diagnosing-bugs",
                "tdd",
                "codebase-design",
            } <= names:
                selected = ["diagnosing-bugs", "tdd", "codebase-design"]
            elif "S4代码撤权" in goal and "s4-code-countermand-guidance" in names:
                selected = ["s4-code-countermand-guidance"]
            elif "S4代码拒绝" in goal and "s4-code-deny-guidance" in names:
                selected = ["s4-code-deny-guidance"]
            elif "S4代码" in goal and "s4-code-guidance" in names:
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
            loaded_names = {
                str(item.get("name") or "")
                for item in loaded_guidance
                if isinstance(item, dict)
            }
            full_delivery_names = {
                "setup-matt-pocock-skills",
                "grill-with-docs",
                "grilling",
                "domain-modeling",
                "to-spec",
                "to-tickets",
                "implement",
                "tdd",
                "code-review",
            }
            if full_delivery_names <= loaded_names:
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                required = {
                    "workspace.refund.read",
                    "workspace.refund.apply-set",
                    "workspace.refund.check",
                    "workspace.refund.commit",
                }
                if not required <= capability_names:
                    raise RuntimeError("S4 full delivery missed governed workspace tools")
                return {
                    "goal": str(user_payload.get("goal") or "完成退款审批研发交付"),
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["同一任务分支先固化规划产物，再以 TDD 实现并完成两轴审查"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "setup_domain",
                            "title": "配置工程 Skill 仓库约定并固化退款领域词汇与 ADR",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": [
                                "setup-matt-pocock-skills",
                                "grill-with-docs",
                                "grilling",
                                "domain-modeling",
                            ],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "spec_tickets",
                            "title": "发布退款审批可验证规格与带 blocking edges 的纵向票据",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["setup_domain"],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": [
                                "setup-matt-pocock-skills",
                                "to-spec",
                                "to-tickets",
                            ],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "read",
                            "title": "读取退款实现",
                            "kind": "tool.read",
                            "required": True,
                            "depends_on": ["spec_tickets"],
                            "capability_refs": ["workspace.refund.read"],
                            "guidance_skill_refs": ["implement"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "red",
                            "title": "证明退款回归在修复前失败",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["read"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": ["tdd"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "apply",
                            "title": "写入退款审批、迁移和前端补丁",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["red"],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": ["implement", "tdd"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "check",
                            "title": "运行退款回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["apply"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": ["tdd"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "frontend_check",
                            "title": "运行退款前端状态回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["check"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": ["tdd"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "review",
                            "title": "按 Standards 与 Spec 两轴完成代码审查",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["frontend_check"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": ["code-review"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "commit",
                            "title": "提交一次性任务分支",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["review"],
                            "capability_refs": ["workspace.refund.commit"],
                            "guidance_skill_refs": ["implement"],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成代码交付报告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["commit"],
                            "capability_refs": [],
                            "guidance_skill_refs": sorted(full_delivery_names),
                            "expected_output_schema": {},
                        },
                    ],
                }
            preparation_phase = ""
            preparation_title = ""
            if "to-tickets" in loaded_names:
                preparation_phase = "tickets"
                preparation_title = "发布带 blocking edges 的纵向票据"
            elif "to-spec" in loaded_names:
                preparation_phase = "spec"
                preparation_title = "发布退款审批可验证规格"
            elif "grill-with-docs" in loaded_names:
                preparation_phase = "domain"
                preparation_title = "固化退款领域词汇与 ADR"
            elif loaded_names == {"setup-matt-pocock-skills"}:
                preparation_phase = "setup"
                preparation_title = "配置工程 Skill 仓库约定"
            combined_preparation = {
                "setup-matt-pocock-skills",
                "grill-with-docs",
                "grilling",
                "domain-modeling",
                "to-spec",
                "to-tickets",
            } <= loaded_names
            if preparation_phase:
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if "workspace.refund.apply-set" not in capability_names:
                    raise RuntimeError("S4 preparation planner missed managed workspace write tool")
                guidance_refs = sorted(loaded_names)
                if combined_preparation:
                    preparation_steps = [
                        {
                            "draft_id": "setup",
                            "title": "配置工程 Skill 仓库约定",
                            "guidance_skill_refs": ["setup-matt-pocock-skills"],
                        },
                        {
                            "draft_id": "domain",
                            "title": "固化退款领域词汇与 ADR",
                            "guidance_skill_refs": [
                                "grill-with-docs",
                                "grilling",
                                "domain-modeling",
                            ],
                        },
                        {
                            "draft_id": "spec",
                            "title": "发布退款审批可验证规格",
                            "guidance_skill_refs": ["to-spec", "setup-matt-pocock-skills"],
                        },
                        {
                            "draft_id": "tickets",
                            "title": "发布带 blocking edges 的纵向票据",
                            "guidance_skill_refs": ["to-tickets", "setup-matt-pocock-skills"],
                        },
                    ]
                    previous = ""
                    steps: list[dict[str, object]] = []
                    for item in preparation_steps:
                        steps.append(
                            {
                                **item,
                                "kind": "tool.write",
                                "required": True,
                                "depends_on": [previous] if previous else [],
                                "capability_refs": ["workspace.refund.apply-set"],
                                "expected_output_schema": {},
                            }
                        )
                        previous = str(item["draft_id"])
                    steps.append(
                        {
                            "draft_id": "answer",
                            "title": "确认 preparation 阶段产物",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [previous],
                            "capability_refs": [],
                            "guidance_skill_refs": guidance_refs,
                            "expected_output_schema": {},
                        }
                    )
                else:
                    steps = [
                        {
                            "draft_id": preparation_phase,
                            "title": preparation_title,
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": guidance_refs,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": f"确认 {preparation_phase} 阶段产物",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [preparation_phase],
                            "capability_refs": [],
                            "guidance_skill_refs": guidance_refs,
                            "expected_output_schema": {},
                        },
                    ]
                return {
                    "goal": str(user_payload.get("goal") or preparation_title),
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["只写入受管演示仓库并保留固定 Skill revision 因果链"],
                    "assumptions": [],
                    "steps": steps,
                }
            if "DIAGNOSING-BUGS-FIXED-COMMIT" in str(loaded_guidance):
                loaded_text = str(loaded_guidance)
                if (
                    "# Test-Driven Development" not in loaded_text
                    or "CODEBASE-DESIGN-FIXED-COMMIT" not in loaded_text
                ):
                    raise RuntimeError("S4 diagnosis planner did not receive all fixed Skills")
                memory_text = str(user_payload.get("memory_context", []))
                if "称呼我为张工" not in memory_text:
                    raise RuntimeError("S4 diagnosis planner did not receive agent memory")
                if "购物售后助手只展示物流摘要" in memory_text:
                    raise RuntimeError("S4 diagnosis planner received another agent's memory")
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                required = {
                    "workspace.memory.read",
                    "workspace.memory.apply-set",
                    "workspace.memory.check",
                    "knowledge.search",
                }
                if not required <= capability_names:
                    raise RuntimeError("S4 diagnosis planner missed governed context or tools")
                marker_names: dict[str, str] = {}
                for item in loaded_guidance:
                    if not isinstance(item, dict):
                        continue
                    item_text = str(item.get("skills") or "")
                    item_name = str(item.get("name") or "")
                    for marker in (
                        "DIAGNOSING-BUGS-FIXED-COMMIT",
                        "# Test-Driven Development",
                        "CODEBASE-DESIGN-FIXED-COMMIT",
                    ):
                        if marker in item_text:
                            marker_names[marker] = item_name
                if len(marker_names) != 3 or any(
                    not value or value not in loaded_names for value in marker_names.values()
                ):
                    raise RuntimeError("S4 diagnosis planner received ambiguous Skill references")
                diagnosing_guidance = marker_names["DIAGNOSING-BUGS-FIXED-COMMIT"]
                tdd_guidance = marker_names["# Test-Driven Development"]
                design_guidance = marker_names["CODEBASE-DESIGN-FIXED-COMMIT"]
                all_guidance = sorted(marker_names.values())
                return {
                    "goal": str(user_payload.get("goal") or "诊断记忆上下文缺失"),
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "先形成 red-capable loop，再给出可证伪假设，最后验证原始症状并清理 instrumentation"
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "read",
                            "title": "读取无 SOP 记忆路由实现",
                            "kind": "tool.read",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["workspace.memory.read"],
                            "guidance_skill_refs": [design_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "knowledge",
                            "title": "检索 Agent Loop 模块手册",
                            "kind": "knowledge",
                            "required": True,
                            "depends_on": ["read"],
                            "capability_refs": ["knowledge.search"],
                            "guidance_skill_refs": [diagnosing_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "test",
                            "title": "在确认的 interface seam 建立原始症状回归测试",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["knowledge"],
                            "capability_refs": ["workspace.memory.apply-set"],
                            "guidance_skill_refs": [tdd_guidance, design_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "red",
                            "title": "运行快速确定的 red-capable loop",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["test"],
                            "capability_refs": ["workspace.memory.check"],
                            "guidance_skill_refs": [diagnosing_guidance, tdd_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "hypotheses",
                            "title": "确认可证伪根因假设",
                            "kind": "clarification",
                            "required": True,
                            "depends_on": ["red"],
                            "capability_refs": [],
                            "guidance_skill_refs": [diagnosing_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "fix",
                            "title": "按已证伪假设最小修复记忆投影",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["hypotheses"],
                            "capability_refs": ["workspace.memory.apply-set"],
                            "guidance_skill_refs": [diagnosing_guidance, tdd_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "green",
                            "title": "重跑原始复现并清理调试 instrumentation",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["fix"],
                            "capability_refs": ["workspace.memory.check"],
                            "guidance_skill_refs": [diagnosing_guidance, tdd_guidance],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成诊断证据报告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["green"],
                            "capability_refs": [],
                            "guidance_skill_refs": all_guidance,
                            "expected_output_schema": {},
                        },
                    ],
                }
            engineering_delivery = (
                "# Test-Driven Development" in str(loaded_guidance)
                and "CODE-REVIEW-FIXED-COMMIT" in str(loaded_guidance)
                and "Implement the work described" in str(loaded_guidance)
            )
            if engineering_delivery or "S4-CODE-FULL-GUIDANCE" in str(loaded_guidance):
                loaded_text = str(loaded_guidance)
                if engineering_delivery:
                    read_guidance = ["implement"]
                    red_guidance = ["tdd"]
                    apply_guidance = ["implement", "tdd"]
                    review_guidance = ["code-review"]
                    answer_guidance = ["implement", "tdd", "code-review"]
                else:
                    if "s4-code-countermand-guidance" in loaded_text:
                        code_guidance_name = "s4-code-countermand-guidance"
                    elif "s4-code-deny-guidance" in loaded_text:
                        code_guidance_name = "s4-code-deny-guidance"
                    else:
                        code_guidance_name = "s4-code-guidance"
                    read_guidance = [code_guidance_name]
                    red_guidance = [code_guidance_name]
                    apply_guidance = [code_guidance_name]
                    review_guidance = [code_guidance_name]
                    answer_guidance = [code_guidance_name]
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                required = {
                    "workspace.refund.read",
                    "workspace.refund.apply-set",
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
                            "guidance_skill_refs": read_guidance,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "red",
                            "title": "证明退款回归在修复前失败",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["read"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": red_guidance,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "apply",
                            "title": "写入退款审批、迁移和前端补丁",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["red"],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": apply_guidance,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "check",
                            "title": "运行退款回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["apply"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": red_guidance,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "frontend_check",
                            "title": "运行退款前端状态回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["check"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": red_guidance,
                            "expected_output_schema": {},
                        },
                        *(
                            [
                                {
                                    "draft_id": "review",
                                    "title": "按 Standards 与 Spec 两轴完成代码审查",
                                    "kind": "tool.execute",
                                    "required": True,
                                    "depends_on": ["frontend_check"],
                                    "capability_refs": ["workspace.refund.check"],
                                    "guidance_skill_refs": review_guidance,
                                    "expected_output_schema": {},
                                }
                            ]
                            if engineering_delivery
                            else []
                        ),
                        {
                            "draft_id": "commit",
                            "title": "提交一次性任务分支",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [
                                "review" if engineering_delivery else "frontend_check"
                            ],
                            "capability_refs": ["workspace.refund.commit"],
                            "guidance_skill_refs": read_guidance,
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成代码交付报告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["commit"],
                            "capability_refs": [],
                            "guidance_skill_refs": answer_guidance,
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
            is_s4_code = (
                "S4-CODE-FULL-GUIDANCE" in str(user_payload)
                or "agent_e2e_delivery" in str(user_payload)
                or "Implement the work described" in str(user_payload)
                or "# Test-Driven Development" in str(user_payload)
                or "CODE-REVIEW-FIXED-COMMIT" in str(user_payload)
                or any(
                    marker in step_title
                    for marker in (
                        "工程 Skill 仓库约定",
                        "领域词汇与 ADR",
                        "可验证规格",
                        "blocking edges",
                    )
                )
                or (step_title.startswith("确认 ") and "阶段产物" in step_title)
            )
            is_s4_diagnosis = "agent_e2e_diagnosis" in str(user_payload)
            is_s4 = "S4-DYNAMIC-FULL-GUIDANCE" in str(user_payload)
            if is_s4_diagnosis:
                client._last_completed_response_metadata = {
                    "response_id": (
                        "e2e-s4-diagnosis-"
                        f"{step_kind}-{hashlib.sha256(step_title.encode()).hexdigest()[:12]}"
                    ),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 14, "output_tokens": 10},
                }
                if step_kind == "tool.read":
                    capability_ref = "workspace.memory.read"
                    arguments = {"path": "app/memory_route.py"}
                elif step_kind == "knowledge":
                    return {
                        "action_kind": "query_knowledge",
                        "arguments": {"query": "Agent Loop 无 SOP 路径 memory_context 投影契约"},
                        "capability_ref": "knowledge.search",
                        "expected_output_schema": {},
                        "rationale": "检索当前诊断分身绑定的模块手册证据",
                    }
                elif step_kind == "tool.write" and "回归测试" in step_title:
                    capability_ref = "workspace.memory.apply-set"
                    arguments = {
                        "changes": [
                            {
                                "path": "tests/test_memory_route.py",
                                "expected_sha256": None,
                                "content": DIAGNOSIS_TEST,
                            }
                        ]
                    }
                elif step_kind == "tool.write":
                    capability_ref = "workspace.memory.apply-set"
                    arguments = {
                        "changes": [
                            {
                                "path": "app/memory_route.py",
                                "expected_sha256": hashlib.sha256(
                                    DIAGNOSIS_MEMORY_BASELINE.encode()
                                ).hexdigest(),
                                "content": DIAGNOSIS_MEMORY_PATCHED,
                            }
                        ]
                    }
                elif step_kind == "tool.execute":
                    capability_ref = "workspace.memory.check"
                    arguments = {
                        "profile": (
                            "diagnosis-red"
                            if "red-capable" in step_title
                            else "diagnosis-green-clean"
                        )
                    }
                elif step_kind == "clarification":
                    return {
                        "action_kind": "wait_input",
                        "arguments": {
                            "question": "red 已命中原始症状，请确认优先验证的可证伪假设",
                            "options": [
                                "无 SOP 分支把已召回 memory_context 替换为空数组",
                                "知识检索覆盖了用户偏好",
                                "Skill 选择器删除了会话记忆",
                            ],
                        },
                        "capability_ref": None,
                        "expected_output_schema": {},
                        "rationale": "在修改前展示并确认排序后的可证伪假设",
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
                                "S4-DIAGNOSIS-SUCCESS：已先建立命中原始症状的 red loop，"
                                "结合模块手册与当前分身记忆确认根因，完成最小修复；原始复现转绿，"
                                "且 [DEBUG-] instrumentation 已清零。"
                            ),
                            "criterion_evidence": {
                                criterion: completed for criterion in criteria
                            },
                            "pending_questions": [],
                        },
                        "capability_ref": None,
                        "expected_output_schema": {},
                        "rationale": "只依据持久 Operation 和已确认假设形成诊断结论",
                    }
                return {
                    "action_kind": "call_tool",
                    "arguments": arguments,
                    "capability_ref": capability_ref,
                    "expected_output_schema": {},
                    "rationale": "按固定诊断/TDD/模块设计 Skill 调用受管能力",
                }
            if is_s4_code:
                full_delivery_execution = "配置工程 Skill 仓库约定" in str(user_payload)
                client._last_completed_response_metadata = {
                    "response_id": (
                        "e2e-s4-code-"
                        f"{step_kind}-{hashlib.sha256(step_title.encode()).hexdigest()[:12]}"
                    ),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                }
                if step_kind == "tool.write" and "并固化退款领域" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {"path": "AGENTS.md", "expected_sha256": None, "content": REFUND_SKILL_SETUP},
                            {
                                "path": "docs/agents/issue-tracker.md",
                                "expected_sha256": None,
                                "content": REFUND_ISSUE_TRACKER,
                            },
                            {
                                "path": "docs/agents/domain.md",
                                "expected_sha256": None,
                                "content": REFUND_DOMAIN_GUIDE,
                            },
                            {"path": "CONTEXT.md", "expected_sha256": None, "content": REFUND_CONTEXT},
                            {
                                "path": "docs/adr/0001-high-refund-approval.md",
                                "expected_sha256": None,
                                "content": REFUND_ADR,
                            },
                        ]
                    }
                elif step_kind == "tool.write" and "规格与带 blocking edges" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {
                                "path": ".scratch/high-refund/spec.md",
                                "expected_sha256": None,
                                "content": REFUND_SPEC,
                            },
                            *(
                                {"path": path, "expected_sha256": None, "content": content}
                                for path, content in REFUND_TICKETS.items()
                            ),
                        ]
                    }
                elif step_kind == "tool.write" and "工程 Skill 仓库约定" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {"path": "AGENTS.md", "expected_sha256": None, "content": REFUND_SKILL_SETUP},
                            {
                                "path": "docs/agents/issue-tracker.md",
                                "expected_sha256": None,
                                "content": REFUND_ISSUE_TRACKER,
                            },
                            {
                                "path": "docs/agents/domain.md",
                                "expected_sha256": None,
                                "content": REFUND_DOMAIN_GUIDE,
                            },
                        ]
                    }
                elif step_kind == "tool.write" and "领域词汇与 ADR" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {"path": "CONTEXT.md", "expected_sha256": None, "content": REFUND_CONTEXT},
                            {
                                "path": "docs/adr/0001-high-refund-approval.md",
                                "expected_sha256": None,
                                "content": REFUND_ADR,
                            },
                        ]
                    }
                elif step_kind == "tool.write" and "可验证规格" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {
                                "path": ".scratch/high-refund/spec.md",
                                "expected_sha256": None,
                                "content": REFUND_SPEC,
                            }
                        ]
                    }
                elif step_kind == "tool.write" and "blocking edges" in step_title:
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {"path": path, "expected_sha256": None, "content": content}
                            for path, content in REFUND_TICKETS.items()
                        ]
                    }
                elif step_kind == "tool.read":
                    capability_ref = "workspace.refund.read"
                    arguments = {"path": "backend/refunds.py"}
                elif step_kind == "tool.execute":
                    capability_ref = "workspace.refund.check"
                    if "修复前" in step_title:
                        arguments = {"profile": "backend-red"}
                    elif "前端" in step_title:
                        arguments = {"profile": "frontend-unit"}
                    elif "Standards" in step_title:
                        arguments = {"profile": "two-axis-review"}
                    else:
                        arguments = {"profile": "backend-unit"}
                elif step_kind == "tool.write" and "提交" in step_title:
                    capability_ref = "workspace.refund.commit"
                    preparation_paths = [
                        "AGENTS.md",
                        "CONTEXT.md",
                        "docs/agents/issue-tracker.md",
                        "docs/agents/domain.md",
                        "docs/adr/0001-high-refund-approval.md",
                        ".scratch/high-refund/spec.md",
                        ".scratch/high-refund/issues/01-request-approval.md",
                        ".scratch/high-refund/issues/02-approve-and-refund.md",
                    ] if full_delivery_execution else []
                    arguments = {
                        "message": "feat: require high refund approval",
                        "paths": [
                            *preparation_paths,
                            "backend/refunds.py",
                            "frontend/refund-state.mjs",
                            "migrations/0002_high_refund_approval.sql",
                        ],
                    }
                elif step_kind == "tool.write":
                    capability_ref = "workspace.refund.apply-set"
                    arguments = {
                        "changes": [
                            {
                                "path": "backend/refunds.py",
                                "expected_sha256": hashlib.sha256(
                                    REFUND_BACKEND_BASELINE.encode()
                                ).hexdigest(),
                                "content": REFUND_BACKEND_PATCHED,
                            },
                            {
                                "path": "frontend/refund-state.mjs",
                                "expected_sha256": hashlib.sha256(
                                    REFUND_FRONTEND_BASELINE.encode()
                                ).hexdigest(),
                                "content": REFUND_FRONTEND_PATCHED,
                            },
                            {
                                "path": "migrations/0002_high_refund_approval.sql",
                                "expected_sha256": None,
                                "content": REFUND_APPROVAL_MIGRATION,
                            },
                        ],
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
                                "Standards 与 Spec 两轴审查均通过，未解决风险为空，"
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
                    "skills-main/skills/engineering/setup-matt-pocock-skills/SKILL.md",
                    "---\n"
                    "name: setup-matt-pocock-skills\n"
                    "description: Set up the engineering workflow and local Markdown tracker.\n"
                    "disable-model-invocation: true\n"
                    "---\n# Setup Matt Pocock Skills\n"
                    "Use the repository-local Markdown tracker and preserve fixed Skill versions.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/grill-with-docs/SKILL.md",
                    "---\n"
                    "name: grill-with-docs\n"
                    "description: Clarify a feature deeply and preserve the decisions in docs.\n"
                    "---\n# Grill With Docs\n"
                    "Use /grilling to surface ambiguity and /domain-modeling to fix shared terms.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/grilling/SKILL.md",
                    "---\n"
                    "name: grilling\n"
                    "description: Ask focused questions until consequential ambiguity is resolved.\n"
                    "---\n# Grilling\nResolve consequential ambiguity before implementation.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/domain-modeling/SKILL.md",
                    "---\n"
                    "name: domain-modeling\n"
                    "description: Establish precise domain vocabulary, states, and invariants.\n"
                    "---\n# Domain Modeling\nDefine states, transitions, actors, and invariants.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/to-spec/SKILL.md",
                    "---\n"
                    "name: to-spec\n"
                    "description: Turn agreed decisions into a verifiable implementation spec.\n"
                    "disable-model-invocation: true\n"
                    "---\n# To Spec\n"
                    "Run /setup-matt-pocock-skills if tracker configuration is missing.\n"
                    "Write observable requirements, exclusions, and acceptance checks.\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/to-tickets/SKILL.md",
                    "---\n"
                    "name: to-tickets\n"
                    "description: Split a spec into ordered vertical tickets with blocking edges.\n"
                    "disable-model-invocation: true\n"
                    "---\n# To Tickets\n"
                    "Run /setup-matt-pocock-skills if tracker configuration is missing.\n"
                    "Create independently verifiable vertical slices.\n",
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
                    "skills-main/skills/engineering/diagnosing-bugs/SKILL.md",
                    "---\n"
                    "name: diagnosing-bugs\n"
                    "description: Diagnosis loop for hard bugs and performance regressions. "
                    "Use when the user reports something broken, failing, or slow.\n"
                    "---\n"
                    "# Diagnosing Bugs\n"
                    "DIAGNOSING-BUGS-FIXED-COMMIT：先建立快速、确定、可由 Agent 运行且能命中"
                    "用户原始症状的 red-capable loop；再最小化复现，列出可证伪假设，修复后重跑"
                    "原始复现，并清理所有 [DEBUG-] instrumentation。\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/codebase-design/SKILL.md",
                    "---\n"
                    "name: codebase-design\n"
                    "description: Shared vocabulary for designing deep modules and deciding "
                    "where a test seam belongs.\n"
                    "---\n"
                    "# Codebase Design\n"
                    "CODEBASE-DESIGN-FIXED-COMMIT：测试应位于调用者使用的 interface seam；"
                    "module 应以小 interface 隐藏较深 implementation，并保持 locality。\n",
                )
                archive.writestr(
                    "skills-main/skills/engineering/code-review/SKILL.md",
                    "---\n"
                    "name: code-review\n"
                    "description: Review changes along the Standards and Spec axes.\n"
                    "---\n# Code Review\n"
                    "CODE-REVIEW-FIXED-COMMIT: review Standards and Spec separately to a fixed "
                    "point; aggregate findings without reranking and list unresolved risks.\n",
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

    reuse_runtime = os.environ.get("FULLSTACK_E2E_REUSE_RUNTIME") == "1"
    database_path = E2E_RUNTIME_DIR / "e2e.sqlite3"
    if reuse_runtime and not database_path.is_file():
        raise RuntimeError("FULLSTACK_E2E_REUSE_RUNTIME requires an existing database")
    if not reuse_runtime:
        shutil.rmtree(E2E_RUNTIME_DIR, ignore_errors=True)
        E2E_RUNTIME_DIR.mkdir(mode=0o700)
    configure_environment(database_path)
    if not reuse_runtime:
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
