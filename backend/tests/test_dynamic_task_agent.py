"""
@Time       : 2026/08/04 02:02
@Author     : zhanglp8181
@File       : test_dynamic_task_agent.py
@CallChain  : pytest → DynamicTaskAgent → Execution Store/受控 ToolExecutor
@Description: 验证首期只读动态动作的持久提案、实时再授权和崩溃恢复去重。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import ModelConfig, SopOperation
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
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
from app.sop_runtime.execution_store import SopExecutionStore
from app.tools.tool_schema import ToolResult


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

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """按入口传入的目标和成功标准构造规范计划。"""

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
            ),
            budget={"max_steps": 4},
        )


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
    assert first.budget == {"max_steps": 4, "max_tool_calls": 2, "max_model_calls": 4}


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
        agent = DynamicTaskAgent(
            db,
            catalog=_StartCatalog(model, snapshot),
            tool_executor=_Executor(),
            planner=_Planner(),
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
        assert first.goal_snapshot_json["success_criteria"][0]["id"] == "criterion_01"
