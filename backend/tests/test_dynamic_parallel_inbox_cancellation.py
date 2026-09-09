"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_dynamic_parallel_inbox_cancellation.py
@CallChain  : pytest → 真实并行读 worker/失败补写 → 旧状态读取 → 独立取消事务 → inbox CAS
@Description: 在临时文件 SQLite 与迁移至 head 的随机 MySQL 库核验迟到生产者 CAS 及回滚。
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import update
from sqlalchemy.engine import Engine
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
    SopInstance,
    SopOperation,
    Tenant,
    Tool,
    User,
    utc_now,
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
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


@dataclass
class _InboxInterleaving:
    """在数据访问边界暂停真实生产者，不替换 CAS、写结果或取消生产方法。"""

    read_old: Event = field(default_factory=Event)
    allow_write: Event = field(default_factory=Event)
    write_finished: Event = field(default_factory=Event)
    allow_return: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    io_sessions: set[int] = field(default_factory=set)
    completed_sources: set[str] = field(default_factory=set)
    item_id: str | None = None
    producer_path: str | None = None
    writer_session: int | None = None
    writer_connection: object | None = None
    result_snapshot: dict[str, object] | None = None
    phases: list[str] = field(default_factory=list)


class _ReadProposer:
    """只替代外部模型，实际计划、冻结、派发和结算仍由生产 Runtime 执行。"""

    def propose(self, *, step: PlanStep, **_kwargs) -> CompletedProviderProposal:
        """为两个同层只读节点分别返回带固定身份的合法提案。"""

        return CompletedProviderProposal(
            response_id=f"inbox-{step.step_key}",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=step.capability_refs[0],
                arguments={"source": step.capability_refs[0]},
                rationale="读取公开数据以验证提交交错",
            ),
        )


@pytest.fixture
def dispatch_engine(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """独立连接访问临时文件库或已迁移的随机 MySQL 库，不用 create_all 替代 MySQL 迁移。"""

    if getattr(request, "param", "sqlite") == "mysql":
        database_url = request.getfixturevalue("mysql_database_url")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = database_url
        alembic_command.upgrade(config, "head")
        engine = create_engine(
            database_url, isolation_level="REPEATABLE READ", poolclass=NullPool, echo=False,
        )
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'parallel-inbox-cancellation.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
            poolclass=NullPool,
        )
        SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(get_settings(), "dynamic_task_max_parallel_reads", 2)
    try:
        with Session(engine, expire_on_commit=False) as db:
            facts = {
                "protocol_version": "dynamic-v1", "sdk_available": True,
                "credentials_verified": True, "structured_output": True, "tool_calling": True,
            }
            model = ModelConfig(
                id="inbox_model", tenant_id="inbox_tenant", name="无网络并发模型",
                api_key_encrypted="fixture-not-a-credential", model="fixture",
                base_url="https://example.invalid", capability_snapshot_json=facts,
                capability_checksum=capability_checksum(facts), preflight_status="ready",
            )
            db.add_all([
                Tenant(id="inbox_tenant", name="并发取消隔离租户"),
                User(id="inbox_user", tenant_id="inbox_tenant", username="inbox_fixture",
                     password_hash="fixture"),
                AgentProfile(id="inbox_agent", tenant_id="inbox_tenant", name="并发读专家",
                             owner_user_id="inbox_user"),
                model,
            ])
            for name in ("read.a", "read.b"):
                tool = Tool(tenant_id="inbox_tenant", name=name, method="GET",
                            url="https://example.invalid")
                publish_tool_contract(tool, ToolReliabilityContract(
                    risk_class="read", side_effect="none", confirmation_policy="none",
                    timeout_policy="failed", dynamic_task_enabled=True, parallel_safe=True,
                    concurrency_key=name, max_in_flight=2,
                ))
                db.add(tool)
                db.flush()
                ensure_private_resource_binding(
                    db, "inbox_tenant", "inbox_agent", "tool", tool.id,
                )
            db.commit()
            snapshots = DynamicCapabilityCatalog(db).list_tools("inbox_tenant", "inbox_agent")
            plan = NormalizedPlan(
                goal="独立读取两个公开来源",
                success_criteria=(SuccessCriterion(id="evidence", type="assertion", spec={}),),
                steps=(
                    PlanStep(step_key="read_a", title="来源 A", kind="tool.read",
                             capability_refs=("read.a",)),
                    PlanStep(step_key="read_b", title="来源 B", kind="tool.read",
                             capability_refs=("read.b",)),
                    PlanStep(step_key="answer", title="汇总", kind="answer",
                             depends_on=("read_a", "read_b")),
                ),
                budget={"max_steps": 3, "max_tool_calls": 4, "max_model_calls": 4,
                        "max_parallel_read_seconds": 45},
            )
            SopExecutionStore(db).start_dynamic_instance(
                tenant_id="inbox_tenant", session_id="inbox_session", agent_id="inbox_agent",
                initiator_user_id="inbox_user", plan=plan,
                capability_snapshot={
                    "tools": [row.model_dump(mode="json") for row in snapshots],
                    "model": {"model_config_id": model.id, "capabilities": facts,
                              "checksum": model.capability_checksum},
                },
            )
            db.commit()
        yield engine
    finally:
        engine.dispose()


