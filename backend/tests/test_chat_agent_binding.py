"""
@Time       : 2026/08/11 01:15
@Author     : zhanglp8181
@File       : test_chat_agent_binding.py
@CallChain  : pytest → Chat API/AgentLoop → ChatSession 员工身份快照
@Description: 验证聊天与调度会话的 Agent 绑定、访问边界、标题和来源投影。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import chat as chat_api
from app.api.chat import (
    _bind_request_to_session_agent,
    _ensure_chat_agent_available,
    _user_message_metadata,
    create_chat_session,
    list_chat_sessions,
)
from app.agents.branching import ensure_private_resource_binding
from app.core.agent_loop import AgentLoop, AgentLoopPreconditionError
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    Message,
    ModelConfig,
    PersonaConfig,
    ScheduledTaskRun,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.session.session_schema import ChatSessionCreateRequest, ChatTurnRequest
from app.tools.tool_schema import ToolCall


def test_existing_chat_session_cannot_switch_agent() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(AgentProfile(id="agent_a", tenant_id="tenant_demo", name="客服 A", is_overall=False))
        db.add(AgentProfile(id="agent_b", tenant_id="tenant_demo", name="客服 B", is_overall=False))
        session = ChatSession(
            id="session_bound",
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_a",
        )
        db.add(session)
        db.commit()

        request = ChatTurnRequest(
            tenant_id="tenant_demo",
            session_id=session.id,
            user_id="user_demo",
            agent_id="agent_b",
            message="你好",
        )

        with pytest.raises(HTTPException) as exc_info:
            _bind_request_to_session_agent(db, request, session, current_user)

        assert exc_info.value.status_code == 409
        assert db.get(ChatSession, session.id).agent_id == "agent_a"


def test_chat_agent_must_be_active_non_overall_agent() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体", is_overall=True)
        )
        db.add(
            AgentProfile(
                id="agent_archived",
                tenant_id="tenant_demo",
                name="已归档",
                is_overall=False,
                status="archived",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as missing:
            _ensure_chat_agent_available(db, "tenant_demo", None, current_user)
        with pytest.raises(HTTPException) as overall:
            _ensure_chat_agent_available(db, "tenant_demo", "agent_overall", current_user)
        with pytest.raises(HTTPException) as archived:
            _ensure_chat_agent_available(db, "tenant_demo", "agent_archived", current_user)

        assert missing.value.status_code == 400
        assert overall.value.status_code == 404
        assert archived.value.status_code == 404


def test_create_chat_session_always_creates_new_agent_session() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="研发",
                is_overall=False,
                metadata_json={"owner_user_id": "user_demo"},
            )
        )
        db.add(
            ChatSession(
                id="session_existing",
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
            )
        )
        db.commit()

        first = create_chat_session(
            ChatSessionCreateRequest(tenant_id="tenant_demo", agent_id="agent_demo"),
            current_user=current_user,
            db=db,
        )
        second = create_chat_session(
            ChatSessionCreateRequest(tenant_id="tenant_demo", agent_id="agent_demo"),
            current_user=current_user,
            db=db,
        )
        session_rows = db.exec(
            select(ChatSession).where(ChatSession.agent_id == "agent_demo")
        ).all()

        assert first.id != "session_existing"
        assert second.id not in {"session_existing", first.id}
        assert len(session_rows) == 3


def test_gallery_chat_requires_usage_and_admin_cannot_open_private_staff() -> None:
    """广场员工先建立使用关系才能工作，治理管理员也不能直接使用他人私人员工。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_demo",
            username="owner",
            password_hash="x",
        )
        member = User(
            id="user_member",
            tenant_id="tenant_demo",
            username="member",
            password_hash="x",
        )
        admin = User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash="x",
        )
        gallery = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            owner_user_id=owner.id,
            published_to_gallery=True,
            visibility_scope="tenant",
        )
        private = AgentProfile(
            id="agent_private",
            tenant_id="tenant_demo",
            name="私人员工",
            owner_user_id=owner.id,
        )
        db.add(owner)
        db.add(member)
        db.add(admin)
        db.add(gallery)
        db.add(private)
        db.commit()

        with pytest.raises(HTTPException) as unused_error:
            _ensure_chat_agent_available(db, "tenant_demo", gallery.id, member)
        with pytest.raises(HTTPException) as admin_error:
            _ensure_chat_agent_available(db, "tenant_demo", private.id, admin)
        assert unused_error.value.status_code == 403
        assert admin_error.value.status_code == 403

        db.add(
            AgentUsage(
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id=gallery.id,
            )
        )
        db.commit()
        assert _ensure_chat_agent_available(db, "tenant_demo", gallery.id, member).id == gallery.id


