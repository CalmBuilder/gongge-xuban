"""
@Time       : 2026/08/03 23:05
@Author     : zhanglp8181
@File       : test_executions_api.py
@CallChain  : pytest → executions API → ExecutionControl/TerminalClosureGuard
@Description: 验证 Execution 命令授权、取消闭环、steer 等待状态和结果发布查询。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.executions import (
    ExecutionCommandRequest,
    get_execution,
    get_execution_result,
    issue_execution_command,
)
from app.db.models import (
    DynamicReadDispatchBatch,
    DynamicReadDispatchItem,
    DynamicReadDispatchResult,
    ExecutionCommand,
    ExecutionPlanRevision,
    GeneralSkillUse,
    SopInstance,
    SopNodeExecution,
    SopOperationAttempt,
    SopWorkItem,
    Tenant,
    User,
    utc_now,
)


@pytest.fixture
def db() -> Session:
    """建立隔离 SQLite 以执行真实命令、租约和终态约束。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _seed_execution(db: Session, *, suffix: str = "cancel"):
    """创建发起人、旁观者和一个无副作用动态 Execution。"""

    if db.get(Tenant, "tenant_demo") is None:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                password_hash="hash",
            )
        )
        db.add(
            User(
                id="user_outsider",
                tenant_id="tenant_demo",
                username="outsider",
                password_hash="hash",
            )
        )
    instance = SopInstance(
        id=f"execution_{suffix}",
        tenant_id="tenant_demo",
        session_id=f"session_{suffix}",
        kind="dynamic_task",
        active_slot_key=f"dynamic:{suffix}",
        initiator_user_id="user_owner",
        agent_id="agent_demo",
        goal_snapshot_json={"goal": suffix},
        current_plan_revision_id="plan_1",
        current_plan_checksum="a" * 64,
        capability_snapshot_json={"capabilities": []},
        status="running",
    )
    db.add(instance)
    db.commit()
    return db.get(User, "user_owner"), db.get(User, "user_outsider"), instance


def test_cancel_command_reaches_terminal_result_and_publication(db: Session) -> None:
    """验证 cancel 不是只写请求，而是关闭 signal 并产出一次可查询终态结果。"""

    owner, _, instance = _seed_execution(db)
    assert owner is not None
    use = GeneralSkillUse(
        tenant_id="tenant_demo",
        session_id=instance.session_id,
        turn_id="turn_cancel_1",
        execution_id=instance.id,
        agent_id="agent_demo",
        user_id=owner.id,
        skill_id="skill_cancel_1",
        revision_id="revision_cancel_1",
        content_checksum="c" * 64,
        selection_mode="forced",
        status="active",
        idempotency_key="cancel-use-1",
    )
    db.add(use)
    db.commit()
    command = issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="cancel_1",
            command_type="cancel",
            expected_revision=instance.revision,
            payload={"reason": "user_requested"},
        ),
        owner,
        db,
    )
    assert command.status == "applied"
    db.refresh(instance)
    assert instance.status == "cancelled"
    db.refresh(use)
    assert use.status == "cancelled"
    assert use.invalidation_reason == "DYNAMIC_EXECUTION_CANCELLED"
    result = get_execution_result(instance.id, "tenant_demo", owner, db)
    assert result.result["status"] == "cancelled"
    assert result.publications[0]["required"] is True
    assert result.publications[0]["status"] == "settled"


