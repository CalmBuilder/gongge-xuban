import subprocess
import sys
from pathlib import Path

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_open_gallery_binding, ensure_private_resource_binding
from app.api.tools import (
    _ensure_tool_visible,
    _normalize_probe_url,
    create_tool,
    delete_tool,
    list_tools,
    probe_tool as _probe_tool,
    tool_read,
)
from app.config import get_settings
from app.db.models import AgentProfile, AgentResourceBinding, Tenant, Tool, User
from app.tools.tool_schema import ToolCreateRequest, ToolProbeRequest


def _admin_user() -> User:
    return User(
        id="user_admin", tenant_id="tenant_demo", username="ops", role="admin", password_hash="test"
    )


def _member_user() -> User:
    return User(
        id="user_member",
        tenant_id="tenant_demo",
        username="member",
        role="member",
        password_hash="test",
    )


def probe_tool(request: ToolProbeRequest, db: Session):  # noqa: ANN201
    return _probe_tool(request, db, _member_user())


def test_managed_workspace_tool_requires_admin_and_valid_local_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证受管多文件工具只能由租户管理员为实际存在的本地仓库发布。"""

    root = tmp_path / "managed"
    repo = root / "tenant_demo" / "refund-demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "robot@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Workspace Robot"], check=True
    )
    (repo / "README.md").write_text("test workspace\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_task_managed_workspace_root", str(root))
    request = ToolCreateRequest.model_validate(
        {
            "tenant_id": "tenant_demo",
            "name": "workspace.refund.apply-set",
            "tool_type": "managed_workspace",
            "method": "POST",
            "url": "",
            "mcp_config": {
                "workspace_id": "refund-demo",
                "base_ref": "main",
                "handler": "apply_files",
            },
            "input_schema": {
                "type": "object",
                "properties": {"changes": {"type": "array", "items": {"type": "object"}}},
                "required": ["changes"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "files": {"type": "array"},
                    "changed_count": {"type": "integer"},
                    "branch": {"type": "string"},
                },
            },
            "reliability_contract": {
                "risk_class": "local_write",
                "side_effect": "local",
                "confirmation_policy": "once",
                "idempotency": {"mode": "none"},
                "reconcile": {"supported": False},
                "model_visibility": {
                    "allowed_paths": [
                        "input.changes",
                        "output.files",
                        "output.changed_count",
                        "output.branch",
                    ],
                    "user_display_paths": ["output.files", "output.branch"],
                    "audit_only_paths": [],
                },
                "timeout_policy": "failed",
                "dynamic_task_enabled": True,
            },
        }
    )
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_workspace",
                tenant_id="tenant_demo",
                owner_user_id="user_member",
                name="Workspace Agent",
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as denied:
            create_tool(request, "agent_workspace", db, current_user=_member_user())
        db.rollback()
        created = create_tool(request, "agent_workspace", db, current_user=_admin_user())

        assert denied.value.status_code == 403
        assert created.tool_type == "managed_workspace"
        assert created.reliability_contract is not None
        assert created.reliability_contract.risk_class == "local_write"


def test_delete_tool_removes_tenant_tool() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        tool = Tool(
            tenant_id="tenant_demo",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)

        result = delete_tool(tool.id, "tenant_demo", db, current_user=_admin_user())

        assert result == {"status": "deleted"}
        assert db.get(Tool, tool.id) is None


def test_delete_tool_is_tenant_scoped() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(Tenant(id="tenant_other", name="Other"))
        tool = Tool(
            tenant_id="tenant_other",
            name="product.lookup",
            display_name="商品查询",
            method="POST",
            url="/api/mock/product/lookup",
        )
        db.add(tool)
        db.commit()
        db.refresh(tool)

        with pytest.raises(HTTPException) as exc_info:
            delete_tool(tool.id, "tenant_demo", db, current_user=_admin_user())

        assert exc_info.value.status_code == 404
        assert db.get(Tool, tool.id) is not None


def test_open_gallery_delete_tool_hides_gallery_without_removing_agent_binding() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                resource_type="tool",
                resource_id=tool.id,
                status="active",
            )
        )
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        result = delete_tool(
            tool.id,
            "tenant_demo",
            db,
            agent_id="agent_overall",
            current_user=_admin_user(),
        )

        assert result == {"status": "hidden"}
        assert db.get(Tool, tool.id) is not None
        branch_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_branch",
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        assert branch_binding.status == "active"
        assert list_tools("tenant_demo", bucket=None, agent_id="agent_overall", db=db) == []
        assert list_tools("tenant_demo", bucket=None, agent_id="agent_branch", db=db) == []


def test_open_gallery_tool_read_returns_persisted_creator_metadata() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.commit()
        ensure_open_gallery_binding(
            db,
            "tenant_demo",
            "tool",
            tool.id,
            "active",
            metadata_json={"creator_name": "admin", "created_by_username": "admin"},
        )
        db.commit()

        rows = list_tools("tenant_demo", bucket=None, agent_id="agent_overall", db=db)

        assert len(rows) == 1
        assert rows[0].metadata["creator_name"] == "admin"
        assert rows[0].metadata["created_by_username"] == "admin"


def test_tool_read_discloses_credential_keys_without_values() -> None:
    """工具读取只返回凭据配置状态，普通列表不能获得认证值。"""

    row = Tool(
        id="tool_secret",
        tenant_id="tenant_demo",
        name="secret.lookup",
        method="POST",
        url="https://example.test/lookup",
        headers_json={"Authorization": "Bearer private", "X-Trace": "trace-value"},
        auth_json={"api_key": "private-key"},
        config_json={"token": "mcp-private"},
    )

    result = tool_read(row)

    assert result.headers == {}
    assert result.auth == {}
    assert result.mcp_config == {}
    assert result.credential_state.configured_fields == ["headers", "auth", "mcp_config"]
    assert result.credential_state.header_keys == ["Authorization", "X-Trace"]
    assert result.credential_state.auth_keys == ["api_key"]
    assert result.credential_state.mcp_config_keys == ["token"]


def test_tool_api_publishes_read_contract_but_legacy_default_stays_out() -> None:
    """验证显式纯读契约生成 checksum，未声明工具仍默认不进动态目录。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.commit()
        legacy = create_tool(
            ToolCreateRequest(
                tenant_id="tenant_demo",
                name="legacy.lookup",
                method="GET",
                url="https://example.invalid/legacy",
            ),
            agent_id="agent_overall",
            db=db,
            current_user=_admin_user(),
        )
        published = create_tool(
            ToolCreateRequest(
                tenant_id="tenant_demo",
                name="weather.lookup",
                method="GET",
                url="https://example.invalid/weather",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
                reliability_contract={
                    "risk_class": "read",
                    "side_effect": "none",
                    "confirmation_policy": "none",
                    "idempotency": {
                        "mode": "none",
                        "argument": None,
                        "remote_scope": None,
                    },
                    "reconcile": {
                        "supported": False,
                        "tool_name": None,
                        "reference_source": None,
                        "terminal_status_mapping": {},
                    },
                    "model_visibility": {
                        "allowed_paths": ["input.city"],
                        "user_display_paths": [],
                        "audit_only_paths": [],
                    },
                    "timeout_policy": "failed",
                    "dynamic_task_enabled": True,
                },
            ),
            agent_id="agent_overall",
            db=db,
            current_user=_admin_user(),
        )

        assert legacy.reliability_contract is None
        assert legacy.reliability_checksum is None
        assert published.reliability_contract is not None
        assert published.reliability_checksum is not None