def test_session_anchors_revision_origin_and_sanitized_capability_snapshot() -> None:
    """新会话固化版本与入口，能力快照不得带出绑定 metadata 中的凭据。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="x",
        )
        agent = AgentProfile(
            id="agent_anchor",
            tenant_id="tenant_demo",
            name="锚定员工",
            owner_user_id=current_user.id,
            profile_revision=7,
        )
        db.add(current_user)
        db.add(agent)
        db.add(
            AgentResourceBinding(
                id="agent_binding_secret",
                tenant_id="tenant_demo",
                agent_id=agent.id,
                resource_type="tool",
                resource_id="tool_demo",
                status="active",
                metadata_json={"authorization": "Bearer secret", "header": "private"},
            )
        )
        db.commit()

        created = create_chat_session(
            ChatSessionCreateRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                origin="owned",
            ),
            current_user=current_user,
            db=db,
        )
        assert created.agent_profile_revision == 7
        assert created.origin == "owned"
        assert created.capability_snapshot == {
            "agent_id": agent.id,
            "profile_revision": 7,
            "resources": [
                {
                    "resource_type": "tool",
                    "resource_id": "tool_demo",
                    "status": "active",
                }
            ],
        }
        assert "secret" not in str(created.capability_snapshot)

        agent.profile_revision = 8
        db.add(agent)
        db.commit()
        persisted = db.get(ChatSession, created.id)
        assert persisted is not None
        assert persisted.agent_profile_revision == 7
        assert persisted.capability_snapshot_json["profile_revision"] == 7


def test_legacy_agent_session_is_explicit_without_fabricated_snapshot() -> None:
    """迁移前会话返回 legacy，且不把当前员工版本伪装成历史事实。"""

    row = ChatSession(
        id="session_legacy",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
    )

    result = chat_api.session_read(row)

    assert result.origin == "legacy"
    assert result.agent_profile_revision is None
    assert result.capability_snapshot is None


def test_agent_loop_first_turn_also_anchors_session_for_draft_chat_route() -> None:
    """对话端草稿路由不先调用建会话 API 时，首轮运行仍必须固化员工版本。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_draft_anchor",
            tenant_id="tenant_demo",
            name="草稿员工",
            owner_user_id="user_demo",
            profile_revision=5,
        )
        db.add(agent)
        db.commit()

        session = AgentLoop(db)._get_or_create_session(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id=agent.id,
                message="从草稿页开始",
            )
        )

        assert session.agent_profile_revision == 5
        assert session.origin == "direct"
        assert session.capability_snapshot_json == {
            "agent_id": agent.id,
            "profile_revision": 5,
            "resources": [],
        }


def test_chat_session_list_exposes_scheduled_origin_without_title_inference() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            ChatSession(
                id="session_normal",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="定时任务：手动命名",
            )
        )
        db.add(
            ChatSession(
                id="session_scheduled",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="已重命名",
            )
        )
        db.add(
            ScheduledTaskRun(
                id="schedrun_demo",
                tenant_id="tenant_demo",
                scheduled_task_id="sched_demo",
                agent_id="agent_demo",
                user_id="user_demo",
                session_id="session_scheduled",
                source_kind="legacy",
                source_ref="legacy:schedrun_demo",
                source_snapshot_json={},
                source_checksum="legacy-schedrun-demo",
                scheduled_for=utc_now(),
            )
        )
        db.commit()

        rows = list_chat_sessions("tenant_demo", current_user=current_user, db=db)
        by_id = {row.id: row for row in rows}

        assert by_id["session_normal"].is_scheduled is False
        assert by_id["session_scheduled"].is_scheduled is True


