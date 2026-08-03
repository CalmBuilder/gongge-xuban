"""
@Time       : 2026/08/03 23:42
@Author     : zhanglp8181
@File       : capability_catalog.py
@CallChain  : Tool/GeneralSkill publish → capability catalog → DynamicTaskAgent dispatch
@Description: 校验动态能力可靠性契约，生成脱敏快照并在执行前重新授权。
"""

from __future__ import annotations

import hashlib
import json
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
from app.db.models import AgentResourceBinding, GeneralSkill, ModelConfig, Tool, utc_now
from app.organization.agent_execution import AgentExecutionAuthorizer, AgentExecutionDenied


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
        return self


class CapabilityViews(BaseModel):
    """保存同一能力的模型、用户和审计投影。"""

    model: dict[str, Any]
    user: dict[str, Any]
    audit: dict[str, Any]


class CapabilitySnapshot(BaseModel):
    """保存计划/操作引用的不可变能力事实，不承载持续授权。"""

    capability_type: Literal["tool", "general_skill", "knowledge"]
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
        "contract": contract.model_dump(mode="json"),
        "input_schema": dict(tool.input_schema or {}),
        "output_schema": dict(tool.output_schema or {}),
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
    tool.reliability_contract_json = contract.model_dump(mode="json")
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

    def list_tools(self, tenant_id: str, agent_id: str) -> list[CapabilitySnapshot]:
        """仅返回已绑定、已启用且显式发布为动态可用的工具。"""

        snapshots: list[CapabilitySnapshot] = []
        for tool in visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False):
            contract = self._published_tool_contract(tool)
            if contract is None or contract.risk_class != "read":
                continue
            snapshots.append(self._tool_snapshot(tool, agent_id, contract))
        return snapshots

    def resolve_tool(
        self, tenant_id: str, agent_id: str, operation_name: str
    ) -> CapabilitySnapshot:
        """按当前数字员工可见边界解析工具，未发布契约时确定性拒绝。"""

        for snapshot in self.list_tools(tenant_id, agent_id):
            if snapshot.name == operation_name:
                return snapshot
        raise CapabilityAccessDenied("CAPABILITY_NOT_AVAILABLE")

    def list_general_skills(
        self, tenant_id: str, agent_id: str
    ) -> list[CapabilitySnapshot]:
        """仅返回当前数字员工可见、已发布且 checksum 与快照一致的通用技能。"""

        return [
            self._general_skill_snapshot(row, agent_id)
            for row in self._visible_general_skills(tenant_id, agent_id)
            if row.status == "published" and self._valid_general_skill_publication(row)
        ]

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
    ) -> Tool:
        """忽略快照的历史授权含义，重查实时启用、绑定、契约和组织权限。"""

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
            "contract": contract.model_dump(mode="json"),
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