def test_execution_card_projects_plan_progress_budget_and_attention(db: Session) -> None:
    """执行卡必须从权威聚合返回目标、当前步骤、预算使用和活动 Attention。"""

    owner, _, instance = _seed_execution(db, suffix="card")
    assert owner is not None
    plan = ExecutionPlanRevision(
        id="plan_1",
        tenant_id="tenant_demo",
        execution_id=instance.id,
        revision_number=1,
        status="active",
        plan_json={
            "goal": "生成合同风险简报",
            "success_criteria": ["覆盖合同证据"],
            "steps": [
                {
                    "step_key": "read_contract",
                    "title": "读取合同",
                    "kind": "tool.read",
                    "required": True,
                    "depends_on": [],
                },
                {
                    "step_key": "clarify_region",
                    "title": "确认区域",
                    "kind": "clarification",
                    "required": True,
                    "depends_on": ["read_contract"],
                },
            ],
            "budget": {"max_steps": 4},
        },
        checksum="b" * 64,
        capability_snapshot_json={},
        capability_checksum="c" * 64,
    )
    instance.budget_snapshot_json = {"max_steps": 4, "max_model_calls": 8}
    instance.context_json = {"dynamic_budget_usage": {"model_calls": 3, "tool_calls": 1}}
    db.add(plan)
    db.add(instance)
    db.add(
        SopNodeExecution(
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_id="clarify_region",
            step_key="clarify_region",
            plan_revision_id=plan.id,
            step_kind="clarification",
            title="确认区域",
            status="waiting",
        )
    )
    db.add(
        SopWorkItem(
            tenant_id="tenant_demo",
            instance_id=instance.id,
            attention_kind="clarification",
            attention_key="clarify_region:clarification",
            attention_identity="attention_identity_card",
            title="确认区域",
            status="offered",
        )
    )
    db.add(
        DynamicReadDispatchBatch(
            id="readbatch_card",
            tenant_id="tenant_demo",
            execution_id=instance.id,
            plan_revision_id=plan.id,
            wave_checksum="d" * 64,
            ordered_step_keys_json=["read_contract", "read_party"],
            status="succeeded",
            parallelism=2,
        )
    )
    db.add(
        DynamicReadDispatchItem(
            tenant_id="tenant_demo",
            batch_id="readbatch_card",
            execution_id=instance.id,
            plan_revision_id=plan.id,
            position=0,
            step_key="read_contract",
            node_execution_id="node_read_contract",
            operation_id="operation_read_contract",
            operation_revision_at_start=1,
            dispatch_token="e" * 64,
            capability_checksum="f" * 64,
            request_fingerprint="1" * 64,
            status="settled",
        )
    )
    db.add(
        DynamicReadDispatchResult(
            tenant_id="tenant_demo",
            dispatch_token="e" * 64,
            status="succeeded",
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    db.add(
        SopOperationAttempt(
            tenant_id="tenant_demo",
            instance_id=instance.id,
            operation_id="operation_read_contract",
            node_execution_id="node_read_contract",
            attempt_number=1,
            status="succeeded",
        )
    )
    db.add(
        GeneralSkillUse(
            tenant_id="tenant_demo",
            session_id=instance.session_id,
            turn_id="turn_card",
            execution_id=instance.id,
            agent_id="agent_demo",
            user_id=owner.id,
            skill_id="skill_card",
            revision_id="revision_card",
            content_checksum="2" * 64,
            selection_mode="forced",
            status="completed",
            idempotency_key="card-use",
        )
    )
    db.commit()

    card = get_execution(instance.id, "tenant_demo", owner, db)

    assert card.goal == "生成合同风险简报"
    assert card.agent_id == "agent_demo"
    assert card.plan_revision_number == 1
    assert card.plan_reason == "initial"
    assert card.success_criteria == ["覆盖合同证据"]
    assert card.current_step_key == "clarify_region"
    assert card.steps[0]["status"] == "pending"
    assert card.steps[1]["status"] == "waiting"
    assert card.budget == {"max_steps": 4, "max_model_calls": 8}
    assert card.usage == {"model_calls": 3, "tool_calls": 1}
    assert card.pending_attention_count == 1
    assert len(card.parallel_waves) == 1
    assert card.parallel_waves[0].parallelism == 2
    assert card.parallel_waves[0].ordered_step_keys == ["read_contract", "read_party"]
    assert card.parallel_waves[0].status == "succeeded"
    assert card.parallel_waves[0].item_count == 1
    assert card.parallel_waves[0].settled_item_count == 1
    assert card.parallel_waves[0].result_count == 1
    assert card.parallel_waves[0].attempt_count == 1
    assert card.skill_uses[0].revision_id == "revision_card"
    assert card.skill_uses[0].content_checksum == "2" * 64
    assert card.skill_uses[0].status == "completed"


def test_execution_card_preserves_completed_step_across_plan_revision(db: Session) -> None:
    """验证 steering 后执行卡沿用同 step_key 的历史成功事实，而不是把证据显示为待执行。"""

    owner, _, instance = _seed_execution(db, suffix="card_revision")
    assert owner is not None
    old_plan = ExecutionPlanRevision(
        id="plan_card_old",
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        revision_number=1,
        status="superseded",
        plan_json={"goal": "核验合同", "success_criteria": [], "steps": []},
        checksum="b" * 64,
        capability_snapshot_json={"model": {"id": "model"}},
        capability_checksum="c" * 64,
    )
    new_plan = ExecutionPlanRevision(
        id="plan_card_new",
        tenant_id=instance.tenant_id,
        execution_id=instance.id,
        revision_number=2,
        parent_revision_id=old_plan.id,
        reason="user_constraint",
        status="active",
        plan_json={
            "goal": "核验合同",
            "success_criteria": [],
            "steps": [
                {"step_key": "read_contract", "title": "读取合同", "kind": "tool.read"},
                {"step_key": "answer", "title": "形成答复", "kind": "answer"},
            ],
        },
        checksum="d" * 64,
        capability_snapshot_json={"model": {"id": "model"}},
        capability_checksum="e" * 64,
    )
    instance.current_plan_revision_id = new_plan.id
    db.add(old_plan)
    db.add(new_plan)
    db.add(instance)
    db.add(
        SopNodeExecution(
            tenant_id=instance.tenant_id,
            instance_id=instance.id,
            node_id="read_contract",
            step_key="read_contract",
            plan_revision_id=old_plan.id,
            step_kind="tool.read",
            title="读取合同",
            status="succeeded",
        )
    )
    db.commit()

    card = get_execution(instance.id, instance.tenant_id, owner, db)

    assert card.steps[0]["status"] == "succeeded"
    assert card.steps[1]["status"] == "pending"


def test_steer_stays_pending_and_idempotency_conflict_is_rejected(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证开关开启后 steer 持久等待 worker，且同 command id 不得改写 payload。"""

    monkeypatch.setattr(
        "app.api.executions.get_settings",
        lambda: SimpleNamespace(dynamic_task_steering_enabled=True),
    )

    owner, _, instance = _seed_execution(db, suffix="steer")
    assert owner is not None
    request = ExecutionCommandRequest(
        tenant_id="tenant_demo",
        command_id="steer_1",
        command_type="steer",
        expected_revision=instance.revision,
        payload={"instruction": "仅看今年"},
    )
    first = issue_execution_command(instance.id, request, owner, db)
    assert first.status == "pending"
    replay = issue_execution_command(instance.id, request, owner, db)
    assert replay.status == "pending"

    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            request.model_copy(update={"payload": {"instruction": "改看去年"}}),
            owner,
            db,
        )
    assert caught.value.status_code == 409


def test_unrelated_user_cannot_cancel_execution(db: Session) -> None:
    """验证平台同租户身份本身不授予 Execution 管理能力。"""

    _, outsider, instance = _seed_execution(db, suffix="forbidden")
    assert outsider is not None
    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            ExecutionCommandRequest(
                tenant_id="tenant_demo",
                command_id="cancel_forbidden",
                command_type="cancel",
                expected_revision=instance.revision,
            ),
            outsider,
            db,
        )
    assert caught.value.status_code == 403


def test_cancel_disposes_pending_steer_before_terminal(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证取消会明确拒绝尚未消费的 steer，不会被旧约束永久阻塞或在终态后应用。"""

    monkeypatch.setattr(
        "app.api.executions.get_settings",
        lambda: SimpleNamespace(dynamic_task_steering_enabled=True),
    )
    owner, _, instance = _seed_execution(db, suffix="steer_then_cancel")
    assert owner is not None
    issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="steer_before_cancel",
            command_type="steer",
            expected_revision=instance.revision,
            payload={"instruction": "change scope"},
        ),
        owner,
        db,
    )
    cancel = issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="cancel_after_steer",
            command_type="cancel",
            expected_revision=instance.revision,
            payload={"reason": "stop"},
        ),
        owner,
        db,
    )
    assert cancel.status == "applied"
    steer = db.exec(
        select(ExecutionCommand).where(
            ExecutionCommand.command_id == "steer_before_cancel"
        )
    ).one()
    assert steer.status == "rejected"
    assert steer.reason_code == "EXECUTION_CANCELLED"


