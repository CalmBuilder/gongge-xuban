"""
@Time       : 2026/08/28 16:00
@Author     : zhanglp8181
@File       : test_skill_generation_scope.py
@CallChain  : pytest → Skill 生成 API 辅助 → Agent 可见工具与服务端 Skill 快照
@Description: 验证 Skill distill/rewrite 不受客户端伪造能力目录和当前草稿内容扩权。
"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.skills import _tool_generation_payload, _with_available_tools, _with_available_tools_for_rewrite
from app.agents.branching import ensure_open_gallery_binding, ensure_private_resource_binding
from app.db.models import AgentProfile, Tenant, Tool
from app.skills.skill_distiller import _tool_mention_to_resolution
from app.skills.skill_schema import SkillCard, SkillDistillRequest, SkillRewriteRequest


def _test_session() -> Session:
    """创建隔离的内存数据库会话，供能力范围测试使用。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _skill_card(skill_id: str = "scope_skill") -> SkillCard:
    """构造最小合法 SkillCard，避免测试依赖模型生成结果。"""

    return SkillCard(
        skill_id=skill_id,
        name="范围测试技能",
        nodes=[
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集信息",
            }
        ],
        start_node_id="collect",
        terminal_node_ids=["collect"],
    )


def test_skill_generation_ignores_client_tool_catalog_and_uses_agent_visibility() -> None:
    """生成上下文只能包含 Agent 可见工具，客户端伪造工具不会进入 Prompt。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体", is_overall=True))
        db.add(AgentProfile(id="agent_private", tenant_id="tenant_demo", name="私有员工"))
        open_tool = Tool(
            id="tool_open",
            tenant_id="tenant_demo",
            name="open.lookup",
            method="POST",
            url="/api/mock/open",
        )
        private_tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private.lookup",
            method="POST",
            url="/api/mock/private",
        )
        db.add(open_tool)
        db.add(private_tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", open_tool.id)
        ensure_private_resource_binding(
            db, "tenant_demo", "agent_private", "tool", private_tool.id
        )
        db.commit()

        request = SkillDistillRequest(
            tenant_id="tenant_demo",
            agent_id="agent_private",
            title="范围测试",
            raw_content="调用工具完成查询",
            available_tools=[{"name": "forged.admin_tool", "url": "https://example.invalid"}],
        )
        enriched = _with_available_tools(db, request)

        assert [item["name"] for item in enriched.available_tools] == ["private.lookup"]


def test_rewrite_generation_uses_the_same_scoped_catalog() -> None:
    """改写上下文与 distill 使用同一 Agent 工具可见性规则。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_private", tenant_id="tenant_demo", name="私有员工"))
        tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private.lookup",
            method="POST",
            url="/api/mock/private",
        )
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", "agent_private", "tool", tool.id)
        db.commit()

        request = SkillRewriteRequest(
            tenant_id="tenant_demo",
            agent_id="agent_private",
            current_skill=_skill_card(),
            instruction="优化工具调用",
            available_tools=[{"name": "forged.admin_tool"}],
        )
        enriched = _with_available_tools_for_rewrite(db, request)

        assert [item["name"] for item in enriched.available_tools] == ["private.lookup"]


def test_skill_generation_payload_does_not_send_endpoint_or_schema_secrets() -> None:
    """生成上下文只能包含工具能力契约，不能把端点或 Schema 示例密钥送给模型。"""

    payload = _tool_generation_payload(
        Tool(
            id="tool_secret",
            tenant_id="tenant_demo",
            name="secret.tool",
            url="https://internal.example.test/mcp?token=private",
            input_schema={
                "type": "object",
                "properties": {"token": {"type": "string", "default": "private-token"}},
                "example": {"token": "private-token"},
            },
            output_schema={"description": "Authorization: Bearer private-token"},
            description="Call token=private-token only with password=private-password",
        )
    )

    assert "url" not in payload
    assert "private-token" not in str(payload)
    assert "private-password" not in str(payload)
    assert "internal.example.test" not in str(payload)


def test_model_tool_suggestion_redacts_url_and_schema_secrets() -> None:
    """模型返回的工具建议也必须脱敏 URL、凭据字段、枚举和 Schema 引用。"""

    raw_url = "https://user:query-secret@example.test/mcp?token=query-secret"
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        agent_id="agent_private",
        title="工具建议脱敏",
        raw_content=f"流程使用 {raw_url}",
    )
    suggestion = _tool_mention_to_resolution(
        {
            "name": "secret_candidate",
            "url": raw_url,
            "description": "Authorization: Bearer private-token",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "enum": ["private-token"],
                        "description": "password=private-password",
                    },
                    "query": {"type": "string", "pattern": "private-token"},
                },
                "$ref": "https://internal.example.test/schema",
            },
            "output_schema": {"description": "secret=private-output"},
            "sample_arguments": {"token": "private-token"},
        },
        request,
    )

    assert suggestion is not None
    serialized = str(suggestion.model_dump(mode="json"))
    assert "query-secret" not in serialized
    assert "private-token" not in serialized
    assert "private-password" not in serialized
    assert "private-output" not in serialized
    assert "internal.example.test" not in serialized
    assert suggestion.input_schema["properties"] == {"query": {"type": "string"}}
