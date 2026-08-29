"""
@Time       : 2026/08/12 23:45
@Author     : zhanglp8181
@File       : test_dynamic_task_parallel_reads.py
@CallChain  : pytest → DynamicTaskAgent parallel wave → detached ToolExecutor → result inbox
@Description: 验证有界纯读并发、稳定结算顺序、独立 Session 和默认串行退路。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import threading

import pytest
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.db.models import (
    AgentProfile,
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    DynamicReadDispatchResult,
    DynamicTaskQuotaLease,
    ModelConfig,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    Tenant,
    Tool,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.worker import reconcile_parallel_read_batches
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
from app.dynamic_tasks.execution_context import project_result_for_model
from app.dynamic_tasks.quotas import DynamicTaskQuotaError, DynamicTaskQuotaService
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

        second, _ = SopExecutionStore(db).start_dynamic_instance(
            tenant_id=tenant.id,
            session_id="session_parallel_quota_denied",
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
        acquire_calls = 0
        original_acquire = DynamicTaskQuotaService.acquire_parallel_contract

        def deny_second_contract(self, operation, *, concurrency_key, limit):
            """在第二个候选制造 backpressure，验证整个 wave 保存点回滚。"""

            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 2:
                raise DynamicTaskQuotaError("DYNAMIC_TASK_PARALLEL_CONTRACT_QUOTA_EXCEEDED")
            return original_acquire(
                self,
                operation,
                concurrency_key=concurrency_key,
                limit=limit,
            )

        monkeypatch.setattr(
            DynamicTaskQuotaService,
            "acquire_parallel_contract",
            deny_second_contract,
        )
        with pytest.raises(DynamicTaskQuotaError):
            agent.advance_ready_parallel_reads(
                execution_id=second.id,
                model_config=model,
                worker_id="parallel-quota-denied",
                actor_user_id=user.id,
            )
        db.commit()

        assert db.exec(
            select(SopOperation).where(SopOperation.instance_id == second.id)
        ).all() == []
        assert db.exec(
            select(SopNodeExecution).where(SopNodeExecution.instance_id == second.id)
        ).all() == []
        assert db.exec(
            select(DynamicReadDispatchBatch).where(
                DynamicReadDispatchBatch.execution_id == second.id
            )
        ).all() == []
        assert db.exec(
            select(DynamicTaskQuotaLease).where(
                DynamicTaskQuotaLease.holder_type == "operation"
            )
        ).all() == []
    engine.dispose()


def test_expired_parallel_wave_is_reconciled_without_running_orphans(
    tmp_path: Path,
) -> None:
    """进程在持久派发后崩溃时，扫描器必须终结 Operation、Node、Batch 与 Execution。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'parallel-recovery.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        tenant = Tenant(id="tenant_recovery", name="Recovery")
        user = User(
            id="user_recovery",
            tenant_id=tenant.id,
            username="recovery",
            password_hash="x",
        )
        db.add(tenant)
        db.add(user)
        db.add(
            AgentProfile(
                id="agent_recovery",
                tenant_id=tenant.id,
                name="恢复测试 Agent",
                is_overall=False,
            )
        )
        plan = _ParallelPlanner().create_plan(
            goal="恢复读取",
            success_criteria=(
                SuccessCriterion(id="done", type="assertion", spec={"required": True}),
            ),
            capabilities=(),
        )
        instance, _ = SopExecutionStore(db).start_dynamic_instance(
            tenant_id=tenant.id,
            session_id="session_recovery",
            agent_id="agent_recovery",
            initiator_user_id=user.id,
            plan=plan,
            capability_snapshot={
                "tools": [
                    {"name": "read.a", "capability_id": "read.a"},
                    {"name": "read.b", "capability_id": "read.b"},
                ]
            },
        )
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="seed-recovery"):
            node = store.enter_node(
                instance,
                "read_a",
                step_key="read_a",
                plan_revision_id=instance.current_plan_revision_id,
                step_kind="tool.read",
                title="读取 A",
                required=True,
            )
            operation = SopOperation(
                tenant_id=tenant.id,
                instance_id=instance.id,
                node_execution_id=node.id,
                    operation_name="read.a",
                    idempotency_key="parallel-recovery-read-a",
                    logical_action_id="parallel-recovery-read-a",
                effect_kind="read",
                status="running",
                request_json={"source": "read.a"},
                request_fingerprint="b" * 64,
                capability_checksum="c" * 64,
                revision=1,
            )
            db.add(operation)
            db.flush()
            attempt = SopOperationAttempt(
                tenant_id=tenant.id,
                instance_id=instance.id,
                operation_id=operation.id,
                node_execution_id=node.id,
                attempt_number=1,
                dispatch_token="parallel-recovery-attempt",
                status="running",
                started_at=SopExecutionStore(db).database_now(),
            )
            db.add(attempt)
            db.flush()
        batch = DynamicReadDispatchBatch(
            tenant_id=tenant.id,
            execution_id=instance.id,
            plan_revision_id=str(instance.current_plan_revision_id),
            wave_checksum="d" * 64,
            ordered_step_keys_json=["read_a"],
            status="dispatched",
            parallelism=1,
            deadline_at=SopExecutionStore(db).database_now() - timedelta(seconds=1),
        )
        db.add(batch)
        db.flush()
        item = DynamicReadDispatchItem(
            tenant_id=tenant.id,
            batch_id=batch.id,
            execution_id=instance.id,
            plan_revision_id=str(instance.current_plan_revision_id),
            position=0,
            step_key="read_a",
            node_execution_id=node.id,
            operation_id=operation.id,
            operation_revision_at_start=operation.revision,
            dispatch_token="e" * 64,
            capability_checksum="c" * 64,
            request_fingerprint="b" * 64,
            status="dispatched",
        )
        db.add(item)
        db.commit()

        assert reconcile_parallel_read_batches(db) == 1
        db.refresh(instance)
        db.refresh(node)
        db.refresh(operation)
        db.refresh(batch)
        db.refresh(item)
        db.refresh(attempt)

        assert instance.status == "failed"
        assert node.status == "failed"
        assert operation.status == "failed"
        assert attempt.status == "failed"
        assert batch.status == "failed"
        assert item.status == "settled"
    engine.dispose()


def test_parallel_result_projection_never_persists_declared_secret_fields() -> None:
    """显式 output schema 也不能授权 token、authorization 等凭据进入 inbox。"""

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "token": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"authorization": {"type": "string"}},
            },
        },
    }
    projected = project_result_for_model(
        {"name": "ok", "token": "TOP-SECRET", "nested": {"authorization": "Bearer x"}},
        schema,
    )

    assert projected == {"name": "ok", "nested": {}}
