"""
@Time       : 2026/08/10 18:55
@Author     : zhanglp8181
@File       : capability_catalog.py
@CallChain  : Tool/GeneralSkill publish → capability catalog → DynamicTaskAgent dispatch
@Description: 校验动态能力可靠性契约，生成脱敏快照并在执行前重新授权。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, select

from app.agents.branching import (
    get_agent,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    visible_tool_rows,
)
from app.agents.identity import agent_owner_user_id
from app.connectors.service import (
    CONNECTION_READ_PERMISSION_CODE,
    CONNECTION_WRITE_PERMISSION_CODE,
    ConnectionError,
    authorize_connection_read_actor,
    authorize_connection_write_actor,
)
from app.connectors.wecom import (
    WECOM_APPLICATION_INFO_ACTION,
    WECOM_APPLICATION_READ_SCOPE,
    WECOM_MESSAGE_SEND_ACTION,
)
from app.db.models import (
    AgentConnectionBinding,
    AgentResourceBinding,
    ConnectionProfile,
    ConnectorThreadBinding,
    GeneralSkill,
    GeneralSkillRevision,
    ModelConfig,
    Tool,
    User,
    utc_now,
)
from app.organization.agent_execution import AgentExecutionAuthorizer, AgentExecutionDenied
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.general_skills.proposals import SKILL_PROPOSAL_TOOL_NAME
from app.config import get_settings


_SECRET_PATH_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)
_PATH_PATTERN = re.compile(r"^(input|output)(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")
logger = logging.getLogger(__name__)


class CapabilityAccessDenied(RuntimeError):
    """表示动态能力在解析或执行前再授权时被确定性拒绝。"""


class IdempotencyContract(BaseModel):
    """声明远端命令的幂等方式与业务键来源。"""

    mode: Literal["none", "request_key", "business_key"] = "none"
    argument: str | None = None
    remote_scope: str | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_combination(self) -> "IdempotencyContract":
        """拒绝缺少远端作用域或业务键字段的伪幂等声明。"""

        if self.mode == "none" and (self.argument is not None or self.remote_scope is not None):
            raise ValueError("none 幂等模式不得声明 argument/remote_scope")
        if self.mode == "request_key" and (
            self.argument is not None or not (self.remote_scope or "").strip()
        ):
            raise ValueError("request_key 必须只声明 remote_scope")
        if self.mode == "business_key" and (
            not (self.argument or "").strip() or not (self.remote_scope or "").strip()
        ):
            raise ValueError("business_key 必须声明 argument 和 remote_scope")
        return self


class ReconcileContract(BaseModel):
    """声明外部写的对账工具、引用来源和终态映射。"""

    supported: bool = False
    tool_name: str | None = None
    reference_source: str | None = None
    terminal_status_mapping: dict[str, Literal["complete", "failed", "unknown"]] = Field(
        default_factory=dict
    )
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_combination(self) -> "ReconcileContract":
        """确保支持对账时所有必需字段完整，不支持时不伪留配置。"""

        configured = bool(
            (self.tool_name or "").strip()
            or (self.reference_source or "").strip()
            or self.terminal_status_mapping
        )
        if self.supported and not (
            (self.tool_name or "").strip()
            and (self.reference_source or "").strip()
            and self.terminal_status_mapping
        ):
            raise ValueError("启用对账必须声明工具、引用来源和终态映射")
        if not self.supported and configured:
            raise ValueError("未启用对账时不得保留对账配置")
        return self


class ModelVisibilityContract(BaseModel):
    """声明模型可见、用户可见和仅审计可见的 schema 路径。"""

    allowed_paths: list[str] = Field(default_factory=list)
    user_display_paths: list[str] = Field(default_factory=list)
    audit_only_paths: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_paths(self) -> "ModelVisibilityContract":
        """拒绝非法路径、敏感字段以及三类视图之间的交叉。"""

        groups = [self.allowed_paths, self.user_display_paths, self.audit_only_paths]
        normalized_groups: list[set[str]] = []
        for paths in groups:
            if len(paths) != len(set(paths)):
                raise ValueError("视图路径不得重复")
            for path in paths:
                if not _PATH_PATTERN.fullmatch(path):
                    raise ValueError(f"非法视图路径: {path}")
                if any(part.lower() in _SECRET_PATH_PARTS for part in path.split(".")):
                    raise ValueError(f"凭据路径不得进入能力视图: {path}")
            normalized_groups.append(set(paths))
        if normalized_groups[2] & (normalized_groups[0] | normalized_groups[1]):
            raise ValueError("审计专用路径不得进入模型或用户视图")
        return self


class ToolReliabilityContract(BaseModel):
    """动态任务工具的服务端可靠性发布契约。"""

    risk_class: Literal["read", "local_write", "execute", "external_write", "destructive"]
    side_effect: Literal["none", "local", "external"]
    confirmation_policy: Literal["none", "once", "always", "forbidden"]
    idempotency: IdempotencyContract = Field(default_factory=IdempotencyContract)
    reconcile: ReconcileContract = Field(default_factory=ReconcileContract)
    model_visibility: ModelVisibilityContract = Field(default_factory=ModelVisibilityContract)
    timeout_policy: Literal["failed", "unknown"]
    dynamic_task_enabled: bool = False
    explore_safe: bool = False
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_reliability(self) -> "ToolReliabilityContract":
        """校验风险、副作用、确认、超时与幂等/对账之间的不变式。"""

        expected_side_effect = {
            "read": "none",
            "local_write": "local",
            "execute": "local",
            "external_write": "external",
            "destructive": "external",
        }[self.risk_class]
        if self.side_effect != expected_side_effect:
            raise ValueError("风险类别与副作用类别不一致")
        if self.risk_class == "read" and (
            self.confirmation_policy != "none"
            or self.timeout_policy != "failed"
            or self.idempotency.mode != "none"
            or self.reconcile.supported
        ):
            raise ValueError("纯读工具不得伪声明确认、幂等或对账")
        if self.side_effect == "external" and self.timeout_policy != "unknown":
            raise ValueError("外部副作用超时必须进入 unknown")
        if self.risk_class == "external_write" and self.confirmation_policy == "none":
            raise ValueError("外部写不得无确认策略")
        if self.risk_class == "destructive" and self.confirmation_policy != "forbidden":
            raise ValueError("首期破坏性能力必须禁止")
        if self.dynamic_task_enabled and self.risk_class == "destructive":
            raise ValueError("破坏性能力不得进入首期动态目录")
        if self.explore_safe and (
            not self.dynamic_task_enabled
            or self.risk_class != "read"
            or self.side_effect != "none"
            or self.confirmation_policy != "none"
        ):
            raise ValueError("Explore 只允许显式启用的无副作用纯读工具")
        return self


class CapabilityViews(BaseModel):
    """保存同一能力的模型、用户和审计投影。"""

    model: dict[str, Any]
    user: dict[str, Any]
    audit: dict[str, Any]


class CapabilitySnapshot(BaseModel):
    """保存计划/操作引用的不可变能力事实，不承载持续授权。"""

    capability_type: Literal["tool", "general_skill", "knowledge", "connector"]
    capability_id: str
    tenant_id: str
    agent_id: str
    name: str
    checksum: str
    contract: dict[str, Any]
    model_view: dict[str, Any]
    user_view: dict[str, Any]
    audit_view: dict[str, Any]


def capability_checksum(value: object) -> str:
    """对严格 RFC8259 规范 JSON 计算稳定 SHA-256，拒绝隐式字符串化。"""

    normalized = _strict_json(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_contract_payload(contract: ToolReliabilityContract) -> dict[str, Any]:
    """序列化发布契约，并在默认关闭时保持既有 checksum 向后兼容。"""

    payload = contract.model_dump(mode="json")
    if payload.get("explore_safe") is False:
        payload.pop("explore_safe", None)
    return payload


def project_tool_capability(tool: Tool, contract: ToolReliabilityContract) -> CapabilityViews:
    """根据显式路径生成不包含 headers/auth/config 的三视图投影。"""

    identity = {
        "id": tool.id,
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "risk_class": contract.risk_class,
        "confirmation_policy": contract.confirmation_policy,
    }
    model_paths = contract.model_visibility.allowed_paths
    user_paths = contract.model_visibility.user_display_paths
    model_view = {
        **identity,
        **_schema_projection(tool.input_schema or {}, tool.output_schema or {}, model_paths),
    }
    user_view = {
        **identity,
        **_schema_projection(tool.input_schema or {}, tool.output_schema or {}, user_paths),
    }
    audit_view = {
        **identity,
        "tool_type": tool.tool_type,
        "method": tool.method,
        "url": tool.url,
        "contract": _tool_contract_payload(contract),
        "input_schema": dict(tool.input_schema or {}),
        "output_schema": dict(tool.output_schema or {}),
    }
    if tool.tool_type == "managed_workspace":
        config = tool.config_json or {}
        audit_view["managed_workspace"] = {
            "workspace_id": config.get("workspace_id"),
            "base_ref": config.get("base_ref"),
            "handler": config.get("handler"),
            "check_profile_names": sorted(
                str(name) for name in (config.get("check_profiles") or {})
            ),
            "check_profiles_checksum": (
                capability_checksum(config.get("check_profiles") or {})
                if config.get("handler") == "run_check"
                else None
            ),
        }
    return CapabilityViews(model=model_view, user=user_view, audit=audit_view)


def publish_tool_contract(
    tool: Tool, contract: ToolReliabilityContract | None
) -> None:
    """在工具行上发布或撤销规范契约，同步维护快照 checksum 与时间。"""

    if contract is None:
        tool.reliability_contract_json = {}
        tool.reliability_checksum = None
        tool.reliability_published_at = None
        return
    tool.reliability_contract_json = _tool_contract_payload(contract)
    tool.reliability_checksum = None
    snapshot = DynamicCapabilityCatalog._tool_snapshot(tool, "__publication__", contract)
    tool.reliability_checksum = snapshot.checksum
    tool.reliability_published_at = utc_now()


def published_tool_snapshot(tool: Tool, agent_id: str) -> CapabilitySnapshot | None:
    """返回任意已发布工具契约的规范快照，不将 dynamic_task_enabled 解释为授权。"""

    raw = tool.reliability_contract_json
    if not isinstance(raw, dict) or not raw or not tool.reliability_checksum:
        return None
    try:
        contract = ToolReliabilityContract.model_validate(raw)
        return DynamicCapabilityCatalog._tool_snapshot(tool, agent_id, contract)
    except (TypeError, ValueError, CapabilityAccessDenied):
        return None


class DynamicCapabilityCatalog:
    """从实时租户/数字员工边界解析动态能力并在 dispatch 前再授权。"""

    def __init__(self, db: Session):
        """绑定当前事务会话，使目录解析与实时授权使用同一数据边界。"""

        self.db = db

    def list_tools(
        self,
        tenant_id: str,
        agent_id: str,
    ) -> list[CapabilitySnapshot]:
        """仅返回已绑定、已启用且显式发布为动态可用的工具。"""

        snapshots: list[CapabilitySnapshot] = []
        for tool in visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False):
            contract = self._published_tool_contract(tool)
            if contract is None or contract.risk_class not in {
                "read",
                "local_write",
                "execute",
            }:
                continue
            snapshots.append(self._tool_snapshot(tool, agent_id, contract))
        return snapshots

    def list_actor_tools(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> list[CapabilitySnapshot]:
        """返回必须同时依赖当前用户身份的内建能力，不改变既有工具目录接口。"""

        if not get_settings().general_skill_agent_proposal_enabled:
            return []
        actor = self.db.get(User, actor_user_id)
        agent = get_agent(self.db, tenant_id, agent_id)
        if (
            actor is None
            or actor.tenant_id != tenant_id
            or actor.membership_status != "active"
            or agent is None
            or agent_owner_user_id(agent) != actor.id
        ):
            return []
        return [self._skill_proposal_snapshot(tenant_id, agent_id)]

    def list_connector_reads(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> list[CapabilitySnapshot]:
        """把明确绑定且当前健康可用的 provider 账号投影为只读能力。"""

        try:
            authorize_connection_read_actor(
                self.db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
            )
        except ConnectionError:
            return []

        bindings = self.db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.agent_id == agent_id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).all()
        snapshots: list[CapabilitySnapshot] = []
        for binding in bindings:
            profile = self.db.get(ConnectionProfile, binding.profile_id)
            if profile is None or profile.tenant_id != tenant_id or profile.status != "active":
                continue
            if (
                profile.provider == "slack"
                and "channels:read" in set(binding.allowed_scopes_json or [])
                and "channels:read" in set(profile.granted_scopes_json or [])
                and "slack.channel_info" in set(profile.tool_allowlist_json or [])
            ):
                snapshots.append(self._slack_channel_snapshot(profile, binding))
            elif (
                profile.provider == "wecom"
                and WECOM_APPLICATION_READ_SCOPE in set(binding.allowed_scopes_json or [])
                and WECOM_APPLICATION_READ_SCOPE in set(profile.granted_scopes_json or [])
                and WECOM_APPLICATION_INFO_ACTION in set(profile.tool_allowlist_json or [])
            ):
                snapshots.append(self._wecom_application_snapshot(profile, binding))
        return snapshots

    def list_connector_writes(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
        session_id: str,
        *,
        source_kind: str = "chat",
        source_ref: str | None = None,
    ) -> list[CapabilitySnapshot]:
        """为交互线程投影一次性批准能力，为调度来源投影精确长期规则能力。"""

        if source_kind == "schedule" and source_ref:
            from app.dynamic_tasks.standing_approvals import scheduled_write_snapshots

            return scheduled_write_snapshots(
                self.db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                initiator_user_id=actor_user_id,
                run_id=source_ref,
            )

        thread = self.db.exec(
            select(ConnectorThreadBinding).where(
                ConnectorThreadBinding.tenant_id == tenant_id,
                ConnectorThreadBinding.session_id == session_id,
                ConnectorThreadBinding.agent_id == agent_id,
                ConnectorThreadBinding.user_id == actor_user_id,
                ConnectorThreadBinding.provider == "wecom",
                ConnectorThreadBinding.status == "active",
            )
        ).first()
        if thread is None:
            return []
        profile = self.db.get(ConnectionProfile, thread.profile_id)
        binding = self.db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.agent_id == agent_id,
                AgentConnectionBinding.profile_id == thread.profile_id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).first()
        if (
            profile is None
            or profile.tenant_id != tenant_id
            or profile.status != "active"
            or binding is None
            or WECOM_MESSAGE_SEND_ACTION not in set(profile.tool_allowlist_json or [])
            or WECOM_MESSAGE_SEND_ACTION not in set(binding.allowed_actions_json or [])
            or not self.write_approver_ids(tenant_id, exclude_user_id=actor_user_id)
        ):
            return []
        return [self._wecom_message_snapshot(profile, binding, thread)]

    def resolve_tool(
        self, tenant_id: str, agent_id: str, operation_name: str
    ) -> CapabilitySnapshot:
        """按当前数字员工可见边界解析工具，未发布契约时确定性拒绝。"""

        for snapshot in self.list_tools(tenant_id, agent_id):
            if snapshot.name == operation_name:
                return snapshot
        raise CapabilityAccessDenied("CAPABILITY_NOT_AVAILABLE")

    @staticmethod
    def _slack_channel_snapshot(
        profile: ConnectionProfile,
        binding: AgentConnectionBinding,
    ) -> CapabilitySnapshot:
        """生成不含 token、secret reference 和可变健康状态的 Slack 只读快照。"""

        name = f"slack.channel_info@{profile.id}"
        payload = {
            "capability_type": "connector",
            "capability_id": profile.id,
            "tenant_id": profile.tenant_id,
            "name": name,
            "contract": {
                "risk_class": "read",
                "side_effect": "none",
                "required_permission_code": CONNECTION_READ_PERMISSION_CODE,
                "provider": "slack",
                "required_scope": "channels:read",
                "required_action": "slack.channel_info",
            },
            "model_view": {
                "name": name,
                "display_name": f"读取 Slack 频道信息（{profile.display_name}）",
                "description": "按频道 ID 读取该工作区中当前应用可见的频道基础信息。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {
                            "type": "string",
                            "description": "Slack 频道 ID",
                        }
                    },
                    "required": ["channel_id"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "is_private": {"type": "boolean"},
                        "is_archived": {"type": "boolean"},
                        "topic": {"type": "object", "additionalProperties": True},
                        "purpose": {"type": "object", "additionalProperties": True},
                    },
                    "additionalProperties": False,
                },
            },
            "user_view": {
                "name": "Slack 频道信息",
                "account_display_name": profile.display_name,
            },
            "audit_view": {
                "provider": "slack",
                "profile_id": profile.id,
                "account_id": profile.account_id,
                "binding_id": binding.id,
                "required_scope": "channels:read",
                "required_action": "slack.channel_info",
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=binding.agent_id,
            checksum=capability_checksum(payload),
        )

    @staticmethod
    def _wecom_application_snapshot(
        profile: ConnectionProfile,
        binding: AgentConnectionBinding,
    ) -> CapabilitySnapshot:
        """生成不含 CorpID、AgentID、Secret、token 或密钥引用的企业微信只读快照。"""

        name = f"{WECOM_APPLICATION_INFO_ACTION}@{profile.id}"
        payload = {
            "capability_type": "connector",
            "capability_id": profile.id,
            "tenant_id": profile.tenant_id,
            "name": name,
            "contract": {
                "risk_class": "read",
                "side_effect": "none",
                "required_permission_code": CONNECTION_READ_PERMISSION_CODE,
                "provider": "wecom",
                "required_scope": WECOM_APPLICATION_READ_SCOPE,
                "required_action": WECOM_APPLICATION_INFO_ACTION,
                "required_result_evidence_paths": ["name", "enabled", "home_url"],
            },
            "model_view": {
                "name": name,
                "display_name": f"读取企业微信应用信息（{profile.display_name}）",
                "description": "读取当前已绑定自建应用的名称、状态和基础说明，不读取成员信息。",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "home_url": {"type": "string"},
                    },
                    "required": ["name", "enabled", "home_url"],
                    "additionalProperties": False,
                },
            },
            "user_view": {
                "name": "企业微信应用信息",
                "account_display_name": profile.display_name,
            },
            "audit_view": {
                "provider": "wecom",
                "profile_id": profile.id,
                "account_id": profile.account_id,
                "binding_id": binding.id,
                "required_scope": WECOM_APPLICATION_READ_SCOPE,
                "required_action": WECOM_APPLICATION_INFO_ACTION,
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=binding.agent_id,
            checksum=capability_checksum(payload),
        )

    @staticmethod
    def _wecom_message_snapshot(
        profile: ConnectionProfile,
        binding: AgentConnectionBinding,
        thread: ConnectorThreadBinding,
    ) -> CapabilitySnapshot:
        """冻结当前线程目标与连接修订，模型只可生成待批准正文。"""

        name = f"{WECOM_MESSAGE_SEND_ACTION}@{profile.id}"
        target_checksum = capability_checksum(
            {
                "tenant_id": profile.tenant_id,
                "profile_id": profile.id,
                "thread_binding_id": thread.id,
                "agent_id": binding.agent_id,
            }
        )
        payload = {
            "capability_type": "connector",
            "capability_id": profile.id,
            "tenant_id": profile.tenant_id,
            "name": name,
            "contract": {
                "risk_class": "external_write",
                "side_effect": "external",
                "confirmation_policy": "once",
                "required_permission_code": CONNECTION_WRITE_PERMISSION_CODE,
                "provider": "wecom",
                "required_scope": WECOM_APPLICATION_READ_SCOPE,
                "required_action": WECOM_MESSAGE_SEND_ACTION,
                "canonical_target": f"wecom_thread:{thread.id}",
                "target_checksum": target_checksum,
                "profile_revision": profile.revision,
                "secret_revision": profile.secret_revision,
                "binding_revision": binding.revision,
                "idempotency": {
                    "mode": "provider_duplicate_check",
                    "window_seconds": 1800,
                },
                "reconcile": {"supported": False, "fallback": "exception_attention"},
                "required_result_evidence_paths": ["delivery_status"],
            },
            "model_view": {
                "name": name,
                "display_name": f"向当前企业微信会话发送消息（{profile.display_name}）",
                "description": "生成待审批的精确消息正文；批准前不会调用企业微信发送接口。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4000,
                            "description": "将原样展示给审批人并发送到当前线程的正文",
                        }
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "delivery_status": {"type": "string"},
                        "message_id": {"type": "string"},
                    },
                    "required": ["delivery_status", "message_id"],
                    "additionalProperties": False,
                },
            },
            "user_view": {
                "name": "企业微信审批后发送",
                "account_display_name": profile.display_name,
                "target": "当前企业微信会话",
            },
            "audit_view": {
                "provider": "wecom",
                "profile_id": profile.id,
                "profile_revision": profile.revision,
                "secret_revision": profile.secret_revision,
                "binding_id": binding.id,
                "binding_revision": binding.revision,
                "thread_binding_id": thread.id,
                "target_checksum": target_checksum,
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=binding.agent_id,
            checksum=capability_checksum(payload),
        )

    def write_approver_ids(self, tenant_id: str, *, exclude_user_id: str) -> list[str]:
        """返回当前仍具外部写权限的活动非发起人，防止规划不可办理动作。"""

        users = self.db.exec(
            select(User).where(
                User.tenant_id == tenant_id,
                User.membership_status == "active",
                User.id != exclude_user_id,
            )
        ).all()
        approved: list[str] = []
        for user in users:
            try:
                authorize_connection_write_actor(
                    self.db,
                    tenant_id=tenant_id,
                    actor_user_id=user.id,
                )
            except ConnectionError:
                continue
            approved.append(user.id)
        return approved

    def list_general_skills(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str | None = None,
    ) -> list[CapabilitySnapshot]:
        """仅返回当前数字员工可见、已发布且 checksum 与快照一致的通用技能。"""

        snapshots = [
            self._general_skill_snapshot(row, agent_id)
            for row in self._visible_general_skills(tenant_id, agent_id)
            if row.status == "published" and self._valid_general_skill_publication(row)
        ]
        if get_settings().general_skill_resolver_v2_enabled and actor_user_id:
            actor = self.db.get(User, actor_user_id)
            if actor is None or actor.tenant_id != tenant_id:
                return []
            catalog = EffectiveGeneralSkillResolver(self.db).resolve(actor, agent_id)
            return [
                snapshot
                for item in catalog.items
                if (snapshot := self._resolved_general_skill_snapshot(item, agent_id)) is not None
            ]
        if get_settings().general_skill_resolver_v2_shadow and actor_user_id:
            actor = self.db.get(User, actor_user_id)
            if actor is not None and actor.tenant_id == tenant_id:
                catalog = EffectiveGeneralSkillResolver(self.db).resolve(actor, agent_id)
                logger.info(
                    "dynamic_general_skill_resolver_shadow",
                    extra={
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "user_id": actor_user_id,
                        "authorization_revision": catalog.authorization_revision,
                        "eligibility_hash": catalog.eligibility_hash,
                        "legacy_skill_ids": sorted(item.capability_id for item in snapshots),
                        "effective_skill_ids": sorted(item.skill_id for item in catalog.items),
                    },
                )
        return snapshots

    def _resolved_general_skill_snapshot(
        self,
        item,
        agent_id: str,
    ) -> CapabilitySnapshot | None:
        """从 resolver 固定 revision 构造动态目录快照，不信任可变根正文。"""

        root = self.db.get(GeneralSkill, item.skill_id)
        revision = self.db.get(GeneralSkillRevision, item.revision_id)
        if root is None or revision is None:
            return None
        model_view: dict[str, Any] = {
            "id": root.id,
            "slug": root.slug,
            "name": item.name,
            "description": item.description,
            "usage_mode": item.usage_mode,
            "revision_id": item.revision_id,
            "revision_number": item.revision_number,
        }
        payload = {
            "capability_type": "general_skill",
            "capability_id": root.id,
            "tenant_id": root.tenant_id,
            "name": root.slug,
            "contract": {
                "usage_mode": item.usage_mode,
                "revision_id": item.revision_id,
                "invocation_policy": item.invocation_policy,
            },
            "model_view": model_view,
            "user_view": {
                key: model_view[key]
                for key in ("id", "slug", "name", "description", "usage_mode")
            },
            "audit_view": {
                "skill_id": item.skill_id,
                "revision_id": item.revision_id,
                "revision_number": item.revision_number,
                "content_checksum": item.content_checksum,
                "binding_id": item.binding_id,
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=capability_checksum(payload),
        )

    def resolve_general_skill(
        self, tenant_id: str, agent_id: str, slug: str
    ) -> CapabilitySnapshot:
        """按 slug 解析已发布原子技能或规划指南快照。"""

        for snapshot in self.list_general_skills(tenant_id, agent_id):
            if snapshot.name == slug:
                return snapshot
        raise CapabilityAccessDenied("GENERAL_SKILL_NOT_AVAILABLE")

    def reauthorize_tool(
        self,
        snapshot: CapabilitySnapshot,
        *,
        actor_user_id: str | None,
        organization_unit_id: str | None,
        active_skill_id: str | None = None,
    ) -> Tool | None:
        """忽略快照的历史授权含义，重查实时启用、绑定、契约和组织权限。"""

        if snapshot.name == SKILL_PROPOSAL_TOOL_NAME:
            actor = self.db.get(User, actor_user_id or "")
            agent = get_agent(self.db, snapshot.tenant_id, snapshot.agent_id)
            current = self._skill_proposal_snapshot(snapshot.tenant_id, snapshot.agent_id)
            if (
                not get_settings().general_skill_agent_proposal_enabled
                or actor is None
                or actor.tenant_id != snapshot.tenant_id
                or actor.membership_status != "active"
                or agent is None
                or agent_owner_user_id(agent) != actor.id
            ):
                raise CapabilityAccessDenied("GENERAL_SKILL_PROPOSAL_ACTOR_DENIED")
            if current.checksum != snapshot.checksum:
                raise CapabilityAccessDenied("CAPABILITY_REVISION_CHANGED")
            return None
        tool = self.db.get(Tool, snapshot.capability_id)
        if tool is None or tool.tenant_id != snapshot.tenant_id:
            raise CapabilityAccessDenied("CAPABILITY_NOT_FOUND")
        if not tool.enabled:
            raise CapabilityAccessDenied("CAPABILITY_DISABLED")
        visible_ids = {
            row.id
            for row in visible_tool_rows(
                self.db, snapshot.tenant_id, snapshot.agent_id, include_inactive=False
            )
        }
        if tool.id not in visible_ids:
            raise CapabilityAccessDenied("CAPABILITY_BINDING_REVOKED")
        contract = self._published_tool_contract(tool)
        if contract is None:
            raise CapabilityAccessDenied("CAPABILITY_CONTRACT_REVOKED")
        current = self._tool_snapshot(tool, snapshot.agent_id, contract)
        if current.checksum != snapshot.checksum:
            raise CapabilityAccessDenied("CAPABILITY_REVISION_CHANGED")
        if tool.required_permission_code:
            try:
                AgentExecutionAuthorizer(self.db).authorize(
                    tenant_id=snapshot.tenant_id,
                    agent_id=snapshot.agent_id,
                    actor_user_id=actor_user_id,
                    active_skill_id=active_skill_id,
                    allowed_skill_ids=list(tool.allowed_skills_json or []),
                    permission_code=tool.required_permission_code,
                    authorization_mode=tool.permission_authorization_mode,
                    organization_unit_id=organization_unit_id,
                )
            except AgentExecutionDenied as exc:
                raise CapabilityAccessDenied(exc.code) from exc
        return tool

    @staticmethod
    def _skill_proposal_snapshot(tenant_id: str, agent_id: str) -> CapabilitySnapshot:
        """构造平台内建的 Agent Skill 提案能力；它只能收窄既有授权且必须逐次人审。"""

        payload = {
            "capability_type": "tool",
            "capability_id": SKILL_PROPOSAL_TOOL_NAME,
            "tenant_id": tenant_id,
            "name": SKILL_PROPOSAL_TOOL_NAME,
            "contract": {
                "risk_class": "local_write",
                "side_effect": "local",
                "confirmation_policy": "once",
                "idempotency": {"mode": "request_key", "remote_scope": "skill_proposal"},
                "reconcile": {"supported": False},
                "timeout_policy": "failed",
                "dynamic_task_enabled": True,
            },
            "model_view": {
                "name": SKILL_PROPOSAL_TOOL_NAME,
                "display_name": "提议把已完成方法保存为当前分身的 Skill",
                "description": (
                    "生成待本人审核的 Skill 草稿；批准前不可见，批准后以 user_only 固定修订绑定当前分身。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"},
                        "description": {"type": "string", "minLength": 1, "maxLength": 500},
                        "instructions": {"type": "string", "minLength": 1, "maxLength": 48000},
                        "requested_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                        },
                        "files": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "artifact_id": {"type": "string"},
                                    "path": {"type": "string"},
                                },
                                "required": ["artifact_id", "path"],
                                "additionalProperties": False,
                            },
                        },
                        "target_skill_id": {"type": ["string", "null"]},
                    },
                    "required": ["name", "description", "instructions", "requested_tools", "files"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "proposal_id": {"type": "string"},
                        "skill_id": {"type": "string"},
                        "revision_id": {"type": "string"},
                        "binding_id": {"type": "string"},
                        "status": {"const": "published"},
                    },
                    "required": ["proposal_id", "skill_id", "revision_id", "binding_id", "status"],
                    "additionalProperties": True,
                },
            },
            "user_view": {
                "name": "Agent 创建 Skill 提案",
                "approval": "每次都需本人审核",
                "publication_scope": "当前用户的当前分身",
            },
            "audit_view": {
                "platform_capability": "general_skill_proposal",
                "publication_policy": "review_then_publish_user_only",
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=capability_checksum(payload),
        )

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """在创建动态 Execution 前检查模型已启用且通过必需能力预检。"""

        model = self.db.get(ModelConfig, model_config_id)
        if model is None or model.tenant_id != tenant_id or not model.enabled:
            raise CapabilityAccessDenied("MODEL_NOT_AVAILABLE")
        if getattr(model, "preflight_status", "unverified") != "ready":
            raise CapabilityAccessDenied("MODEL_PREFLIGHT_REQUIRED")
        facts = getattr(model, "capability_snapshot_json", None) or {}
        required = ("sdk_available", "credentials_verified", "tool_calling", "structured_output")
        if not all(facts.get(name) is True for name in required):
            raise CapabilityAccessDenied("MODEL_CAPABILITY_MISSING")
        checksum = capability_checksum(facts)
        if checksum != getattr(model, "capability_checksum", None):
            raise CapabilityAccessDenied("MODEL_CAPABILITY_SNAPSHOT_INVALID")
        return model

    def reauthorize_general_skill(self, snapshot: CapabilitySnapshot) -> GeneralSkill:
        """在加载指南或执行原子技能前重查状态、绑定与发布修订。"""

        row = self.db.get(GeneralSkill, snapshot.capability_id)
        if row is None or row.tenant_id != snapshot.tenant_id:
            raise CapabilityAccessDenied("GENERAL_SKILL_NOT_FOUND")
        if row.status != "published":
            raise CapabilityAccessDenied("GENERAL_SKILL_DISABLED")
        visible_ids = {
            item.id
            for item in self._visible_general_skills(snapshot.tenant_id, snapshot.agent_id)
        }
        if row.id not in visible_ids:
            raise CapabilityAccessDenied("GENERAL_SKILL_BINDING_REVOKED")
        if not self._valid_general_skill_publication(row):
            raise CapabilityAccessDenied("GENERAL_SKILL_PUBLICATION_INVALID")
        current = self._general_skill_snapshot(row, snapshot.agent_id)
        if current.checksum != snapshot.checksum:
            raise CapabilityAccessDenied("GENERAL_SKILL_REVISION_CHANGED")
        return row

    @staticmethod
    def _published_tool_contract(tool: Tool) -> ToolReliabilityContract | None:
        """解析已发布契约，无契约、校验失败或动态开关关闭均按不可用处理。"""

        raw = getattr(tool, "reliability_contract_json", None)
        if not isinstance(raw, dict) or not raw:
            return None
        try:
            contract = ToolReliabilityContract.model_validate(raw)
        except (TypeError, ValueError):
            return None
        return contract if contract.dynamic_task_enabled else None

    @staticmethod
    def _tool_snapshot(
        tool: Tool, agent_id: str, contract: ToolReliabilityContract
    ) -> CapabilitySnapshot:
        """生成不含凭据、可重放但不可用于绕过授权的工具快照。"""

        views = project_tool_capability(tool, contract)
        payload = {
            "capability_type": "tool",
            "capability_id": tool.id,
            "tenant_id": tool.tenant_id,
            "name": tool.name,
            "contract": _tool_contract_payload(contract),
            "model_view": views.model,
            "user_view": views.user,
            "audit_view": views.audit,
        }
        checksum = capability_checksum(payload)
        published_checksum = getattr(tool, "reliability_checksum", None)
        if published_checksum and published_checksum != checksum:
            raise CapabilityAccessDenied("CAPABILITY_PUBLISHED_CHECKSUM_INVALID")
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=checksum,
        )

    def _visible_general_skills(
        self, tenant_id: str, agent_id: str
    ) -> list[GeneralSkill]:
        """从当前 agent binding 或整体开放广场边界读取通用技能。"""

        agent = get_agent(self.db, tenant_id, agent_id)
        if agent is None:
            return []
        if agent.is_overall:
            rows = self.db.exec(
                select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id)
            ).all()
            return [
                row
                for row in rows
                if is_open_gallery_resource(self.db, tenant_id, "general_skill", row)
            ]
        bindings = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).all()
        rows: list[GeneralSkill] = []
        for binding in bindings:
            row = self.db.get(GeneralSkill, binding.resource_id)
            if (
                row is not None
                and row.tenant_id == tenant_id
                and is_bound_resource_visible_for_agent(
                    self.db, tenant_id, "general_skill", row, binding
                )
            ):
                rows.append(row)
        return rows

    @staticmethod
    def _valid_general_skill_publication(row: GeneralSkill) -> bool:
        """检查快照 checksum 既匹配快照本身，也匹配当前可发布内容。"""

        snapshot = row.planning_guidance_json
        return bool(
            isinstance(snapshot, dict)
            and snapshot
            and row.planning_guidance_checksum
            and capability_checksum(snapshot) == row.planning_guidance_checksum
            and capability_checksum(
                DynamicCapabilityCatalog._general_skill_publication_payload(row)
            )
            == row.planning_guidance_checksum
        )

    @staticmethod
    def _general_skill_publication_payload(row: GeneralSkill) -> dict[str, object]:
        """从当前技能行重建规范发布载荷，用于发现绕过 API 的内容漂移。"""

        return {
            "schema_version": "1",
            "id": row.id,
            "tenant_id": row.tenant_id,
            "slug": row.slug,
            "name": row.name,
            "description": row.description,
            "usage_mode": row.usage_mode,
            "skill_markdown": row.skill_markdown,
            "skill_files": list(row.skill_files_json or []),
            "permissions": dict(row.permissions_json or {}),
            "runtime_config": dict(row.runtime_config_json or {}),
        }

    @staticmethod
    def _general_skill_snapshot(
        row: GeneralSkill, agent_id: str
    ) -> CapabilitySnapshot:
        """把已发布通用技能投影为规划可用但不承载持续授权的快照。"""

        frozen = dict(row.planning_guidance_json or {})
        usage_mode = str(frozen.get("usage_mode") or row.usage_mode)
        model_view: dict[str, Any] = {
            "id": row.id,
            "slug": frozen.get("slug", row.slug),
            "name": frozen.get("name", row.name),
            "description": frozen.get("description"),
            "usage_mode": usage_mode,
        }
        if usage_mode == "planning_guidance":
            model_view["skill_markdown"] = frozen.get("skill_markdown", "")
            model_view["resources"] = [
                {
                    key: item[key]
                    for key in ("path", "size", "mime_type")
                    if key in item
                }
                for item in frozen.get("skill_files", [])
                if isinstance(item, dict)
            ]
        payload = {
            "capability_type": "general_skill",
            "capability_id": row.id,
            "tenant_id": row.tenant_id,
            "name": row.slug,
            "contract": {"usage_mode": usage_mode},
            "model_view": model_view,
            "user_view": {
                "id": row.id,
                "slug": frozen.get("slug", row.slug),
                "name": frozen.get("name", row.name),
                "description": frozen.get("description"),
                "usage_mode": usage_mode,
            },
            "audit_view": frozen,
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=capability_checksum(payload),
        )


def _schema_projection(
    input_schema: Mapping[str, Any], output_schema: Mapping[str, Any], paths: Sequence[str]
) -> dict[str, Any]:
    """按 input/output 精确属性路径投影 JSON Schema，嵌套白名单不扩张同级字段。"""

    projected: dict[str, Any] = {}
    for root, schema in (("input", input_schema), ("output", output_schema)):
        relative_paths = [
            path.split(".")[1:] for path in paths if path.startswith(root + ".")
        ]
        projected[f"{root}_schema"] = _project_schema_paths(schema, relative_paths)
    return projected


def schema_path_exists(schema: Mapping[str, Any], relative_path: Sequence[str]) -> bool:
    """验证白名单路径在 JSON Schema properties 中逐层存在。"""

    current: object = schema
    for part in relative_path:
        if not isinstance(current, Mapping):
            return False
        properties = current.get("properties")
        if not isinstance(properties, Mapping) or part not in properties:
            return False
        current = properties[part]
    return bool(relative_path)


def _project_schema_paths(
    schema: Mapping[str, Any], relative_paths: Sequence[Sequence[str]]
) -> dict[str, Any]:
    """递归保留精确命中属性，并同步收窄 required 而不携带默认/示例值。"""

    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    grouped: dict[str, list[Sequence[str]]] = {}
    for path in relative_paths:
        if path:
            grouped.setdefault(path[0], []).append(path[1:])
    selected: dict[str, object] = {}
    for name, tails in grouped.items():
        if not isinstance(properties, Mapping) or name not in properties:
            continue
        source = properties[name]
        if any(not tail for tail in tails):
            selected[name] = _safe_schema_node(source)
            continue
        if isinstance(source, Mapping):
            projected_child = _project_schema_paths(source, tails)
            safe_source = _safe_schema_node(source)
            base = {
                key: value
                for key, value in (
                    safe_source.items() if isinstance(safe_source, Mapping) else ()
                )
                if key not in {"properties", "required"}
            }
            selected[name] = {**base, **projected_child}
    result: dict[str, Any] = {"type": "object", "properties": selected}
    required = schema.get("required") if isinstance(schema, Mapping) else None
    if isinstance(required, list):
        result["required"] = [name for name in required if name in selected]
    return result


def _safe_schema_node(value: object) -> object:
    """只保留模型需要的 schema 结构关键字，移除 default/example/const 等可夹带凭据的值。"""

    if isinstance(value, Mapping):
        allowed = {
            "type",
            "description",
            "enum",
            "format",
            "items",
            "properties",
            "required",
            "nullable",
            "additionalProperties",
            "oneOf",
            "anyOf",
            "allOf",
        }
        return {
            key: _safe_schema_node(item)
            for key, item in value.items()
            if key in allowed
        }
    if isinstance(value, list):
        return [_safe_schema_node(item) for item in value]
    return value


def _strict_json(value: object, *, path: str = "$") -> object:
    """递归验证仅含 RFC8259 类型，禁止 NaN/Infinity、非字符串键和集合。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 包含非有限浮点数")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} 包含非字符串键")
            normalized[key] = _strict_json(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_strict_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} 包含不支持的 JSON 类型: {type(value).__name__}")
