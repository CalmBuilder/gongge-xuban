"""
@Time       : 2026/09/25
@Author     : zhanglp8181
@File       : test_agent_loop_operation_replay_mysql.py
@CallChain  : pytest mysql → Alembic head → AgentLoop兼容回执查询 → MySQL账本
@Description: 验证持久动作身份查询、冲突、撤权恢复在隔离 MySQL 上与 SQLite 一致。
"""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, create_engine, select

from app.api import chat as chat_api
from app.agents.branching import ensure_private_resource_binding
from app.core.agent_loop import AgentLoop
from app.core.router import Router
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ModelConfig,
    Skill,
    Tool,
    User,
)
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把本轮隔离数据库迁移到当前 Alembic head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


class _MySQLCountingExecutor(ToolExecutor):
    """保留生产授权边界，只在本地适配器入口统计派发次数。"""

    def __init__(self, db: Session) -> None:
        """保存数据库依赖并初始化隔离测试计数器。"""

        super().__init__(db)
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
        """授权通过后只增加一次本地派发计数，不访问外部服务。"""

        authorization_error = self.authorize(
            tenant_id,
            tool_call,
            active_skill_id,
            agent_id,
            actor_user_id,
            **kwargs,
        )
        if authorization_error is not None:
            return authorization_error
        self.dispatches += 1
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"ticket_id": f"mysql-synthetic-{self.dispatches}"},
        )


