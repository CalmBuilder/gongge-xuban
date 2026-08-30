"""
@Time       : 2026/08/11 23:05
@Author     : zhanglp8181
@File       : test_standing_approvals.py
@CallChain  : pytest → Standing Approval service → SQLModel/能力快照/规则匹配
@Description: 验证长期批准创建重放、精确匹配、漂移降级、撤销和高风险拒绝契约。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    ConnectionProfile,
    ConnectorThreadBinding,
    ManagementAuditLog,
    ScheduledTask,
    ScheduledTaskRun,
    SopInstance,
    StandingApprovalCommandReceipt,
    StandingApprovalRule,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.capability_catalog import DynamicCapabilityCatalog
from app.dynamic_tasks.standing_approvals import (
    StandingApprovalError,
    create_standing_approval_rule,
    match_standing_approval_rule,
    revoke_standing_approval_rule,
    schedule_definition_checksum,
    scheduled_write_snapshots,
)
from app.sop_runtime.execution_control import canonical_checksum


def test_create_replay_and_revoke_rule_are_audited_and_cas_guarded(monkeypatch) -> None:
    """同命令重放不复制规则，语义冲突拒绝，撤销必须使用当前 revision。"""

    db, facts = _runtime(monkeypatch)
    rule = _create_rule(db, facts, command_id="create-rule-1")
    replay = _create_rule(db, facts, command_id="create-rule-1")

    assert replay.id == rule.id
    assert len(db.exec(select(StandingApprovalRule)).all()) == 1
    assert len(db.exec(select(StandingApprovalCommandReceipt)).all()) == 1
    with pytest.raises(StandingApprovalError, match="STANDING_APPROVAL_COMMAND_CONFLICT"):
        create_standing_approval_rule(
            db,
            tenant_id="tenant_rule",
            command_id="create-rule-1",
            current_user=facts.manager,
            agent_id=facts.agent.id,
            source_schedule_id=facts.task.id,
            profile_id=facts.profile.id,
            thread_binding_id=facts.thread.id,
            tool_action="wecom.message_send",
            argument_constraints={"content": {"equals": "changed"}},
            valid_from=rule.valid_from,
            valid_to=rule.valid_to,
        )
    with pytest.raises(StandingApprovalError, match="STANDING_APPROVAL_REVISION_CONFLICT"):
        revoke_standing_approval_rule(
            db,
            tenant_id="tenant_rule",
            rule_id=rule.id,
            command_id="revoke-stale",
            expected_revision=2,
            current_user=facts.manager,
        )

    revoked = revoke_standing_approval_rule(
        db,
        tenant_id="tenant_rule",
        rule_id=rule.id,
        command_id="revoke-rule-1",
        expected_revision=1,
        current_user=facts.manager,
    )
    replay_revoked = revoke_standing_approval_rule(
        db,
        tenant_id="tenant_rule",
        rule_id=rule.id,
        command_id="revoke-rule-1",
        expected_revision=1,
        current_user=facts.manager,
    )

    assert revoked.status == replay_revoked.status == "revoked"
    assert revoked.revision == 2
    replacement = _create_rule(db, facts, command_id="create-replacement-rule")
    assert replacement.id != revoked.id
    assert replacement.active_scope_key
    assert revoked.active_scope_key is None
    audits = db.exec(select(ManagementAuditLog).order_by(ManagementAuditLog.created_at)).all()
    assert [row.action for row in audits] == [
        "standing_approval.create",
        "standing_approval.revoke",
        "standing_approval.create",
    ]
    assert "消息正文" not in str(audits)
    db.close()


def test_rule_matches_exact_schedule_target_snapshot_and_arguments(monkeypatch) -> None:
    """只有来源、工具快照、精确线程、有效期和参数同时相等时才自动放行。"""

    db, facts = _runtime(monkeypatch)
    rule = _create_rule(db, facts, command_id="create-match-rule")
    run, instance = _scheduled_execution(db, facts)
    snapshot = DynamicCapabilityCatalog._wecom_message_snapshot(
        facts.profile,
        facts.binding,
        facts.thread,
    )

    match = match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    )

    assert match is not None
    assert match.rule.id == rule.id
    assert match.evidence["source_schedule_id"] == facts.task.id
    assert match.evidence["canonical_target"] == f"wecom_thread:{facts.thread.id}"
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "参数已漂移"},
    ) is None

    facts.initiator.membership_status = "disabled"
    db.add(facts.initiator)
    db.commit()
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None
    facts.initiator.membership_status = "active"
    db.add(facts.initiator)
    db.commit()

    snapshots = scheduled_write_snapshots(
        db,
        tenant_id="tenant_rule",
        agent_id=facts.agent.id,
        initiator_user_id=facts.task.created_by_user_id,
        run_id=run.id,
    )
    assert [item.checksum for item in snapshots] == [rule.tool_snapshot_checksum]

    facts.profile.revision += 1
    db.add(facts.profile)
    db.commit()
    assert scheduled_write_snapshots(
        db,
        tenant_id="tenant_rule",
        agent_id=facts.agent.id,
        initiator_user_id=facts.task.created_by_user_id,
        run_id=run.id,
    ) == []
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None
    db.close()


def test_schedule_drift_expiry_revoke_and_disabled_switch_fail_closed(monkeypatch) -> None:
    """调度漂移、到期、撤销或总开关关闭都必须退回普通审批路径。"""

    db, facts = _runtime(monkeypatch)
    rule = _create_rule(db, facts, command_id="create-drift-rule")
    run, instance = _scheduled_execution(db, facts)
    snapshot = DynamicCapabilityCatalog._wecom_message_snapshot(
        facts.profile,
        facts.binding,
        facts.thread,
    )
    run.source_snapshot_json = {**run.source_snapshot_json, "agent_id": "tampered-agent"}
    db.add(run)
    db.commit()
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None
    run.source_snapshot_json = {**run.source_snapshot_json, "agent_id": facts.agent.id}
    db.add(run)
    db.commit()
    facts.task.prompt = "已修改的调度正文"
    db.add(facts.task)
    db.commit()
    assert schedule_definition_checksum(facts.task) != rule.source_schedule_checksum
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None

    facts.task.prompt = "发送固定日报"
    rule.valid_to = utc_now() - timedelta(seconds=1)
    db.add_all([facts.task, rule])
    db.commit()
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None

    rule.valid_to = utc_now() + timedelta(days=1)
    rule.status = "revoked"
    db.add(rule)
    db.commit()
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None

    monkeypatch.setattr(
        "app.dynamic_tasks.standing_approvals.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_external_write_enabled=True,
            dynamic_task_high_risk_external_write_allows=lambda _tenant, _agent: True,
            dynamic_task_standing_approval_enabled=False,
        ),
    )
    rule.status = "active"
    db.add(rule)
    db.commit()
    assert match_standing_approval_rule(
        db,
        instance=instance,
        snapshot=snapshot,
        arguments={"content": "固定日报"},
    ) is None
    db.close()


def test_constraints_and_non_wecom_action_reject_code_or_broad_scope(monkeypatch) -> None:
    """规则不接受正则/表达式、空约束、超长有效期或 execute/destructive 类动作。"""

    db, facts = _runtime(monkeypatch)
    now = utc_now()
    invalid_constraints = (
        {},
        {"content": {"regex": ".*"}},
        {"content": {"equals": ""}},
        {"content": {"enum": []}},
        {"content": {"min_length": 10, "max_length": 2}},
    )
    for index, constraints in enumerate(invalid_constraints):
        with pytest.raises(
            StandingApprovalError,
            match="STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID",
        ):
            create_standing_approval_rule(
                db,
                tenant_id="tenant_rule",
                command_id=f"invalid-constraints-{index}",
                current_user=facts.manager,
                agent_id=facts.agent.id,
                source_schedule_id=facts.task.id,
                profile_id=facts.profile.id,
                thread_binding_id=facts.thread.id,
                tool_action="wecom.message_send",
                argument_constraints=constraints,
                valid_from=now,
                valid_to=now + timedelta(days=1),
            )
    with pytest.raises(StandingApprovalError, match="STANDING_APPROVAL_TOOL_UNSUPPORTED"):
        create_standing_approval_rule(
            db,
            tenant_id="tenant_rule",
            command_id="reject-exec",
            current_user=facts.manager,
            agent_id=facts.agent.id,
            source_schedule_id=facts.task.id,
            profile_id=facts.profile.id,
            thread_binding_id=facts.thread.id,
            tool_action="run_shell",
            argument_constraints={"content": {"equals": "固定日报"}},
            valid_from=now,
            valid_to=now + timedelta(days=1),
        )
    with pytest.raises(StandingApprovalError, match="STANDING_APPROVAL_VALIDITY_TOO_LONG"):
        create_standing_approval_rule(
            db,
            tenant_id="tenant_rule",
            command_id="reject-long-lived",
            current_user=facts.manager,
            agent_id=facts.agent.id,
            source_schedule_id=facts.task.id,
            profile_id=facts.profile.id,
            thread_binding_id=facts.thread.id,
            tool_action="wecom.message_send",
            argument_constraints={"content": {"equals": "固定日报"}},
            valid_from=now,
            valid_to=now + timedelta(days=91),
        )
    db.close()


def _runtime(monkeypatch) -> tuple[Session, SimpleNamespace]:
    """创建含调度、Agent、连接、线程和规则管理员的最小内存治理环境。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    manager = User(
        id="rule-manager",
        tenant_id="tenant_rule",
        username="manager",
        password_hash="x",
    )
    initiator = User(
        id="schedule-owner",
        tenant_id="tenant_rule",
        username="owner",
        password_hash="x",
    )
    agent = AgentProfile(
        id="agent-rule",
        tenant_id="tenant_rule",
        name="Rule Agent",
        owner_user_id=manager.id,
    )
    task = ScheduledTask(
        id="schedule-rule",
        tenant_id="tenant_rule",
        agent_id=agent.id,
        created_by_user_id=initiator.id,
        title="固定日报",
        prompt="发送固定日报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
    )
    profile = ConnectionProfile(
        id="profile-rule",
        tenant_id="tenant_rule",
        provider="wecom",
        account_id="corp:agent",
        display_name="Rule WeCom",
        secret_ref_id="secret-rule",
        granted_scopes_json=["wecom.application:read"],
        tool_allowlist_json=["wecom.message_send"],
        created_by_user_id=manager.id,
        updated_by_user_id=manager.id,
    )
    binding = AgentConnectionBinding(
        id="binding-rule",
        tenant_id="tenant_rule",
        agent_id=agent.id,
        profile_id=profile.id,
        allowed_scopes_json=["wecom.application:read"],
        allowed_actions_json=["wecom.message_send"],
        created_by_user_id=manager.id,
        updated_by_user_id=manager.id,
    )
    thread = ConnectorThreadBinding(
        id="thread-rule",
        tenant_id="tenant_rule",
        provider="wecom",
        profile_id=profile.id,
        sender_ref_hash="sender-hash",
        encrypted_recipient_ref="encrypted",
        user_id=initiator.id,
        agent_id=agent.id,
        session_id="source-session",
    )
    db.add_all(
        [
            Tenant(id="tenant_rule", name="Rule tenant"),
            manager,
            initiator,
            agent,
            task,
            profile,
            binding,
            thread,
        ]
    )
    db.commit()
    monkeypatch.setattr(
        "app.dynamic_tasks.standing_approvals._require_rule_manager",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.dynamic_tasks.standing_approvals._can_manage_schedule_and_agent",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.dynamic_tasks.standing_approvals.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_external_write_enabled=True,
            dynamic_task_high_risk_external_write_allows=lambda _tenant, _agent: True,
            dynamic_task_standing_approval_enabled=True,
        ),
    )
    return db, SimpleNamespace(
        manager=manager,
        initiator=initiator,
        agent=agent,
        task=task,
        profile=profile,
        binding=binding,
        thread=thread,
    )


