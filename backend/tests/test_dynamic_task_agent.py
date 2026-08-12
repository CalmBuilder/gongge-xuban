"""
@Time       : 2026/08/10 19:20
@Author     : zhanglp8181
@File       : test_dynamic_task_agent.py
@CallChain  : pytest → DynamicTaskAgent → Execution Store/受控 ToolExecutor
@Description: 验证动态动作、可信 Artifact、实时再授权和崩溃恢复去重。
"""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
from pathlib import Path
import sys

import pytest
from pypdf import PdfWriter
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_private_knowledge_branch, ensure_private_resource_binding
from app.connectors.service import ConnectionService
from app.connectors.runtime import ConnectorRuntimeService
from app.connectors.slack import SlackCallResult
from app.connectors.wecom import WeComCallResult
from app.config import get_settings
from app.db.models import (
    ActionProposalRecord,
    AgentProfile,
    AgentEvent,
    ArtifactInputLink,
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    ConnectorOutboundDelivery,
    ConnectorThreadBinding,
    DynamicTaskQuotaLease,
    ExecutionArtifact,
    ExecutionPlanRevision,
    ExecutionSignal,
    ExecutionPublication,
    ExecutionResult,
    GeneralSkillUse,
    InputResourceSnapshot,
    KnowledgeBase,
    KnowledgeBaseVersion,
    MCPServer,
    Message,
    ModelConfig,
    SopOperation,
    SopWorkItem,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.dynamic_tasks.agent import (
    DynamicRunOutcome,
    DynamicTaskAgent,
    DynamicTaskAgentError,
    _model_lease_ttl_seconds,
)
from app.dynamic_tasks.artifacts import ArtifactAccessDenied, ArtifactService
from app.dynamic_tasks.capability_catalog import (
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    capability_checksum,
    publish_tool_contract,
)
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.worker import (
    _is_terminal_signal_error,
    due_dynamic_task_signals,
    process_dynamic_task_signal,
)
from app.security.encryption import encrypt_secret
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
    DynamicPlanDraft,
    DynamicPlanDraftStep,
    normalize_plan_draft,
)
from app.dynamic_tasks.quotas import DynamicTaskQuotaError
from app.knowledge.schema import KnowledgeSearchResponse
from app.organization.governance import ensure_builtin_governance_catalog
from app.organization.permissions import sync_role_permissions
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.execution_control import ExecutionControlService
from app.session.managed_resources import ManagedInputResourceService
from app.tools.tool_schema import ToolResult
from app.tools.tool_executor import ToolExecutor


class _Catalog:
    """记录 dispatch 前实时再授权次数的受控目录替身。"""

    def __init__(self) -> None:
        self.calls = 0

    def reauthorize_tool(self, snapshot, **kwargs):
        """模拟当前授权仍有效，并保留调用证据。"""

        self.calls += 1
        return object()


class _Executor:
    """返回确定性只读结果并记录真实 adapter 调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, tenant_id, tool_call, **kwargs):
        """模拟一个生产同形 read adapter。"""

        self.calls += 1
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"contracts": ["C-001"]},
        )


class _StartCatalog(_Catalog):
    """为创建入口提供冻结模型和只读能力目录。"""

    def __init__(self, model: ModelConfig, snapshot: CapabilitySnapshot) -> None:
        super().__init__()
        self.model = model
        self.snapshot = snapshot

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """返回测试中已通过 preflight 的同租户模型。"""

        assert tenant_id == self.model.tenant_id
        assert model_config_id == self.model.id
        return self.model

    def list_tools(self, tenant_id: str, agent_id: str) -> list[CapabilitySnapshot]:
        """返回一个已发布 read 工具。"""

        return [self.snapshot]

    def list_general_skills(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str | None = None,
    ) -> list[CapabilitySnapshot]:
        """本测试不提供规划指南，但校验生产调用已携带当前操作者。"""

        assert actor_user_id
        return []

    def list_connector_reads(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> list[CapabilitySnapshot]:
        """本测试不提供外部连接读取能力。"""

        assert actor_user_id
        return []

    def list_connector_writes(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
        session_id: str,
    ) -> list[CapabilitySnapshot]:
        """普通动态任务测试不暴露外部写能力，且验证新目录契约参数完整。"""

        assert tenant_id == self.model.tenant_id
        assert agent_id
        assert actor_user_id
        assert session_id
        return []


class _Planner:
    """记录服务端任务契约并返回固定有界计划。"""

    def __init__(self) -> None:
        """初始化规划调用次数，供入口幂等测试核对。"""

        self.calls = 0

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """按入口传入的目标和成功标准构造规范计划。"""

        self.calls += 1
        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="query_contract",
                    title="读取合同",
                    kind="tool.read",
                    capability_refs=("contract.query",),
                ),
                PlanStep(
                    step_key="answer",
                    title="形成风险简报",
                    kind="answer",
                    depends_on=("query_contract",),
                ),
            ),
            budget={"max_steps": 4},
        )


class _EmptyStartCatalog(_StartCatalog):
    """模拟没有工具、连接、知识或规划指南，但模型本身可执行动态协议的 Agent。"""

    def list_tools(self, tenant_id: str, agent_id: str) -> list[CapabilitySnapshot]:
        """返回空工具目录，验证澄清/回答计划不依赖伪造能力。"""

        return []


class _EmptyCapabilityPlanner(_Planner):
    """在空能力目录下只规划澄清和回答，不引用任何 capability。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """断言目录为空，并返回由 Runtime 原生支持的无工具动态计划。"""

        self.calls += 1
        assert capabilities == []
        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="clarify_scope",
                    title="确认范围",
                    kind="clarification",
                ),
                PlanStep(
                    step_key="answer",
                    title="形成答复",
                    kind="answer",
                    depends_on=("clarify_scope",),
                ),
            ),
            budget={"max_steps": 4},
        )


class _ArtifactPlanner(_Planner):
    """在基础只读计划上声明一个必需 Markdown 交付物。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """复用基础依赖图并加入终态必须满足的 Artifact 契约。"""

        plan = super().create_plan(
            goal=goal,
            success_criteria=success_criteria,
            capabilities=capabilities,
            input_resources=input_resources,
        )
        return plan.model_copy(
            update={
                "expected_artifacts": (
                    {
                        "artifact_key": "risk_brief",
                        "filename": "风险简报.md",
                        "mime_type": "text/markdown",
                        "content_source": "result.markdown",
                        "required": True,
                    },
                )
            }
        )


class _CorruptOnVerifyArtifactService(ArtifactService):
    """模拟对象存储写入后、终态前内容损坏。"""

    def resolve(self, artifact_id: str, *, tenant_id: str, actor_user_id: str):
        """在登记后的强制完整性检查处返回稳定损坏错误。"""

        raise ArtifactAccessDenied("ARTIFACT_INTEGRITY_FAILED")


class _Proposer:
    """返回当前 read 步骤的完整 provider proposal。"""

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, *, view, step):
        """记录机械执行视图存在，并返回固定只读动作。"""

        assert view.execution_context["plan_checksum"]
        assert step.step_key == "query_contract"
        self.calls += 1
        return _response()


class _RunProposer(_Proposer):
    """按当前步骤返回 read 或最终 answer 提案。"""

    def propose(self, *, view, step):
        """为完整推进循环模拟两次独立 provider 完成响应。"""

        self.calls += 1
        if step.kind == "tool.read":
            return _response()
        return CompletedProviderProposal(
            response_id="provider_final_run",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 风险简报\n\n合同证据已核验。",
                    "criterion_evidence": {"criterion_01": ["query_contract"]},
                    "pending_questions": [],
                },
                rationale="形成最终结果",
            ),
        )


class _UsageRunProposer(_RunProposer):
    """为每个 provider 响应附带可持久累计的 token usage。"""

    def propose(self, *, view, step):
        """复用合法动作并加入固定 token 计量事实。"""

        response = super().propose(view=view, step=step)
        return response.model_copy(
            update={"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        )


class _RepairingRunProposer(_RunProposer):
    """首次伪造终态证据、收到修复反馈后返回合法证据。"""

    def propose(self, *, view, step):
        """验证修复轮可见机械失败详情，并为同一 answer 生成新 provider 身份。"""

        if step.kind != "answer":
            return super().propose(view=view, step=step)
        self.calls += 1
        repair_feedback = str(view.messages)
        repairing = "DYNAMIC_RESULT_VERIFICATION_FAILED" in repair_feedback
        return CompletedProviderProposal(
            response_id=("provider_final_repaired" if repairing else "provider_final_invalid"),
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 风险简报\n\n合同证据已核验。",
                    "criterion_evidence": {
                        "criterion_01": [
                            "query_contract" if repairing else "invented_receipt"
                        ]
                    },
                    "pending_questions": [],
                },
                rationale="形成最终结果",
            ),
        )


class _InputRunProposer(_RunProposer):
    """额外验证受管附件正文来自 snapshot 实时解析而非客户端字段。"""

    def propose(self, *, view, step):
        """断言 provider view 只有受控提取文本和 snapshot 引用。"""

        serialized = view.model_dump_json()
        assert "合同正文：续约日期 2026-12-31" in serialized
        assert "storage_locator" not in serialized
        return super().propose(view=view, step=step)


class _NativeMediaRunProposer(_RunProposer):
    """验证已通过 preflight 的图片和 PDF 以原生 part 进入临时 provider view。"""

    def propose(self, *, view, step):
        """断言原生图片不混入机械 ExecutionContext 或持久输入元数据。"""

        assert len(view.native_input_parts) == 2
        image_part, pdf_part = view.native_input_parts
        assert image_part["type"] == "image_url"
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
        assert pdf_part["type"] == "file"
        assert pdf_part["file"]["filename"] == "contract.pdf"
        assert pdf_part["file"]["file_data"].startswith("data:application/pdf;base64,")
        assert "storage_locator" not in view.model_dump_json()
        return super().propose(view=view, step=step)


class _KnowledgePlanner:
    """返回 knowledge→answer 计划，并确认规划器只能看到安全知识元数据。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """验证冻结目录含知识版本但不含正文，再创建有界知识计划。"""

        knowledge = [item for item in capabilities if item.capability_type == "knowledge"]
        assert len(knowledge) == 1
        serialized = knowledge[0].model_dump_json()
        assert "kb_version_1" in serialized
        assert "制度正文" not in serialized
        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="search_policy",
                    title="检索制度证据",
                    kind="knowledge",
                    capability_refs=("knowledge.search",),
                ),
                PlanStep(
                    step_key="answer",
                    title="形成制度答复",
                    kind="answer",
                    depends_on=("search_policy",),
                ),
            ),
            budget={"max_steps": 4},
        )


class _KnowledgeProposer:
    """按知识检索和最终答复两个步骤返回完整 provider 提案。"""

    def __init__(self) -> None:
        """初始化可核对模型调用次数。"""

        self.calls = 0

    def propose(self, *, view, step):
        """为当前步骤生成严格匹配 kind 的动作。"""

        self.calls += 1
        if step.kind == "knowledge":
            return CompletedProviderProposal(
                response_id="provider_knowledge_1",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.QUERY_KNOWLEDGE,
                    capability_ref="knowledge.search",
                    arguments={"query": "差旅报销上限", "desired_evidence": "制度原文"},
                    rationale="需要企业制度证据",
                ),
            )
        return CompletedProviderProposal(
            response_id="provider_knowledge_answer_1",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "差旅报销上限已按制度证据核验。",
                    "criterion_evidence": {"criterion_01": ["search_policy"]},
                    "pending_questions": [],
                },
                rationale="知识证据足以形成答复",
            ),
        )


