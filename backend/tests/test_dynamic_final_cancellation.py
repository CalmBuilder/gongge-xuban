"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_dynamic_final_cancellation.py
@CallChain  : pytest → 真实 DynamicTaskAgent → 模型返回/发布边界 → 正式 Store 取消协议
@Description: 在隔离 SQLite/MySQL 验证模型边界取消阻断外呼与报告，且普通冲突不得被吞。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.cancellation import TurnCancellationRequested
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    ExecutionPublication,
    ExecutionResult,
    Message,
    ModelConfig,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    Tenant,
    User,
)
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore
from app.tools.tool_schema import ToolCall, ToolResult
from app.vertical_abilities.resolver import VerticalAbilityResolver

SOURCE_URL = "https://example.org/final-cancellation-source"
REPORT = (
    "## 结论摘要\n公开原文已读取。\n"
    "## 关键事实\n原文说明取消后不发布迟到报告。\n"
    f"## 来源与证据\n{SOURCE_URL}\n"
    "Provider：fixture；retrieved_at=2026-09-05T00:00:00Z。\n"
    "## 冲突/缺口\n仅有一个原文，未独立交叉验证。\n"
    "## 后续建议\n补充第二个独立来源后再发布正式结论。"
)


class _BoundaryProposer:
    """只替代模型 I/O，在读动作或最终提案返回边界注入确定性取消或冲突。"""

    def __init__(self) -> None:
        """分别保留读模型与回答模型钩子，默认不打断以验证正常发布对照。"""

        self.after_read: Callable[[], None] | None = None
        self.after_answer: Callable[[], None] | None = None
        self.steps: list[str] = []

    def propose(self, *, step: PlanStep, **_kwargs) -> CompletedProviderProposal:
        """先构造完整读动作或回答，再触发对应钩子；不替换 Runtime 的生产方法。"""

        self.steps.append(step.step_key)
        proposal = (
            RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref="intelligence.web_read",
                arguments={"url": SOURCE_URL},
                rationale="读取公开原文",
            )
            if step.kind == "tool.read"
            else RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": REPORT,
                    "criterion_evidence": {"evidence": ["read_source"]},
                    "pending_questions": [],
                },
                rationale="依据已落账的读取回执回答",
            )
        )
        completed = CompletedProviderProposal(
            response_id=f"final-boundary-{len(self.steps)}",
            finish_reason="stop",
            proposal=proposal,
        )
        if step.kind == "tool.read" and self.after_read is not None:
            self.after_read()
        if step.kind == "answer" and self.after_answer is not None:
            self.after_answer()
        return completed


class _SourceExecutor:
    """只返回本地公开原文样本，保留真实 Operation、证据验证和调用预算。"""

    def __init__(self) -> None:
        """独立记录适配器调用，避免用数据库回滚后的空表假证没有外呼。"""

        self.calls: list[ToolCall] = []

    def execute(self, _tenant: str, call: ToolCall, **_kwargs) -> ToolResult:
        """记录调用后提供非空正文，不访问网络或真实凭据。"""

        self.calls.append(call)
        assert call.name == "intelligence.web_read"
        assert call.arguments == {"url": SOURCE_URL}
        return ToolResult(
            tool_name=call.name,
            success=True,
            data={
                "url": SOURCE_URL,
                "content": "公开原文说明取消后不发布迟到报告。",
                "provider": "fixture",
                "retrieved_at": "2026-09-05T00:00:00Z",
            },
        )