def test_mysql_persistent_action_identity_replay_conflict_and_rebind(
    mysql_database_url: str,
) -> None:
    """MySQL 上同动作只派发一次，冲突拒绝，撤权恢复仍复用原回执。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd1_mysql_identity"
        session_id = "session_sd1_mysql_identity"
        agent_id = "agent_sd1_mysql_identity"
        tool_id = "tool_sd1_mysql_identity"
        tool_name = "mysql.review_ticket"
        tool_call = ToolCall(name=tool_name, arguments={"subject": "synthetic"})
        tool = Tool(
            id=tool_id,
            tenant_id=tenant_id,
            name=tool_name,
            display_name="MySQL 隔离工单",
            method="POST",
            url="https://example.invalid/ticket",
            enabled=True,
        )
        session = ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        db.add_all(
            [
                tool,
                session,
                AgentProfile(id=agent_id, tenant_id=tenant_id, name="MySQL 隔离员工"),
            ]
        )
        db.commit()
        ensure_private_resource_binding(
            db,
            tenant_id,
            agent_id,
            "tool",
            tool_id,
            "active",
        )
        db.commit()

        loop = AgentLoop(db)
        executor = _MySQLCountingExecutor(db)
        loop.tool_executor = executor
        request = ChatTurnRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            client_turn_id="sd1-mysql-turn",
            message="创建隔离测试工单",
        )
        action_zero = loop._trusted_tool_action_id(
            request,
            tool_call,
            None,
            action_scope="skill:mysql_review:step:node_a",
            action_ordinal=0,
        )
        action_one = loop._trusted_tool_action_id(
            request,
            tool_call,
            None,
            action_scope="skill:mysql_review:step:node_b",
            action_ordinal=0,
        )
        assert action_zero and action_one and action_zero != action_one

        first = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-temporary-first",
            persistent_action_id=action_zero,
        )
        assert first.success is True
        assert executor.dispatches == 1

        for number in range(201):
            filler = AgentEvent(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="tool_call_finished",
                payload_json={
                    "tool_name": "mysql.unrelated",
                    "persistent_action_id": f"mysql-unrelated-{number}",
                    "success": True,
                },
            )
            filler.created_at += timedelta(seconds=number + 1)
            db.add(filler)
        db.commit()

        replay = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-temporary-retry",
            persistent_action_id=action_zero,
        )
        assert replay.success is True
        assert replay.data["receipt_source"] == "compatibility_event"
        assert executor.dispatches == 1

        new_action = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-temporary-new-action",
            persistent_action_id=action_one,
        )
        assert new_action.success is True
        assert executor.dispatches == 2

        changed = loop._execute_tool_call(
            request,
            session,
            ToolCall(name=tool_name, arguments={"subject": "changed"}),
            tool_call_id="mysql-temporary-conflict",
            persistent_action_id=action_zero,
        )
        assert changed.success is False
        assert changed.error is not None
        assert changed.error.code == "SOP_EXECUTION_CONFLICT"
        assert executor.dispatches == 2

        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_id == tool_id,
            )
        ).one()
        binding.status = "deleted"
        db.add(binding)
        db.commit()

        denied = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-temporary-denied",
            persistent_action_id=action_zero,
        )
        assert denied.success is False
        assert denied.error is not None and denied.error.code == "NOT_ALLOWED"
        assert executor.dispatches == 2

        binding.status = "active"
        db.add(binding)
        db.commit()
        rebound = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-temporary-rebind",
            persistent_action_id=action_zero,
        )
        assert rebound.success is True
        assert rebound.data["receipt_source"] == "compatibility_event"
        assert executor.dispatches == 2

        unknown_action_id = loop._trusted_tool_action_id(
            request,
            tool_call,
            None,
            action_scope="skill:mysql_review:step:node_unknown",
            action_ordinal=0,
        )
        assert unknown_action_id
        db.add(
            AgentEvent(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="tool_call_started",
                payload_json={
                    "tool_name": tool_name,
                    "tool_call": tool_call.model_dump(mode="json"),
                    "persistent_action_id": unknown_action_id,
                    "request_fingerprint": AgentLoop._tool_request_fingerprint(tool_call),
                    "tool_call_id": "mysql-unknown-temporary",
                },
            )
        )
        db.commit()
        unknown = loop._execute_tool_call(
            request,
            session,
            tool_call,
            tool_call_id="mysql-unknown-retry",
            persistent_action_id=unknown_action_id,
        )
        assert unknown.success is False
        assert unknown.error is not None
        assert unknown.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
        assert executor.dispatches == 2
    finally:
        db.close()
        engine.dispose()


def test_mysql_real_agent_loop_replays_target_chat_turn_after_later_turn(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL 隔离账本按真实 AgentLoop 完成事件恢复 A，不借用后续 B 的回复。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd3_mysql_replay"
        user = User(
            id="user_sd3_mysql_replay",
            tenant_id=tenant_id,
            username="sd3-mysql-replay",
            password_hash="x",
        )
        agent = AgentProfile(
            id="agent_sd3_mysql_replay",
            tenant_id=tenant_id,
            name="MySQL 回放员工",
            is_overall=False,
            status="active",
        )
        session = ChatSession(
            id="session_sd3_mysql_replay",
            tenant_id=tenant_id,
            user_id=user.id,
            agent_id=agent.id,
        )
        skill = Skill(
            tenant_id=tenant_id,
            skill_id="sd3_mysql_replay_skill",
            version="1.0.0",
            name="MySQL 回放技能",
            content_json={
                "skill_id": "sd3_mysql_replay_skill",
                "name": "MySQL 回放技能",
                "nodes": [
                    {
                        "node_id": "reply",
                        "type": "response",
                        "name": "回复",
                        "instruction": "回复用户",
                        "expected_user_info": [],
                        "allowed_actions": ["answer_user"],
                    }
                ],
                "start_node_id": "reply",
                "terminal_node_ids": ["reply"],
                "edges": [],
            },
            status="published",
        )
        model_config = ModelConfig(
            id="model_sd3_mysql_replay",
            tenant_id=tenant_id,
            name="MySQL 回放测试模型",
            api_key_encrypted="",
            model="synthetic-real-agent-loop",
            enabled=True,
            is_default=True,
        )
        db.add_all([user, agent, session, skill, model_config])
        db.commit()

        def fake_router_decide(
            _router: Router,
            _message: str,
            _session: ChatSession,
            _skills: list[Skill],
            _model: ModelConfig,
            *_args: object,
            **_kwargs: object,
        ) -> RouterDecision:
            """只替换 MySQL 测试的路由模型边界，保留 AgentLoop 账本写入。"""

            return RouterDecision(
                decision="start_new_task",
                target_skill_id=skill.skill_id,
                target_step_id="reply",
                confidence=1.0,
            )

        def fake_step_agent(
            _loop: AgentLoop,
            request: ChatTurnRequest,
            *_args: object,
            **_kwargs: object,
        ) -> StepAgentResult:
            """生成区分 A/B 的步骤结果，不触发外部模型或业务服务。"""

            return StepAgentResult(action="reply", reply=f"步骤:{request.message}", is_step_completed=True)

        def fake_reply(
            _loop: AgentLoop,
            message: str,
            *_args: object,
            **_kwargs: object,
        ) -> str:
            """生成可核对来源的 MySQL 产品回复。"""

            return f"产品回复:{message}"

        monkeypatch.setattr(Router, "decide", fake_router_decide)
        monkeypatch.setattr(AgentLoop, "_get_request_model", lambda *_args, **_kwargs: model_config)
        monkeypatch.setattr(AgentLoop, "_list_published_skills", lambda *_args: [skill])
        monkeypatch.setattr(AgentLoop, "_list_enabled_tools", lambda *_args: [])
        monkeypatch.setattr(AgentLoop, "_tools_with_general_skills", lambda *_args: [])
        monkeypatch.setattr(AgentLoop, "_run_step_agent_with_context_repair", fake_step_agent)
        monkeypatch.setattr(AgentLoop, "_generate_reply_segment", fake_reply)
        monkeypatch.setattr(
            AgentLoop,
            "_try_handle_general_skill_after_scene_router",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(chat_api, "_ensure_request_tenant", lambda *_args: None)
        monkeypatch.setattr(chat_api, "ensure_tenant", lambda *_args: None)
        monkeypatch.setattr(chat_api, "_ensure_chat_session_available", lambda *_args: session)
        monkeypatch.setattr(chat_api, "_bind_request_to_session_agent", lambda *_args: _args[1])
        monkeypatch.setattr(chat_api, "_schedule_session_title_summary", lambda *_args: None)

        def request(message: str, client_turn_id: str) -> ChatTurnRequest:
            """构造 MySQL 隔离库中的稳定客户端 Turn 请求。"""

            return ChatTurnRequest(
                tenant_id=tenant_id,
                session_id=session.id,
                agent_id=agent.id,
                user_id=user.id,
                message=message,
                client_turn_id=client_turn_id,
            )

        first = chat_api.chat_turn(request("A", "mysql-turn-a"), current_user=user, db=db)
        second = chat_api.chat_turn(request("B", "mysql-turn-b"), current_user=user, db=db)
        replayed = chat_api.chat_turn(request("A", "mysql-turn-a"), current_user=user, db=db)
        complete_events = db.exec(
            select(AgentEvent).where(
                AgentEvent.session_id == session.id,
                AgentEvent.event_type == "complete",
            )
        ).all()

        assert first.reply == "产品回复:A"
        assert second.reply == "产品回复:B"
        assert replayed.reply == "产品回复:A"
        assert len(complete_events) == 2
        assert all("session_id" not in (event.payload_json or {}) for event in complete_events)
    finally:
        db.close()
        engine.dispose()


def test_mysql_tool_cycle_recovers_without_manual_action_id_and_blocks_ambiguity(
    mysql_database_url: str,
) -> None:
    """MySQL 实际工具循环按任务身份恢复，歧义恢复不得降级为新派发。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd1_mysql_cycle"
        session_id = "session_sd1_mysql_cycle"
        agent_id = "agent_sd1_mysql_cycle"
        tool_id = "tool_sd1_mysql_cycle"
        skill_id = "skill_sd1_mysql_cycle"
        tool_name = "mysql.review_ticket"
        tool_call = ToolCall(name=tool_name, arguments={"subject": "synthetic"})
        tool = Tool(
            id=tool_id,
            tenant_id=tenant_id,
            name=tool_name,
            display_name="MySQL 循环工单",
            method="POST",
            url="https://example.invalid/ticket",
            enabled=True,
        )
        skill = Skill(
            tenant_id=tenant_id,
            skill_id=skill_id,
            name="MySQL 循环身份",
            status="published",
            content_json={
                "start_node_id": "node_a",
                "nodes": [
                    {
                        "node_id": "node_a",
                        "allowed_actions": [f"call_tool:{tool_name}"],
                    },
                    {"node_id": "reply", "allowed_actions": ["answer_user"]},
                ],
                "edges": [],
            },
        )
        session = ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            active_skill_id=skill_id,
            active_step_id="node_a",
            awaiting_input_json={"task_id": "task_a"},
        )
        db.add_all(
            [
                tool,
                skill,
                session,
                AgentProfile(id=agent_id, tenant_id=tenant_id, name="MySQL 循环员工"),
            ]
        )
        db.commit()
        ensure_private_resource_binding(
            db,
            tenant_id,
            agent_id,
            "tool",
            tool_id,
            "active",
        )
        db.commit()

        loop = AgentLoop(db)
        executor = _MySQLCountingExecutor(db)
        loop.tool_executor = executor
        request = ChatTurnRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            client_turn_id="sd1-mysql-cycle-turn",
            message="执行隔离循环",
        )

        _first_step, first = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            None,
            StepAgentResult(tool_call=tool_call),
        )
        assert first is not None and first.success is True
        assert executor.dispatches == 1
        first_started = db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == tenant_id,
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == "tool_call_started",
            )
        ).one()
        first_action_id = str((first_started.payload_json or {}).get("persistent_action_id") or "")
        assert "task:task_a:skill:" in first_action_id

        session.active_step_id = "reply"
        session.awaiting_input_json = {"task_id": "task_a"}
        db.add(session)
        db.commit()
        _retry_step, retry = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            None,
            StepAgentResult(tool_call=tool_call),
        )
        assert retry is not None and retry.success is True
        assert retry.data["receipt_source"] == "compatibility_event"
        assert executor.dispatches == 1

        session.active_step_id = "node_a"
        session.awaiting_input_json = {"task_id": "task_b"}
        db.add(session)
        db.commit()
        _new_step, new_action = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            None,
            StepAgentResult(tool_call=tool_call),
        )
        assert new_action is not None and new_action.success is True
        assert executor.dispatches == 2
        action_ids = [
            str((event.payload_json or {}).get("persistent_action_id") or "")
            for event in db.exec(
                select(AgentEvent).where(
                    AgentEvent.tenant_id == tenant_id,
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "tool_call_started",
                )
            ).all()
        ]
        assert first_action_id in action_ids
        assert len(set(action_ids)) == 2

        session.active_step_id = "reply"
        session.awaiting_input_json = {"task_id": "task_b"}
        db.add(session)
        db.commit()
        _ambiguous_step, ambiguous = loop._execute_tool_action_cycle(
            request,
            session,
            skill,
            [tool],
            None,
            StepAgentResult(tool_call=tool_call),
        )
        assert ambiguous is not None and ambiguous.success is False
        assert ambiguous.error is not None
        assert ambiguous.error.code == "TOOL_CALL_OUTCOME_UNKNOWN"
        assert executor.dispatches == 2
    finally:
        db.close()
        engine.dispose()