class _KnowledgeService:
    """记录固定版本检索请求，并返回生产契约同形响应。"""

    def __init__(self) -> None:
        """初始化检索调用计数与最后请求。"""

        self.calls = 0
        self.request = None

    def search(self, request, model_config):
        """验证 Runtime 将冻结知识库与版本同时传给既有检索服务。"""

        self.calls += 1
        self.request = request
        assert request.knowledge_base_ids == ["kb_policy"]
        assert request.knowledge_base_version_ids == ["kb_version_1"]
        return KnowledgeSearchResponse(
            outcome="evidence_found",
            evidence_pack=[{"content": "制度正文：差旅报销上限为 500 元"}],
        )


class _CombinedPlanner:
    """构造附件上下文下 knowledge→双 read→answer 的生产同形计划。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """验证规划期只接收安全附件元数据，并返回有界依赖图。"""

        assert len(input_resources) == 1
        assert input_resources[0]["filename"] == "contract.txt"
        assert "storage_locator" not in str(input_resources)
        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="search_policy",
                    title="检索当前制度",
                    kind="knowledge",
                    capability_refs=("knowledge.search",),
                ),
                PlanStep(
                    step_key="query_contract",
                    title="读取合同台账",
                    kind="tool.read",
                    depends_on=("search_policy",),
                    capability_refs=("contract.query",),
                ),
                PlanStep(
                    step_key="query_risk",
                    title="读取风险登记",
                    kind="tool.read",
                    depends_on=("query_contract",),
                    capability_refs=("risk.query",),
                ),
                PlanStep(
                    step_key="answer",
                    title="生成可核验简报",
                    kind="answer",
                    depends_on=("query_risk",),
                ),
            ),
            expected_artifacts=(
                {
                    "artifact_key": "renewal_risk_brief",
                    "filename": "续约风险简报.md",
                    "mime_type": "text/markdown",
                    "content_source": "result.markdown",
                    "required": True,
                },
            ),
            budget={
                "max_steps": 6,
                "max_tool_calls": 3,
                "max_model_calls": 6,
                "max_input_tokens": 120_000,
                "max_output_tokens": 24_000,
                "max_total_tokens": 144_000,
                "max_runtime_seconds": 900,
            },
        )


class _CombinedProposer:
    """按组合计划返回知识、双工具和最终结果提案并核验附件投影。"""

    def __init__(self) -> None:
        """初始化步骤调用轨迹。"""

        self.calls: list[str] = []

    def propose(self, *, view, step):
        """为每个步骤生成严格匹配的动作，确保附件贯穿临时 provider view。"""

        serialized = view.model_dump_json()
        assert "合同正文：续约日期 2026-12-31" in serialized
        assert "storage_locator" not in serialized
        self.calls.append(step.step_key)
        if step.kind == "knowledge":
            return CompletedProviderProposal(
                response_id="combined_knowledge",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.QUERY_KNOWLEDGE,
                    capability_ref="knowledge.search",
                    arguments={"query": "合同续约制度", "desired_evidence": "当前有效制度"},
                    rationale="先取得制度证据",
                ),
            )
        if step.step_key == "query_contract":
            return _tool_response(
                response_id="combined_contract",
                capability_ref="contract.query",
                arguments={"text": "C-001"},
            )
        if step.step_key == "query_risk":
            return _tool_response(
                response_id="combined_risk",
                capability_ref="risk.query",
                arguments={"numbers": [2, 3]},
            )
        return CompletedProviderProposal(
            response_id="combined_answer",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 续约风险简报\n\n附件、当前制度、合同台账与风险登记均已核验。",
                    "criterion_evidence": {
                        "criterion_01": ["search_policy", "query_contract", "query_risk"]
                    },
                    "pending_questions": [],
                },
                rationale="全部必需证据已经形成闭环",
            ),
        )


class _ClarificationPlanner:
    """返回 clarification→read→answer 计划以验证跨连接恢复。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """构造不依赖运行时重规划的固定澄清计划。"""

        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="clarify_partner",
                    title="补充合作方",
                    kind="clarification",
                ),
                PlanStep(
                    step_key="query_contract",
                    title="读取合同",
                    kind="tool.read",
                    depends_on=("clarify_partner",),
                    capability_refs=("contract.query",),
                ),
                PlanStep(
                    step_key="answer",
                    title="形成风险简报",
                    kind="answer",
                    depends_on=("query_contract",),
                ),
            ),
            budget={"max_steps": 5},
        )


class _ClarificationProposer(_RunProposer):
    """为澄清步骤返回结构化问题，其余步骤复用只读完整闭环提案。"""

    def propose(self, *, view, step):
        """按 clarification/read/answer 顺序返回当前步骤唯一合法动作。"""

        if step.kind == "clarification":
            self.calls += 1
            return CompletedProviderProposal(
                response_id="provider_clarification_1",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.WAIT_INPUT,
                    arguments={
                        "question": "请选择需要核验的合作方",
                        "options": ["星海科技", "云帆数据"],
                    },
                    rationale="缺少合同检索所需合作方",
                ),
            )
        return super().propose(view=view, step=step)


def _snapshot(*, risk_class: str = "read") -> CapabilitySnapshot:
    """构造与 B0.3 checksum 规则一致的冻结能力快照。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "tool_contract",
        "tenant_id": "tenant_demo",
        "name": "contract.query",
        "contract": {"risk_class": risk_class},
        "model_view": {"name": "contract.query"},
        "user_view": {"name": "contract.query"},
        "audit_view": {"name": "contract.query"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


def _output_snapshot() -> CapabilitySnapshot:
    """构造只允许模型读取 contracts 字段、禁止泄漏同级敏感字段的能力快照。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "tool_contract",
        "tenant_id": "tenant_demo",
        "name": "contract.query",
        "contract": {"risk_class": "read"},
        "model_view": {
            "name": "contract.query",
            "output_schema": {
                "type": "object",
                "properties": {
                    "contracts": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "additionalProperties": False,
            },
        },
        "user_view": {"name": "contract.query"},
        "audit_view": {"name": "contract.query"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


def _tool_response(
    *,
    response_id: str,
    capability_ref: str,
    arguments: dict[str, object],
) -> CompletedProviderProposal:
    """为组合闭环构造一个完整结束的指定只读工具提案。"""

    return CompletedProviderProposal(
        response_id=response_id,
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref=capability_ref,
            arguments=arguments,
            rationale="读取完成任务所需的权威事实",
        ),
    )


def _response() -> CompletedProviderProposal:
    """返回一个已经完整结束并通过 schema 解析的 read proposal。"""

    return CompletedProviderProposal(
        response_id="provider_response_1",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref="contract.query",
            arguments={"partner": "星海科技"},
            rationale="需要读取合同事实",
        ),
    )


def _model_capabilities() -> dict[str, object]:
    """返回通过动态协议预检的冻结模型能力。"""

    return {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "structured_output": True,
        "tool_calling": True,
    }


def _instance(db: Session, snapshot: CapabilitySnapshot):
    """创建只含一个 read 步骤的统一动态 Execution。"""

    plan = NormalizedPlan(
        goal="生成续约风险简报",
        success_criteria=(
            SuccessCriterion(id="contract_found", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="query_contract",
                title="读取合同",
                kind="tool.read",
                capability_refs=("contract.query",),
            ),
        ),
        budget={"max_steps": 4},
    )
    return SopExecutionStore(db).start_dynamic_instance(
        tenant_id="tenant_demo",
        session_id="session_demo",
        agent_id="agent_demo",
        initiator_user_id="user_demo",
        plan=plan,
        capability_snapshot={"tools": [snapshot.model_dump(mode="json")]},
    )[0]


class _ConnectorSlackStub:
    """为动态 reauth 闭环提供建档、轮换和一次真实读取的 provider 替身。"""

    def __init__(self) -> None:
        """初始化健康探测和读取计数。"""

        self.read_calls = 0
        self.rate_limit_once = False

    def auth_test(self, _token: str) -> SlackCallResult:
        """始终确认同一 workspace 和必需 scope。"""

        return SlackCallResult(
            True,
            {"ok": True, "team_id": "T-DYNAMIC"},
            granted_scopes=frozenset({"channels:read"}),
        )

    def conversations_info(self, _token: str, *, channel_id: str) -> SlackCallResult:
        """记录恢复后唯一一次 provider read。"""

        self.read_calls += 1
        if self.rate_limit_once and self.read_calls == 1:
            return SlackCallResult(
                False,
                {},
                error_code="SLACK_RATE_LIMITED",
                rate_limited_until=utc_now(),
            )
        return SlackCallResult(
            True,
            {"ok": True, "channel": {"id": channel_id, "name": "contracts"}},
        )


class _ConnectorWeComStub:
    """为动态企业微信只读闭环返回固定应用事实并记录调用次数。"""

    def __init__(self) -> None:
        """初始化调用和缓存撤销计数。"""

        self.calls = 0
        self.invalidations = 0

    def application_info(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> WeComCallResult:
        """验证凭据抵达 adapter 边界并返回不含成员数据的应用投影。"""

        assert corp_id == "corp-dynamic"
        assert corp_secret == "secret-dynamic"
        assert agent_id == "1000002"
        self.calls += 1
        return WeComCallResult(
            True,
            {
                "agent_id": agent_id,
                "name": "企业微信动态应用",
                "description": "动态只读闭环",
                "enabled": True,
                "home_url": "",
            },
            granted_scopes=frozenset({"application:read"}),
        )

    def invalidate_credentials(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> None:
        """记录旧凭据 token 缓存撤销。"""

        assert corp_id and corp_secret and agent_id
        self.invalidations += 1


def _connector_response(capability_name: str) -> CompletedProviderProposal:
    """构造引用冻结 Slack 账号能力的完整 read proposal。"""

    return CompletedProviderProposal(
        response_id="provider_connector_read",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref=capability_name,
            arguments={"channel_id": "C123"},
            rationale="读取合同频道信息",
        ),
    )


def _wecom_connector_response(capability_name: str) -> CompletedProviderProposal:
    """构造无自由参数的企业微信应用信息 read proposal。"""

    return CompletedProviderProposal(
        response_id="provider_wecom_connector_read",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref=capability_name,
            arguments={},
            rationale="读取已绑定企业微信应用状态",
        ),
    )