def test_session_title_summary_uses_first_user_message_when_title_empty(monkeypatch) -> None:
    engine = _test_engine()
    monkeypatch.setattr(chat_api, "engine", engine)
    with Session(engine) as db:
        db.add(ChatSession(id="session_title", tenant_id="tenant_demo", user_id="user_demo"))
        db.add(
            Message(
                id="msg_user",
                tenant_id="tenant_demo",
                session_id="session_title",
                role="user",
                content="请查询北京今天的天气。",
            )
        )
        db.commit()

    chat_api._summarize_session_title_once("tenant_demo", "user_demo", "session_title", None)

    with Session(engine) as db:
        row = db.get(ChatSession, "session_title")
        event = db.exec(
            select(AgentEvent).where(
                AgentEvent.session_id == "session_title",
                AgentEvent.event_type == chat_api.SESSION_TITLE_SUMMARY_EVENT,
            )
        ).first()

    assert row is not None
    assert row.title == "请查询北京今天的天气"
    assert event is not None
    assert event.payload_json["title"] == "请查询北京今天的天气"


def test_session_title_summary_does_not_override_existing_title(monkeypatch) -> None:
    engine = _test_engine()
    monkeypatch.setattr(chat_api, "engine", engine)
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_manual_title",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="手动标题",
            )
        )
        db.add(
            Message(
                id="msg_user_manual",
                tenant_id="tenant_demo",
                session_id="session_manual_title",
                role="user",
                content="请查询北京今天的天气。",
            )
        )
        db.commit()

    chat_api._summarize_session_title_once("tenant_demo", "user_demo", "session_manual_title", None)

    with Session(engine) as db:
        row = db.get(ChatSession, "session_manual_title")
        events = db.exec(
            select(AgentEvent).where(AgentEvent.session_id == "session_manual_title")
        ).all()

    assert row is not None
    assert row.title == "手动标题"
    assert events == []


def test_scheduled_task_chat_turn_marks_user_message_metadata() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="每天18点复盘差评",
        interaction_mode="scheduled_task",
    )

    assert _user_message_metadata(request) == {"interaction_mode": "scheduled_task"}


def test_normal_chat_turn_user_message_metadata_is_empty() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="每天18点复盘差评",
    )

    assert _user_message_metadata(request) == {}


def test_dynamic_task_chat_turn_marks_selected_engine_metadata() -> None:
    """页面选择的 DynamicTaskAgent 必须持久化为本轮请求契约，支持审计与重放校验。"""

    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="整理材料并形成结果",
        execution_engine="dynamic_task",
    )

    assert _user_message_metadata(request) == {"execution_engine": "dynamic_task"}


def test_chat_turn_can_select_enabled_model_config() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            ModelConfig(
                id="model_default",
                tenant_id="tenant_demo",
                name="默认模型",
                api_key_encrypted="",
                model="default-model",
                is_default=True,
            )
        )
        db.add(
            ModelConfig(
                id="model_selected",
                tenant_id="tenant_demo",
                name="选择模型",
                api_key_encrypted="",
                model="selected-model",
            )
        )
        db.commit()
        loop = AgentLoop(db)

        model = loop._get_request_model(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                model_config_id="model_selected",
                message="你好",
            )
        )

        assert model is not None
        assert model.id == "model_selected"


def test_agent_loop_only_exposes_tools_bound_to_current_employee() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent_a = AgentProfile(id="agent_a", tenant_id="tenant_demo", name="员工 A")
        agent_b = AgentProfile(id="agent_b", tenant_id="tenant_demo", name="员工 B")
        tool_a = Tool(
            id="tool_a",
            tenant_id="tenant_demo",
            name="tool.a",
            method="POST",
            url="https://example.test/a",
            enabled=True,
        )
        tool_b = Tool(
            id="tool_b",
            tenant_id="tenant_demo",
            name="tool.b",
            method="POST",
            url="https://example.test/b",
            enabled=True,
        )
        db.add(agent_a)
        db.add(agent_b)
        db.add(tool_a)
        db.add(tool_b)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", agent_a.id, "tool", tool_a.id, "active")
        ensure_private_resource_binding(db, "tenant_demo", agent_b.id, "tool", tool_b.id, "active")
        db.commit()

        loop = AgentLoop(db)

        assert [row.id for row in loop._list_enabled_tools("tenant_demo", agent_a.id)] == [
            tool_a.id
        ]
        assert [row.id for row in loop._list_enabled_tools("tenant_demo", agent_b.id)] == [
            tool_b.id
        ]


