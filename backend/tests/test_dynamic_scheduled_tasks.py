"""
@Time       : 2026/08/11 01:30
@Author     : zhanglp8181
@File       : test_dynamic_scheduled_tasks.py
@CallChain  : pytest → ScheduledTask service/AgentLoop → Signal/Dynamic Execution → Run 对账
@Description: 验证调度动态任务的稳定来源、误期/并发、实时鉴权、挂起恢复和终态闭环。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop
from app.core.non_sop_capability import (
    NonSopCapabilityDecision,
    NonSopCapabilityRouteResult,
)
from app.db.models import (
    AgentProfile,
    ChatSession,
    ExecutionSignal,
    Message,
    ScheduledTask,
    ScheduledTaskRun,
    SopInstance,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent, DynamicTaskAgentError
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.dynamic_tasks.worker import due_dynamic_task_signals
from app.dynamic_tasks import worker as dynamic_worker
from app.general_skills.schema import GeneralSkillSelection
from app.scheduled_tasks import service as scheduled_service
from app.scheduled_tasks import worker as scheduled_worker
from app.session.session_schema import ChatTurnRequest
from app.sop_runtime.execution_control import ExecutionControlService, canonical_checksum
from app.sop_runtime.execution_store import SopExecutionStore


def test_same_due_time_reuses_run_and_freezes_source_snapshot() -> None:
    """任务定义随后变化时，同一到期事实仍返回原 run 和原始语义快照。"""

    with _session() as db:
        task = _seed_task(db)
        scheduled_for = utc_now() + timedelta(minutes=1)

        first = scheduled_service._prepare_scheduled_task_run(
            db, task, scheduled_for, False
        )
        frozen_checksum = first.source_checksum
        task.prompt = "后来修改的任务内容"
        db.add(task)
        db.commit()
        second = scheduled_service._prepare_scheduled_task_run(
            db, task, scheduled_for, False
        )

        assert second.id == first.id
        assert second.source_ref == first.source_ref
        assert second.source_checksum == frozen_checksum
        assert second.source_snapshot_json["prompt"] == "生成合同风险周报"
        assert second.source_kind == "schedule"


def test_skip_misfire_creates_terminal_run_without_agent_call(monkeypatch) -> None:
    """超过宽限期的 skip 任务只生成可审计 skipped run，不调用 AgentLoop。"""

    monkeypatch.setattr(
        scheduled_service,
        "AgentLoop",
        lambda _db: (_ for _ in ()).throw(AssertionError("misfire must not run agent")),
    )
    with _session() as db:
        task = _seed_task(db, misfire_policy="skip")
        run = scheduled_service.execute_scheduled_task(
            db,
            task,
            scheduled_for=utc_now() - timedelta(minutes=5),
        )

        assert run.status == "skipped"
        assert "misfire" in str(run.error)
        assert run.session_id is None
        assert task.run_count == 1


def test_schedule_dispatch_capacity_releases_lease_without_losing_due_fact(monkeypatch) -> None:
    """执行池满载时不得创建半成品 run，且必须释放任务租约供后续扫描重试。"""

    monkeypatch.setattr(
        scheduled_service,
        "_scheduled_dispatch_slots",
        SimpleNamespace(acquire=lambda **_kwargs: False),
    )
    with _session() as db:
        task = _seed_task(db)
        task.lease_owner = "busy-worker"
        task.lease_until = utc_now() + timedelta(minutes=10)
        db.add(task)
        db.commit()

        run = scheduled_service.start_scheduled_task_async(db, task)

        db.refresh(task)
        assert run is None
        assert task.lease_owner is None
        assert task.lease_until is None
        assert db.exec(select(ScheduledTaskRun)).all() == []


def test_resident_worker_dispatches_dynamic_signals_without_inline_execution(monkeypatch) -> None:
    """常驻扫描只提交有界后台工作；确定性的 once 模式仍在当前线程完成。"""

    signal = SimpleNamespace(id="signal_schedule")
    submitted: list[str] = []
    inline: list[str] = []
    monkeypatch.setattr(scheduled_worker, "due_dynamic_task_signals", lambda _db: [signal])
    monkeypatch.setattr(
        scheduled_worker,
        "start_dynamic_task_signal_async",
        lambda signal_id: submitted.append(signal_id) is None,
    )
    monkeypatch.setattr(
        scheduled_worker,
        "process_dynamic_task_signal",
        lambda _db, item: inline.append(item.id),
    )

    assert scheduled_worker._process_due_dynamic_signals(SimpleNamespace(), once=False) == 1
    assert submitted == [signal.id]
    assert inline == []

    assert scheduled_worker._process_due_dynamic_signals(SimpleNamespace(), once=True) == 1
    assert inline == [signal.id]


def test_dynamic_signal_dispatch_is_deduplicated_and_bounded(monkeypatch) -> None:
    """同一 Signal 不重复排队，达到容量后新 Signal 留在数据库等待下一轮。"""

    submitted: list[str] = []
    monkeypatch.setattr(dynamic_worker, "SIGNAL_DISPATCH_CAPACITY", 1)
    monkeypatch.setattr(
        dynamic_worker,
        "_signal_executor",
        SimpleNamespace(submit=lambda _func, signal_id: submitted.append(signal_id)),
    )
    with dynamic_worker._signal_inflight_lock:
        dynamic_worker._signal_inflight.clear()
    try:
        assert dynamic_worker.start_dynamic_task_signal_async("signal_a") is True
        assert dynamic_worker.start_dynamic_task_signal_async("signal_a") is False
        assert dynamic_worker.start_dynamic_task_signal_async("signal_b") is False
        assert submitted == ["signal_a"]
    finally:
        with dynamic_worker._signal_inflight_lock:
            dynamic_worker._signal_inflight.clear()


def test_forbid_policy_treats_waiting_approval_as_active_overlap() -> None:
    """上一轮等待审批时 forbid 必须跳过新轮次，不能并行制造第二个后果动作。"""

    with _session() as db:
        task = _seed_task(db, concurrency_policy="forbid")
        first_due = utc_now() - timedelta(minutes=2)
        first = scheduled_service._create_run(
            db,
            task,
            first_due,
            "waiting",
            manual=False,
        )
        db.commit()

        second = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            first_due + timedelta(minutes=1),
            False,
        )

        assert first.status == "waiting"
        assert second.status == "skipped"
        assert "forbid" in str(second.error)


def test_allow_policy_creates_independent_run_while_previous_run_waits() -> None:
    """allow 策略显式允许等待中的上一轮与新轮次并存，且两轮来源身份不能复用。"""

    with _session() as db:
        task = _seed_task(db, concurrency_policy="allow")
        first_due = utc_now() - timedelta(minutes=2)
        first = scheduled_service._create_run(
            db,
            task,
            first_due,
            "waiting",
            manual=False,
        )
        db.commit()

        second = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            first_due + timedelta(minutes=1),
            False,
        )

        assert first.status == "waiting"
        assert second.status == "running"
        assert second.id != first.id
        assert second.source_ref != first.source_ref


def test_coalesce_misfire_creates_recoverable_run_instead_of_skipping() -> None:
    """coalesce 对停机期间错过的时间点生成唯一可恢复 run，不误套用 skip 宽限策略。"""

    with _session() as db:
        task = _seed_task(db, misfire_policy="coalesce")
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() - timedelta(minutes=5),
            False,
        )

        assert run.status == "running"
        assert run.error is None
        assert run.session_id is not None


def test_database_lease_prevents_second_worker_from_claiming_same_due_task() -> None:
    """两个 worker 依次扫描同一到期事实时，数据库租约只允许首个 worker 取得任务。"""

    with _session() as first_db:
        task = _seed_task(first_db)
        task.next_run_at = utc_now() - timedelta(seconds=1)
        first_db.add(task)
        first_db.commit()
        claimed = scheduled_service.due_scheduled_tasks(first_db)

        with Session(first_db.get_bind()) as second_db:
            duplicate = scheduled_service.due_scheduled_tasks(second_db)

        assert [item.id for item in claimed] == [task.id]
        assert duplicate == []


def test_runtime_reauthorizes_creator_and_fails_before_agent(monkeypatch) -> None:
    """创建者停用后，历史 Schedule 不得凭旧配置继续调用 Agent 或工具。"""

    monkeypatch.setattr(
        scheduled_service,
        "AgentLoop",
        lambda _db: (_ for _ in ()).throw(AssertionError("revoked run must fail closed")),
    )
    with _session() as db:
        task = _seed_task(db)
        user = db.get(User, task.created_by_user_id)
        assert user is not None
        user.membership_status = "suspended"
        db.add(user)
        db.commit()

        run = scheduled_service.execute_scheduled_task(
            db,
            task,
            scheduled_for=utc_now() + timedelta(seconds=1),
        )

        assert run.status == "failed"
        assert run.error == "SCHEDULE_INITIATOR_INACTIVE"
        assert run.execution_id is None


def test_agent_loop_resolves_scheduled_run_as_dynamic_source() -> None:
    """ScheduledTaskRun id 是动态 Execution 的稳定 source_ref，不能退化成 user message id。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        assert run.session_id is not None
        loop = object.__new__(AgentLoop)
        loop.db = db
        session = db.get(ChatSession, run.session_id)
        assert session is not None

        source = loop._dynamic_task_source(
            ChatTurnRequest(
                tenant_id=task.tenant_id,
                session_id=session.id,
                agent_id=task.agent_id,
                user_id=task.created_by_user_id,
                message=task.prompt,
                channel="scheduled_task",
                interaction_mode="scheduled_task",
            ),
            session,
            "message_should_not_be_source",
        )

        assert source == ("schedule", run.id)