@dataclass
class _FinalRun:
    """保存当前隔离执行及可控模型边界，不共享 Session 给取消事务。"""

    engine: Engine
    db: Session
    instance: SopInstance
    model: ModelConfig
    agent: DynamicTaskAgent
    proposer: _BoundaryProposer
    executor: _SourceExecutor
    stopped: Event

    def run(self) -> DynamicRunOutcome:
        """执行正式两步计划，让 Runtime 自己验证、落账并发布最终报告。"""

        return self.agent.run_until_blocked_or_complete(
            execution_id=self.instance.id,
            model_config=self.model,
            worker_id="final-report-worker",
            actor_user_id=self.instance.initiator_user_id,
        )

    def cancel_independently(self) -> None:
        """使用新连接和正式取消抢占租约，在模型返回时提交权威取消状态。"""

        with Session(self.engine, expire_on_commit=False) as cancel_db:
            assert cancel_db is not self.db
            cancelled = cancel_db.get(SopInstance, self.instance.id)
            assert cancelled is not None and cancelled.status == "running"
            assert cancelled.lease_owner == "final-report-worker"
            store = SopExecutionStore(cancel_db)
            with store.owned_for_cancellation(cancelled, worker_id="authenticated-cancel"):
                assert store.request_cancellation(
                    cancelled,
                    actor_user_id=cancelled.initiator_user_id,
                    reason="最终报告模型返回时取消。",
                )
            cancel_db.commit()
            assert cancelled.status == "cancelled"


@pytest.fixture
def final_run(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FinalRun]:
    """用正式 Store 构造执行；独立连接仅访问临时 SQLite 或已迁移至 head 的随机 MySQL。"""

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
            f"sqlite:///{tmp_path / 'final-cancellation.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
            poolclass=NullPool,
        )
        SQLModel.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            facts = {
                "protocol_version": "dynamic-v1",
                "sdk_available": True,
                "credentials_verified": True,
                "structured_output": True,
                "tool_calling": True,
            }
            model = ModelConfig(
                id="final_model",
                tenant_id="final_tenant",
                name="取消边界测试模型",
                api_key_encrypted="fixture-not-a-credential",
                model="fixture",
                base_url="https://example.invalid",
                capability_snapshot_json=facts,
                capability_checksum=capability_checksum(facts),
                preflight_status="ready",
            )
            db.add_all([
                Tenant(id="final_tenant", name="取消边界隔离租户"),
                User(
                    id="final_user", tenant_id="final_tenant",
                    username="final_fixture", password_hash="fixture",
                ),
                AgentProfile(
                    id="final_agent", tenant_id="final_tenant",
                    name="公开原文研究专家", owner_user_id="final_user",
                ),
                ChatSession(
                    id="final_session", tenant_id="final_tenant",
                    agent_id="final_agent", user_id="final_user",
                ),
                model,
            ])
            db.commit()
            resolution = VerticalAbilityResolver().resolve(
                ["web-intelligence"], tenant_id="final_tenant", agent_id="final_agent"
            )
            snapshot = next(
                item for item in resolution.capabilities if item.name == "intelligence.web_read"
            )
            plan = NormalizedPlan(
                goal="读取公开原文并生成有来源的报告",
                success_criteria=(SuccessCriterion(id="evidence", type="assertion", spec={}),),
                steps=(
                    PlanStep(
                        step_key="read_source", title="读取原文", kind="tool.read",
                        capability_refs=(snapshot.name,),
                    ),
                    PlanStep(
                        step_key="answer", title="形成报告", kind="answer",
                        depends_on=("read_source",),
                    ),
                ),
                budget={"max_steps": 2, "max_tool_calls": 4, "max_model_calls": 4},
            )
            instance, _ = SopExecutionStore(db).start_dynamic_instance(
                tenant_id="final_tenant", session_id="final_session", agent_id="final_agent",
                initiator_user_id="final_user", plan=plan,
                capability_snapshot={
                    **resolution.snapshot,
                    "tools": [snapshot.model_dump(mode="json")],
                    "model": {
                        "model_config_id": model.id,
                        "checksum": model.capability_checksum,
                        "capabilities": facts,
                    },
                },
                context={"vertical_ability_ids": ["web-intelligence"]},
            )
            db.commit()
            stopped = Event()
            proposer = _BoundaryProposer()
            executor = _SourceExecutor()
            agent = DynamicTaskAgent(
                db, action_proposer=proposer, tool_executor=executor,
                is_cancelled=stopped.is_set,
            )

            def verified_model(*_args) -> ModelConfig:
                """仅替代外部模型预检；冻结模型校验及生产发布门禁保持启用。"""

                return model

            monkeypatch.setattr(agent.catalog, "require_dynamic_model", verified_model)
            yield _FinalRun(engine, db, instance, model, agent, proposer, executor, stopped)
    finally:
        engine.dispose()


