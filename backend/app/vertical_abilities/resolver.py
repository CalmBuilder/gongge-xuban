"""
@Time       : 2026/09/02 10:00
@Author     : zhanglp8181
@File       : resolver.py
@CallChain  : ChatTurn ability_ids → VerticalAbilityResolver → Execution 快照/Planner
@Description: 将平台内置垂直能力包解析为带租户执行上下文的工具和 Guidance 快照。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.vertical_abilities.catalog import (
    VerticalAbilityCatalog,
    VerticalAbilityCapability,
    VerticalAbilityDefinition,
)


VERTICAL_GUIDANCE_ID_PREFIX = "vertical_ability:"


@dataclass(frozen=True, slots=True)
class VerticalAbilityResolution:
    """保存本轮选中的能力包、工具快照、指导正文和组合身份。"""

    definitions: tuple[VerticalAbilityDefinition, ...]
    capabilities: tuple[CapabilitySnapshot, ...]
    loaded_guidance: tuple[dict[str, object], ...]
    snapshot: dict[str, object]

    @property
    def ability_ids(self) -> tuple[str, ...]:
        """返回按用户选择顺序去重后的能力包 ID。"""

        return tuple(str(value) for value in self.snapshot.get("ability_ids", ()))

    @property
    def guidance_use_ids(self) -> frozenset[str]:
        """返回本轮合成 Guidance 使用的虚拟稳定身份。"""

        return frozenset(
            str(value)
            for item in self.loaded_guidance
            for value in item.get("skill_use_ids", ())
            if str(value).strip()
        )


class VerticalAbilityResolver:
    """把公开能力包定义编译为当前 Execution 可冻结的能力事实。"""

    def __init__(self, catalog: VerticalAbilityCatalog | None = None) -> None:
        """绑定项目内置目录；解析器不读取用户、租户或数字员工绑定表。"""

        self.catalog = catalog or VerticalAbilityCatalog()

    def resolve(
        self,
        ability_ids: Sequence[str],
        *,
        tenant_id: str,
        agent_id: str,
    ) -> VerticalAbilityResolution:
        """解析本轮能力包并为每个适配器生成带执行范围的冻结快照。"""

        definitions = self.catalog.resolve_many(tuple(ability_ids))
        capabilities: list[CapabilitySnapshot] = []
        loaded_guidance: list[dict[str, object]] = []
        seen_capability_names: set[str] = set()
        for definition in definitions:
            loaded_guidance.append(self._guidance(definition))
            for capability in definition.capabilities:
                if capability.name in seen_capability_names:
                    continue
                seen_capability_names.add(capability.name)
                capabilities.append(
                    self._capability_snapshot(
                        definition,
                        capability.name,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                    )
                )
        ability_rows = [
            {
                "id": definition.id,
                "name": definition.name,
                "version": definition.version,
                "checksum": definition.checksum,
                "capability_names": [item.name for item in definition.capabilities],
            }
            for definition in definitions
        ]
        snapshot = {
            "ability_ids": [definition.id for definition in definitions],
            "vertical_abilities": ability_rows,
            "vertical_capability_names": [item.name for item in capabilities],
            "vertical_guidance": loaded_guidance,
            "vertical_output_contracts": [
                {
                    "ability_id": definition.id,
                    "name": definition.name,
                    "contract": dict(definition.output_contract),
                }
                for definition in definitions
            ],
            "composition_checksum": capability_checksum(
                {
                    "abilities": ability_rows,
                }
            ),
        }
        return VerticalAbilityResolution(
            definitions=definitions,
            capabilities=tuple(capabilities),
            loaded_guidance=tuple(loaded_guidance),
            snapshot=snapshot,
        )

    def capability_snapshot(
        self,
        *,
        ability_id: str,
        capability_name: str,
        tenant_id: str,
        agent_id: str,
    ) -> CapabilitySnapshot:
        """按能力包身份重建一个快照，供读操作实时校验项目版本是否漂移。"""

        definition = self.catalog.resolve(ability_id)
        return self._capability_snapshot(
            definition,
            capability_name,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    def compatible_capability(self, snapshot: CapabilitySnapshot) -> VerticalAbilityCapability:
        """验证历史快照完整性和当前适配器合同，包版本/说明变化不撤销旧任务。"""

        marker = snapshot.audit_view.get("vertical_ability")
        payload = snapshot.model_dump(mode="json", exclude={"agent_id", "checksum"})
        if (
            not isinstance(marker, Mapping)
            or snapshot.capability_type != "tool"
            or snapshot.contract.get("risk_class") != "read"
            or capability_checksum(payload) != snapshot.checksum
        ):
            raise ValueError("VERTICAL_ABILITY_REVISION_CHANGED")
        definition = self.catalog.resolve(str(marker.get("id") or ""))
        capability = next(
            (item for item in definition.capabilities if item.name == snapshot.name), None
        )
        if capability is None:
            raise KeyError("VERTICAL_ABILITY_CAPABILITY_NOT_FOUND")
        if (
            capability.contract != snapshot.contract
            or capability.adapter != snapshot.audit_view.get("adapter")
            or capability.input_schema != snapshot.model_view.get("input_schema")
            or capability.output_schema != snapshot.model_view.get("output_schema")
        ):
            raise ValueError("VERTICAL_ABILITY_REVISION_CHANGED")
        return capability

    def restore(
        self,
        frozen: Mapping[str, object],
        *,
        checksum: str,
        tenant_id: str,
        agent_id: str,
    ) -> VerticalAbilityResolution:
        """从服务端父 Execution 恢复能力、指导和输出合同，不按 ID 静默升级。"""

        if not checksum or capability_checksum(dict(frozen)) != checksum:
            raise ValueError("VERTICAL_ABILITY_SNAPSHOT_INVALID")
        rows = frozen.get("vertical_abilities")
        guidance = frozen.get("vertical_guidance")
        tools = frozen.get("tools")
        ids = frozen.get("ability_ids")
        if not all(isinstance(value, list) for value in (rows, guidance, tools, ids)):
            raise ValueError("VERTICAL_ABILITY_SNAPSHOT_INVALID")
        identities = {
            str(row.get("id")): row for row in rows if isinstance(row, Mapping)
        }
        if set(ids) != set(identities) or len(ids) != len(identities):
            raise ValueError("VERTICAL_ABILITY_SNAPSHOT_INVALID")
        capabilities: list[CapabilitySnapshot] = []
        for raw in tools:
            if not isinstance(raw, Mapping):
                continue
            audit = raw.get("audit_view")
            if not isinstance(audit, Mapping) or not isinstance(audit.get("vertical_ability"), Mapping):
                continue
            snapshot = CapabilitySnapshot.model_validate(raw)
            marker = snapshot.audit_view["vertical_ability"]
            row = identities.get(marker.get("id"))
            if (
                snapshot.tenant_id != tenant_id or snapshot.agent_id != agent_id
                or row is None
                or any(row.get(key) != marker.get(key) for key in ("id", "version", "checksum"))
            ):
                raise ValueError("VERTICAL_ABILITY_SNAPSHOT_INVALID")
            self.compatible_capability(snapshot)
            capabilities.append(snapshot)
        if (
            not capabilities
            or {item.get("ability_id") for item in guidance if isinstance(item, Mapping)} != set(ids)
        ):
            raise ValueError("VERTICAL_ABILITY_SNAPSHOT_INVALID")
        keys = (
            "ability_ids", "vertical_abilities", "vertical_capability_names", "vertical_guidance",
            "vertical_output_contracts", "composition_checksum",
        )
        restored = deepcopy({key: frozen[key] for key in keys if key in frozen})
        return VerticalAbilityResolution(
            definitions=(), capabilities=tuple(capabilities),
            loaded_guidance=tuple(deepcopy(guidance)), snapshot=restored,
        )

    def find_definition_for_capability(
        self,
        capability_name: str,
    ) -> VerticalAbilityDefinition | None:
        """按适配器能力名查找内置包，供执行器判断名称保留边界。"""

        normalized = str(capability_name or "").strip()
        for definition in self.catalog_definitions():
            if any(item.name == normalized for item in definition.capabilities):
                return definition
        return None

    def catalog_definitions(self) -> tuple[VerticalAbilityDefinition, ...]:
        """返回目录定义的只读视图，避免执行器依赖目录私有字段。"""

        return self.catalog.definitions()

    @staticmethod
    def _guidance(definition: VerticalAbilityDefinition) -> dict[str, object]:
        """把能力包操作规程编译为 Planner 可校验的单一 Guidance 块。"""

        guidance_id = f"{VERTICAL_GUIDANCE_ID_PREFIX}{definition.id}"
        return {
            "name": guidance_id,
            "ability_id": definition.id,
            "description": definition.description,
            "output_contract": dict(definition.output_contract),
            "skill_use_ids": [guidance_id],
            "selection_mode": "forced",
            "skills": [
                {
                    "name": definition.name,
                    "instructions": definition.instructions,
                    "reviewed_resources": [],
                    "selection_mode": "forced",
                }
            ],
        }

    @staticmethod
    def _capability_snapshot(
        definition: VerticalAbilityDefinition,
        capability_name: str,
        *,
        tenant_id: str,
        agent_id: str,
    ) -> CapabilitySnapshot:
        """根据内置定义生成单个只读工具快照，checksum 不承载持续授权。"""

        capability = next(
            (item for item in definition.capabilities if item.name == capability_name),
            None,
        )
        if capability is None:
            raise KeyError("VERTICAL_ABILITY_CAPABILITY_NOT_FOUND")
        model_view = {
            "name": capability.name,
            "description": capability.description,
            "input_schema": capability.input_schema,
            "output_schema": capability.output_schema,
            "risk_class": "read",
            "confirmation_policy": "none",
        }
        payload: dict[str, Any] = {
            "capability_type": "tool",
            "capability_id": capability.name,
            "tenant_id": tenant_id,
            "name": capability.name,
            "contract": capability.contract,
            "model_view": model_view,
            "user_view": {
                "name": capability.name,
                "display_name": capability.name,
                "description": capability.description,
                "risk_class": "read",
            },
            "audit_view": {
                "vertical_ability": {
                    "id": definition.id,
                    "name": definition.name,
                    "version": definition.version,
                    "checksum": definition.checksum,
                },
                "adapter": capability.adapter,
                "source_repository": definition.source_metadata.get("repository", ""),
                "source_revision": definition.source_metadata.get("revision", ""),
                "source_license": definition.source_metadata.get("license", ""),
            },
        }
        return CapabilitySnapshot(
            **payload,
            agent_id=agent_id,
            checksum=capability_checksum(payload),
        )


def is_vertical_capability_snapshot(snapshot: CapabilitySnapshot) -> bool:
    """判断能力快照是否明确来自平台内置垂直能力包。"""

    return isinstance(snapshot.audit_view.get("vertical_ability"), Mapping)
