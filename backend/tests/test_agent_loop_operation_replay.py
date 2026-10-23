"""
@Time       : 2026/09/24
@Author     : zhanglp8181
@File       : test_agent_loop_operation_replay.py
@CallChain  : pytest → AgentLoop → 兼容动作账本/ToolExecutor → SQLite
@Description: 验证兼容入口的持久动作回执不会被无关事件淘汰或撤权事件遮蔽。
"""

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.core.agent_loop import AgentLoop
from app.db.models import AgentEvent, AgentProfile, AgentResourceBinding, ChatSession, Tool
from app.session.session_schema import ChatTurnRequest
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


class CountingExecutor(ToolExecutor):
    """保留生产授权，只在适配器边界统计派发并返回合成结果。"""

    def __init__(self, db: Session, outcome: str) -> None:
        """记录测试期望的首次结果，不连接真实外部服务。"""

        super().__init__(db)
        self.outcome = outcome
        self.dispatches = 0

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
        **kwargs: object,
    ) -> ToolResult:
        """先执行生产授权，再在真实工具适配器边界计数一次派发。"""

        denied = self.authorize(
            tenant_id,
            tool_call,
            active_skill_id,
            agent_id,
            actor_user_id,
            **kwargs,
        )
        if denied is not None:
            return denied
        self.dispatches += 1
        if self.outcome == "unknown":
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                error=ToolError(code="TIMEOUT", message="合成超时"),
            )
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"ticket_id": f"synthetic-{self.dispatches}"},
        )


def _case(
    outcome: str,
    *,
    with_agent_binding: bool = False,
) -> tuple[Session, AgentLoop, CountingExecutor, ChatTurnRequest, ChatSession, ToolCall]:
    """创建真实 SQLite 兼容入口，身份由生产 ChatTurnRequest 派生。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    tool = Tool(
        id="tool_replay_review",
        tenant_id="review-tenant",
        name="review.create_ticket",
        display_name="测试工单",
        method="POST",
        url="https://example.invalid/ticket",
        enabled=True,
    )
    session = ChatSession(
        id="review-session",
        tenant_id="review-tenant",
        agent_id="review-agent" if with_agent_binding else None,
    )
    db.add(tool)
    db.add(session)
    if with_agent_binding:
        db.add(
            AgentProfile(
                id="review-agent",
                tenant_id="review-tenant",
                name="审核员工",
            )
        )
    db.commit()
    if with_agent_binding:
        ensure_private_resource_binding(
            db,
            "review-tenant",
            "review-agent",
            "tool",
            tool.id,
            "active",
        )
        db.commit()
    loop = AgentLoop(db)
    executor = CountingExecutor(db, outcome)
    loop.tool_executor = executor
    request = ChatTurnRequest(
        tenant_id="review-tenant",
        session_id=session.id,
        client_turn_id="persisted-turn-1",
        message="创建测试工单",
    )
    tool_call = ToolCall(name=tool.name, arguments={"subject": "synthetic"})
    return db, loop, executor, request, session, tool_call


@pytest.mark.parametrize("outcome", ["succeeded", "unknown"])
def test_compatibility_receipt_survives_more_than_200_unrelated_events(outcome: str) -> None:
    """同一动作的成功或 unknown 回执不因 201 条无关事件而再次派发。"""

    db, loop, executor, request, session, tool_call = _case(outcome)
    try:
        first = loop._execute_tool_call(request, session, tool_call, tool_call_id="call-first")
        assert executor.dispatches == 1
        assert first.success is (outcome == "succeeded")
        for number in range(201):
            filler = AgentEvent(
                tenant_id=request.tenant_id,
                session_id=session.id,
                event_type="tool_call_finished",
                payload_json={
                    "tool_name": "review.other_tool",
                    "persistent_action_id": f"other-{number}",
                    "success": True,
                },
            )
            filler.created_at += timedelta(seconds=1 + number)
            db.add(filler)
        db.commit()

        loop._execute_tool_call(request, session, tool_call, tool_call_id="call-retry")

        assert executor.dispatches == 1
    finally:
        db.close()


@pytest.mark.parametrize("outcome", ["succeeded", "unknown"])
def test_authorization_rejection_does_not_hide_receipt_after_rebind(outcome: str) -> None:
    """撤权拒绝不能覆盖原成功或 unknown，恢复绑定后仍不得再次派发。"""

    db, loop, executor, request, session, tool_call = _case(
        outcome,
        with_agent_binding=True,
    )
    try:
        first = loop._execute_tool_call(request, session, tool_call, tool_call_id="call-first")
        assert executor.dispatches == 1
        assert first.success is (outcome == "succeeded")
        conflict = loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool_call.name, arguments={"subject": "changed"}),
            tool_call_id="call-conflict",
        )
        assert conflict.success is False
        assert conflict.error is not None and conflict.error.code == "SOP_EXECUTION_CONFLICT"
        assert executor.dispatches == 1
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == request.tenant_id,
                AgentResourceBinding.agent_id == session.agent_id,
                AgentResourceBinding.resource_id == "tool_replay_review",
            )
        ).one()
        binding.status = "deleted"
        db.add(binding)
        db.commit()

        denied = loop._execute_tool_call(request, session, tool_call, tool_call_id="call-denied")
        assert denied.success is False
        assert denied.error is not None and denied.error.code == "NOT_ALLOWED"
        assert executor.dispatches == 1

        binding.status = "active"
        db.add(binding)
        db.commit()
        replay = loop._execute_tool_call(request, session, tool_call, tool_call_id="call-rebind")

        assert executor.dispatches == 1
        assert replay.success is (outcome == "succeeded")
        if outcome == "succeeded":
            assert replay.data["receipt_source"] == "compatibility_event"
        else:
            assert replay.error is not None
            assert replay.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
    finally:
        db.close()
