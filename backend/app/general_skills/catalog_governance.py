"""
@Time       : 2026/08/29 19:45
@Author     : zhanglp8181
@File       : catalog_governance.py
@CallChain  : Skill 广场管理 API → 目录审核/目标绑定服务 → GeneralSkill/Revision/Binding
@Description: 完成项目 Skill 候选审核、项目广场发布和两类 Agent 显式安装/绑定治理。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.audit.service import append_management_audit
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    GeneralSkill,
    GeneralSkillCatalogCommand,
    GeneralSkillRevision,
    OrganizationUnit,
    PublicationRelease,
    User,
    utc_now,
)
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.general_skills.eligibility import GeneralSkillBindingMetadata
from app.general_skills.governance import bump_general_skill_authorization_revision


CatalogReviewDecision = Literal["approve", "reject"]
CatalogBindingMode = Literal["install", "bind"]
CatalogLifecycleAction = Literal["archive", "revoke"]


class CatalogGovernanceError(RuntimeError):
    """表示目录审核或目标绑定违反权限、状态、版本或幂等契约。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        """保存稳定错误码和 HTTP 建议状态。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CatalogReviewResult:
    """表达一次批量审核的可重放摘要和逐项状态。"""

    command_id: str
    replayed: bool
    approved_count: int
    rejected_count: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CatalogBindingResult:
    """表达一次 Skill 到能力分身或组织数字员工的显式绑定结果。"""

    action: Literal["created", "updated", "unchanged"]
    mode: CatalogBindingMode
    binding: AgentResourceBinding


@dataclass(frozen=True, slots=True)
class CatalogLifecycleResult:
    """表达一次平台 Skill 下架或安全撤销的幂等结果和影响范围。"""

    command_id: str
    replayed: bool
    action: CatalogLifecycleAction
    skill_id: str
    slug: str
    skill_status: str
    revision_id: str
    revision_status: str
    skill_row_version: int
    revision_row_version: int
    deactivated_binding_count: int


class GeneralSkillCatalogGovernanceService:
    """管理项目级 Skill 候选审核、广场发布和租户 Agent 目标绑定。"""

    review_command_type = "catalog_review"
    lifecycle_command_type = "catalog_lifecycle"

    def __init__(self, db: Session) -> None:
        """绑定当前请求数据库会话，所有操作由调用方决定事务边界。"""

        self.db = db

    def review(
        self,
        *,
        tenant_id: str,
        command_id: str,
        actor_user_id: str,
        items: Sequence[Mapping[str, Any]],
    ) -> CatalogReviewResult:
        """以两级 CAS 原子审核全部候选，批准后进入项目级 Skill 广场。"""

        self._ensure_admin(tenant_id, actor_user_id)
        normalized_command_id = self._command_id(command_id)
        normalized_items = self._normalize_review_items(items)
        request_checksum = _checksum(
            {
                "catalog_scope": "platform",
                "command_type": self.review_command_type,
                "items": normalized_items,
            }
        )
        previous = self.db.exec(
            select(GeneralSkillCatalogCommand).where(
                GeneralSkillCatalogCommand.catalog_scope == "platform",
                GeneralSkillCatalogCommand.scope_key == "platform",
                GeneralSkillCatalogCommand.command_type == self.review_command_type,
                GeneralSkillCatalogCommand.command_id == normalized_command_id,
            )
        ).first()
        if previous is not None:
            if previous.request_checksum != request_checksum:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_COMMAND_CONFLICT",
                    "catalog review command id was used for another request",
                )
            return _review_result_from_command(previous, replayed=True)

        candidates = self._review_candidates(tenant_id, normalized_items)
        try:
            result_items: list[dict[str, Any]] = []
            approved_count = 0
            rejected_count = 0
            for item, skill, revision in candidates:
                result = self._apply_review(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    command_id=normalized_command_id,
                    item=item,
                    skill=skill,
                    revision=revision,
                )
                result_items.append(result)
                if result["decision"] == "approve":
                    approved_count += 1
                else:
                    rejected_count += 1
            command = GeneralSkillCatalogCommand(
                tenant_id=None,
                catalog_scope="platform",
                scope_key="platform",
                command_type=self.review_command_type,
                command_id=normalized_command_id,
                request_checksum=request_checksum,
                source_revision="catalog-review-v1",
                status="committed",
                result_json={
                    "approved_count": approved_count,
                    "rejected_count": rejected_count,
                    "items": result_items,
                },
            )
            self.db.add(command)
            self.db.flush()
            append_management_audit(
                self.db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_display_name=self._actor_display_name(actor_user_id),
                action="general_skill.catalog.review_batch",
                action_kind="review",
                outcome="success",
                resource_type="general_skill_catalog",
                resource_id=command.id,
                request_id=normalized_command_id,
                detail={
                    "item_count": len(result_items),
                    "approved_count": approved_count,
                    "rejected_count": rejected_count,
                },
            )
            self.db.commit()
        except CatalogGovernanceError:
            self.db.rollback()
            raise
        except IntegrityError as exc:
            self.db.rollback()
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog review changed concurrently",
            ) from exc
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(command)
        return _review_result_from_command(command, replayed=False)

    def bind(
        self,
        *,
        current_user: User,
        skill_id: str,
        agent_id: str,
        mode: CatalogBindingMode,
        revision_policy: str,
        pinned_revision_id: str | None,
        invocation_policy: str,
    ) -> CatalogBindingResult:
        """将已发布项目 Skill 显式安装到能力分身或绑定到组织数字员工。"""

        if mode not in {"install", "bind"}:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_BINDING_MODE_INVALID",
                "catalog binding mode is invalid",
                400,
            )
        skill = self._published_catalog_skill(current_user.tenant_id, skill_id)
        agent = self._target_agent(current_user, agent_id, mode)
        revision = self._binding_revision(skill, revision_policy, pinned_revision_id)
        metadata = GeneralSkillBindingMetadata(
            revision_policy=revision_policy,
            pinned_revision_id=revision.id if revision_policy == "pinned" else None,
            invocation_policy=invocation_policy,
            created_by_user_id=current_user.id,
            managed_catalog=True,
            catalog_key=str((skill.metadata_json or {}).get("catalog_key") or ""),
        )
        binding = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == current_user.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).first()
        desired = metadata.model_dump(mode="json")
        action: Literal["created", "updated", "unchanged"]
        if binding is None:
            binding = AgentResourceBinding(
                tenant_id=current_user.tenant_id,
                agent_id=agent.id,
                resource_type="general_skill",
                resource_id=skill.id,
                status="active",
                metadata_json=desired,
            )
            self.db.add(binding)
            action = "created"
        elif binding.status == "active" and dict(binding.metadata_json or {}) == desired:
            action = "unchanged"
        else:
            binding.status = "active"
            binding.metadata_json = desired
            binding.row_version += 1
            binding.updated_at = utc_now()
            self.db.add(binding)
            action = "updated"
        self.db.flush()
        if action != "unchanged":
            bump_general_skill_authorization_revision(
                self.db,
                current_user.tenant_id,
                event_type="catalog_binding_created" if action == "created" else "catalog_binding_updated",
                resource_id=binding.id,
                payload={
                    "mode": mode,
                    "agent_id": agent.id,
                    "skill_id": skill.id,
                    "revision_id": revision.id,
                },
            )
            append_management_audit(
                self.db,
                tenant_id=current_user.tenant_id,
                actor_user_id=current_user.id,
                actor_display_name=current_user.display_name or current_user.username,
                action="general_skill.catalog.binding",
                action_kind="create" if action == "created" else "update",
                outcome="success",
                resource_type="general_skill_binding",
                resource_id=binding.id,
                request_id=f"catalog-binding:{binding.id}:{binding.row_version}",
                before={"status": "inactive" if action == "updated" else None},
                after={
                    "status": binding.status,
                    "mode": mode,
                    "agent_id": agent.id,
                    "skill_id": skill.id,
                    "revision_id": revision.id,
                },
            )
            self.db.commit()
            self.db.refresh(binding)
        return CatalogBindingResult(action=action, mode=mode, binding=binding)

    def lifecycle(
        self,
        *,
        current_user: User,
        skill_id: str,
        command_id: str,
        action: CatalogLifecycleAction,
        expected_skill_row_version: int,
        expected_revision_row_version: int,
        reason: str,
    ) -> CatalogLifecycleResult:
        """以双 CAS 下架或安全撤销平台 Skill，并按动作处理既有租户绑定。"""

        self._ensure_admin(current_user.tenant_id, current_user.id)
        if action not in {"archive", "revoke"}:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_LIFECYCLE_ACTION_INVALID",
                "catalog lifecycle action is invalid",
                400,
            )
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 2000:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_LIFECYCLE_REASON_INVALID",
                "catalog lifecycle reason is invalid",
                400,
            )
        normalized_command_id = self._command_id(command_id)
        request_checksum = _checksum(
            {
                "catalog_scope": "platform",
                "command_type": self.lifecycle_command_type,
                "skill_id": skill_id,
                "action": action,
                "expected_skill_row_version": expected_skill_row_version,
                "expected_revision_row_version": expected_revision_row_version,
                "reason": normalized_reason,
            }
        )
        previous = self.db.exec(
            select(GeneralSkillCatalogCommand).where(
                GeneralSkillCatalogCommand.catalog_scope == "platform",
                GeneralSkillCatalogCommand.scope_key == "platform",
                GeneralSkillCatalogCommand.command_type == self.lifecycle_command_type,
                GeneralSkillCatalogCommand.command_id == normalized_command_id,
            )
        ).first()
        if previous is not None:
            if previous.request_checksum != request_checksum:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_COMMAND_CONFLICT",
                    "catalog lifecycle command id was used for another request",
                )
            return _lifecycle_result_from_command(previous, replayed=True)

        skill = self.db.get(GeneralSkill, skill_id)
        revision = (
            self.db.get(GeneralSkillRevision, skill.current_published_revision_id)
            if skill is not None and skill.current_published_revision_id
            else None
        )
        if (
            skill is None
            or skill.catalog_scope != "platform"
            or skill.tenant_id is not None
            or (skill.metadata_json or {}).get("managed_catalog") is not True
            or skill.status != "published"
            or revision is None
            or revision.status != "published"
            or skill.current_published_revision_id != revision.id
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_NOT_AVAILABLE",
                "published catalog skill is unavailable",
                404,
            )
        if (
            skill.row_version != expected_skill_row_version
            or revision.row_version != expected_revision_row_version
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog skill changed before lifecycle transition",
            )
        if not self._revision_checksum_valid(revision):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_CHECKSUM_INVALID",
                "catalog revision checksum is invalid",
            )

        now = utc_now()
        before = {"skill_status": skill.status, "revision_status": revision.status}
        metadata = dict(skill.metadata_json or {})
        metadata["catalog_lifecycle_status"] = "revoked" if action == "revoke" else "archived"
        metadata["last_lifecycle_reason"] = normalized_reason
        metadata["last_lifecycle_command_id"] = normalized_command_id
        revision_values: dict[str, Any] = {
            "row_version": GeneralSkillRevision.row_version + 1,
        }
        if action == "revoke":
            revision_values.update(status="revoked", revoked_at=now)
        try:
            revision_result = self.db.exec(
                update(GeneralSkillRevision)
                .where(
                    GeneralSkillRevision.id == revision.id,
                    GeneralSkillRevision.row_version == expected_revision_row_version,
                    GeneralSkillRevision.status == "published",
                )
                .values(**revision_values)
                .execution_options(synchronize_session=False)
            )
            skill_result = self.db.exec(
                update(GeneralSkill)
                .where(
                    GeneralSkill.id == skill.id,
                    GeneralSkill.row_version == expected_skill_row_version,
                    GeneralSkill.status == "published",
                    GeneralSkill.current_published_revision_id == revision.id,
                )
                .values(
                    status="archived",
                    metadata_json=metadata,
                    row_version=GeneralSkill.row_version + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if revision_result.rowcount != 1 or skill_result.rowcount != 1:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog skill changed during lifecycle transition",
                )
            bindings = (
                self.db.exec(
                    select(AgentResourceBinding).where(
                        AgentResourceBinding.resource_type == "general_skill",
                        AgentResourceBinding.resource_id == skill.id,
                        AgentResourceBinding.status == "active",
                    )
                ).all()
                if action == "revoke"
                else []
            )
            for binding in bindings:
                binding.status = "inactive"
                binding.row_version += 1
                binding.updated_at = now
                self.db.add(binding)
            self.db.flush()
            affected_tenants = sorted({binding.tenant_id for binding in bindings})
            event_type = (
                "catalog_skill_revoked" if action == "revoke" else "catalog_skill_archived"
            )
            for binding_tenant_id in affected_tenants:
                bump_general_skill_authorization_revision(
                    self.db,
                    binding_tenant_id,
                    event_type=event_type,
                    resource_id=skill.id,
                    payload={
                        "action": action,
                        "skill_id": skill.id,
                        "revision_id": revision.id,
                        "command_id": normalized_command_id,
                    },
                )
            revision_status = "revoked" if action == "revoke" else "published"
            result_json = {
                "action": action,
                "skill_id": skill.id,
                "slug": skill.slug,
                "skill_status": "archived",
                "revision_id": revision.id,
                "revision_status": revision_status,
                "skill_row_version": expected_skill_row_version + 1,
                "revision_row_version": expected_revision_row_version + 1,
                "deactivated_binding_count": len(bindings),
            }
            command = GeneralSkillCatalogCommand(
                tenant_id=None,
                catalog_scope="platform",
                scope_key="platform",
                command_type=self.lifecycle_command_type,
                command_id=normalized_command_id,
                request_checksum=request_checksum,
                source_revision="catalog-lifecycle-v1",
                status="committed",
                result_json=result_json,
            )
            self.db.add(command)
            self.db.flush()
            append_management_audit(
                self.db,
                tenant_id=current_user.tenant_id,
                actor_user_id=current_user.id,
                actor_display_name=self._actor_display_name(current_user.id),
                action="general_skill.catalog.lifecycle",
                action_kind=action,
                outcome="success",
                resource_type="general_skill",
                resource_id=skill.id,
                request_id=normalized_command_id,
                before=before,
                after={
                    "skill_status": "archived",
                    "revision_status": revision_status,
                },
                detail={
                    "revision_id": revision.id,
                    "reason": normalized_reason,
                    "deactivated_binding_count": len(bindings),
                    "affected_tenant_count": len(affected_tenants),
                },
            )
            self.db.commit()
        except CatalogGovernanceError:
            self.db.rollback()
            raise
        except IntegrityError as exc:
            self.db.rollback()
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog lifecycle changed concurrently",
            ) from exc
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(command)
        return _lifecycle_result_from_command(command, replayed=False)

    def _review_candidates(
        self,
        tenant_id: str,
        items: Sequence[Mapping[str, Any]],
    ) -> list[tuple[dict[str, Any], GeneralSkill, GeneralSkillRevision]]:
        """在写入前完整校验全部平台候选，确保批量审核不会产生部分成功。"""

        candidates: list[tuple[dict[str, Any], GeneralSkill, GeneralSkillRevision]] = []
        for item in items:
            skill = self.db.get(GeneralSkill, str(item["skill_id"]))
            if (
                skill is None
                or skill.catalog_scope != "platform"
                or skill.tenant_id is not None
                or (skill.metadata_json or {}).get("managed_catalog") is not True
            ):
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_NOT_AVAILABLE",
                    "catalog candidate is unavailable",
                    404,
                )
            revision = self._latest_revision(skill)
            if revision is None:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_REVISION_MISSING",
                    "catalog candidate has no revision",
                )
            if skill.row_version != int(item["expected_skill_row_version"]):
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog candidate changed before review",
                )
            if revision.row_version != int(item["expected_revision_row_version"]):
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog revision changed before review",
                )
            if skill.status not in {"draft", "reviewing"} or revision.status not in {
                "draft",
                "reviewing",
            }:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog candidate is no longer awaiting review",
                )
            if not self._revision_checksum_valid(revision):
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_CHECKSUM_INVALID",
                    "catalog revision checksum is invalid",
                )
            candidates.append((dict(item), skill, revision))
        return candidates

    def _apply_review(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        command_id: str,
        item: dict[str, Any],
        skill: GeneralSkill,
        revision: GeneralSkillRevision,
    ) -> dict[str, Any]:
        """以 CAS 更新单条平台候选并追加审计和前后状态。"""

        decision = str(item["decision"])
        now = utc_now()
        before = {"skill_status": skill.status, "revision_status": revision.status}
        metadata = dict(skill.metadata_json or {})
        metadata["review_status"] = "approved" if decision == "approve" else "rejected"
        if item.get("review_note"):
            metadata["last_review_note"] = str(item["review_note"])
        if decision == "approve":
            revision_result = self.db.exec(
                update(GeneralSkillRevision)
                .where(
                    GeneralSkillRevision.id == revision.id,
                    GeneralSkillRevision.row_version == revision.row_version,
                    GeneralSkillRevision.status.in_(["draft", "reviewing"]),
                )
                .values(
                    status="published",
                    row_version=GeneralSkillRevision.row_version + 1,
                    published_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            skill_result = self.db.exec(
                update(GeneralSkill)
                .where(
                    GeneralSkill.id == skill.id,
                    GeneralSkill.row_version == skill.row_version,
                    GeneralSkill.status.in_(["draft", "reviewing"]),
                    GeneralSkill.current_published_revision_id.is_(None),
                )
                .values(
                    status="published",
                    current_published_revision_id=revision.id,
                    metadata_json=metadata,
                    row_version=GeneralSkill.row_version + 1,
                    planning_guidance_json=self._guidance_snapshot(skill),
                    planning_guidance_checksum=self._guidance_checksum(skill),
                    planning_guidance_published_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if revision_result.rowcount != 1 or skill_result.rowcount != 1:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog candidate changed during review",
                )
            after = {"skill_status": "published", "revision_status": "published"}
        elif decision == "reject":
            revision_result = self.db.exec(
                update(GeneralSkillRevision)
                .where(
                    GeneralSkillRevision.id == revision.id,
                    GeneralSkillRevision.row_version == revision.row_version,
                    GeneralSkillRevision.status.in_(["draft", "reviewing"]),
                )
                .values(
                    status="rejected",
                    row_version=GeneralSkillRevision.row_version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            skill_result = self.db.exec(
                update(GeneralSkill)
                .where(
                    GeneralSkill.id == skill.id,
                    GeneralSkill.row_version == skill.row_version,
                    GeneralSkill.status.in_(["draft", "reviewing"]),
                    GeneralSkill.current_published_revision_id.is_(None),
                )
                .values(
                    status="archived",
                    metadata_json=metadata,
                    row_version=GeneralSkill.row_version + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if revision_result.rowcount != 1 or skill_result.rowcount != 1:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                    "catalog candidate changed during review",
                )
            after = {"skill_status": "archived", "revision_status": "rejected"}
        else:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_REVIEW_DECISION_INVALID",
                "catalog review decision is invalid",
                400,
            )
        append_management_audit(
            self.db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_display_name=self._actor_display_name(actor_user_id),
            action="general_skill.catalog.review",
            action_kind=decision,
            outcome="success",
            resource_type="general_skill",
            resource_id=skill.id,
            request_id=command_id,
            before=before,
            after=after,
            detail={
                "revision_id": revision.id,
                "review_note": item.get("review_note"),
            },
        )
        return {
            "skill_id": skill.id,
            "revision_id": revision.id,
            "decision": decision,
            "skill_status": after["skill_status"],
            "revision_status": after["revision_status"],
        }

    def _published_catalog_skill(self, operator_tenant_id: str, skill_id: str) -> GeneralSkill:
        """只返回已审核且属于项目目录的 Skill，租户参数仅用于审计上下文。"""

        del operator_tenant_id

        skill = self.db.get(GeneralSkill, skill_id)
        if (
            skill is None
            or skill.catalog_scope != "platform"
            or skill.tenant_id is not None
            or skill.status != "published"
            or (skill.metadata_json or {}).get("managed_catalog") is not True
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_NOT_AVAILABLE",
                "published catalog skill is unavailable",
                404,
            )
        if not (skill.metadata_json or {}).get("catalog_key"):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog skill has no stable catalog key",
            )
        return skill

    def _target_agent(
        self,
        current_user: User,
        agent_id: str,
        mode: CatalogBindingMode,
    ) -> AgentProfile:
        """按安装/绑定语义校验目标 Agent 的租户、所有权和组织治理事实。"""

        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != current_user.tenant_id
            or agent.is_overall
            or agent.status != "active"
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_TARGET_UNAVAILABLE",
                "target agent is unavailable",
                404,
            )
        if mode == "install":
            if agent_owner_user_id(agent) != current_user.id:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_INSTALL_FORBIDDEN",
                    "only the capability avatar owner can install a catalog skill",
                    403,
                )
            return agent
        if current_user.role != "admin":
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_BIND_FORBIDDEN",
                "only an administrator can bind a catalog skill to an organization employee",
                403,
            )
        if not agent.responsible_org_unit_id:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_ORGANIZATION_REQUIRED",
                "organization employee requires a responsible organization",
            )
        organization = self.db.get(OrganizationUnit, agent.responsible_org_unit_id)
        if (
            organization is None
            or organization.tenant_id != current_user.tenant_id
            or organization.status != "active"
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_ORGANIZATION_REQUIRED",
                "organization employee requires an active responsible organization",
            )
        role_bindings = self.db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.tenant_id == current_user.tenant_id,
                AgentRoleBinding.agent_id == agent.id,
                AgentRoleBinding.status == "active",
            )
        ).all()
        valid_role_binding = False
        for binding in role_bindings:
            role = self.db.get(BusinessRole, binding.business_role_id)
            supervisor = (
                self.db.get(EmployeeProfile, binding.supervisor_employee_profile_id)
                if binding.supervisor_employee_profile_id
                else None
            )
            if (
                role is not None
                and role.tenant_id == current_user.tenant_id
                and role.status == "active"
                and role.role_kind == "business"
                and supervisor is not None
                and supervisor.tenant_id == current_user.tenant_id
                and supervisor.status == "active"
            ):
                valid_role_binding = True
                break
        if not valid_role_binding:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_SUPERVISOR_REQUIRED",
                "organization employee requires an active business role and supervisor",
            )
        active_release = self.db.exec(
            select(PublicationRelease).where(
                PublicationRelease.tenant_id == current_user.tenant_id,
                PublicationRelease.resource_type == "agent",
                PublicationRelease.resource_id == agent.id,
                PublicationRelease.status == "active",
            )
        ).first()
        if active_release is None:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_AGENT_RELEASE_REQUIRED",
                "organization employee requires an active publication release",
            )
        return agent

    def _binding_revision(
        self,
        skill: GeneralSkill,
        revision_policy: str,
        pinned_revision_id: str | None,
    ) -> GeneralSkillRevision:
        """按 pinned/follow_latest 解析已发布修订并校验平台范围。"""

        if revision_policy not in {"pinned", "follow_latest"}:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_REVISION_INVALID",
                "revision policy is invalid",
                400,
            )
        revision_id = pinned_revision_id if revision_policy == "pinned" else skill.current_published_revision_id
        if not revision_id:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_REVISION_UNAVAILABLE",
                "published catalog revision is unavailable",
            )
        revision = self.db.get(GeneralSkillRevision, revision_id)
        allowed = {"published", "superseded"} if revision_policy == "pinned" else {"published"}
        if (
            revision is None
            or revision.catalog_scope != "platform"
            or revision.tenant_id is not None
            or revision.skill_id != skill.id
            or revision.status not in allowed
        ):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_REVISION_UNAVAILABLE",
                "requested catalog revision is unavailable",
            )
        return revision

    def _latest_revision(self, skill: GeneralSkill) -> GeneralSkillRevision | None:
        """读取目录 Skill 最大修订号，避免把 JSON 元数据当作审核事实。"""

        return self.db.exec(
            select(GeneralSkillRevision)
            .where(
                GeneralSkillRevision.catalog_scope == "platform",
                GeneralSkillRevision.tenant_id.is_(None),
                GeneralSkillRevision.skill_id == skill.id,
            )
            .order_by(GeneralSkillRevision.revision_number.desc())
        ).first()

    def _ensure_admin(self, tenant_id: str, actor_user_id: str) -> None:
        """验证批审操作者与租户管理员身份。"""

        actor = self.db.get(User, actor_user_id)
        if actor is None or actor.tenant_id != tenant_id or actor.role != "admin":
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_FORBIDDEN",
                "only a tenant administrator can review catalog",
                403,
            )

    def _actor_display_name(self, actor_user_id: str) -> str | None:
        """读取脱敏审计显示名。"""

        actor = self.db.get(User, actor_user_id)
        return (actor.display_name or actor.username) if actor else None

    @staticmethod
    def _command_id(value: str) -> str:
        """拒绝空白、过长和控制字符命令号。"""

        normalized = value.strip()
        if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_COMMAND_INVALID",
                "catalog command id is invalid",
                400,
            )
        return normalized

    @staticmethod
    def _normalize_review_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """稳定化审核输入并拒绝重复 Skill，保证同命令 checksum 唯一。"""

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            skill_id = str(item.get("skill_id") or "").strip()
            if not skill_id or skill_id in seen:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_REVIEW_DUPLICATE_SKILL",
                    "review batch contains duplicate or empty skill",
                    400,
                )
            decision = str(item.get("decision") or "")
            if decision not in {"approve", "reject"}:
                raise CatalogGovernanceError(
                    "GENERAL_SKILL_CATALOG_REVIEW_DECISION_INVALID",
                    "catalog review decision is invalid",
                    400,
                )
            note = item.get("review_note")
            normalized.append(
                {
                    "skill_id": skill_id,
                    "decision": decision,
                    "expected_skill_row_version": int(item.get("expected_skill_row_version") or 0),
                    "expected_revision_row_version": int(item.get("expected_revision_row_version") or 0),
                    "review_note": str(note).strip() if note else None,
                }
            )
            seen.add(skill_id)
        if not normalized:
            raise CatalogGovernanceError(
                "GENERAL_SKILL_CATALOG_REVIEW_EMPTY",
                "review batch is empty",
                400,
            )
        return sorted(normalized, key=lambda item: item["skill_id"])

    @staticmethod
    def _revision_checksum_valid(revision: GeneralSkillRevision) -> bool:
        """用不可变资源清单复核内容 checksum，阻断被改写的候选发布。"""

        resources = revision.resource_manifest_json or []
        if not resources:
            return revision.source_snapshot_json.get("source_kind") == "legacy_backfill"
        normalized: list[dict[str, str]] = []
        for resource in resources:
            path = resource.get("relative_path") or resource.get("path")
            checksum = resource.get("content_checksum") or resource.get("checksum")
            if not isinstance(path, str) or not isinstance(checksum, str):
                return False
            normalized.append({"path": path, "checksum": checksum})
        normalized.sort(key=lambda item: item["path"])
        return _checksum(normalized) == revision.content_checksum

    @staticmethod
    def _guidance_snapshot(skill: GeneralSkill) -> dict[str, Any]:
        """生成已发布 Skill 的兼容指导快照，不把 binding 授权混入能力正文。"""

        return {
            "schema_version": "1",
            "id": skill.id,
            "tenant_id": skill.tenant_id,
            "slug": skill.slug,
            "name": skill.name,
            "description": skill.description,
            "usage_mode": skill.usage_mode,
            "skill_markdown": skill.skill_markdown,
            "skill_files": list(skill.skill_files_json or []),
            "permissions": dict(skill.permissions_json or {}),
            "runtime_config": dict(skill.runtime_config_json or {}),
        }

    def _guidance_checksum(self, skill: GeneralSkill) -> str:
        """用动态能力目录相同的严格 JSON 规则生成指导快照 checksum。"""

        return capability_checksum(self._guidance_snapshot(skill))


def _checksum(value: object) -> str:
    """对审核命令与资源清单生成跨 SQLite/MySQL 一致的规范 checksum。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _review_result_from_command(
    command: GeneralSkillCatalogCommand,
    *,
    replayed: bool,
) -> CatalogReviewResult:
    """从已持久化命令恢复批审结果，不重新修改业务状态。"""

    result = command.result_json or {}
    items = tuple(dict(item) for item in result.get("items", []) if isinstance(item, dict))
    return CatalogReviewResult(
        command_id=command.command_id,
        replayed=replayed,
        approved_count=int(result.get("approved_count", 0)),
        rejected_count=int(result.get("rejected_count", 0)),
        items=items,
    )


def _lifecycle_result_from_command(
    command: GeneralSkillCatalogCommand,
    *,
    replayed: bool,
) -> CatalogLifecycleResult:
    """从已持久化命令恢复平台 Skill 生命周期结果，不重复停用绑定。"""

    result = command.result_json or {}
    return CatalogLifecycleResult(
        command_id=command.command_id,
        replayed=replayed,
        action=str(result.get("action") or "archive"),
        skill_id=str(result.get("skill_id") or ""),
        slug=str(result.get("slug") or ""),
        skill_status=str(result.get("skill_status") or "archived"),
        revision_id=str(result.get("revision_id") or ""),
        revision_status=str(result.get("revision_status") or "published"),
        skill_row_version=int(result.get("skill_row_version") or 1),
        revision_row_version=int(result.get("revision_row_version") or 1),
        deactivated_binding_count=int(result.get("deactivated_binding_count") or 0),
    )
