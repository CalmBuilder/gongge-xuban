"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : eligibility.py
@CallChain  : API/Agent Loop/DynamicTask → EffectiveGeneralSkillResolver → DB authorization facts
@Description: 统一解析用户、数字员工、绑定策略与不可变修订交集内的 Skill 权威目录。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillAuthorizationState,
    GeneralSkillRevision,
    User,
)
from app.security.permissions import can_use_agent_in_chat


def _checksum(value: object) -> str:
    """以规范 JSON 生成可跨进程比较的目录或内容校验和。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class GeneralSkillBindingMetadata(BaseModel):
    """校验 general_skill 绑定的版本策略和调用策略，拒绝自由 metadata。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    revision_policy: str = Field(pattern=r"^(pinned|follow_latest)$")
    pinned_revision_id: str | None = None
    invocation_policy: str = Field(pattern=r"^(model_allowed|user_only)$")
    atomic_execution_allowed: bool = False
    created_by_user_id: str


@dataclass(frozen=True, slots=True)
class EffectiveGeneralSkill:
    """表示经过完整身份、绑定和 revision 校验的单个目录项。"""

    binding_id: str
    skill_id: str
    revision_id: str
    revision_number: int
    content_checksum: str
    manifest_checksum: str
    name: str
    description: str
    usage_mode: str
    invocation_policy: str
    revision_policy: str


@dataclass(frozen=True, slots=True)
class EffectiveGeneralSkillCatalog:
    """返回权威 eligibility 集合、单调授权 revision 和稳定 parity hash。"""

    tenant_id: str
    user_id: str
    agent_id: str
    authorization_revision: int
    eligibility_hash: str
    items: tuple[EffectiveGeneralSkill, ...]


class EffectiveGeneralSkillResolver:
    """从数据库实时求交 Skill 授权事实，不以进程缓存作为撤权保证。"""

    def __init__(self, db: Session):
        """绑定当前数据库事务会话。"""

        self.db = db

    def resolve(self, current_user: User, agent_id: str) -> EffectiveGeneralSkillCatalog:
        """解析当前用户可使用 Agent 上全部有效且版本唯一的 GeneralSkill。"""

        agent = self.db.get(AgentProfile, agent_id)
        if agent is None or not can_use_agent_in_chat(self.db, agent, current_user):
            return self._catalog(current_user, agent_id, [])
        bindings = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == current_user.tenant_id,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).all()
        items: list[EffectiveGeneralSkill] = []
        for binding in bindings:
            resolved = self._resolve_binding(current_user, binding)
            if resolved is not None:
                items.append(resolved)
        items.sort(key=lambda item: (item.name.casefold(), item.skill_id, item.revision_id))
        return self._catalog(current_user, agent_id, items)

    def _resolve_binding(
        self,
        current_user: User,
        binding: AgentResourceBinding,
    ) -> EffectiveGeneralSkill | None:
        """把单条 active binding 解析为固定 revision，任一事实异常即 fail-closed。"""

        skill = self.db.get(GeneralSkill, binding.resource_id)
        if (
            skill is None
            or skill.tenant_id != current_user.tenant_id
            or skill.status != "published"
            or not self._visible_to_user(skill, current_user)
        ):
            return None
        try:
            metadata = GeneralSkillBindingMetadata.model_validate(binding.metadata_json)
        except ValidationError:
            return None
        revision_id = (
            metadata.pinned_revision_id
            if metadata.revision_policy == "pinned"
            else skill.current_published_revision_id
        )
        if not revision_id:
            return None
        revision = self.db.get(GeneralSkillRevision, revision_id)
        allowed_statuses = (
            {"published", "superseded"}
            if metadata.revision_policy == "pinned"
            else {"published"}
        )
        if (
            revision is None
            or revision.tenant_id != skill.tenant_id
            or revision.skill_id != skill.id
            or revision.status not in allowed_statuses
            or not self._revision_checksum_valid(revision)
        ):
            return None
        return EffectiveGeneralSkill(
            binding_id=binding.id,
            skill_id=skill.id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_checksum=revision.content_checksum,
            manifest_checksum=revision.manifest_checksum,
            name=skill.name,
            description=skill.description or "",
            usage_mode=skill.usage_mode,
            invocation_policy=metadata.invocation_policy,
            revision_policy=metadata.revision_policy,
        )

    @staticmethod
    def _visible_to_user(skill: GeneralSkill, current_user: User) -> bool:
        """按稳定所有者与显式可见范围判断，不从名称或来源推断共享。"""

        if skill.owner_user_id == current_user.id:
            return True
        return skill.visibility_scope in {"agent_private", "tenant_gallery"}

    @staticmethod
    def _revision_checksum_valid(revision: GeneralSkillRevision) -> bool:
        """使用不可变资源清单重算内容 checksum；缺清单的 legacy 修订留给迁移判定。"""

        resources = revision.resource_manifest_json
        if not resources:
            return revision.source_snapshot_json.get("source_kind") == "legacy_backfill"
        normalized: list[dict[str, object]] = []
        for resource in resources:
            path = resource.get("path")
            checksum = resource.get("checksum") or resource.get("content_checksum")
            if not isinstance(path, str) or not isinstance(checksum, str):
                return False
            normalized.append({"path": path, "checksum": checksum})
        normalized.sort(key=lambda item: str(item["path"]))
        return _checksum(normalized) == revision.content_checksum

    def _catalog(
        self,
        current_user: User,
        agent_id: str,
        items: list[EffectiveGeneralSkill],
    ) -> EffectiveGeneralSkillCatalog:
        """生成不含正文的稳定目录哈希和当前租户授权 revision。"""

        state = self.db.get(GeneralSkillAuthorizationState, current_user.tenant_id)
        payload = [
            {
                "binding_id": item.binding_id,
                "skill_id": item.skill_id,
                "revision_id": item.revision_id,
                "content_checksum": item.content_checksum,
                "invocation_policy": item.invocation_policy,
                "revision_policy": item.revision_policy,
            }
            for item in items
        ]
        return EffectiveGeneralSkillCatalog(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            agent_id=agent_id,
            authorization_revision=state.revision if state is not None else 0,
            eligibility_hash=_checksum(payload),
            items=tuple(items),
        )
