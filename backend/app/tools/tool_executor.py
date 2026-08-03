"""
@Time       : 2026/07/22 22:18
@Author     : zhanglp8181
@File       : tool_executor.py
@CallChain  : Agent Loop/Tool API → ToolExecutor → 授权边界 → HTTP/MCP
@Description: 在外部调用前执行受控授权，并向 HTTP 写适配器传递 Runtime 冻结的远端幂等键。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.config import get_settings
from app.db.models import MCPServer, Tool
from app.organization.agent_execution import (
    AgentExecutionAuthorizer,
    AgentExecutionDecision,
    AgentExecutionDenied,
)
from app.tools.http_request import prepare_get_request
from app.tools.builtin_tools import execute_builtin_tool
from app.tools.mcp_client import MCPClientError, execute_mcp_tool
from app.tools.tool_schema import ToolCall, ToolError, ToolResult
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token


SECRET_PATTERN = re.compile(r"\$\{secret\.([A-Z0-9_]+)\}")


class ToolExecutor:
    def __init__(self, db: Session):
        """初始化工具执行器及其数字员工授权服务。"""

        self.db = db
        self.settings = get_settings()
        self.agent_authorizer = AgentExecutionAuthorizer(db)

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        actor_user_id: str | None = None,
        execution_org_unit_id: str | None = None,
        remote_idempotency_key: str | None = None,
    ) -> ToolResult:
        """鉴权后执行工具，并仅向 HTTP 非 GET 写请求发送服务端远端幂等键。"""

        with self.db.no_autoflush:
            tool = self.db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
            ).first()
        if not tool:
            return self._error(tool_call.name, "NOT_FOUND", "工具不存在或未配置。")
        if not tool.enabled:
            return self._error(tool.name, "DISABLED", "工具当前未启用。")
        if agent_id and tool.id not in {
            row.id
            for row in visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)
        }:
            return self._error(tool.name, "NOT_ALLOWED", "当前员工未启用该工具。")
        if (
            active_skill_id
            and tool.allowed_skills_json
            and active_skill_id not in tool.allowed_skills_json
            and not tool.required_permission_code
        ):
            return self._error(tool.name, "NOT_ALLOWED", "当前技能不允许调用该工具。")

        authorization: AgentExecutionDecision | None = None
        if tool.required_permission_code:
            try:
                authorization = self.agent_authorizer.authorize(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    active_skill_id=active_skill_id,
                    allowed_skill_ids=list(tool.allowed_skills_json or []),
                    permission_code=tool.required_permission_code,
                    authorization_mode=tool.permission_authorization_mode,
                    organization_unit_id=execution_org_unit_id,
                )
            except AgentExecutionDenied as exc:
                return self._error(tool.name, exc.code, exc.message)

        if (tool.tool_type or "http") == "mcp":
            return self._with_authorization(
                self._execute_mcp_tool(tool, tool_call.arguments), authorization
            )
        if (tool.tool_type or "http") == "builtin":
            try:
                data = execute_builtin_tool(
                    self.db,
                    tenant_id=tenant_id,
                    tool_name=tool.name,
                    arguments=tool_call.arguments,
                    actor_user_id=actor_user_id,
                )
                return self._with_authorization(
                    ToolResult(tool_name=tool.name, success=True, data=data, error=None),
                    authorization,
                )
            except Exception as exc:
                error_code = str(getattr(exc, "code", "") or "BUILTIN_EXECUTION_ERROR")
                return self._with_authorization(
                    self._error(tool.name, error_code, str(exc)),
                    authorization,
                )
        if (tool.tool_type or "http") != "http":
            return self._error(
                tool.name, "UNSUPPORTED_TOOL_TYPE", f"不支持的工具类型：{tool.tool_type}"
            )

        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        if remote_idempotency_key and tool.method.upper() != "GET":
            headers = {
                key: value
                for key, value in headers.items()
                if key.lower() != "idempotency-key"
            }
            headers["Idempotency-Key"] = remote_idempotency_key
        try:
            with httpx.Client(timeout=self.settings.tool_timeout_seconds) as client:
                if tool.method.upper() == "GET":
                    request_url, request_kwargs = prepare_get_request(tool.url, tool_call.arguments)
                    response = client.request(
                        tool.method.upper(), request_url, headers=headers, **request_kwargs
                    )
                else:
                    response = client.request(
                        tool.method.upper(), tool.url, headers=headers, json=tool_call.arguments
                    )
                response.raise_for_status()
                return self._with_authorization(
                    ToolResult(
                        tool_name=tool.name,
                        success=True,
                        data=self._response_data(response),
                        error=None,
                    ),
                    authorization,
                )
        except httpx.TimeoutException:
            return self._with_authorization(
                self._error(tool.name, "TIMEOUT", "工具调用超时。"), authorization
            )
        except httpx.HTTPStatusError as exc:
            return self._with_authorization(
                self._error(
                    tool.name,
                    "HTTP_ERROR",
                    f"工具返回异常状态码：{exc.response.status_code}",
                ),
                authorization,
            )
        except Exception as exc:
            return self._with_authorization(
                self._error(tool.name, "EXECUTION_ERROR", str(exc)), authorization
            )

    def _with_authorization(
        self, result: ToolResult, decision: AgentExecutionDecision | None
    ) -> ToolResult:
        """把成功授权的冻结上下文写入工具结果，供事件审计使用。"""

        if decision is None:
            return result
        return result.model_copy(update={"authorization_context": decision.as_dict()})

    def _execute_mcp_tool(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        """调用已持久化配置的 MCP 工具，并把异常规范化为统一工具错误。"""

        try:
            config, tool_name = self._resolve_mcp_config(tool)
            data = execute_mcp_tool(
                config,
                arguments,
                timeout_seconds=self.settings.tool_timeout_seconds,
                tool_name=tool_name,
            )
            return ToolResult(tool_name=tool.name, success=True, data=data, error=None)
        except MCPClientError as exc:
            return self._error(tool.name, "MCP_ERROR", str(exc))
        except Exception as exc:
            return self._error(tool.name, "MCP_EXECUTION_ERROR", str(exc))

    def _resolve_mcp_config(self, tool: Tool) -> tuple[dict[str, Any], str | None]:
        """通过持久化 MCP Server 关联解析客户端配置和目标工具名。"""
        tool_config = tool.config_json or {}
        tool_name = (
            str(tool_config.get("tool") or tool_config.get("tool_name") or "").strip() or None
        )
        if not tool.mcp_server_id:
            raise MCPClientError("MCP 工具未关联 Server。")
        server = self.db.get(MCPServer, tool.mcp_server_id)
        if server is None:
            raise MCPClientError("MCP 工具关联的 Server 不存在或已删除。")
        return self._server_client_config(server), tool_name

    def _server_client_config(self, server: MCPServer) -> dict[str, Any]:
        """把不同传输类型的服务端记录转换为 MCP 客户端配置。"""

        transport = server.transport or "streamable_http"
        config: dict[str, Any] = {"transport": transport}
        if transport in {"streamable_http", "sse"}:
            config["url"] = server.url or ""
            if server.headers_json:
                config["headers"] = dict(server.headers_json)
        elif transport == "stdio":
            config["command"] = server.command or ""
            config["args"] = list(server.args_json or [])
            if server.env_json:
                config["env"] = dict(server.env_json)
            if server.cwd:
                config["cwd"] = server.cwd
        elif transport == "builtin":
            config["server"] = "builtin.demo"
        return config

    def _response_data(self, response: httpx.Response) -> Any:
        """优先解析 JSON 响应，非 JSON 内容则保留原始文本。"""

        try:
            return response.json()
        except Exception:
            return response.text

    def _resolve_headers(self, headers: dict[str, Any], auth: dict[str, Any]) -> dict[str, str]:
        """解析工具请求头和认证配置中受控引用的环境密钥。"""

        resolved = {key: self._resolve_secret(str(value)) for key, value in headers.items()}
        if auth.get("type") == "bearer" and auth.get("token"):
            resolved["Authorization"] = f"Bearer {self._resolve_secret(str(auth['token']))}"
        return resolved

    def _request_headers(self, url: str, headers: dict[str, str]) -> dict[str, str]:
        """仅为同源公共 mock 请求附加内部服务认证头。"""

        if not self._is_internal_mock_url(url):
            return headers
        resolved = dict(headers)
        resolved[INTERNAL_SERVICE_HEADER] = internal_service_token()
        return resolved

    def _is_internal_mock_url(self, url: str) -> bool:
        """判断目标是否为当前部署同源且路径受限的公共 mock 接口。"""

        target = urlsplit(url)
        if not target.path.startswith("/api/mock/"):
            return False
        if not target.scheme and not target.netloc:
            return True
        configured = urlsplit(self.settings.normalized_tool_base_url)
        return (
            target.scheme.lower(),
            target.hostname,
            target.port or _default_port(target.scheme),
        ) == (
            configured.scheme.lower(),
            configured.hostname,
            configured.port or _default_port(configured.scheme),
        )

    def _resolve_secret(self, value: str) -> str:
        """替换字符串中的受控密钥占位符，不向日志输出密钥值。"""

        def repl(match: re.Match[str]) -> str:
            """按环境变量名称返回单个密钥占位符的实际值。"""

            name = match.group(1)
            if name == "PUBLIC_MOCK_API_KEY":
                return self.settings.public_mock_api_key
            return os.getenv(name, "")

        return SECRET_PATTERN.sub(repl, value)

    def _error(self, tool_name: str, code: str, message: str) -> ToolResult:
        """构造统一且不携带业务成功数据的工具失败回执。"""

        return ToolResult(
            tool_name=tool_name,
            success=False,
            data=None,
            error=ToolError(code=code, message=message),
        )


def _default_port(scheme: str) -> int | None:
    """返回 HTTP/HTTPS 的默认端口，未知协议不推断端口。"""

    return 443 if scheme.lower() == "https" else 80 if scheme.lower() == "http" else None