def test_reauth_signal_resumes_same_connector_read_operation_without_duplicate_dispatch() -> None:
    """验证 token 失效等待、轮换、持久 signal 和原 Operation 恢复形成完整闭环。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            role="admin",
            password_hash="x",
        )
        agent_profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="Slack 数字员工",
            owner_user_id=user.id,
        )
        model = ModelConfig(
            id="model_connector",
            tenant_id="tenant_demo",
            name="Dynamic Model",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=_model_capabilities(),
            capability_checksum=capability_checksum(_model_capabilities()),
            preflight_status="ready",
        )
        db.add_all([Tenant(id="tenant_demo", name="Demo"), user, agent_profile, model])
        db.flush()
        employee = EmployeeProfile(
            id="employee_connector_reader",
            tenant_id="tenant_demo",
            user_id=user.id,
            employee_id="E-CONNECTOR",
            employee_name="连接读取人",
        )
        role = BusinessRole(
            id="role_connector_reader",
            tenant_id="tenant_demo",
            role_code="cross.external_reader",
            name="外部连接读取人",
            category="cross_functional",
        )
        db.add_all([employee, role])
        ensure_builtin_governance_catalog(db, "tenant_demo")
        db.flush()
        sync_role_permissions(
            db,
            role=role,
            permission_codes=["external_connection.read"],
        )
        db.add(
            EmployeeRoleAssignment(
                id="grant_connector_reader",
                tenant_id="tenant_demo",
                employee_profile_id=employee.id,
                business_role_id=role.id,
                scope_type="tenant",
                scope_id="*",
                include_descendants=True,
                granted_by_user_id=user.id,
            )
        )
        slack = _ConnectorSlackStub()
        connection_service = ConnectionService(db, slack=slack)
        profile = connection_service.create_slack_profile(
            tenant_id="tenant_demo",
            display_name="合同工作区",
            token="expired-token",
            required_scopes={"channels:read"},
            actor_user_id=user.id,
        )
        binding = connection_service.bind_agent(
            tenant_id="tenant_demo",
            profile_id=profile.id,
            agent_id=agent_profile.id,
            allowed_scopes={"channels:read"},
            expected_profile_revision=profile.revision,
            actor_user_id=user.id,
        )
        profile.status = "reauth_required"
        profile.health_status = "unhealthy"
        profile.health_error_code = "CONNECTION_TOKEN_EXPIRED"
        profile.revision += 1
        db.add(profile)
        db.commit()
        snapshot = DynamicCapabilityCatalog._slack_channel_snapshot(profile, binding)
        plan = NormalizedPlan(
            goal="读取合同频道",
            success_criteria=(
                SuccessCriterion(id="channel_found", type="assertion", spec={"required": True}),
            ),
            steps=(
                PlanStep(
                    step_key="read_slack_channel",
                    title="读取 Slack 频道",
                    kind="tool.read",
                    capability_refs=(snapshot.name,),
                ),
            ),
            budget={"max_steps": 3, "max_tool_calls": 3},
        )
        instance = SopExecutionStore(db).start_dynamic_instance(
            tenant_id="tenant_demo",
            session_id="session_connector",
            agent_id="agent_demo",
            initiator_user_id=user.id,
            plan=plan,
            capability_snapshot={
                "tools": [],
                "connectors": [snapshot.model_dump(mode="json")],
                "model": {
                    "model_config_id": model.id,
                    "capabilities": _model_capabilities(),
                    "checksum": model.capability_checksum,
                },
            },
        )[0]
        runtime = DynamicTaskAgent(db, connection_service=connection_service)

        blocked = runtime.execute_read_proposal(
            execution_id=instance.id,
            step_key="read_slack_channel",
            completed_response=_connector_response(snapshot.name),
            provider="openai_compatible",
            model="model-demo",
            model_capabilities=_model_capabilities(),
            worker_id="worker_before_reauth",
            actor_user_id=user.id,
        )

        operation = db.exec(select(SopOperation)).one()
        attention = db.exec(select(SopWorkItem)).one()
        assert blocked.error is not None and blocked.error.code == "DYNAMIC_REAUTH_REQUIRED"
        assert operation.status == "prepared"
        assert attention.attention_kind == "reauth"
        assert attention.payload_json["operation_id"] == operation.id
        assert slack.read_calls == 0

        connection_service.rotate_slack_secret(
            tenant_id="tenant_demo",
            profile_id=profile.id,
            token="fresh-token",
            expected_revision=profile.revision,
            actor_user_id=user.id,
        )
        db.commit()
        control = ExecutionControlService(db)
        with control.store.owned(instance, worker_id="reauth_attention"):
            control.resolve_attention(
                instance,
                attention,
                actor_user_id=user.id,
                command_id="reauth_completed_1",
                command="reauthorize",
                expected_revision=attention.revision,
            )
        db.commit()
        signal = db.exec(select(ExecutionSignal)).one()
        restarted = DynamicTaskAgent(db, connection_service=connection_service)
        restarted.run_until_blocked_or_complete = lambda **_kwargs: DynamicRunOutcome(
            "resumed", instance.id
        )
        slack.rate_limit_once = True

        reauth_outcome = process_dynamic_task_signal(
            db,
            signal,
            agent_factory=lambda _db: restarted,
        )
        timer_signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.signal_type == "timer")
        ).one()
        timer_signal.available_at = datetime(2000, 1, 1)
        db.add(timer_signal)
        db.commit()
        assert [item.id for item in due_dynamic_task_signals(db)] == [timer_signal.id]
        outcome = process_dynamic_task_signal(
            db,
            timer_signal,
            agent_factory=lambda _db: restarted,
        )
        replay = restarted.resume_reauth_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_replay",
            actor_user_id=user.id,
        )

        db.refresh(operation)
        db.refresh(signal)
        db.refresh(timer_signal)
        assert reauth_outcome is not None and reauth_outcome.status == "blocked"
        assert outcome is not None, (timer_signal.status, timer_signal.last_error_json)
        assert outcome.status == replay.status == "resumed"
        assert operation.status == "succeeded"
        assert db.exec(select(SopOperation)).all() == [operation]
        assert signal.status == "consumed"
        assert timer_signal.status == "consumed"
        assert slack.read_calls == 2
        event_types = {
            row.event_type
            for row in db.exec(
                select(AgentEvent).where(AgentEvent.aggregate_id == instance.id)
            ).all()
        }
        assert {
            "connection_profile_reauth_required",
            "connection_profile_unhealthy",
            "connection_profile_recovered",
        } <= event_types

        wecom = _ConnectorWeComStub()
        wecom_service = ConnectionService(db, slack=slack, wecom=wecom)
        wecom_profile = wecom_service.create_wecom_profile(
            tenant_id="tenant_demo",
            display_name="企业微信动态应用",
            corp_id="corp-dynamic",
            agent_id="1000002",
            corp_secret="secret-dynamic",
            actor_user_id=user.id,
        )
        wecom_binding = wecom_service.bind_agent(
            tenant_id="tenant_demo",
            profile_id=wecom_profile.id,
            agent_id=agent_profile.id,
            allowed_scopes={"application:read"},
            expected_profile_revision=wecom_profile.revision,
            actor_user_id=user.id,
        )
        db.commit()
        wecom_snapshot = DynamicCapabilityCatalog._wecom_application_snapshot(
            wecom_profile,
            wecom_binding,
        )
        wecom_plan = NormalizedPlan(
            goal="核验企业微信应用状态",
            success_criteria=(
                SuccessCriterion(id="application_found", type="assertion", spec={"required": True}),
            ),
            steps=(
                PlanStep(
                    step_key="read_wecom_application",
                    title="读取企业微信应用",
                    kind="tool.read",
                    capability_refs=(wecom_snapshot.name,),
                ),
            ),
            budget={"max_steps": 2, "max_tool_calls": 1},
        )
        wecom_instance = SopExecutionStore(db).start_dynamic_instance(
            tenant_id="tenant_demo",
            session_id="session_wecom_connector",
            agent_id=agent_profile.id,
            initiator_user_id=user.id,
            plan=wecom_plan,
            capability_snapshot={
                "tools": [],
                "connectors": [wecom_snapshot.model_dump(mode="json")],
                "model": {
                    "model_config_id": model.id,
                    "capabilities": _model_capabilities(),
                    "checksum": model.capability_checksum,
                },
            },
        )[0]
        db.commit()

        wecom_result = DynamicTaskAgent(db, connection_service=wecom_service).execute_read_proposal(
            execution_id=wecom_instance.id,
            step_key="read_wecom_application",
            completed_response=_wecom_connector_response(wecom_snapshot.name),
            provider="openai_compatible",
            model="model-demo",
            model_capabilities=_model_capabilities(),
            worker_id="worker_wecom_read",
            actor_user_id=user.id,
        )

        assert wecom_result.success is True
        assert wecom_result.data["agent_id"] == "1000002"
        assert wecom.calls == 2
        assert len(db.exec(select(SopOperation)).all()) == 2
        assert wecom_snapshot.contract["required_result_evidence_paths"] == [
            "name",
            "enabled",
            "home_url",
        ]
        assert set(wecom_snapshot.model_view["output_schema"]["properties"]) == {
            "name",
            "enabled",
            "home_url",
        }
        assert "corp-dynamic" not in wecom_snapshot.model_dump_json()
        assert "1000002" not in wecom_snapshot.model_dump_json()


def test_completed_read_operation_is_reused_without_second_adapter_call() -> None:
    """验证完成后崩溃/重放只读取 Operation 回执，不重复执行已完成 read。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        snapshot = _snapshot()
        instance = _instance(db, snapshot)
        catalog = _Catalog()
        executor = _Executor()
        agent = DynamicTaskAgent(db, catalog=catalog, tool_executor=executor)
        first = agent.execute_read_proposal(
            execution_id=instance.id,
            step_key="query_contract",
            completed_response=_response(),
            provider="openai_compatible",
            model="model_demo",
            model_capabilities=_model_capabilities(),
            worker_id="worker_1",
            actor_user_id="user_demo",
        )
        db.commit()
        second = agent.execute_read_proposal(
            execution_id=instance.id,
            step_key="query_contract",
            completed_response=_response(),
            provider="openai_compatible",
            model="model_demo",
            model_capabilities=_model_capabilities(),
            worker_id="worker_2",
            actor_user_id="user_demo",
        )
        db.commit()

        operation = db.exec(select(SopOperation)).one()
        assert first.data == second.data == {"contracts": ["C-001"]}
        assert operation.status == "succeeded"
        assert executor.calls == 1
        assert catalog.calls == 1


