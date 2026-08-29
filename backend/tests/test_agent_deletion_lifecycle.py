"""
@Time       : 2026/08/28
@Author     : zhanglp8181
@File       : test_agent_deletion_lifecycle.py
@CallChain  : pytest → AgentDeletionService → Agent/Profile、Execution、会话与调度生命周期
@Description: 验证数字员工删除的墓碑优先、幂等重试、执行阻断和租户隔离契约。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.deletion import AgentDeletionService, reconcile_pending_agent_deletions
from app.api.attention_items import AttentionResolveRequest, resolve_attention_item
from app.core.agent_loop import AgentLoop, AgentLoopPreconditionError
from app.db.models import (
    AgentProfile,
    AgentEvent,
    AgentResourceBinding,
    ArtifactRendererJob,
    AttachmentUploadDailyUsage,
    AttachmentUploadQuotaLease,
    ChatSession,
    DraftUploadBinding,
    ExecutionSignal,
    ExecutionArtifact,
    HumanHandoffRequest,
    Message,
    MemoryRecord,
    ScheduledTask,
    SopInstance,
    SopWorkItem,
    SopWorkItemCandidate,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.scheduled_tasks.service import due_scheduled_tasks
from app.sop_runtime.execution_store import SopExecutionStore
from app.session.upload_quotas import AttachmentUploadQuotaPolicy, AttachmentUploadQuotaService


def _test_session() -> Session:
    """创建包含完整 SQLModel 元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_agent(db: Session, *, agent_id: str = "agent_delete") -> AgentProfile:
    """建立可管理的租户、所有者和私人 Agent 基础数据。"""

    db.add(Tenant(id="tenant_delete", name="删除测试租户"))
    db.add(
        User(
            id="user_delete",
            tenant_id="tenant_delete",
            username="delete-owner",
            role="member",
            password_hash="test-password-hash",
        )
    )
    agent = AgentProfile(
        id=agent_id,
        tenant_id="tenant_delete",
        name="待删除员工",
        owner_user_id="user_delete",
        metadata_json={"owner_user_id": "user_delete"},
    )
    db.add(agent)
    db.commit()
    return agent