def _assert_cancelled_without_report(run: _FinalRun, *, read_dispatched: bool = True) -> None:
    """核验取消留账及报告回滚，并按取消边界核对真实适配器调用和 Operation。"""

    with Session(run.engine) as observer:
        instance = observer.get(SopInstance, run.instance.id)
        assert instance is not None and instance.status == "cancelled"
        assert instance.cancellation_requested_at is not None
        assert instance.active_slot_key is None
        assert not observer.exec(select(Message).where(
            Message.session_id == instance.session_id, Message.role == "assistant",
        )).all()
        events = observer.exec(select(AgentEvent).where(
            AgentEvent.session_id == instance.session_id,
        )).all()
        assert not {event.event_type for event in events} & {
            "assistant_message_created", "execution_succeeded", "execution_failed",
            "dynamic_task_execution_failed",
        }
        # 正式取消自身必须保留统一终态结果与应用内投影，不可误当成模型报告。
        results = observer.exec(select(ExecutionResult).where(
            ExecutionResult.execution_id == instance.id,
        )).all()
        assert len(results) == 1
        assert results[0].result_json.get("status") == "cancelled"
        assert "markdown" not in results[0].result_json
        assert results[0].verification_json == {"passed": True, "source": "runtime_cancellation"}
        assert results[0].created_by_step_key is None
        publications = observer.exec(select(ExecutionPublication).where(
            ExecutionPublication.execution_id == instance.id,
        )).all()
        assert len(publications) == 1
        assert publications[0].result_id == results[0].id
        assert publications[0].receipt_json.get("projection") == "execution_result"
        assert not publications[0].receipt_json.get("message_id")
        operations = observer.exec(select(SopOperation).where(
            SopOperation.instance_id == instance.id,
        )).all()
        if read_dispatched:
            assert len(operations) == 1 and operations[0].status == "succeeded"
        else:
            assert not operations, "读模型返回边界取消后不得准备或执行工具操作"
        answer_nodes = observer.exec(select(SopNodeExecution).where(
            SopNodeExecution.instance_id == instance.id, SopNodeExecution.step_key == "answer",
        )).all()
        assert not answer_nodes, "取消事务必须回滚最终报告步骤及其产物"
    assert len(run.executor.calls) == (1 if read_dispatched else 0)
    assert run.proposer.steps == (["read_source", "answer"] if read_dispatched else ["read_source"])