def test_agent_loop_rejects_unbound_tool_before_execution_or_replay() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = AgentProfile(id="agent_owner", tenant_id="tenant_demo", name="员工 A")
        other = AgentProfile(id="agent_other", tenant_id="tenant_demo", name="员工 B")
        tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private.lookup",
            method="POST",
            url="https://example.test/private",
            enabled=True,
        )
        session = ChatSession(
            id="session_other",
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id=other.id,
        )
        db.add(owner)
        db.add(other)
        db.add(tool)
        db.add(session)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", owner.id, "tool", tool.id, "active")
        db.commit()

        result = AgentLoop(db)._execute_tool_call(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                session_id=session.id,
                user_id="user_demo",
                agent_id=other.id,
                message="执行私有工具",
            ),
            session,
            ToolCall(name=tool.name, arguments={}),
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "NOT_ALLOWED"
        event_types = [row.event_type for row in db.exec(select(AgentEvent)).all()]
        assert event_types == ["tool_call_started", "tool_call_finished"]


def test_chat_turn_rejects_disabled_selected_model_config() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            ModelConfig(
                id="model_disabled",
                tenant_id="tenant_demo",
                name="停用模型",
                api_key_encrypted="",
                model="disabled-model",
                enabled=False,
            )
        )
        db.commit()
        loop = AgentLoop(db)

        with pytest.raises(AgentLoopPreconditionError) as exc_info:
            loop._get_request_model(
                ChatTurnRequest(
                    tenant_id="tenant_demo",
                    agent_id="agent_demo",
                    model_config_id="model_disabled",
                    message="你好",
                )
            )

        assert exc_info.value.code == "disabled_model_config"


def test_agent_persona_prompt_includes_employee_identity_and_metadata() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_dev",
                tenant_id="tenant_demo",
                name="研发员工",
                description="负责研发资料查询、SOP 执行和交付记录沉淀。",
                is_overall=False,
                metadata_json={
                    "role_name": "研发",
                    "work_styles": ["目标明确", "证据优先"],
                    "expertise_tags": ["代码检索", "SOP 执行"],
                    "work_modes": ["理解需求", "推进执行"],
                    "owner_user_id": "user_demo",
                },
            )
        )
        db.commit()

        prompt = AgentLoop(db)._get_persona_prompt("tenant_demo", "agent_dev")

        assert prompt is not None
        assert "员工名称：研发员工" in prompt
        assert "员工描述：负责研发资料查询、SOP 执行和交付记录沉淀。" in prompt
        assert "岗位：研发" in prompt
        assert "工作风格：目标明确、证据优先" in prompt
        assert "擅长领域：代码检索、SOP 执行" in prompt
        assert "工作方式：理解需求、推进执行" in prompt
        assert "owner_user_id" not in prompt
        assert "user_demo" not in prompt


def test_agent_persona_prompt_keeps_custom_prompt_with_identity() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(PersonaConfig(tenant_id="tenant_demo", system_prompt="全局员工设定"))
        db.add(
            AgentProfile(
                id="agent_finance",
                tenant_id="tenant_demo",
                name="财务员工",
                description="负责报销核对。",
                persona_prompt="只能在有证据时给结论。\n必要时先追问缺失凭证。",
                is_overall=False,
                metadata_json={"role_name": "财务"},
            )
        )
        db.commit()

        prompt = AgentLoop(db)._get_persona_prompt("tenant_demo", "agent_finance")

        assert prompt is not None
        assert "员工名称：财务员工" in prompt
        assert "岗位：财务" in prompt
        assert "员工角色补充要求：" in prompt
        assert "只能在有证据时给结论。\n必要时先追问缺失凭证。" in prompt
        assert "全局员工设定" not in prompt


def _test_session() -> Session:
    return Session(_test_engine())


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine
