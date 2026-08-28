"""
@Time       : 2026/08/28 14:10
@Author     : zhanglp8181
@File       : test_tool_executor.py
@CallChain  : pytest → ToolExecutor → HTTP/MCP 工具执行与出网策略
@Description: 验证工具执行的授权、幂等键、取消和持久化 HTTP 目标安全边界。
"""

import sys
from pathlib import Path

import httpx
import pytest

from app.agents.branching import ensure_private_resource_binding
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall
from app.tools.managed_workspace import ManagedCodeWorkspaceService
from app.db.models import AgentProfile, MCPServer, Tenant, Tool
import app.security.outbound as outbound
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def test_resolve_secret_header(monkeypatch):
    monkeypatch.setenv("ORDER_API_TOKEN", "token-123")
    executor = object.__new__(ToolExecutor)

    headers = executor._resolve_headers(
        {"Authorization": "Bearer ${secret.ORDER_API_TOKEN}"},
        {},
    )

    assert headers["Authorization"] == "Bearer token-123"


def test_resolve_public_mock_key_from_settings_without_process_environment(monkeypatch):
    monkeypatch.delenv("PUBLIC_MOCK_API_KEY", raising=False)
    executor = object.__new__(ToolExecutor)
    executor.settings = type("Settings", (), {"public_mock_api_key": "configured-mock-key"})()

    headers = executor._resolve_headers(
        {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"},
        {},
    )

    assert headers == {"X-API-Key": "configured-mock-key"}


def test_third_party_bearer_header_does_not_receive_public_mock_key(monkeypatch):
    monkeypatch.setenv("BIGMODEL_API_KEY", "bigmodel-token")
    executor = object.__new__(ToolExecutor)
    executor.settings = type("Settings", (), {"public_mock_api_key": "configured-mock-key"})()

    headers = executor._resolve_headers(
        {"Authorization": "Bearer ${secret.BIGMODEL_API_KEY}"},
        {},
    )

    assert headers == {"Authorization": "Bearer bigmodel-token"}
    assert "X-API-Key" not in headers


def test_internal_mock_request_adds_service_token_only_for_configured_origin() -> None:
    executor = object.__new__(ToolExecutor)
    executor.settings = type(
        "Settings",
        (),
        {"normalized_tool_base_url": "http://127.0.0.1:5173"},
    )()

    internal = executor._request_headers(
        "http://127.0.0.1:5173/api/mock/order/query",
        {"Content-Type": "application/json"},
    )
    external = executor._request_headers(
        "https://example.test/api/mock/order/query",
        {"Content-Type": "application/json"},
    )

    assert internal[INTERNAL_SERVICE_HEADER] == internal_service_token()
    assert INTERNAL_SERVICE_HEADER not in external


def test_execute_rejects_tool_not_bound_to_current_employee() -> None:
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
        db.add(owner)
        db.add(other)
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", owner.id, "tool", tool.id, "active")
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name=tool.name, arguments={}),
            agent_id=other.id,
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "NOT_ALLOWED"


def test_execute_builtin_mcp_tool_success() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_builtin", tenant_id="tenant_demo", name="builtin", transport="builtin"
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.demo_echo",
                display_name="MCP Demo Echo",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/echo",
                mcp_server_id="server_builtin",
                config_json={"tool": "echo"},
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.demo_echo", arguments={"text": "hello mcp"}),
        )

        assert result.success is True
        assert result.data == {"text": "hello mcp", "length": 9}


def test_execute_builtin_mcp_tool_unknown_config_returns_error() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_builtin", tenant_id="tenant_demo", name="builtin", transport="builtin"
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.bad",
                display_name="Bad MCP",
                tool_type="mcp",
                method="POST",
                url="mcp://builtin.demo/missing",
                mcp_server_id="server_builtin",
                config_json={"tool": "missing"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.bad", arguments={}),
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"


def test_managed_workspace_failed_check_is_not_reported_as_success(monkeypatch) -> None:
    """容器退出非零时保留回执但必须阻断后续提交，不能把调用成功混同测试通过。"""

    monkeypatch.setattr(
        ManagedCodeWorkspaceService,
        "run_check",
        lambda self, **kwargs: {
            "profile": "backend-unit",
            "passed": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "FAILED",
        },
    )
    with _test_session() as db:
        db.add(Tenant(id="tenant_workspace", name="Workspace"))
        db.add(
            Tool(
                tenant_id="tenant_workspace",
                name="workspace.check",
                tool_type="managed_workspace",
                method="POST",
                url="",
                config_json={
                    "workspace_id": "demo",
                    "base_ref": "main",
                    "handler": "run_check",
                    "check_profiles": {"backend-unit": {}},
                },
            )
        )
        db.commit()
        executor = ToolExecutor(db)
        monkeypatch.setattr(executor.settings, "dynamic_task_managed_workspace_enabled", True)
        result = executor.execute(
            tenant_id="tenant_workspace",
            tool_call=ToolCall(name="workspace.check", arguments={"profile": "backend-unit"}),
            execution_id="exec_workspace_check",
        )

        assert result.success is False
        assert result.data["exit_code"] == 1
        assert result.error is not None
        assert result.error.code == "WORKSPACE_CHECK_FAILED"


def test_managed_workspace_change_set_rejects_extra_model_arguments(monkeypatch) -> None:
    """多文件写只能接收冻结 changes，模型不能夹带镜像、命令或工作区根路径。"""

    captured: list[list[dict[str, object]]] = []

    def fake_apply_files(self, **kwargs):  # noqa: ANN001, ANN202
        """捕获经过执行器严格参数校验后的变更清单。"""

        captured.append(kwargs["changes"])
        return {"changed_count": 1, "files": [], "branch": "task/exec", "replayed": False}

    monkeypatch.setattr(ManagedCodeWorkspaceService, "apply_files", fake_apply_files)
    with _test_session() as db:
        db.add(Tenant(id="tenant_workspace", name="Workspace"))
        db.add(
            Tool(
                tenant_id="tenant_workspace",
                name="workspace.apply-set",
                tool_type="managed_workspace",
                method="POST",
                url="",
                config_json={
                    "workspace_id": "demo",
                    "base_ref": "main",
                    "handler": "apply_files",
                },
            )
        )
        db.commit()
        executor = ToolExecutor(db)
        monkeypatch.setattr(executor.settings, "dynamic_task_managed_workspace_enabled", True)
        changes = [{"path": "a.py", "expected_sha256": None, "content": "A = 1\n"}]
        accepted = executor.execute(
            tenant_id="tenant_workspace",
            tool_call=ToolCall(name="workspace.apply-set", arguments={"changes": changes}),
            execution_id="exec_workspace_apply_set",
        )
        rejected = executor.execute(
            tenant_id="tenant_workspace",
            tool_call=ToolCall(
                name="workspace.apply-set",
                arguments={"changes": changes, "argv": ["sh", "-c", "id"]},
            ),
            execution_id="exec_workspace_apply_set",
        )

        assert accepted.success is True
        assert captured == [changes]
        assert rejected.success is False
        assert rejected.error is not None
        assert rejected.error.code == "WORKSPACE_ARGUMENTS_INVALID"


def test_execute_stdio_mcp_tool_success() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_stdio",
                tenant_id="tenant_demo",
                name="stdio",
                transport="stdio",
                command=sys.executable,
                args_json=[str(_mock_mcp_server_path())],
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.real_echo",
                display_name="Real MCP Echo",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/echo",
                mcp_server_id="server_stdio",
                config_json={"tool": "echo"},
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.real_echo", arguments={"text": "hello real mcp"}),
        )

        assert result.success is True
        assert result.data == {"text": "hello real mcp", "length": 14}