def test_non_read_frozen_capability_is_rejected_before_dispatch() -> None:
    """验证模型即使提出同名能力，也不能把非 read 快照带入 B1.1。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        instance = _instance(db, _snapshot(risk_class="external_write"))
        executor = _Executor()
        agent = DynamicTaskAgent(db, catalog=_Catalog(), tool_executor=executor)

        try:
            agent.execute_read_proposal(
                execution_id=instance.id,
                step_key="query_contract",
                completed_response=_response(),
                provider="openai_compatible",
                model="model_demo",
                model_capabilities=_model_capabilities(),
                worker_id="worker_1",
                actor_user_id="user_demo",
            )
        except DynamicTaskAgentError as exc:
            assert str(exc) == "DYNAMIC_READ_ONLY_VIOLATION"
        else:
            raise AssertionError("非 read 能力不应进入 adapter")
        assert executor.calls == 0


def test_plan_draft_receives_stable_server_keys_and_bounded_budget() -> None:
    """验证模型只提步骤语义，稳定 key 与预算由服务端生成且重放一致。"""

    draft = DynamicPlanDraft(
        goal="生成续约风险简报",
        success_criteria=(
            SuccessCriterion(id="brief_ready", type="assertion", spec={"required": True}),
        ),
        steps=(
            DynamicPlanDraftStep(
                draft_id="contracts",
                title="查询合同",
                kind="tool.read",
                capability_refs=("contract.query",),
            ),
            DynamicPlanDraftStep(
                draft_id="summary",
                title="形成简报",
                kind="answer",
                depends_on=("contracts",),
            ),
        ),
    )

    first = normalize_plan_draft(draft, max_steps=4, max_tool_calls=2, max_model_calls=4)
    second = normalize_plan_draft(draft, max_steps=4, max_tool_calls=2, max_model_calls=4)

    assert first == second
    assert first.steps[0].step_key.startswith("step_01_")
    assert first.steps[1].depends_on == (first.steps[0].step_key,)
    assert first.budget == {
        "max_steps": 4,
        "max_tool_calls": 2,
        "max_model_calls": 4,
        "max_input_tokens": 120_000,
        "max_output_tokens": 24_000,
        "max_total_tokens": 144_000,
        "max_runtime_seconds": 900,
    }


def test_plan_budget_counts_knowledge_and_tools_as_capability_calls() -> None:
    """规划期与 Runtime 必须一致地把读写工具和 knowledge 计入同一能力调用预算。"""

    draft = DynamicPlanDraft(
        goal="形成证据简报",
        success_criteria=(
            SuccessCriterion(id="brief_ready", type="assertion", spec={"required": True}),
        ),
        steps=(
            DynamicPlanDraftStep(
                draft_id="policy",
                title="检索制度",
                kind="knowledge",
                capability_refs=("knowledge.search",),
            ),
            DynamicPlanDraftStep(
                draft_id="contract",
                title="读取合同",
                kind="tool.read",
                capability_refs=("contract.query",),
                depends_on=("policy",),
            ),
            DynamicPlanDraftStep(
                draft_id="risk",
                title="读取风险",
                kind="tool.read",
                capability_refs=("risk.query",),
                depends_on=("contract",),
            ),
            DynamicPlanDraftStep(
                draft_id="answer",
                title="形成答复",
                kind="answer",
                depends_on=("risk",),
            ),
        ),
    )

    try:
        normalize_plan_draft(draft, max_steps=5, max_tool_calls=2, max_model_calls=6)
    except ValueError as exc:
        assert str(exc) == "动态计划能力调用步骤超过服务端预算"
    else:
        raise AssertionError("知识与工具合计超过预算时必须在计划激活前拒绝")

    accepted = normalize_plan_draft(
        draft,
        max_steps=5,
        max_tool_calls=3,
        max_model_calls=6,
    )
    assert accepted.budget["max_tool_calls"] == 3


def test_plan_attaches_every_loaded_skill_cause_to_capability_steps() -> None:
    """模型即使只在回答步骤引用 Skill，所有能力外呼仍必须携带全部固定 Use 因果。"""

    draft = DynamicPlanDraft(
        goal="读取订单并形成受 Skill 约束的答复",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        steps=(
            DynamicPlanDraftStep(
                draft_id="read",
                title="读取订单",
                kind="tool.read",
                capability_refs=("crm.order.read",),
            ),
            DynamicPlanDraftStep(
                draft_id="answer",
                title="形成答复",
                kind="answer",
                depends_on=("read",),
                guidance_skill_refs=("writing",),
            ),
        ),
    )

    normalized = normalize_plan_draft(
        draft,
        max_steps=4,
        max_tool_calls=2,
        max_model_calls=4,
        guidance_use_ids_by_name={"writing": ("use_a", "use_b")},
    )

    assert normalized.steps[0].guidance_skill_use_ids == ("use_a", "use_b")
    assert normalized.steps[1].guidance_skill_use_ids == ("use_a", "use_b")


def test_start_task_uses_preflight_catalog_and_reuses_same_active_execution() -> None:
    """验证动态入口只从已验证模型/实时目录建计划，相同请求不会产生第二个活动实例。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_demo",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        snapshot = _snapshot()
        planner = _Planner()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, snapshot),
            tool_executor=_Executor(),
            planner=planner,
        )
        first, created = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_start",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        second, repeated_created = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_start",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )

        assert created is True
        assert repeated_created is False
        assert second.id == first.id
        assert planner.calls == 1
        assert first.goal_snapshot_json["success_criteria"][0]["id"] == "criterion_01"


def test_start_task_without_tools_can_create_clarification_and_answer_plan() -> None:
    """空工具目录仍可创建只含澄清/回答的持久动态任务，且冻结快照不伪造能力。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_empty_catalog",
            tenant_id="tenant_demo",
            name="空目录动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        planner = _EmptyCapabilityPlanner()
        agent = DynamicTaskAgent(
            db,
            catalog=_EmptyStartCatalog(model, _snapshot()),
            planner=planner,
        )

        instance, created = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_empty_catalog",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="确认范围后形成答复",
            success_criteria=("获得范围并完成答复",),
            model_config=model,
        )

        assert created is True
        assert planner.calls == 1
        assert instance.status == "running"
        assert instance.capability_snapshot_json["tools"] == []
        assert instance.capability_snapshot_json["connectors"] == []
        assert instance.capability_snapshot_json["knowledge"] == []


def test_advance_connects_plan_provider_view_proposal_and_read_operation() -> None:
    """验证 B1 主链从权威计划机械选步，经唯一 provider view 后落提案并执行 read。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_advance",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        snapshot = _snapshot()
        catalog = _StartCatalog(model, snapshot)
        executor = _Executor()
        proposer = _Proposer()
        agent = DynamicTaskAgent(
            db,
            catalog=catalog,
            tool_executor=executor,
            planner=_Planner(),
            action_proposer=proposer,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_advance",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )

        step, result = agent.advance_next_read_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_advance",
            actor_user_id="user_demo",
        )
        db.commit()

        assert step.step_key == "query_contract"
        assert result.success is True
        assert proposer.calls == 1
        assert executor.calls == 1


def test_completed_read_result_is_schema_projected_into_answer_context() -> None:
    """最终回答模型必须看到工具真实结果，同时不得看到 output schema 未声明字段。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_projected_result",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _output_snapshot()),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=_Proposer(),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_projected_result",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        agent.advance_next_read_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_projected_result",
            actor_user_id="user_demo",
        )
        operation = db.exec(
            select(SopOperation).where(SopOperation.instance_id == instance.id)
        ).one()
        operation.result_json = {
            "data": {
                "contracts": ["C-001"],
                "access_token": "must-not-enter-provider-view",
            }
        }
        db.add(operation)
        db.commit()

        projection = build_execution_context_projection(
            db,
            tenant_id="tenant_demo",
            execution_id=instance.id,
        )

        assert projection.completed_steps[0]["model_output"] == {
            "contracts": ["C-001"]
        }
        assert "must-not-enter-provider-view" not in projection.model_dump_json()


def test_verified_result_message_publication_and_terminal_state_commit_together() -> None:
    """验证成功标准证据通过后，最终消息、publication 和终态形成一个真实闭环。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_result",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        snapshot = _snapshot()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, snapshot),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=_Proposer(),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_result",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        agent.advance_next_read_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_read",
            actor_user_id="user_demo",
        )
        response = CompletedProviderProposal(
            response_id="provider_result_1",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 续约风险简报\n\n合同 C-001 已核验。",
                    "criterion_evidence": {"criterion_01": ["query_contract"]},
                    "pending_questions": [],
                },
                rationale="证据已足够形成结果",
            ),
        )

        proposal_count_before_stale_result = len(db.exec(select(ActionProposalRecord)).all())
        with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_PLAN_REVISION_CHANGED"):
            agent.complete_with_result_proposal(
                execution_id=instance.id,
                step_key="answer",
                completed_response=response,
                provider="openai_compatible",
                model="model-demo",
                model_capabilities=capabilities,
                worker_id="worker_stale_result",
                expected_plan_revision_id="stale-plan-revision",
            )
        assert len(db.exec(select(ActionProposalRecord)).all()) == proposal_count_before_stale_result

        message = agent.complete_with_result_proposal(
            execution_id=instance.id,
            step_key="answer",
            completed_response=response,
            provider="openai_compatible",
            model="model-demo",
            model_capabilities=capabilities,
            worker_id="worker_result",
            expected_plan_revision_id=instance.current_plan_revision_id,
        )

        db.refresh(instance)
        result = db.exec(select(ExecutionResult)).one()
        publication = db.exec(select(ExecutionPublication)).one()
        assert db.exec(select(Message)).one().id == message.id
        assert result.status == "verified"
        assert publication.status == "settled"
        assert publication.receipt_json["message_id"] == message.id
        assert instance.status == "succeeded"


def test_connector_result_waits_for_required_external_publication() -> None:
    """Connector 动态结果先停在 waiting，只有外部 outbox 结算后才能成功终止。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_connector_result",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.add(
            ConnectorThreadBinding(
                id="thread_connector_result",
                tenant_id="tenant_demo",
                provider="wecom",
                profile_id="profile_connector_result",
                sender_ref_hash="a" * 64,
                encrypted_recipient_ref=encrypt_secret("external-user"),
                user_id="user_demo",
                agent_id="agent_demo",
                session_id="session_connector_result",
            )
        )
        db.flush()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=_Proposer(),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_connector_result",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
            source_kind="connector",
        )
        agent.advance_next_read_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_connector_read",
            actor_user_id="user_demo",
        )
        response = CompletedProviderProposal(
            response_id="provider_connector_result",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 验收简报\n\n已核验。",
                    "criterion_evidence": {"criterion_01": ["query_contract"]},
                    "pending_questions": [],
                },
                rationale="证据已完整",
            ),
        )

        agent.complete_with_result_proposal(
            execution_id=instance.id,
            step_key="answer",
            completed_response=response,
            provider="openai_compatible",
            model="model-demo",
            model_capabilities=capabilities,
            worker_id="worker_connector_result",
        )

        db.refresh(instance)
        publications = db.exec(
            select(ExecutionPublication).where(
                ExecutionPublication.execution_id == instance.id
            )
        ).all()
        delivery = db.exec(select(ConnectorOutboundDelivery)).one()
        assert instance.status == "waiting"
        assert {item.target_type: item.status for item in publications} == {
            "application": "settled",
            "external_thread": "pending",
        }
        delivery.status = "unknown"
        delivery.error_json = {"code": "WECOM_DELIVERY_UNKNOWN"}
        db.add(delivery)
        db.commit()
        runtime = ConnectorRuntimeService(db)
        assert runtime.sync_execution_delivery_status(
            delivery,
            worker_id="publication-status",
        ) is True
        db.refresh(instance)
        assert instance.status == "waiting"
        assert db.get(ExecutionPublication, delivery.source_ref).status == "unknown"
        delivery.status = "settled"
        delivery.receipt_json = {"provider_message_id": "remote-1"}
        db.add(delivery)
        db.commit()

        assert runtime.settle_execution_delivery(
            delivery,
            worker_id="publication-settler",
        ) is True
        db.refresh(instance)
        assert instance.status == "succeeded"
        assert db.get(ExecutionPublication, delivery.source_ref).status == "settled"


def test_artifact_integrity_failure_rolls_back_result_message_and_terminal_state(tmp_path) -> None:
    """验证交付物写后校验失败时成功结果、消息和终态必须整体回滚。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_artifact_failure",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        user = User(
            id="user_artifact_failure",
            tenant_id="tenant_demo",
            username="artifact-failure",
            password_hash="x",
        )
        db.add(model)
        db.add(user)
        db.flush()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=_Executor(),
            planner=_ArtifactPlanner(),
            action_proposer=_Proposer(),
            artifact_service=_CorruptOnVerifyArtifactService(db, storage_root=tmp_path),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_artifact_failure",
            agent_id="agent_demo",
            initiator_user_id=user.id,
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        agent.advance_next_read_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_artifact_read",
            actor_user_id=user.id,
        )
        response = CompletedProviderProposal(
            response_id="provider_artifact_failure",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 风险简报\n\n合同证据已核验。",
                    "criterion_evidence": {"criterion_01": ["query_contract"]},
                    "pending_questions": [],
                },
                rationale="证据已足够形成结果",
            ),
        )

        with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_ARTIFACT_REGISTRATION_FAILED"):
            agent.complete_with_result_proposal(
                execution_id=instance.id,
                step_key="answer",
                completed_response=response,
                provider="openai_compatible",
                model="model-demo",
                model_capabilities=capabilities,
                worker_id="worker_artifact_result",
            )

        db.refresh(instance)
        assert instance.status == "running"
        assert db.exec(select(ExecutionArtifact)).all() == []
        assert db.exec(select(ExecutionResult)).all() == []
        assert db.exec(select(ExecutionPublication)).all() == []
        assert db.exec(select(Message)).all() == []


