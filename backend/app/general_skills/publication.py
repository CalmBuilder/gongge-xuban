"""
@Time       : 2026/08/13 21:05
@Author     : zhanglp8181
@File       : publication.py
@CallChain  : 发布 API/Attention → PublicationService → snapshot/request/release/adoption
@Description: 管理 Skill 与整 Agent 的冻结发布、职责分离审核、组织广场 Release 和 pinned 主动采用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.db.models import (
    AgentProfile,
    AgentPublicationRevision,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillPublicationRevision,
    GeneralSkillRevision,
    KnowledgeBase,
    PublicationAdoptionCommand,
    PublicationRelease,
    ResourcePublicationRequest,
    SopInstance,
    SopWorkItem,
    SopWorkItemCandidate,
    Skill,
    Tool,
    User,
    new_id,
    utc_now,
)
from app.general_skills.eligibility import GeneralSkillBindingMetadata
from app.general_skills.governance import bump_general_skill_authorization_revision
from app.general_skills.publication_schema import (
    PublicationAdoptRead,
    PublicationReleaseRead,
    PublicationRequestRead,
)
from app.sop_runtime.work_items import SopWorkItemService


class PublicationError(RuntimeError):
    """表示发布所有权、状态、快照、职责分离或采用契约被拒绝。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存稳定错误码和 HTTP 建议状态。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _checksum(value: object) -> str:
    """对冻结快照生成规范 JSON checksum。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PublicationService:
    """以类型化快照隔离私有当前态、审核证据和可发现 Release。"""

    def __init__(self, db: Session) -> None:
        """绑定请求事务。"""

        self.db = db

    def submit(
        self,
        resource_type: str,
        resource_id: str,
        expected_revision: int,
        owner: User,
    ) -> PublicationRequestRead:
        """冻结本人 Skill 或 Agent 当前状态并创建管理员候选 Attention。"""

        if resource_type == "general_skill":
            snapshot, resource_name = self._snapshot_skill(resource_id, expected_revision, owner)
        elif resource_type == "agent":
            snapshot, resource_name = self._snapshot_agent(resource_id, expected_revision, owner)
        else:
            raise PublicationError("PUBLICATION_TYPE_INVALID", "unsupported resource type", 400)
        active = self.db.exec(
            select(ResourcePublicationRequest).where(
                ResourcePublicationRequest.tenant_id == owner.tenant_id,
                ResourcePublicationRequest.resource_type == resource_type,
                ResourcePublicationRequest.resource_id == resource_id,
                ResourcePublicationRequest.active_slot_key == "active",
            )
        ).first()
        if active is not None:
            if active.snapshot_checksum == snapshot["snapshot_checksum"]:
                return self.read_request(active)
            self._mark_request_stale(active, "resource_changed_before_resubmit")
        request = ResourcePublicationRequest(
            tenant_id=owner.tenant_id,
            owner_user_id=owner.id,
            resource_type=resource_type,
            resource_id=resource_id,
            snapshot_kind=resource_type,
            snapshot_id="pending",
            snapshot_checksum=snapshot["snapshot_checksum"],
            submitted_by_user_id=owner.id,
            status="submitted",
        )
        self.db.add(request)
        self.db.flush()
        if resource_type == "general_skill":
            typed = GeneralSkillPublicationRevision(request_id=request.id, **snapshot)
        else:
            typed = AgentPublicationRevision(request_id=request.id, **snapshot)
        self.db.add(typed)
        self.db.flush()
        request.snapshot_id = typed.id
        execution = self._publication_execution(request, owner)
        attention = self._publication_attention(request, execution, owner, resource_name)
        request.attention_id = attention.id
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return self.read_request(request)

    def review(
        self,
        request_id: str,
        *,
        command: str,
        command_id: str,
        expected_request_row_version: int,
        expected_attention_revision: int,
        reviewer: User,
        comment: str | None,
    ) -> PublicationRequestRead:
        """由非提交人的管理员 CAS 批准或拒绝，批准生成唯一 Release。"""

        request = self._request(request_id, reviewer.tenant_id)
        attention = self.db.get(SopWorkItem, request.attention_id or "")
        if reviewer.role != "admin" or reviewer.id == request.owner_user_id:
            raise PublicationError("PUBLICATION_REVIEWER_DENIED", "reviewer separation required", 403)
        if (
            attention is None
            or attention.tenant_id != request.tenant_id
            or attention.source_type != "resource_publication_request"
            or attention.source_ref != request.id
            or not SopWorkItemService(self.db).is_current_candidate(attention, reviewer.id)
        ):
            raise PublicationError(
                "PUBLICATION_REVIEWER_DENIED", "reviewer is not a frozen candidate", 403
            )
        if request.status in {"approved", "rejected"}:
            if attention and attention.resolution_json.get("command_id") == command_id:
                return self.read_request(request)
            raise PublicationError("PUBLICATION_ALREADY_REVIEWED", "request is terminal")
        if (
            request.status != "submitted"
            or request.row_version != expected_request_row_version
            or attention is None
            or attention.status not in {"offered", "claimed"}
            or attention.revision != expected_attention_revision
        ):
            raise PublicationError("PUBLICATION_STALE", "publication review is stale")
        self._assert_snapshot_current(request)
        if command not in {"approve", "reject"}:
            raise PublicationError("PUBLICATION_COMMAND_INVALID", "unsupported command", 400)
        now = utc_now()
        attention.status = "completed"
        attention.revision += 1
        attention.resolution_json = {
            "command": command,
            "command_id": command_id,
            "actor_user_id": reviewer.id,
            "comment": comment,
            "revision": attention.revision,
        }
        attention.completed_at = now
        attention.updated_at = now
        request.status = "approved" if command == "approve" else "rejected"
        request.reviewed_by_user_id = reviewer.id
        request.row_version += 1
        request.updated_at = now
        request.terminal_at = now
        request.active_slot_key = None
        self.db.add(attention)
        self.db.add(request)
        if command == "approve":
            self._create_release(request)
            if request.resource_type == "general_skill":
                skill = self.db.get(GeneralSkill, request.resource_id)
                if skill is not None:
                    skill.visibility_scope = "tenant_gallery"
                    skill.row_version += 1
                    skill.updated_at = now
                    self.db.add(skill)
        execution = self.db.get(SopInstance, attention.instance_id)
        if execution is not None:
            execution.status = "succeeded" if command == "approve" else "failed"
            execution.active_slot_key = None
            execution.completed_at = now
            execution.updated_at = now
            self.db.add(execution)
        self.db.commit()
        self.db.refresh(request)
        return self.read_request(request)

    def list_releases(self, tenant_id: str, resource_type: str | None = None) -> list[PublicationReleaseRead]:
        """列出租户内 active Release，并从类型化快照投影展示信息。"""

        statement = select(PublicationRelease).where(
            PublicationRelease.tenant_id == tenant_id,
            PublicationRelease.status == "active",
        )
        if resource_type:
            statement = statement.where(PublicationRelease.resource_type == resource_type)
        releases = self.db.exec(statement.order_by(PublicationRelease.created_at.desc())).all()
        return [self._release_read(row) for row in releases]

    def transition_release(
        self,
        release_id: str,
        *,
        command: str,
        command_id: str,
        expected_row_version: int,
        actor: User,
        reason: str,
    ) -> PublicationReleaseRead:
        """管理员普通下架或安全撤销 Release，并在安全撤销时立即 bump 授权。"""

        release = self.db.exec(
            select(PublicationRelease)
            .where(
                PublicationRelease.id == release_id,
                PublicationRelease.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        ).first()
        if (
            release is None
            or release.tenant_id != actor.tenant_id
            or actor.role != "admin"
        ):
            raise PublicationError("PUBLICATION_RELEASE_NOT_FOUND", "release unavailable", 404)
        if release.status != "active":
            if release.terminal_command_id == command_id:
                return self._release_read(release)
            raise PublicationError("PUBLICATION_RELEASE_STALE", "release transition is stale")
        if release.row_version != expected_row_version:
            raise PublicationError("PUBLICATION_RELEASE_STALE", "release transition is stale")
        if command not in {"unpublish", "security_revoke"}:
            raise PublicationError("PUBLICATION_COMMAND_INVALID", "unsupported release command", 400)
        now = utc_now()
        transitioned = self.db.exec(
            update(PublicationRelease)
            .where(
                PublicationRelease.id == release.id,
                PublicationRelease.tenant_id == actor.tenant_id,
                PublicationRelease.status == "active",
                PublicationRelease.row_version == expected_row_version,
            )
            .values(
                status="unpublished" if command == "unpublish" else "security_revoked",
                active_slot_key=None,
                row_version=PublicationRelease.row_version + 1,
                terminal_command_id=command_id,
                terminal_by_user_id=actor.id,
                terminal_reason=reason,
                updated_at=now,
                terminal_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:
            self.db.rollback()
            raise PublicationError("PUBLICATION_RELEASE_STALE", "release transition is stale")
        self.db.refresh(release)
        if command == "security_revoke":
            if release.resource_type == "agent":
                self._revoke_adopted_agents(release, now=now)
            bump_general_skill_authorization_revision(
                self.db,
                actor.tenant_id,
                event_type="organization_release_security_revoked",
                resource_id=release.id,
                payload={"resource_type": release.resource_type, "reason": reason},
            )
        self.db.commit()
        self.db.refresh(release)
        return self._release_read(release)

    def _revoke_adopted_agents(self, release: PublicationRelease, *, now: datetime) -> None:
        """安全撤销整 Agent Release 时停用全部采用副本及其资源绑定。"""

        adopted_agents = self.db.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == release.tenant_id)
        ).all()
        for agent in adopted_agents:
            if (agent.metadata_json or {}).get("adopted_release_id") != release.id:
                continue
            agent.status = "inactive"
            agent.updated_at = now
            self.db.add(agent)
            bindings = self.db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == release.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.status == "active",
                )
            ).all()
            for binding in bindings:
                binding.status = "inactive"
                binding.row_version += 1
                binding.updated_at = now
                self.db.add(binding)

    def adopt(
        self,
        release_id: str,
        target_agent_id: str | None,
        idempotency_key: str,
        actor: User,
    ) -> PublicationAdoptRead:
        """主动采用 Skill 到本人 Agent，或按 Agent 快照克隆新的本人 Agent。"""

        release = self.db.exec(
            select(PublicationRelease)
            .where(
                PublicationRelease.id == release_id,
                PublicationRelease.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        ).first()
        if release is None or release.tenant_id != actor.tenant_id or release.status != "active":
            raise PublicationError("PUBLICATION_RELEASE_NOT_FOUND", "release unavailable", 404)
        request_checksum = _checksum(
            {"release_id": release.id, "target_agent_id": target_agent_id}
        )
        previous = self.db.exec(
            select(PublicationAdoptionCommand).where(
                PublicationAdoptionCommand.tenant_id == actor.tenant_id,
                PublicationAdoptionCommand.actor_user_id == actor.id,
                PublicationAdoptionCommand.idempotency_key == idempotency_key,
            )
        ).first()
        if previous is not None:
            if previous.request_checksum != request_checksum:
                raise PublicationError(
                    "PUBLICATION_IDEMPOTENCY_CONFLICT",
                    "idempotency key was used for another adoption",
                )
            return self._adoption_read(previous)
        reserved = self.db.exec(
            update(PublicationRelease)
            .where(
                PublicationRelease.id == release.id,
                PublicationRelease.tenant_id == actor.tenant_id,
                PublicationRelease.status == "active",
                PublicationRelease.row_version == release.row_version,
            )
            .values(
                row_version=PublicationRelease.row_version + 1,
                updated_at=utc_now(),
            )
        )
        if reserved.rowcount != 1:
            self.db.rollback()
            raise PublicationError("PUBLICATION_RELEASE_STALE", "release adoption is stale")
        self.db.refresh(release)
        if release.resource_type == "general_skill":
            result = self._adopt_skill(release, target_agent_id, actor)
        else:
            result = self._adopt_agent(release, actor)
        self.db.refresh(release)
        if release.status != "active":
            self.db.rollback()
            raise PublicationError("PUBLICATION_RELEASE_NOT_FOUND", "release unavailable", 404)
        command = PublicationAdoptionCommand(
            tenant_id=actor.tenant_id,
            actor_user_id=actor.id,
            idempotency_key=idempotency_key,
            request_checksum=request_checksum,
            release_id=release.id,
            resource_type=result.resource_type,
            target_agent_id=result.target_agent_id,
            binding_id=result.binding_id,
            adopted_agent_id=result.adopted_agent_id,
        )
        self.db.add(command)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            winner = self.db.exec(
                select(PublicationAdoptionCommand).where(
                    PublicationAdoptionCommand.tenant_id == actor.tenant_id,
                    PublicationAdoptionCommand.actor_user_id == actor.id,
                    PublicationAdoptionCommand.idempotency_key == idempotency_key,
                )
            ).first()
            if winner is None or winner.request_checksum != request_checksum:
                raise PublicationError(
                    "PUBLICATION_ADOPTION_CONFLICT",
                    "concurrent adoption could not be reconciled",
                )
            return self._adoption_read(winner)
        return result

    def read_request(self, request: ResourcePublicationRequest) -> PublicationRequestRead:
        """投影不含冻结正文的申请摘要。"""

        return PublicationRequestRead(
            id=request.id,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            snapshot_id=request.snapshot_id,
            snapshot_checksum=request.snapshot_checksum,
            attention_id=request.attention_id,
            status=request.status,
            row_version=request.row_version,
        )

    def _snapshot_skill(self, skill_id: str, expected_revision: int, owner: User) -> tuple[dict[str, Any], str]:
        """冻结本人当前已发布 Skill revision、来源、许可证、权限和风险。"""

        skill = self.db.get(GeneralSkill, skill_id)
        if (
            skill is None
            or skill.tenant_id != owner.tenant_id
            or skill.owner_user_id != owner.id
            or skill.row_version != expected_revision
            or skill.status != "published"
            or not skill.current_published_revision_id
        ):
            raise PublicationError("PUBLICATION_RESOURCE_STALE", "skill unavailable", 404)
        revision = self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
        if revision is None or revision.status != "published":
            raise PublicationError("PUBLICATION_RESOURCE_STALE", "revision unavailable")
        facts = {
            "skill_id": skill.id,
            "approved_revision_id": revision.id,
            "content_checksum": revision.content_checksum,
            "manifest_checksum": revision.manifest_checksum,
            "source_snapshot_json": dict(revision.source_snapshot_json or {}),
            "license_snapshot_json": {
                "license_hint": revision.parsed_metadata_json.get("license"),
                "declared": bool(revision.parsed_metadata_json.get("license")),
            },
            "capability_snapshot_json": dict(revision.requested_capabilities_json or {}),
            "risk_snapshot_json": {
                "resource_count": len(revision.resource_manifest_json or []),
                "contains_executable_content": any(
                    str(item.get("relative_path") or "").endswith((".sh", ".py", ".js"))
                    for item in revision.resource_manifest_json or []
                ),
            },
            "tenant_id": owner.tenant_id,
        }
        facts["snapshot_checksum"] = _checksum(facts)
        return facts, skill.name

    def _snapshot_agent(self, agent_id: str, expected_revision: int, owner: User) -> tuple[dict[str, Any], str]:
        """冻结 Agent persona 与可传播组件，明确排除记忆、连接账号和凭据。"""

        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != owner.tenant_id
            or agent_owner_user_id(agent) != owner.id
            or agent.profile_revision != expected_revision
            or agent.status != "active"
        ):
            raise PublicationError("PUBLICATION_RESOURCE_STALE", "agent unavailable", 404)
        components: list[dict[str, object]] = []
        for binding in self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == owner.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.status == "active",
            )
        ).all():
            if binding.resource_type not in {
                "general_skill",
                "knowledge_base",
                "tool",
                "skill",
            }:
                continue
            metadata = dict(binding.metadata_json or {})
            if binding.resource_type == "general_skill":
                skill = self.db.get(GeneralSkill, binding.resource_id)
                if (
                    skill is None
                    or skill.tenant_id != owner.tenant_id
                    or not skill.current_published_revision_id
                ):
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_STALE",
                        "agent contains an unavailable Skill",
                    )
                pinned_revision_id = (
                    metadata.get("pinned_revision_id")
                    if metadata.get("revision_policy") == "pinned"
                    else skill.current_published_revision_id
                )
                revision = self.db.get(GeneralSkillRevision, str(pinned_revision_id or ""))
                if revision is None or revision.status not in {"published", "superseded"}:
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_STALE",
                        "agent contains an unavailable Skill revision",
                    )
                if skill.owner_user_id != owner.id and not self._released_component_authorized(
                    binding, resource_type="general_skill", resource_id=skill.id
                ):
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_DENIED",
                        "agent contains a private Skill without propagation authorization",
                        403,
                    )
                metadata = {
                    **metadata,
                    "revision_policy": "pinned",
                    "pinned_revision_id": revision.id,
                    "published_content_checksum": revision.content_checksum,
                }
            elif binding.resource_type == "knowledge_base":
                knowledge = self.db.get(KnowledgeBase, binding.resource_id)
                if (
                    knowledge is None
                    or knowledge.tenant_id != owner.tenant_id
                    or knowledge.status != "active"
                    or (
                        knowledge.owner_user_id != owner.id
                        and knowledge.access_scope != "tenant"
                        and not self._released_component_authorized(
                            binding, resource_type="knowledge_base", resource_id=knowledge.id
                        )
                    )
                ):
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_DENIED", "agent contains unavailable knowledge", 403
                    )
            elif binding.resource_type == "tool":
                tool = self.db.get(Tool, binding.resource_id)
                if tool is None or tool.tenant_id != owner.tenant_id or not tool.enabled:
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_DENIED", "agent contains an unavailable tool", 403
                    )
            elif binding.resource_type == "skill":
                legacy_skill = self.db.get(Skill, binding.resource_id)
                if (
                    legacy_skill is None
                    or legacy_skill.tenant_id != owner.tenant_id
                    or legacy_skill.status != "published"
                ):
                    raise PublicationError(
                        "PUBLICATION_COMPONENT_DENIED", "agent contains an unavailable skill", 403
                    )
            components.append(
                {
                    "resource_type": binding.resource_type,
                    "resource_id": binding.resource_id,
                    "metadata": metadata,
                }
            )
        components.sort(key=lambda item: (str(item["resource_type"]), str(item["resource_id"])))
        persona = {
            "name": agent.name,
            "description": agent.description,
            "persona_prompt": agent.persona_prompt,
            "agent_category_code": agent.agent_category_code,
            "responsible_org_unit_id": agent.responsible_org_unit_id,
            "profile_revision": agent.profile_revision,
        }
        facts = {
            "tenant_id": owner.tenant_id,
            "agent_id": agent.id,
            "persona_checksum": _checksum(persona),
            "persona_snapshot_json": persona,
            "component_snapshot_json": components,
            "governance_snapshot_json": {
                "excluded": ["memory", "conversation", "connection", "credential", "schedule"],
                "owner_user_id": owner.id,
            },
        }
        facts["snapshot_checksum"] = _checksum(facts)
        return facts, agent.name

    def _released_component_authorized(
        self,
        binding: AgentResourceBinding,
        *,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """只接受仍有效且快照中明确包含该组件的组织 Release 传播证据。"""

        release_id = binding.metadata_json.get("publication_release_id")
        snapshot_id = binding.metadata_json.get("publication_snapshot_id")
        if not isinstance(release_id, str) or not isinstance(snapshot_id, str):
            return False
        release = self.db.get(PublicationRelease, release_id)
        if (
            release is None
            or release.tenant_id != binding.tenant_id
            or release.status != "active"
            or release.snapshot_id != snapshot_id
        ):
            return False
        if release.resource_type == "general_skill" and resource_type == "general_skill":
            snapshot = self.db.get(GeneralSkillPublicationRevision, snapshot_id)
            return bool(
                release.resource_id == resource_id
                and snapshot is not None
                and snapshot.tenant_id == binding.tenant_id
                and snapshot.skill_id == resource_id
                and snapshot.approved_revision_id
                == binding.metadata_json.get("pinned_revision_id")
                and snapshot.content_checksum
                == binding.metadata_json.get("published_content_checksum")
            )
        if release.resource_type != "agent":
            return False
        snapshot = self.db.get(AgentPublicationRevision, snapshot_id)
        if snapshot is None or snapshot.tenant_id != binding.tenant_id:
            return False
        for component in snapshot.component_snapshot_json or []:
            if (
                component.get("resource_type") != resource_type
                or component.get("resource_id") != resource_id
            ):
                continue
            frozen_metadata = dict(component.get("metadata") or {})
            if resource_type != "general_skill":
                return True
            return bool(
                frozen_metadata.get("pinned_revision_id")
                == binding.metadata_json.get("pinned_revision_id")
                and frozen_metadata.get("published_content_checksum")
                == binding.metadata_json.get("published_content_checksum")
            )
        return False

    def _publication_execution(self, request: ResourcePublicationRequest, owner: User) -> SopInstance:
        """创建只承载组织发布 Attention 的持久 Execution。"""

        execution = SopInstance(
            id=new_id("sopinst"),
            tenant_id=owner.tenant_id,
            session_id=f"publication:{request.id}",
            kind="dynamic_task",
            active_slot_key=f"publication:{request.id}",
            initiator_user_id=owner.id,
            source_kind="publication",
            source_ref=request.id,
            agent_id=request.resource_id if request.resource_type == "agent" else f"publication-{request.id}",
            goal_snapshot_json={"goal": f"审核组织发布 {request.resource_type}:{request.resource_id}"},
            current_plan_revision_id=f"publication-plan:{request.id}",
            current_plan_checksum=request.snapshot_checksum,
            capability_snapshot_json={"publication_snapshot_checksum": request.snapshot_checksum},
            status="waiting",
        )
        self.db.add(execution)
        self.db.flush()
        return execution

    def _publication_attention(
        self,
        request: ResourcePublicationRequest,
        execution: SopInstance,
        owner: User,
        resource_name: str,
    ) -> SopWorkItem:
        """冻结活动管理员为审核候选，提交人即便是管理员也不能自审。"""

        admins = self.db.exec(
            select(User).where(
                User.tenant_id == owner.tenant_id,
                User.role == "admin",
                User.membership_status == "active",
                User.id != owner.id,
            )
        ).all()
        if not admins:
            raise PublicationError("PUBLICATION_REVIEWER_UNAVAILABLE", "no separated admin reviewer")
        attention = SopWorkItem(
            tenant_id=owner.tenant_id,
            instance_id=execution.id,
            attention_kind="publication",
            attention_key=f"publication:{request.id}",
            title=f"审核组织发布：{resource_name}",
            source_type="resource_publication_request",
            source_ref=request.id,
            payload_json={
                "publication_request_kind": request.resource_type,
                "publication_request_id": request.id,
                "resource_id": request.resource_id,
                "resource_name": resource_name,
                "snapshot_id": request.snapshot_id,
                "snapshot_checksum": request.snapshot_checksum,
                "owner_user_id": owner.id,
                "request_row_version": request.row_version,
            },
            allowed_commands_json=["approve", "reject"],
            candidate_snapshot_json=[{"user_id": item.id} for item in admins],
            initiator_user_id=owner.id,
            exclude_initiator=True,
            status="offered",
        )
        self.db.add(attention)
        self.db.flush()
        for admin in admins:
            self.db.add(
                SopWorkItemCandidate(
                    tenant_id=owner.tenant_id,
                    work_item_id=attention.id,
                    user_id=admin.id,
                    source_types_json=["organization_publication_reviewer"],
                )
            )
        return attention

    def _assert_snapshot_current(self, request: ResourcePublicationRequest) -> None:
        """批准前检查原资源仍指向提交时快照；变化则申请 stale。"""

        if request.resource_type == "general_skill":
            snapshot = self.db.get(GeneralSkillPublicationRevision, request.snapshot_id)
            skill = self.db.get(GeneralSkill, request.resource_id)
            current = skill.current_published_revision_id if skill else None
            valid = snapshot is not None and current == snapshot.approved_revision_id
        else:
            snapshot = self.db.get(AgentPublicationRevision, request.snapshot_id)
            agent = self.db.get(AgentProfile, request.resource_id)
            owner = self.db.get(User, request.owner_user_id)
            try:
                current_snapshot = (
                    self._snapshot_agent(
                        request.resource_id,
                        int(snapshot.persona_snapshot_json.get("profile_revision") or 0),
                        owner,
                    )[0]
                    if snapshot is not None and owner is not None
                    else None
                )
            except PublicationError:
                current_snapshot = None
            valid = (
                agent is not None
                and snapshot is not None
                and current_snapshot is not None
                and current_snapshot["snapshot_checksum"] == snapshot.snapshot_checksum
            )
        if not valid:
            self._mark_request_stale(request, "resource_changed_before_review")
            self.db.commit()
            raise PublicationError("PUBLICATION_SNAPSHOT_STALE", "resource changed after submission")

    def _create_release(self, request: ResourcePublicationRequest) -> PublicationRelease:
        """从批准申请生成独立 active Release，保留申请为不可变审核证据。"""

        existing = self.db.exec(
            select(PublicationRelease).where(
                PublicationRelease.tenant_id == request.tenant_id,
                PublicationRelease.resource_type == request.resource_type,
                PublicationRelease.resource_id == request.resource_id,
                PublicationRelease.active_slot_key == "active",
            )
        ).first()
        if existing is not None:
            existing.status = "unpublished"
            existing.active_slot_key = None
            existing.row_version += 1
            existing.updated_at = utc_now()
            existing.terminal_at = existing.updated_at
            self.db.add(existing)
        release = PublicationRelease(
            tenant_id=request.tenant_id,
            approved_request_id=request.id,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            snapshot_kind=request.snapshot_kind,
            snapshot_id=request.snapshot_id,
            snapshot_checksum=request.snapshot_checksum,
        )
        self.db.add(release)
        self.db.flush()
        return release

    def _release_read(self, release: PublicationRelease) -> PublicationReleaseRead:
        """从类型化冻结快照投影 Release 展示内容。"""

        if release.resource_type == "general_skill":
            snapshot = self.db.get(GeneralSkillPublicationRevision, release.snapshot_id)
            skill = self.db.get(GeneralSkill, release.resource_id)
            if snapshot is None or skill is None:
                raise PublicationError("PUBLICATION_RELEASE_CORRUPT", "skill release corrupt")
            return PublicationReleaseRead(
                id=release.id,
                resource_type=release.resource_type,
                resource_id=release.resource_id,
                snapshot_id=release.snapshot_id,
                snapshot_checksum=release.snapshot_checksum,
                name=skill.name,
                description=skill.description or "",
                approved_revision_id=snapshot.approved_revision_id,
                status=release.status,
                row_version=release.row_version,
            )
        snapshot = self.db.get(AgentPublicationRevision, release.snapshot_id)
        if snapshot is None:
            raise PublicationError("PUBLICATION_RELEASE_CORRUPT", "agent release corrupt")
        return PublicationReleaseRead(
            id=release.id,
            resource_type=release.resource_type,
            resource_id=release.resource_id,
            snapshot_id=release.snapshot_id,
            snapshot_checksum=release.snapshot_checksum,
            name=str(snapshot.persona_snapshot_json.get("name") or "组织数字员工"),
            description=str(snapshot.persona_snapshot_json.get("description") or ""),
            components=list(snapshot.component_snapshot_json or []),
            status=release.status,
            row_version=release.row_version,
        )

    def _adopt_skill(
        self, release: PublicationRelease, target_agent_id: str | None, actor: User
    ) -> PublicationAdoptRead:
        """把已审 Skill revision pinned 到本人目标 Agent，不授予 Skill 根修改权。"""

        agent = self.db.get(AgentProfile, target_agent_id or "")
        snapshot = self.db.get(GeneralSkillPublicationRevision, release.snapshot_id)
        if agent is None or agent_owner_user_id(agent) != actor.id or snapshot is None:
            raise PublicationError("PUBLICATION_ADOPTION_TARGET_DENIED", "target agent denied", 403)
        binding = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == actor.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == release.resource_id,
            )
        ).first()
        metadata = GeneralSkillBindingMetadata(
            revision_policy="pinned",
            pinned_revision_id=snapshot.approved_revision_id,
            invocation_policy="user_only",
            atomic_execution_allowed=False,
            created_by_user_id=actor.id,
            publication_release_id=release.id,
            publication_snapshot_id=release.snapshot_id,
            published_content_checksum=snapshot.content_checksum,
        ).model_dump(mode="json")
        if binding is None:
            binding = AgentResourceBinding(
                tenant_id=actor.tenant_id,
                agent_id=agent.id,
                resource_type="general_skill",
                resource_id=release.resource_id,
                status="active",
                metadata_json=metadata,
            )
        else:
            binding.status = "active"
            binding.metadata_json = metadata
            binding.row_version += 1
            binding.updated_at = utc_now()
        self.db.add(binding)
        self.db.flush()
        bump_general_skill_authorization_revision(
            self.db,
            actor.tenant_id,
            event_type="organization_skill_release_adopted",
            resource_id=binding.id,
            payload={"release_id": release.id, "agent_id": agent.id},
        )
        return PublicationAdoptRead(
            release_id=release.id,
            resource_type="general_skill",
            target_agent_id=agent.id,
            binding_id=binding.id,
        )

    def _adopt_agent(self, release: PublicationRelease, actor: User) -> PublicationAdoptRead:
        """从已审 Agent 快照克隆本人 Agent，并仅复制明确可传播组件。"""

        snapshot = self.db.get(AgentPublicationRevision, release.snapshot_id)
        if snapshot is None:
            raise PublicationError("PUBLICATION_RELEASE_CORRUPT", "agent snapshot unavailable")
        persona = dict(snapshot.persona_snapshot_json or {})
        name_base = str(persona.get("name") or "组织数字员工")
        name = f"{name_base}（采用）"
        suffix = 2
        while self.db.exec(
            select(AgentProfile.id).where(
                AgentProfile.tenant_id == actor.tenant_id,
                AgentProfile.name == name,
            )
        ).first():
            name = f"{name_base}（采用 {suffix}）"
            suffix += 1
        adopted = AgentProfile(
            tenant_id=actor.tenant_id,
            name=name,
            description=str(persona.get("description") or ""),
            persona_prompt=str(persona.get("persona_prompt") or ""),
            owner_user_id=actor.id,
            source_agent_id=release.resource_id,
            source_agent_version=release.snapshot_checksum,
            agent_category_code=str(persona.get("agent_category_code") or "assistant"),
            metadata_json={"adopted_release_id": release.id, "snapshot_id": release.snapshot_id},
        )
        self.db.add(adopted)
        self.db.flush()
        for component in snapshot.component_snapshot_json or []:
            resource_type = str(component.get("resource_type") or "")
            if resource_type not in {"general_skill", "knowledge_base", "tool", "skill"}:
                continue
            metadata = {
                **dict(component.get("metadata") or {}),
                "publication_release_id": release.id,
                "publication_snapshot_id": release.snapshot_id,
            }
            self.db.add(
                AgentResourceBinding(
                    tenant_id=actor.tenant_id,
                    agent_id=adopted.id,
                    resource_type=resource_type,
                    resource_id=str(component.get("resource_id") or ""),
                    status="active",
                    metadata_json=metadata,
                )
            )
        bump_general_skill_authorization_revision(
            self.db,
            actor.tenant_id,
            event_type="organization_agent_release_adopted",
            resource_id=adopted.id,
            payload={"release_id": release.id, "agent_id": adopted.id},
        )
        return PublicationAdoptRead(
            release_id=release.id,
            resource_type="agent",
            target_agent_id=adopted.id,
            adopted_agent_id=adopted.id,
        )

    def _adoption_read(self, command: PublicationAdoptionCommand) -> PublicationAdoptRead:
        """把已提交采用命令还原为稳定响应，不重复创建绑定或 Agent。"""

        return PublicationAdoptRead(
            release_id=command.release_id,
            resource_type=command.resource_type,
            target_agent_id=command.target_agent_id or command.adopted_agent_id or "",
            binding_id=command.binding_id,
            adopted_agent_id=command.adopted_agent_id,
        )

    def _mark_request_stale(
        self,
        request: ResourcePublicationRequest,
        reason: str,
    ) -> None:
        """终止旧申请及其 Attention/Execution，释放活动槽供新快照重新提交。"""

        now = utc_now()
        request.status = "stale"
        request.active_slot_key = None
        request.row_version += 1
        request.updated_at = now
        request.terminal_at = now
        attention = self.db.get(SopWorkItem, request.attention_id or "")
        if attention is not None and attention.status in {"offered", "claimed"}:
            attention.status = "cancelled"
            attention.revision += 1
            attention.resolution_json = {"reason": reason}
            attention.completed_at = now
            attention.updated_at = now
            self.db.add(attention)
            execution = self.db.get(SopInstance, attention.instance_id)
            if execution is not None:
                execution.status = "cancelled"
                execution.active_slot_key = None
                execution.completed_at = now
                execution.updated_at = now
                self.db.add(execution)
        self.db.add(request)
        self.db.flush()

    def _request(self, request_id: str, tenant_id: str) -> ResourcePublicationRequest:
        """按 tenant 定位申请，不跨租户枚举。"""

        request = self.db.get(ResourcePublicationRequest, request_id)
        if request is None or request.tenant_id != tenant_id:
            raise PublicationError("PUBLICATION_REQUEST_NOT_FOUND", "request unavailable", 404)
        return request