def test_reconcile_contract_requires_an_enabled_published_read_tool() -> None:
    """验证外部写不能仅填对账工具名就伪装为可对账。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as caught:
            create_tool(
                ToolCreateRequest(
                    tenant_id="tenant_demo",
                    name="message.send",
                    method="POST",
                    url="https://example.invalid/send",
                    reliability_contract={
                        "risk_class": "external_write",
                        "side_effect": "external",
                        "confirmation_policy": "once",
                        "idempotency": {
                            "mode": "none",
                            "argument": None,
                            "remote_scope": None,
                        },
                        "reconcile": {
                            "supported": True,
                            "tool_name": "message.status",
                            "reference_source": "result.message_id",
                            "terminal_status_mapping": {"sent": "complete"},
                        },
                        "model_visibility": {
                            "allowed_paths": [],
                            "user_display_paths": [],
                            "audit_only_paths": [],
                        },
                        "timeout_policy": "unknown",
                        "dynamic_task_enabled": False,
                    },
                ),
                agent_id="agent_overall",
                db=db,
                current_user=_admin_user(),
            )

        assert caught.value.status_code == 422
        assert caught.value.detail == "RECONCILE_TOOL_NOT_AVAILABLE"


def test_tool_api_rejects_visibility_path_missing_from_schema() -> None:
    """验证发布不得接受无法在当前 schema 解析的模型可见路径。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as caught:
            create_tool(
                ToolCreateRequest(
                    tenant_id="tenant_demo",
                    name="weather.invalid-view",
                    method="GET",
                    url="https://example.invalid/weather",
                    input_schema={"type": "object", "properties": {}},
                    reliability_contract={
                        "risk_class": "read",
                        "side_effect": "none",
                        "confirmation_policy": "none",
                        "idempotency": {
                            "mode": "none",
                            "argument": None,
                            "remote_scope": None,
                        },
                        "reconcile": {
                            "supported": False,
                            "tool_name": None,
                            "reference_source": None,
                            "terminal_status_mapping": {},
                        },
                        "model_visibility": {
                            "allowed_paths": ["input.city"],
                            "user_display_paths": [],
                            "audit_only_paths": [],
                        },
                        "timeout_policy": "failed",
                        "dynamic_task_enabled": True,
                    },
                ),
                agent_id="agent_overall",
                db=db,
                current_user=_admin_user(),
            )

        assert caught.value.status_code == 422
        assert caught.value.detail == "CAPABILITY_PATH_NOT_FOUND:input.city"