def _snapshot(engine: Engine, item_id: str) -> dict[str, object]:
    """在生产者退出之前独立读库，避免后续协调器清扫掩盖迟到覆盖。"""

    with Session(engine) as observer:
        item = observer.get(DynamicReadDispatchItem, item_id)
        assert item is not None
        instance = observer.get(SopInstance, item.execution_id)
        operation = observer.get(SopOperation, item.operation_id)
        assert instance is not None and operation is not None
        rows = observer.exec(select(DynamicReadDispatchResult).where(
            DynamicReadDispatchResult.tenant_id == item.tenant_id,
            DynamicReadDispatchResult.dispatch_token == item.dispatch_token,
        )).all()
        return {
            "item_status": item.status,
            "execution_status": instance.status,
            "operation_status": operation.status,
            "inbox": [
                {"id": row.id, "status": row.status,
                 "result": row.result_json, "error": row.error_json}
                for row in rows
            ],
        }


def _install_read_barrier(
    engine: Engine, gate: _InboxInterleaving, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅观测实际 Session.get/退出时机，所有数据库操作及生产 helper 仍执行原实现。"""

    class ObservedSession(Session):
        """把指定生产者停在已经读取旧状态、尚未 CAS/插入的窗口。"""

        def get(self, entity, ident, **kwargs):
            """真实 SELECT 返回后才暂停，排除 I/O 初始读取和其他步骤。"""

            item = super().get(entity, ident, **kwargs)
            if entity is not DynamicReadDispatchItem or item is None or item.step_key != "read_a":
                return item
            with gate.lock:
                is_writer = (
                    "read.a" in gate.completed_sources and id(self) not in gate.io_sessions
                    and gate.writer_session is None
                )
                if not is_writer:
                    return item
                assert item.status == "dispatched"
                gate.writer_session = id(self)
                gate.writer_connection = self.connection().connection.dbapi_connection
                gate.item_id = item.id
                frame = inspect.currentframe()
                try:
                    assert frame is not None and frame.f_back is not None
                    gate.producer_path = frame.f_back.f_code.co_name
                finally:
                    del frame
                gate.phases.append("worker_read_dispatched")
            gate.read_old.set()
            assert gate.allow_write.wait(timeout=20), "未按约定放行 inbox 生产者"
            return item

        def __exit__(self, exc_type, exc_value, traceback):
            """等真实事务提交或回滚并关闭后取证，再阻止协调器二次清扫掩盖结果。"""

            result = super().__exit__(exc_type, exc_value, traceback)
            if id(self) == gate.writer_session:
                assert gate.item_id is not None
                gate.result_snapshot = _snapshot(engine, gate.item_id)
                gate.phases.append("worker_transaction_finished")
                gate.write_finished.set()
                assert gate.allow_return.wait(timeout=20), "未释放已完成取证的生产者"
            return result

    monkeypatch.setattr("app.dynamic_tasks.agent.Session", ObservedSession)


def _run_real_wave(engine: Engine, gate: _InboxInterleaving, outcome: str):
    """运行真实并行派发及两个生产写入口；仅模型和外部工具返回确定性样本。"""

    class SourceExecutor:
        """记录 I/O Session，以区分实际 inbox 写入 Session，禁止用同连接伪造并发。"""

        def __init__(self, db: Session) -> None:
            """保存仍存活的 I/O Session 身份供观测器排除初始读取。"""

            self.db = db
            with gate.lock:
                gate.io_sessions.add(id(db))

        def execute(self, _tenant: str, call: ToolCall, **_kwargs) -> ToolResult:
            """分别触发正常成功、Provider 失败和 worker 异常补写，均不发送网络请求。"""

            with gate.lock:
                gate.completed_sources.add(call.name)
            if call.name == "read.a":
                if outcome == "worker-exception":
                    raise RuntimeError("隔离测试的 I/O worker 异常")
                if outcome == "provider-failure":
                    return ToolResult(tool_name=call.name, success=False,
                                      error=ToolError(code="FIXTURE_PROVIDER_FAILURE",
                                                      message="公开来源暂不可用"))
            return ToolResult(tool_name=call.name, success=True, data={"source": call.name})

    with Session(engine, expire_on_commit=False) as db:
        instance = db.exec(select(SopInstance)).one()
        model = db.get(ModelConfig, "inbox_model")
        assert model is not None
        agent = DynamicTaskAgent(
            db, action_proposer=_ReadProposer(), parallel_tool_executor_factory=SourceExecutor,
        )
        return agent.advance_ready_parallel_reads(
            execution_id=instance.id, model_config=model, worker_id="inbox-coordinator",
            actor_user_id="inbox_user",
        )


def _cancel_and_discard(engine: Engine, gate: _InboxInterleaving) -> None:
    """用另一物理连接提交正式取消和协调器条件丢弃，旧 worker 此时仍持有旧对象。"""

    assert gate.item_id is not None
    with Session(engine, expire_on_commit=False) as cancel_db:
        assert cancel_db.connection().connection.dbapi_connection is not gate.writer_connection
        item = cancel_db.get(DynamicReadDispatchItem, gate.item_id)
        assert item is not None
        instance = cancel_db.get(SopInstance, item.execution_id)
        assert instance is not None and instance.status == "running"
        store = SopExecutionStore(cancel_db)
        with store.owned_for_cancellation(instance, worker_id="inbox-authenticated-cancel"):
            assert store.request_cancellation(instance, actor_user_id="inbox_user",
                                              reason="生产者读旧状态后用户取消。")
        changed = cancel_db.execute(update(DynamicReadDispatchItem).where(
            DynamicReadDispatchItem.id == item.id,
            DynamicReadDispatchItem.tenant_id == item.tenant_id,
            DynamicReadDispatchItem.execution_id == instance.id,
            DynamicReadDispatchItem.batch_id == item.batch_id,
            DynamicReadDispatchItem.dispatch_token == item.dispatch_token,
            DynamicReadDispatchItem.status.in_(["dispatched", "result_ready"]),
        ).values(status="discarded", updated_at=utc_now())
            .execution_options(synchronize_session="fetch"))
        assert changed.rowcount == 1
        batch = cancel_db.get(DynamicReadDispatchBatch, item.batch_id)
        assert batch is not None
        batch.status = "cancelled"
        cancel_db.add(batch)
        cancel_db.commit()
    snapshot = _snapshot(engine, gate.item_id)
    assert snapshot["item_status"] == "discarded"
    assert snapshot["execution_status"] == "cancelled"
    assert snapshot["operation_status"] == "cancelled"
    assert snapshot["inbox"] == []
    gate.phases.append("coordinator_cancel_discard_committed")


def _assert_claim_and_insert_rollback(engine: Engine, item_id: str) -> None:
    """实际调用新 CAS helper 并 flush inbox 后故意失败，证明 helper 没有提前提交。"""

    before = _snapshot(engine, item_id)
    with pytest.raises(RuntimeError, match="故意回滚"):
        with Session(engine, expire_on_commit=False) as writer:
            item = writer.get(DynamicReadDispatchItem, item_id)
            assert item is not None
            assert DynamicTaskAgent._claim_parallel_inbox_write(writer, item)
            writer.add(DynamicReadDispatchResult(
                tenant_id=item.tenant_id, dispatch_token=item.dispatch_token,
                status="succeeded", result_json={"data": {"fixture": "rolled-back"}},
                started_at=utc_now(), finished_at=utc_now(),
            ))
            writer.flush()
            assert writer.exec(select(DynamicReadDispatchResult).where(
                DynamicReadDispatchResult.dispatch_token == item.dispatch_token,
            )).one().status == "succeeded"
            raise RuntimeError("inbox 插入后的故意回滚")
    assert _snapshot(engine, item_id) == before


def _assert_append_once(engine: Engine, item_id: str, before: dict[str, object]) -> None:
    """已 result_ready 时直接 CAS 和真实失败补写重放都不得覆盖原回执或追加重复行。"""

    with Session(engine, expire_on_commit=False) as duplicate:
        item = duplicate.get(DynamicReadDispatchItem, item_id)
        assert item is not None and item.status == "result_ready"
        assert not DynamicTaskAgent._claim_parallel_inbox_write(duplicate, item)
        duplicate.commit()
    DynamicTaskAgent._append_parallel_dispatch_failure(
        engine, item_id, code="FIXTURE_DUPLICATE", message="重复补写不得替换既有回执",
    )
    assert _snapshot(engine, item_id) == before


@pytest.mark.parametrize(
    ("dispatch_engine", "interleaving", "outcome"),
    [
        *[
            pytest.param("sqlite", interleaving, outcome,
                         id=f"sqlite-{interleaving}-{outcome}")
            for interleaving in ("cancel", "normal", "rollback")
            for outcome in ("success", "provider-failure", "worker-exception")
        ],
        pytest.param("mysql", "cancel", "success", marks=pytest.mark.mysql,
                     id="mysql-cancel-success"),
        pytest.param("mysql", "cancel", "worker-exception", marks=pytest.mark.mysql,
                     id="mysql-cancel-worker-exception"),
        pytest.param("mysql", "rollback", "success", marks=pytest.mark.mysql,
                     id="mysql-rollback-success"),
    ],
    indirect=["dispatch_engine"],
)
def test_parallel_producer_respects_committed_discard_and_atomic_inbox(
    dispatch_engine: Engine, monkeypatch: pytest.MonkeyPatch, outcome: str, interleaving: str,
) -> None:
    """两库真实写入口在旧读后取消须拒绝迟到回执，回滚分支也核验正常写入和幂等。"""

    gate = _InboxInterleaving()
    _install_read_barrier(dispatch_engine, gate, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(_run_real_wave, dispatch_engine, gate, outcome)
        try:
            assert gate.read_old.wait(timeout=20), "真实生产者未到达已读 dispatched 的观测边界"
            assert gate.item_id is not None
            assert gate.producer_path == (
                "_append_parallel_dispatch_failure" if outcome == "worker-exception" else "execute_one"
            )
            if interleaving == "cancel":
                _cancel_and_discard(dispatch_engine, gate)
            elif interleaving == "rollback":
                _assert_claim_and_insert_rollback(dispatch_engine, gate.item_id)
                gate.phases.append("claim_and_insert_rolled_back")
            else:
                gate.phases.append("normal_write_allowed")
            gate.allow_write.set()
            assert gate.write_finished.wait(timeout=20), "生产者没有完成真实提交或回滚"
            snapshot = gate.result_snapshot
            assert snapshot is not None
            assert gate.phases == [
                "worker_read_dispatched",
                {"cancel": "coordinator_cancel_discard_committed",
                 "normal": "normal_write_allowed", "rollback": "claim_and_insert_rolled_back"}[
                    interleaving
                ],
                "worker_transaction_finished",
            ]
            if interleaving == "cancel":
                assert snapshot["item_status"] == "discarded", "迟到生产者复活了已丢弃的派发"
                assert snapshot["execution_status"] == "cancelled"
                assert snapshot["operation_status"] == "cancelled"
                assert snapshot["inbox"] == [], "CAS 失败不能留下迟到成功或失败 inbox"
            else:
                assert snapshot["item_status"] == "result_ready"
                rows = snapshot["inbox"]
                assert len(rows) == 1
                assert rows[0]["status"] == ("succeeded" if outcome == "success" else "failed")
                _assert_append_once(dispatch_engine, gate.item_id, snapshot)
        finally:
            gate.allow_write.set()
            gate.allow_return.set()
            wave = future.result(timeout=20)
        if interleaving == "cancel":
            assert wave == ()
        else:
            assert [step.step_key for step, _result in wave] == ["read_a", "read_b"]
            assert [result.success for _step, result in wave] == [outcome == "success", True]
