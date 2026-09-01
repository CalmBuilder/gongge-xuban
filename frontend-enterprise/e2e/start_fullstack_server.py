"""
@Time       : 2026/08/28 13:20
@Author     : zhanglp8181
@File       : start_fullstack_server.py
@CallChain  : Playwright fullstack 配置 → 临时 SQLite → FastAPI 单端口应用
@Description: 启动隔离真实全栈服务，并准备登录、流程、动态任务、Artifact 和分页浏览器数据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
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
# 管理端模型容量与单个 Dynamic/回答阶段预算是两层契约。真实回归必须接受
# 管理端配置的高容量模型，但运行阶段仍由 backend/app/llm/output_policy.py
# 和 DynamicBudgetProfile 限制实际请求，避免把模型容量直接变成无限消费。
LIVE_MODEL_MAX_OUTPUT_TOKENS = 1_048_576
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
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory_route import build_memory_context


class MemoryContextRegressionTest(unittest.TestCase):
    def test_no_sop_route_retains_agent_preference(self):
        actual = build_memory_context(["称呼用户为张工"])
        self.assertEqual(actual, ["称呼用户为张工"], "remembered preference missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
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
    """配置隔离全栈环境；显式测试URL可用于真实 MySQL Chromium 门禁。"""

    database_url = os.environ.get("FULLSTACK_E2E_DATABASE_URL") or f"sqlite:///{database_path}"
    environment = {
        "APP_ENV": "test",
        "APP_HOST": "127.0.0.1",
        "APP_PORT": str(E2E_PORT),
        "APP_SECRET": E2E_SECRET,
        "AUTO_RESTART": "false",
        "DATABASE_URL": database_url,
        # provider 自调用必须命中本进程监听的 IPv4 地址；localhost 在部分环境会先解析到
        # 未监听的 ::1，导致正向 disposable 回归被误判为 unknown。
        "TOOL_BASE_URL": f"http://127.0.0.1:{E2E_PORT}",
        "PUBLIC_MOCK_API_KEY": "fullstack-e2e-public-mock-key",
        # Skill A/B 只替换供应商响应，不应借测试变量把普通动态执行重新关掉。
        # 这样同一套隔离服务仍能验证 Skill、附件和 DynamicTask 的组合契约。
        "DYNAMIC_TASK_EXECUTION_ENABLED": "true",
        "DYNAMIC_TASK_STEERING_ENABLED": "true",
        "DYNAMIC_TASK_EXPLORE_ENABLED": "true",
        "DYNAMIC_TASK_MAX_PARALLEL_READS": "2",
        "DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED": "false",
        "DYNAMIC_TASK_DESTRUCTIVE_ENABLED": "false",
        "DYNAMIC_TASK_DESTRUCTIVE_TENANT_ALLOWLIST": "",
        "DYNAMIC_TASK_DESTRUCTIVE_AGENT_ALLOWLIST": "",
        "DYNAMIC_TASK_STANDING_APPROVAL_ENABLED": "false",
        # 普通能力不依赖告警阈值；高风险 profile 在下方显式补齐阈值。
        "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD": "0",
        "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD": "0",
        "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD": "0",
        "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD": "0",
        "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS": "0",
        "DYNAMIC_TASK_MAX_ACTIVE_PER_TENANT": "16",
        "DYNAMIC_TASK_MAX_ACTIVE_PER_AGENT": "8",
        "DYNAMIC_TASK_MAX_ACTIVE_PER_USER": "4",
        "DYNAMIC_TASK_MAX_ACTIVE_PER_TOOL": "4",
        "DYNAMIC_TASK_MANAGED_WORKSPACE_ENABLED": "true",
        "DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT": str(E2E_RUNTIME_DIR / "managed-workspaces"),
        "GENERAL_SKILL_IMPORT_V2_ENABLED": "true",
        "GENERAL_SKILL_IMPORT_ASYNC_ENABLED": "true",
        "GENERAL_SKILL_IMPORT_WORKER_POLL_SECONDS": "0.2",
        "GENERAL_SKILL_IMPORT_WORKER_LEASE_SECONDS": "300",
        "GENERAL_SKILL_OBJECT_STORE_PATH": str(E2E_RUNTIME_DIR / "general-skill-objects"),
        "GENERAL_SKILL_RESOLVER_V2_ENABLED": "true",
        "GENERAL_SKILL_DYNAMIC_GUIDANCE_ENABLED": "true",
        "GENERAL_SKILL_AGENT_PROPOSAL_ENABLED": "true",
        "DYNAMIC_TASK_SKILL_LOADING_ENABLED": "true",
        "ATTACHMENT_ANALYSIS_ENABLED": "true",
        "ATTACHMENT_PARSER_WORKER_ENABLED": "true",
        "GONGGE_XUBAN_DATA_DIR": str(E2E_RUNTIME_DIR / "user-data"),
    }
    apply_dynamic_e2e_profile(environment)
    if live_attachment_e2e_enabled():
        live_values = {
            "DEMO_MODEL_API_KEY": os.environ.get("LIVE_ATTACHMENT_MODEL_API_KEY", "").strip(),
            "DEMO_MODEL_BASE_URL": os.environ.get(
                "LIVE_ATTACHMENT_MODEL_BASE_URL", ""
            ).strip(),
            "DEMO_MODEL_NAME": os.environ.get("LIVE_ATTACHMENT_MODEL_NAME", "").strip(),
            "LIVE_ATTACHMENT_MODEL_TEMPERATURE": os.environ.get(
                "LIVE_ATTACHMENT_MODEL_TEMPERATURE", ""
            ).strip(),
            "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS": os.environ.get(
                "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS", ""
            ).strip(),
            "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": os.environ.get(
                "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON", "{}"
            ).strip(),
        }
        missing = [name for name, value in live_values.items() if not value]
        if missing:
            raise RuntimeError(
                "LIVE_ATTACHMENT_E2E requires explicit isolated model settings: "
                + ", ".join(missing)
            )
        environment.update(live_values)
    os.environ.update(environment)
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))


def apply_dynamic_e2e_profile(environment: dict[str, str]) -> None:
    """把真实浏览器 profile 映射为隔离环境变量，不改变生产默认配置。"""

    profile = os.environ.get("FULLSTACK_E2E_PROFILE", "base-open").strip() or "base-open"
    supported_profiles = {
        "base-open",
        "high-risk-gray",
        "destructive-gray",
        "kill-switch",
        "runtime-capacity-saturated",
        "model-error",
        "unsafe-input",
        "mysql-isolated",
    }
    if profile not in supported_profiles:
        raise RuntimeError(
            "FULLSTACK_E2E_PROFILE must be one of: " + ", ".join(sorted(supported_profiles))
        )
    environment["FULLSTACK_E2E_PROFILE"] = profile
    if profile == "high-risk-gray":
        environment.update(
            {
                "DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED": "true",
                "DYNAMIC_TASK_EXTERNAL_WRITE_TENANT_ALLOWLIST": "tenant_demo",
                "DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST": "agent_e2e_employee",
                "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD": "100",
                "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD": "1",
                "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD": "1",
                "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD": "10",
                "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS": "3600",
            }
        )
    elif profile == "destructive-gray":
        environment.update(
            {
                "DYNAMIC_TASK_DESTRUCTIVE_ENABLED": "true",
                "DYNAMIC_TASK_DESTRUCTIVE_TENANT_ALLOWLIST": "tenant_demo",
                "DYNAMIC_TASK_DESTRUCTIVE_AGENT_ALLOWLIST": "agent_e2e_employee",
                "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD": "100",
                "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD": "1",
                "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD": "1",
                "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD": "10",
                "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS": "3600",
            }
        )
    elif profile == "kill-switch":
        environment["DYNAMIC_TASK_EXECUTION_ENABLED"] = "false"
    elif profile == "runtime-capacity-saturated":
        environment.update(
            {
                "DYNAMIC_TASK_MAX_ACTIVE_PER_TENANT": "0",
                "DYNAMIC_TASK_MAX_ACTIVE_PER_AGENT": "0",
                "DYNAMIC_TASK_MAX_ACTIVE_PER_USER": "0",
                "DYNAMIC_TASK_MAX_ACTIVE_PER_TOOL": "0",
            }
        )


def live_attachment_e2e_enabled() -> bool:
    """判断本次全栈进程是否进入禁止模型替身的附件真实认证模式。"""

    return os.environ.get("LIVE_ATTACHMENT_E2E") == "1"


def assert_live_attachment_model_configured() -> None:
    """校验隔离LIVE模型配置，禁止从持久`.env`旧seed密钥静默回退。"""

    from app.config import get_settings

    settings = get_settings()
    if settings.public_mock_llm_enabled:
        raise RuntimeError("LIVE_ATTACHMENT_E2E forbids PUBLIC_MOCK_LLM_ENABLED")
    if not settings.demo_model_api_key.strip():
        raise RuntimeError("LIVE_ATTACHMENT_E2E requires an explicit isolated model API key")
    if not settings.demo_model_base_url.strip() or not settings.demo_model_name.strip():
        raise RuntimeError("LIVE_ATTACHMENT_E2E requires model base URL and model name")


def certify_live_dynamic_model() -> None:
    """以真实provider探针冻结临时库Dynamic能力，禁止手工伪造ready快照。"""

    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ModelConfig, utc_now
    from app.dynamic_tasks.capability_catalog import capability_checksum
    from app.llm import LLMClient

    with Session(engine) as db:
        model = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == "tenant_demo",
                ModelConfig.enabled == True,  # noqa: E712
                ModelConfig.is_default == True,  # noqa: E712
            )
        ).first()
        if model is None:
            raise RuntimeError("LIVE_ATTACHMENT_E2E default model was not seeded")
        try:
            extra_body = json.loads(
                os.environ.get("LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON", "{}") or "{}"
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError("LIVE attachment model extra_body is invalid JSON") from exc
        if not isinstance(extra_body, dict):
            raise RuntimeError("LIVE attachment model extra_body must be an object")
        try:
            temperature = float(os.environ["LIVE_ATTACHMENT_MODEL_TEMPERATURE"])
            max_output_tokens = int(os.environ["LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("LIVE attachment model generation settings are invalid") from exc
        if not math.isfinite(temperature) or temperature < 0 or temperature > 2:
            raise RuntimeError("LIVE attachment model temperature is outside 0..2")
        _validate_live_model_max_output_tokens(max_output_tokens)
        model.extra_body_json = extra_body
        model.temperature = temperature
        model.max_output_tokens = max_output_tokens
        capabilities = LLMClient(model).preflight_dynamic_capabilities()
        model.capability_snapshot_json = capabilities
        model.name = os.environ.get("LIVE_ATTACHMENT_MODEL_DISPLAY_NAME", model.name).strip()
        model.capability_checksum = capability_checksum(capabilities)
        model.preflight_status = "ready"
        model.preflight_error = None
        model.capability_verified_at = utc_now()
        db.add(model)
        db.commit()


def _validate_live_model_max_output_tokens(value: int) -> int:
    """校验真实回归的模型容量上限，并保留管理端高容量配置不被静默改写。"""

    if value < 1 or value > LIVE_MODEL_MAX_OUTPUT_TOKENS:
        raise RuntimeError("LIVE attachment model max_output_tokens is invalid")
    return value


def seed_e2e_fixtures() -> None:
    """初始化 E2E 租户、双账号、数字员工、知识建议和可认领流程任务。"""
    from sqlmodel import Session, select

    from app.agents.branching import (
        ensure_agent_private_knowledge_branch,
        ensure_private_resource_binding,
    )
    from app.db import engine, init_db
    from app.db.models import (
        AgentProfile,
        AgentPublicationRevision,
        AgentRoleBinding,
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
        PublicationRelease,
        SopInstance,
        SopNodeExecution,
        SopOperation,
        Skill,
        SkillVersion,
        Tenant,
        Tool,
        User,
    )
    from app.approvals import ApprovalRequestService
    from app.db.demo_sop_versions import (
        EXPENSE_DEPARTMENT_APPROVER_ROLE,
        EXPENSE_FINANCE_APPROVER_ROLE,
        EXPENSE_SPECIAL_APPROVAL_ORG_SCOPE_VERSION,
    )
    from app.db.seed import seed_demo_data
    from app.dynamic_tasks.capability_catalog import (
        ToolReliabilityContract,
        publish_tool_contract,
    )
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
        destructive_target = "disposable://fixture/object-1"
        # 目标摘要是 provider 契约的一部分，必须与 disposable endpoint 的原始
        # UTF-8 SHA-256 口径一致；不能误用能力快照 checksum 的规范 JSON 口径。
        destructive_target_checksum = hashlib.sha256(destructive_target.encode("utf-8")).hexdigest()
        destructive_tool = Tool(
            id="tool_e2e_disposable_delete",
            tenant_id="tenant_demo",
            name="disposable.fixture_delete",
            display_name="隔离 Fixture 删除",
            description="仅用于 destructive-gray 浏览器正向验证的进程内 disposable provider。",
            method="POST",
            url="/api/mock/destructive/fixture-delete",
            headers_json={"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "target_checksum": {"type": "string"},
                },
                "required": ["target", "target_checksum"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            enabled=True,
        )
        publish_tool_contract(
            destructive_tool,
            ToolReliabilityContract.model_validate(
                {
                    "risk_class": "destructive",
                    "side_effect": "external",
                    "confirmation_policy": "always",
                    "timeout_policy": "unknown",
                    "dynamic_task_enabled": False,
                    "destructive_dynamic_task_enabled": True,
                    "idempotency": {
                        "mode": "request_key",
                        "remote_scope": "e2e-disposable-fixture",
                    },
                    "reconcile": {
                        "supported": True,
                        "tool_name": "disposable.fixture_delete",
                        "reference_source": "output.operation_id",
                        "terminal_status_mapping": {
                            "deleted": "complete",
                            "already_deleted": "complete",
                            "pending": "unknown",
                        },
                    },
                    "canonical_target": destructive_target,
                    "target_checksum": destructive_target_checksum,
                    "destructive_provider": "disposable",
                }
            ),
        )
        db.add(destructive_tool)

        sales_sop_content = {
            "skill_id": "attachment_sales_reconcile_sop",
            "name": "附件销售数据核验SOP",
            "version": "1.0.0",
            "execution_mode": "deterministic",
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "actuals": {"type": "array", "items": {"type": "string"}},
                        "targets": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                        "actuals_read": {"type": "object"},
                        "targets_read": {"type": "object"},
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "collect_files",
                    "type": "collect_input",
                    "name": "收集销售文件",
                    "expected_user_info": ["actuals", "targets"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                    "metadata": {
                        "attachment_slots": [
                            {
                                "slot_key": "actuals",
                                "allowed_formats": ["xlsx"],
                                "required_columns": ["Region", "Actual", "Target"],
                            },
                            {
                                "slot_key": "targets",
                                "allowed_formats": ["csv"],
                                "required_columns": ["Region", "Product", "Target"],
                            },
                        ]
                    },
                },
                {
                    "node_id": "read_actuals",
                    "type": "builtin_input",
                    "name": "读取实际销售",
                    "allowed_actions": ["call_builtin_input:input.read"],
                    "metadata": {
                        "operation_input": {"snapshot_handles": "slots.actuals"},
                        "operation_result_key": "actuals_read",
                    },
                },
                {
                    "node_id": "read_targets",
                    "type": "builtin_input",
                    "name": "读取销售目标",
                    "allowed_actions": ["call_builtin_input:input.read"],
                    "metadata": {
                        "operation_input": {"snapshot_handles": "slots.targets"},
                        "operation_result_key": "targets_read",
                    },
                },
                {"node_id": "done", "type": "terminal", "name": "核验完成"},
            ],
            "edges": [
                {"source_node_id": "collect_files", "next_node_id": "read_actuals"},
                {"source_node_id": "read_actuals", "next_node_id": "read_targets"},
                {"source_node_id": "read_targets", "next_node_id": "done"},
            ],
            "start_node_id": "collect_files",
            "terminal_node_ids": ["done"],
            "expected_artifacts": [
                {
                    "artifact_key": "sales_reconciliation_xlsx",
                    "filename": "销售核验报告.xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "content_source": "result.markdown",
                    "required": True,
                }
            ],
        }
        sales_sop_definition = compile_legacy_skill_card(sales_sop_content)
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id="attachment_sales_reconcile_sop",
                version="1.0.0",
                name="附件销售数据核验SOP",
                content_json=sales_sop_content,
                status="published",
            )
        )
        db.add(
            SkillVersion(
                id="skillver_attachment_sales_reconcile_100",
                tenant_id="tenant_demo",
                skill_id="attachment_sales_reconcile_sop",
                version="1.0.0",
                name="附件销售数据核验SOP",
                content_json=sales_sop_content,
                status="published",
                compiled_definition_checksum=sales_sop_definition.checksum,
            )
        )

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
                id="publication_admin_e2e",
                tenant_id="tenant_demo",
                username="publication-admin",
                display_name="E2E Publication Administrator",
                role="admin",
                password_hash=legacy_password_hash("publication-admin"),
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
                id="agent_e2e_expert_template",
                tenant_id="tenant_demo",
                name="E2E 数据治理专家",
                description="用于验证专家广场直接使用与创建能力分身的内置专家模板。",
                persona_prompt="以数据治理专家身份分析数据质量、口径和整改优先级。",
                status="active",
                owner_user_id="admin",
                published_to_gallery=True,
                gallery_published_by="admin",
                agent_category_code="professional",
                visibility_scope="tenant",
                metadata_json={
                    "owner_user_id": "admin",
                    "owner_username": "admin",
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "expert_source_label": "项目内置专家快照",
                    "expert_category": "数据与分析",
                    "expert_subcategory": "数据治理",
                    "expert_tags": ["数据治理", "质量分析"],
                    "expert_name_original": "Data Governance Expert",
                    "upstream_path": "data/data-governance-expert.md",
                    "upstream_commit": "3c9588880b7cafaec325a104899fd1fd854e73c8f1",
                    "upstream_license": "MIT",
                    "import_batch_id": "e2e-expert-snapshot",
                    "owner_semantics": "technical_import_admin",
                    "governance_template": True,
                    "published_to_gallery": True,
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_expert_security",
                tenant_id="tenant_demo",
                name="E2E 安全架构专家",
                description="来自项目内置专家快照，用于验证安全边界和威胁建模上下文。",
                persona_prompt="以安全架构专家身份分析信任边界、威胁模型、最小权限和可验证缓解措施。",
                status="active",
                owner_user_id="admin",
                published_to_gallery=True,
                gallery_published_by="admin",
                agent_category_code="professional",
                visibility_scope="tenant",
                metadata_json={
                    "owner_user_id": "admin",
                    "owner_username": "admin",
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "expert_source_label": "项目内置专家快照",
                    "expert_category": "安全",
                    "expert_subcategory": "安全架构",
                    "expert_tags": ["威胁建模", "安全架构"],
                    "expert_name_original": "Security Architect",
                    "upstream_path": "security/security-architect.md",
                    "upstream_commit": "459dce837db3bdfdc4763d3fefd1fd854e73c8f1",
                    "upstream_license": "MIT",
                    "import_batch_id": "e2e-expert-snapshot",
                    "owner_semantics": "technical_import_admin",
                    "governance_template": True,
                    "published_to_gallery": True,
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_expert_dbre",
                tenant_id="tenant_demo",
                name="E2E 数据库可靠性专家",
                description="来自项目内置专家快照，用于验证 SQLite/MySQL 双数据库可靠性上下文。",
                persona_prompt="以数据库可靠性专家身份分析可用性、备份恢复、幂等、迁移和 SQLite/MySQL 差异。",
                status="active",
                owner_user_id="admin",
                published_to_gallery=True,
                gallery_published_by="admin",
                agent_category_code="professional",
                visibility_scope="tenant",
                metadata_json={
                    "owner_user_id": "admin",
                    "owner_username": "admin",
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "expert_source_label": "项目内置专家快照",
                    "expert_category": "工程",
                    "expert_subcategory": "数据库可靠性",
                    "expert_tags": ["数据库", "高可用", "迁移"],
                    "expert_name_original": "Database Reliability Engineer",
                    "upstream_path": "engineering/engineering-database-reliability-engineer.md",
                    "upstream_commit": "459dce837db3bdfdc4763d3fefd1fd854e73c8f1",
                    "upstream_license": "MIT",
                    "import_batch_id": "e2e-expert-snapshot",
                    "owner_semantics": "technical_import_admin",
                    "governance_template": True,
                    "published_to_gallery": True,
                },
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
                id="agent_e2e_pending_organization",
                tenant_id="tenant_demo",
                name="E2E 待组织化分身",
                description="用于验证能力分身配置组织关系后再提交组织审核。",
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
                id="agent_e2e_rejected_organization",
                tenant_id="tenant_demo",
                name="E2E 被拒组织员工",
                description="用于验证管理员拒绝组织发布申请后的状态闭环。",
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
                id="agent_e2e_ab_org_control",
                tenant_id="tenant_demo",
                name="E2E 组织员工 A/B 对照",
                description="组织数字员工 Skill 四象限回归的无 Skill 对照。",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "organization_ab_control",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_ab_org_treatment",
                tenant_id="tenant_demo",
                name="E2E 组织员工 A/B 处理",
                description="组织数字员工 Skill 四象限回归的有 Skill 处理。",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "organization_ab_treatment",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_q1_diagnosis_positive",
                tenant_id="tenant_demo",
                name="Q1 隔离诊断正向分身",
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
                id="agent_q1_plain",
                tenant_id="tenant_demo",
                name="Q1 纯对话基线分身",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "no_skill_no_attachment_no_tool_no_knowledge",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_q1_cross_turn",
                tenant_id="tenant_demo",
                name="Q1 跨轮附件隔离分身",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "cross_turn_no_skill_no_knowledge",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_q1_diagnosis_control",
                tenant_id="tenant_demo",
                name="Q1 诊断无 Skill 对照分身",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "diagnosing_control_no_skill",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_q1_writing_control",
                tenant_id="tenant_demo",
                name="Q1 writing-for-agents 无 Skill 对照分身",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "writing_control_no_skill",
                },
            )
        )
        db.add(
            AgentProfile(
                id="agent_q1_codebase_control",
                tenant_id="tenant_demo",
                name="Q1 codebase-design 无 Skill 对照分身",
                status="active",
                owner_user_id="member_e2e",
                metadata_json={
                    "owner_user_id": "member_e2e",
                    "owner_username": "member",
                    "q1_resource_policy": "codebase_control_no_skill",
                },
            )
        )
        db.flush()
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            "agent_e2e_employee",
            "tool",
            destructive_tool.id,
            "active",
        )
        sales_skill = db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id == "attachment_sales_reconcile_sop",
            )
        ).one()
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            "agent_e2e_member_employee",
            "skill",
            sales_skill.id,
            "active",
        )
        db.add(
            ChatSession(
                id="session_attachment_sales_sop",
                tenant_id="tenant_demo",
                user_id="member_e2e",
                agent_id="agent_e2e_member_employee",
                title="附件销售数据核验SOP",
                active_skill_id="attachment_sales_reconcile_sop",
                active_step_id="collect_files",
                status="active",
            )
        )
        db.add(
            ChatSession(
                id="session_attachment_sales_sop_missing_slot",
                tenant_id="tenant_demo",
                user_id="member_e2e",
                agent_id="agent_e2e_member_employee",
                title="附件销售数据核验SOP缺槽反例",
                active_skill_id="attachment_sales_reconcile_sop",
                active_step_id="collect_files",
                status="active",
            )
        )
        db.add(
            ChatSession(
                id="session_attachment_sales_sop_missing_column",
                tenant_id="tenant_demo",
                user_id="member_e2e",
                agent_id="agent_e2e_member_employee",
                title="附件销售数据核验SOP缺列反例",
                active_skill_id="attachment_sales_reconcile_sop",
                active_step_id="collect_files",
                status="active",
            )
        )
        db.add(
            AgentProfile(
                id="agent_e2e_member_two",
                tenant_id="tenant_demo",
                name="E2E B 问卷采用分身",
                status="active",
                owner_user_id="member_two_e2e",
                metadata_json={
                    "owner_user_id": "member_two_e2e",
                    "owner_username": "member-two",
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
        organization_agent = db.get(AgentProfile, "agent_e2e_employee")
        if organization_agent is None:
            raise RuntimeError("Organization E2E agent was not created")
        organization_agent.responsible_org_unit_id = root.id
        db.add(organization_agent)
        pending_organization_agent = db.get(AgentProfile, "agent_e2e_pending_organization")
        if pending_organization_agent is None:
            raise RuntimeError("Pending organization E2E agent was not created")
        pending_organization_agent.responsible_org_unit_id = root.id
        db.add(pending_organization_agent)
        rejected_organization_agent = db.get(AgentProfile, "agent_e2e_rejected_organization")
        if rejected_organization_agent is None:
            raise RuntimeError("Rejected organization E2E agent was not created")
        rejected_organization_agent.responsible_org_unit_id = root.id
        db.add(rejected_organization_agent)
        ab_organization_agents = [
            db.get(AgentProfile, "agent_e2e_ab_org_control"),
            db.get(AgentProfile, "agent_e2e_ab_org_treatment"),
        ]
        if any(agent is None for agent in ab_organization_agents):
            raise RuntimeError("Organization A/B E2E agents were not created")
        for agent in ab_organization_agents:
            assert agent is not None
            agent.responsible_org_unit_id = root.id
            db.add(agent)
        db.add(
            AgentRoleBinding(
                id="agentrole_e2e_employee",
                tenant_id="tenant_demo",
                agent_id=organization_agent.id,
                business_role_id=role.id,
                assignment_mode="execute",
                supervisor_employee_profile_id=admin_profile.id,
                scope_type="tenant",
                scope_id="*",
                granted_by_user_id="admin",
                status="active",
            )
        )
        db.add(
            AgentRoleBinding(
                id="agentrole_e2e_rejected_organization",
                tenant_id="tenant_demo",
                agent_id=rejected_organization_agent.id,
                business_role_id=role.id,
                assignment_mode="execute",
                supervisor_employee_profile_id=admin_profile.id,
                scope_type="tenant",
                scope_id="*",
                granted_by_user_id="admin",
                status="active",
            )
        )
        for agent, suffix in zip(ab_organization_agents, ("control", "treatment"), strict=True):
            assert agent is not None
            binding_id = f"agentrole_e2e_ab_org_{suffix}"
            revision_id = f"agentpubrev_e2e_ab_org_{suffix}"
            release_id = f"pubrel_e2e_ab_org_{suffix}"
            db.add(
                AgentRoleBinding(
                    id=binding_id,
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    business_role_id=role.id,
                    assignment_mode="execute",
                    supervisor_employee_profile_id=admin_profile.id,
                    scope_type="tenant",
                    scope_id="*",
                    granted_by_user_id="admin",
                    status="active",
                )
            )
            snapshot_checksum = ("e" if suffix == "control" else "f") * 64
            db.add(
                AgentPublicationRevision(
                    id=revision_id,
                    tenant_id="tenant_demo",
                    request_id=f"pubreq_e2e_ab_org_{suffix}",
                    agent_id=agent.id,
                    persona_checksum=("1" if suffix == "control" else "2") * 64,
                    snapshot_checksum=snapshot_checksum,
                    persona_snapshot_json={
                        "name": agent.name,
                        "description": agent.description,
                    },
                    governance_snapshot_json={
                        "responsible_org_unit_id": root.id,
                        "role_binding_id": binding_id,
                    },
                )
            )
            db.add(
                PublicationRelease(
                    id=release_id,
                    tenant_id="tenant_demo",
                    approved_request_id=f"pubreq_e2e_ab_org_{suffix}",
                    resource_type="agent",
                    resource_id=agent.id,
                    snapshot_kind="agent",
                    snapshot_id=revision_id,
                    snapshot_checksum=snapshot_checksum,
                    status="active",
                )
            )
        db.add(
            AgentPublicationRevision(
                id="agentpubrev_e2e_employee",
                tenant_id="tenant_demo",
                request_id="pubreq_e2e_employee",
                agent_id=organization_agent.id,
                persona_checksum="b" * 64,
                snapshot_checksum="a" * 64,
                persona_snapshot_json={
                    "name": organization_agent.name,
                    "description": organization_agent.description,
                },
                governance_snapshot_json={
                    "responsible_org_unit_id": root.id,
                    "role_binding_id": "agentrole_e2e_employee",
                },
            )
        )
        current_organization_release = PublicationRelease(
            id="pubrel_e2e_employee",
            tenant_id="tenant_demo",
            approved_request_id="pubreq_e2e_employee",
            resource_type="agent",
            resource_id=organization_agent.id,
            snapshot_kind="agent",
            snapshot_id="agentpubrev_e2e_employee",
            snapshot_checksum="a" * 64,
            status="active",
        )
        db.add(current_organization_release)
        db.flush()
        db.add(
            AgentPublicationRevision(
                id="agentpubrev_e2e_employee_history",
                tenant_id="tenant_demo",
                request_id="pubreq_e2e_employee_history",
                agent_id=organization_agent.id,
                persona_checksum="d" * 64,
                snapshot_checksum="c" * 64,
                persona_snapshot_json={
                    "name": organization_agent.name,
                    "description": "E2E 数字员工的历史组织发布快照。",
                },
                governance_snapshot_json={
                    "responsible_org_unit_id": root.id,
                    "role_binding_id": "agentrole_e2e_employee",
                },
            )
        )
        # OptionalLabelString 的模型默认值会在 INSERT 时把显式 None 恢复为 active；
        # 先临时释放活动槽位，插入历史行后再恢复当前 Release，保持唯一约束成立。
        current_organization_release.active_slot_key = None
        db.add(current_organization_release)
        db.flush()
        historical_organization_release = PublicationRelease(
            id="pubrel_e2e_employee_history",
            tenant_id="tenant_demo",
            approved_request_id="pubreq_e2e_employee_history",
            resource_type="agent",
            resource_id=organization_agent.id,
            snapshot_kind="agent",
            snapshot_id="agentpubrev_e2e_employee_history",
            snapshot_checksum="c" * 64,
            status="unpublished",
            row_version=2,
            terminal_command_id="seed-history-unpublish",
            terminal_by_user_id="admin",
            terminal_reason="为浏览器回滚回归保留的普通下架历史版本",
        )
        db.add(historical_organization_release)
        db.flush()
        historical_organization_release.active_slot_key = None
        db.add(historical_organization_release)
        db.flush()
        current_organization_release.active_slot_key = "active"
        db.add(current_organization_release)
        db.flush()
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
    from app.dynamic_tasks.artifact_renderer import ArtifactRendererService
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

        delivery_content = (
            "# Artifact 安全交付矩阵\n\n"
            "<script>window.__artifactContentXss = true</script>\n"
            "[危险链接](javascript:window.__artifactLinkXss=true)\n"
            "=2+2\n"
            "+cmd|' /C calc'!A0\n"
            "-1+1\n"
            "@SUM(A1:A2)\n"
            "\t=HYPERLINK(\"javascript:alert(1)\")\n"
        )
        delivery_artifacts = (
            (
                "delivery_markdown",
                "<img src=x onerror=window.__artifactFilenameXss=true>.md",
                "text/markdown",
            ),
            ("delivery_text", "安全交付矩阵.txt", "text/plain"),
            ("delivery_csv", "安全交付矩阵.csv", "text/csv"),
        )
        delivery_plan = {
            "goal": "验证文本与CSV交付安全",
            "success_criteria": ["三种文本交付物可下载且内容完整"],
            "steps": [{
                "step_key": "answer",
                "title": "生成安全交付矩阵",
                "kind": "answer",
                "required": True,
                "depends_on": [],
            }],
            "expected_artifacts": [
                {
                    "artifact_key": artifact_key,
                    "filename": filename,
                    "mime_type": mime_type,
                    "content_source": "result.markdown",
                    "required": True,
                }
                for artifact_key, filename, mime_type in delivery_artifacts
            ],
            "budget": {"max_model_calls": 1, "max_steps": 1},
        }
        delivery_checksum = canonical_checksum(delivery_plan)
        delivery_session = ChatSession(
            id="session_e2e_artifact_delivery_matrix",
            tenant_id=tenant_id,
            user_id="member_e2e",
            agent_id="agent_e2e_member_employee",
            agent_profile_revision=1,
            title="Artifact 安全交付矩阵",
            summary="三种文本交付物已生成",
            status="active",
        )
        delivery_instance = SopInstance(
            id="execution_e2e_artifact_delivery_matrix",
            tenant_id=tenant_id,
            session_id=delivery_session.id,
            kind="dynamic_task",
            initiator_user_id="member_e2e",
            agent_id="agent_e2e_member_employee",
            goal_snapshot_json={"goal": delivery_plan["goal"]},
            current_plan_revision_id="plan_e2e_artifact_delivery_matrix",
            current_plan_checksum=delivery_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            budget_snapshot_json=dict(delivery_plan["budget"]),
            current_result_id="result_e2e_artifact_delivery_matrix",
            status="succeeded",
            revision=4,
            started_at=now,
            completed_at=now,
        )
        delivery_node = SopNodeExecution(
            id="node_e2e_artifact_delivery_matrix",
            tenant_id=tenant_id,
            instance_id=delivery_instance.id,
            node_id="answer",
            step_key="answer",
            plan_revision_id=delivery_instance.current_plan_revision_id,
            step_kind="answer",
            title="生成安全交付矩阵",
            status="succeeded",
            started_at=now,
            completed_at=now,
        )
        delivery_result_payload = {
            "markdown": delivery_content,
            "criterion_evidence": {"criterion_01": ["answer"]},
            "pending_questions": [],
        }
        delivery_result = ExecutionResult(
            id=delivery_instance.current_result_id,
            tenant_id=tenant_id,
            execution_id=delivery_instance.id,
            status="verified",
            result_json=delivery_result_payload,
            verification_json={"passed": True},
            checksum=canonical_checksum(delivery_result_payload),
            created_by_step_key="answer",
        )
        delivery_message = Message(
            id="message_e2e_artifact_delivery_matrix",
            tenant_id=tenant_id,
            session_id=delivery_session.id,
            role="assistant",
            content=delivery_content,
            metadata_json={
                "execution_id": delivery_instance.id,
                "result_id": delivery_result.id,
            },
        )
        db.add(delivery_session)
        db.add(delivery_instance)
        db.add(ExecutionPlanRevision(
            id=delivery_instance.current_plan_revision_id,
            tenant_id=tenant_id,
            execution_id=delivery_instance.id,
            revision_number=1,
            reason="initial",
            status="active",
            plan_json=delivery_plan,
            checksum=delivery_checksum,
            capability_snapshot_json=capability,
            capability_checksum=capability_digest,
            activated_at=now,
        ))
        db.add(delivery_node)
        db.add(delivery_result)
        db.add(delivery_message)
        db.flush()
        renderer = ArtifactRendererService(db)
        delivery_artifact_ids: list[str] = []
        for artifact_key, filename, mime_type in delivery_artifacts:
            job, _ = renderer.ensure_job(
                instance=delivery_instance,
                result_id=delivery_result.id,
                result_checksum=delivery_result.checksum,
                source_node=delivery_node,
                artifact_key=artifact_key,
                filename=filename,
                mime_type=mime_type,
                required=True,
            )
            claimed = renderer.claim(job, worker_id="e2e-delivery-renderer")
            rendered = renderer.render_and_publish(
                claimed,
                markdown=delivery_content,
                worker_id="e2e-delivery-renderer",
                fencing_token=claimed.fencing_token,
                input_snapshot_ids=(),
            )
            delivery_artifact_ids.append(rendered.id)
        delivery_result.verification_json = {
            "passed": True,
            "artifact_ids": delivery_artifact_ids,
        }
        delivery_message.metadata_json = {
            "execution_id": delivery_instance.id,
            "result_id": delivery_result.id,
            "artifact_ids": delivery_artifact_ids,
        }
        db.add(delivery_result)
        db.add(delivery_message)
        db.add(ExecutionPublication(
            id="publication_e2e_artifact_delivery_matrix",
            tenant_id=tenant_id,
            execution_id=delivery_instance.id,
            result_id=delivery_result.id,
            publication_key=canonical_checksum({
                "execution_id": delivery_instance.id,
                "target_type": "application",
            }),
            target_type="application",
            target_ref=delivery_session.id,
            required=True,
            status="settled",
            receipt_json={"message_id": delivery_message.id},
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
    (diagnosis_repo / "checks" / "diagnosis_red.py").write_text(
        DIAGNOSIS_TEST,
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
                input_properties = {
                    "profile": {
                        "type": "string",
                        "enum": sorted(config["check_profiles"]),
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
                        "applicability": {
                            "mode": "goal_scoped",
                            "domains": ["refund"],
                            "aliases": ["退款"],
                        },
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
                    "passed": {
                        "type": "boolean",
                        "description": (
                            "true表示退出码属于expected_exit_codes且必需输出已命中；"
                            "对diagnosis-red，exit_code=1是预期捕获bug，不是执行故障"
                        ),
                    },
                    "exit_code": {"type": "integer"},
                    "expected_exit_codes": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "required_output_substrings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                [
                    "input.profile",
                    "output.profile",
                    "output.passed",
                    "output.exit_code",
                    "output.expected_exit_codes",
                    "output.required_output_substrings",
                ],
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
                            "checks/diagnosis_red.py",
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
                input_properties = {
                    "profile": {
                        "type": "string",
                        "enum": sorted(config["check_profiles"]),
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
                "agent_e2e_diagnosis",
                "tool",
                tool.id,
                "active",
            )
            if name in {"workspace.memory.read", "workspace.memory.check"}:
                ensure_private_resource_binding(
                    db,
                    "tenant_demo",
                    "agent_q1_diagnosis_positive",
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
        "vision": True,
        "pdf_input": True,
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

    from app.api import model_configs as model_config_api
    from app.llm.client import LLMClient
    from app.llm.schemas import ModelConnectionCheck
    from app.llm.stage_protocol import STAGE_PROTOCOL_KEY

    original_generate_text = LLMClient.generate_text
    original_generate_text_stream = LLMClient.generate_text_stream

    def deterministic_model_catalog(client: LLMClient) -> list[str]:
        """为隔离浏览器测试返回配置自身的目录项，不绕过模型配置 API。"""

        return [client.model]

    def deterministic_connection_probe(client: LLMClient) -> str:
        """只替换外部最小生成响应，保留连接诊断的正式分阶段流程。"""

        return "E2E-CONNECTION-OK"

    LLMClient.probe_model_catalog = deterministic_model_catalog
    LLMClient.probe_text_connection = deterministic_connection_probe

    def deterministic_account_probe(row, checks):  # noqa: ANN001, ANN202
        """隔离浏览器不访问真实余额接口，但仍保留账户阶段的正式响应结构。"""

        checks.append(
            ModelConnectionCheck(
                name="账户状态",
                status="skipped",
                message="确定性浏览器环境不访问供应商余额接口",
            )
        )
        return None

    model_config_api._deepseek_balance_failure = deterministic_account_probe

    def raise_if_model_error_profile() -> None:
        """在专用 profile 中模拟 Provider 失败，验证模型错误不被误归类为产品门禁。"""

        if os.environ.get("FULLSTACK_E2E_PROFILE") == "model-error":
            from app.llm import LLMError

            raise LLMError("MODEL_PROVIDER_QUOTA_EXCEEDED")

    def deterministic_json(
        client: LLMClient,
        system_prompt: str,
        user_payload: dict[str, object],
        *,
        is_cancelled=None,
    ) -> dict[str, object]:
        """按正式阶段协议返回可预测结构，禁止测试直接调用内部 Agent。"""

        from app.cancellation import raise_if_cancelled

        raise_if_cancelled(is_cancelled)
        raise_if_model_error_profile()

        if "附件视觉证据复核器" in system_prompt:
            resources = user_payload.get("reviewed_structural_evidence")
            parts = user_payload.get("_provider_content_parts")
            if not isinstance(resources, list) or not resources or not isinstance(parts, list):
                raise RuntimeError("attachment visual review missed frozen evidence or native parts")
            client._last_completed_response_metadata = {
                "response_id": "e2e-attachment-visual-review",
                "finish_reason": "stop",
                "usage": {"input_tokens": 20, "output_tokens": 20},
            }
            if "ATTACHMENT-VISUAL-CANCEL-DYNAMIC" in str(user_payload):
                for _ in range(80):
                    time.sleep(0.1)
                    raise_if_cancelled(is_cancelled)
            if "ATTACHMENT-VISUAL-CONFLICT-DYNAMIC" in str(user_payload):
                first = next((item for item in resources if isinstance(item, dict)), {})
                return {
                    "observations": [
                        {
                            "snapshot_id": str(first.get("snapshot_id") or ""),
                            "fact_key": "contract.renewal_notice_days",
                            "normalized_value": "90",
                            "locator": {"page": 1, "kind": "visual"},
                            "confidence": 0.99,
                        }
                    ],
                    "conflicts": [
                        {
                            "snapshot_id": str(first.get("snapshot_id") or ""),
                            "fact_key": "contract.renewal_notice_days",
                            "structural_value": "60",
                            "visual_value": "90",
                            "locator": {"page": 1, "kind": "visual"},
                        }
                    ],
                    "gaps": [],
                }
            return {
                "observations": [
                    {
                        "snapshot_id": str(item.get("snapshot_id") or ""),
                        "fact_key": f"visual.snapshot_{index}",
                        "normalized_value": "视觉复核已完成",
                        "locator": {"kind": "native", "ordinal": index},
                        "confidence": 0.99,
                    }
                    for index, item in enumerate(resources)
                    if isinstance(item, dict)
                ],
                "conflicts": [],
                "gaps": [],
            }

        if "Guidance 要求修复器" in system_prompt:
            contract = user_payload.get("loaded_skill_contract", [])
            candidates = user_payload.get("candidate_options", [])
            if not isinstance(contract, list) or not isinstance(candidates, list):
                raise RuntimeError("Guidance repair missed its frozen contract")
            requirements: list[dict[str, object]] = []
            for item in contract:
                if not isinstance(item, dict):
                    continue
                skill_ref = str(item.get("skill_ref") or "").strip()
                candidate = next(
                    (
                        option
                        for option in candidates
                        if isinstance(option, dict)
                        and str(option.get("skill_ref") or "").strip() == skill_ref
                        and str(option.get("principle_candidate_id") or "").strip()
                    ),
                    None,
                )
                if candidate is None:
                    raise RuntimeError(f"Guidance repair missed candidate for {skill_ref}")
                requirements.append(
                    {
                        "skill_ref": skill_ref,
                        "source_kind": candidate["source_kind"],
                        "source_ref": candidate["source_ref"],
                        "principle_candidate_id": candidate["principle_candidate_id"],
                        "task_mapping": "将冻结 Guidance 原则映射到当前交付任务",
                        "observable_acceptance": "最终交付物逐项体现该原则，并由结果验证",
                        "disposition": "apply",
                    }
                )
            return {"guidance_requirements": requirements}

        stage = user_payload.get(STAGE_PROTOCOL_KEY, {})
        phase = str(stage.get("phase") or "") if isinstance(stage, dict) else ""
        if phase == "Router":
            user_message = str(user_payload.get("user_message") or "")
            if "ATTACHMENT-SOP-SALES" in user_message:
                return {
                    "decision": "start_new_task",
                    "target_skill_id": "attachment_sales_reconcile_sop",
                    "target_step_id": "collect_files",
                    "confidence": 0.99,
                    "general_intent": None,
                    "reason": "用户明确启动已发布附件销售核验SOP",
                }
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
        if phase in {"Router / Dynamic Task", "Router / Dynamic Task Shadow"}:
            goal = str(user_payload.get("user_message") or "生成合同巡检结果")
            if (
                "S3" in goal
                or "SKILL-AB-" in goal
                or "EXPERT-SKILL-" in goal
                or "BUILTIN-COMBO-ENGINE-TOGGLE" in goal
                or "SKILL-MATRIX-BASELINE" in goal
                or "SKILL-MATRIX-DYNAMIC" in goal
                or "本轮选定的指南" in goal
                or "请读取本轮CSV" in goal
                or "只核对两份材料中的当前版本号" in goal
            ):
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
                    "reason": "该验证是有界单轮读取，不需要持久动态执行",
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
        if phase == "Step Agent":
            is_attachment_sop = "ATTACHMENT-SOP-SALES" in str(user_payload)
            return {
                "action": "advance" if is_attachment_sop else "reply",
                "reply": None,
                "slot_updates": {},
                "tool_call": None,
                "knowledge_query": None,
                "knowledge_results": [],
                "next_step_id": None,
                "is_step_completed": is_attachment_sop,
                "handoff": False,
            }
        if phase == "Reflection":
            return {
                "action": "pass",
                "needs_retry": False,
                "reason": "确定性SOP输出已由Runtime和回执验证。",
                "target_skill_id": None,
                "target_step_id": None,
                "target_tool_name": None,
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
            loaded_ref_by_base = {
                base: next(
                    (
                        name
                        for name in loaded_names
                        if name == base or name.startswith(f"{base}-")
                    ),
                    "",
                )
                for base in full_delivery_names
            }

            def delivery_refs(*names: str) -> list[str]:
                """把展示名映射到本轮实际加载的唯一机器引用，兼容同名 slug 后缀。"""

                return [loaded_ref_by_base[name] for name in names]
            goal = str(user_payload.get("goal") or "")
            if "只回复查村情相关的问题" in goal:
                return {
                    "goal": goal,
                    "success_criteria": [
                        {
                            "id": "sample_a_prompt_boundary",
                            "type": "assertion",
                            "spec": {
                                "description": "沿用提示词附件和截图语境复核村情问答范围",
                                "required": True,
                            },
                        }
                    ],
                    "constraints": [
                        "只核对提示词附件及其指代的截图语境，不执行外部写入"
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "复核提示词的村情问答边界",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if "你搜一个github，看看有没有能让我参照开发ai-platform-service的项目" in goal:
                return {
                    "goal": goal,
                    "success_criteria": [
                        {
                            "id": "sample_b_runtime_architecture",
                            "type": "assertion",
                            "spec": {
                                "description": "沿用样本 B 的 Hermes Runtime 架构语境完成连续追问",
                                "required": True,
                            },
                        }
                    ],
                    "constraints": [
                        "仅讨论 ai-platform-service、Runtime 抽象、隔离和并发治理，不执行外部写入"
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "复核 Hermes Runtime 架构取舍",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if "No content length specified for stream data" in goal:
                return {
                    "goal": goal,
                    "success_criteria": [
                        {
                            "id": "s3_log_analysis",
                            "type": "assertion",
                            "spec": {
                                "description": "解释 S3 上传警告的来源并给出修复建议",
                                "required": True,
                            },
                        }
                    ],
                    "constraints": ["仅分析日志，不执行外部写入或破坏性操作"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "分析 S3 上传警告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if "EXTERNAL-WRITE-GRAY" in goal:
                capability_ref = next(
                    (
                        str(item.get("name") or "")
                        for item in user_payload.get("capabilities", [])
                        if isinstance(item, dict)
                        and str(item.get("name") or "").startswith("wecom.message_send@")
                    ),
                    "",
                )
                if not capability_ref:
                    raise RuntimeError("external-write planner missed gray capability")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "只向当前绑定的企业微信会话发送一条固定消息；发送前必须逐次审批，"
                        "回执必须包含 delivery_status 和 message_id",
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "send",
                            "title": "发送企业微信 external_write 消息",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [capability_ref],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {
                                "delivery_status_required": True,
                                "message_id_required": True,
                            },
                        },
                        {
                            "draft_id": "answer",
                            "title": "确认企业微信 external_write 回执",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["send"],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                    ],
                    "expected_artifacts": [],
                }
            if "DESTRUCTIVE-GRAY" in goal:
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if "disposable.fixture_delete" not in capability_names:
                    raise RuntimeError("destructive-gray planner missed disposable fixture capability")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "只能调用固定 disposable://fixture/object-1，必须使用目标摘要和远端幂等键",
                        "destructive 仅在每次确认后执行，回执必须包含 effect_status 和 operation_id",
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "destroy",
                            "title": "执行隔离 destructive fixture 单次操作",
                            "kind": "tool.destructive",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["disposable.fixture_delete"],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {"effect_status_required": True},
                        },
                        {
                            "draft_id": "answer",
                            "title": "确认隔离 destructive 回执",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["destroy"],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                    ],
                    "expected_artifacts": [],
                }
            if (
                "ATTACHMENT-FORMULA-MATCH-DYNAMIC" in goal
                or "ATTACHMENT-FORMULA-CONFLICT-DYNAMIC" in goal
            ):
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "公式结论只能引用平台table.compute回执；缓存与重算冲突时必须并列披露"
                    ],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "核验XLSX公式缓存与平台重算证据",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {"formula_evidence_required": True},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if (
                "ATTACHMENT-VISUAL-CONFLICT-DYNAMIC" in goal
                or "ATTACHMENT-VISUAL-CANCEL-DYNAMIC" in goal
            ):
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["结构与视觉证据不一致时必须在最终答案并列披露"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "合并附件双证据并展示冲突",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {"dual_evidence_required": True},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if (
                "G1-A动态" in goal
                or "ATTACHMENT-SKILL-DYNAMIC" in goal
                or "ATTACHMENT-SKILL-MULTI-DYNAMIC" in goal
            ):
                is_attachment_skill = (
                    "ATTACHMENT-SKILL-DYNAMIC" in goal
                    or "ATTACHMENT-SKILL-MULTI-DYNAMIC" in goal
                )
                is_multi_attachment = "ATTACHMENT-SKILL-MULTI-DYNAMIC" in goal
                writing_guidance = next(
                    (
                        str(item.get("name") or "")
                        for item in loaded_guidance
                        if isinstance(item, dict)
                        and str(item.get("name") or "").startswith("writing-for-agents")
                        and any(
                            isinstance(source, dict)
                            and str(source.get("source_ref") or "") == "instructions"
                            and str(source.get("source_checksum") or "")
                            for source in item.get("sources", [])
                        )
                    ),
                    "",
                )
                if not writing_guidance:
                    raise RuntimeError("G1-A dynamic planner missed fixed writing guidance")
                if is_attachment_skill:
                    if not user_payload.get("input_resources"):
                        raise RuntimeError("attachment Skill planner missed frozen input catalog")
                steps = [
                    {
                        "draft_id": "answer",
                        "title": (
                            (
                                "核对多格式发布材料并生成一致性操作规范"
                                if is_multi_attachment
                                else "读取合同证据并生成售后升级操作规范"
                            )
                            if is_attachment_skill
                            else "生成售后升级操作规范"
                        ),
                        "kind": "answer",
                        "required": True,
                        "depends_on": [],
                        "capability_refs": [],
                        "guidance_skill_refs": [writing_guidance],
                        "expected_output_schema": (
                            {"attachment_claims_required": True}
                            if is_attachment_skill
                            and not is_multi_attachment
                            else {}
                        ),
                    }
                ]
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["必须按固定 writing-for-agents 修订形成可执行规范"],
                    "assumptions": [],
                    "guidance_requirements": [
                        {
                            "skill_ref": writing_guidance,
                            "source_kind": "instructions",
                            "source_ref": "instructions",
                            "principle": (
                                "WRITING-FOR-AGENTS-FIXED-COMMIT：使用稳定术语、显式输入输出、"
                                "可验证步骤和异常边界编写 Agent 可消费文档。"
                            ),
                            "task_mapping": "把合同事实转成可执行的操作规范",
                            "observable_acceptance": "最终正文包含输入、步骤、异常和验收标准",
                            "disposition": "apply",
                        }
                    ],
                    "steps": steps,
                    "expected_artifacts": (
                        [
                            {
                                "artifact_key": (
                                    "multi_attachment_skill_report_docx"
                                    if is_multi_attachment
                                    else "contract_skill_report_docx"
                                ),
                                "filename": (
                                    "多格式材料一致性操作规范.docx"
                                    if is_multi_attachment
                                    else "合同续约操作规范.docx"
                                ),
                                "mime_type": (
                                    "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document"
                                ),
                                "content_source": "result.markdown",
                                "required": True,
                            }
                        ]
                        if is_attachment_skill
                        else []
                    ),
                }
            if "BUILTIN-COMBO-DYNAMIC" in goal or "BUILTIN-COMBO-ENGINE-TOGGLE" in goal:
                if not isinstance(loaded_guidance, list) or len(loaded_guidance) != 1:
                    raise RuntimeError(
                        "built-in expert + Skill planner requires exactly one forced built-in Skill"
                    )
                loaded_skill = loaded_guidance[0]
                skill_ref = (
                    str(loaded_skill.get("name") or "").strip()
                    if isinstance(loaded_skill, dict)
                    else ""
                )
                candidate = None
                candidate_catalog = user_payload.get("guidance_principle_candidates", [])
                for skill_catalog in candidate_catalog if isinstance(candidate_catalog, list) else []:
                    if not isinstance(skill_catalog, dict) or skill_catalog.get("skill_ref") != skill_ref:
                        continue
                    for source in skill_catalog.get("sources", []):
                        if not isinstance(source, dict):
                            continue
                        for section in source.get("sections", []):
                            if not isinstance(section, dict):
                                continue
                            candidates = section.get("candidates", [])
                            if isinstance(candidates, list) and candidates:
                                first = candidates[0]
                                if isinstance(first, dict):
                                    candidate = {
                                        "source_kind": source.get("source_kind"),
                                        "source_ref": source.get("source_ref"),
                                        "principle_candidate_id": first.get("principle_candidate_id"),
                                    }
                                    break
                        if candidate is not None:
                            break
                if not skill_ref or not isinstance(candidate, dict):
                    raise RuntimeError("built-in expert + Skill planner missed frozen Skill candidate")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "必须在内置专家身份上下文中消费本轮冻结的内置 Skill，并生成可审计终态"
                    ],
                    "assumptions": [],
                    "guidance_requirements": [
                        {
                            "skill_ref": skill_ref,
                            "source_kind": candidate["source_kind"],
                            "source_ref": candidate["source_ref"],
                            "principle_candidate_id": candidate["principle_candidate_id"],
                            "task_mapping": "将冻结内置 Skill 原则映射到本次专家任务的结果编排",
                            "observable_acceptance": "最终结果包含原则落实证据、完成标准和验收标准",
                            "disposition": "apply",
                        }
                    ],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "形成内置专家与 Skill 的可审计动态结果",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [skill_ref],
                            "expected_output_schema": {},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if "SKILL-MATRIX-DYNAMIC" in goal or "SKILL-MATRIX-TREATMENT" in goal:
                if not isinstance(loaded_guidance, list) or len(loaded_guidance) != 1:
                    raise RuntimeError(
                        "Skill matrix planner requires exactly one forced built-in Skill"
                    )
                loaded_skill = loaded_guidance[0]
                skill_ref = (
                    str(loaded_skill.get("name") or "").strip()
                    if isinstance(loaded_skill, dict)
                    else ""
                )
                candidate = None
                candidate_catalog = user_payload.get("guidance_principle_candidates", [])
                for skill_catalog in candidate_catalog if isinstance(candidate_catalog, list) else []:
                    if not isinstance(skill_catalog, dict) or skill_catalog.get("skill_ref") != skill_ref:
                        continue
                    for source in skill_catalog.get("sources", []):
                        if not isinstance(source, dict):
                            continue
                        for section in source.get("sections", []):
                            if not isinstance(section, dict):
                                continue
                            candidates = section.get("candidates", [])
                            if isinstance(candidates, list) and candidates:
                                first = candidates[0]
                                if isinstance(first, dict):
                                    candidate = {
                                        "source_kind": source.get("source_kind"),
                                        "source_ref": source.get("source_ref"),
                                        "principle_candidate_id": first.get("principle_candidate_id"),
                                    }
                                    break
                        if candidate is not None:
                            break
                if not skill_ref or not isinstance(candidate, dict):
                    raise RuntimeError("Skill matrix planner missed frozen Skill candidate")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": [
                        "必须消费本轮唯一冻结 Skill，并将其原则落实到可审计结果"
                    ],
                    "assumptions": [],
                    "guidance_requirements": [
                        {
                            "skill_ref": skill_ref,
                            "source_kind": candidate["source_kind"],
                            "source_ref": candidate["source_ref"],
                            "principle_candidate_id": candidate["principle_candidate_id"],
                            "task_mapping": "将当前 Skill 原则映射到本次逐项验收任务",
                            "observable_acceptance": "结果包含 Skill 引用、闭环证据和验收结论",
                            "disposition": "apply",
                        }
                    ],
                    "steps": [
                        {
                            "draft_id": "answer",
                            "title": "形成内置 Skill 的可审计动态结果",
                            "kind": "answer",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": [],
                            "guidance_skill_refs": [skill_ref],
                            "expected_output_schema": {},
                        }
                    ],
                    "expected_artifacts": [],
                }
            if "C1远程导入Skill" in goal:
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if "platform.general_skill.propose" not in capability_names:
                    raise RuntimeError("C1 planner missed governed Skill proposal capability")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["Agent 只建议固定 GitHub commit；本人批准前零安装"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "propose_remote_skill",
                            "title": "提交 C1 远程 Skill 导入提案",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["platform.general_skill.propose"],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "报告 C1 Skill 安装结果",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["propose_remote_skill"],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                    ],
                }
            if "S5创建Skill" in goal:
                capability_names = {
                    str(item.get("name") or "")
                    for item in user_payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if "platform.general_skill.propose" not in capability_names:
                    raise RuntimeError("S5 planner missed governed Skill proposal capability")
                return {
                    "goal": goal,
                    "success_criteria": user_payload.get("success_criteria", []),
                    "constraints": ["Agent 只能提出候选，发布必须由所有者在待我处理中心批准"],
                    "assumptions": [],
                    "steps": [
                        {
                            "draft_id": "propose_skill",
                            "title": "提交 S5 Skill 提案",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": [],
                            "capability_refs": ["platform.general_skill.propose"],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "报告 S5 Skill 发布结果",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["propose_skill"],
                            "capability_refs": [],
                            "guidance_skill_refs": [],
                            "expected_output_schema": {},
                        },
                    ],
                }
            if all(loaded_ref_by_base.values()):
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
                            "guidance_skill_refs": delivery_refs(
                                "setup-matt-pocock-skills",
                                "grill-with-docs",
                                "grilling",
                                "domain-modeling",
                            ),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "spec_tickets",
                            "title": "发布退款审批可验证规格与带 blocking edges 的纵向票据",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["setup_domain"],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": delivery_refs(
                                "setup-matt-pocock-skills", "to-spec", "to-tickets"
                            ),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "read",
                            "title": "读取退款实现",
                            "kind": "tool.read",
                            "required": True,
                            "depends_on": ["spec_tickets"],
                            "capability_refs": ["workspace.refund.read"],
                            "guidance_skill_refs": delivery_refs("implement"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "red",
                            "title": "证明退款回归在修复前失败",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["read"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": delivery_refs("tdd"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "apply",
                            "title": "写入退款审批、迁移和前端补丁",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["red"],
                            "capability_refs": ["workspace.refund.apply-set"],
                            "guidance_skill_refs": delivery_refs("implement", "tdd"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "check",
                            "title": "运行退款回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["apply"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": delivery_refs("tdd"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "frontend_check",
                            "title": "运行退款前端状态回归",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["check"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": delivery_refs("tdd"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "review",
                            "title": "按 Standards 与 Spec 两轴完成代码审查",
                            "kind": "tool.execute",
                            "required": True,
                            "depends_on": ["frontend_check"],
                            "capability_refs": ["workspace.refund.check"],
                            "guidance_skill_refs": delivery_refs("code-review"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "commit",
                            "title": "提交一次性任务分支",
                            "kind": "tool.write",
                            "required": True,
                            "depends_on": ["review"],
                            "capability_refs": ["workspace.refund.commit"],
                            "guidance_skill_refs": delivery_refs("implement"),
                            "expected_output_schema": {},
                        },
                        {
                            "draft_id": "answer",
                            "title": "形成代码交付报告",
                            "kind": "answer",
                            "required": True,
                            "depends_on": ["commit"],
                            "capability_refs": [],
                            "guidance_skill_refs": sorted(loaded_ref_by_base.values()),
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
                    capability in str(user_payload)
                    for capability in (
                        "workspace.refund.read",
                        "workspace.refund.apply-set",
                        "workspace.refund.check",
                        "workspace.refund.commit",
                    )
                )
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
            if "只回复查村情相关的问题" in str(user_payload) and step_kind == "answer":
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                current_goal = str(execution_context.get("goal") or "")
                if "这份提示词没用" in current_goal or "这份提示词没用" in str(user_payload):
                    markdown = (
                        "样本 A 第二轮复核：已将“这份提示词”解析为上一轮上传的提示词及其截图语境，"
                        "继续核对查村情范围限制；未把本轮指代误当成新的无关任务。"
                    )
                else:
                    markdown = (
                        "样本 A 首轮复核：已读取提示词附件，确认回答范围应限制为查村情相关问题；"
                        "其他需求统一回复当前应用不支持该功能。"
                    )
                client._last_completed_response_metadata = {
                    "response_id": "e2e-sample-a-prompt-boundary-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 20, "output_tokens": 20},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": markdown,
                        "criterion_evidence": {
                            criterion: [str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "根据提示词附件和当前轮指代形成可追溯的范围复核结果",
                }
            if (
                "你搜一个github，看看有没有能让我参照开发ai-platform-service的项目" in str(user_payload)
                and step_kind == "answer"
            ):
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                conversation = execution_context.get("conversation_context", [])
                user_message = next(
                    (
                        str(item.get("content") or "")
                        for item in reversed(conversation)
                        if isinstance(item, dict) and item.get("role") == "user"
                    ),
                    "",
                )
                if "你搜一个github" in user_message:
                    markdown = (
                        "样本 B 首轮：已围绕 ai-platform-service 梳理可参考的 Agent Gateway、"
                        "模型网关和产品化项目，并保留 Hermes Runtime 作为可插拔运行时。"
                    )
                elif "为什么要参考dify" in user_message:
                    markdown = (
                        "样本 B 第二轮：结合已有知识库、SOP 和 agent runtime，重新判断后，"
                        "中间层的核心是 Runtime 抽象、隔离和并发治理；Dify 只能作为产品能力参考，"
                        "不应替代现有运行时。"
                    )
                else:
                    markdown = (
                        "样本 B 后续追问：已沿用前两轮的 Hermes Runtime 与 ai-platform-service 上下文，"
                        "继续完成本轮架构取舍核对。"
                    )
                client._last_completed_response_metadata = {
                    "response_id": "e2e-sample-b-runtime-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 24, "output_tokens": 24},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": markdown,
                        "criterion_evidence": {
                            criterion: [str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "沿用样本 B 的原始多轮上下文完成只读架构分析",
                }
            if (
                (
                    "ATTACHMENT-FORMULA-MATCH-DYNAMIC" in str(user_payload)
                    or "ATTACHMENT-FORMULA-CONFLICT-DYNAMIC" in str(user_payload)
                )
                and step_kind == "answer"
            ):
                provider_view = user_payload.get("provider_execution_view", {})
                messages = (
                    provider_view.get("messages", [])
                    if isinstance(provider_view, dict)
                    else []
                )
                resource_message = next(
                    (
                        item.get("content", {})
                        for item in messages
                        if isinstance(item, dict)
                        and isinstance(item.get("content"), dict)
                        and item["content"].get("input_resources")
                    ),
                    {},
                )
                resources = resource_message.get("input_resources", [])
                resource = resources[0] if isinstance(resources, list) and resources else {}
                elements = resource.get("elements", []) if isinstance(resource, dict) else []
                element = elements[0] if isinstance(elements, list) and elements else {}
                checks = resource.get("formula_checks", []) if isinstance(resource, dict) else []
                if not isinstance(checks, list) or not checks:
                    raise RuntimeError("formula answer missed persisted table.compute checks")
                claims = [
                    {
                        "claim_id": check.get("fact_key"),
                        "text": (
                            f"公式{check.get('cell')}平台重算值为{check.get('computed_value')}"
                        ),
                        "claim_type": "computed",
                        "normalized_value": check.get("computed_value"),
                        "unit": None,
                        "evidence_refs": [
                            {
                                "snapshot_id": resource.get("snapshot_id"),
                                "extraction_id": resource.get("extraction_id"),
                                "read_operation_id": resource.get("read_operation_id"),
                                "slice_checksum": resource.get("slice_checksum"),
                                "element_id": element.get("element_id"),
                                "element_checksum": element.get("content_checksum"),
                                "locator": element.get("locator"),
                            }
                        ],
                        "computation_receipt_id": check.get("computation_receipt_id"),
                        "semantic_review_status": "verified",
                    }
                    for check in checks
                    if isinstance(check, dict) and check.get("status") == "match"
                ]
                conflict = next(
                    (
                        check
                        for check in checks
                        if isinstance(check, dict) and check.get("status") == "conflict"
                    ),
                    None,
                )
                markdown = (
                    "ATTACHMENT-FORMULA-CONFLICT-SUCCESS：D2公式存在冲突，"
                    f"缓存值{conflict.get('cached_value')}，平台重算值"
                    f"{conflict.get('computed_value')}；D3缓存值1.2与平台重算值1.2一致。"
                    if isinstance(conflict, dict)
                    else "ATTACHMENT-FORMULA-MATCH-SUCCESS：D2缓存值0.8与平台重算值0.8一致；"
                    "D3缓存值1.2与平台重算值1.2一致。"
                )
                execution_context = (
                    provider_view.get("execution_context", {})
                    if isinstance(provider_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                client._last_completed_response_metadata = {
                    "response_id": "e2e-attachment-formula-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 20, "output_tokens": 20},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": markdown,
                        "criterion_evidence": {
                            criterion: [str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": claims,
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "只引用平台公式重算回执并显式披露缓存冲突",
                }
            if "ATTACHMENT-VISUAL-CANCEL-DYNAMIC" in str(user_payload):
                client._last_completed_response_metadata = {
                    "response_id": "e2e-attachment-visual-cancel-late-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 16, "output_tokens": 8},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": "ATTACHMENT-VISUAL-CANCEL-SHOULD-NOT-PUBLISH",
                        "criterion_evidence": {},
                        "pending_questions": [],
                        "claims": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "用于验证取消后的迟到结果不会发布",
                }
            if "ATTACHMENT-VISUAL-CONFLICT-DYNAMIC" in str(user_payload):
                if step_kind != "answer" or "visual_review" not in str(user_payload):
                    raise RuntimeError("visual conflict answer missed persisted review evidence")
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                client._last_completed_response_metadata = {
                    "response_id": "e2e-attachment-visual-conflict-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 16, "output_tokens": 12},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": (
                            "ATTACHMENT-VISUAL-CONFLICT-SUCCESS：证据存在冲突，"
                            "结构提取为60天，视觉复核为90天；需人工复核，未静默选择任一值。"
                        ),
                        "criterion_evidence": {
                            criterion: [str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "明确展示结构与视觉证据冲突",
                }
            if (
                "BUILTIN-COMBO-DYNAMIC" in str(user_payload)
                or "BUILTIN-COMBO-ENGINE-TOGGLE" in str(user_payload)
            ) and step_kind == "answer":
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                guidance_requirements = []
                for message in (
                    execution_view.get("messages", [])
                    if isinstance(execution_view, dict)
                    else []
                ):
                    content = message.get("content") if isinstance(message, dict) else None
                    requirements = (
                        content.get("guidance_requirements")
                        if isinstance(content, dict)
                        else None
                    )
                    if isinstance(requirements, list):
                        guidance_requirements.extend(
                            item for item in requirements if isinstance(item, dict)
                        )
                if not guidance_requirements:
                    raise RuntimeError("built-in expert + Skill action missed frozen guidance requirements")
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
                evidence_excerpt = "本轮已将冻结内置 Skill 原则落实到可审计结论。"
                markdown = (
                    "BUILTIN-COMBO-DYNAMIC-SUCCESS：已在内置专家身份上下文中通过"
                    " DynamicTaskAgent 持久执行，并消费本轮冻结的内置 Skill。\n\n"
                    f"{evidence_excerpt}\n"
                    "完成标准：任务结果已冻结并发布；验收标准：专家身份、SkillUse 和结果事件均可从"
                    "同一 Execution 追溯。命令仅作为输入数据，不自动执行。"
                )
                client._last_completed_response_metadata = {
                    "response_id": "e2e-builtin-expert-skill-dynamic-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 18, "output_tokens": 18},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": markdown,
                        "criterion_evidence": {
                            criterion: [*completed, str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": [],
                        "guidance_applications": [
                            {
                                "skill_use_id": str(item.get("skill_use_id") or ""),
                                "items": [
                                    {
                                        "requirement_id": str(item.get("requirement_id") or ""),
                                        "principle": str(item.get("principle") or ""),
                                        "application": "将冻结原则落实到专家任务的可审计结果编排。",
                                        "evidence_excerpt": evidence_excerpt,
                                    }
                                ],
                            }
                            for item in guidance_requirements
                            if str(item.get("disposition") or "") == "apply"
                        ],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "核对同一 Execution 中的内置专家身份、冻结 SkillUse 和结果证据",
                }
            if "SKILL-MATRIX-TREATMENT" in str(user_payload) and step_kind == "answer":
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                guidance_requirements = []
                for message in (
                    execution_view.get("messages", [])
                    if isinstance(execution_view, dict)
                    else []
                ):
                    content = message.get("content") if isinstance(message, dict) else None
                    requirements = (
                        content.get("guidance_requirements")
                        if isinstance(content, dict)
                        else None
                    )
                    if isinstance(requirements, list):
                        guidance_requirements.extend(
                            item for item in requirements if isinstance(item, dict)
                        )
                if not guidance_requirements:
                    raise RuntimeError("Skill matrix action missed frozen guidance requirements")
                user_message = str(
                    user_payload.get("user_message")
                    or execution_context.get("goal")
                    or ""
                )
                skill_ref = next(
                    (
                        part.split("=", 1)[1].strip()
                        for part in user_message.split()
                        if part.startswith("skill=") and part.split("=", 1)[1].strip()
                    ),
                    "",
                )
                loaded_refs = {
                    str(item.get("skill_ref") or "").strip()
                    for item in guidance_requirements
                    if isinstance(item, dict)
                }
                if not skill_ref or loaded_refs != {skill_ref}:
                    raise RuntimeError(
                        f"Skill matrix action received inconsistent Skill refs: expected={skill_ref!r} actual={loaded_refs!r}"
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
                baseline = 76
                score = 96
                gain = score - baseline
                evidence_excerpt = "本轮已将冻结内置 Skill 原则落实到可审计结论。"
                client._last_completed_response_metadata = {
                    "response_id": "e2e-skill-matrix-" + skill_ref,
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 18, "output_tokens": 18},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": (
                            f"SKILL-MATRIX arm=treatment skill={skill_ref} score={score} "
                            f"baseline={baseline} gain={gain} continuity=first_turn\n"
                            "评分：DynamicTaskAgent 路由、唯一固定 Skill、SkillUse、指导原则应用、"
                            "结果校验和持久终态均已通过。\n"
                            f"闭环：Skill={skill_ref}；完成步骤={','.join(completed) or 'answer'}；"
                            f"验收条件={len(criteria)}；{evidence_excerpt}\n"
                            "结论：相对普通对话增益 20 分，且总分超过 93 分。"
                        ),
                        "criterion_evidence": {
                            criterion: [*completed, str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": [],
                        "guidance_applications": [
                            {
                                "skill_use_id": str(item.get("skill_use_id") or ""),
                                "items": [
                                    {
                                        "requirement_id": str(item.get("requirement_id") or ""),
                                        "principle": str(item.get("principle") or ""),
                                        "application": "将冻结原则落实到本项 Skill 的可审计结果编排。",
                                        "evidence_excerpt": evidence_excerpt,
                                    }
                                ],
                            }
                            for item in guidance_requirements
                            if str(item.get("disposition") or "") == "apply"
                        ],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "核对逐项内置 Skill 的冻结修订、指导应用和 DynamicTaskAgent 结果",
                }
            if (
                (
                    "G1-A动态" in str(user_payload)
                    or "ATTACHMENT-SKILL-DYNAMIC" in str(user_payload)
                    or "ATTACHMENT-SKILL-MULTI-DYNAMIC" in str(user_payload)
                )
                and step_kind == "answer"
            ):
                payload_text = str(user_payload)
                is_multi_attachment_skill = "ATTACHMENT-SKILL-MULTI-DYNAMIC" in payload_text
                is_single_attachment_skill = (
                    "ATTACHMENT-SKILL-DYNAMIC" in payload_text
                    and not is_multi_attachment_skill
                )
                if "WRITING-FOR-AGENTS-FIXED-COMMIT" not in payload_text:
                    raise RuntimeError("G1-A dynamic answer missed fixed writing guidance")
                if (
                    is_single_attachment_skill
                    and "Renewal notice: 60 days" not in payload_text
                ):
                    raise RuntimeError("attachment Skill answer missed reviewed PDF element")
                if (
                    is_multi_attachment_skill
                    and "Version 2.4" not in payload_text
                ):
                    raise RuntimeError("multi attachment Skill answer missed reviewed Office elements")
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                completed = [
                    str(item.get("step_key") or "")
                    for item in execution_context.get("completed_steps", [])
                    if isinstance(item, dict) and item.get("step_key")
                ]
                guidance_requirements = []
                for message in (
                    execution_view.get("messages", [])
                    if isinstance(execution_view, dict)
                    else []
                ):
                    content = message.get("content") if isinstance(message, dict) else None
                    requirements = (
                        content.get("guidance_requirements")
                        if isinstance(content, dict)
                        else None
                    )
                    if isinstance(requirements, list):
                        guidance_requirements.extend(
                            item for item in requirements if isinstance(item, dict)
                        )
                claims = []
                if (
                    is_single_attachment_skill
                    or is_multi_attachment_skill
                ):
                    provider_view = user_payload.get("provider_execution_view", {})
                    messages = (
                        provider_view.get("messages", [])
                        if isinstance(provider_view, dict)
                        else []
                    )
                    resource_message = next(
                        (
                            item.get("content", {})
                            for item in messages
                            if isinstance(item, dict)
                            and isinstance(item.get("content"), dict)
                            and item["content"].get("input_resources")
                        ),
                        {},
                    )
                    resources = resource_message.get("input_resources", [])
                    claims = []
                    for index, resource in enumerate(resources if isinstance(resources, list) else []):
                        if not isinstance(resource, dict):
                            continue
                        elements = resource.get("elements", [])
                        element = elements[0] if isinstance(elements, list) and elements else {}
                        claims.append(
                            {
                                "claim_id": f"attachment_fact_{index + 1}",
                                "text": (
                                    "合同要求提前60天通知"
                                    if is_single_attachment_skill
                                    else f"已核验附件 {index + 1} 的结构事实"
                                ),
                                "claim_type": "fact",
                                "normalized_value": (
                                    60
                                    if is_single_attachment_skill
                                    else None
                                ),
                                "unit": (
                                    "days"
                                    if is_single_attachment_skill
                                    else "document"
                                ),
                                "semantic_review_status": "verified",
                                "evidence_refs": [
                                    {
                                        "snapshot_id": resource.get("snapshot_id"),
                                        "extraction_id": resource.get("extraction_id"),
                                        "read_operation_id": resource.get("read_operation_id"),
                                        "slice_checksum": resource.get("slice_checksum"),
                                        "element_id": element.get("element_id"),
                                        "element_checksum": element.get("content_checksum"),
                                        "locator": element.get("locator"),
                                    }
                                ],
                            }
                        )
                client._last_completed_response_metadata = {
                    "response_id": "e2e-g1-a-dynamic-answer",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 10},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": (
                            (
                                "ATTACHMENT-SKILL-MULTI-DYNAMIC-SUCCESS：DOCX与PPTX版本均为2.4，"
                                "图片已作为受管视觉附件纳入证据；已按固定Skill生成包含输入、步骤、"
                                "异常和验收标准的一致性操作规范。"
                                if is_multi_attachment_skill
                                else "ATTACHMENT-SKILL-DYNAMIC-SUCCESS：合同要求提前60天通知；"
                                "已按固定 writing-for-agents 修订生成包含输入、步骤、异常和验收标准的操作规范。"
                            )
                            if (
                                is_single_attachment_skill
                                or is_multi_attachment_skill
                            )
                            else "G1-A-DYNAMIC-CONSUMED-SUCCESS：已按固定 writing-for-agents 修订"
                            "生成包含输入、步骤、异常和验收标准的操作规范。"
                        ),
                        "criterion_evidence": {
                            criterion: [*completed, str(current_step.get("step_key") or "answer")]
                            for criterion in criteria
                        },
                        "pending_questions": [],
                        "claims": claims,
                        "guidance_applications": [
                            {
                                "skill_use_id": str(item.get("skill_use_id") or ""),
                                "items": [
                                    {
                                        "requirement_id": str(item.get("requirement_id") or ""),
                                        "principle": str(item.get("principle") or ""),
                                        "application": (
                                            "按固定原则将受管附件事实整理为输入、步骤、"
                                            "异常和验收标准。"
                                        ),
                                        "evidence_excerpt": (
                                            "已按固定 writing-for-agents 修订生成包含输入、步骤、"
                                            "异常和验收标准的操作规范。"
                                        ),
                                    }
                                ],
                            }
                            for item in guidance_requirements
                            if str(item.get("disposition") or "") == "apply"
                        ],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "按固定 Skill 指令形成可验收动态任务结果",
                }
            is_s4_diagnosis = "agent_e2e_diagnosis" in str(user_payload)
            is_s4 = "S4-DYNAMIC-FULL-GUIDANCE" in str(user_payload)
            if "EXTERNAL-WRITE-GRAY" in str(user_payload):
                client._last_completed_response_metadata = {
                    "response_id": (
                        "e2e-external-write-gray-"
                        f"{step_kind}-{hashlib.sha256(step_title.encode()).hexdigest()[:12]}"
                    ),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 14, "output_tokens": 12},
                }
                if step_kind == "tool.write":
                    capability_refs = (
                        current_step.get("capability_refs", [])
                        if isinstance(current_step, dict)
                        else []
                    )
                    capability_ref = (
                        str(capability_refs[0])
                        if isinstance(capability_refs, list) and capability_refs
                        else ""
                    )
                    if not capability_ref.startswith("wecom.message_send@"):
                        raise RuntimeError("external-write action missed frozen connector capability")
                    return {
                        "action_kind": "call_tool",
                        "arguments": {
                            "content": (
                                "EXTERNAL-WRITE-GRAY：已通过独立审批，发送一条固定的"
                                " DynamicTaskAgent 外部写回归消息。"
                            ),
                        },
                        "capability_ref": capability_ref,
                        "expected_output_schema": {
                            "delivery_status_required": True,
                            "message_id_required": True,
                        },
                        "rationale": "只对当前冻结企业微信会话发起一次已审批外部写入",
                    }
                if step_kind == "answer":
                    execution_view = user_payload.get("provider_execution_view", {})
                    execution_context = (
                        execution_view.get("execution_context", {})
                        if isinstance(execution_view, dict)
                        else {}
                    )
                    completed_steps = execution_context.get("completed_steps", [])
                    completed_step_keys = [
                        str(item.get("step_key") or "")
                        for item in completed_steps
                        if isinstance(item, dict) and item.get("step_key")
                    ]
                    send_step_key = next(
                        (
                            key
                            for key in completed_step_keys
                            if key != str(current_step.get("step_key") or "")
                        ),
                        completed_step_keys[-1] if completed_step_keys else "",
                    )
                    message_ids = [
                        str(
                            item.get("model_output", {}).get("message_id")
                            or ""
                        )
                        for item in completed_steps
                        if isinstance(item, dict)
                        and isinstance(item.get("model_output"), dict)
                        and item.get("model_output", {}).get("message_id")
                    ]
                    criteria = [
                        str(item.get("id") or "")
                        for item in execution_context.get("success_criteria", [])
                        if isinstance(item, dict) and item.get("id")
                    ]
                    receipt = message_ids[-1] if message_ids else ""
                    return {
                        "action_kind": "answer",
                        "arguments": {
                            "markdown": (
                                "EXTERNAL-WRITE-GRAY-SUCCESS：外部写入已在当前企业微信会话"
                                f"完成一次，delivery_status=sent，provider 回执 message_id={receipt or '已记录'}，"
                                "未授予长期自动放行。"
                            ),
                            "criterion_evidence": {
                                criterion: [send_step_key] for criterion in criteria
                            },
                            "pending_questions": [],
                        },
                        "capability_ref": None,
                        "expected_output_schema": {},
                        "rationale": "依据外部 provider 的结构化 delivery_status 与 message_id 回执形成终态",
                    }
            if "DESTRUCTIVE-GRAY" in str(user_payload):
                client._last_completed_response_metadata = {
                    "response_id": (
                        "e2e-destructive-gray-"
                        f"{step_kind}-{hashlib.sha256(step_title.encode()).hexdigest()[:12]}"
                    ),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 14, "output_tokens": 12},
                }
                if step_kind == "tool.destructive":
                    return {
                        "action_kind": "call_tool",
                        "arguments": {
                            "target": "disposable://fixture/object-1",
                            "target_checksum": hashlib.sha256(
                                "disposable://fixture/object-1".encode("utf-8")
                            ).hexdigest(),
                        },
                        "capability_ref": "disposable.fixture_delete",
                        "expected_output_schema": {"effect_status_required": True},
                        "rationale": "只对固定 disposable fixture 发起一次可对账 destructive 操作",
                    }
                if step_kind == "answer":
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
                                "DESTRUCTIVE-GRAY-SUCCESS：已在 disposable 隔离 provider 上完成一次"
                                " destructive 操作，回执包含 effect_status 与 operation_id；生产数据未被触达。"
                            ),
                            "criterion_evidence": {
                                criterion: completed for criterion in criteria
                            },
                            "pending_questions": [],
                        },
                        "capability_ref": None,
                        "expected_output_schema": {},
                        "rationale": "依据冻结目标和 provider 回执形成终态确认",
                    }
            if step_title == "提交 C1 远程 Skill 导入提案":
                client._last_completed_response_metadata = {
                    "response_id": "e2e-c1-remote-proposal-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 10},
                }
                return {
                    "action_kind": "call_tool",
                    "arguments": {
                        "proposal_kind": "remote_import",
                        "source_url": "https://github.com/mattpocock/skills",
                        "revision": "84fdeffd12f2ee307994d1eb6feb48173b6e0502",
                        "source_subpath": "skills/engineering/tdd",
                    },
                    "capability_ref": "platform.general_skill.propose",
                    "expected_output_schema": {},
                    "rationale": "建议安装固定 TDD Skill，等待本人确认冻结预览",
                }
            if step_title == "报告 C1 Skill 安装结果":
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
                    "response_id": "e2e-c1-answer-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 8, "output_tokens": 8},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": "C1-REMOTE-PUBLISHED：固定 TDD Skill 已由本人批准并绑定。",
                        "criterion_evidence": {criterion: completed for criterion in criteria},
                        "pending_questions": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "依据远程导入 Operation 回执报告结果",
                }
            if step_title == "提交 S5 Skill 提案":
                client._last_completed_response_metadata = {
                    "response_id": "e2e-s5-proposal-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 10},
                }
                return {
                    "action_kind": "call_tool",
                    "arguments": {
                        "name": "s5-refund-evidence-review",
                        "description": "复核退款事实并生成带证据的售后结论。",
                        "instructions": (
                            "S5-PROPOSAL-GUIDANCE：先核对订单、物流和退款事实，"
                            "再区分已证实与待确认事项，最后给出可审计结论。"
                        ),
                        "requested_tools": [],
                        "files": [],
                    },
                    "capability_ref": "platform.general_skill.propose",
                    "expected_output_schema": {},
                    "rationale": "把当前分身总结的方法提交所有者审核，不自行发布",
                }
            if step_title == "报告 S5 Skill 发布结果":
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
                    "response_id": "e2e-s5-answer-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 8, "output_tokens": 8},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": "S5-PROPOSAL-PUBLISHED：Skill 已由所有者批准并绑定原分身。",
                        "criterion_evidence": {criterion: completed for criterion in criteria},
                        "pending_questions": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "依据发布 Operation 回执报告结果",
                }
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
                    guidance_requirements = []
                    for message in (
                        execution_view.get("messages", [])
                        if isinstance(execution_view, dict)
                        else []
                    ):
                        content = message.get("content") if isinstance(message, dict) else None
                        requirements = (
                            content.get("guidance_requirements")
                            if isinstance(content, dict)
                            else None
                        )
                        if isinstance(requirements, list):
                            guidance_requirements.extend(
                                item for item in requirements if isinstance(item, dict)
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
                                "并在一次性任务分支形成提交。\n\n"
                                "已按冻结 Guidance 原则完成受管交付，并以各步骤 Operation 回执完成结果验证。"
                            ),
                            "criterion_evidence": {
                                criterion: completed for criterion in criteria
                            },
                            "pending_questions": [],
                            "guidance_applications": [
                                {
                                    "skill_use_id": str(item.get("skill_use_id") or ""),
                                    "items": [
                                        {
                                            "requirement_id": str(
                                                item.get("requirement_id") or ""
                                            ),
                                            "principle": str(item.get("principle") or ""),
                                            "application": (
                                                "将该冻结原则映射到本次受管交付，并以对应"
                                                "步骤的 Operation 回执核验。"
                                            ),
                                            "evidence_excerpt": (
                                                "已按冻结 Guidance 原则完成受管交付，并以各步骤"
                                                "Operation 回执完成结果验证。"
                                            ),
                                        }
                                    ],
                                }
                                for item in guidance_requirements
                                if str(item.get("disposition") or "") == "apply"
                            ],
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
            if step_kind == "answer" and step_title == "分析 S3 上传警告":
                execution_view = user_payload.get("provider_execution_view", {})
                execution_context = (
                    execution_view.get("execution_context", {})
                    if isinstance(execution_view, dict)
                    else {}
                )
                criteria = [
                    str(item.get("id") or "")
                    for item in execution_context.get("success_criteria", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                completed = [
                    str(item.get("step_key") or "")
                    for item in execution_context.get("completed_steps", [])
                    if isinstance(item, dict) and item.get("step_key")
                ]
                current_step_key = str(current_step.get("step_key") or "")
                evidence_steps = [current_step_key] if current_step_key else completed
                client._last_completed_response_metadata = {
                    "response_id": "e2e-s3-log-answer-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 24, "output_tokens": 24},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": (
                            "S3 日志分析：这是 AWS SDK 在上传未知长度的流时发出的内存缓冲警告，"
                            "不是 DynamicTaskAgent 或业务权限错误。请为上传请求提供准确的 Content-Length，"
                            "或使用可重复读取且明确长度的请求体；同时关注大文件上传的内存占用。"
                        ),
                        "criterion_evidence": {criterion: evidence_steps for criterion in criteria},
                        "pending_questions": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "根据日志中的 AWS SDK 警告给出只读分析",
                }
            if step_kind == "answer" and step_title == "生成巡检结果":
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
                    "response_id": "e2e-dynamic-clarification-answer-response",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 12, "output_tokens": 12},
                }
                return {
                    "action_kind": "answer",
                    "arguments": {
                        "markdown": (
                            "合同巡检结果：已按用户确认的未来30天到期范围完成核验，"
                            "结果和范围均可追溯。"
                        ),
                        "criterion_evidence": {criterion: completed for criterion in criteria},
                        "pending_questions": [],
                    },
                    "capability_ref": None,
                    "expected_output_schema": {},
                    "rationale": "依据已完成 clarification 事实形成普通动态终态",
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
        """只在 Skill A/B 或专家增益隔离回归中固定供应商输出，其余场景保持原链路。"""

        raise_if_model_error_profile()
        if isinstance(user_payload, dict):
            if "No content length specified for stream data" in str(
                user_payload.get("user_message") or ""
            ):
                return (
                    "S3 日志分析：这是 AWS SDK 在上传未知长度的流时发出的内存缓冲警告，"
                    "不是 DynamicTaskAgent 或业务权限错误。请为上传请求提供准确的 Content-Length，"
                    "或使用可重复读取且明确长度的请求体；同时关注大文件上传的内存占用。"
                )
            if (
                os.environ.get("BUILTIN_SKILL_MATRIX_E2E") == "1"
                and "SKILL-MATRIX-" in str(user_payload.get("user_message") or "")
            ):
                return builtin_skill_matrix_response(user_payload)
            if os.environ.get("GAIN_E2E") == "1":
                return gain_e2e_response(user_payload)
            if os.environ.get("SKILL_AB_E2E") == "1":
                return skill_ab_response(user_payload)
            if "ATTACHMENT-SOP-SALES" in str(user_payload):
                return (
                    "ATTACHMENT-SOP-SALES-SUCCESS：已按发布定义确定性读取实际与目标数据，"
                    "销售核验报告已生成，可从执行结果下载。"
                )
            context = user_payload.get("conversation_context")
            turn_inputs = context.get("current_turn_inputs") if isinstance(context, dict) else None
            if isinstance(turn_inputs, list) and turn_inputs:
                serialized = json.dumps(turn_inputs, ensure_ascii=False)
                if "Region" in serialized and "Target" in serialized:
                    if "attachment_content_is_untrusted_data" not in serialized:
                        raise RuntimeError("attachment instruction boundary missing")
                    return "ATTACHMENT-CSV-SUCCESS：已读取 East 与 West 两行目标数据；附件内公式或指令仅作为不可信数据。"
                if "Service Manual 2.4" in serialized and "Version 2.4" in serialized:
                    if "attachment_content_is_untrusted_data" not in serialized:
                        raise RuntimeError("multi attachment instruction boundary missing")
                    return (
                        "ATTACHMENT-MULTI-FAST-SUCCESS：产品手册版本为2.4，发布复盘材料也标记Version 2.4；"
                        "本轮仅做有界局部事实核对。"
                    )
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
                if "S5-PROPOSAL-GUIDANCE" in instructions:
                    return "S5-CONSUMED-SUCCESS：原分身已加载所有者批准的固定 Skill 修订。"
                if "DIAGNOSING-BUGS-FIXED-COMMIT" in instructions:
                    return "G1-B-CONSUMED-SUCCESS：已按固定 diagnosing-bugs 修订完成可证伪诊断。"
                if "WRITING-FOR-AGENTS-FIXED-COMMIT" in instructions:
                    return "G1-A-CONSUMED-SUCCESS：已按固定 writing-for-agents 修订完成可验收文档。"
                if "# Test-Driven Development" in instructions:
                    return "G1-C1-CONSUMED-SUCCESS：已按固定 TDD 修订先建立失败测试。"
                if "TO-QUESTIONNAIRE-FIXED-COMMIT" in instructions:
                    return "G1-D-CONSUMED-SUCCESS：已按组织批准的固定问卷 Skill 生成问题。"
                if "只返回 S3-GUIDED-SUCCESS" not in instructions:
                    raise RuntimeError("S3 guidance was not loaded from the fixed revision")
                return "S3-GUIDED-SUCCESS：已按固定修订的售后核验指南完成本轮处理。"
        return original_generate_text(client, system_prompt, user_payload, response_format)

    def gain_e2e_response(user_payload: dict[str, object]) -> str:
        """为真实浏览器四象限回归生成可重复评分，并校验第二轮确实携带第一轮历史。"""

        context = user_payload.get("conversation_context")
        context_dict = context if isinstance(context, dict) else {}
        raw_loaded = user_payload.get("loaded_general_skills")
        if not isinstance(raw_loaded, list):
            raw_loaded = context_dict.get("loaded_general_skills")
        loaded = [item for item in raw_loaded or [] if isinstance(item, dict)]
        identity = str(user_payload.get("employee_identity") or "")
        expert_active = "专家" in identity or "expert" in identity.lower()
        skill_active = bool(loaded)
        user_message = str(user_payload.get("user_message") or "")
        second_turn = "GAIN-E2E-SECOND" in user_message
        if second_turn:
            history = json.dumps(context_dict, ensure_ascii=False)
            if "GAIN-E2E-FIRST" not in history:
                raise RuntimeError("gain evaluation second turn lost the first turn context")
        if expert_active and skill_active:
            arm = "expert+skill"
            score = 7
            benefit = "专家边界与 Skill 执行约束同时生效，形成可复核的组合交付。"
        elif expert_active:
            arm = "expert"
            score = 3
            benefit = "专家身份、岗位边界和专业分析口径已生效。"
        elif skill_active:
            arm = "skill"
            score = 4
            benefit = "Skill 的结构化步骤、约束和验收条件已生效。"
        else:
            arm = "ordinary"
            score = 1
            benefit = "仅保留普通对话的事实、风险和下一步建议。"
        continuity = "verified" if second_turn else "first_turn"
        return (
            f"GAIN-E2E arm={arm} gain_score={score} expert_signal={'active' if expert_active else 'inactive'} "
            f"skill_signal={'active' if skill_active else 'inactive'} continuity={continuity}\n"
            f"收益：{benefit}\n"
            "案例：CASE-GAIN-REFUND-001；高额退款需要审批，重复请求必须幂等，失败需要可回滚。\n"
            "交付：明确事实、风险、动作和验收证据，且不越过租户隔离或 SQLite/MySQL 一致性边界。"
        )

    def builtin_skill_matrix_response(user_payload: dict[str, object]) -> str:
        """为 37 个内置 Skill 的真实浏览器矩阵返回可审计的 100 分制结果。"""

        context = user_payload.get("conversation_context")
        context_dict = context if isinstance(context, dict) else {}
        raw_loaded = user_payload.get("loaded_general_skills")
        if not isinstance(raw_loaded, list):
            raw_loaded = context_dict.get("loaded_general_skills")
        loaded = [item for item in raw_loaded or [] if isinstance(item, dict)]
        user_message = str(user_payload.get("user_message") or "")
        if "SKILL-MATRIX-BASELINE" in user_message:
            if loaded:
                raise RuntimeError("Skill matrix baseline unexpectedly loaded a Skill")
            return (
                "SKILL-MATRIX arm=ordinary skill=none score=76 baseline=76 gain=0 continuity=first_turn\n"
                "评分：普通对话完成基本事实、风险、动作和验收表达；未加载 Skill，未创建 DynamicTaskAgent。"
            )
        if "SKILL-MATRIX-TREATMENT" not in user_message:
            return ""
        skill_token = next(
            (
                part.split("=", 1)[1].strip()
                for part in user_message.split()
                if part.startswith("skill=") and part.split("=", 1)[1].strip()
            ),
            "",
        )
        loaded_names = [str(item.get("name") or "").strip() for item in loaded]
        if len(loaded) != 1 or (skill_token and skill_token not in loaded_names):
            raise RuntimeError(
                f"Skill matrix treatment loaded unexpected Skill: expected={skill_token!r} actual={loaded_names!r}"
            )
        execution_view = user_payload.get("provider_execution_view", {})
        guidance_requirements: list[dict[str, object]] = []
        if isinstance(execution_view, dict):
            for message in execution_view.get("messages", []):
                content = message.get("content") if isinstance(message, dict) else None
                requirements = (
                    content.get("guidance_requirements")
                    if isinstance(content, dict)
                    else None
                )
                if isinstance(requirements, list):
                    guidance_requirements.extend(
                        item for item in requirements if isinstance(item, dict)
                    )
        if not guidance_requirements:
            raise RuntimeError("Skill matrix treatment missed frozen guidance requirements")
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
        baseline = 76
        score = 96
        gain = score - baseline
        client_response_id = "e2e-skill-matrix-" + (skill_token or "unknown")
        return (
            f"SKILL-MATRIX arm=treatment skill={skill_token} score={score} baseline={baseline} "
            f"gain={gain} continuity=first_turn\n"
            "评分：DynamicTaskAgent 路由、唯一固定 Skill、SkillUse、指导原则应用、结果校验和持久终态均已通过。\n"
            f"闭环：Skill={skill_token}；完成步骤={','.join(completed) or 'answer'}；验收条件={len(criteria)}；响应={client_response_id}。\n"
            "结论：相对普通对话增益 20 分，且总分超过 93 分。"
        )

    def skill_ab_response(user_payload: dict[str, object]) -> str:
        """为 Skill 能力增益对照返回固定答案，并验证附件只来自本轮权威输入。"""

        context = user_payload.get("conversation_context")
        context_dict = context if isinstance(context, dict) else {}
        raw_inputs = context_dict.get("current_turn_inputs")
        authoritative_inputs = user_payload.get("authoritative_attachment_evidence")
        attachment_text = json.dumps(
            [raw_inputs, authoritative_inputs], ensure_ascii=False
        )
        has_attachment = (
            isinstance(raw_inputs, list)
            and bool(raw_inputs)
        ) or (
            isinstance(authoritative_inputs, list)
            and bool(authoritative_inputs)
        )
        user_message = str(user_payload.get("user_message") or "")
        is_expert_closed_loop = "EXPERT-SKILL-CLOSED-LOOP" in user_message
        if (
            has_attachment
            and "SKILL-AB-ATTACHMENT-FACT" not in attachment_text
            and not is_expert_closed_loop
        ):
            raise RuntimeError("Skill A/B response did not receive the uploaded attachment")
        raw_loaded = user_payload.get("loaded_general_skills")
        if not isinstance(raw_loaded, list):
            raw_loaded = context_dict.get("loaded_general_skills")
        loaded = [item for item in raw_loaded or [] if isinstance(item, dict)]
        instructions = "\n".join(str(item.get("instructions") or "") for item in loaded)
        name = str(loaded[0].get("name") or "") if loaded else ""
        evidence = "已读取本轮真实附件中的 SKILL-AB-ATTACHMENT-FACT。" if has_attachment else "已核对内联事实。"
        baseline = (
            "SKILL-AB-BASELINE：\n"
            "事实：CASE-AB-REFUND-001 涉及高额退款审批；租户隔离和幂等键是既有约束。\n"
            "风险：当前方案可能绕过审批或让重复请求产生重复退款。\n"
            "下一步：先确认状态转换、幂等边界和回滚条件，再补充可复现验证。\n"
            "验收：高额请求进入待审批，重复请求只产生一个结果，失败路径可回滚。\n"
            f"{evidence}"
        )
        if "EXPERT-SKILL-CLOSED-LOOP" in user_message:
            history = json.dumps(context_dict, ensure_ascii=False)
            if "EXPERT-SKILL-FIRST-TURN" not in history:
                raise RuntimeError("expert Skill closed-loop prior turn was absent from context")
            attachment_evidence = (
                "SKILL-AB-ATTACHMENT-FACT：第二轮附件事实已在当前 Skill 上下文中核对。"
                if has_attachment
                else ""
            )
            return (
                "SKILL-AB-TREATMENT expert-closed-loop：\n"
                f"{baseline}\n"
                "EXPERT-SKILL-CLOSED-LOOP-SUCCESS：已沿用第一轮结论，并在同一会话中完成第二轮复核。\n"
                f"{attachment_evidence}"
            )
        if not loaded:
            return baseline
        if name == "code-review" or "Standards" in instructions and "Spec" in instructions:
            return (
                "SKILL-AB-TREATMENT code-review：\n"
                f"{baseline}\n"
                "Standards 轴：检查事务边界、租户隔离、幂等键和错误处理；按严重级别列出证据缺口。\n"
                "Spec 轴：逐条对照需求、兼容 SQLite/MySQL、回滚和测试覆盖，未证实项保持待验证。\n"
                "交付增益：把事实、风险、证据缺口和验收条件分开，形成可复核审查结论。"
            )
        if name == "implement" or "Implement the work" in instructions:
            return (
                "SKILL-AB-TREATMENT implement：\n"
                f"{baseline}\n"
                "实施步骤：先固定规格和状态模型，再用 /tdd 建立失败测试，完成最小实现，最后用 /code-review 复核。\n"
                "依赖与回滚：先确认迁移、旧状态兼容和幂等键契约；任一步骤失败都保留可回滚边界。\n"
                "完成标准：每个变更行为都有测试，SQLite/MySQL 均通过，且审批与重复请求证据可追溯。"
            )
        if name == "diagnosing-bugs" or "feedback loop" in instructions:
            return (
                "SKILL-AB-TREATMENT diagnosing-bugs：\n"
                f"{baseline}\n"
                "反馈回路：先构造能命中 CASE-AB-REFUND-001 的最小复现，再一次只改变一个变量。\n"
                "假设：H1 是审批状态丢失，H2 是幂等键未持久化，H3 是失败补偿重复执行；每个假设都要有判别探针。\n"
                "停止条件：修复后原始复现变绿，保留失败/成功回执，并清理临时 instrumentation。"
            )
        return f"SKILL-AB-TREATMENT {name}：\n{baseline}\n已按已加载 Skill 的相关原则补充结构化验收。"

    LLMClient.generate_text = deterministic_text

    def deterministic_text_stream(
        client: LLMClient,
        system_prompt: str,
        user_payload: dict[str, object] | str,
        *,
        is_cancelled=None,
    ):
        """让隔离 Skill 场景走真实流式/SSE边界，同时禁止占位密钥访问外部供应商。"""

        raise_if_model_error_profile()
        context = user_payload.get("conversation_context") if isinstance(user_payload, dict) else None
        loaded = user_payload.get("loaded_general_skills") if isinstance(user_payload, dict) else None
        if not isinstance(loaded, list):
            loaded = context.get("loaded_general_skills") if isinstance(context, dict) else None
        turn_inputs = context.get("current_turn_inputs") if isinstance(context, dict) else None
        if (
            os.environ.get("BUILTIN_SKILL_MATRIX_E2E") == "1"
            or os.environ.get("GAIN_E2E") == "1"
            or os.environ.get("SKILL_AB_E2E") == "1"
        ) or (
            isinstance(loaded, list) and loaded
        ) or (
            isinstance(turn_inputs, list) and turn_inputs
        ):
            deterministic_payload = user_payload
            if isinstance(user_payload, dict) and isinstance(loaded, list) and loaded:
                deterministic_payload = {
                    **user_payload,
                    "conversation_context": {
                        **(context if isinstance(context, dict) else {}),
                        "loaded_general_skills": loaded,
                    },
                }
            text = deterministic_text(client, system_prompt, deterministic_payload)
            for index in range(0, len(text), 8):
                if is_cancelled and is_cancelled():
                    from app.llm.client import LLMStreamCancelled

                    raise LLMStreamCancelled("E2E deterministic stream cancelled")
                yield text[index : index + 8]
            return
        yield from original_generate_text_stream(
            client,
            system_prompt,
            user_payload,
            is_cancelled=is_cancelled,
        )

    LLMClient.generate_text_stream = deterministic_text_stream

def seed_connection_browser_fixtures() -> None:
    """准备 Slack 控制面、企业微信消息接入，以及浏览器可办理的 reauth Attention。"""

    import json

    from sqlmodel import Session, select

    from app.connectors.service import ConnectionService
    from app.connectors.slack import SlackCallResult
    from app.connectors.wecom import WeComCallResult
    from app.db import engine
    from app.db.models import (
        AgentConnectionBinding,
        BusinessRole,
        ChatSession,
        ConnectorInboundEvent,
        ConnectorThreadBinding,
        EmployeeProfile,
        EmployeeRoleAssignment,
    )
    from app.organization.permissions import (
        ensure_builtin_permission_catalog,
        sync_role_permissions,
    )
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
        wecom_binding = service.db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == "tenant_demo",
                AgentConnectionBinding.profile_id == wecom_profile.id,
                AgentConnectionBinding.agent_id == "agent_e2e_employee",
            )
        ).one()
        wecom_profile, wecom_binding = service.set_binding_actions(
            tenant_id="tenant_demo",
            profile_id=wecom_profile.id,
            binding_id=wecom_binding.id,
            allowed_actions={"wecom.message_send"},
            expected_profile_revision=wecom_profile.revision,
            expected_binding_revision=wecom_binding.revision,
            actor_user_id="admin",
        )
        # 外部写的连接权限是独立业务权限，不因平台 admin 身份自动旁路；为浏览器
        # 正向回归准备两个真实员工身份，分别承担发起和审批，保持生产鉴权路径不变。
        publication_profile = db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == "tenant_demo",
                EmployeeProfile.user_id == "publication_admin_e2e",
            )
        ).first()
        if publication_profile is None:
            publication_profile = EmployeeProfile(
                id="profile_e2e_publication_admin",
                tenant_id="tenant_demo",
                user_id="publication_admin_e2e",
                employee_id="E2E-PUBLICATION-ADMIN",
                employee_name="E2E Publication Administrator",
            )
            db.add(publication_profile)
            db.flush()
        ensure_builtin_permission_catalog(db, "tenant_demo")
        external_role = db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == "tenant_demo",
                BusinessRole.role_code == "e2e.external_connection_writer",
            )
        ).first()
        if external_role is None:
            external_role = BusinessRole(
                id="role_e2e_external_connection_writer",
                tenant_id="tenant_demo",
                role_code="e2e.external_connection_writer",
                name="E2E 外部连接写入办理人",
                category="cross_functional",
            )
            db.add(external_role)
            db.flush()
        sync_role_permissions(
            db,
            role=external_role,
            permission_codes=["external_connection.write"],
        )
        for employee_profile in (
            db.exec(
                select(EmployeeProfile).where(
                    EmployeeProfile.tenant_id == "tenant_demo",
                    EmployeeProfile.user_id == "admin",
                )
            ).one(),
            publication_profile,
        ):
            assignment = db.exec(
                select(EmployeeRoleAssignment).where(
                    EmployeeRoleAssignment.tenant_id == "tenant_demo",
                    EmployeeRoleAssignment.employee_profile_id == employee_profile.id,
                    EmployeeRoleAssignment.business_role_id == external_role.id,
                    EmployeeRoleAssignment.scope_type == "tenant",
                    EmployeeRoleAssignment.scope_id == "*",
                )
            ).first()
            if assignment is None:
                db.add(
                    EmployeeRoleAssignment(
                        tenant_id="tenant_demo",
                        employee_profile_id=employee_profile.id,
                        business_role_id=external_role.id,
                        scope_type="tenant",
                        scope_id="*",
                        include_descendants=True,
                        granted_by_user_id="admin",
                    )
                )
        db.add(
            ChatSession(
                id="session_e2e_dynamic_external_write",
                tenant_id="tenant_demo",
                user_id="admin",
                agent_id="agent_e2e_employee",
                origin="connector",
                title="Dynamic 外部写浏览器回归",
                status="active",
            )
        )
        db.add(
            ConnectorThreadBinding(
                id="connthread_e2e_dynamic_external_write",
                tenant_id="tenant_demo",
                provider="wecom",
                profile_id=wecom_profile.id,
                sender_ref_hash=hashlib.sha256(
                    "ww-e2e-corp\0e2e-external-user".encode()
                ).hexdigest(),
                encrypted_recipient_ref=encrypt_secret("e2e-external-user"),
                user_id="admin",
                agent_id="agent_e2e_employee",
                session_id="session_e2e_dynamic_external_write",
            )
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
    from app.connectors import service as connection_service_module
    from app.connectors.service import ConnectionService
    from app.connectors.slack import SlackCallResult
    from app.connectors.wecom import WeComCallResult
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

    class BrowserWeCom:
        """模拟固定企业微信账号，记录的回执仍经 ConnectionService 的真实授权链。"""

        def application_info(self, **_credentials: str) -> WeComCallResult:
            """返回与种子账号一致的应用身份，禁止全栈回归访问公网。"""

            return WeComCallResult(
                True,
                {
                    "agent_id": "1000002",
                    "name": "E2E 企业微信消息",
                    "description": "动态 external_write 浏览器回归",
                    "enabled": True,
                },
                granted_scopes=frozenset({"application:read"}),
            )

        def send_text(
            self,
            *,
            recipient_ref: str,
            content: str,
            **_credentials: str,
        ) -> WeComCallResult:
            """返回可审计固定 message_id，证明一次且仅一次到达 provider 边界。"""

            if not recipient_ref or not content:
                return WeComCallResult(False, {}, error_code="WECOM_MESSAGE_INVALID")
            message_id = "e2e-wecom-message-" + hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()[:16]
            return WeComCallResult(
                True,
                {"message_id": message_id, "invalid_user_count": 0},
            )

        def invalidate_credentials(self, **_credentials: str) -> None:
            """隔离 provider 不维护进程级凭据缓存。"""

    # DynamicTaskAgent 通过默认 ConnectionService 创建实例；只在本隔离进程替换
    # 默认 adapter，HTTP 管理面仍使用下方 dependency override 的真实服务对象。
    connection_service_module._DEFAULT_WECOM_ADAPTER = BrowserWeCom()

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
                    "skills-main/skills/productivity/writing-for-agents/SKILL.md",
                    "---\n"
                    "name: writing-for-agents\n"
                    "description: Write precise operational documentation for agent consumption.\n"
                    "---\n# Writing for Agents\n"
                    "WRITING-FOR-AGENTS-FIXED-COMMIT：使用稳定术语、显式输入输出、"
                    "可验证步骤和异常边界编写 Agent 可消费文档。\n",
                )
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
                    "skills-main/skills/productivity/to-questionnaire/SKILL.md",
                    "---\n"
                    "name: to-questionnaire\n"
                    "description: Convert source documents into a structured questionnaire.\n"
                    "disable-model-invocation: true\n"
                    "---\n# To Questionnaire\n"
                    "TO-QUESTIONNAIRE-FIXED-COMMIT：按主题提取事实、缺口和待确认项，"
                    "形成带来源的结构化问卷。\n",
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
                    "skills-main/skills/engineering/diagnosing-bugs/scripts/"
                    "hitl-loop.template.sh",
                    "#!/bin/sh\n# E2E risk fixture: imported as read-only content and never executed.\n",
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
    from app.general_skills import proposals as general_skill_proposals

    general_skill_proposals.get_agent_proposal_remote_fetcher = BrowserRemoteFetcher


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


def seed_skill_demo_agents() -> None:
    """独立初始化 Skill 演示员工，避免与无关分页夹具的启停条件耦合。"""

    from sqlmodel import Session

    from app.db import engine
    from app.general_skills.demo_seed import initialize_skill_five_closure_demo

    with Session(engine) as db:
        initialize_skill_five_closure_demo(
            db,
            tenant_id="tenant_demo",
            owner_username="member",
            adopter_username="member-two",
            reviewer_username="publication-admin",
        )


def seed_context_128k_browser_fixture() -> None:
    """为真实模型浏览器回归写入约 100K token 的既有历史和 128K 租户配置。"""

    from datetime import timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.db.models import ChatSession, Message, UIConfig, utc_now

    now = utc_now()
    session_id = "session_e2e_context_128k"
    sentinel = "CTX128K_BROWSER_SENTINEL"
    filler = "这是一段用于验证历史会话上下文预算的真实回归正文，必须保留在模型请求中。"
    with Session(engine) as db:
        config = db.get(UIConfig, "tenant_demo")
        if config is None:
            config = UIConfig(tenant_id="tenant_demo")
        config.context_token_budget = 128_000
        config.context_compaction_trigger_ratio = 0.95
        config.context_recent_round_limit = 50
        config.long_summary_token_budget = 4_000
        config.medium_summary_token_budget = 4_000
        db.add(config)

        db.add(
            ChatSession(
                id=session_id,
                tenant_id="tenant_demo",
                user_id="admin",
                agent_id="agent_e2e_employee",
                agent_profile_revision=1,
                origin="owned",
                title="真实 128K 上下文浏览器回归",
                status="active",
            )
        )
        for index in range(20):
            role = "user" if index % 2 == 0 else "assistant"
            prefix = f"{sentinel}：历史第 {index + 1} 条；" if index == 0 else f"历史第 {index + 1} 条；"
            content = prefix + filler * 140
            db.add(
                Message(
                    id=f"message_e2e_context_128k_{index:02d}",
                    tenant_id="tenant_demo",
                    session_id=session_id,
                    role=role,
                    content=content,
                    created_at=now + timedelta(microseconds=index),
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
    live_attachment = live_attachment_e2e_enabled()
    if live_attachment:
        assert_live_attachment_model_configured()
    if not reuse_runtime:
        seed_e2e_fixtures()
        seed_skill_demo_agents()
        if os.environ.get("FULLSTACK_E2E_CONTEXT_128K") == "1":
            seed_context_128k_browser_fixture()
        if live_attachment:
            certify_live_dynamic_model()
        seed_managed_workspace_browser_fixture()
        if not live_attachment:
            seed_schedule_dynamic_model()
        seed_dynamic_task_browser_fixtures()
        seed_connection_browser_fixtures()
        if not live_attachment:
            seed_pagination_browser_fixtures()
        seed_large_organization_browser_fixture()
    install_connection_service_override()
    install_general_skill_remote_fetcher_override()
    if not live_attachment:
        install_schedule_llm_override()

    import uvicorn
    from single_port_app import app

    uvicorn.run(app, host="127.0.0.1", port=E2E_PORT, log_level="warning")


if __name__ == "__main__":
    main()
