"""
@Time       : 2026/08/28
@Author     : zhanglp8181
@File       : deletion.py
@CallChain  : Agent 管理 API → AgentDeletionService → Runtime 取消、资源销毁与控制面回收
@Description: 以墓碑优先、可重试的方式关闭数字员工，避免硬删除绕过执行和外部投递边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from sqlalchemy import and_, delete, or_
from sqlmodel import Session, select

from app.audit.service import append_management_audit
from app.db.models import (
    AgentConnectionBinding,
    AgentEvent,
    AgentKnowledgeBranch,
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    AgentUsage,
    ArtifactRendererJob,
    AttachmentUploadQuotaLease,
    AttachmentUploadQuotaReservation,
    ChatSession,
    ConnectorInboundEvent,
    ConnectorInboundRoute,
    ConnectorOutboundDelivery,
    ConnectorThreadBinding,
    DraftUploadBinding,
    ExecutionArtifact,
    GeneralSkillInstallIntent,
    GeneralSkillProposal,
    GeneralSkillUse,
    HumanHandoffRequest,
    ManagedInputResource,
    MemoryRecord,
    Message,
    MessageFeedback,
    MessageInputBindingLink,
    MessageInputResourceLink,
    PublicationRelease,
    ResourceSessionBinding,
    ScheduledTask,
    ScheduledTaskRun,
    SessionGeneralSkillOverride,
    SkillFeedback,
    SopInstance,
    StandingApprovalRule,
    TurnInputReadReceipt,
    TurnInputSnapshot,
    User,
    new_id,
    utc_now,
)
from app.session.managed_resources import (
    InputResourceAccessDenied,
    ManagedInputResourceService,
)
from app.session.upload_quotas import AttachmentUploadQuotaService
from app.sop_runtime.execution_store import (
    ACTIVE_INSTANCE_STATUSES,
    SopExecutionStore,
)


DELETION_METADATA_KEY = "agent_deletion"
DELETION_PENDING = "deletion_pending"
DELETION_COMPLETED = "deleted"
DELETION_RECONCILE_LIMIT = 50
DELETION_RECONCILE_STATES = {"deleting", DELETION_PENDING}
DELETION_LEASE_SECONDS = 5 * 60
logger = logging.getLogger(__name__)


def agent_deletion_state(agent: AgentProfile) -> str | None:
    """读取 Agent 的不可逆删除状态，供所有管理写入口共享墓碑判定。"""

    value = (agent.metadata_json or {}).get(DELETION_METADATA_KEY)
    if not isinstance(value, dict):
        return None
    state = str(value.get("state") or "").strip()
    return state if state in {"deleting", DELETION_PENDING, DELETION_COMPLETED} else None


@dataclass(frozen=True, slots=True)
class AgentDeletionResult:
    """描述一次 Agent 关闭尝试的可观察结果，供 API 和浏览器显示。"""

    status: str
    agent_id: str
    cleaned_session_count: int = 0
    pending_execution_ids: tuple[str, ...] = ()
    pending_resource_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """转换为不包含内部数据库对象的稳定响应载荷。"""

        return {
            "status": self.status,
            "agent_id": self.agent_id,
            "cleaned_session_count": self.cleaned_session_count,
            "pending_execution_ids": list(self.pending_execution_ids),
            "pending_resource_ids": list(self.pending_resource_ids),
        }


class AgentDeletionLeaseLost(RuntimeError):
    """表示删除阶段已失去租约，迟到执行者不得继续改写 Agent 及其关联事实。"""


class AgentDeletionService:
    """按墓碑、取消、控制面回收、会话清理顺序执行可重复的 Agent 删除。"""

    def __init__(self, db: Session) -> None:
        """绑定调用方事务会话；资源服务仍负责自己的 purge lease 与墓碑。"""

        self.db = db

    def delete(self, agent: AgentProfile, *, actor_user_id: str) -> AgentDeletionResult:
        """关闭一个非 Overall Agent，未能安全清理的执行或资源会保留待重试状态。"""

        locked_agent = self.db.exec(
            select(AgentProfile)
            .where(
                AgentProfile.tenant_id == agent.tenant_id,
                AgentProfile.id == agent.id,
            )
            .with_for_update()
        ).first()
        if locked_agent is None:
            raise ValueError("Agent does not exist")
        agent = locked_agent
        if agent.is_overall:
            raise ValueError("Overall agent cannot be deleted")
        deletion = self._deletion_metadata(agent)
        if deletion.get("state") == DELETION_COMPLETED:
            return AgentDeletionResult(status=DELETION_COMPLETED, agent_id=agent.id)
        if self._deletion_lease_is_active(deletion):
            return AgentDeletionResult(
                status=DELETION_PENDING,
                agent_id=agent.id,
                pending_execution_ids=self._pending_ids(deletion, "pending_execution_ids"),
                pending_resource_ids=self._pending_ids(deletion, "pending_resource_ids"),
            )
        deletion["lease_owner"] = new_id("agentdelete")
        deletion["lease_expires_at"] = (
            utc_now() + timedelta(seconds=DELETION_LEASE_SECONDS)
        ).isoformat()
        lease_owner = str(deletion["lease_owner"])

        actor = self.db.get(User, actor_user_id)
        append_management_audit(
            self.db,
            tenant_id=agent.tenant_id,
            actor_user_id=actor_user_id,
            actor_display_name=actor.username if actor is not None else actor_user_id,
            actor_type="system" if actor_user_id.startswith("system:") else "user",
            action="agent.delete.requested",
            action_kind="delete",
            outcome="pending",
            resource_type="agent_profile",
            resource_id=agent.id,
            before={"status": agent.status},
            after={"status": "archived", "deletion_state": "deleting"},
            detail={"lifecycle": "tombstone_first", "lease_owner": lease_owner},
        )
        self._tombstone_agent(agent, actor_user_id=actor_user_id, deletion=deletion)
        self.db.add(agent)
        self.db.commit()
        agent = self._assert_deletion_lease(agent, lease_owner)

        session_ids = self._agent_session_ids(agent)
        agent = self._assert_deletion_lease(agent, lease_owner)
        pending_execution_ids = self._cancel_active_executions(
            agent,
            session_ids=session_ids,
            actor_user_id=actor_user_id,
        )
        agent = self._assert_deletion_lease(agent, lease_owner)
        self._retire_control_plane(
            agent,
            actor_user_id=actor_user_id,
            session_ids=session_ids,
        )
        self.db.commit()
        agent = self._assert_deletion_lease(agent, lease_owner)

        pending_resource_ids: set[str] = set()
        cleaned_session_count = 0
        active_execution_ids = set(pending_execution_ids)
        if not active_execution_ids:
            for session in self._agent_sessions(agent):
                agent = self._assert_deletion_lease(agent, lease_owner)
                resource_ids, resource_pending = self._purge_session_resources(session)
                pending_resource_ids.update(resource_ids)
                agent = self._assert_deletion_lease(agent, lease_owner)
                if resource_pending:
                    continue
                self._purge_session_records(session)
                cleaned_session_count += 1
            self.db.commit()
            agent = self._assert_deletion_lease(agent, lease_owner)
            pending_resource_ids.update(self._purge_agent_resources(agent))
            agent = self._assert_deletion_lease(agent, lease_owner)
        else:
            pending_resource_ids.update(
                resource.id
                for resource in self._agent_resources(agent)
                if resource.destruction_status != "purged"
            )

        if pending_execution_ids or pending_resource_ids:
            agent = self._assert_deletion_lease(agent, lease_owner)
            self._set_deletion_state(
                agent,
                state=DELETION_PENDING,
                actor_user_id=actor_user_id,
                pending_execution_ids=pending_execution_ids,
                pending_resource_ids=sorted(pending_resource_ids),
            )
            self.db.add(agent)
            self.db.commit()
            return AgentDeletionResult(
                status=DELETION_PENDING,
                agent_id=agent.id,
                cleaned_session_count=cleaned_session_count,
                pending_execution_ids=tuple(sorted(pending_execution_ids)),
                pending_resource_ids=tuple(sorted(pending_resource_ids)),
            )

        agent = self._assert_deletion_lease(agent, lease_owner)
        self._set_deletion_state(
            agent,
            state=DELETION_COMPLETED,
            actor_user_id=actor_user_id,
            pending_execution_ids=(),
            pending_resource_ids=(),
        )
        self.db.add(agent)
        self.db.commit()
        return AgentDeletionResult(
            status=DELETION_COMPLETED,
            agent_id=agent.id,
            cleaned_session_count=cleaned_session_count,
        )

    def _assert_deletion_lease(
        self,
        agent: AgentProfile,
        lease_owner: str,
    ) -> AgentProfile:
        """按 tenant+Agent 锁定并核对删除租约，所有阶段提交前都必须重新 fencing。"""

        current = self.db.exec(
            select(AgentProfile)
            .where(
                AgentProfile.tenant_id == agent.tenant_id,
                AgentProfile.id == agent.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if current is None:
            raise AgentDeletionLeaseLost("AGENT_DELETION_AGENT_MISSING")
        deletion = self._deletion_metadata(current)
        if (
            deletion.get("state") != "deleting"
            or str(deletion.get("lease_owner") or "") != lease_owner
            or not self._deletion_lease_is_active(deletion)
        ):
            raise AgentDeletionLeaseLost("AGENT_DELETION_LEASE_LOST")
        return current

    @staticmethod
    def _pending_ids(deletion: dict[str, object], key: str) -> tuple[str, ...]:
        """从删除进度中读取仅含标识符的待处理集合。"""

        values = deletion.get(key)
        if not isinstance(values, (list, tuple, set)):
            return ()
        return tuple(sorted(str(value) for value in values if str(value).strip()))

    @staticmethod
    def _deletion_lease_is_active(deletion: dict[str, object]) -> bool:
        """判断删除工作租约是否仍有效，过期后允许后台继续收敛。"""

        if deletion.get("state") != "deleting" or not deletion.get("lease_owner"):
            return False
        try:
            expires_at = datetime.fromisoformat(str(deletion.get("lease_expires_at")))
        except (TypeError, ValueError):
            return False
        return expires_at > utc_now()

    def _tombstone_agent(
        self,
        agent: AgentProfile,
        *,
        actor_user_id: str,
        deletion: dict[str, object],
    ) -> None:
        """先把 Agent 变成不可用墓碑，阻断新聊天、调度和连接器流量。"""

        now = utc_now()
        if agent.status != "archived":
            agent.status = "archived"
            agent.profile_revision = max(int(agent.profile_revision or 1), 1) + 1
        agent.published_to_gallery = False
        agent.visibility_scope = "private"
        agent.gallery_published_at = None
        agent.gallery_published_by = None
        deletion.setdefault("requested_at", now.isoformat())
        deletion.setdefault("requested_by_user_id", actor_user_id)
        deletion["last_attempt_at"] = now.isoformat()
        deletion["state"] = "deleting"
        deletion.pop("last_error_code", None)
        agent.metadata_json = {
            **dict(agent.metadata_json or {}),
            "published_to_gallery": False,
            "hidden_from_product": True,
            DELETION_METADATA_KEY: deletion,
        }
        agent.updated_at = now

    def _cancel_active_executions(
        self,
        agent: AgentProfile,
        *,
        session_ids: set[str],
        actor_user_id: str,
    ) -> set[str]:
        """通过统一 fencing 取消活动 Execution，外部 unknown 只记录待对账而不硬删。"""

        conditions = [
            SopInstance.tenant_id == agent.tenant_id,
            SopInstance.status.in_(ACTIVE_INSTANCE_STATUSES),
            or_(
                SopInstance.agent_id == agent.id,
                SopInstance.session_id.in_(session_ids) if session_ids else False,
            ),
        ]
        instances = self.db.exec(select(SopInstance).where(*conditions)).all()
        pending: set[str] = set()
        for instance in instances:
            try:
                with self.db.begin_nested():
                    store = SopExecutionStore(self.db)
                    with store.owned_for_cancellation(
                        instance,
                        worker_id=f"agent-delete:{agent.id[-32:]}",
                        allow_archived_agent=True,
                    ):
                        settled = store.request_cancellation(
                            instance,
                            actor_user_id=actor_user_id,
                            reason="agent_deleted",
                        )
                if not settled:
                    pending.add(instance.id)
            except Exception:
                pending.add(instance.id)
        self.db.commit()
        return pending

    def _retire_control_plane(
        self,
        agent: AgentProfile,
        *,
        actor_user_id: str,
        session_ids: set[str] | None = None,
    ) -> None:
        """撤销绑定、发布、渠道、审批和调度控制面，但保留不可变执行审计。"""

        now = utc_now()
        for row in self._rows(AgentResourceBinding, agent.tenant_id, agent.id):
            row.status = "deleted"
            row.row_version += 1
            row.updated_at = now
            self.db.add(row)
        for row in self._rows(AgentRoleBinding, agent.tenant_id, agent.id):
            row.status = "deleted"
            row.updated_at = now
            self.db.add(row)
        for model in (AgentModelBinding, AgentUsage):
            for row in self._rows(model, agent.tenant_id, agent.id):
                self.db.delete(row)
        for model in (AgentSkillBranch, AgentSkillBranchVersion, AgentKnowledgeBranch):
            for row in self._rows(model, agent.tenant_id, agent.id):
                row.status = "deleted"
                row.updated_at = now
                self.db.add(row)

        for row in self._rows(AgentConnectionBinding, agent.tenant_id, agent.id):
            row.enabled = False
            row.revision += 1
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
            self.db.add(row)
        for row in self._rows(ConnectorInboundRoute, agent.tenant_id, agent.id):
            row.enabled = False
            row.revision += 1
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
            self.db.add(row)

        thread_ids = {
            row.id for row in self._rows(ConnectorThreadBinding, agent.tenant_id, agent.id)
        }
        for row in self._rows(ConnectorThreadBinding, agent.tenant_id, agent.id):
            row.status = "disabled"
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            self.db.add(row)
        if thread_ids:
            for row in self.db.exec(
                select(ConnectorOutboundDelivery).where(
                    ConnectorOutboundDelivery.tenant_id == agent.tenant_id,
                    ConnectorOutboundDelivery.thread_binding_id.in_(thread_ids),
                    ConnectorOutboundDelivery.status.in_(("pending", "delivering")),
                )
            ).all():
                row.status = "dead_letter" if row.status == "pending" else "unknown"
                row.lease_owner = None
                row.lease_until = None
                row.error_json = {"code": "AGENT_DELETED"}
                row.updated_at = now
                self.db.add(row)
            for row in self.db.exec(
                select(ConnectorInboundEvent).where(
                    ConnectorInboundEvent.tenant_id == agent.tenant_id,
                    ConnectorInboundEvent.thread_binding_id.in_(thread_ids),
                    ConnectorInboundEvent.status.in_(("pending", "processing")),
                )
            ).all():
                row.status = "dead_letter"
                row.lease_owner = None
                row.lease_until = None
                row.last_error_code = "AGENT_DELETED"
                row.updated_at = now
                self.db.add(row)

        for row in self.db.exec(
            select(PublicationRelease).where(
                PublicationRelease.tenant_id == agent.tenant_id,
                PublicationRelease.resource_type == "agent",
                PublicationRelease.resource_id == agent.id,
                PublicationRelease.status == "active",
            )
        ).all():
            row.status = "unpublished"
            row.terminal_by_user_id = actor_user_id
            row.terminal_reason = "agent_deleted"
            row.terminal_at = now
            row.updated_at = now
            self.db.add(row)

        for row in self.db.exec(
            select(StandingApprovalRule).where(
                StandingApprovalRule.tenant_id == agent.tenant_id,
                StandingApprovalRule.agent_id == agent.id,
                StandingApprovalRule.status == "active",
            )
        ).all():
            row.status = "revoked"
            row.revoked_by_user_id = actor_user_id
            row.revoked_at = now
            row.updated_at = now
            self.db.add(row)

        execution_ids = set(
            self.db.exec(
                select(SopInstance.id).where(
                    SopInstance.tenant_id == agent.tenant_id,
                    or_(
                        SopInstance.agent_id == agent.id,
                        SopInstance.session_id.in_(session_ids) if session_ids else False,
                    ),
                )
            ).all()
        )
        if execution_ids:
            for artifact in self.db.exec(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.tenant_id == agent.tenant_id,
                    ExecutionArtifact.execution_id.in_(execution_ids),
                    ExecutionArtifact.status == "ready",
                )
            ).all():
                artifact.status = "revoked"
                artifact.revoked_at = now
                artifact.updated_at = now
                self.db.add(artifact)
            for job in self.db.exec(
                select(ArtifactRendererJob).where(
                    ArtifactRendererJob.tenant_id == agent.tenant_id,
                    ArtifactRendererJob.execution_id.in_(execution_ids),
                    ArtifactRendererJob.status.not_in(
                        ("ready", "failed", "dead_letter", "cancelled")
                    ),
                )
            ).all():
                job.status = "cancelled"
                job.lease_owner = None
                job.lease_expires_at = None
                job.retry_at = None
                job.error_code = "AGENT_DELETED"
                job.updated_at = now
                self.db.add(job)

        for row in self.db.exec(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == agent.tenant_id,
                ScheduledTask.agent_id == agent.id,
                ScheduledTask.status == "active",
            )
        ).all():
            row.status = "paused"
            row.next_run_at = None
            row.lease_owner = None
            row.lease_until = None
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "agent_deletion": "agent_deleted",
            }
            row.updated_at = now
            self.db.add(row)
        for row in self.db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.tenant_id == agent.tenant_id,
                ScheduledTaskRun.agent_id == agent.id,
                ScheduledTaskRun.status.in_(("queued", "running", "waiting")),
            )
        ).all():
            row.status = "skipped"
            row.error = "AGENT_DELETED"
            row.finished_at = now
            row.updated_at = now
            self.db.add(row)

        for row in self.db.exec(
            select(HumanHandoffRequest).where(
                HumanHandoffRequest.tenant_id == agent.tenant_id,
                HumanHandoffRequest.agent_id == agent.id,
                HumanHandoffRequest.status == "pending",
            )
        ).all():
            row.status = "cancelled"
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "cancelled_reason": "agent_deleted",
            }
            row.updated_at = now
            self.db.add(row)

        for row in self._rows(GeneralSkillUse, agent.tenant_id, agent.id):
            if row.status in {"loading", "active"}:
                row.status = "cancelled"
                row.invalidation_reason = "agent_deleted"
                row.updated_at = now
                self.db.add(row)
        for row in self._rows(GeneralSkillProposal, agent.tenant_id, agent.id):
            if row.status in {"staged", "awaiting_approval", "publishing"}:
                row.status = "failed"
                row.error_code = "AGENT_DELETED"
                row.terminal_at = now
                row.updated_at = now
                self.db.add(row)
        for row in self._rows(GeneralSkillInstallIntent, agent.tenant_id, agent.id):
            if row.status not in {"cancelled", "failed", "expired", "stale"}:
                row.status = "cancelled"
                row.error_code = "AGENT_DELETED"
                row.terminal_at = now
                row.updated_at = now
                self.db.add(row)
        for row in self.db.exec(
            select(SessionGeneralSkillOverride).where(
                SessionGeneralSkillOverride.tenant_id == agent.tenant_id,
                SessionGeneralSkillOverride.agent_id == agent.id,
            )
        ).all():
            self.db.delete(row)

        upload_bindings = self._rows(DraftUploadBinding, agent.tenant_id, agent.id)
        quota_service = AttachmentUploadQuotaService(self.db)
        for row in upload_bindings:
            if row.status in {"active", "claimed"}:
                row.status = "expired"
                row.lease_owner = None
                row.updated_at = now
                self.db.add(row)
            reservations = self.db.exec(
                select(AttachmentUploadQuotaReservation).where(
                    AttachmentUploadQuotaReservation.tenant_id == agent.tenant_id,
                    AttachmentUploadQuotaReservation.binding_id == row.binding_id,
                    AttachmentUploadQuotaReservation.status == "active",
                )
            ).all()
            for reservation in reservations:
                quota_service.release_for_deletion(reservation)
            reservation_ids = [reservation.id for reservation in reservations]
            if reservation_ids:
                self.db.exec(
                    delete(AttachmentUploadQuotaLease).where(
                        AttachmentUploadQuotaLease.tenant_id == agent.tenant_id,
                        AttachmentUploadQuotaLease.reservation_id.in_(reservation_ids),
                    )
                )

        for row in self.db.exec(
            select(MemoryRecord).where(
                MemoryRecord.tenant_id == agent.tenant_id,
                MemoryRecord.agent_id == agent.id,
            )
        ).all():
            self.db.delete(row)

    def _purge_session_resources(self, session: ChatSession) -> tuple[set[str], bool]:
        """按受管资源服务清理会话附件，任何不确定性都阻止该会话删除。"""

        links = self.db.exec(
            select(MessageInputResourceLink).where(
                MessageInputResourceLink.tenant_id == session.tenant_id,
                MessageInputResourceLink.session_id == session.id,
            )
        ).all()
        bindings = self.db.exec(
            select(ResourceSessionBinding).where(
                ResourceSessionBinding.tenant_id == session.tenant_id,
                ResourceSessionBinding.session_id == session.id,
            )
        ).all()
        resource_keys = {
            (binding.resource_id, binding.resource_version) for binding in bindings
        }
        resource_keys.update((link.resource_id, link.resource_version) for link in links)
        pending: set[str] = set()
        for resource_id, resource_version in sorted(resource_keys):
            resource = self.db.get(ManagedInputResource, resource_id)
            if resource is None or resource.version != resource_version:
                pending.add(resource_id)
                continue
            if resource.destruction_status == "purged":
                continue
            binding = next(
                (
                    item
                    for item in bindings
                    if item.resource_id == resource_id and item.resource_version == resource_version
                ),
                None,
            )
            try:
                if binding is None:
                    pending.add(resource_id)
                    continue
                ManagedInputResourceService(self.db).purge_session_resource(
                    resource,
                    session_id=session.id,
                    actor_user_id=resource.owner_user_id,
                )
            except InputResourceAccessDenied:
                self.db.rollback()
                pending.add(resource_id)
        return pending, bool(pending)

    def _purge_agent_resources(self, agent: AgentProfile) -> set[str]:
        """清理没有会话绑定的草稿资源，已发送或状态不明的资源保留待重试。"""

        pending: set[str] = set()
        for resource in self._agent_resources(agent):
            if resource.destruction_status == "purged":
                continue
            links = self.db.exec(
                select(MessageInputResourceLink.id).where(
                    MessageInputResourceLink.tenant_id == resource.tenant_id,
                    MessageInputResourceLink.resource_id == resource.id,
                    MessageInputResourceLink.resource_version == resource.version,
                )
            ).first()
            if links is not None:
                pending.add(resource.id)
                continue
            try:
                ManagedInputResourceService(self.db).discard_unreferenced(
                    resource,
                    actor_user_id=resource.owner_user_id,
                )
            except InputResourceAccessDenied:
                self.db.rollback()
                pending.add(resource.id)
        return pending

    def _purge_session_records(self, session: ChatSession) -> None:
        """删除会话可见内容和短期投影，保留 Execution、外部投递及管理审计事实。"""

        message_ids = set(
            self.db.exec(
                select(Message.id).where(
                    Message.tenant_id == session.tenant_id,
                    Message.session_id == session.id,
                )
            ).all()
        )
        execution_ids = set(
            self.db.exec(
                select(SopInstance.id).where(
                    SopInstance.tenant_id == session.tenant_id,
                    SopInstance.session_id == session.id,
                )
            ).all()
        )
        for model in (
            MessageInputResourceLink,
            TurnInputSnapshot,
            MessageFeedback,
            SkillFeedback,
            SessionGeneralSkillOverride,
            GeneralSkillInstallIntent,
            HumanHandoffRequest,
            MemoryRecord,
        ):
            statement = select(model).where(
                model.tenant_id == session.tenant_id,
                model.session_id == session.id,
            )
            for row in self.db.exec(statement).all():
                self.db.delete(row)
        for row in self.db.exec(
            select(AgentEvent).where(
                AgentEvent.tenant_id == session.tenant_id,
                AgentEvent.session_id == session.id,
                or_(
                    AgentEvent.aggregate_type.is_(None),
                    AgentEvent.aggregate_type != "execution",
                ),
            )
        ).all():
            self.db.delete(row)
        if message_ids:
            for row in self.db.exec(
                select(TurnInputReadReceipt).where(
                    TurnInputReadReceipt.tenant_id == session.tenant_id,
                    TurnInputReadReceipt.turn_id.in_(message_ids),
                )
            ).all():
                self.db.delete(row)
        if execution_ids:
            for row in self.db.exec(
                select(MessageInputBindingLink).where(
                    MessageInputBindingLink.tenant_id == session.tenant_id,
                    MessageInputBindingLink.execution_id.in_(execution_ids),
                )
            ).all():
                self.db.delete(row)
        for row in self.db.exec(
            select(Message).where(
                Message.tenant_id == session.tenant_id,
                Message.session_id == session.id,
            )
        ).all():
            self.db.delete(row)
        self.db.delete(session)

    def _agent_sessions(self, agent: AgentProfile) -> list[ChatSession]:
        """读取直接绑定或由历史 Execution 继承该 Agent 的会话，防止遗留内容漏清。"""

        return self.db.exec(
            select(ChatSession)
            .where(
                ChatSession.tenant_id == agent.tenant_id,
                or_(
                    ChatSession.agent_id == agent.id,
                    and_(
                        ChatSession.agent_id.is_(None),
                        select(SopInstance.id)
                        .where(
                            SopInstance.tenant_id == agent.tenant_id,
                            SopInstance.session_id == ChatSession.id,
                            SopInstance.agent_id == agent.id,
                        )
                        .exists(),
                    ),
                ),
            )
        ).all()

    def _agent_session_ids(self, agent: AgentProfile) -> set[str]:
        """返回当前 Agent 会话身份集合，供 Execution 和资源边界联查。"""

        return {session.id for session in self._agent_sessions(agent)}

    def _agent_resources(self, agent: AgentProfile) -> list[ManagedInputResource]:
        """只读取同租户、同 Agent 的受管资源，避免按资源 ID 误伤其他租户。"""

        return self.db.exec(
            select(ManagedInputResource).where(
                ManagedInputResource.tenant_id == agent.tenant_id,
                ManagedInputResource.agent_id == agent.id,
            )
        ).all()

    def _rows(self, model: type, tenant_id: str, agent_id: str) -> list[object]:
        """读取具有标准 tenant_id/agent_id 列的 Agent 关联行。"""

        return list(
            self.db.exec(
                select(model).where(
                    model.tenant_id == tenant_id,
                    model.agent_id == agent_id,
                )
            ).all()
        )

    @staticmethod
    def _deletion_metadata(agent: AgentProfile) -> dict[str, object]:
        """读取并浅复制删除元数据，避免原地修改共享 JSON 对象。"""

        value = (agent.metadata_json or {}).get(DELETION_METADATA_KEY)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _set_deletion_state(
        agent: AgentProfile,
        *,
        state: str,
        actor_user_id: str,
        pending_execution_ids: tuple[str, ...] | set[str],
        pending_resource_ids: list[str] | tuple[str, ...],
    ) -> None:
        """持久化不含正文的删除进度，后续 DELETE 重试即可继续收敛。"""

        metadata = AgentDeletionService._deletion_metadata(agent)
        metadata.update(
            {
                "state": state,
                "last_attempt_at": utc_now().isoformat(),
                "last_attempt_by_user_id": actor_user_id,
                "pending_execution_ids": sorted(pending_execution_ids),
                "pending_resource_ids": sorted(pending_resource_ids),
            }
        )
        metadata.pop("lease_owner", None)
        metadata.pop("lease_expires_at", None)
        agent.metadata_json = {
            **dict(agent.metadata_json or {}),
            DELETION_METADATA_KEY: metadata,
        }
        agent.updated_at = utc_now()


def reconcile_pending_agent_deletions(
    db: Session,
    *,
    limit: int = DELETION_RECONCILE_LIMIT,
) -> int:
    """由后台 worker 重试待收敛删除，直至执行和资源都进入确定终态。"""

    bounded_limit = max(1, min(int(limit), DELETION_RECONCILE_LIMIT))
    archived_agents = db.exec(
        select(AgentProfile)
        .where(AgentProfile.status == "archived")
        .order_by(AgentProfile.updated_at, AgentProfile.id)
    ).all()
    candidates = []
    for agent in archived_agents:
        deletion = AgentDeletionService._deletion_metadata(agent)
        if deletion.get("state") in DELETION_RECONCILE_STATES:
            candidates.append(agent)
        if len(candidates) >= bounded_limit:
            break

    reconciled = 0
    for agent in candidates:
        deletion = AgentDeletionService._deletion_metadata(agent)
        actor_user_id = str(
            deletion.get("requested_by_user_id") or "system:agent-deletion-reconciler"
        )
        try:
            result = AgentDeletionService(db).delete(agent, actor_user_id=actor_user_id)
            if result.status == DELETION_COMPLETED:
                append_management_audit(
                    db,
                    tenant_id=agent.tenant_id,
                    actor_user_id=actor_user_id,
                    actor_display_name="Agent 删除对账器",
                    actor_type="system",
                    action="agent.delete.reconciled",
                    action_kind="delete",
                    outcome="success",
                    resource_type="agent_profile",
                    resource_id=agent.id,
                    before={"status": "archived"},
                    after=result.as_dict(),
                    detail={"lifecycle": "background_reconciliation"},
                )
                db.commit()
                reconciled += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            current = db.get(AgentProfile, agent.id)
            current_deletion = (
                AgentDeletionService._deletion_metadata(current) if current is not None else {}
            )
            if current is not None and not AgentDeletionService._deletion_lease_is_active(
                current_deletion
            ):
                current_deletion.update(
                    {
                        "state": DELETION_PENDING,
                        "last_error_code": type(exc).__name__,
                        "last_attempt_at": utc_now().isoformat(),
                        "last_attempt_by_user_id": actor_user_id,
                    }
                )
                current.metadata_json = {
                    **dict(current.metadata_json or {}),
                    DELETION_METADATA_KEY: current_deletion,
                }
                current.updated_at = utc_now()
                db.add(current)
                db.commit()
            logger.warning(
                "Agent deletion reconciliation deferred agent_id=%s error=%s",
                agent.id,
                type(exc).__name__,
            )
    return reconciled