def test_agent_loop_rejects_tampered_scheduled_source_snapshot() -> None:
    """run 来源快照被修改后即使其他 ID 匹配，也必须在创建 Execution 前失败关闭。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        assert run.session_id is not None
        run.source_snapshot_json = {**run.source_snapshot_json, "prompt": "篡改后的指令"}
        db.add(run)
        db.commit()
        session = db.get(ChatSession, run.session_id)
        assert session is not None
        loop = object.__new__(AgentLoop)
        loop.db = db

        with pytest.raises(DynamicTaskAgentError, match="DYNAMIC_SCHEDULE_SOURCE_TAMPERED"):
            loop._dynamic_task_source(
                ChatTurnRequest(
                    tenant_id=task.tenant_id,
                    session_id=session.id,
                    agent_id=task.agent_id,
                    user_id=task.created_by_user_id,
                    message=task.prompt,
                    channel="scheduled_task",
                    interaction_mode="scheduled_task",
                ),
                session,
                "message_schedule",
            )


def test_agent_loop_queues_durable_start_signal_without_inline_execution(monkeypatch) -> None:
    """调度路由创建统一 Execution 后必须立即交给持久 Signal，不能在扫描线程内长跑。"""

    calls: list[str] = []

    class _ScheduledAgent:
        """用真实 Store 创建最小动态实例，并禁止同步推进。"""

        def __init__(self, db: Session) -> None:
            """保存共享事务和统一 Execution Store。"""

            self.db = db
            self.store = SopExecutionStore(db)

        def start_task(self, **kwargs):
            """断言 schedule identity 后创建一个可由 worker 恢复的计划。"""

            calls.append("start")
            assert kwargs["source_kind"] == "schedule"
            plan = NormalizedPlan(
                goal=kwargs["goal"],
                success_criteria=(
                    SuccessCriterion(
                        id="criterion_01",
                        type="assertion",
                        spec={"description": "生成周报", "required": True},
                    ),
                ),
                steps=(PlanStep(step_key="answer", title="生成周报", kind="answer"),),
                budget={"max_steps": 2},
            )
            return self.store.start_dynamic_instance(
                tenant_id=kwargs["tenant_id"],
                session_id=kwargs["session_id"],
                agent_id=kwargs["agent_id"],
                initiator_user_id=kwargs["initiator_user_id"],
                plan=plan,
                capability_snapshot={
                    "model": {"model_config_id": "model_schedule", "checksum": "model"}
                },
                source_kind=kwargs["source_kind"],
                source_ref=kwargs["source_ref"],
            )[0], True

        def run_until_blocked_or_complete(self, **_kwargs):
            """若 AgentLoop 仍同步执行则立即让测试失败。"""

            raise AssertionError("scheduled dynamic task must run from durable signal")

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _ScheduledAgent)
    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        assert run.session_id is not None
        session = db.get(ChatSession, run.session_id)
        assert session is not None
        loop = object.__new__(AgentLoop)
        loop.db = db
        loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
        loop._knowledge_capability_payload = lambda *_args, **_kwargs: {"available": False}
        route = NonSopCapabilityRouteResult(
            selected_general_skill=None,
            general_selection=GeneralSkillSelection(),
            effective_decision=NonSopCapabilityDecision(
                mode="dynamic_task",
                goal="生成合同风险周报",
                success_criteria=["生成周报"],
                confidence=0.95,
            ),
            shadow_decision=None,
            shadow_attempted=False,
            shadow_duration_ms=0,
        )

        response = loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id=task.tenant_id,
                session_id=session.id,
                agent_id=task.agent_id,
                user_id=task.created_by_user_id,
                message=task.prompt,
                channel="scheduled_task",
                interaction_mode="scheduled_task",
            ),
            session,
            SimpleNamespace(),  # type: ignore[arg-type]
            route,
            "message_schedule",
        )

        signals = db.exec(select(ExecutionSignal)).all()
        executions = db.exec(select(SopInstance)).all()
        assert calls == ["start"]
        assert response is not None
        assert response.step_result is not None
        assert response.step_result.is_step_completed is False
        assert len(executions) == 1
        assert executions[0].source_kind == "schedule"
        assert executions[0].source_ref == run.id
        db.refresh(run)
        assert run.execution_id == executions[0].id
        assert len(signals) == 1
        assert signals[0].signal_type == "scheduled_start"
        assert signals[0].payload_json == {"scheduled_task_run_id": run.id}


def test_scheduled_entry_forces_durable_execution_when_model_routes_answer(monkeypatch) -> None:
    """调度入口不得因非 SOP 模型误判为普通回答而产生无 Execution 的假成功。"""

    captured: dict[str, object] = {}

    class _ScheduledAgent:
        """捕获调度委托参数并创建最小统一 Execution。"""

        def __init__(self, db: Session) -> None:
            """保存事务和统一执行存储。"""

            self.store = SopExecutionStore(db)

        def start_task(self, **kwargs):
            """记录服务端强制的目标与成功标准，再持久化动态实例。"""

            captured.update(kwargs)
            plan = NormalizedPlan(
                goal=kwargs["goal"],
                success_criteria=(
                    SuccessCriterion(
                        id="criterion_01",
                        type="assertion",
                        spec={"description": kwargs["success_criteria"][0], "required": True},
                    ),
                ),
                steps=(PlanStep(step_key="answer", title="生成结果", kind="answer"),),
                budget={"max_steps": 2},
            )
            return self.store.start_dynamic_instance(
                tenant_id=kwargs["tenant_id"],
                session_id=kwargs["session_id"],
                agent_id=kwargs["agent_id"],
                initiator_user_id=kwargs["initiator_user_id"],
                plan=plan,
                capability_snapshot={
                    "model": {"model_config_id": "model_schedule", "checksum": "model"}
                },
                source_kind=kwargs["source_kind"],
                source_ref=kwargs["source_ref"],
            )[0], True

    monkeypatch.setattr("app.core.agent_loop.DynamicTaskAgent", _ScheduledAgent)
    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        assert run.session_id is not None
        session = db.get(ChatSession, run.session_id)
        assert session is not None
        loop = object.__new__(AgentLoop)
        loop.db = db
        loop.events = SimpleNamespace(record=lambda *_args, **_kwargs: None)
        loop._knowledge_capability_payload = lambda *_args, **_kwargs: {"available": False}
        answer_route = NonSopCapabilityRouteResult(
            selected_general_skill=None,
            general_selection=GeneralSkillSelection(),
            effective_decision=NonSopCapabilityDecision(mode="answer", confidence=0.93),
            shadow_decision=NonSopCapabilityDecision(mode="answer", confidence=0.93),
            shadow_attempted=True,
            shadow_duration_ms=3,
        )

        response = loop._try_handle_dynamic_task(
            ChatTurnRequest(
                tenant_id=task.tenant_id,
                session_id=session.id,
                agent_id=task.agent_id,
                user_id=task.created_by_user_id,
                message=task.prompt,
                channel="scheduled_task",
                interaction_mode="scheduled_task",
            ),
            session,
            SimpleNamespace(),  # type: ignore[arg-type]
            answer_route,
            "message_schedule_answer_route",
        )

        assert response is not None
        assert captured["goal"] == task.prompt
        assert captured["source_kind"] == "schedule"
        assert captured["source_ref"] == run.id
        assert captured["success_criteria"] == (
            "完成调度目标并形成可审计的结果或明确等待事项",
        )
        db.refresh(run)
        assert run.execution_id is not None


def test_scheduled_start_signal_is_consumed_only_after_explicit_outcome(monkeypatch) -> None:
    """调度启动 signal 在推进返回 waiting 后才消费，进程退出前始终可重领。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        execution = _dynamic_execution(run)
        db.add(execution)
        db.flush()
        run.execution_id = execution.id
        db.add(run)
        signal = _start_signal(execution, run.id)
        db.add(signal)
        db.commit()
        agent = DynamicTaskAgent(db)
        monkeypatch.setattr(
            agent,
            "run_until_blocked_or_complete",
            lambda **_kwargs: DynamicRunOutcome(
                status="waiting",
                execution_id=execution.id,
                blocking_step_key="approval",
            ),
        )

        outcome = agent.resume_scheduled_start_signal(
            signal_id=signal.id,
            model_config=SimpleNamespace(),  # type: ignore[arg-type]
            worker_id="schedule-worker",
        )

        db.refresh(signal)
        assert outcome.status == "waiting"
        assert signal.status == "consumed"
        assert signal.consumed_at is not None


