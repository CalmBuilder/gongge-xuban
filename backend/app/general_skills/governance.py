"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : governance.py
@CallChain  : 管理 API → GeneralSkillGovernanceService → Binding/AuthorizationState/Audit
@Description: 以乐观锁更新 Skill 绑定策略并单调推进跨实例授权 revision。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import update
from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillAuthorizationEvent,
    GeneralSkillAuthorizationState,
    GeneralSkillRevision,
    User,
    utc_now,
)
from app.general_skills.eligibility import GeneralSkillBindingMetadata
from app.general_skills.lifecycle import RevisionStatus


class GeneralSkillGovernanceError(Exception):
    """表示绑定治理请求违反身份、状态或并发契约。"""

    def __init__(self, error_code: str, message: str, status_code: int = 409):
        """保存稳定错误码、脱敏消息和建议 HTTP 状态。"""

        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def bump_general_skill_authorization_revision(
    db: Session,
    tenant_id: str,
    *,
    event_type: str,
    resource_id: str | None,
    payload: dict[str, object],
) -> int:
    """在调用方事务内锁定租户状态、推进 revision 并追加唯一失效事件。"""

    normalized_event = {
        "event_type": event_type,
        "resource_id": resource_id,
        "payload": payload,
    }
    checksum = hashlib.sha256(
        json.dumps(normalized_event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state = db.exec(
        select(GeneralSkillAuthorizationState)
        .where(GeneralSkillAuthorizationState.tenant_id == tenant_id)
        .with_for_update()
    ).first()
    if state is None:
        revision = 1
        state = GeneralSkillAuthorizationState(
            tenant_id=tenant_id,
            revision=revision,
            last_event_checksum=checksum,
            updated_at=utc_now(),
        )
    else:
        revision = state.revision + 1
        state.revision = revision
        state.last_event_checksum = checksum
        state.updated_at = utc_now()
    db.add(state)
    db.flush()
    db.add(
        GeneralSkillAuthorizationEvent(
            tenant_id=tenant_id,
            authorization_revision=revision,
            event_type=event_type,
            resource_id=resource_id,
            event_checksum=checksum,
            payload_json=normalized_event,
        )
    )
    db.flush()
    return revision


class GeneralSkillGovernanceService:
    """维护 GeneralSkill 与数字员工绑定的版本策略和授权失效事实。"""

    def __init__(self, db: Session):
        """绑定当前数据库事务。"""

        self.db = db

    def update_binding_policy(
        self,
        *,
        current_user: User,
        agent_id: str,
        binding_id: str,
        revision_policy: str,
        pinned_revision_id: str | None,
        expected_row_version: int,
    ) -> AgentResourceBinding:
        """校验所有权与目标 revision 后以 CAS 更新绑定并推进授权 revision。"""

        binding = self._manageable_binding(current_user, agent_id, binding_id)
        current_metadata = GeneralSkillBindingMetadata.model_validate(binding.metadata_json)
        return self.update_binding_configuration(
            current_user=current_user,
            agent_id=agent_id,
            binding_id=binding_id,
            status=binding.status,
            revision_policy=revision_policy,
            pinned_revision_id=pinned_revision_id,
            invocation_policy=current_metadata.invocation_policy,
            expected_row_version=expected_row_version,
        )

    def update_binding_configuration(
        self,
        *,
        current_user: User,
        agent_id: str,
        binding_id: str,
        status: str,
        revision_policy: str,
        pinned_revision_id: str | None,
        invocation_policy: str,
        expected_row_version: int,
    ) -> AgentResourceBinding:
        """单事务更新绑定启停、修订与调用策略，避免复合表单部分成功。"""

        if status not in {"active", "inactive"} or invocation_policy not in {
            "model_allowed",
            "user_only",
        }:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_STATE_CONFLICT", "unsupported binding configuration", 400
            )
        binding = self._manageable_binding(current_user, agent_id, binding_id)
        skill = self._owned_skill(current_user, binding.resource_id)
        revision = self._binding_revision(skill, revision_policy, pinned_revision_id)
        current_metadata = GeneralSkillBindingMetadata.model_validate(binding.metadata_json)
        metadata = current_metadata.model_copy(
            update={
                "revision_policy": revision_policy,
                "pinned_revision_id": revision.id if revision_policy == "pinned" else None,
                "invocation_policy": invocation_policy,
            }
        )
        now = utc_now()
        result = self.db.exec(
            update(AgentResourceBinding)
            .where(
                AgentResourceBinding.id == binding.id,
                AgentResourceBinding.row_version == expected_row_version,
            )
            .values(
                status=status,
                metadata_json=metadata.model_dump(mode="json"),
                row_version=AgentResourceBinding.row_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_STATE_CONFLICT", "binding was changed by another request"
            )
        bump_general_skill_authorization_revision(
            self.db,
            current_user.tenant_id,
            event_type="binding_policy_updated",
            resource_id=binding.id,
            payload={
                "binding_id": binding.id,
                "row_version": expected_row_version + 1,
                "revision_policy": revision_policy,
                "revision_id": revision.id,
                "status": status,
                "invocation_policy": invocation_policy,
            },
        )
        self.db.commit()
        self.db.expire(binding)
        self.db.refresh(binding)
        return binding

    def set_binding_status(
        self,
        *,
        current_user: User,
        agent_id: str,
        binding_id: str,
        status: str,
        expected_row_version: int,
    ) -> AgentResourceBinding:
        """以所有权和 CAS 门禁启用或停用绑定并立即推进授权 revision。"""

        binding = self._manageable_binding(current_user, agent_id, binding_id)
        metadata = GeneralSkillBindingMetadata.model_validate(binding.metadata_json)
        return self.update_binding_configuration(
            current_user=current_user,
            agent_id=agent_id,
            binding_id=binding_id,
            status=status,
            revision_policy=metadata.revision_policy,
            pinned_revision_id=metadata.pinned_revision_id,
            invocation_policy=metadata.invocation_policy,
            expected_row_version=expected_row_version,
        )

    def rollback_skill(
        self,
        *,
        current_user: User,
        skill_id: str,
        target_revision_id: str,
        expected_skill_row_version: int,
        expected_target_row_version: int,
    ) -> GeneralSkillRevision:
        """把已审核旧修订重新设为 current，并使现 current 变为 superseded。"""

        skill = self._owned_skill(current_user, skill_id)
        target = self.db.get(GeneralSkillRevision, target_revision_id)
        if (
            target is None
            or target.tenant_id != skill.tenant_id
            or target.skill_id != skill.id
            or target.status != RevisionStatus.SUPERSEDED.value
            or target.row_version != expected_target_row_version
        ):
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "rollback revision is unavailable"
            )
        current = (
            self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
            if skill.current_published_revision_id
            else None
        )
        if current is None or current.status != RevisionStatus.PUBLISHED.value:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "current published revision is unavailable"
            )
        result = self.db.exec(
            update(GeneralSkill)
            .where(
                GeneralSkill.id == skill.id,
                GeneralSkill.row_version == expected_skill_row_version,
                GeneralSkill.current_published_revision_id == current.id,
            )
            .values(
                current_published_revision_id=target.id,
                status="published",
                row_version=GeneralSkill.row_version + 1,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_STATE_CONFLICT", "skill was changed by another request"
            )
        target.status = RevisionStatus.PUBLISHED.value
        target.row_version += 1
        target.published_at = utc_now()
        target.revoked_at = None
        current.status = RevisionStatus.SUPERSEDED.value
        current.row_version += 1
        self.db.add(target)
        self.db.add(current)
        bump_general_skill_authorization_revision(
            self.db,
            skill.tenant_id,
            event_type="revision_rolled_back",
            resource_id=skill.id,
            payload={"from_revision_id": current.id, "to_revision_id": target.id},
        )
        self.db.commit()
        self.db.refresh(target)
        return target

    def revoke_revision(
        self,
        *,
        current_user: User,
        skill_id: str,
        revision_id: str,
        expected_skill_row_version: int,
        expected_revision_row_version: int,
    ) -> GeneralSkillRevision:
        """软撤销修订；若为 current 则同步归档 Skill 并清空发布指针。"""

        skill = self._owned_skill(current_user, skill_id)
        revision = self.db.get(GeneralSkillRevision, revision_id)
        if (
            revision is None
            or revision.tenant_id != skill.tenant_id
            or revision.skill_id != skill.id
            or revision.status not in {"published", "superseded"}
            or revision.row_version != expected_revision_row_version
        ):
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "revision cannot be revoked"
            )
        if skill.row_version != expected_skill_row_version:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_STATE_CONFLICT", "skill was changed by another request"
            )
        now = utc_now()
        revision_result = self.db.exec(
            update(GeneralSkillRevision)
            .where(
                GeneralSkillRevision.id == revision.id,
                GeneralSkillRevision.row_version == expected_revision_row_version,
                GeneralSkillRevision.status.in_(["published", "superseded"]),
            )
            .values(
                status=RevisionStatus.REVOKED.value,
                row_version=GeneralSkillRevision.row_version + 1,
                revoked_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        skill_values: dict[str, object] = {
            "row_version": GeneralSkill.row_version + 1,
            "updated_at": now,
        }
        skill_conditions = [
            GeneralSkill.id == skill.id,
            GeneralSkill.row_version == expected_skill_row_version,
        ]
        if skill.current_published_revision_id == revision.id:
            skill_conditions.append(GeneralSkill.current_published_revision_id == revision.id)
            skill_values.update(
                {"current_published_revision_id": None, "status": "archived"}
            )
        skill_result = self.db.exec(
            update(GeneralSkill)
            .where(*skill_conditions)
            .values(**skill_values)
            .execution_options(synchronize_session=False)
        )
        if revision_result.rowcount != 1 or skill_result.rowcount != 1:
            self.db.rollback()
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_STATE_CONFLICT", "revision was changed by another request"
            )
        bump_general_skill_authorization_revision(
            self.db,
            skill.tenant_id,
            event_type="revision_revoked",
            resource_id=skill.id,
            payload={"revision_id": revision.id},
        )
        self.db.commit()
        self.db.expire(skill)
        self.db.expire(revision)
        self.db.refresh(revision)
        return revision

    def _manageable_binding(
        self,
        current_user: User,
        agent_id: str,
        binding_id: str,
    ) -> AgentResourceBinding:
        """返回当前主体有权管理且属于指定分身的 Skill 绑定。"""

        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != current_user.tenant_id
            or (current_user.role != "admin" and agent_owner_user_id(agent) != current_user.id)
        ):
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_FORBIDDEN", "current user cannot manage this agent", 403
            )
        binding = self.db.get(AgentResourceBinding, binding_id)
        if (
            binding is None
            or binding.tenant_id != current_user.tenant_id
            or binding.agent_id != agent_id
            or binding.resource_type != "general_skill"
        ):
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_NOT_AVAILABLE", "general skill binding is unavailable", 404
            )
        return binding

    def _owned_skill(self, current_user: User, skill_id: str) -> GeneralSkill:
        """返回本人或管理员可治理的同租户 Skill，隐藏跨租户存在性。"""

        skill = self.db.get(GeneralSkill, skill_id)
        if skill is None or skill.tenant_id != current_user.tenant_id:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_NOT_AVAILABLE", "general skill is unavailable", 404
            )
        if skill.owner_user_id != current_user.id and current_user.role != "admin":
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_FORBIDDEN", "current user does not own this skill", 403
            )
        return skill

    def _binding_revision(
        self,
        skill: GeneralSkill,
        revision_policy: str,
        pinned_revision_id: str | None,
    ) -> GeneralSkillRevision:
        """解析并校验策略目标，follow_latest 不信任客户端传入 revision。"""

        if revision_policy not in {"pinned", "follow_latest"}:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "unsupported revision policy", 400
            )
        revision_id = (
            pinned_revision_id
            if revision_policy == "pinned"
            else skill.current_published_revision_id
        )
        if not revision_id:
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "binding revision is unavailable"
            )
        revision = self.db.get(GeneralSkillRevision, revision_id)
        allowed = {"published", "superseded"} if revision_policy == "pinned" else {"published"}
        if (
            revision is None
            or revision.tenant_id != skill.tenant_id
            or revision.skill_id != skill.id
            or revision.status not in allowed
        ):
            raise GeneralSkillGovernanceError(
                "GENERAL_SKILL_REVISION_CONFLICT", "binding revision is not eligible"
            )
        return revision
