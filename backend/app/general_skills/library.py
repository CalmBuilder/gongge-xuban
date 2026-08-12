"""
@Time       : 2026/08/12 11:20
@Author     : zhanglp8181
@File       : library.py
@CallChain  : Skill library API → GeneralSkillLibraryService → Revision/Binding/Audit
@Description: 查询本人 Skill 库并以 checksum、CAS 和幂等账本原子装配多个本人 Agent。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillBindingBatchCommand,
    GeneralSkillRevision,
    ManagementAuditLog,
    User,
    utc_now,
)
from app.general_skills.eligibility import GeneralSkillBindingMetadata
from app.general_skills.governance import bump_general_skill_authorization_revision
from app.general_skills.governance_schema import GeneralSkillBindingRead
from app.general_skills.library_schema import (
    GeneralSkillBindingBatchCommitRead,
    GeneralSkillBindingBatchCommitRequest,
    GeneralSkillBindingBatchPreviewRead,
    GeneralSkillBindingBatchPreviewRequest,
    GeneralSkillBindingBatchTarget,
    GeneralSkillBindingBatchTargetPreview,
    MyGeneralSkillRead,
    MyGeneralSkillAgentRead,
)


class GeneralSkillLibraryError(RuntimeError):
    """表示用户 Skill 库请求违反所有权、并发或幂等契约。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存稳定错误码与 HTTP 建议状态。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GeneralSkillLibraryService:
    """维护本人私有 Skill 的跨 Agent 装配视图和单事务命令。"""

    def __init__(self, db: Session) -> None:
        """绑定请求级数据库事务。"""

        self.db = db

    def list_owned(self, current_user: User) -> list[MyGeneralSkillRead]:
        """仅列出本人拥有且具有当前不可变修订的 Skill 及本人 Agent 绑定。"""

        skills = list(
            self.db.exec(
                select(GeneralSkill)
                .where(
                    GeneralSkill.tenant_id == current_user.tenant_id,
                    GeneralSkill.owner_user_id == current_user.id,
                    GeneralSkill.visibility_scope == "user_private",
                    GeneralSkill.current_published_revision_id.is_not(None),
                )
                .order_by(GeneralSkill.updated_at.desc())
            ).all()
        )
        if not skills:
            return []
        agents = {
            row.id: row
            for row in self.db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == current_user.tenant_id,
                    AgentProfile.owner_user_id == current_user.id,
                    AgentProfile.is_overall.is_(False),
                )
            ).all()
        }
        bindings = list(
            self.db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == current_user.tenant_id,
                    AgentResourceBinding.resource_type == "general_skill",
                    AgentResourceBinding.resource_id.in_([row.id for row in skills]),
                    AgentResourceBinding.agent_id.in_(list(agents) or ["__none__"]),
                    AgentResourceBinding.status != "deleted",
                )
            ).all()
        )
        bindings_by_skill: dict[str, list[GeneralSkillBindingRead]] = {}
        for binding in bindings:
            bindings_by_skill.setdefault(binding.resource_id, []).append(_binding_read(binding))
        result: list[MyGeneralSkillRead] = []
        for skill in skills:
            revision = self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
            if revision is None or revision.skill_id != skill.id:
                continue
            result.append(
                MyGeneralSkillRead(
                    id=skill.id,
                    name=skill.name,
                    slug=skill.slug,
                    description=skill.description,
                    visibility_scope=skill.visibility_scope,
                    status=skill.status,
                    current_revision_id=revision.id,
                    current_revision_number=revision.revision_number,
                    content_checksum=revision.content_checksum,
                    manifest_checksum=revision.manifest_checksum,
                    row_version=skill.row_version,
                    source_kind=str(revision.source_snapshot_json.get("source_kind") or "") or None,
                    bindings=bindings_by_skill.get(skill.id, []),
                )
            )
        return result

    def list_owned_agents(self, current_user: User) -> list[MyGeneralSkillAgentRead]:
        """从与事务校验相同的正式 owner 字段列出可装配目标。"""

        rows = self.db.exec(
            select(AgentProfile)
            .where(
                AgentProfile.tenant_id == current_user.tenant_id,
                AgentProfile.owner_user_id == current_user.id,
                AgentProfile.is_overall.is_(False),
                AgentProfile.status == "active",
            )
            .order_by(AgentProfile.name, AgentProfile.id)
        ).all()
        return [
            MyGeneralSkillAgentRead(
                id=row.id,
                name=row.name,
                status=row.status,
                profile_revision=row.profile_revision,
            )
            for row in rows
        ]

    def preview(
        self,
        request: GeneralSkillBindingBatchPreviewRequest,
        current_user: User,
    ) -> GeneralSkillBindingBatchPreviewRead:
        """验证全部目标并冻结提交所需的数据库事实，不产生绑定或审计写入。"""

        skill, revision = self._owned_skill_revision(request.skill_id, current_user)
        normalized = self._normalized_targets(request.targets)
        rows, facts = self._target_facts(
            skill, revision, normalized, current_user, enforce_expected_versions=False
        )
        return GeneralSkillBindingBatchPreviewRead(
            skill_id=skill.id,
            revision_id=revision.id,
            preview_checksum=_checksum(facts),
            targets=rows,
        )

    def commit(
        self,
        request: GeneralSkillBindingBatchCommitRequest,
        *,
        idempotency_key: str,
        current_user: User,
    ) -> GeneralSkillBindingBatchCommitRead:
        """重算 preview 后在单事务中创建/更新全部绑定并保存可重放结果。"""

        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise GeneralSkillLibraryError("GENERAL_SKILL_IDEMPOTENCY_INVALID", "invalid key", 400)
        normalized = self._normalized_targets(request.targets)
        request_checksum = _checksum(
            {"skill_id": request.skill_id, "targets": [row.model_dump() for row in normalized]}
        )
        existing = self.db.exec(
            select(GeneralSkillBindingBatchCommand).where(
                GeneralSkillBindingBatchCommand.tenant_id == current_user.tenant_id,
                GeneralSkillBindingBatchCommand.owner_user_id == current_user.id,
                GeneralSkillBindingBatchCommand.idempotency_key == key,
            )
        ).first()
        if existing is not None:
            if existing.request_checksum != request_checksum:
                raise GeneralSkillLibraryError(
                    "GENERAL_SKILL_IDEMPOTENCY_CONFLICT", "key already used for another request"
                )
            return self._command_read(existing, replayed=True)
        skill, revision = self._owned_skill_revision(request.skill_id, current_user)
        preview_rows, facts = self._target_facts(
            skill, revision, normalized, current_user, enforce_expected_versions=True
        )
        if request.preview_checksum != _checksum(facts):
            raise GeneralSkillLibraryError("GENERAL_SKILL_PREVIEW_STALE", "binding preview is stale")
        bindings: list[AgentResourceBinding] = []
        for target, preview_row in zip(normalized, preview_rows, strict=True):
            binding = (
                self.db.get(AgentResourceBinding, preview_row.current_binding_id)
                if preview_row.current_binding_id
                else None
            )
            metadata = GeneralSkillBindingMetadata(
                revision_policy=target.revision_policy,
                pinned_revision_id=revision.id if target.revision_policy == "pinned" else None,
                invocation_policy=target.invocation_policy,
                created_by_user_id=current_user.id,
            )
            if binding is None:
                binding = AgentResourceBinding(
                    tenant_id=current_user.tenant_id,
                    agent_id=target.agent_id,
                    resource_type="general_skill",
                    resource_id=skill.id,
                    status=target.status,
                    metadata_json=metadata.model_dump(mode="json"),
                )
            elif preview_row.action != "unchanged":
                binding.status = target.status
                binding.metadata_json = metadata.model_dump(mode="json")
                binding.row_version += 1
                binding.updated_at = utc_now()
            self.db.add(binding)
            self.db.flush()
            bindings.append(binding)
            self.db.add(
                ManagementAuditLog(
                    tenant_id=current_user.tenant_id,
                    actor_user_id=current_user.id,
                    action="general_skill.binding.batch_applied",
                    action_kind="update" if preview_row.current_binding_id else "create",
                    outcome="success",
                    resource_type="general_skill_binding",
                    resource_id=binding.id,
                    source_type="user_private_skill_library",
                    detail_json={"skill_id": skill.id, "agent_id": target.agent_id},
                )
            )
        bump_general_skill_authorization_revision(
            self.db,
            current_user.tenant_id,
            event_type="binding_batch_applied",
            resource_id=skill.id,
            payload={"agent_ids": [row.agent_id for row in normalized], "revision_id": revision.id},
        )
        result = {"bindings": [_binding_read(row).model_dump() for row in bindings]}
        command = GeneralSkillBindingBatchCommand(
            tenant_id=current_user.tenant_id,
            owner_user_id=current_user.id,
            idempotency_key=key,
            skill_id=skill.id,
            revision_id=revision.id,
            preview_checksum=request.preview_checksum,
            request_checksum=request_checksum,
            result_json=result,
        )
        self.db.add(command)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise GeneralSkillLibraryError(
                "GENERAL_SKILL_STATE_CONFLICT", "binding batch changed concurrently"
            ) from exc
        self.db.refresh(command)
        return self._command_read(command, replayed=False)

    def _owned_skill_revision(
        self, skill_id: str, current_user: User
    ) -> tuple[GeneralSkill, GeneralSkillRevision]:
        """返回本人私有 Skill 与当前可绑定修订。"""

        skill = self.db.get(GeneralSkill, skill_id)
        if (
            skill is None
            or skill.tenant_id != current_user.tenant_id
            or skill.owner_user_id != current_user.id
            or skill.visibility_scope != "user_private"
        ):
            raise GeneralSkillLibraryError("GENERAL_SKILL_NOT_AVAILABLE", "skill unavailable", 404)
        revision = self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
        if revision is None or revision.skill_id != skill.id or revision.status != "published":
            raise GeneralSkillLibraryError("GENERAL_SKILL_REVISION_CONFLICT", "revision unavailable")
        return skill, revision

    def _normalized_targets(
        self, targets: list[GeneralSkillBindingBatchTarget]
    ) -> list[GeneralSkillBindingBatchTarget]:
        """拒绝重复 Agent 并按稳定 ID 排序形成 checksum 输入。"""

        if len({row.agent_id for row in targets}) != len(targets):
            raise GeneralSkillLibraryError(
                "GENERAL_SKILL_BATCH_TARGET_DUPLICATE", "duplicate target agent", 400
            )
        return sorted(targets, key=lambda row: row.agent_id)

    def _target_facts(
        self,
        skill: GeneralSkill,
        revision: GeneralSkillRevision,
        targets: list[GeneralSkillBindingBatchTarget],
        current_user: User,
        *,
        enforce_expected_versions: bool,
    ) -> tuple[list[GeneralSkillBindingBatchTargetPreview], dict[str, object]]:
        """校验目标所有权/CAS 并返回动作和防 stale 权威事实。"""

        previews: list[GeneralSkillBindingBatchTargetPreview] = []
        fact_targets: list[dict[str, object]] = []
        for target in targets:
            agent = self.db.get(AgentProfile, target.agent_id)
            if (
                agent is None
                or agent.tenant_id != current_user.tenant_id
                or agent.is_overall
                or agent_owner_user_id(agent) != current_user.id
            ):
                raise GeneralSkillLibraryError(
                    "GENERAL_SKILL_FORBIDDEN", "target agent is not manageable", 403
                )
            binding = self.db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == current_user.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "general_skill",
                    AgentResourceBinding.resource_id == skill.id,
                )
            ).first()
            if enforce_expected_versions and target.expected_binding_row_version != (
                binding.row_version if binding else None
            ):
                raise GeneralSkillLibraryError(
                    "GENERAL_SKILL_STATE_CONFLICT", "binding version does not match"
                )
            desired_metadata = GeneralSkillBindingMetadata(
                revision_policy=target.revision_policy,
                pinned_revision_id=revision.id if target.revision_policy == "pinned" else None,
                invocation_policy=target.invocation_policy,
                created_by_user_id=current_user.id,
            ).model_dump(mode="json")
            action = "create"
            if binding is not None:
                action = (
                    "unchanged"
                    if binding.status == target.status and binding.metadata_json == desired_metadata
                    else "update"
                )
            previews.append(
                GeneralSkillBindingBatchTargetPreview(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    action=action,
                    current_binding_id=binding.id if binding else None,
                    current_binding_row_version=binding.row_version if binding else None,
                    eligible=True,
                )
            )
            fact_targets.append(
                {
                    "target": target.model_dump(exclude={"expected_binding_row_version"}),
                    "agent_profile_revision": agent.profile_revision,
                    "binding_id": binding.id if binding else None,
                    "binding_row_version": binding.row_version if binding else None,
                    "binding_status": binding.status if binding else None,
                    "binding_metadata": binding.metadata_json if binding else None,
                }
            )
        return previews, {
            "skill_id": skill.id,
            "skill_row_version": skill.row_version,
            "revision_id": revision.id,
            "revision_row_version": revision.row_version,
            "targets": fact_targets,
        }

    def _command_read(
        self, command: GeneralSkillBindingBatchCommand, *, replayed: bool
    ) -> GeneralSkillBindingBatchCommitRead:
        """从幂等账本投影稳定响应。"""

        return GeneralSkillBindingBatchCommitRead(
            command_id=command.id,
            replayed=replayed,
            bindings=[GeneralSkillBindingRead.model_validate(row) for row in command.result_json["bindings"]],
        )


def _checksum(value: object) -> str:
    """对排序 JSON 生成批量事务使用的稳定 SHA-256。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _binding_read(row: AgentResourceBinding) -> GeneralSkillBindingRead:
    """把绑定及严格 metadata 投影为公共治理响应。"""

    metadata = GeneralSkillBindingMetadata.model_validate(row.metadata_json)
    return GeneralSkillBindingRead(
        id=row.id,
        agent_id=row.agent_id,
        skill_id=row.resource_id,
        status=row.status,
        revision_policy=metadata.revision_policy,
        pinned_revision_id=metadata.pinned_revision_id,
        invocation_policy=metadata.invocation_policy,
        row_version=row.row_version,
    )