def test_scheduled_start_signal_closes_before_execution_terminal_state(monkeypatch) -> None:
    """验证成功终态前先消费启动 signal，避免 active_signals 永久阻断调度闭环。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        execution = _dynamic_execution(run)
        db.add(execution)
        db.flush()
        run.execution_id = execution.id
        db.add(run)
        signal = _start_signal(execution, run.id)
        db.add(signal)
        db.commit()
        agent = DynamicTaskAgent(db)

        def complete_with_signal(**kwargs) -> DynamicRunOutcome:
            """模拟结果冻结事务内先结算恢复 signal，再执行统一终态闭合检查。"""

            assert kwargs["resume_signal_id"] == signal.id
            assert kwargs["signal_worker_id"] == "schedule-terminal-worker"
            with agent.store.owned(execution, worker_id="schedule-terminal-worker"):
                control = ExecutionControlService(db, agent.store)
                control.freeze_result(
                    execution,
                    result={"markdown": "调度任务完成"},
                    verification={"passed": True},
                    application_publication_status="settled",
                )
                control.consume_signal(
                    execution,
                    signal,
                    worker_id="schedule-terminal-worker",
                )
                blockers = control.terminal_blockers(execution, "succeeded")
                assert "active_signals" not in blockers
            db.commit()
            return DynamicRunOutcome(status="waiting", execution_id=execution.id)

        monkeypatch.setattr(agent, "run_until_blocked_or_complete", complete_with_signal)

        outcome = agent.resume_scheduled_start_signal(
            signal_id=signal.id,
            model_config=SimpleNamespace(),  # type: ignore[arg-type]
            worker_id="schedule-terminal-worker",
        )

        db.refresh(signal)
        db.refresh(execution)
        assert outcome.status == "waiting"
        assert signal.status == "consumed"
        assert execution.status == "running"


def test_expired_scheduled_start_lease_is_reclaimed_after_worker_crash(monkeypatch) -> None:
    """worker 认领后崩溃时，租约到期的 scheduled_start 会重领且仍只推进原 Execution。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        execution = _dynamic_execution(run)
        signal = _start_signal(execution, run.id)
        run.execution_id = execution.id
        signal.status = "claimed"
        signal.lease_owner = "crashed-worker"
        signal.lease_expires_at = utc_now() - timedelta(seconds=1)
        signal.claimed_at = utc_now() - timedelta(minutes=5)
        signal.attempt_count = 1
        db.add(execution)
        db.add(signal)
        db.commit()
        agent = DynamicTaskAgent(db)
        monkeypatch.setattr(
            agent,
            "run_until_blocked_or_complete",
            lambda **_kwargs: DynamicRunOutcome(
                status="waiting",
                execution_id=execution.id,
                blocking_step_key="approval",
            ),
        )

        due = due_dynamic_task_signals(db)
        outcome = agent.resume_scheduled_start_signal(
            signal_id=due[0].id,
            model_config=SimpleNamespace(),  # type: ignore[arg-type]
            worker_id="replacement-worker",
        )

        db.refresh(signal)
        assert [item.id for item in due] == [signal.id]
        assert outcome.execution_id == execution.id
        assert signal.status == "consumed"
        assert signal.attempt_count == 2


