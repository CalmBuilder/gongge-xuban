"""
@Time       : 2026/09/05 19:00
@Author     : zhanglp8181
@File       : test_dynamic_chat_cancellation.py
@CallChain  : pytest → AgentLoop 聊天流 → DynamicTaskAgent 串行/探索/并行 → 取消消息与事件
@Description: 使用隔离 SQLite、确定性提案和无网络执行器验证真实 Runtime 取消贯通聊天协议。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.cancellation import TurnCancellationRequested, raise_if_cancelled
from app.config import get_settings
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    Message,
    ModelConfig,
    SopInstance,
    SopOperation,
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
from app.session.session_schema import ChatTurnRequest
from app.sop_runtime.execution_store import SopExecutionStore
from app.tools.tool_schema import ToolCall, ToolResult


class _ReadProposer:
    """只替代外部模型；正常经过 Runtime 提案持久化、冻结能力和调用预算。"""

    def propose(self, *, step: PlanStep, **_kwargs) -> CompletedProviderProposal:
        """为当前已声明的纯读或探索步骤返回完整且固定身份的合法提案。"""

        assert step.kind in {"tool.read", "explore"}, "取消后不应进入最终报告提案"
        return CompletedProviderProposal(
            response_id=f"cancel-proposal-{step.step_key}",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=step.capability_refs[0],
                arguments={},
                rationale="读取公开证据以核验取消边界",
            ),
        )


@pytest.mark.parametrize("path", ["serial", "explore", "parallel"])
@pytest.mark.parametrize("late_success", [False, True], ids=["cooperative", "late-result"])
def test_dynamic_reads_cancel_through_chat_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    late_success: bool,
) -> None:
    """真实串行、探索和并行读取取消后只落取消账本，迟到成功不得变成回复或失败。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'dynamic-chat-cancel.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(
        get_settings(), "dynamic_task_max_parallel_reads", 2 if path == "parallel" else 1
    )
    stopped = Event()
    barrier = Barrier(2) if path == "parallel" else None
    calls: list[tuple[str, int]] = []
    calls_lock = Lock()
    observed_interruptions: list[TurnCancellationRequested] = []
    created: list[SopInstance] = []

    class CancelReadExecutor:
        """用有界屏障触发真实并发，不发送网络请求、不读取站点凭据。"""

        def __init__(self, db: Session) -> None:
            """保留当前 worker 的 Session 身份以证明并行没有共享 Session。"""

            self.db = db

        def execute(
            self,
            _tenant: str,
            call: ToolCall,
            *,
            is_cancelled: Callable[[], bool],
            **_kwargs,
        ) -> ToolResult:
            """在派发后取消，或故意返回迟到成功以验证 Runtime 的结果阶段取消守卫。"""

            assert callable(is_cancelled)
            with calls_lock:
                calls.append((call.name, id(self.db)))
            if barrier is not None:
                barrier.wait(timeout=5)
            stopped.set()
            if not late_success:
                try:
                    raise_if_cancelled(is_cancelled)
                except TurnCancellationRequested as exc:
                    with calls_lock:
                        observed_interruptions.append(exc)
                    raise
            return ToolResult(tool_name=call.name, success=True, data={"text": "迟到结果不得发布"})

    try:
        with Session(engine, expire_on_commit=False) as db:
            tenant = Tenant(id="cancel_tenant", name="取消隔离测试")
            user = User(
                id="cancel_user",
                tenant_id=tenant.id,
                username="cancel_fixture",
                password_hash="fixture",
            )
            profile = AgentProfile(
                id="cancel_agent", tenant_id=tenant.id, name="研究专家", owner_user_id=user.id
            )
            chat = ChatSession(
                id="cancel_chat", tenant_id=tenant.id, user_id=user.id, agent_id=profile.id
            )
            facts = {
                "protocol_version": "dynamic-v1",
                "sdk_available": True,
                "credentials_verified": True,
                "structured_output": True,
                "tool_calling": True,
            }
            model = ModelConfig(
                id="cancel_model",
                tenant_id=tenant.id,
                name="无网络测试模型",
                api_key_encrypted="fixture-not-a-credential",
                model="fixture",
                base_url="https://example.invalid",
                capability_snapshot_json=facts,
                capability_checksum=capability_checksum(facts),
                preflight_status="ready",
            )
            db.add_all([tenant, user, profile, chat, model])
            for name in ("fixture.read_a", "fixture.read_b"):
                tool = Tool(
                    tenant_id=tenant.id, name=name, method="GET", url="https://example.invalid"
                )
                publish_tool_contract(
                    tool,
                    ToolReliabilityContract(
                        risk_class="read",
                        side_effect="none",
                        confirmation_policy="none",
                        timeout_policy="failed",
                        dynamic_task_enabled=True,
                        explore_safe=True,
                        parallel_safe=True,
                        concurrency_key=name,
                        max_in_flight=2,
                    ),
                )
                db.add(tool)
                db.flush()
                ensure_private_resource_binding(db, tenant.id, profile.id, "tool", tool.id)
            db.commit()
            snapshots = DynamicCapabilityCatalog(db).list_tools(tenant.id, profile.id)
            reads = [
                PlanStep(
                    step_key="read_a",
                    title="读取原文 A",
                    kind="explore" if path == "explore" else "tool.read",
                    capability_refs=("fixture.read_a",),
                )
            ]
            if path == "parallel":
                reads.append(
                    PlanStep(
                        step_key="read_b",
                        title="读取原文 B",
                        kind="tool.read",
                        capability_refs=("fixture.read_b",),
                    )
                )
            plan = NormalizedPlan(
                goal="核验公开证据",
                success_criteria=(SuccessCriterion(id="evidence", type="assertion", spec={}),),
                steps=(
                    *reads,
                    PlanStep(
                        step_key="answer",
                        title="形成报告",
                        kind="answer",
                        depends_on=tuple(step.step_key for step in reads),
                    ),
                ),
                budget={"max_steps": 6, "max_tool_calls": 6, "max_model_calls": 6},
            )
            loop = AgentLoop(db)
            monkeypatch.setattr(loop, "_get_or_create_session", lambda _request: chat)
            monkeypatch.setattr(loop, "_get_request_model", lambda *_args: model)
            monkeypatch.setattr(loop, "_list_published_skills", lambda *_args: [])
            monkeypatch.setattr(loop, "_list_enabled_tools", lambda *_args: [])
            monkeypatch.setattr(loop, "_tools_with_general_skills", lambda *_args: [])
            monkeypatch.setattr(loop, "_get_persona_prompt", lambda *_args: None)
            monkeypatch.setattr(loop, "_conversation_context", lambda *_args, **_kwargs: {})
            monkeypatch.setattr(
                loop, "_decide_non_sop_capability", lambda *_args, **_kwargs: SimpleNamespace()
            )
            monkeypatch.setattr(loop.memory, "context_memories", lambda *_args, **_kwargs: [])
            monkeypatch.setattr(loop, "_pace_stream", lambda: None)

            def run_dynamic(request, session, selected_model, _route, message_id, **_kwargs):
                """只跳过模型路由与规划，实际执行、聊天响应和取消落账保持生产路径。"""

                runtime = DynamicTaskAgent(
                    db,
                    action_proposer=_ReadProposer(),
                    explore_proposer=_ReadProposer(),
                    explore_enabled=True,
                    is_cancelled=stopped.is_set,
                    tool_executor=CancelReadExecutor(db),
                    parallel_tool_executor_factory=CancelReadExecutor,
                )
                instance, _ = SopExecutionStore(db).start_dynamic_instance(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id=profile.id,
                    initiator_user_id=user.id,
                    source_kind="chat",
                    source_ref=message_id,
                    plan=plan,
                    capability_snapshot={
                        "tools": [item.model_dump(mode="json") for item in snapshots],
                        "model": {
                            "model_config_id": model.id,
                            "checksum": model.capability_checksum,
                        },
                    },
                )
                db.commit()
                created.append(instance)
                try:
                    outcome = loop._run_dynamic_execution(
                        dynamic_agent=runtime,
                        instance=instance,
                        model_config=selected_model,
                        request=request,
                        chat_session=session,
                        user_message_id=message_id,
                    )
                except TurnCancellationRequested as exc:
                    if path == "explore" and not late_success:
                        assert exc is observed_interruptions[0]
                    raise
                return loop._dynamic_task_response(
                    chat_session=session,
                    outcome=outcome,
                    user_message_id=message_id,
                )

            monkeypatch.setattr(loop, "_try_handle_dynamic_task", run_dynamic)
            stream = loop.handle_turn_stream(
                ChatTurnRequest(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    agent_id=profile.id,
                    session_id=chat.id,
                    client_turn_id=f"cancel-{path}",
                    message="核验公开证据",
                    execution_engine="dynamic_task",
                )
            )
            stream_events = []
            try:
                for event in stream:
                    stream_events.append(event)
                    if event["event"] == "error_occurred":
                        break  # 负例在错误事实落账后停止，不进入无关失败回复生成。
            finally:
                stream.close()
            assert created, "测试必须真正进入 DynamicTaskAgent"
            db.refresh(created[0])
            operations = db.exec(
                select(SopOperation).where(SopOperation.instance_id == created[0].id)
            ).all()
            assert len(operations) == (2 if path == "parallel" else 1)
            assert created[0].status == "cancelled"
            assert all(operation.status == "cancelled" for operation in operations)
            events = db.exec(select(AgentEvent).where(AgentEvent.session_id == chat.id)).all()
            event_types = [event.event_type for event in events]
            assert "dynamic_task_execution_failed" not in event_types
            assert "error_occurred" not in event_types
            assert event_types.count("stream_cancelled") == 1
            assistant = db.exec(
                select(Message).where(
                    Message.session_id == chat.id,
                    Message.role == "assistant",
                )
            ).one()
            assert assistant.metadata_json["status"] == "cancelled"
            assert assistant.metadata_json["terminal_reason"] == "user_cancelled"
            assert "迟到结果" not in assistant.content
            assert not any(
                event["event"] in {"complete", "error_occurred"} for event in stream_events
            )
            assert loop._active_cancellation_token is None
            if path == "parallel":
                assert len({session_id for _name, session_id in calls}) == 2
                assert db.exec(select(DynamicReadDispatchBatch)).one().status == "cancelled"
                with Session(engine) as audit_db:
                    statuses = audit_db.exec(select(DynamicReadDispatchItem.status)).all()
                assert statuses == ["discarded", "discarded"]
    finally:
        engine.dispose()