def test_delete_archives_agent_cleans_session_and_pauses_schedule() -> None:
    """删除空闲员工应保留墓碑、清空会话内容、撤销绑定并暂停定时任务。"""

    with _test_session() as db:
        agent = _seed_agent(db)
        session = ChatSession(
            id="session_delete",
            tenant_id=agent.tenant_id,
            user_id="user_delete",
            agent_id=agent.id,
            title="待清理会话",
        )
        task = ScheduledTask(
            id="task_delete",
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            created_by_user_id="user_delete",
            title="待暂停任务",
            prompt="执行汇总",
            next_run_at=utc_now(),
        )
        db.add(session)
        db.add(
            Message(
                id="message_delete",
                tenant_id=agent.tenant_id,
                session_id=session.id,
                role="user",
                content="不应继续保留",
            )
        )
        db.add(
            MemoryRecord(
                id="memory_agent_delete",
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                user_id="user_delete",
                kind="preference",
                content="不应继续保留的员工级记忆",
            )
        )
        db.add(
            DraftUploadBinding(
                id="upload_binding_delete",
                binding_id="binding_delete",
                tenant_id=agent.tenant_id,
                owner_user_id="user_delete",
                agent_id=agent.id,
                session_id=session.id,
                nonce_checksum="a" * 64,
                idempotency_key="upload-idempotency-delete",
                expires_at=utc_now() + timedelta(minutes=5),
            )
        )
        db.add(
            AgentResourceBinding(
                id="binding_delete",
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                resource_type="tool",
                resource_id="tool_delete",
            )
        )
        db.add(
            AgentEvent(
                id="execution_event_delete",
                tenant_id=agent.tenant_id,
                session_id=session.id,
                event_type="execution_progress",
                aggregate_type="execution",
                aggregate_id="execution_agent_delete",
                payload_json={"step": "done"},
            )
        )
        db.add(
            AgentEvent(
                id="chat_event_delete",
                tenant_id=agent.tenant_id,
                session_id=session.id,
                event_type="chat_delta",
                aggregate_type="chat",
                aggregate_id="message_delete",
                payload_json={"content": "短期事件"},
            )
        )
        db.add(
            SopInstance(
                id="execution_agent_delete",
                tenant_id=agent.tenant_id,
                session_id=session.id,
                kind="dynamic_task",
                source_kind="chat",
                source_ref="message_delete",
                agent_id=agent.id,
                initiator_user_id="user_delete",
                goal_snapshot_json={"goal": "已完成报告", "success_criteria": []},
                current_plan_revision_id="plan_agent_delete",
                current_plan_checksum="d" * 64,
                capability_snapshot_json={"model": {}},
                capability_checksum="e" * 64,
                status="succeeded",
                current_node_id="done",
            )
        )
        db.add(
            ExecutionArtifact(
                id="artifact_agent_delete",
                tenant_id=agent.tenant_id,
                execution_id="execution_agent_delete",
                source_node_execution_id="node_agent_delete",
                source_step_key="report",
                artifact_key="report",
                filename="report.md",
                mime_type="text/markdown",
                size_bytes=0,
                content_checksum="b" * 64,
                storage_locator="artifacts/agent-delete",
                acl_json={"user_ids": ["user_delete"]},
            )
        )
        db.add(
            ArtifactRendererJob(
                id="render_job_agent_delete",
                tenant_id=agent.tenant_id,
                execution_id="execution_agent_delete",
                result_id="result_agent_delete",
                result_checksum="c" * 64,
                source_node_execution_id="node_agent_delete",
                artifact_key="report",
                filename="report.md",
                mime_type="text/markdown",
                renderer_version="1",
                status="pending",
            )
        )
        db.add(task)
        reservation = AttachmentUploadQuotaService(db).acquire(
            tenant_id=agent.tenant_id,
            owner_user_id="user_delete",
            binding_id="binding_delete",
            policy=AttachmentUploadQuotaPolicy(
                user_concurrency=1,
                tenant_concurrency=1,
                user_daily_bytes=1024,
                tenant_daily_bytes=1024,
                reservation_ttl_seconds=300,
                reservation_bytes=1024,
            ),
        )
        db.commit()

        result = AgentDeletionService(db).delete(agent, actor_user_id="user_delete")
        assert result.status == "deleted"
        assert result.cleaned_session_count == 1

        persisted_agent = db.get(AgentProfile, agent.id)
        assert persisted_agent is not None
        assert persisted_agent.status == "archived"
        assert persisted_agent.metadata_json["hidden_from_product"] is True
        assert persisted_agent.metadata_json["agent_deletion"]["state"] == "deleted"
        assert db.get(ChatSession, session.id) is None
        assert db.get(Message, "message_delete") is None
        assert db.get(MemoryRecord, "memory_agent_delete") is None
        assert db.get(AgentEvent, "execution_event_delete") is not None
        assert db.get(AgentEvent, "chat_event_delete") is None
        artifact = db.get(ExecutionArtifact, "artifact_agent_delete")
        assert artifact is not None and artifact.status == "revoked"
        renderer_job = db.get(ArtifactRendererJob, "render_job_agent_delete")
        assert renderer_job is not None and renderer_job.status == "cancelled"
        db.refresh(reservation)
        assert reservation.status == "released"
        assert db.exec(select(AttachmentUploadQuotaLease)).all() == []
        assert all(
            usage.reserved_bytes == 0
            for usage in db.exec(select(AttachmentUploadDailyUsage)).all()
        )
        persisted_task = db.get(ScheduledTask, task.id)
        assert persisted_task is not None
        assert persisted_task.status == "paused"
        assert persisted_task.next_run_at is None
        persisted_binding = db.get(AgentResourceBinding, "binding_delete")
        assert persisted_binding is not None
        assert persisted_binding.status == "deleted"
        assert due_scheduled_tasks(db, now=utc_now() + timedelta(minutes=1)) == []


