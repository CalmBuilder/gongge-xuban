"""
@Time       : 2026/09/02 10:00
@Author     : zhanglp8181
@File       : executor.py
@CallChain  : DynamicTaskAgent → VerticalAbilityToolExecutor → frozen snapshot → adapter
@Description: 将内置能力适配器接入现有 ToolExecutor，同时阻断无 Execution 快照的同名旁路。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlmodel import Session

from app.cancellation import TurnCancellationRequested
from app.config import get_settings
from app.db.models import SopInstance
from app.security.dns_resolver import default_adaptive_resolver
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
)
from app.vertical_abilities.credentials import (
    VerticalAbilityCredentialError,
    resolve_jina_credential,
    resolve_provider_credential,
)
from app.vertical_abilities.connection_service import (
    VerticalAbilityConnectionError,
    resolve_user_connection_credentials,
)
from app.vertical_abilities.platform_runtime import PlatformRuntimeError
from app.vertical_abilities.relay_service import (
    BrowserRelayError,
    enqueue_and_wait_for_relay_command,
)
from app.vertical_abilities.resolver import VerticalAbilityResolver


class VerticalAbilityToolExecutor:
    """在现有工具执行器外增加“冻结快照后才可调用”的内置能力分派。"""

    def __init__(self, db: Session) -> None:
        """绑定数据库会话、普通工具执行器和平台能力目录。"""

        self.db = db
        self.delegate = ToolExecutor(db)
        self.resolver = VerticalAbilityResolver()

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
        """校验能力包已冻结到指定 Execution 后分派适配器，否则委托普通工具路径。"""

        definition = self.resolver.find_definition_for_capability(tool_call.name)
        instance = self.db.get(SopInstance, execution_id) if execution_id else None
        frozen_tools = (instance.capability_snapshot_json or {}).get("tools", []) if instance else []
        was_vertical = isinstance(frozen_tools, list) and any(
            isinstance(raw, dict) and raw.get("name") == tool_call.name
            and isinstance(raw.get("audit_view"), dict)
            and isinstance(raw["audit_view"].get("vertical_ability"), dict)
            for raw in frozen_tools
        )
        if definition is None and not was_vertical:
            return self.delegate.execute(
                tenant_id,
                tool_call,
                active_skill_id=active_skill_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
                execution_org_unit_id=execution_org_unit_id,
                remote_idempotency_key=remote_idempotency_key,
                execution_id=execution_id,
                is_cancelled=is_cancelled,
            )
        if not execution_id:
            return self._error(tool_call.name, "VERTICAL_ABILITY_EXECUTION_REQUIRED", "内置垂直能力只能在动态 Execution 中调用。")
        if (
            instance is None
            or instance.kind != "dynamic_task"
            or instance.tenant_id != tenant_id
            or (agent_id and instance.agent_id != agent_id)
        ):
            return self._error(tool_call.name, "VERTICAL_ABILITY_EXECUTION_INVALID", "能力包 Execution 上下文无效。")
        frozen = self._frozen_capability(instance, tool_call.name)
        if frozen is None:
            return self._error(tool_call.name, "VERTICAL_ABILITY_NOT_FROZEN", "能力包未冻结到当前 Execution。")
        capability = self.resolver.compatible_capability(frozen)
        try:
            jina_api_key: str | None = None
            platform_credentials: dict[str, str] = {}
            platform_connections: dict[str, dict[str, str]] = {}
            if actor_user_id:
                platform_connections = resolve_user_connection_credentials(
                    self.db,
                    tenant_id=tenant_id,
                    user_id=actor_user_id,
                )
            relay_executor = self._build_relay_executor(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                platform_connections=platform_connections,
            )
            if capability.adapter in {
                "jina.reader",
                "linkedin.public_read",
                "github.public_repo",
                "github.public_search",
            }:
                credential = resolve_jina_credential(self.db, settings=get_settings())
                if capability.adapter in {"jina.reader", "linkedin.public_read"} and credential.source == "none":
                    return self._error(
                        tool_call.name,
                        "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
                        "全网情报员需要先配置 Jina API Key。",
                    )
                jina_api_key = credential.value or None
            elif capability.adapter in {"media.whisper_transcribe", "xiaoyuzhou.whisper_transcribe"}:
                platform_credentials = self._resolve_whisper_credentials(tool_call.arguments)
                requested_provider = str(tool_call.arguments.get("provider") or "auto").strip().casefold() or "auto"
                if requested_provider in {"auto", "groq", "openai"} and not platform_credentials:
                    label = {
                        "auto": "Groq 或 OpenAI",
                        "groq": "Groq",
                        "openai": "OpenAI",
                    }[requested_provider]
                    return self._error(
                        tool_call.name,
                        "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
                        f"全网情报员 YouTube 无字幕转写需要先在能力包配置中配置 {label} API Key。",
                    )
            data = execute_vertical_adapter(
                capability.adapter,
                tool_call.arguments,
                settings=get_settings(),
                is_cancelled=is_cancelled,
                resolver=default_adaptive_resolver(),
                jina_api_key=jina_api_key,
                platform_credentials=platform_credentials,
                platform_connections=platform_connections,
                relay_executor=relay_executor,
            )
        except VerticalAbilityCredentialError as exc:
            return self._error(tool_call.name, exc.code, exc.message)
        except VerticalAbilityConnectionError as exc:
            return self._error(tool_call.name, exc.code, exc.message)
        except VerticalAbilityAdapterError as exc:
            return self._error(tool_call.name, exc.code, exc.message)
        except TurnCancellationRequested:
            raise
        except Exception:
            return self._error(tool_call.name, "VERTICAL_ABILITY_EXECUTION_ERROR", "能力包适配器执行失败。")
        return ToolResult(tool_name=tool_call.name, success=True, data=data)

    def _build_relay_executor(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None,
        platform_connections: dict[str, dict[str, str]],
    ) -> Callable[..., bytes] | None:
        """为当前 Execution 绑定用户级 Relay 会话，拒绝把远程命令变成通用代理。"""

        if not actor_user_id:
            return None

        def execute_relay_command(
            platform_id: str,
            action: str,
            positional_args: list[str],
            limit: int | None,
            *,
            timeout_seconds: float,
            is_cancelled: Callable[[], bool] | None,
        ) -> bytes:
            """只把已解析的平台、动作和位置参数投递到当前用户的 Relay 会话。"""

            credentials = platform_connections.get(platform_id) or {}
            reference = str(credentials.get("session_reference") or "")
            if not reference.startswith("relay:"):
                raise PlatformRuntimeError(
                    "VERTICAL_ABILITY_RELAY_NOT_CONNECTED",
                    "当前网站没有可用的宿主机浏览器 Relay。",
                )
            try:
                return enqueue_and_wait_for_relay_command(
                    self.db,
                    tenant_id=tenant_id,
                    user_id=actor_user_id,
                    session_id=reference.removeprefix("relay:"),
                    platform_id=platform_id,
                    action=action,
                    positional_args=positional_args,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                    is_cancelled=is_cancelled,
                )
            except BrowserRelayError as exc:
                raise PlatformRuntimeError(exc.code, exc.message) from exc

        return execute_relay_command

    def _resolve_whisper_credentials(self, arguments: dict[str, object]) -> dict[str, str]:
        """按本次转写请求选择 Groq/OpenAI 凭据，只把密钥留在当前调用栈。"""

        provider = str(arguments.get("provider") or "auto").strip().casefold() or "auto"
        provider_ids = {
            "auto": ("groq", "openai"),
            "groq": ("groq",),
            "openai": ("openai",),
        }.get(provider, ())
        credentials: dict[str, str] = {}
        for provider_id in provider_ids:
            credential = resolve_provider_credential(
                self.db,
                provider_id,
                settings=get_settings(),
            )
            if credential.source != "none" and credential.value:
                credentials[provider_id] = credential.value
        return credentials

    def _is_frozen_capability(
        self,
        instance: SopInstance,
        capability_name: str,
        ability_id: str,
    ) -> bool:
        """确认能力名和包身份已冻结，且当前适配器仍兼容该历史合同。"""

        snapshot = self._frozen_capability(instance, capability_name)
        return snapshot is not None and snapshot.audit_view["vertical_ability"]["id"] == ability_id

    def _frozen_capability(
        self, instance: SopInstance, capability_name: str,
    ) -> CapabilitySnapshot | None:
        """以当前 Execution 的工具归属分派，避免共享工具被目录首个包截获。"""

        frozen = instance.capability_snapshot_json or {}
        if not instance.capability_checksum or capability_checksum(frozen) != instance.capability_checksum:
            return None
        tools = frozen.get("tools")
        if not isinstance(tools, list):
            return None
        matches = [raw for raw in tools if isinstance(raw, dict) and raw.get("name") == capability_name]
        if len(matches) != 1:
            return None
        for raw in matches:
            audit = raw.get("audit_view")
            marker = audit.get("vertical_ability") if isinstance(audit, dict) else None
            if not isinstance(marker, dict) or marker.get("id") not in frozen.get("ability_ids", ()):
                return None
            try:
                snapshot = CapabilitySnapshot.model_validate(raw)
                if snapshot.tenant_id != instance.tenant_id or snapshot.agent_id != instance.agent_id:
                    return None
                self.resolver.compatible_capability(snapshot)
            except (KeyError, ValueError):
                return None
            return snapshot
        return None

    @staticmethod
    def _error(tool_name: str, code: str, message: str) -> ToolResult:
        """构造与既有 ToolExecutor 一致的结构化失败结果。"""

        return ToolResult(
            tool_name=tool_name,
            success=False,
            error=ToolError(code=code, message=message),
        )