def test_run_loop_serially_reaches_verified_terminal_result() -> None:
    """验证真实多步循环按 read→answer 串行推进，不需要 Agent Loop 复制 Runtime 状态。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_loop",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        snapshot = _snapshot()
        executor = _Executor()
        proposer = _RunProposer()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, snapshot),
            tool_executor=executor,
            planner=_Planner(),
            action_proposer=proposer,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_loop",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )

        outcome = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_loop",
            actor_user_id="user_demo",
        )

        assert outcome.status == "succeeded"
        assert outcome.message is not None
        assert executor.calls == 1
        assert proposer.calls == 2


def test_run_loop_repairs_invalid_result_once_and_settles_skill_use() -> None:
    """终态证据首次不合格时留痕并仅修复一次，成功后同步结算固定 Skill Use。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_result_repair",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        proposer = _RepairingRunProposer()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=proposer,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_result_repair",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        use = GeneralSkillUse(
            id="gsuse_result_repair",
            tenant_id="tenant_demo",
            session_id="session_result_repair",
            turn_id="turn_result_repair",
            execution_id=instance.id,
            agent_id="agent_demo",
            user_id="user_demo",
            skill_id="skill_demo",
            revision_id="revision_demo",
            content_checksum="a" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="result-repair-use",
        )
        db.add(use)
        db.commit()

        outcome = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_result_repair",
            actor_user_id="user_demo",
        )

        proposals = db.exec(
            select(ActionProposalRecord).where(
                ActionProposalRecord.execution_id == instance.id
            )
        ).all()
        db.refresh(use)
        assert outcome.status == "succeeded"
        assert proposer.calls == 3
        assert [row.status for row in proposals].count("superseded") == 1
        assert [row.status for row in proposals].count("consumed") == 2
        assert use.status == "completed"
        assert use.result_summary_json["message_id"] == outcome.message.id


def test_fail_execution_settles_active_skill_use_with_stable_reason() -> None:
    """动态执行确定性失败时 Skill Use 同步失败，且保留稳定错误码。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_failure_settlement",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            planner=_Planner(),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_failure_settlement",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        use = GeneralSkillUse(
            tenant_id="tenant_demo",
            session_id="session_failure_settlement",
            turn_id="turn_failure_settlement",
            execution_id=instance.id,
            agent_id="agent_demo",
            user_id="user_demo",
            skill_id="skill_demo",
            revision_id="revision_demo",
            content_checksum="b" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="failure-settlement-use",
        )
        db.add(use)
        db.commit()

        agent.fail_execution(
            execution_id=instance.id,
            worker_id="worker_failure_settlement",
            error_code="DYNAMIC_RESULT_VERIFICATION_FAILED",
        )

        db.refresh(instance)
        db.refresh(use)
        assert instance.status == "failed"
        assert instance.terminal_reason_json["code"] == "DYNAMIC_RESULT_VERIFICATION_FAILED"
        assert use.status == "failed"
        assert use.invalidation_reason == "DYNAMIC_RESULT_VERIFICATION_FAILED"


def test_fail_execution_reentry_settles_skill_use_after_child_already_failed() -> None:
    """子流程先结束 Execution 后，统一失败收口仍必须结算遗留的 active Skill Use。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model = ModelConfig(
            id="model_failure_reentry",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=_model_capabilities(),
            capability_checksum=capability_checksum(_model_capabilities()),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            planner=_Planner(),
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_failure_reentry",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        with agent.store.owned(instance, worker_id="child_failure"):
            instance.terminal_reason_json = {"code": "DYNAMIC_CHILD_FAILED"}
            agent.store.fail_instance(
                instance,
                context_patch={"failure_code": "DYNAMIC_CHILD_FAILED"},
            )
        use = GeneralSkillUse(
            tenant_id="tenant_demo",
            session_id=instance.session_id,
            turn_id="turn_failure_reentry",
            execution_id=instance.id,
            agent_id="agent_demo",
            user_id="user_demo",
            skill_id="skill_demo",
            revision_id="revision_demo",
            content_checksum="d" * 64,
            selection_mode="forced",
            status="active",
            idempotency_key="failure-reentry-use",
        )
        db.add(use)
        db.commit()

        agent.fail_execution(
            execution_id=instance.id,
            worker_id="failure_reentry_settler",
            error_code="DYNAMIC_CHILD_FAILED",
        )

        db.refresh(use)
        assert use.status == "failed"
        assert use.invalidation_reason == "DYNAMIC_CHILD_FAILED"


def test_model_lease_covers_provider_timeout_with_bounded_commit_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """慢模型租约覆盖 JSON 修复和空响应重试的最坏窗口，同时保持有界。"""

    settings = get_settings()
    monkeypatch.setattr(settings, "model_api_timeout_seconds", 600.0)
    assert _model_lease_ttl_seconds() == 7230
    monkeypatch.setattr(settings, "model_api_timeout_seconds", 15.2)
    assert _model_lease_ttl_seconds() == 213
    monkeypatch.setattr(settings, "model_api_timeout_seconds", 3600.0)
    assert _model_lease_ttl_seconds() == 14_400


def test_signal_error_disposition_separates_deterministic_and_transient_failures() -> None:
    """预算耗尽与 Skill 撤权必须一次终结，未知网络/供应商错误仍允许指数退避。"""

    assert _is_terminal_signal_error("GENERAL_SKILL_COUNTERMANDED") is True
    assert _is_terminal_signal_error("DYNAMIC_RESULT_REPAIR_EXHAUSTED") is True
    assert _is_terminal_signal_error("DYNAMIC_RUNTIME_BUDGET_EXCEEDED") is True
    assert _is_terminal_signal_error("TimeoutError") is False
    assert _is_terminal_signal_error("LLM_PROVIDER_UNAVAILABLE") is False


def test_claimed_signal_deterministic_failure_closes_execution_and_command() -> None:
    """Signal 恢复遇到确定性预算错误时必须一次性死信，并同步终结 Execution 与命令。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_terminal_budget",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "继续处理，但不得突破预算"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()

        def fail_after_claim(**kwargs):
            """模拟恢复函数已经持久认领 signal，随后发现确定性运行预算耗尽。"""

            worker_id = str(kwargs["worker_id"])
            ExecutionControlService(db).claim_signal(
                signal,
                worker_id=worker_id,
                ttl_seconds=300,
            )
            db.commit()
            raise DynamicTaskAgentError("DYNAMIC_RUNTIME_BUDGET_EXCEEDED")

        agent.resume_steer_signal = fail_after_claim

        outcome = process_dynamic_task_signal(
            db,
            signal,
            agent_factory=lambda _db: agent,
        )

        db.refresh(instance)
        db.refresh(signal)
        db.refresh(command)
        assert outcome is None
        assert signal.status == "dead_letter"
        assert signal.last_error_json == {"code": "DYNAMIC_RUNTIME_BUDGET_EXCEEDED"}
        assert command.status == "rejected"
        assert command.reason_code == "DYNAMIC_RUNTIME_BUDGET_EXCEEDED"
        assert instance.status == "failed"
        assert instance.terminal_reason_json == {"code": "DYNAMIC_RUNTIME_BUDGET_EXCEEDED"}


def test_persistent_model_and_token_budgets_block_before_unbounded_followup_calls() -> None:
    """验证规划调用计入账本，模型次数和 token 越界后不会继续调用工具或下一轮模型。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_budget",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(model)
        db.flush()

        calls_executor = _Executor()
        calls_proposer = _RunProposer()
        calls_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=calls_executor,
            planner=_Planner(),
            action_proposer=calls_proposer,
        )
        calls_instance, _ = calls_agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_model_budget",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        calls_instance.budget_snapshot_json = {
            **calls_instance.budget_snapshot_json,
            "max_model_calls": 2,
        }
        db.add(calls_instance)
        db.commit()
        try:
            calls_agent.run_until_blocked_or_complete(
                execution_id=calls_instance.id,
                model_config=model,
                worker_id="worker_model_budget",
                actor_user_id="user_demo",
            )
        except DynamicTaskAgentError as exc:
            assert str(exc) == "DYNAMIC_MODEL_CALLS_BUDGET_EXCEEDED"
        else:
            raise AssertionError("模型调用次数超过冻结上限后必须停止")
        db.refresh(calls_instance)
        assert calls_instance.context_json["dynamic_budget_usage"]["model_calls"] == 2
        assert calls_proposer.calls == 1
        assert calls_executor.calls == 1

        token_executor = _Executor()
        token_proposer = _UsageRunProposer()
        token_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=token_executor,
            planner=_Planner(),
            action_proposer=token_proposer,
        )
        token_instance, _ = token_agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_token_budget",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )
        token_instance.budget_snapshot_json = {
            **token_instance.budget_snapshot_json,
            "max_input_tokens": 9,
            "max_output_tokens": 5,
            "max_total_tokens": 14,
        }
        db.add(token_instance)
        db.commit()
        try:
            token_agent.run_until_blocked_or_complete(
                execution_id=token_instance.id,
                model_config=model,
                worker_id="worker_token_budget",
                actor_user_id="user_demo",
            )
        except DynamicTaskAgentError as exc:
            assert str(exc) == "DYNAMIC_TOKEN_BUDGET_EXCEEDED"
        else:
            raise AssertionError("provider 返回 token 超过冻结上限后必须停止")
        db.refresh(token_instance)
        usage = token_instance.context_json["dynamic_budget_usage"]
        assert usage["input_tokens"] == 10
        assert usage["total_tokens"] == 15
        assert token_proposer.calls == 1
        assert token_executor.calls == 0