def test_delete_is_idempotent_and_does_not_cross_tenant() -> None:
    """重复删除只返回已完成状态，其他租户同 ID 的员工和数据不受影响。"""

    with _test_session() as db:
        agent = _seed_agent(db)
        db.add(Tenant(id="tenant_other", name="其他租户"))
        other = AgentProfile(
            id="agent_other",
            tenant_id="tenant_other",
            name="其他租户员工",
            owner_user_id="other-owner",
        )
        db.add(other)
        db.commit()

        service = AgentDeletionService(db)
        first = service.delete(agent, actor_user_id="user_delete")
        second = service.delete(agent, actor_user_id="user_delete")

        assert first.status == "deleted"
        assert second.status == "deleted"
        assert db.get(AgentProfile, other.id).status == "active"
        assert db.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == "tenant_other")
        ).one().status == "active"


def test_background_reconciler_completes_pending_deletion() -> None:
    """后台删除对账应复用同一服务，将无阻塞的待删除墓碑收敛为已完成。"""

    with _test_session() as db:
        agent = _seed_agent(db, agent_id="agent_reconcile")
        agent.status = "archived"
        agent.metadata_json = {
            "hidden_from_product": True,
            "agent_deletion": {
                "state": "deletion_pending",
                "requested_by_user_id": "user_delete",
                "pending_execution_ids": [],
                "pending_resource_ids": [],
            },
        }
        db.add(agent)
        db.commit()

        assert reconcile_pending_agent_deletions(db) == 1

        db.refresh(agent)
        assert agent.status == "archived"
        assert agent.metadata_json["agent_deletion"]["state"] == "deleted"


def test_runtime_creation_is_fenced_after_agent_tombstone() -> None:
    """Agent 已进入墓碑后，Runtime 创建新 SOP 必须在写入前拒绝。"""

    with _test_session() as db:
        agent = _seed_agent(db, agent_id="agent_fenced")
        store = SopExecutionStore(db)
        agent.status = "archived"
        db.add(agent)
        db.commit()

        with pytest.raises(ValueError, match="归档"):
            store.start_instance(
                tenant_id=agent.tenant_id,
                session_id="session_after_delete",
                skill_id="skill_after_delete",
                skill_version_id="version_after_delete",
                skill_version="1.0.0",
                definition_checksum="a" * 64,
                start_node_id="start",
                agent_id=agent.id,
                enforce_agent_lifecycle=True,
            )

        assert db.exec(select(SopInstance).where(SopInstance.agent_id == agent.id)).all() == []