def test_execute_stdio_mcp_tool_error_is_stable() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            MCPServer(
                id="server_stdio",
                tenant_id="tenant_demo",
                name="stdio",
                transport="stdio",
                command=sys.executable,
                args_json=[str(_mock_mcp_server_path())],
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="mcp.real_sum",
                display_name="Real MCP Sum",
                tool_type="mcp",
                method="POST",
                url="mcp://stdio/mock/sum",
                mcp_server_id="server_stdio",
                config_json={"tool": "sum"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="mcp.real_sum", arguments={"numbers": ["bad"]}),
        )

        assert result.success is False
        assert result.error is not None
        assert result.error.code == "MCP_ERROR"
        assert "numbers" in result.error.message


def test_execute_get_tool_preserves_query_string_when_arguments_empty(monkeypatch) -> None:
    _fake_public_dns(monkeypatch)
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None, **kwargs):
            requested.update({"method": method, "url": url, "params": params})
            return httpx.Response(
                200,
                json={"current": {"temperature_2m": 27.4}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="weather.forecast",
                display_name="天气查询",
                method="GET",
                url=(
                    "https://api.open-meteo.com/v1/forecast"
                    "?latitude=39.90&longitude=116.40&current=temperature_2m"
                ),
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="weather.forecast", arguments={}),
        )

    assert result.success is True
    assert result.data == {"current": {"temperature_2m": 27.4}}
    assert requested == {
        "method": "GET",
        "url": (
            "https://93.184.216.34/v1/forecast"
            "?latitude=39.90&longitude=116.40&current=temperature_2m"
        ),
        "params": None,
    }


def test_execute_http_write_sends_authoritative_remote_idempotency_header(monkeypatch) -> None:
    """验证远端幂等键只进入 HTTP 写请求头，并覆盖工具配置中的同名大小写变体。"""

    _fake_public_dns(monkeypatch)
    requested: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            """接受真实客户端构造参数但不建立网络连接。"""

        def __enter__(self):
            """返回当前假客户端。"""

            return self

        def __exit__(self, *args):
            """结束上下文时无需释放外部资源。"""

            return None

        def request(self, method, url, headers=None, json=None, params=None, **kwargs):
            """记录最终请求头并返回成功响应。"""

            requested.update({"method": method, "headers": dict(headers or {}), "json": json})
            return httpx.Response(
                200,
                json={"id": "remote-1"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="expense.submit",
                method="POST",
                url="https://example.test/expenses",
                headers_json={"idempotency-key": "untrusted-config-value"},
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="expense.submit", arguments={"amount": 100}),
            remote_idempotency_key="runtime-command-key",
        )

    assert result.success is True
    assert requested["headers"] == {
        "Idempotency-Key": "runtime-command-key",
        "Host": "example.test",
    }
    assert requested["json"] == {"amount": 100}


def test_execute_http_tool_rejects_private_target_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久化 HTTP 工具也必须在连接前阻断未批准的私网目标。"""

    class MustNotRequestClient:
        def __init__(self, *args, **kwargs):
            """允许构造客户端，但任何真实请求都应在目标策略前被阻断。"""

        def __enter__(self):
            """返回测试客户端。"""

            return self

        def __exit__(self, *args):
            """结束测试上下文。"""

            return None

        def request(self, *args, **kwargs):
            """若进入请求方法则说明 SSRF 防护顺序失效。"""

            raise AssertionError("私网持久化工具不应发起 HTTP 请求")

    monkeypatch.setattr(httpx, "Client", MustNotRequestClient)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="internal.lookup",
                method="POST",
                url="http://127.0.0.1:8080/metadata",
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name="internal.lookup", arguments={}),
        )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "OUTBOUND_TARGET_BLOCKED"


def _fake_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """为持久化 HTTP 工具测试固定公网解析结果，避免依赖环境 DNS。"""

    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (
                outbound.socket.AF_INET,
                outbound.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", port),
            )
        ],
    )


def _mock_mcp_server_path() -> Path:
    return Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
