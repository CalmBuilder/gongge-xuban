"""
@Time       : 2026/08/10 23:05
@Author     : zhanglp8181
@File       : test_readonly_explore_agent.py
@CallChain  : pytest → ReadOnlyExploreProposer/Planner → 父 Dynamic Execution
@Description: 验证探索上下文、纯读白名单、报告证据和禁止递归的冻结契约。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ActionProposalRecord,
    AgentProfile,
    ModelConfig,
    Message,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.capability_catalog import (
    CapabilityAccessDenied,
    CapabilitySnapshot,
    ToolReliabilityContract,
    capability_checksum,
)
from app.dynamic_tasks.explorer import ReadOnlyExploreProposer, ReadOnlyExploreReport
from app.dynamic_tasks.execution_context import build_execution_context_projection
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_store import SopExecutionStore
from app.tools.tool_schema import ToolResult


def _snapshot(*, explore_safe: bool) -> CapabilitySnapshot:
    """构造显式允许或禁止进入探索上下文的纯读能力。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "tool_contract",
        "tenant_id": "tenant_demo",
        "name": "contract.query",
        "contract": {"risk_class": "read", "explore_safe": explore_safe},
        "model_view": {
            "name": "contract.query",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {
                "type": "object",
                "properties": {"contract_id": {"type": "string"}},
            },
        },
        "user_view": {"name": "合同查询"},
        "audit_view": {"name": "contract.query"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


class _ExplorePlanClient:
    """返回 explore 汇聚到 answer 的规范草案。"""

    def __init__(self) -> None:
        """保留规划 payload 供能力披露断言。"""

        self.payload: dict[str, object] | None = None

    def generate_json(self, _system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """验证服务端只在安全能力存在时允许 explore。"""

        self.payload = payload
        return {
            "goal": "核验合同",
            "success_criteria": [
                {"id": "verified", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "research",
                    "title": "探索合同事实",
                    "kind": "explore",
                    "capability_refs": ["contract.query"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成结论",
                    "kind": "answer",
                    "depends_on": ["research"],
                },
            ],
        }


def test_planner_only_exposes_explore_for_explicit_safe_tools() -> None:
    """普通 read 不足以进入探索，必须另有 explore_safe 发布事实。"""

    safe_client = _ExplorePlanClient()
    plan = DynamicTaskPlanner(safe_client, explore_enabled=True).create_plan(
        goal="核验合同",
        success_criteria=(
            SuccessCriterion(id="verified", type="assertion", spec={"required": True}),
        ),
        capabilities=(_snapshot(explore_safe=True),),
    )
    assert plan.steps[0].kind == "explore"
    assert "explore" in safe_client.payload["limits"]["allowed_step_kinds"]

    unsafe_client = _ExplorePlanClient()
    with pytest.raises(ValueError, match="explore-safe"):
        DynamicTaskPlanner(unsafe_client, explore_enabled=True).create_plan(
            goal="核验合同",
            success_criteria=(
                SuccessCriterion(id="verified", type="assertion", spec={"required": True}),
            ),
            capabilities=(_snapshot(explore_safe=False),),
        )
    assert "explore" not in unsafe_client.payload["limits"]["allowed_step_kinds"]


def test_explore_safe_contract_rejects_write_or_disabled_dynamic_tool() -> None:
    """探索资格不能把写工具或未发布工具伪装成低风险读取。"""

    with pytest.raises(ValidationError, match="Explore"):
        ToolReliabilityContract(
            risk_class="external_write",
            side_effect="external",
            confirmation_policy="once",
            timeout_policy="unknown",
            dynamic_task_enabled=True,
            explore_safe=True,
        )
    with pytest.raises(ValidationError, match="Explore"):
        ToolReliabilityContract(
            risk_class="read",
            side_effect="none",
            confirmation_policy="none",
            timeout_policy="failed",
            dynamic_task_enabled=False,
            explore_safe=True,
        )


class _ExploreClient:
    """按给定响应模拟探索独立模型上下文。"""

    def __init__(self, response: dict[str, object]) -> None:
        """保存响应和最后一次临时上下文。"""

        self.response = response
        self.payload: dict[str, object] | None = None

    def generate_json_with_metadata(
        self,
        _system_prompt: str,
        payload: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """返回完整 provider identity，供提案账本建立稳定身份。"""

        self.payload = payload
        return self.response, {
            "response_id": "explore-response-1",
            "finish_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }


def test_explore_proposer_only_accepts_declared_read_or_evidenced_report() -> None:
    """探索模型只能调用当前 Step 工具，完成报告必须留给 Runtime 校验 Operation 引用。"""

    step = PlanStep(
        step_key="explore_contract",
        title="探索合同",
        kind="explore",
        capability_refs=("contract.query",),
    )
    call_client = _ExploreClient(
        {
            "action_kind": "call_tool",
            "capability_ref": "contract.query",
            "arguments": {"partner": "星海科技"},
            "rationale": "读取事实",
        }
    )
    proposal = ReadOnlyExploreProposer(call_client).propose(
        goal="核验合同",
        step=step,
        capabilities=(_snapshot(explore_safe=True),),
        observations=(),
        remaining_tool_calls=3,
    )
    assert proposal.proposal.action_kind.value == "call_tool"
    assert call_client.payload is not None
    assert "execution_id" not in str(call_client.payload)
    assert call_client.payload["limits"]["recursion_allowed"] is False
    assert isinstance(call_client.payload["output_contract"]["arguments"], str)

    bad_client = _ExploreClient(
        {
            "action_kind": "call_tool",
            "capability_ref": "admin.delete",
            "arguments": {},
            "rationale": "越权",
        }
    )
    with pytest.raises(ValueError, match="冻结"):
        ReadOnlyExploreProposer(bad_client).propose(
            goal="核验合同",
            step=step,
            capabilities=(_snapshot(explore_safe=True),),
            observations=(),
            remaining_tool_calls=3,
        )


def test_explore_report_requires_nonempty_operation_evidence() -> None:
    """没有成功 Operation 引用的文字不能成为父 Step 的可信探索输出。"""

    with pytest.raises(ValidationError):
        ReadOnlyExploreReport(report="看起来没有风险", evidence=[])
    report = ReadOnlyExploreReport(
        report="合同 C-001 已核验。",
        evidence=[{"operation_id": "sopop_1", "capability_ref": "contract.query"}],
        limitations=[],
    )
    assert report.evidence[0].operation_id == "sopop_1"


class _SequencedExploreClient:
    """首轮读取，次轮根据真实 observation 生成带 Operation 引用的报告。"""

    def __init__(self) -> None:
        """初始化调用计数和临时上下文快照。"""

        self.calls = 0
        self.payloads: list[dict[str, object]] = []

    def generate_json_with_metadata(
        self,
        _system_prompt: str,
        payload: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """只从第二轮已投影 observation 引用真实 Operation。"""

        self.calls += 1
        self.payloads.append(payload)
        metadata = {
            "response_id": f"explore-runtime-{self.calls}",
            "finish_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
        if self.calls == 1:
            return (
                {
                    "action_kind": "call_tool",
                    "capability_ref": "contract.query",
                    "arguments": {"partner": "星海科技"},
                    "rationale": "读取权威合同",
                },
                metadata,
            )
        observations = payload["observations"]
        assert isinstance(observations, list) and len(observations) == 1
        observed = observations[0]
        return (
            {
                "action_kind": "complete",
                "capability_ref": None,
                "arguments": {
                    "report": "合同 C-001 已由权威系统核验。",
                    "evidence": [
                        {
                            "operation_id": observed["operation_id"],
                            "capability_ref": observed["capability_ref"],
                        }
                    ],
                    "limitations": [],
                },
                "rationale": "证据已充分，返回压缩报告",
            },
            metadata,
        )


class _RuntimeCatalog:
    """提供冻结模型并记录探索每次派发前的实时再授权。"""

    def __init__(self, model: ModelConfig, *, revoke: bool = False) -> None:
        """配置授权成功或撤销故障。"""

        self.model = model
        self.revoke = revoke
        self.reauthorize_calls = 0

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """返回同租户、同 ID 的已验证模型。"""

        assert tenant_id == self.model.tenant_id
        assert model_config_id == self.model.id
        return self.model

    def reauthorize_tool(self, _snapshot: CapabilitySnapshot, **_kwargs: object) -> object:
        """模拟派发前授权仍有效或已撤销。"""

        self.reauthorize_calls += 1
        if self.revoke:
            raise CapabilityAccessDenied("CAPABILITY_BINDING_REVOKED")
        return object()


class _ExploreExecutor:
    """返回含敏感同级字段的结果，验证探索临时上下文只取发布 schema。"""

    def __init__(self) -> None:
        """初始化真实 adapter 调用计数。"""

        self.calls = 0

    def execute(self, _tenant_id: str, tool_call: object, **_kwargs: object) -> ToolResult:
        """返回确定性合同事实和不得进入模型的 secret 字段。"""

        self.calls += 1
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"contract_id": "C-001", "secret": "must-not-leak"},
        )


class _CrashOnceExploreExecutor(_ExploreExecutor):
    """模拟 adapter 返回前进程异常，验证同一只读 Operation 可恢复。"""

    def execute(self, tenant_id: str, tool_call: object, **kwargs: object) -> ToolResult:
        """第一次在回执前中断，第二次复用相同 Operation 返回结果。"""

        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated adapter crash")
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"contract_id": "C-001", "secret": "must-not-leak"},
        )


class _InvalidEvidenceExploreClient(_SequencedExploreClient):
    """第二轮伪造不属于本 Step 的 Operation 引用。"""

    def generate_json_with_metadata(
        self,
        system_prompt: str,
        payload: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """保留合法首轮读取，但用伪造引用提交报告。"""

        if self.calls == 0:
            return super().generate_json_with_metadata(system_prompt, payload)
        self.calls += 1
        self.payloads.append(payload)
        return (
            {
                "action_kind": "complete",
                "capability_ref": None,
                "arguments": {
                    "report": "伪造报告",
                    "evidence": [
                        {
                            "operation_id": "sopop_other_execution",
                            "capability_ref": "contract.query",
                        }
                    ],
                    "limitations": [],
                },
                "rationale": "尝试越权引用",
            },
            {
                "response_id": f"explore-runtime-{self.calls}",
                "finish_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )


class _FinalActionProposer:
    """验证父 answer 只消费探索压缩报告并返回可核验结果。"""

    def __init__(self) -> None:
        """初始化最终动作调用次数。"""

        self.calls = 0

    def propose(self, *, view: object, step: PlanStep) -> CompletedProviderProposal:
        """从机械完成步骤中确认报告可见、中间结果不可见。"""

        self.calls += 1
        assert step.kind == "answer"
        serialized = view.model_dump_json()
        assert "合同 C-001 已由权威系统核验" in serialized
        assert "must-not-leak" not in serialized
        assert "contract_id" not in serialized
        return CompletedProviderProposal(
            response_id="final-answer-1",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "# 核验结论\n\n合同 C-001 已核验。",
                    "criterion_evidence": {"verified": ["explore_contract"]},
                    "pending_questions": [],
                },
                rationale="基于探索报告形成最终结论",
            ),
        )


def _runtime_fixture(db: Session) -> tuple[ModelConfig, CapabilitySnapshot, object]:
    """创建包含 explore→answer 计划的最小父 Dynamic Execution。"""

    model_capabilities = {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "structured_output": True,
        "tool_calling": True,
    }
    model = ModelConfig(
        id="model_explore",
        tenant_id="tenant_demo",
        name="Explore Model",
        provider="openai_compatible",
        model="model-demo",
        api_key_encrypted="encrypted",
        capability_snapshot_json=model_capabilities,
        capability_checksum=capability_checksum(model_capabilities),
        preflight_status="ready",
    )
    db.add_all(
        [
            Tenant(id="tenant_demo", name="Demo"),
            User(
                id="user_demo",
                tenant_id="tenant_demo",
                username="demo",
                password_hash="x",
                role="admin",
            ),
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="探索数字员工",
                owner_user_id="user_demo",
            ),
            model,
        ]
    )
    db.flush()
    snapshot = _snapshot(explore_safe=True)
    plan = NormalizedPlan(
        goal="核验合同",
        success_criteria=(
            SuccessCriterion(id="verified", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="explore_contract",
                title="探索合同",
                kind="explore",
                capability_refs=("contract.query",),
            ),
            PlanStep(
                step_key="answer",
                title="形成结论",
                kind="answer",
                depends_on=("explore_contract",),
            ),
        ),
        budget={
            "max_steps": 4,
            "max_tool_calls": 5,
            "max_model_calls": 8,
            "max_input_tokens": 1000,
            "max_output_tokens": 1000,
            "max_total_tokens": 2000,
            "max_runtime_seconds": 300,
        },
    )
    instance = SopExecutionStore(db).start_dynamic_instance(
        tenant_id="tenant_demo",
        session_id="session_explore",
        agent_id="agent_demo",
        initiator_user_id="user_demo",
        plan=plan,
        capability_snapshot={
            "tools": [snapshot.model_dump(mode="json")],
            "model": {
                "model_config_id": model.id,
                "capabilities": model_capabilities,
                "checksum": model.capability_checksum,
            },
        },
    )[0]
    db.commit()
    return model, snapshot, instance


def test_explore_runtime_persists_operations_and_only_returns_compressed_report() -> None:
    """真实父账本包含中间 Operation，但父模型上下文只获得报告和引用。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        client = _SequencedExploreClient()
        catalog = _RuntimeCatalog(model)
        executor = _ExploreExecutor()
        report = DynamicTaskAgent(
            db,
            catalog=catalog,
            tool_executor=executor,
            explore_proposer=ReadOnlyExploreProposer(client),
            explore_enabled=True,
        ).advance_next_explore_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="explore-worker",
            actor_user_id="user_demo",
        )

        assert report.report == "合同 C-001 已由权威系统核验。"
        assert executor.calls == 1
        assert catalog.reauthorize_calls == 1
        assert len(db.exec(select(SopOperation)).all()) == 1
        assert len(db.exec(select(ActionProposalRecord)).all()) == 2
        assert {row.status for row in db.exec(select(ActionProposalRecord)).all()} == {"consumed"}
        assert db.exec(select(SopNodeExecution)).one().status == "succeeded"
        assert db.exec(select(SopWorkItem)).all() == []
        assert "must-not-leak" not in str(client.payloads[1])

        projection = build_execution_context_projection(
            db,
            tenant_id="tenant_demo",
            execution_id=instance.id,
        )
        serialized = projection.model_dump_json()
        assert "合同 C-001 已由权威系统核验" in serialized
        assert "must-not-leak" not in serialized
        assert "contract_id" not in serialized


def test_explore_runtime_kill_switch_blocks_before_step_or_model_call() -> None:
    """全局开关关闭时即使计划和工具已冻结，也必须在任何模型或 Tool 外呼前拒绝。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        client = _SequencedExploreClient()
        executor = _ExploreExecutor()
        runtime = DynamicTaskAgent(
            db,
            catalog=_RuntimeCatalog(model),
            tool_executor=executor,
            explore_proposer=ReadOnlyExploreProposer(client),
            explore_enabled=False,
        )
        with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_EXPLORE_DISABLED"):
            runtime.advance_next_explore_step(
                execution_id=instance.id,
                model_config=model,
                worker_id="explore-worker",
                actor_user_id="user_demo",
            )
        assert client.calls == 0
        assert executor.calls == 0
        assert db.exec(select(SopNodeExecution)).all() == []


def test_explore_runtime_revocation_fails_without_attention_or_adapter_call() -> None:
    """能力在计划后撤销时探索确定失败，不降级为人工批准或继续调用 adapter。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        client = _SequencedExploreClient()
        executor = _ExploreExecutor()
        runtime = DynamicTaskAgent(
            db,
            catalog=_RuntimeCatalog(model, revoke=True),
            tool_executor=executor,
            explore_proposer=ReadOnlyExploreProposer(client),
            explore_enabled=True,
        )
        with pytest.raises(DynamicTaskAgentError, match="CAPABILITY_BINDING_REVOKED"):
            runtime.advance_next_explore_step(
                execution_id=instance.id,
                model_config=model,
                worker_id="explore-worker",
                actor_user_id="user_demo",
            )

        db.refresh(instance)
        assert instance.status == "failed"
        assert executor.calls == 0
        assert db.exec(select(SopOperation)).one().status == "cancelled"
        assert db.exec(select(SopWorkItem)).all() == []


def test_explore_runtime_reuses_running_read_after_adapter_crash() -> None:
    """回执前崩溃后恢复同一只读 Operation，不重新询问模型或创建第二条逻辑动作。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        client = _SequencedExploreClient()
        executor = _CrashOnceExploreExecutor()
        runtime = DynamicTaskAgent(
            db,
            catalog=_RuntimeCatalog(model),
            tool_executor=executor,
            explore_proposer=ReadOnlyExploreProposer(client),
            explore_enabled=True,
        )
        with pytest.raises(RuntimeError, match="adapter crash"):
            runtime.advance_next_explore_step(
                execution_id=instance.id,
                model_config=model,
                worker_id="explore-worker-before-crash",
                actor_user_id="user_demo",
            )
        operation = db.exec(select(SopOperation)).one()
        assert operation.status == "running"
        assert client.calls == 1

        report = runtime.advance_next_explore_step(
            execution_id=instance.id,
            model_config=model,
            worker_id="explore-worker-after-crash",
            actor_user_id="user_demo",
        )
        assert report.evidence[0].operation_id == operation.id
        assert executor.calls == 2
        assert client.calls == 2
        assert db.exec(select(SopOperation)).all() == [operation]


def test_explore_runtime_rejects_report_with_foreign_operation_reference() -> None:
    """报告引用其他执行或不存在的 Operation 时，父 Step 和 Execution 一起失败。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        runtime = DynamicTaskAgent(
            db,
            catalog=_RuntimeCatalog(model),
            tool_executor=_ExploreExecutor(),
            explore_proposer=ReadOnlyExploreProposer(_InvalidEvidenceExploreClient()),
            explore_enabled=True,
        )
        with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_EXPLORE_EVIDENCE_INVALID"):
            runtime.advance_next_explore_step(
                execution_id=instance.id,
                model_config=model,
                worker_id="explore-worker",
                actor_user_id="user_demo",
            )
        db.refresh(instance)
        assert instance.status == "failed"
        assert [row.status for row in db.exec(select(ActionProposalRecord)).all()] == [
            "consumed",
            "superseded",
        ]
        assert db.exec(select(SopWorkItem)).all() == []


def test_dynamic_run_completes_parent_answer_from_explore_report() -> None:
    """从 explore 到 answer、Result、Message 和 Execution 终态形成同一真实闭环。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        model, _snapshot_value, instance = _runtime_fixture(db)
        final_proposer = _FinalActionProposer()
        outcome = DynamicTaskAgent(
            db,
            catalog=_RuntimeCatalog(model),
            tool_executor=_ExploreExecutor(),
            explore_proposer=ReadOnlyExploreProposer(_SequencedExploreClient()),
            explore_enabled=True,
            action_proposer=final_proposer,
        ).run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="full-explore-worker",
            actor_user_id="user_demo",
        )

        db.refresh(instance)
        assert outcome.status == "succeeded"
        assert instance.status == "succeeded"
        assert final_proposer.calls == 1
        assert outcome.message is not None
        assert outcome.message.content.startswith("# 核验结论")
        assert db.exec(select(Message)).all() == [outcome.message]
        assert [row.step_kind for row in db.exec(select(SopNodeExecution)).all()] == [
            "explore",
            "answer",
        ]
