"""
@Time       : 2026/08/04 02:02
@Author     : zhanglp8181
@File       : test_dynamic_task_agent.py
@CallChain  : pytest → DynamicTaskAgent → Execution Store/受控 ToolExecutor
@Description: 验证首期只读动态动作的持久提案、实时再授权和崩溃恢复去重。
"""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
from pathlib import Path
import sys

from pypdf import PdfWriter
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_private_knowledge_branch, ensure_private_resource_binding
from app.db.models import (
    AgentProfile,
    ExecutionSignal,
    ExecutionPublication,
    ExecutionResult,
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
)
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import (
    CapabilitySnapshot,
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    capability_checksum,
    publish_tool_contract,
)
from app.dynamic_tasks.worker import due_dynamic_task_signals, process_dynamic_task_signal
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
from app.knowledge.schema import KnowledgeSearchResponse
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

    def list_general_skills(self, tenant_id: str, agent_id: str) -> list[CapabilitySnapshot]:
        """本测试不提供规划指南。"""

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


def test_plan_budget_counts_knowledge_and_tools_as_external_read_calls() -> None:
    """规划期与 Runtime 必须一致地把 knowledge 和 tool.read 计入同一外部调用预算。"""

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
        assert str(exc) == "动态计划外部读取步骤超过服务端预算"
    else:
        raise AssertionError("知识与工具合计超过预算时必须在计划激活前拒绝")

    accepted = normalize_plan_draft(
        draft,
        max_steps=5,
        max_tool_calls=3,
        max_model_calls=6,
    )
    assert accepted.budget["max_tool_calls"] == 3


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

        message = agent.complete_with_result_proposal(
            execution_id=instance.id,
            step_key="answer",
            completed_response=response,
            provider="openai_compatible",
            model="model-demo",
            model_capabilities=capabilities,
            worker_id="worker_result",
        )

        db.refresh(instance)
        result = db.exec(select(ExecutionResult)).one()
        publication = db.exec(select(ExecutionPublication)).one()
        assert db.exec(select(Message)).one().id == message.id
        assert result.status == "verified"
        assert publication.status == "settled"
        assert publication.receipt_json["message_id"] == message.id
        assert instance.status == "succeeded"


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