def test_private_tool_is_not_visible_without_employee_scope() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(id="agent_private", tenant_id="tenant_demo", name="研发员工")
        tool = Tool(
            id="tool_private",
            tenant_id="tenant_demo",
            name="private.lookup",
            method="POST",
            url="https://example.test/private",
        )
        db.add(agent)
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", agent.id, "tool", tool.id, "active")
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            _ensure_tool_visible(db, "tenant_demo", tool, None)

        assert exc_info.value.status_code == 404


def test_agent_without_tool_binding_does_not_see_open_gallery_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        rows = list_tools("tenant_demo", bucket=None, agent_id="agent_branch", db=db)

        assert rows == []


def test_invalid_agent_id_does_not_fall_back_to_open_gallery_tools() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather.forecast",
            display_name="天气查询",
            method="POST",
            url="/api/mock/weather",
        )
        db.add(tool)
        db.commit()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            list_tools("tenant_demo", bucket=None, agent_id="agent_missing", db=db)

        assert exc_info.value.status_code == 404


def test_probe_tool_success_infers_output_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            assert method == "POST"
            assert url == (
                f"{get_settings().normalized_tool_base_url}"
                "/api/mock/member/benefit-reconcile"
            )
            assert json == {"user_id": "user_demo", "order_id": "A12345"}
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "missing_benefits": [{"benefit_id": "coupon_001", "amount": 30}],
                },
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="member.benefit_reconcile",
                method="POST",
                url="/api/mock/member/benefit-reconcile",
                sample_arguments={"user_id": "user_demo", "order_id": "A12345"},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.inferred_output_schema["properties"]["found"]["type"] == "boolean"
        assert result.inferred_output_schema["properties"]["missing_benefits"]["type"] == "array"


def test_probe_mcp_tool_success_infers_output_schema() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.demo_sum",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/sum",
                mcp_config={"server": "builtin.demo", "tool": "sum"},
                sample_arguments={"numbers": [1, 2, 3]},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.data_preview == {"numbers": [1, 2, 3], "total": 6, "count": 3}
        assert result.inferred_output_schema["properties"]["total"]["type"] == "integer"


def test_probe_get_tool_preserves_query_string_when_arguments_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            requested.update({"method": method, "url": url, "params": params})
            return httpx.Response(200, json={"current": {"temperature_2m": 27.4}})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="weather.forecast",
                method="GET",
                url=(
                    "https://api.open-meteo.com/v1/forecast"
                    "?latitude=39.90&longitude=116.40&current=temperature_2m"
                ),
                sample_arguments={},
            ),
            db,
        )

    assert result.success is True
    assert requested == {
        "method": "GET",
        "url": (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=39.90&longitude=116.40&current=temperature_2m"
        ),
        "params": None,
    }


def test_probe_get_tool_sends_sample_arguments_as_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            requested.update({"method": method, "url": url, "params": params, "json": json})
            return httpx.Response(200, json={"timezone": "Asia/Shanghai"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="weather.forecast",
                method="GET",
                url="https://api.open-meteo.com/v1/forecast",
                sample_arguments={
                    "latitude": "39.90",
                    "longitude": "116.40",
                    "current": "temperature_2m,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                },
            ),
            db,
        )

    assert result.success is True
    assert requested == {
        "method": "GET",
        "url": "https://api.open-meteo.com/v1/forecast",
        "params": {
            "latitude": "39.90",
            "longitude": "116.40",
            "current": "temperature_2m,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Shanghai",
        },
        "json": None,
    }


def test_probe_mcp_tool_error_is_stable() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.bad",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/missing",
                mcp_config={"server": "builtin.demo", "tool": "missing"},
                sample_arguments={},
            ),
            db,
        )

        assert result.success is False
        assert result.status_code == 400
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"


def test_probe_stdio_mcp_tool_success_infers_output_schema() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="mcp.real_product_lookup",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/product_lookup",
                mcp_config={
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(_mock_mcp_server_path())],
                    "tool": "product_lookup",
                },
                sample_arguments={"product_id": "A1"},
            ),
            db,
        )

        assert result.success is True
        assert result.status_code == 200
        assert result.data_preview["found"] is True
        assert result.data_preview["price"] == 129.0
        assert result.inferred_output_schema["properties"]["price"]["type"] == "number"


def test_probe_tool_relative_url_uses_configured_tool_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_BASE_URL", "http://127.0.0.1:10086/")
    get_settings.cache_clear()
    try:
        assert _normalize_probe_url("/api/mock/member/benefit-reconcile") == (
            "http://127.0.0.1:10086/api/mock/member/benefit-reconcile"
        )
    finally:
        get_settings.cache_clear()


def test_probe_tool_http_error_returns_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="missing.tool",
                method="POST",
                url="http://example.invalid/missing",
                sample_arguments={"query": "x"},
            ),
            db,
        )

        assert result.success is False
        assert result.status_code == 404
        assert result.error is not None
        assert result.error.code == "HTTP_ERROR"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _mock_mcp_server_path() -> Path:
    return Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"