def test_steer_api_rejects_new_command_when_kill_switch_is_off(db: Session) -> None:
    """验证默认关闭 steering 时不写命令或 signal，避免半开启能力。"""

    owner, _, instance = _seed_execution(db, suffix="steer_disabled")
    assert owner is not None
    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            ExecutionCommandRequest(
                tenant_id="tenant_demo",
                command_id="steer_disabled_1",
                command_type="steer",
                expected_revision=instance.revision,
                payload={"instruction": "仅看今年"},
            ),
            owner,
            db,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "DYNAMIC_STEERING_DISABLED"
    assert db.exec(select(ExecutionCommand)).first() is None


def test_add_skill_api_stays_pending_behind_its_own_kill_switch(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证结构化加 Skill 使用独立灰度开关，开启后由持久 worker 异步消费。"""

    monkeypatch.setattr(
        "app.api.executions.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_steering_enabled=False,
            dynamic_task_skill_loading_enabled=True,
        ),
    )
    owner, _, instance = _seed_execution(db, suffix="add_skill")
    assert owner is not None
    result = issue_execution_command(
        instance.id,
        ExecutionCommandRequest(
            tenant_id="tenant_demo",
            command_id="add_skill_api_1",
            command_type="add_skill",
            expected_revision=instance.revision,
            payload={"skill_id": "gskill_writing"},
        ),
        owner,
        db,
    )

    assert result.status == "pending"
    command = db.exec(select(ExecutionCommand)).one()
    assert command.payload_json == {"skill_id": "gskill_writing", "trigger": "user"}


def test_add_skill_api_rejects_new_command_when_kill_switch_is_off(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证运行中加载默认关闭时不留下半生效命令或 signal。"""

    monkeypatch.setattr(
        "app.api.executions.get_settings",
        lambda: SimpleNamespace(
            dynamic_task_steering_enabled=True,
            dynamic_task_skill_loading_enabled=False,
        ),
    )
    owner, _, instance = _seed_execution(db, suffix="add_skill_disabled")
    assert owner is not None
    with pytest.raises(HTTPException) as caught:
        issue_execution_command(
            instance.id,
            ExecutionCommandRequest(
                tenant_id="tenant_demo",
                command_id="add_skill_disabled_1",
                command_type="add_skill",
                expected_revision=instance.revision,
                payload={"skill_id": "gskill_writing"},
            ),
            owner,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "DYNAMIC_SKILL_LOADING_DISABLED"
    assert db.exec(select(ExecutionCommand)).first() is None