def _create_rule(
    db: Session,
    facts: SimpleNamespace,
    *,
    command_id: str,
) -> StandingApprovalRule:
    """创建正文精确等于“固定日报”的一天期规则。"""

    now = getattr(facts, "rule_valid_from", None) or utc_now()
    facts.rule_valid_from = now
    return create_standing_approval_rule(
        db,
        tenant_id="tenant_rule",
        command_id=command_id,
        current_user=facts.manager,
        agent_id=facts.agent.id,
        source_schedule_id=facts.task.id,
        profile_id=facts.profile.id,
        thread_binding_id=facts.thread.id,
        tool_action="wecom.message_send",
        argument_constraints={"content": {"equals": "固定日报"}},
        valid_from=now,
        valid_to=now + timedelta(days=1),
    )


def _scheduled_execution(
    db: Session,
    facts: SimpleNamespace,
) -> tuple[ScheduledTaskRun, SopInstance]:
    """创建绑定同一调度来源的 running run 与 Dynamic Execution。"""

    now = utc_now()
    source_ref = "scheduled-task:schedule-rule:schedule:run-rule"
    source_snapshot = {
        "scheduled_task_id": facts.task.id,
        "tenant_id": "tenant_rule",
        "agent_id": facts.agent.id,
        "initiator_user_id": facts.task.created_by_user_id,
        "source_kind": "schedule",
        "source_ref": source_ref,
    }
    run = ScheduledTaskRun(
        id="run-rule",
        tenant_id="tenant_rule",
        scheduled_task_id=facts.task.id,
        agent_id=facts.agent.id,
        user_id=facts.task.created_by_user_id,
        source_kind="schedule",
        source_ref=source_ref,
        source_snapshot_json=source_snapshot,
        source_checksum=canonical_checksum(source_snapshot),
        scheduled_for=now,
        status="running",
    )
    instance = SopInstance(
        id="execution-rule",
        tenant_id="tenant_rule",
        session_id="run-session",
        kind="dynamic_task",
        agent_id=facts.agent.id,
        initiator_user_id=facts.task.created_by_user_id,
        source_kind="schedule",
        source_ref=run.id,
        goal_snapshot_json={"goal": "发送固定日报"},
        current_plan_revision_id="plan-revision-rule",
        current_plan_checksum="plan-checksum-rule",
        capability_snapshot_json={"model": {"model_config_id": "model-rule"}},
        status="running",
        active_slot_key="foreground:run-session",
        started_at=now,
    )
    run.execution_id = instance.id
    db.add_all([run, instance])
    db.commit()
    return run, instance