def test_scheduled_start_reauthorizes_after_queue_and_fails_closed() -> None:
    """Signal 排队后成员被停用时，应消费唤醒并形成失败终态而非执行或无限重试。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        execution = _dynamic_execution(run)
        db.add(execution)
        db.flush()
        run.execution_id = execution.id
        signal = _start_signal(execution, run.id)
        user = db.get(User, task.created_by_user_id)
        assert user is not None
        user.membership_status = "suspended"
        db.add(run)
        db.add(signal)
        db.add(user)
        db.commit()

        outcome = DynamicTaskAgent(db).resume_scheduled_start_signal(
            signal_id=signal.id,
            model_config=SimpleNamespace(),  # type: ignore[arg-type]
            worker_id="schedule-worker",
        )

        db.refresh(signal)
        db.refresh(execution)
        assert outcome.status == "failed"
        assert execution.status == "failed"
        assert execution.terminal_reason_json == {"code": "DYNAMIC_SCHEDULE_ACCESS_DENIED"}
        assert signal.status == "consumed"


def test_reconcile_waiting_run_tracks_terminal_execution_once() -> None:
    """审批恢复后的动态终态必须幂等回写原 run，且不会重复增加 task.run_count。"""

    with _session() as db:
        task = _seed_task(db)
        run = scheduled_service._prepare_scheduled_task_run(
            db,
            task,
            utc_now() + timedelta(minutes=1),
            False,
        )
        execution = _dynamic_execution(run, status="succeeded")
        execution.completed_at = utc_now()
        db.add(execution)
        db.add(
            Message(
                tenant_id=run.tenant_id,
                session_id=run.session_id,
                role="assistant",
                content="最终合同风险周报",
            )
        )
        run.execution_id = execution.id
        run.status = "waiting"
        task.run_count = 1
        task.last_status = "waiting"
        db.add(run)
        db.add(task)
        db.commit()

        first = scheduled_service.reconcile_scheduled_dynamic_runs(db)
        second = scheduled_service.reconcile_scheduled_dynamic_runs(db)

        db.refresh(run)
        db.refresh(task)
        assert first == 1
        assert second == 0
        assert run.status == "succeeded"
        assert run.finished_at == execution.completed_at
        assert run.result_summary == "最终合同风险周报"
        assert task.run_count == 1
        assert task.last_status == "succeeded"


def _seed_task(
    db: Session,
    *,
    concurrency_policy: str = "forbid",
    misfire_policy: str = "coalesce",
) -> ScheduledTask:
    """创建可由 owner 使用的 Agent、活动成员和每日调度任务。"""

    db.add(Tenant(id="tenant_schedule", name="调度租户"))
    user = User(
        id="user_schedule",
        tenant_id="tenant_schedule",
        username="schedule-owner",
        password_hash="x",
    )
    agent = AgentProfile(
        id="agent_schedule",
        tenant_id="tenant_schedule",
        name="合同员工",
        owner_user_id=user.id,
        status="active",
    )
    task = ScheduledTask(
        id="task_schedule",
        tenant_id="tenant_schedule",
        agent_id=agent.id,
        created_by_user_id=user.id,
        title="合同风险周报",
        prompt="生成合同风险周报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
        timezone="Asia/Shanghai",
        concurrency_policy=concurrency_policy,
        misfire_policy=misfire_policy,
        next_run_at=utc_now() + timedelta(minutes=1),
    )
    db.add(user)
    db.add(agent)
    db.add(task)
    db.commit()
    return task


def _dynamic_execution(
    run,
    *,
    status: str = "running",
) -> SopInstance:
    """构造满足统一动态身份约束且由当前 schedule run 发起的 Execution。"""

    terminal = status in {"succeeded", "failed", "cancelled", "timed_out"}
    return SopInstance(
        id=f"execution_{run.id}",
        tenant_id=run.tenant_id,
        session_id=run.session_id,
        kind="dynamic_task",
        active_slot_key=None if terminal else f"foreground:{run.session_id}",
        initiator_user_id=run.user_id,
        source_kind="schedule",
        source_ref=run.id,
        agent_id=run.agent_id,
        goal_snapshot_json={"goal": "生成合同风险周报", "success_criteria": []},
        current_plan_revision_id=f"plan_{run.id}",
        current_plan_checksum="plan-checksum",
        capability_snapshot_json={"model": {"model_config_id": "model_schedule"}},
        capability_checksum="capability-checksum",
        status=status,
    )


def _start_signal(execution: SopInstance, run_id: str) -> ExecutionSignal:
    """构造与 schedule source_ref 精确绑定的待消费启动信号。"""

    payload = {"scheduled_task_run_id": run_id}
    return ExecutionSignal(
        id=f"signal_{run_id}",
        tenant_id=execution.tenant_id,
        execution_id=execution.id,
        signal_type="scheduled_start",
        dedupe_key=f"dedupe-{run_id}",
        causation_type="scheduled_task_run",
        causation_id=run_id,
        payload_json=payload,
        payload_checksum=canonical_checksum(payload),
        available_at=utc_now() - timedelta(seconds=1),
    )


def _session() -> Session:
    """创建完整元数据的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)
