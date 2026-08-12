"""
@Time       : 2026/08/12 23:45
@Author     : zhanglp8181
@File       : test_dynamic_task_parallel_reads.py
@CallChain  : pytest → DynamicTaskAgent parallel wave → detached ToolExecutor → result inbox
@Description: 验证有界纯读并发、稳定结算顺序、独立 Session 和默认串行退路。
"""

from __future__ import annotations

from pathlib import Path
import threading

from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.db.models import (
    AgentProfile,
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    DynamicReadDispatchResult,
    ModelConfig,
    SopNodeExecution,
    Tenant,
    Tool,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import (
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    capability_checksum,
    publish_tool_contract,
)
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


def _contract(key: str) -> ToolReliabilityContract:
    """发布一个可并行且无副作用的纯读工具契约。"""

    return ToolReliabilityContract(
        risk_class="read",
        side_effect="none",
        confirmation_policy="none",
        timeout_policy="failed",
        dynamic_task_enabled=True,
        parallel_safe=True,
        concurrency_key=key,
        max_in_flight=2,
    )


class _ParallelPlanner:
    """生成两个同层只读步骤和一个稳定汇聚 answer。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """返回可由并行 wave 识别的固定 DAG。"""

        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="read_a",
                    title="读取 A",
                    kind="tool.read",
                    capability_refs=("read.a",),
                ),
                PlanStep(
                    step_key="read_b",
                    title="读取 B",
                    kind="tool.read",
                    capability_refs=("read.b",),
                ),
                PlanStep(
                    step_key="answer",
                    title="汇总",
                    kind="answer",
                    depends_on=("read_a", "read_b"),
                ),
            ),
            budget={"max_steps": 4, "max_tool_calls": 4, "max_model_calls": 6},
        )


class _ParallelProposer:
    """按当前步骤返回与冻结能力一致的完整工具提案。"""

    def propose(self, *, view, step):
        """把 step identity 映射到唯一只读能力。"""

        capability = "read.a" if step.step_key == "read_a" else "read.b"
        return CompletedProviderProposal(
            response_id=f"response-{step.step_key}",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=capability,
                arguments={"source": capability},
                rationale="读取权威事实",
            ),
        )


class _BlockingExecutor:
    """用 barrier 证明两个独立 Session 的外呼真实重叠。"""

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0
    session_ids: set[int] = set()

    def __init__(self, db: Session):
        """记录每个 worker 独立创建的 Session 身份。"""

        self.db = db

    def execute(self, tenant_id, tool_call, **kwargs):
        """等待另一调用进入后返回确定性结果。"""

        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            type(self).session_ids.add(id(self.db))
        self.barrier.wait(timeout=5)
        with self.lock:
            type(self).active -= 1
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"source": tool_call.name},
        )


def test_parallel_read_wave_uses_independent_sessions_and_stable_plan_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """验证真实并发、append-only inbox 和逆完成无关的 Plan 顺序结算。"""

    database_url = f"sqlite:///{tmp_path / 'parallel-reads.db'}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(get_settings(), "dynamic_task_max_parallel_reads", 2)
    _BlockingExecutor.barrier = threading.Barrier(2)
    _BlockingExecutor.active = 0
    _BlockingExecutor.max_active = 0
    _BlockingExecutor.session_ids = set()
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id="tenant_parallel", name="Parallel")
        user = User(
            id="user_parallel",
            tenant_id=tenant.id,
            username="parallel",
            password_hash="x",
        )
        profile = AgentProfile(
            id="agent_parallel",
            tenant_id=tenant.id,
            name="并行数字员工",
            owner_user_id=user.id,
        )
        model_capabilities = {
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "structured_output": True,
            "tool_calling": True,
        }
        model = ModelConfig(
            id="model_parallel",
            tenant_id=tenant.id,
            name="Parallel model",
            api_key_encrypted="x",
            model="model-demo",
            capability_snapshot_json=model_capabilities,
            capability_checksum=capability_checksum(model_capabilities),
            preflight_status="ready",
        )
        db.add(tenant)
        db.add(user)
        db.add(profile)
        db.add(model)
        for index, name in enumerate(("read.a", "read.b")):
            tool = Tool(
                id=f"tool_parallel_{index}",
                tenant_id=tenant.id,
                name=name,
                method="GET",
                url="https://example.invalid",
            )
            publish_tool_contract(tool, _contract(f"key-{index}"))
            db.add(tool)
            db.flush()
            ensure_private_resource_binding(db, tenant.id, profile.id, "tool", tool.id)
        db.commit()
        catalog = DynamicCapabilityCatalog(db)
        snapshots = catalog.list_tools(tenant.id, profile.id)
        plan = _ParallelPlanner().create_plan(
            goal="读取两个来源",
            success_criteria=(
                SuccessCriterion(id="done", type="assertion", spec={"required": True}),
            ),
            capabilities=snapshots,
        )
        instance, _ = SopExecutionStore(db).start_dynamic_instance(
            tenant_id=tenant.id,
            session_id="session_parallel",
            agent_id=profile.id,
            initiator_user_id=user.id,
            plan=plan,
            capability_snapshot={
                "tools": [row.model_dump(mode="json") for row in snapshots],
                "model": {
                    "model_config_id": model.id,
                    "capabilities": model_capabilities,
                    "checksum": model.capability_checksum,
                },
            },
        )
        db.commit()
        agent = DynamicTaskAgent(
            db,
            catalog=catalog,
            planner=_ParallelPlanner(),
            action_proposer=_ParallelProposer(),
            parallel_tool_executor_factory=_BlockingExecutor,
        )

        results = agent.advance_ready_parallel_reads(
            execution_id=instance.id,
            model_config=model,
            worker_id="parallel-coordinator",
            actor_user_id=user.id,
        )

        assert [step.step_key for step, _ in results] == ["read_a", "read_b"]
        assert _BlockingExecutor.max_active == 2
        assert len(_BlockingExecutor.session_ids) == 2
        assert db.exec(select(DynamicReadDispatchBatch)).one().status == "succeeded"
        assert len(db.exec(select(DynamicReadDispatchItem)).all()) == 2
        assert len(db.exec(select(DynamicReadDispatchResult)).all()) == 2
        assert [row.step_key for row in db.exec(
            select(SopNodeExecution).order_by(SopNodeExecution.created_at, SopNodeExecution.id)
        ).all()] == ["read_a", "read_b"]
    engine.dispose()