def test_deletion_keeps_unknown_write_attention_and_reconciles_archived_agent() -> None:
    """验证删除引发的 unknown 外部写可由治理人对账，并最终取消执行。"""

    with _test_session() as db:
        agent = _seed_agent(db, agent_id="agent_delete_reconcile")
        governance_user = User(
            id="user_delete_governance",
            tenant_id=agent.tenant_id,
            username="delete-governance",
            role="admin",
            password_hash="test-password-hash",
        )
        db.add(governance_user)
        db.commit()

        store = SopExecutionStore(db)
        instance, revision = store.start_dynamic_instance(
            tenant_id=agent.tenant_id,
            session_id="session_delete_reconcile",
            agent_id=agent.id,
            initiator_user_id="user_delete",
            plan=NormalizedPlan(
                goal="删除时收敛外部写",
                success_criteria=(
                    SuccessCriterion(
                        id="write_reconciled",
                        type="assertion",
                        spec={"required": True},
                    ),
                ),
                steps=(
                    PlanStep(
                        step_key="send_message",
                        title="发送外部消息",
                        kind="tool.write",
                    ),
                ),
                budget={"max_steps": 2, "max_tool_calls": 1},
            ),
            capability_snapshot={"model": {}},
        )
        assert revision.execution_id == instance.id
        with store.owned(instance, worker_id="write-worker"):
            node = store.enter_node(
                instance,
                "send_message",
                input_snapshot={},
                plan_revision_id=revision.id,
                step_key="send_message",
                step_kind="tool.write",
            )
            operation, _ = store.prepare_operation(
                instance,
                node,
                operation_name="wecom.message_send",
                request={"content": "删除前的消息"},
                logical_action_id="delete-reconcile-write",
                effect_kind="external_write",
            )
            store.start_operation(operation)
        db.commit()

        result = AgentDeletionService(db).delete(
            agent,
            actor_user_id="user_delete",
        )
        assert result.status == "deletion_pending"
        db.refresh(instance)
        db.refresh(operation)
        assert instance.status == "waiting"
        assert operation.status == "unknown"
        attention = db.exec(
            select(SopWorkItem).where(
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.attention_kind == "exception",
            )
        ).one()
        assert attention.status == "offered"
        assert attention.source_type == "agent_deletion"
        assert db.exec(
            select(SopWorkItemCandidate.id).where(
                SopWorkItemCandidate.work_item_id == attention.id,
                SopWorkItemCandidate.user_id == governance_user.id,
            )
        ).one()

        resolved = resolve_attention_item(
            attention.id,
            AttentionResolveRequest(
                tenant_id=agent.tenant_id,
                command_id="delete-reconcile-not-applied",
                command="confirm_not_applied",
                expected_revision=attention.revision,
                comment="外部系统审计记录显示消息未送达。",
            ),
            governance_user,
            db,
        )
        assert resolved.status == "completed"
        signal = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.signal_type == "attention_decided",
                ExecutionSignal.execution_id == instance.id,
            )
        ).one()
        outcome = DynamicTaskAgent(db).resume_write_reconciliation_signal(
            signal_id=signal.id,
            model_config=None,  # 取消已收敛时不应重新进入模型运行路径。
            worker_id="delete-reconcile-worker",
            actor_user_id=governance_user.id,
        )
        assert outcome.status == "cancelled"
        db.refresh(instance)
        db.refresh(operation)
        assert instance.status == "cancelled"
        assert operation.status == "failed"
        assert signal.status == "discarded"
        assert reconcile_pending_agent_deletions(db) == 1
        db.refresh(agent)
        assert agent.metadata_json["agent_deletion"]["state"] == "deleted"


def test_deletion_surfaces_missing_reconciliation_candidate_as_safe_block() -> None:
    """没有外部写治理候选人时，删除应保留 unknown 并写出可观测阻塞原因。"""

    with _test_session() as db:
        agent = _seed_agent(db, agent_id="agent_delete_no_candidate")
        store = SopExecutionStore(db)
        instance, created = store.start_instance(
            tenant_id=agent.tenant_id,
            session_id="session_delete_no_candidate",
            skill_id="skill_delete_no_candidate",
            skill_version_id="skillver_delete_no_candidate",
            skill_version="1.0.0",
            definition_checksum="a" * 64,
            start_node_id="send_message",
            initiator_user_id="user_delete",
            agent_id=agent.id,
        )
        assert created is True
        with store.owned(instance, worker_id="write-worker"):
            node = store.enter_node(instance, "send_message", input_snapshot={})
            operation, _ = store.prepare_operation(
                instance,
                node,
                operation_name="wecom.message_send",
                request={"content": "没有治理候选人的消息"},
                logical_action_id="delete-no-candidate-write",
                effect_kind="external_write",
            )
            store.start_operation(operation)
        db.commit()

        result = AgentDeletionService(db).delete(
            agent,
            actor_user_id="user_delete",
        )
        assert result.status == "deletion_pending"
        db.refresh(instance)
        assert instance.context_json["external_effect_reconciliation"][operation.id]["code"] == (
            "RECONCILIATION_CANDIDATE_REQUIRED"
        )
        assert db.exec(
            select(AgentEvent).where(
                AgentEvent.aggregate_id == instance.id,
                AgentEvent.event_type == "external_write_reconciliation_blocked",
            )
        ).one()


def test_runtime_creation_rejects_cross_tenant_agent_by_default() -> None:
    """Execution 默认生命周期校验必须拒绝把已存在的其他租户 Agent 借入当前租户。"""

    with _test_session() as db:
        agent = _seed_agent(db, agent_id="agent_tenant_a")
        db.add(Tenant(id="tenant_other", name="其他租户"))
        db.commit()

        with pytest.raises(ValueError, match="当前租户"):
            SopExecutionStore(db).start_instance(
                tenant_id="tenant_other",
                session_id="session_cross_tenant",
                skill_id="skill_cross_tenant",
                skill_version_id="version_cross_tenant",
                skill_version="1.0.0",
                definition_checksum="f" * 64,
                start_node_id="start",
                agent_id=agent.id,
            )


