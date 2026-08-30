"""
@Time       : 2026/08/28 13:20
@Author     : zhanglp8181
@File       : tool_executor.py
@CallChain  : Agent Loop/Tool API → ToolExecutor → 授权边界 → HTTP/MCP
@Description: 在外部调用前执行受控授权，并向 HTTP 写适配器传递 Runtime 冻结的远端幂等键。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.cancellation import TurnCancellationRequested, raise_if_cancelled, run_cancellable
from app.config import get_settings
from app.db.models import MCPServer, SopOperation, Tool
from app.general_skills.proposals import (
    GeneralSkillProposalError,
    GeneralSkillProposalService,
    SKILL_PROPOSAL_TOOL_NAME,
)
from app.organization.agent_execution import (
    AgentExecutionAuthorizer,
    AgentExecutionDecision,
    AgentExecutionDenied,
)
from app.security.outbound import (
    OutboundTargetError,
    allowed_hosts_from_settings,
    prepare_outbound_request,
)
from app.dynamic_tasks.capability_catalog import ToolReliabilityContract
from app.tools.http_request import prepare_get_request
from app.tools.builtin_tools import execute_builtin_tool
from app.tools.mcp_client import MCPClientError, execute_mcp_tool
from app.tools.managed_workspace import (
    ManagedCodeWorkspaceError,
    ManagedCodeWorkspaceService,
)
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
        execution_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """鉴权后执行工具，并仅向 HTTP 非 GET 写请求发送服务端远端幂等键。"""

        raise_if_cancelled(is_cancelled)
        if tool_call.name == SKILL_PROPOSAL_TOOL_NAME:
            return self._publish_general_skill_proposal(
                tenant_id=tenant_id,
                arguments=tool_call.arguments,
                actor_user_id=actor_user_id,
                execution_id=execution_id,
            )
        with self.db.no_autoflush:
            tool = self.db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
            ).first()
        if not tool:
            return self._error(tool_call.name, "NOT_FOUND", "工具不存在或未配置。")
        if not tool.enabled:
            return self._error(tool.name, "DISABLED", "工具当前未启用。")
        destructive_error = self._validate_destructive_dispatch(
            tool,
            tool_call.arguments,
            remote_idempotency_key=remote_idempotency_key,
        )
        if destructive_error is not None:
            return destructive_error
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
            result = self._execute_mcp_tool(
                tool,
                tool_call.arguments,
                is_cancelled=is_cancelled,
            )
            raise_if_cancelled(is_cancelled)
            return self._with_authorization(result, authorization)
        if (tool.tool_type or "http") == "builtin":
            try:
                data = execute_builtin_tool(
                    self.db,
                    tenant_id=tenant_id,
                    tool_name=tool.name,
                    arguments=tool_call.arguments,
                    actor_user_id=actor_user_id,
                )
                raise_if_cancelled(is_cancelled)
                return self._with_authorization(
                    ToolResult(tool_name=tool.name, success=True, data=data, error=None),
                    authorization,
                )
            except TurnCancellationRequested:
                raise
            except Exception as exc:
                error_code = str(getattr(exc, "code", "") or "BUILTIN_EXECUTION_ERROR")
                return self._with_authorization(
                    self._error(tool.name, error_code, str(exc)),
                    authorization,
                )
        if (tool.tool_type or "http") == "managed_workspace":
            result = self._execute_managed_workspace(tool, tool_call.arguments, execution_id)
            raise_if_cancelled(is_cancelled)
            return self._with_authorization(result, authorization)
        if (tool.tool_type or "http") != "http":
            return self._error(
                tool.name, "UNSUPPORTED_TOOL_TYPE", f"不支持的工具类型：{tool.tool_type}"
            )

        tool_url = self._normalize_tool_url(tool.url)
        headers = self._request_headers(
            tool_url,
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
            allowed_hosts = allowed_hosts_from_settings(self.settings)
            with httpx.Client(
                timeout=self.settings.tool_timeout_seconds,
                follow_redirects=False,
            ) as client:
                def request_http() -> httpx.Response:
                    """校验并固定目标后执行 HTTP 请求，供 Turn 取消轮询包装。"""

                    if tool.method.upper() == "GET":
                        request_url, request_kwargs = prepare_get_request(
                            tool_url, tool_call.arguments
                        )
                    else:
                        request_url = tool_url
                        request_kwargs = {"json": tool_call.arguments}
                    target = prepare_outbound_request(
                        request_url,
                        allowed_hosts=allowed_hosts,
                    )
                    return client.request(
                        tool.method.upper(),
                        target.request_url,
                        headers={**headers, **target.headers},
                        **request_kwargs,
                        **({"extensions": target.extensions} if target.extensions else {}),
                    )

                response = run_cancellable(
                    request_http,
                    is_cancelled,
                    on_cancel=lambda: _best_effort_close_http_client(client),
                )
                raise_if_cancelled(is_cancelled)
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
        except OutboundTargetError as exc:
            return self._with_authorization(
                self._error(tool.name, "OUTBOUND_TARGET_BLOCKED", str(exc)), authorization
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
        except TurnCancellationRequested:
            raise
        except Exception as exc:
            return self._with_authorization(
                self._error(tool.name, "EXECUTION_ERROR", str(exc)), authorization
            )

    def _execute_managed_workspace(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        execution_id: str | None,
    ) -> ToolResult:
        """按工具发布的固定 handler 调用受管工作区，模型不能传入根目录或命令。"""

        if not self.settings.dynamic_task_managed_workspace_enabled:
            return self._error(tool.name, "WORKSPACE_DISABLED", "受管代码工作区未启用。")
        config = tool.config_json or {}
        workspace_id = str(config.get("workspace_id") or "")
        base_ref = str(config.get("base_ref") or "main")
        handler = str(config.get("handler") or "")
        service = ManagedCodeWorkspaceService(
            root=self.settings.dynamic_task_managed_workspace_root
        )
        try:
            if handler == "read_file":
                self._require_argument_keys(arguments, {"path"})
                data = service.read_file(
                    tenant_id=tool.tenant_id,
                    workspace_id=workspace_id,
                    path=str(arguments["path"]),
                )
            elif handler == "status":
                self._require_argument_keys(arguments, set())
                data = service.status(tenant_id=tool.tenant_id, workspace_id=workspace_id)
            elif handler == "apply_file":
                self._require_execution(execution_id)
                self._require_argument_keys(
                    arguments, {"path", "expected_sha256", "content"}
                )
                data = service.apply_file(
                    tenant_id=tool.tenant_id,
                    workspace_id=workspace_id,
                    execution_id=str(execution_id),
                    base_ref=base_ref,
                    path=str(arguments["path"]),
                    expected_sha256=str(arguments["expected_sha256"]),
                    content=str(arguments["content"]),
                )
            elif handler == "apply_files":
                self._require_execution(execution_id)
                self._require_argument_keys(arguments, {"changes"})
                changes = arguments["changes"]
                if not isinstance(changes, list):
                    raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_INVALID")
                data = service.apply_files(
                    tenant_id=tool.tenant_id,
                    workspace_id=workspace_id,
                    execution_id=str(execution_id),
                    base_ref=base_ref,
                    changes=changes,
                )
            elif handler == "run_check":
                self._require_execution(execution_id)
                self._require_argument_keys(arguments, {"profile"})
                profiles = config.get("check_profiles")
                if not isinstance(profiles, dict):
                    raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_PROFILE_INVALID")
                data = service.run_check(
                    tenant_id=tool.tenant_id,
                    workspace_id=workspace_id,
                    execution_id=str(execution_id),
                    base_ref=base_ref,
                    profile=str(arguments["profile"]),
                    profiles=profiles,
                )
                if data.get("passed") is not True:
                    return ToolResult(
                        tool_name=tool.name,
                        success=False,
                        data=data,
                        error=ToolError(
                            code="WORKSPACE_CHECK_FAILED",
                            message="受管代码检查未通过。",
                        ),
                    )
            elif handler == "commit":
                self._require_execution(execution_id)
                self._require_argument_keys(arguments, {"message", "paths"})
                paths = arguments["paths"]
                if not isinstance(paths, list) or not all(
                    isinstance(path, str) for path in paths
                ):
                    raise ManagedCodeWorkspaceError("WORKSPACE_COMMIT_PATHS_INVALID")
                data = service.commit(
                    tenant_id=tool.tenant_id,
                    workspace_id=workspace_id,
                    execution_id=str(execution_id),
                    base_ref=base_ref,
                    message=str(arguments["message"]),
                    paths=paths,
                )
            else:
                raise ManagedCodeWorkspaceError("WORKSPACE_HANDLER_FORBIDDEN")
            return ToolResult(tool_name=tool.name, success=True, data=data, error=None)
        except ManagedCodeWorkspaceError as exc:
            return self._error(tool.name, str(exc), "受管代码工作区操作被拒绝。")

    def _publish_general_skill_proposal(
        self,
        *,
        tenant_id: str,
        arguments: dict[str, Any],
        actor_user_id: str | None,
        execution_id: str | None,
    ) -> ToolResult:
        """只允许已进入 running 且绑定一次性审批的 Operation 发布暂存 Skill。"""

        if not self.settings.general_skill_agent_proposal_enabled:
            return self._error(
                SKILL_PROPOSAL_TOOL_NAME,
                "GENERAL_SKILL_PROPOSAL_DISABLED",
                "Agent 创建 Skill 功能未启用。",
            )
        if not actor_user_id or not execution_id:
            return self._error(
                SKILL_PROPOSAL_TOOL_NAME,
                "GENERAL_SKILL_PROPOSAL_CONTEXT_REQUIRED",
                "Skill 提案必须绑定发起人和持久执行。",
            )
        operation = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == tenant_id,
                SopOperation.instance_id == execution_id,
                SopOperation.operation_name == SKILL_PROPOSAL_TOOL_NAME,
                SopOperation.status == "running",
            )
        ).first()
        if operation is None or dict(operation.request_json or {}) != arguments:
            return self._error(
                SKILL_PROPOSAL_TOOL_NAME,
                "GENERAL_SKILL_PROPOSAL_OPERATION_INVALID",
                "未找到与冻结参数一致的已批准提案。",
            )
        try:
            data = GeneralSkillProposalService(self.db).publish_approved_operation(
                tenant_id=tenant_id,
                execution_id=execution_id,
                operation_id=operation.id,
                initiator_user_id=actor_user_id,
            )
            return ToolResult(
                tool_name=SKILL_PROPOSAL_TOOL_NAME,
                success=True,
                data=data,
                error=None,
            )
        except GeneralSkillProposalError as exc:
            return self._error(
                SKILL_PROPOSAL_TOOL_NAME,
                exc.code,
                "Skill 提案发布被拒绝。",
            )

    @staticmethod
    def _require_argument_keys(arguments: dict[str, Any], expected: set[str]) -> None:
        """拒绝模型额外注入 workspace、image、argv、网络或其他未发布参数。"""

        if set(arguments) != expected:
            raise ManagedCodeWorkspaceError("WORKSPACE_ARGUMENTS_INVALID")

    @staticmethod
    def _require_execution(execution_id: str | None) -> None:
        """写入、检查和提交必须绑定持久 Execution，禁止管理页试调用旁路。"""

        if not execution_id:
            raise ManagedCodeWorkspaceError("WORKSPACE_EXECUTION_REQUIRED")

    def _with_authorization(
        self, result: ToolResult, decision: AgentExecutionDecision | None
    ) -> ToolResult:
        """把成功授权的冻结上下文写入工具结果，供事件审计使用。"""

        if decision is None:
            return result
        return result.model_copy(update={"authorization_context": decision.as_dict()})

    def _validate_destructive_dispatch(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        remote_idempotency_key: str | None,
    ) -> ToolResult | None:
        """把 destructive 真实派发限制在固定目标、幂等键和隔离 provider 内。"""

        raw_contract = tool.reliability_contract_json
        if not isinstance(raw_contract, dict) or not raw_contract:
            return None
        try:
            contract = ToolReliabilityContract.model_validate(raw_contract)
        except (TypeError, ValueError):
            return self._error(tool.name, "DESTRUCTIVE_CONTRACT_INVALID", "破坏性工具契约无效。")
        if contract.risk_class != "destructive":
            return None
        if not contract.destructive_dynamic_task_enabled:
            return self._error(tool.name, "DESTRUCTIVE_NOT_PUBLISHED", "破坏性工具未发布。")
        if not remote_idempotency_key:
            return self._error(
                tool.name,
                "DESTRUCTIVE_IDEMPOTENCY_KEY_MISSING",
                "破坏性操作缺少远端幂等键。",
            )
        if (
            arguments.get("target") != contract.canonical_target
            or arguments.get("target_checksum") != contract.target_checksum
        ):
            return self._error(
                tool.name,
                "DESTRUCTIVE_TARGET_MISMATCH",
                "破坏性操作目标与冻结目标不一致。",
            )
        normalized_url = self._normalize_tool_url(tool.url)
        if contract.destructive_provider == "disposable":
            target = urlsplit(normalized_url)
            if (
                (tool.tool_type or "http") != "http"
                or not self._is_internal_mock_url(normalized_url)
                or not target.path.startswith("/api/mock/destructive/")
            ):
                return self._error(
                    tool.name,
                    "DESTRUCTIVE_PROVIDER_NOT_ISOLATED",
                    "disposable provider 必须是同源隔离测试资源。",
                )
        elif contract.destructive_provider == "isolated":
            if (tool.config_json or {}).get("isolated_provider") is not True:
                return self._error(
                    tool.name,
                    "DESTRUCTIVE_PROVIDER_NOT_ISOLATED",
                    "isolated provider 缺少隔离声明。",
                )
        else:
            return self._error(
                tool.name,
                "DESTRUCTIVE_PROVIDER_NOT_ISOLATED",
                "破坏性 provider 不在隔离白名单内。",
            )
        return None

    def _execute_mcp_tool(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """调用已持久化配置的 MCP 工具，并把异常规范化为统一工具错误。"""

        try:
            config, tool_name = self._resolve_mcp_config(tool)
            data = execute_mcp_tool(
                config,
                arguments,
                timeout_seconds=self.settings.tool_timeout_seconds,
                tool_name=tool_name,
                allowed_hosts=allowed_hosts_from_settings(self.settings),
                is_cancelled=is_cancelled,
            )
            return ToolResult(tool_name=tool.name, success=True, data=data, error=None)
        except MCPClientError as exc:
            return self._error(tool.name, "MCP_ERROR", str(exc))
        except TurnCancellationRequested:
            raise
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

    def _normalize_tool_url(self, url: str) -> str:
        """把保存工具支持的相对 mock 路径解析到配置的同源服务地址。"""

        stripped = str(url or "").strip()
        if stripped.startswith("/"):
            return f"{self.settings.normalized_tool_base_url}{stripped}"
        return stripped

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


def _best_effort_close_http_client(client: object) -> None:
    """取消 HTTP 工具调用时关闭真实客户端，兼容不提供 close 的测试替身。"""

    close = getattr(client, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