def test_managed_attachment_is_snapshotted_and_reauthorized_into_provider_view(tmp_path) -> None:
    """验证真实附件从权威消息形成 snapshot，执行时重查 ACL/checksum 后才进入模型。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(
            User(
                id="user_demo",
                tenant_id="tenant_demo",
                username="demo",
                password_hash="x",
            )
        )
        db.add(
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="法务数字员工",
                owner_user_id="user_demo",
            )
        )
        resource_service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, attachment = resource_service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id="user_demo",
            agent_id="agent_demo",
            filename="contract.txt",
            content_type="text/plain",
            data="合同正文：续约日期 2026-12-31".encode(),
        )
        message = Message(
            id="message_input",
            tenant_id="tenant_demo",
            session_id="session_input",
            role="user",
            content="根据附件生成简报",
            metadata_json={"attachments": [attachment.model_dump(mode="json")]},
        )
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_input",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(message)
        db.add(model)
        db.flush()
        snapshot = _snapshot()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, snapshot),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=_InputRunProposer(),
            resource_service=resource_service,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_input",
            agent_id="agent_demo",
            initiator_user_id="user_demo",
            goal="生成续约风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
            source_ref=message.id,
            input_resource_ids=(resource.id,),
        )

        outcome = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_input",
            actor_user_id="user_demo",
        )

        assert outcome.status == "succeeded"


def test_native_image_and_pdf_require_explicit_preflight_and_never_expose_locator(
    tmp_path,
) -> None:
    """验证图片/PDF 只按显式模型能力原生投影，换到未验证模型时提前拒绝。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="图片审核数字员工",
            owner_user_id=user.id,
        )
        vision_capabilities = {**_model_capabilities(), "vision": True, "pdf_input": True}
        vision_model = ModelConfig(
            id="model_vision",
            tenant_id="tenant_demo",
            name="视觉动态模型",
            api_key_encrypted="encrypted",
            model="model-vision",
            capability_snapshot_json=vision_capabilities,
            capability_checksum=capability_checksum(vision_capabilities),
            preflight_status="ready",
        )
        plain_capabilities = _model_capabilities()
        plain_model = ModelConfig(
            id="model_plain",
            tenant_id="tenant_demo",
            name="文本动态模型",
            api_key_encrypted="encrypted",
            model="model-plain",
            capability_snapshot_json=plain_capabilities,
            capability_checksum=capability_checksum(plain_capabilities),
            preflight_status="ready",
        )
        db.add(user)
        db.add(profile)
        db.add(vision_model)
        db.add(plain_model)
        db.flush()
        resource_service = ManagedInputResourceService(db, storage_root=tmp_path)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z1pAAAAAASUVORK5CYII="
        )
        resource, attachment = resource_service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id=profile.id,
            filename="evidence.png",
            content_type="image/png",
            data=png,
        )
        pdf_buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(pdf_buffer)
        pdf_resource, pdf_attachment = resource_service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id=profile.id,
            filename="contract.pdf",
            content_type="application/pdf",
            data=pdf_buffer.getvalue(),
        )
        vision_message = Message(
            id="message_vision",
            tenant_id="tenant_demo",
            session_id="session_vision",
            role="user",
            content="根据图片生成风险简报",
            metadata_json={
                "attachments": [
                    attachment.model_dump(mode="json"),
                    pdf_attachment.model_dump(mode="json"),
                ]
            },
        )
        plain_message = Message(
            id="message_plain_image",
            tenant_id="tenant_demo",
            session_id="session_plain_image",
            role="user",
            content="根据图片生成风险简报",
            metadata_json={
                "attachments": [
                    attachment.model_dump(mode="json"),
                    pdf_attachment.model_dump(mode="json"),
                ]
            },
        )
        db.add(vision_message)
        db.add(plain_message)
        db.flush()
        vision_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(vision_model, _snapshot()),
            tool_executor=_Executor(),
            planner=_Planner(),
            action_proposer=_NativeMediaRunProposer(),
            resource_service=resource_service,
        )
        vision_instance, _ = vision_agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_vision",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="根据图片生成风险简报",
            success_criteria=("覆盖图片证据",),
            model_config=vision_model,
            source_ref=vision_message.id,
            input_resource_ids=(resource.id, pdf_resource.id),
        )

        outcome = vision_agent.run_until_blocked_or_complete(
            execution_id=vision_instance.id,
            model_config=vision_model,
            worker_id="worker_vision",
            actor_user_id=user.id,
        )
        assert outcome.status == "succeeded"

        executor = _Executor()
        proposer = _RunProposer()
        plain_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(plain_model, _snapshot()),
            tool_executor=executor,
            planner=_Planner(),
            action_proposer=proposer,
            resource_service=resource_service,
        )
        plain_instance, _ = plain_agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_plain_image",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="根据图片生成风险简报",
            success_criteria=("覆盖图片证据",),
            model_config=plain_model,
            source_ref=plain_message.id,
            input_resource_ids=(resource.id, pdf_resource.id),
        )
        try:
            plain_agent.run_until_blocked_or_complete(
                execution_id=plain_instance.id,
                model_config=plain_model,
                worker_id="worker_plain",
                actor_user_id=user.id,
            )
        except DynamicTaskAgentError as exc:
            assert str(exc) == "DYNAMIC_INPUT_MODEL_UNSUPPORTED"
        else:
            raise AssertionError("未通过 vision preflight 的模型不应接收图片")
        assert proposer.calls == 0
        assert executor.calls == 0


def test_knowledge_step_freezes_version_reauthorizes_and_resumes_without_second_search() -> None:
    """验证知识步骤冻结版本、执行前重算权限，并在成功重放时不重复检索。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="制度数字员工",
            owner_user_id=user.id,
        )
        knowledge_base = KnowledgeBase(
            id="kb_policy",
            tenant_id="tenant_demo",
            name="差旅制度",
            owner_user_id=user.id,
            access_scope="owner",
        )
        version = KnowledgeBaseVersion(
            id="kb_version_1",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="差旅制度",
        )
        model = ModelConfig(
            id="model_knowledge",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(user)
        db.add(profile)
        db.add(knowledge_base)
        db.add(version)
        db.add(model)
        db.flush()
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            profile.id,
            knowledge_base,
            metadata_json={"owner_user_id": user.id},
        )
        service = _KnowledgeService()
        proposer = _KnowledgeProposer()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            planner=_KnowledgePlanner(),
            action_proposer=proposer,
            knowledge_service=service,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_knowledge",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="回答差旅报销上限",
            success_criteria=("必须引用当前制度版本",),
            model_config=model,
            knowledge_capability={
                "available": True,
                "knowledge_bases": [
                    {
                        "id": knowledge_base.id,
                        "version_id": version.id,
                        "version": version.version,
                        "name": version.name,
                        "description": version.description,
                    }
                ],
            },
        )

        first = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_knowledge",
            actor_user_id=user.id,
        )
        replay = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_knowledge_replay",
            actor_user_id=user.id,
        )

        db.refresh(instance)
        operation = db.exec(select(SopOperation)).one()
        assert first.status == replay.status == "succeeded"
        assert operation.operation_name == "knowledge.search"
        assert operation.status == "succeeded"
        assert service.calls == 1
        assert proposer.calls == 2
        assert instance.lease_owner is None
        assert instance.lease_expires_at is None


def test_combined_attachment_knowledge_two_tools_and_result_form_one_execution(
    tmp_path,
) -> None:
    """验证附件、知识、双只读工具和最终发布在同一 Execution 内形成生产级组合闭环。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="合同风控数字员工",
            owner_user_id=user.id,
        )
        knowledge_base = KnowledgeBase(
            id="kb_policy",
            tenant_id="tenant_demo",
            name="合同续约制度",
            owner_user_id=user.id,
            access_scope="owner",
        )
        version = KnowledgeBaseVersion(
            id="kb_version_1",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="合同续约制度",
        )
        capabilities = _model_capabilities()
        model = ModelConfig(
            id="model_combined",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(user)
        db.add(profile)
        db.add(knowledge_base)
        db.add(version)
        db.add(model)
        db.flush()
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            profile.id,
            knowledge_base,
            metadata_json={"owner_user_id": user.id},
        )
        mcp_server_path = Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"
        contract_server = MCPServer(
            id="mcp_contract",
            tenant_id="tenant_demo",
            name="合同只读系统",
            transport="stdio",
            command=sys.executable,
            args_json=[str(mcp_server_path)],
        )
        risk_server = MCPServer(
            id="mcp_risk",
            tenant_id="tenant_demo",
            name="风险只读系统",
            transport="stdio",
            command=sys.executable,
            args_json=[str(mcp_server_path)],
        )
        read_contract = ToolReliabilityContract.model_validate(
            {
                "risk_class": "read",
                "side_effect": "none",
                "confirmation_policy": "none",
                "timeout_policy": "failed",
                "dynamic_task_enabled": True,
                "model_visibility": {
                    "allowed_paths": ["input.text", "output.text", "output.length"],
                    "user_display_paths": [],
                    "audit_only_paths": [],
                },
            }
        )
        read_risk = ToolReliabilityContract.model_validate(
            {
                "risk_class": "read",
                "side_effect": "none",
                "confirmation_policy": "none",
                "timeout_policy": "failed",
                "dynamic_task_enabled": True,
                "model_visibility": {
                    "allowed_paths": ["input.numbers", "output.total", "output.count"],
                    "user_display_paths": [],
                    "audit_only_paths": [],
                },
            }
        )
        contract_tool = Tool(
            id="tool_contract",
            tenant_id="tenant_demo",
            name="contract.query",
            display_name="合同台账查询",
            tool_type="mcp",
            method="POST",
            url="mcp://contract/echo",
            mcp_server_id=contract_server.id,
            config_json={"tool": "echo"},
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}, "length": {"type": "integer"}},
            },
        )
        risk_tool = Tool(
            id="tool_risk",
            tenant_id="tenant_demo",
            name="risk.query",
            display_name="风险登记查询",
            tool_type="mcp",
            method="POST",
            url="mcp://risk/sum",
            mcp_server_id=risk_server.id,
            config_json={"tool": "sum"},
            input_schema={
                "type": "object",
                "properties": {"numbers": {"type": "array", "items": {"type": "number"}}},
            },
            output_schema={
                "type": "object",
                "properties": {"total": {"type": "number"}, "count": {"type": "integer"}},
            },
        )
        publish_tool_contract(contract_tool, read_contract)
        publish_tool_contract(risk_tool, read_risk)
        db.add(contract_server)
        db.add(risk_server)
        db.add(contract_tool)
        db.add(risk_tool)
        db.flush()
        ensure_private_resource_binding(
            db, "tenant_demo", profile.id, "tool", contract_tool.id, "active"
        )
        ensure_private_resource_binding(
            db, "tenant_demo", profile.id, "tool", risk_tool.id, "active"
        )
        resource_service = ManagedInputResourceService(db, storage_root=tmp_path)
        resource, attachment = resource_service.persist_upload(
            tenant_id="tenant_demo",
            owner_user_id=user.id,
            agent_id=profile.id,
            filename="contract.txt",
            content_type="text/plain",
            data="合同正文：续约日期 2026-12-31".encode(),
        )
        user_message = Message(
            id="message_combined",
            tenant_id="tenant_demo",
            session_id="session_combined",
            role="user",
            content="结合附件和内部证据生成续约风险简报",
            metadata_json={"attachments": [attachment.model_dump(mode="json")]},
        )
        db.add(user_message)
        db.flush()

        knowledge_service = _KnowledgeService()
        proposer = _CombinedProposer()
        agent = DynamicTaskAgent(
            db,
            catalog=DynamicCapabilityCatalog(db),
            tool_executor=ToolExecutor(db),
            planner=_CombinedPlanner(),
            action_proposer=proposer,
            resource_service=resource_service,
            knowledge_service=knowledge_service,
            artifact_service=ArtifactService(db, storage_root=tmp_path / "artifacts"),
        )
        instance, created = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_combined",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="生成续约风险简报",
            success_criteria=("附件、制度、合同台账和风险登记均有证据",),
            model_config=model,
            source_ref=user_message.id,
            input_resource_ids=(resource.id,),
            knowledge_capability={
                "available": True,
                "knowledge_bases": [
                    {
                        "id": knowledge_base.id,
                        "version_id": version.id,
                        "version": version.version,
                        "name": version.name,
                    }
                ],
            },
        )

        outcome = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_combined",
            actor_user_id=user.id,
        )

        db.refresh(instance)
        operations = db.exec(
            select(SopOperation)
            .where(SopOperation.instance_id == instance.id)
            .order_by(SopOperation.created_at, SopOperation.id)
        ).all()
        assistant_messages = db.exec(
            select(Message).where(
                Message.session_id == instance.session_id,
                Message.role == "assistant",
            )
        ).all()
        result = db.exec(
            select(ExecutionResult).where(ExecutionResult.execution_id == instance.id)
        ).one()
        publication = db.exec(
            select(ExecutionPublication).where(
                ExecutionPublication.execution_id == instance.id
            )
        ).one()
        artifact = db.exec(
            select(ExecutionArtifact).where(ExecutionArtifact.execution_id == instance.id)
        ).one()
        artifact_link = db.exec(
            select(ArtifactInputLink).where(ArtifactInputLink.artifact_id == artifact.id)
        ).one()

        assert created is True
        assert outcome.status == "succeeded"
        assert instance.status == "succeeded"
        assert proposer.calls == ["search_policy", "query_contract", "query_risk", "answer"]
        assert knowledge_service.calls == 1
        assert [operation.operation_name for operation in operations] == [
            "knowledge.search",
            "contract.query",
            "risk.query",
        ]
        assert all(operation.status == "succeeded" for operation in operations)
        assert operations[1].result_json["data"] == {"text": "C-001", "length": 5}
        assert operations[2].result_json["data"]["total"] == 5
        assert result.status == "verified"
        assert publication.status == "settled"
        assert artifact.filename == "续约风险简报.md"
        assert artifact.content_checksum
        assert artifact_link.input_snapshot_id == db.exec(
            select(InputResourceSnapshot.id).where(
                InputResourceSnapshot.execution_id == instance.id
            )
        ).one()
        assert assistant_messages[0].metadata_json["artifact_ids"] == [artifact.id]
        assert len(assistant_messages) == 1
        assert assistant_messages[0].metadata_json["execution_id"] == instance.id
        assert instance.context_json["dynamic_budget_usage"] == {
            "model_calls": 5,
            "tool_calls": 3,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }


def test_knowledge_revocation_is_rejected_before_search_dispatch() -> None:
    """验证入口后成员被停用时实时权限检查失败，冻结快照不能继续授权检索。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="制度数字员工",
            owner_user_id=user.id,
        )
        knowledge_base = KnowledgeBase(
            id="kb_policy",
            tenant_id="tenant_demo",
            name="差旅制度",
            owner_user_id=user.id,
            access_scope="owner",
        )
        version = KnowledgeBaseVersion(
            id="kb_version_1",
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            version="1.0.0",
            name="差旅制度",
        )
        model = ModelConfig(
            id="model_knowledge_revoked",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(user)
        db.add(profile)
        db.add(knowledge_base)
        db.add(version)
        db.add(model)
        db.flush()
        ensure_agent_private_knowledge_branch(db, "tenant_demo", profile.id, knowledge_base)
        service = _KnowledgeService()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            planner=_KnowledgePlanner(),
            action_proposer=_KnowledgeProposer(),
            knowledge_service=service,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_knowledge_revoked",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="回答差旅报销上限",
            success_criteria=("必须引用当前制度版本",),
            model_config=model,
            knowledge_capability={
                "available": True,
                "knowledge_bases": [
                    {
                        "id": knowledge_base.id,
                        "version_id": version.id,
                        "version": version.version,
                        "name": version.name,
                    }
                ],
            },
        )
        user.membership_status = "suspended"
        db.add(user)
        db.commit()

        try:
            agent.advance_next_knowledge_step(
                execution_id=instance.id,
                model_config=model,
                worker_id="worker_revoked",
                actor_user_id=user.id,
            )
        except DynamicTaskAgentError as exc:
            assert str(exc) == "DYNAMIC_KNOWLEDGE_ACTOR_DENIED"
        else:
            raise AssertionError("成员撤权后不应继续检索冻结知识版本")
        assert service.calls == 0


def test_clarification_attention_resumes_same_execution_after_restart_style_signal() -> None:
    """验证澄清进入统一待处理中心，决定后由持久 signal 恢复同一 Execution。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        capabilities = _model_capabilities()
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_demo",
            tenant_id="tenant_demo",
            name="合同数字员工",
            owner_user_id=user.id,
        )
        model = ModelConfig(
            id="model_clarification",
            tenant_id="tenant_demo",
            name="动态模型",
            api_key_encrypted="encrypted",
            model="model-demo",
            capability_snapshot_json=capabilities,
            capability_checksum=capability_checksum(capabilities),
            preflight_status="ready",
        )
        db.add(user)
        db.add(profile)
        db.add(model)
        db.flush()
        executor = _Executor()
        proposer = _ClarificationProposer()
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=executor,
            planner=_ClarificationPlanner(),
            action_proposer=proposer,
        )
        instance, _ = agent.start_task(
            tenant_id="tenant_demo",
            session_id="session_clarification",
            agent_id=profile.id,
            initiator_user_id=user.id,
            goal="生成合作方合同风险简报",
            success_criteria=("覆盖合同证据",),
            model_config=model,
        )

        waiting = agent.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="worker_before_restart",
            actor_user_id=user.id,
        )
        attention = db.exec(select(SopWorkItem)).one()
        assert waiting.status == "waiting"
        assert waiting.blocking_step_key == "clarify_partner"
        assert attention.attention_kind == "clarification"
        assert attention.candidate_snapshot_json == [{"user_id": user.id}]
        assert attention.payload_json["question"] == "请选择需要核验的合作方"

        control = ExecutionControlService(db)
        with control.store.owned(instance, worker_id="attention_api"):
            control.resolve_attention(
                instance,
                attention,
                actor_user_id=user.id,
                command_id="clarification_answer_1",
                command="answer",
                expected_revision=attention.revision,
                comment="星海科技",
            )
        db.commit()
        signal = db.exec(select(ExecutionSignal)).one()

        restarted_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=executor,
            planner=_ClarificationPlanner(),
            action_proposer=proposer,
        )
        def crash_after_resume_commit(**kwargs):
            """模拟 clarification 已恢复提交、但后续计划尚未推进时进程退出。"""

            assert kwargs["resume_signal_id"] == signal.id
            raise RuntimeError("simulated process crash")

        restarted_agent.run_until_blocked_or_complete = crash_after_resume_commit
        try:
            restarted_agent.resume_clarification_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id="worker_after_restart",
                actor_user_id=user.id,
            )
        except RuntimeError as exc:
            assert str(exc) == "simulated process crash"
        else:
            raise AssertionError("故障注入必须发生在恢复事实提交后")
        db.refresh(signal)
        assert signal.status == "claimed"
        signal.lease_expires_at = datetime(2000, 1, 1)
        db.add(signal)
        db.commit()

        recovered_agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, _snapshot()),
            tool_executor=executor,
            planner=_ClarificationPlanner(),
            action_proposer=proposer,
        )
        due = due_dynamic_task_signals(db)
        assert [item.id for item in due] == [signal.id]
        outcome = process_dynamic_task_signal(
            db,
            signal,
            agent_factory=lambda _db: recovered_agent,
        )

        db.refresh(instance)
        db.refresh(signal)
        assert outcome is not None and outcome.status == "succeeded"
        assert instance.id == waiting.execution_id
        assert instance.slots_json["clarifications"]["clarify_partner"]["answer"] == "星海科技"
        assert signal.status == "consumed"
        assert executor.calls == 1
        assert proposer.calls == 3


def _steering_execution(db: Session):
    """创建已完成首个只读步骤、可接受运行中约束的动态 Execution。"""

    capabilities = _model_capabilities()
    user = User(
        id="user_steering",
        tenant_id="tenant_demo",
        username="steering",
        password_hash="x",
    )
    profile = AgentProfile(
        id="agent_demo",
        tenant_id="tenant_demo",
        name="合同数字员工",
        owner_user_id=user.id,
    )
    model = ModelConfig(
        id="model_steering",
        tenant_id="tenant_demo",
        name="动态模型",
        api_key_encrypted="encrypted",
        model="model-demo",
        capability_snapshot_json=capabilities,
        capability_checksum=capability_checksum(capabilities),
        preflight_status="ready",
    )
    snapshot = _snapshot()
    db.add(user)
    db.add(profile)
    db.add(model)
    db.flush()
    executor = _Executor()
    proposer = _RunProposer()
    agent = DynamicTaskAgent(
        db,
        catalog=_StartCatalog(model, snapshot),
        tool_executor=executor,
        planner=_Planner(),
        action_proposer=proposer,
    )
    instance, _ = agent.start_task(
        tenant_id="tenant_demo",
        session_id="session_steering",
        agent_id=profile.id,
        initiator_user_id=user.id,
        goal="生成合作方合同风险简报",
        success_criteria=("覆盖合同证据",),
        model_config=model,
    )
    agent.advance_next_read_step(
        execution_id=instance.id,
        model_config=model,
        worker_id="worker_initial_read",
        actor_user_id=user.id,
    )
    db.commit()
    return agent, instance, model, user, executor, proposer