@pytest.mark.parametrize("cancel_source", ["callback", "independent-store"])
@pytest.mark.parametrize(
    "final_run", ["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)], indirect=True,
)
def test_read_model_return_cancellation_never_dispatches(
    final_run: _FinalRun, cancel_source: str,
) -> None:
    """读模型完成时取消仍须穿过外层旧租约传播取消，不能外呼、修复模型或包装失败。"""

    final_run.proposer.after_read = (
        final_run.stopped.set if cancel_source == "callback" else final_run.cancel_independently
    )
    with pytest.raises(TurnCancellationRequested):
        final_run.run()
    _assert_cancelled_without_report(final_run, read_dispatched=False)


@pytest.mark.parametrize("during_repair", [False, True], ids=["initial", "repair"])
def test_read_model_conflict_is_not_cancelled_or_repaired(
    final_run: _FinalRun, during_repair: bool,
) -> None:
    """首次读模型或既有有界修复轮发生普通冲突时保留原异常，不能误认取消或额外重试。"""

    conflict = SopExecutionConflictError("读动作模型返回时的普通冲突，无取消事实。")

    def raise_at_read_boundary() -> None:
        """需要覆盖修复路径时只先抛一次模型校验错误，再抛同一普通冲突。"""

        if during_repair and len(final_run.proposer.steps) == 1:
            raise ValueError("用于进入一次既有模型修复的测试校验错误。")
        raise conflict

    final_run.proposer.after_read = raise_at_read_boundary
    with pytest.raises(SopExecutionConflictError) as caught:
        final_run.run()
    assert caught.value is conflict
    assert final_run.proposer.steps == ["read_source"] * (2 if during_repair else 1)
    assert not final_run.executor.calls
    with Session(final_run.engine) as observer:
        instance = observer.get(SopInstance, final_run.instance.id)
        assert instance is not None and instance.status == "running"
        assert instance.cancellation_requested_at is None
        assert not observer.exec(select(SopOperation)).all()
        assert not observer.exec(select(ExecutionResult)).all()
        assert not observer.exec(select(ExecutionPublication)).all()
        assert not observer.exec(select(Message).where(Message.role == "assistant")).all()
        event_types = set(observer.exec(select(AgentEvent.event_type)).all())
        assert not event_types & {"dynamic_task_execution_failed", "execution_failed"}


@pytest.mark.parametrize("cancel_source", ["callback", "independent-store"])
@pytest.mark.parametrize(
    "final_run", ["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)], indirect=True,
)
def test_answer_model_return_cancellation_never_publishes(
    final_run: _FinalRun, cancel_source: str,
) -> None:
    """模型已返回的报告在本地取消或跨事务正式取消后必须抛取消而非成功或失败。"""

    final_run.proposer.after_answer = (
        final_run.stopped.set if cancel_source == "callback" else final_run.cancel_independently
    )
    with pytest.raises(TurnCancellationRequested):
        final_run.run()
    _assert_cancelled_without_report(final_run)


def test_final_publication_transaction_cancellation_rolls_back_report(
    final_run: _FinalRun, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告已在发布事务内写入但尚未提交时取消，必须回滚全部报告产物。"""

    original = ExecutionControlService.settle_application_publication
    observed_messages: list[str] = []

    def settle_then_cancel(self, instance, publication, *, message_id):
        """执行真实发布投影写入后触发取消，证明末端守卫可回滚已 flush 的消息。"""

        result = original(self, instance, publication, message_id=message_id)
        assert self.db.get(Message, message_id) is not None
        observed_messages.append(message_id)
        final_run.stopped.set()
        return result

    monkeypatch.setattr(
        ExecutionControlService, "settle_application_publication", settle_then_cancel,
    )
    with pytest.raises(TurnCancellationRequested):
        final_run.run()
    assert len(observed_messages) == 1
    _assert_cancelled_without_report(final_run)


def test_ordinary_model_boundary_conflict_is_not_disguised_as_cancellation(
    final_run: _FinalRun,
) -> None:
    """同一模型边界没有权威取消事实的冲突必须保留原异常身份，不得误吞为取消。"""

    conflict = SopExecutionConflictError("测试中的普通租约冲突，无取消事实。")

    def raise_ordinary_conflict() -> None:
        """不写取消标志，只在模型完成边界抛出待保留的原冲突。"""

        raise conflict

    final_run.proposer.after_answer = raise_ordinary_conflict
    with pytest.raises(SopExecutionConflictError) as caught:
        final_run.run()
    assert caught.value is conflict
    with Session(final_run.engine) as observer:
        instance = observer.get(SopInstance, final_run.instance.id)
        assert instance is not None and instance.status == "running"
        assert instance.cancellation_requested_at is None
        assert not observer.exec(select(ExecutionResult)).all()
        assert not observer.exec(select(ExecutionPublication)).all()
        assert not observer.exec(select(Message).where(Message.role == "assistant")).all()


def test_same_report_without_cancellation_can_really_succeed(final_run: _FinalRun) -> None:
    """正对照证明测试报告满足实际发布门禁，取消负例并非依赖无效模型输出。"""

    outcome = final_run.run()
    assert outcome.status == "succeeded"
    assert outcome.message is not None and outcome.message.content.startswith(REPORT)
    assert final_run.proposer.steps == ["read_source", "answer"]
    with Session(final_run.engine) as observer:
        result = observer.exec(select(ExecutionResult)).one()
        publication = observer.exec(select(ExecutionPublication)).one()
        assert result.result_json["markdown"] == outcome.message.content
        assert publication.status == "settled"
        assert publication.receipt_json["message_id"] == outcome.message.id
