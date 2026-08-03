"""
@Time       : 2026/07/22 09:52
@Author     : zhanglp8181
@File       : work_items.py
@CallChain  : Coordinator/任务箱 API → SopWorkItemService → 工作项状态机/SQLModel
@Description: 解析业务角色候选快照并处理人工工作项的认领、释放和幂等结构化决定。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    MemberOrgAssignment,
    OrganizationUnit,
    SopInstance,
    SopNodeExecution,
    SopWorkItem,
    SopWorkItemCandidate,
    SopWorkItemCommandReceipt,
    SopWorkItemDecision,
    User,
    utc_now,
)
from app.organization.permissions import user_permission_codes
from app.organization.query import (
    current_assignment_predicates,
    resolve_organization_subtree_ids,
)
from app.organization.roles import role_source_codes
from app.sop_runtime.contracts import CompletionMode, WorkItemStatus
from app.sop_runtime.definition import (
    HumanTaskConfig,
    HumanTaskKind,
    ParticipantScopeResolver,
)
from app.sop_runtime.state_machine import RevisionConflictError, transition_work_item


ACTIVE_WORK_ITEM_STATUSES = (
    WorkItemStatus.OFFERED.value,
    WorkItemStatus.CLAIMED.value,
)


class WorkItemError(ValueError):
    """人工工作项命令违反候选资格、状态或定义契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和可安全展示的错误说明。"""

        self.code = code
        super().__init__(message)


class SopWorkItemService:
    """维护人工工作项聚合，并确保候选解析和所有写命令受租户边界保护。"""

    def __init__(self, db: Session) -> None:
        """绑定数据库事务，提交与回滚由 API 或 Coordinator 调用方控制。"""

        self.db = db

    def offer(
        self,
        instance: SopInstance,
        execution: SopNodeExecution,
        config: HumanTaskConfig,
        *,
        initiator_user_id: str | None,
    ) -> tuple[SopWorkItem, bool]:
        """按定义和当前有效任职解析去重候选，并幂等创建工作项快照。"""

        self._assert_execution_owner(instance, execution)
        if config.kind is not HumanTaskKind.STRUCTURED_WORK_ITEM:
            raise WorkItemError(
                "WORK_ITEM_CONFIG_INVALID",
                "只有结构化人工任务可以创建工作项。",
            )
        existing = self.db.exec(
            select(SopWorkItem).where(
                SopWorkItem.tenant_id == instance.tenant_id,
                SopWorkItem.node_execution_id == execution.id,
            )
        ).first()
        if existing is not None:
            return existing, False

        scope_snapshot = self._resolve_participant_scope(
            tenant_id=instance.tenant_id,
            config=config,
            initiator_user_id=initiator_user_id,
        )
        candidate_sources = self._resolve_candidate_sources(
            tenant_id=instance.tenant_id,
            role_codes=config.candidate_role_codes,
            user_ids=config.candidate_user_ids,
            organization_unit_ids=set(scope_snapshot["organization_unit_ids"])
            if scope_snapshot["organization_unit_ids"]
            else None,
        )
        if config.exclude_initiator and initiator_user_id:
            candidate_sources.pop(initiator_user_id, None)
        if not candidate_sources:
            raise WorkItemError(
                "WORK_ITEM_NO_ELIGIBLE_CANDIDATE",
                "当前人工任务没有符合条件且不违反职责分离的候选人。",
            )

        identity = (instance.context_json or {}).get("identity")
        identity_context = identity if isinstance(identity, Mapping) else {}
        completion_policy = config.completion_policy
        expires_at = (
            utc_now() + timedelta(seconds=config.timeout_policy.timeout_seconds)
            if config.timeout_policy is not None
            else None
        )
        candidate_snapshot = [
            {
                "user_id": user_id,
                "employee_profile_id": source.get("employee_profile_id"),
                "source_role_codes": sorted(source["role_codes"]),
                "source_types": sorted(source["source_types"]),
            }
            for user_id, source in sorted(candidate_sources.items())
        ]
        work_item = SopWorkItem(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_execution_id=execution.id,
            skill_version_id=instance.skill_version_id,
            node_id=execution.node_id,
            initiator_user_id=initiator_user_id,
            subject_employee_profile_id=str(
                identity_context.get("subject_employee_profile_id") or ""
            )
            or None,
            completion_mode=completion_policy.mode.value,
            claim_required=completion_policy.claim_required,
            required_count=completion_policy.required_count,
            exclude_initiator=config.exclude_initiator,
            allowed_outcomes_json=list(config.allowed_outcomes),
            outcome_options_json=[
                option.model_dump(mode="json") for option in config.outcome_options
            ],
            action_permissions_json=dict(config.action_permissions),
            candidate_snapshot_json=candidate_snapshot,
            participant_scope_snapshot_json=scope_snapshot,
            expires_at=expires_at,
            timeout_action=(
                config.timeout_policy.action.value if config.timeout_policy is not None else "fail"
            ),
        )
        self.db.add(work_item)
        self.db.flush()
        for snapshot in candidate_snapshot:
            self.db.add(
                SopWorkItemCandidate(
                    tenant_id=instance.tenant_id,
                    work_item_id=work_item.id,
                    user_id=str(snapshot["user_id"]),
                    employee_profile_id=(
                        str(snapshot["employee_profile_id"])
                        if snapshot.get("employee_profile_id")
                        else None
                    ),
                    source_role_codes_json=list(snapshot["source_role_codes"]),
                    source_types_json=list(snapshot["source_types"]),
                )
            )
        self.db.flush()
        return work_item, True

    def claim(
        self,
        work_item: SopWorkItem,
        *,
        actor_user_id: str,
        command_id: str,
        expected_revision: int | None = None,
    ) -> SopWorkItem:
        """由候选用户认领 offered 工作项，相同命令 ID 安全返回第一次结果。"""

        work_item = self._lock_work_item(work_item)
        replay = self._replayed_command(work_item, command_id, "claim", actor_user_id)
        if replay:
            return work_item
        self._assert_candidate(work_item, actor_user_id)
        self._assert_current_candidate_eligibility(work_item, actor_user_id)
        self._assert_action_permission(work_item, actor_user_id, "claim")
        if work_item.status == WorkItemStatus.CLAIMED.value:
            if work_item.assignee_user_id != actor_user_id:
                raise WorkItemError("WORK_ITEM_ALREADY_CLAIMED", "工作项已被其他候选人认领。")
            self._record_command(work_item, command_id, "claim", actor_user_id)
            return work_item
        if work_item.status != WorkItemStatus.OFFERED.value:
            raise WorkItemError("WORK_ITEM_NOT_ACTIVE", "工作项已经结束，不能再次认领。")
        transition = transition_work_item(
            WorkItemStatus.OFFERED,
            WorkItemStatus.CLAIMED,
            actual_revision=work_item.revision,
            expected_revision=expected_revision,
        )
        claimed_at = utc_now()
        result = self.db.execute(
            update(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == work_item.tenant_id,
                SopWorkItem.id == work_item.id,
                SopWorkItem.status == WorkItemStatus.OFFERED.value,
                SopWorkItem.revision == transition.previous_revision,
            )
            .values(
                status=transition.status.value,
                revision=transition.revision,
                assignee_user_id=actor_user_id,
                claimed_at=claimed_at,
                updated_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.expire(work_item)
        self.db.refresh(work_item)
        if result.rowcount != 1:
            if work_item.status == WorkItemStatus.CLAIMED.value:
                if work_item.assignee_user_id != actor_user_id:
                    raise WorkItemError(
                        "WORK_ITEM_ALREADY_CLAIMED",
                        "工作项已被其他候选人认领。",
                    )
                self._record_command(work_item, command_id, "claim", actor_user_id)
                return work_item
            if work_item.revision != transition.previous_revision:
                raise RevisionConflictError(
                    transition.previous_revision,
                    work_item.revision,
                )
            raise WorkItemError("WORK_ITEM_NOT_ACTIVE", "工作项已经结束，不能再次认领。")
        self._record_command(work_item, command_id, "claim", actor_user_id)
        self.db.flush()
        return work_item

    def unclaim(
        self,
        work_item: SopWorkItem,
        *,
        actor_user_id: str,
        command_id: str,
        expected_revision: int | None = None,
    ) -> SopWorkItem:
        """仅允许当前 assignee 释放工作项，并保留原候选快照。"""

        work_item = self._lock_work_item(work_item)
        replay = self._replayed_command(work_item, command_id, "unclaim", actor_user_id)
        if replay:
            return work_item
        if (
            work_item.status != WorkItemStatus.CLAIMED.value
            or work_item.assignee_user_id != actor_user_id
        ):
            raise WorkItemError(
                "WORK_ITEM_NOT_ASSIGNEE",
                "只有当前实际处理人可以释放工作项。",
            )
        self._assert_action_permission(work_item, actor_user_id, "unclaim")
        transition = transition_work_item(
            WorkItemStatus.CLAIMED,
            WorkItemStatus.OFFERED,
            actual_revision=work_item.revision,
            expected_revision=expected_revision,
        )
        work_item.status = transition.status.value
        work_item.revision = transition.revision
        work_item.assignee_user_id = None
        work_item.claimed_at = None
        work_item.updated_at = utc_now()
        self.db.add(work_item)
        self._record_command(work_item, command_id, "unclaim", actor_user_id)
        self.db.flush()
        return work_item

    def complete(
        self,
        work_item: SopWorkItem,
        *,
        actor_user_id: str,
        command_id: str,
        outcome: str,
        comment: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[SopWorkItem, bool]:
        """记录候选人的结构化决定，并在满足完成门槛时结束工作项。"""

        work_item = self._lock_work_item(work_item)
        replay = self._replayed_command(work_item, command_id, "complete", actor_user_id)
        if replay:
            return work_item, work_item.status == WorkItemStatus.COMPLETED.value
        if work_item.status not in ACTIVE_WORK_ITEM_STATUSES:
            raise WorkItemError("WORK_ITEM_NOT_ACTIVE", "工作项已经结束，不能重复处理。")
        self._assert_candidate(work_item, actor_user_id)
        self._assert_current_candidate_eligibility(work_item, actor_user_id)
        if work_item.exclude_initiator and work_item.initiator_user_id == actor_user_id:
            raise WorkItemError("WORK_ITEM_SELF_APPROVAL_FORBIDDEN", "申请人不能处理自己的工作项。")
        if work_item.claim_required and work_item.assignee_user_id != actor_user_id:
            raise WorkItemError("WORK_ITEM_CLAIM_REQUIRED", "请先认领该工作项再处理。")
        if work_item.assignee_user_id and work_item.assignee_user_id != actor_user_id:
            raise WorkItemError("WORK_ITEM_NOT_ASSIGNEE", "工作项已由其他处理人认领。")
        normalized_outcome = outcome.strip()
        if normalized_outcome not in set(work_item.allowed_outcomes_json or []):
            raise WorkItemError("WORK_ITEM_OUTCOME_INVALID", "提交的处理结果不在允许范围内。")
        self._assert_action_permission(
            work_item,
            actor_user_id,
            f"outcome:{normalized_outcome}",
        )
        outcome_option = self._outcome_option(work_item, normalized_outcome)
        normalized_comment = (comment or "").strip()[:10000] or None
        if outcome_option.get("comment_required") and normalized_comment is None:
            raise WorkItemError(
                "WORK_ITEM_COMMENT_REQUIRED",
                "当前办理结果必须填写处理说明。",
            )
        if expected_revision is not None and expected_revision != work_item.revision:
            raise RevisionConflictError(expected_revision, work_item.revision)

        existing_decision = self.db.exec(
            select(SopWorkItemDecision).where(
                SopWorkItemDecision.tenant_id == work_item.tenant_id,
                SopWorkItemDecision.work_item_id == work_item.id,
                SopWorkItemDecision.actor_user_id == actor_user_id,
            )
        ).first()
        if existing_decision is not None:
            raise WorkItemError(
                "WORK_ITEM_ACTOR_ALREADY_DECIDED",
                "当前处理人已经提交过该工作项决定。",
            )
        decision = SopWorkItemDecision(
            tenant_id=work_item.tenant_id,
            work_item_id=work_item.id,
            actor_user_id=actor_user_id,
            outcome=normalized_outcome,
            comment=normalized_comment,
            idempotency_key=self._decision_idempotency_key(
                work_item.tenant_id,
                work_item.id,
                command_id,
            ),
        )
        self.db.add(decision)
        self.db.flush()
        completed, final_outcome = self._completion_result(work_item)
        if completed:
            transition = transition_work_item(
                WorkItemStatus(work_item.status),
                WorkItemStatus.COMPLETED,
                actual_revision=work_item.revision,
            )
            work_item.status = transition.status.value
            work_item.revision = transition.revision
            work_item.outcome = final_outcome
            work_item.comment = decision.comment
            work_item.completed_at = utc_now()
        else:
            work_item.revision += 1
        work_item.updated_at = utc_now()
        self.db.add(work_item)
        self._record_command(
            work_item,
            command_id,
            "complete",
            actor_user_id,
            result={"completed": completed, "outcome": final_outcome},
        )
        self.db.flush()
        return work_item, completed

    @staticmethod
    def _outcome_option(work_item: SopWorkItem, outcome: str) -> dict[str, object]:
        """从创建时冻结快照读取结果选项，旧工作项返回安全兼容选项。"""

        for raw_option in work_item.outcome_options_json or []:
            if isinstance(raw_option, Mapping) and raw_option.get("value") == outcome:
                return dict(raw_option)
        return {
            "value": outcome,
            "label": outcome,
            "tone": "primary",
            "comment_required": False,
            "completion_message": "人工任务已处理，流程已继续执行。",
        }

    def expire_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[SopWorkItem]:
        """锁定并过期到达截止时间的活动工作项，供后台扫描器幂等消费。"""

        deadline = now or utc_now()
        candidates = self.db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.status.in_(ACTIVE_WORK_ITEM_STATUSES),
                SopWorkItem.expires_at.is_not(None),
                SopWorkItem.expires_at <= deadline,
            )
            .order_by(SopWorkItem.expires_at)
            .limit(max(1, min(limit, 1000)))
        ).all()
        expired: list[SopWorkItem] = []
        for candidate in candidates:
            work_item = self._lock_work_item(candidate)
            if (
                work_item.status not in ACTIVE_WORK_ITEM_STATUSES
                or work_item.expires_at is None
                or work_item.expires_at > deadline
            ):
                continue
            transition = transition_work_item(
                WorkItemStatus(work_item.status),
                WorkItemStatus.EXPIRED,
                actual_revision=work_item.revision,
            )
            work_item.status = transition.status.value
            work_item.revision = transition.revision
            work_item.expired_at = deadline
            work_item.updated_at = deadline
            self.db.add(work_item)
            expired.append(work_item)
        self.db.flush()
        return expired

    def candidates(self, work_item: SopWorkItem) -> list[SopWorkItemCandidate]:
        """返回工作项创建时冻结的去重候选用户列表。"""

        return self.db.exec(
            select(SopWorkItemCandidate)
            .where(
                SopWorkItemCandidate.tenant_id == work_item.tenant_id,
                SopWorkItemCandidate.work_item_id == work_item.id,
            )
            .order_by(SopWorkItemCandidate.user_id)
        ).all()

    def decisions(self, work_item: SopWorkItem) -> list[SopWorkItemDecision]:
        """按提交时间返回工作项已经接受的不同处理人决定。"""

        return self.db.exec(
            select(SopWorkItemDecision)
            .where(
                SopWorkItemDecision.tenant_id == work_item.tenant_id,
                SopWorkItemDecision.work_item_id == work_item.id,
            )
            .order_by(SopWorkItemDecision.created_at)
        ).all()

    def preview_candidate_sources(
        self,
        *,
        tenant_id: str,
        role_codes: tuple[str, ...],
        user_ids: tuple[str, ...],
        organization_unit_ids: set[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        """只读解析候选用户及授权来源，供发布预检复用运行时同一资格口径。

        参数：
            tenant_id: 当前租户，所有用户、角色和组织过滤都受此边界限制。
            role_codes: SOP 人工节点声明的活动业务角色编码。
            user_ids: SOP 人工节点显式声明的候选登录用户 ID。
            organization_unit_ids: 可选的已解析组织范围；为空表示租户范围。
        返回：
            以登录用户 ID 为键、包含员工档案、角色和来源类型的只读投影。
        """

        return self._resolve_candidate_sources(
            tenant_id=tenant_id,
            role_codes=role_codes,
            user_ids=user_ids,
            organization_unit_ids=organization_unit_ids,
        )

    def _resolve_candidate_sources(
        self,
        *,
        tenant_id: str,
        role_codes: tuple[str, ...],
        user_ids: tuple[str, ...],
        organization_unit_ids: set[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        """把直接用户和有效业务任职合并为按用户去重的候选来源。"""

        if organization_unit_ids is not None:
            organization_unit_ids = set(
                self.db.exec(
                    select(OrganizationUnit.id).where(
                        OrganizationUnit.tenant_id == tenant_id,
                        OrganizationUnit.id.in_(organization_unit_ids),
                        OrganizationUnit.status == "active",
                    )
                ).all()
            )
        users = self.db.exec(
            select(User).where(
                User.tenant_id == tenant_id,
                User.membership_status == "active",
            )
        ).all()
        users_by_id = {user.id: user for user in users}
        sources: dict[str, dict[str, object]] = {}
        profiles = self.db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == tenant_id,
                EmployeeProfile.status == "active",
            )
        ).all()
        profiles_by_user_id = {profile.user_id: profile for profile in profiles}
        scoped_profile_ids: set[str] | None = None
        if organization_unit_ids is not None:
            scoped_profile_ids = set(
                self.db.exec(
                    select(MemberOrgAssignment.employee_profile_id).where(
                        MemberOrgAssignment.tenant_id == tenant_id,
                        MemberOrgAssignment.org_unit_id.in_(organization_unit_ids),
                        *current_assignment_predicates(),
                    )
                ).all()
            )
        for user_id in user_ids:
            profile = profiles_by_user_id.get(user_id)
            if (
                user_id not in users_by_id
                or profile is None
                or scoped_profile_ids is not None
                and profile.id not in scoped_profile_ids
            ):
                continue
            sources[user_id] = {
                "employee_profile_id": profile.id,
                "role_codes": set(),
                "source_types": {"direct_user"},
            }
        if not role_codes:
            return sources
        roles = self.db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == tenant_id,
                BusinessRole.role_code.in_(role_codes),
                BusinessRole.role_kind == "business",
                BusinessRole.status == "active",
            )
        ).all()
        roles_by_id = {role.id: role for role in roles}
        if set(role_codes) != {role.role_code for role in roles}:
            raise WorkItemError(
                "WORK_ITEM_ROLE_NOT_FOUND",
                "人工任务引用了不存在或已停用的业务角色。",
            )
        now = utc_now()
        requested_role_ids = set(roles_by_id)
        for profile in profiles:
            if profile.user_id not in users_by_id:
                continue
            if scoped_profile_ids is not None and profile.id not in scoped_profile_ids:
                continue
            role_sources = role_source_codes(
                self.db,
                tenant_id=tenant_id,
                employee_profile_id=profile.id,
                role_ids=requested_role_ids,
                at=now,
                organization_unit_ids=organization_unit_ids,
            )
            if not role_sources:
                continue
            source = sources.setdefault(
                profile.user_id,
                {
                    "employee_profile_id": profile.id,
                    "role_codes": set(),
                    "source_types": set(),
                },
            )
            role_code_set = source["role_codes"]
            source_type_set = source["source_types"]
            if isinstance(role_code_set, set) and isinstance(source_type_set, set):
                for role_id, role_source_types in role_sources.items():
                    role_code_set.add(roles_by_id[role_id].role_code)
                    source_type_set.update(role_source_types)
        return sources

    def _resolve_participant_scope(
        self,
        *,
        tenant_id: str,
        config: HumanTaskConfig,
        initiator_user_id: str | None,
    ) -> dict[str, object]:
        """把定义中的唯一 resolver 解析成工作项级不可变组织集合。"""

        resolver = config.participant_scope_resolver
        if resolver is ParticipantScopeResolver.TENANT:
            return {
                "schema_version": 1,
                "resolver": resolver.value,
                "root_org_unit_id": None,
                "organization_unit_ids": [],
            }
        root_org_unit_id = config.participant_scope_org_unit_id
        include_descendants = resolver in {
            ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE,
            ParticipantScopeResolver.EXPLICIT_ORG,
        }
        if resolver in {
            ParticipantScopeResolver.INITIATOR_PRIMARY_ORG,
            ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE,
        }:
            if not initiator_user_id:
                raise WorkItemError(
                    "WORK_ITEM_INITIATOR_ORG_REQUIRED",
                    "当前人工任务需要发起人的有效主组织。",
                )
            profile = self.db.exec(
                select(EmployeeProfile).where(
                    EmployeeProfile.tenant_id == tenant_id,
                    EmployeeProfile.user_id == initiator_user_id,
                    EmployeeProfile.status == "active",
                )
            ).first()
            if profile is None:
                raise WorkItemError(
                    "WORK_ITEM_INITIATOR_ORG_REQUIRED",
                    "当前人工任务需要发起人的有效主组织。",
                )
            primary = self.db.exec(
                select(MemberOrgAssignment)
                .where(
                    MemberOrgAssignment.tenant_id == tenant_id,
                    MemberOrgAssignment.employee_profile_id == profile.id,
                    MemberOrgAssignment.is_primary.is_(True),
                    *current_assignment_predicates(),
                )
                .order_by(MemberOrgAssignment.effective_from.desc())
            ).first()
            root_org_unit_id = primary.org_unit_id if primary is not None else None
        if not root_org_unit_id:
            raise WorkItemError(
                "WORK_ITEM_SCOPE_ORG_NOT_FOUND",
                "人工任务组织范围不存在或当前不可用。",
            )
        root = self.db.exec(
            select(OrganizationUnit).where(
                OrganizationUnit.tenant_id == tenant_id,
                OrganizationUnit.id == root_org_unit_id,
                OrganizationUnit.status == "active",
            )
        ).first()
        if root is None:
            raise WorkItemError(
                "WORK_ITEM_SCOPE_ORG_NOT_FOUND",
                "人工任务组织范围不存在或当前不可用。",
            )
        try:
            organization_unit_ids = resolve_organization_subtree_ids(
                self.db,
                tenant_id=tenant_id,
                root_org_unit_id=root_org_unit_id,
                include_descendants=include_descendants,
            )
        except ValueError as error:
            raise WorkItemError(
                "WORK_ITEM_SCOPE_ORG_NOT_FOUND",
                "人工任务组织范围不存在或当前不可用。",
            ) from error
        active_organization_unit_ids = set(
            self.db.exec(
                select(OrganizationUnit.id).where(
                    OrganizationUnit.tenant_id == tenant_id,
                    OrganizationUnit.id.in_(organization_unit_ids),
                    OrganizationUnit.status == "active",
                )
            ).all()
        )
        organization_unit_ids = [
            org_unit_id
            for org_unit_id in organization_unit_ids
            if org_unit_id in active_organization_unit_ids
        ]
        return {
            "schema_version": 1,
            "resolver": resolver.value,
            "root_org_unit_id": root_org_unit_id,
            "organization_unit_ids": organization_unit_ids,
        }

    def is_current_candidate(self, work_item: SopWorkItem, user_id: str) -> bool:
        """判断冻结候选人是否仍满足新范围快照的实时资格。"""

        try:
            self._assert_candidate(work_item, user_id)
            self._assert_current_candidate_eligibility(work_item, user_id)
        except WorkItemError:
            return False
        return True

    def _assert_current_candidate_eligibility(
        self,
        work_item: SopWorkItem,
        user_id: str,
    ) -> None:
        """对 M3-B 新快照复核成员、组织归属和至少一条原候选来源。"""

        scope_snapshot = work_item.participant_scope_snapshot_json or {}
        if not scope_snapshot:
            return
        candidate_snapshot = next(
            (
                row
                for row in work_item.candidate_snapshot_json or []
                if str(row.get("user_id")) == user_id
            ),
            None,
        )
        if candidate_snapshot is None:
            raise WorkItemError("WORK_ITEM_NOT_CANDIDATE", "当前用户不是该工作项候选人。")
        source_types = {str(value) for value in candidate_snapshot.get("source_types") or []}
        sources = self._resolve_candidate_sources(
            tenant_id=work_item.tenant_id,
            role_codes=tuple(
                str(value) for value in candidate_snapshot.get("source_role_codes") or []
            ),
            user_ids=(user_id,) if "direct_user" in source_types else (),
            organization_unit_ids=set(scope_snapshot.get("organization_unit_ids") or [])
            if scope_snapshot.get("organization_unit_ids")
            else None,
        )
        if user_id not in sources:
            raise WorkItemError(
                "WORK_ITEM_CANDIDATE_NO_LONGER_ELIGIBLE",
                "当前员工已不再满足该工作项的成员、组织或业务角色资格。",
            )

    def _lock_work_item(self, work_item: SopWorkItem) -> SopWorkItem:
        """在写命令期间锁定并刷新聚合根，串行化 MySQL 上的竞争认领与决定。"""

        locked = self.db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == work_item.tenant_id,
                SopWorkItem.id == work_item.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if locked is None:
            raise WorkItemError("WORK_ITEM_NOT_FOUND", "工作项不存在或已被删除。")
        return locked

    def _completion_result(self, work_item: SopWorkItem) -> tuple[bool, str | None]:
        """按办理类型和完成模式计算结果；多人审批仍保持拒绝优先语义。"""

        decisions = self.decisions(work_item)
        mode = CompletionMode(work_item.completion_mode)
        approval_outcomes = set(work_item.allowed_outcomes_json or []) == {
            "approved",
            "rejected",
        }
        if not approval_outcomes and mode in {CompletionMode.SINGLE, CompletionMode.ANY}:
            first_decision = decisions[0] if decisions else None
            return (
                first_decision is not None,
                first_decision.outcome if first_decision is not None else None,
            )
        rejected = next(
            (decision for decision in decisions if decision.outcome == "rejected"),
            None,
        )
        if rejected is not None:
            return True, rejected.outcome
        approved_count = len([decision for decision in decisions if decision.outcome == "approved"])
        if mode in {CompletionMode.SINGLE, CompletionMode.ANY}:
            return approved_count >= 1, "approved" if approved_count else None
        candidate_count = len(self.candidates(work_item))
        if mode is CompletionMode.ALL:
            return (
                approved_count >= candidate_count,
                "approved" if approved_count >= candidate_count else None,
            )
        required_count = work_item.required_count or candidate_count
        return (
            approved_count >= required_count,
            "approved" if approved_count >= required_count else None,
        )

    def _assert_candidate(self, work_item: SopWorkItem, user_id: str) -> None:
        """在每个写入口按冻结候选快照复核实际用户资格。"""

        candidate = self.db.exec(
            select(SopWorkItemCandidate).where(
                SopWorkItemCandidate.tenant_id == work_item.tenant_id,
                SopWorkItemCandidate.work_item_id == work_item.id,
                SopWorkItemCandidate.user_id == user_id,
            )
        ).first()
        if candidate is None:
            raise WorkItemError("WORK_ITEM_NOT_CANDIDATE", "当前用户不是该工作项候选人。")

    def _assert_action_permission(
        self,
        work_item: SopWorkItem,
        user_id: str,
        action_key: str,
    ) -> None:
        """按工作项冻结的动作契约复核办理人当前有效业务权限。"""

        required_permission = (work_item.action_permissions_json or {}).get(action_key)
        if not required_permission:
            return
        effective_permissions = user_permission_codes(
            self.db,
            tenant_id=work_item.tenant_id,
            user_id=user_id,
            organization_unit_ids=set(
                (work_item.participant_scope_snapshot_json or {}).get(
                    "organization_unit_ids"
                )
                or []
            )
            or None,
        )
        if required_permission not in effective_permissions:
            raise WorkItemError(
                "WORK_ITEM_PERMISSION_REQUIRED",
                f"当前员工缺少办理动作所需权限：{required_permission}",
            )

    def _replayed_command(
        self,
        work_item: SopWorkItem,
        command_id: str,
        command_type: str,
        actor_user_id: str,
    ) -> SopWorkItemCommandReceipt | None:
        """识别已执行命令，并拒绝同一命令 ID 被不同语义或用户复用。"""

        receipt = self.db.exec(
            select(SopWorkItemCommandReceipt).where(
                SopWorkItemCommandReceipt.tenant_id == work_item.tenant_id,
                SopWorkItemCommandReceipt.command_id == command_id,
            )
        ).first()
        if receipt is None:
            return None
        if (
            receipt.work_item_id != work_item.id
            or receipt.command_type != command_type
            or receipt.actor_user_id != actor_user_id
        ):
            raise WorkItemError(
                "WORK_ITEM_COMMAND_ID_REUSED",
                "命令 ID 已被其他工作项、动作或用户使用。",
            )
        return receipt

    def _record_command(
        self,
        work_item: SopWorkItem,
        command_id: str,
        command_type: str,
        actor_user_id: str,
        *,
        result: Mapping[str, object] | None = None,
    ) -> SopWorkItemCommandReceipt:
        """在同一事务记录工作项命令结果，作为重试时的稳定回执。"""

        receipt = SopWorkItemCommandReceipt(
            tenant_id=work_item.tenant_id,
            work_item_id=work_item.id,
            command_id=command_id,
            command_type=command_type,
            actor_user_id=actor_user_id,
            aggregate_revision=work_item.revision,
            result_json={
                "work_item_id": work_item.id,
                "status": work_item.status,
                "revision": work_item.revision,
                **dict(result or {}),
            },
        )
        self.db.add(receipt)
        return receipt

    @staticmethod
    def _decision_idempotency_key(tenant_id: str, work_item_id: str, command_id: str) -> str:
        """为结构化决定生成数据库唯一的稳定 SHA-256 幂等键。"""

        return hashlib.sha256(f"{tenant_id}:{work_item_id}:{command_id}".encode()).hexdigest()

    @staticmethod
    def _assert_execution_owner(
        instance: SopInstance,
        execution: SopNodeExecution,
    ) -> None:
        """拒绝跨租户或跨实例组合工作项与节点执行。"""

        if instance.tenant_id != execution.tenant_id or instance.id != execution.instance_id:
            raise WorkItemError(
                "WORK_ITEM_EXECUTION_MISMATCH",
                "节点执行记录不属于指定 SOP 实例。",
            )