def test_steer_appends_constraint_revision_and_preserves_completed_step() -> None:
    """验证追加约束形成不可变计划修订，已完成证据不重跑且 signal 最终消费。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, executor, proposer = _steering_execution(db)
        old_plan_id = instance.current_plan_revision_id
        control = ExecutionControlService(db)
        command, _ = control.issue_command(
            instance,
            command_id="steer_scope_1",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "仅分析 2026 年内到期合同"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()

        outcome = agent.resume_steer_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_steer",
            actor_user_id=user.id,
            steering_enabled=True,
        )

        db.refresh(instance)
        db.refresh(command)
        db.refresh(signal)
        revisions = db.exec(
            select(ExecutionPlanRevision)
            .where(ExecutionPlanRevision.execution_id == instance.id)
            .order_by(ExecutionPlanRevision.revision_number)
        ).all()
        assert outcome.status == "succeeded"
        assert [row.status for row in revisions] == ["superseded", "active"]
        assert revisions[0].id == old_plan_id
        assert revisions[1].reason == "user_constraint"
        assert revisions[1].plan_json["constraints"] == ["仅分析 2026 年内到期合同"]
        assert command.status == "applied"
        assert command.result_plan_revision_id == revisions[1].id
        assert signal.status == "consumed"
        assert executor.calls == 1
        assert proposer.calls == 2


def test_steer_crash_after_apply_replays_without_duplicate_plan_revision() -> None:
    """验证计划已提交但进程退出后，过期 signal 恢复不会重复追加同一修订。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, executor, proposer = _steering_execution(db)
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_crash_1",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "排除已终止合同"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()

        def crash_after_apply(**kwargs):
            """在计划和命令均已提交后模拟 worker 退出。"""

            assert kwargs["resume_signal_id"] == signal.id
            raise RuntimeError("simulated steer crash")

        agent.run_until_blocked_or_complete = crash_after_apply
        with pytest.raises(RuntimeError, match="simulated steer crash"):
            agent.resume_steer_signal(
                signal_id=signal.id,
                model_config=model,
                worker_id="worker_steer_crash",
                actor_user_id=user.id,
                steering_enabled=True,
            )
        db.refresh(command)
        db.refresh(signal)
        assert command.status == "applied"
        assert signal.status == "claimed"
        signal.lease_expires_at = datetime(2000, 1, 1)
        db.add(signal)
        db.commit()

        recovered = DynamicTaskAgent(
            db,
            catalog=agent.catalog,
            tool_executor=executor,
            planner=_Planner(),
            action_proposer=proposer,
        )
        outcome = recovered.resume_steer_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_steer_recovered",
            actor_user_id=user.id,
            steering_enabled=False,
        )

        assert outcome.status == "succeeded"
        assert len(
            db.exec(
                select(ExecutionPlanRevision).where(
                    ExecutionPlanRevision.execution_id == instance.id
                )
            ).all()
        ) == 2
        assert executor.calls == 1


def test_second_steer_on_same_plan_conflicts_after_first_revision() -> None:
    """验证并发追加约束以基础 plan revision CAS 收敛，后到命令不会覆盖先到结果。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        control = ExecutionControlService(db)
        first, _ = control.issue_command(
            instance,
            command_id="steer_first",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "先处理高风险合同"},
        )
        second, _ = control.issue_command(
            instance,
            command_id="steer_second",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "改为先处理低风险合同"},
        )
        db.commit()
        first_signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == first.id)
        ).one()
        second_signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == second.id)
        ).one()
        original_run = agent.run_until_blocked_or_complete
        agent.run_until_blocked_or_complete = lambda **kwargs: DynamicRunOutcome(
            "blocked", instance.id
        )
        agent.resume_steer_signal(
            signal_id=first_signal.id,
            model_config=model,
            worker_id="worker_first",
            actor_user_id=user.id,
            steering_enabled=True,
        )
        agent.run_until_blocked_or_complete = original_run
        outcome = agent.resume_steer_signal(
            signal_id=second_signal.id,
            model_config=model,
            worker_id="worker_second",
            actor_user_id=user.id,
            steering_enabled=True,
        )

        db.refresh(first)
        db.refresh(second)
        db.refresh(second_signal)
        assert first.status == "applied"
        assert second.status == "conflicted"
        assert second.reason_code == "STEER_PLAN_REVISION_CONFLICT"
        assert second_signal.status == "consumed"
        assert outcome.status == "conflicted"


def test_pending_steer_is_rejected_and_consumed_when_switch_turns_off() -> None:
    """验证发布后关闭开关会终结存量 pending 命令，而不是留下永久阻塞事实。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_disabled_after_issue",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "仅处理高风险合同"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()

        outcome = agent.resume_steer_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_disabled",
            actor_user_id=user.id,
            steering_enabled=False,
        )

        db.refresh(command)
        db.refresh(signal)
        assert outcome.status == "rejected"
        assert command.status == "rejected"
        assert command.reason_code == "DYNAMIC_STEERING_DISABLED"
        assert signal.status == "consumed"


def test_pending_steer_is_rejected_when_issuer_membership_is_revoked() -> None:
    """验证命令排队期间发起人被停用后重新鉴权，旧授权不能继续修改计划。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_revoked_actor",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "只保留有效合同"},
        )
        user.membership_status = "suspended"
        db.add(user)
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()

        outcome = agent.resume_steer_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_revoked_actor",
            actor_user_id=user.id,
            steering_enabled=True,
        )

        db.refresh(command)
        db.refresh(signal)
        assert outcome.status == "rejected"
        assert command.status == "rejected"
        assert command.reason_code == "DYNAMIC_STEER_ACTOR_DENIED"
        assert signal.status == "consumed"


def test_steer_cancels_prepared_unsent_operation_before_plan_revision() -> None:
    """验证未 dispatch 的 prepared 动作可证明撤销，旧节点结束后才激活新约束。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        with agent.store.owned(instance, worker_id="prepare_old_action"):
            node = agent.store.enter_node(
                instance,
                "answer",
                step_key="answer",
                plan_revision_id=instance.current_plan_revision_id,
                step_kind="answer",
                title="形成风险简报",
            )
            operation = SopOperation(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                node_execution_id=node.id,
                operation_name="artifact.prepare",
                idempotency_key="prepared-steer-operation",
                logical_action_id="prepared-steer-action",
                request_fingerprint="f" * 64,
                effect_kind="read",
                status="prepared",
            )
            db.add(operation)
            db.flush()
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_prepared",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "不要生成附件，只返回文本"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()
        agent.run_until_blocked_or_complete = lambda **kwargs: DynamicRunOutcome(
            "blocked", instance.id
        )

        outcome = agent.resume_steer_signal(
            signal_id=signal.id,
            model_config=model,
            worker_id="worker_prepared",
            actor_user_id=user.id,
            steering_enabled=True,
        )

        db.refresh(operation)
        db.refresh(node)
        assert outcome.status == "blocked"
        assert operation.status == "cancelled"
        assert operation.cancellation_disposition == "not_dispatched"
        assert node.status == "failed"
        assert node.error_json["code"] == "DYNAMIC_ACTION_SUPERSEDED_BY_STEERING"


def test_steer_retries_while_dispatched_operation_is_unsettled(monkeypatch) -> None:
    """验证 running 外呼不会被伪撤回，worker 保留 pending 命令并退避 signal。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        completed_node = db.exec(
            select(SopOperation).where(SopOperation.instance_id == instance.id)
        ).one().node_execution_id
        operation = SopOperation(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=completed_node,
            operation_name="remote.read.pending",
            idempotency_key="running-steer-operation",
            logical_action_id="running-steer-action",
            request_fingerprint="e" * 64,
            effect_kind="read",
            status="running",
        )
        db.add(operation)
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_running",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "排除过期数据"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()
        monkeypatch.setattr(
            "app.dynamic_tasks.worker.get_settings",
            lambda: type("Settings", (), {"dynamic_task_steering_enabled": True})(),
        )

        outcome = process_dynamic_task_signal(
            db,
            signal,
            agent_factory=lambda _db: agent,
        )

        db.refresh(command)
        db.refresh(signal)
        assert outcome is None
        assert command.status == "pending"
        assert signal.status == "pending"
        assert signal.last_error_json["code"] == "DYNAMIC_STEER_OPERATION_UNSETTLED"


def test_steer_rolls_back_prepared_cancellation_when_plan_append_fails(monkeypatch) -> None:
    """验证安全边界处置与计划/命令同成同败，worker 只提交 signal 退避事实。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, user, _, _ = _steering_execution(db)
        with agent.store.owned(instance, worker_id="prepare_rollback_action"):
            node = agent.store.enter_node(
                instance,
                "answer",
                step_key="answer",
                plan_revision_id=instance.current_plan_revision_id,
                step_kind="answer",
                title="形成风险简报",
            )
            operation = SopOperation(
                tenant_id=instance.tenant_id,
                instance_id=instance.id,
                node_execution_id=node.id,
                operation_name="artifact.prepare.rollback",
                idempotency_key="prepared-steer-rollback-operation",
                logical_action_id="prepared-steer-rollback-action",
                request_fingerprint="a" * 64,
                effect_kind="read",
                status="prepared",
            )
            db.add(operation)
            db.flush()
        command, _ = ExecutionControlService(db).issue_command(
            instance,
            command_id="steer_append_failure",
            command_type="steer",
            actor_user_id=user.id,
            expected_execution_revision=instance.revision,
            payload={"instruction": "只返回文本"},
        )
        db.commit()
        signal = db.exec(
            select(ExecutionSignal).where(ExecutionSignal.causation_id == command.id)
        ).one()
        monkeypatch.setattr(
            "app.dynamic_tasks.worker.get_settings",
            lambda: type("Settings", (), {"dynamic_task_steering_enabled": True})(),
        )

        def fail_plan_append(*args, **kwargs):
            """在 prepared Operation 已进入撤销逻辑后模拟计划写入失败。"""

            raise RuntimeError("simulated plan append failure")

        monkeypatch.setattr(agent.store, "append_plan_revision", fail_plan_append)
        outcome = process_dynamic_task_signal(
            db,
            signal,
            agent_factory=lambda _db: agent,
        )

        db.refresh(operation)
        db.refresh(node)
        db.refresh(command)
        db.refresh(signal)
        assert outcome is None
        assert operation.status == "prepared"
        assert node.status == "running"
        assert command.status == "pending"
        assert signal.status == "pending"


def test_capacity_retry_signal_backfills_quota_and_persistently_backs_off(monkeypatch) -> None:
    """验证升级前活动 Execution 先补齐三类槽位，工具仍满载时 signal 不会热循环或丢失。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        agent, instance, model, _, _, _ = _steering_execution(db)
        signal = ExecutionControlService(db).enqueue_signal(
            instance,
            signal_type="capacity_retry",
            causation_type="quota_backpressure",
            causation_id=f"{instance.id}:{instance.revision}",
            payload={"reason_code": "DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED"},
            max_attempts=16,
        )
        db.commit()
        configured = type(
            "Settings",
            (),
            {
                "dynamic_task_steering_enabled": True,
                "dynamic_task_max_active_per_tenant": 2,
                "dynamic_task_max_active_per_agent": 2,
                "dynamic_task_max_active_per_user": 2,
                "dynamic_task_max_active_per_tool": 1,
            },
        )()
        monkeypatch.setattr("app.dynamic_tasks.worker.get_settings", lambda: configured)

        def still_saturated(**_kwargs):
            """模拟持久恢复时工具槽仍被其他 Execution 占用。"""

            raise DynamicTaskQuotaError("DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED")

        agent.run_until_blocked_or_complete = still_saturated
        outcome = process_dynamic_task_signal(db, signal, agent_factory=lambda _db: agent)

        db.refresh(signal)
        assert outcome is None
        assert signal.status == "pending"
        assert signal.attempt_count == 1
        assert signal.last_error_json == {"code": "DYNAMIC_TASK_TOOL_QUOTA_EXCEEDED"}
        leases = db.exec(
            select(DynamicTaskQuotaLease).where(
                DynamicTaskQuotaLease.holder_id == instance.id
            )
        ).all()
        assert {lease.scope_type for lease in leases} == {"tenant", "agent", "user"}