def test_runtime_creation_rejects_missing_agent_by_default() -> None:
    """生命周期校验开启时，不允许创建绑定不存在 Agent 的悬空 Execution。"""

    with _test_session() as db:
        with pytest.raises(ValueError, match="Agent 不存在"):
            SopExecutionStore(db).start_instance(
                tenant_id="tenant_demo",
                session_id="session_missing_agent",
                skill_id="skill_missing_agent",
                skill_version_id="version_missing_agent",
                skill_version="1.0.0",
                definition_checksum="e" * 64,
                start_node_id="start",
                agent_id="agent_does_not_exist",
            )


def test_delete_keeps_session_when_execution_cancellation_is_not_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部动作无法确认取消时只撤销入口并返回待重试，不删除执行所属会话。"""

    with _test_session() as db:
        agent = _seed_agent(db)
        session = ChatSession(
            id="session_pending_delete",
            tenant_id=agent.tenant_id,
            user_id="user_delete",
            agent_id=agent.id,
        )
        instance = SopInstance(
            id="execution_pending_delete",
            tenant_id=agent.tenant_id,
            session_id=session.id,
            kind="dynamic_task",
            active_slot_key=f"foreground:{session.id}",
            source_kind="chat",
            source_ref="turn_pending_delete",
            agent_id=agent.id,
            initiator_user_id="user_delete",
            goal_snapshot_json={"goal": "等待对账"},
            current_plan_revision_id="plan_pending_delete",
            current_plan_checksum="a" * 64,
            capability_snapshot_json={"tools": []},
            status="running",
            current_node_id="planning",
        )
        db.add(session)
        db.add(
            HumanHandoffRequest(
                id="handoff_answered_delete",
                tenant_id=agent.tenant_id,
                session_id=session.id,
                agent_id=agent.id,
                requester_user_id="user_delete",
                status="answered",
                human_reply="已完成历史答复",
                answered_at=utc_now(),
            )
        )
        db.add(instance)
        db.commit()

        def unresolved_cancellation(
            _store: SopExecutionStore,
            _instance: SopInstance,
            *,
            actor_user_id: str,
            reason: str,
        ) -> bool:
            """模拟远端副作用仍处于 unknown 的取消结果。"""

            assert actor_user_id == "user_delete"
            assert reason == "agent_deleted"
            return False

        monkeypatch.setattr(SopExecutionStore, "request_cancellation", unresolved_cancellation)

        result = AgentDeletionService(db).delete(agent, actor_user_id="user_delete")

        assert result.status == "deletion_pending"
        assert result.pending_execution_ids == (instance.id,)
        assert db.get(AgentProfile, agent.id).status == "archived"
        assert db.get(ChatSession, session.id) is not None
        assert db.get(SopInstance, instance.id).status == "running"
        answered = db.get(HumanHandoffRequest, "handoff_answered_delete")
        assert answered is not None and answered.status == "answered"


def test_deleted_agent_rejects_late_agent_loop_finalization() -> None:
    """数字员工进入墓碑状态后，迟到 Agent Loop 不能追加最终消息或事件。"""

    with _test_session() as db:
        agent = _seed_agent(db)
        session = ChatSession(
            id="session_late_finalize",
            tenant_id=agent.tenant_id,
            user_id="user_delete",
            agent_id=agent.id,
        )
        db.add(session)
        agent.status = "archived"
        agent.metadata_json = {
            **dict(agent.metadata_json or {}),
            "agent_deletion": {"state": "deleted"},
        }
        db.add(agent)
        db.commit()

        loop = object.__new__(AgentLoop)
        loop.db = db
        with pytest.raises(AgentLoopPreconditionError, match="迟到回复"):
            loop._finalize_turn(session, agent.tenant_id, "不应落库的迟到回复")

        assert db.exec(select(Message)).all() == []
