"""
@Time       : 2026/08/03 22:42
@Author     : zhanglp8181
@File       : test_dynamic_execution_planning.py
@CallChain  : pytest → planning contracts/SopExecutionStore → unified execution tables
@Description: 验证动态计划追加、稳定步骤、完整提案账本和既有 SOP 兼容契约。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ActionProposalRecord,
    AgentProfile,
    ExecutionMutationRejection,
    ExecutionPlanRevision,
    GeneralSkillUse,
    ManagedInputResource,
    Message,
    SopInstance,
    SopNodeExecution,
    User,
)
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    FormalSopPlanner,
    NormalizedPlan,
    PlanReason,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_store import (
    SopExecutionConflictError,
    SopExecutionFencedError,
    SopExecutionStore,
)
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card


def _test_session() -> Session:
    """创建包含 B0.4 完整元数据的共享内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _plan(*steps: PlanStep) -> NormalizedPlan:
    """构造带可验证成功标准的最小规范动态计划。"""

    return NormalizedPlan(
        goal="汇总华东经营数据并说明异常",
        success_criteria=(
            SuccessCriterion(id="summary_ready", type="assertion", spec={"required": True}),
        ),
        steps=steps
        or (
            PlanStep(
                step_key="read_sales",
                title="读取销售数据",
                kind="tool.read",
                capability_refs=("sales.read",),
            ),
            PlanStep(
                step_key="summarize",
                title="形成摘要",
                kind="answer",
                depends_on=("read_sales",),
            ),
        ),
        budget={"max_steps": 8},
    )


def _start_dynamic(
    store: SopExecutionStore,
    plan: NormalizedPlan | None = None,
):
    """创建绑定 Agent、发起人、能力快照和首个计划的动态 Execution。"""

    if store.db.get(User, "user_owner") is None:
        store.db.add(
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                password_hash="x",
            )
        )
    if store.db.get(AgentProfile, "agent_ops") is None:
        store.db.add(
            AgentProfile(
                id="agent_ops",
                tenant_id="tenant_demo",
                name="运营数字员工",
                owner_user_id="user_owner",
            )
        )
    store.db.flush()

    return store.start_dynamic_instance(
        tenant_id="tenant_demo",
        session_id="session_dynamic",
        agent_id="agent_ops",
        initiator_user_id="user_owner",
        plan=plan or _plan(),
        capability_snapshot={"tools": [{"id": "sales.read", "checksum": "a" * 64}]},
    )


def test_dynamic_execution_uses_same_envelope_without_fake_sop_identity() -> None:
    """验证动态任务复用统一 Execution，但不会伪造已发布 SOP 版本。"""

    with _test_session() as db:
        instance, revision = _start_dynamic(SopExecutionStore(db))
        repeated_instance, repeated_revision = _start_dynamic(SopExecutionStore(db))
        with pytest.raises(SopExecutionConflictError, match="语义不同"):
            _start_dynamic(
                SopExecutionStore(db),
                _plan(
                    PlanStep(
                        step_key="different",
                        title="不同任务",
                        kind="answer",
                    )
                ),
            )
        db.commit()
        values = {
            "instance_id": instance.id,
            "kind": instance.kind,
            "skill_id": instance.skill_id,
            "skill_version_id": instance.skill_version_id,
            "current_plan_revision_id": instance.current_plan_revision_id,
            "plan_id": revision.id,
            "plan_status": revision.status,
            "revision_number": revision.revision_number,
            "instance_checksum": instance.current_plan_checksum,
            "plan_checksum": revision.checksum,
            "repeated_instance_id": repeated_instance.id,
            "repeated_revision_id": repeated_revision.id,
        }

    assert values["kind"] == "dynamic_task"
    assert values["skill_id"] is None
    assert values["skill_version_id"] is None
    assert values["current_plan_revision_id"] == values["plan_id"]
    assert values["plan_status"] == "active"
    assert values["revision_number"] == 1
    assert values["instance_checksum"] == values["plan_checksum"]
    assert values["repeated_instance_id"] == values["instance_id"]
    assert values["repeated_revision_id"] == values["plan_id"]


def test_dynamic_execution_run_number_advances_after_terminal_history() -> None:
    """验证同一会话结束后再次创建动态 Execution 使用递增 run number 而非固定为一。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        first, _ = _start_dynamic(store)
        first.status = "succeeded"
        first.active_slot_key = None
        db.add(first)
        db.commit()
        second, _ = _start_dynamic(store)
        db.commit()
        run_numbers = (first.run_number, second.run_number)

    assert run_numbers == (1, 2)


def test_replan_preserves_completed_step_and_requires_new_key_for_changed_step() -> None:
    """验证已完成步骤不可移除，未执行步骤改变语义时也必须换新 step key。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance, revision = _start_dynamic(store)
        with store.owned(instance, worker_id="worker-plan"):
            completed = store.enter_node(
                instance,
                "read_sales",
                step_key="read_sales",
                plan_revision_id=revision.id,
                step_kind="tool.read",
            )
            store.complete_node(instance, completed, output={"rows": 12})
        db.commit()

        changed_same_key = _plan(
            PlanStep(
                step_key="read_sales",
                title="改为读取全部数据",
                kind="tool.read",
                capability_refs=("sales.read",),
            ),
            PlanStep(step_key="summarize", title="形成摘要", kind="answer"),
        )
        with pytest.raises(SopExecutionConflictError, match="新的 step key"):
            with store.owned(instance, worker_id="worker-plan"):
                store.append_plan_revision(
                    instance,
                    plan=changed_same_key,
                    reason=PlanReason.EXTERNAL_CHANGE,
                    capability_snapshot={"tools": [{"id": "sales.read", "checksum": "a" * 64}]},
                )
        db.rollback()
        db.refresh(instance)

        valid = _plan(
            PlanStep(
                step_key="read_sales",
                title="读取销售数据",
                kind="tool.read",
                capability_refs=("sales.read",),
            ),
            PlanStep(
                step_key="summarize_v2",
                title="按新约束形成摘要",
                kind="answer",
                depends_on=("read_sales",),
            ),
        )
        with store.owned(instance, worker_id="worker-plan"):
            replan_proposal, _ = store.record_action_proposal(
                instance,
                completed,
                provider="openai_compatible",
                model="model-demo",
                model_capability_snapshot={"structured_output": True},
                completed_response=CompletedProviderProposal(
                    response_id="response-replan-1",
                    finish_reason="stop",
                    proposal=RuntimeActionProposal(
                        action_kind=ActionKind.REPLAN,
                        arguments={"reason": "用户增加约束"},
                        rationale="需要追加计划修订",
                    ),
                ),
            )
            next_revision, created = store.append_plan_revision(
                instance,
                plan=valid,
                reason=PlanReason.USER_CONSTRAINT,
                capability_snapshot={"tools": [{"id": "sales.read", "checksum": "a" * 64}]},
                created_by_proposal_id=replan_proposal.id,
            )
            restored_revision, restored_created = store.append_plan_revision(
                instance,
                plan=_plan(),
                reason=PlanReason.EXTERNAL_CHANGE,
                capability_snapshot={"tools": [{"id": "sales.read", "checksum": "a" * 64}]},
            )
        db.commit()
        revisions = db.exec(
            select(ExecutionPlanRevision).order_by(ExecutionPlanRevision.revision_number)
        ).all()
        completed_identity = (completed.step_key, completed.plan_revision_id)
        revision_id = revision.id
        replan_consumption = (
            replan_proposal.status,
            replan_proposal.consumed_plan_revision_id,
        )

    assert created is True
    assert restored_created is True
    assert next_revision.revision_number == 2
    assert restored_revision.revision_number == 3
    assert restored_revision.checksum == revision.checksum
    assert replan_consumption == ("consumed", next_revision.id)
    assert [row.status for row in revisions] == ["superseded", "superseded", "active"]
    assert completed_identity == ("read_sales", revision_id)


def test_replan_rejects_guidance_use_outside_execution_or_frozen_revision() -> None:
    """验证计划不能伪造其他 Execution 的 Use，也不能绕过固定 revision/checksum。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance, _ = _start_dynamic(store)
        use = GeneralSkillUse(
            id="gsuse_foreign",
            tenant_id=instance.tenant_id,
            session_id=instance.session_id,
            turn_id="turn_foreign",
            execution_id="another_execution",
            agent_id=instance.agent_id,
            user_id=instance.initiator_user_id,
            skill_id="genskill_one",
            revision_id="gsrev_one",
            content_checksum="b" * 64,
            selection_mode="forced",
            idempotency_key="c" * 64,
            status="active",
        )
        db.add(use)
        db.commit()
        revised = _plan(
            PlanStep(
                step_key="read_sales_skill",
                title="读取销售数据",
                kind="tool.read",
                capability_refs=("sales.read",),
                guidance_skill_use_ids=(use.id,),
            ),
            PlanStep(
                step_key="summarize_skill",
                title="形成摘要",
                kind="answer",
                depends_on=("read_sales_skill",),
                guidance_skill_use_ids=(use.id,),
            ),
        )

        with pytest.raises(SopExecutionConflictError, match="不属于当前活动 Execution"):
            with store.owned(instance, worker_id="worker-forged-use"):
                store.append_plan_revision(
                    instance,
                    plan=revised,
                    reason=PlanReason.SKILL_ADDED,
                    capability_snapshot={
                        "tools": [{"id": "sales.read", "checksum": "a" * 64}],
                        "general_skill_uses": [
                            {
                                "use_id": use.id,
                                "skill_id": use.skill_id,
                                "revision_id": use.revision_id,
                                "content_checksum": use.content_checksum,
                            }
                        ],
                    },
                )
def test_completed_provider_proposal_is_persisted_and_consumed_idempotently() -> None:
    """验证完整提案落库后恢复复用同一记录，消费不能改绑另一 Operation。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance, revision = _start_dynamic(store)
        response = CompletedProviderProposal(
            response_id="response-1",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref="sales.read",
                arguments={"region": "east"},
                rationale="需要读取受管销售数据",
            ),
            usage={"input_tokens": 100, "output_tokens": 30},
        )
        with store.owned(instance, worker_id="worker-proposal"):
            execution = store.enter_node(
                instance,
                "read_sales",
                step_key="read_sales",
                plan_revision_id=revision.id,
                step_kind="tool.read",
            )
            proposal, created = store.record_action_proposal(
                instance,
                execution,
                provider="openai_compatible",
                model="model-demo",
                model_capability_snapshot={"structured_output": True, "required_tool_call": True},
                completed_response=response,
            )
            repeated, repeated_created = store.record_action_proposal(
                instance,
                execution,
                provider="openai_compatible",
                model="model-demo",
                model_capability_snapshot={"structured_output": True, "required_tool_call": True},
                completed_response=response,
            )
            with pytest.raises(SopExecutionConflictError, match="provider response"):
                store.record_action_proposal(
                    instance,
                    execution,
                    provider="openai_compatible",
                    model="model-demo",
                    model_capability_snapshot={
                        "structured_output": True,
                        "required_tool_call": True,
                    },
                    completed_response=response.model_copy(
                        update={
                            "proposal": response.proposal.model_copy(
                                update={"arguments": {"region": "west"}}
                            )
                        }
                    ),
                )
            unrelated_execution = store.enter_node(
                instance,
                "summarize",
                step_key="summarize",
                plan_revision_id=revision.id,
                step_kind="answer",
            )
            unrelated_operation, _ = store.prepare_operation(
                instance,
                unrelated_execution,
                operation_name="local.summary",
                request={"format": "markdown"},
                logical_action_id="unrelated-summary",
            )
            with pytest.raises(SopExecutionConflictError, match="同一 Execution"):
                store.consume_action_proposal(
                    instance,
                    proposal,
                    operation_id=unrelated_operation.id,
                )
            operation, operation_created = store.prepare_operation_from_proposal(
                instance,
                execution,
                proposal,
                operation_name="sales.read",
                request={"region": "east"},
            )
            repeated_operation, repeated_operation_created = store.prepare_operation_from_proposal(
                instance,
                execution,
                proposal,
                operation_name="sales.read",
                request={"region": "east"},
            )
            assert store.consume_action_proposal(
                instance,
                proposal,
                operation_id=operation.id,
            ) is False
            other_operation, _ = store.prepare_operation(
                instance,
                execution,
                operation_name="sales.read",
                request={"region": "west"},
                logical_action_id="other-read",
            )
            with pytest.raises(SopExecutionConflictError, match="不得改绑"):
                store.consume_action_proposal(
                    instance,
                    proposal,
                    operation_id=other_operation.id,
                )
            proposal_ids = (proposal.id, repeated.id)
            operation_ids = (operation.id, repeated_operation.id)
        db.commit()
        rows = db.exec(select(ActionProposalRecord)).all()

    assert created is True
    assert repeated_created is False
    assert proposal_ids[0] == proposal_ids[1]
    assert operation_created is True
    assert repeated_operation_created is False
    assert operation_ids[0] == operation_ids[1]
    assert len(rows) == 1
    assert rows[0].status == "consumed"
    assert rows[0].provider_response_id == "response-1"
    assert rows[0].finish_reason == "stop"
    assert rows[0].validation_json["capability_scope_validated"] is True


def test_partial_or_identity_overriding_provider_output_cannot_enter_proposal_ledger() -> None:
    """验证流式半包及试图覆盖服务端身份/授权的输出无法形成 ProposalRecord。"""

    with pytest.raises(ValidationError):
        CompletedProviderProposal.model_validate(
            {
                "response_id": "response-partial",
                "finish_reason": "length",
                "proposal": {
                    "action_kind": "call_tool",
                    "capability_ref": "sales.read",
                    "arguments": {"region": "east"},
                    "rationale": "尚未完成",
                },
            }
        )
    with pytest.raises(ValidationError, match="不得覆盖"):
        RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref="sales.read",
            arguments={"tenant_id": "tenant_other"},
            rationale="尝试覆盖服务端身份",
        )
    with pytest.raises(ValidationError, match="不得覆盖"):
        RuntimeActionProposal(
            action_kind=ActionKind.CALL_TOOL,
            capability_ref="sales.read",
            arguments={"payload": {"authorized": True}},
            rationale="尝试嵌套覆盖授权",
        )

    with _test_session() as db:
        assert db.exec(select(ActionProposalRecord)).all() == []


def test_plan_rejects_cycles_and_capabilities_missing_from_frozen_catalog() -> None:
    """验证计划必须是 DAG，且不能引用未进入 B0.3 冻结目录的能力。"""

    with pytest.raises(ValidationError, match="无环图"):
        _plan(
            PlanStep(
                step_key="first",
                title="第一步",
                kind="answer",
                depends_on=("second",),
            ),
            PlanStep(
                step_key="second",
                title="第二步",
                kind="answer",
                depends_on=("first",),
            ),
        )
    unavailable = _plan(
        PlanStep(
            step_key="read_finance",
            title="读取财务数据",
            kind="tool.read",
            capability_refs=("finance.read",),
        )
    )
    with _test_session() as db, pytest.raises(
        SopExecutionConflictError,
        match="未冻结的动态能力",
    ):
        _start_dynamic(SopExecutionStore(db), unavailable)


def test_proposal_cannot_use_capability_not_declared_by_current_step() -> None:
    """验证能力虽在冻结目录中，若当前计划步骤未声明也不能写入提案账本。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance, revision = _start_dynamic(store)
        with store.owned(instance, worker_id="worker-proposal-scope"):
            execution = store.enter_node(
                instance,
                "summarize",
                step_key="summarize",
                plan_revision_id=revision.id,
                step_kind="answer",
            )
            response = CompletedProviderProposal(
                response_id="response-undeclared",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL,
                    capability_ref="sales.read",
                    arguments={"region": "east"},
                    rationale="试图在回答步骤调用读取能力",
                ),
            )
            with pytest.raises(SopExecutionConflictError, match="当前计划步骤冻结"):
                store.record_action_proposal(
                    instance,
                    execution,
                    provider="openai_compatible",
                    model="model-demo",
                    model_capability_snapshot={"structured_output": True},
                    completed_response=response,
                )


def test_formal_sop_nodes_keep_node_id_as_stable_step_key() -> None:
    """验证正式 SOP 进入节点后仍保持旧 node id/attempt 行为并补充统一 step identity。"""

    with _test_session() as db:
        store = SopExecutionStore(db)
        instance, _ = store.start_instance(
            tenant_id="tenant_demo",
            session_id="session_sop",
            skill_id="skill_demo",
            skill_version_id="skillver_demo",
            skill_version="1.0.0",
            definition_checksum="b" * 64,
            start_node_id="collect",
        )
        with store.owned(instance, worker_id="worker-sop"):
            execution = store.enter_node(instance, "collect")
        db.commit()
        rows = db.exec(select(SopNodeExecution)).all()

    assert len(rows) == 1
    assert execution.node_id == "collect"
    assert execution.step_key == "collect"
    assert execution.plan_revision_id is None


def test_formal_sop_planner_is_read_only_projection_of_published_definition() -> None:
    """验证 FormalSopPlanner 保留节点身份与依赖，但不会把 SOP 伪装成动态 PlanRevision。"""

    definition = compile_legacy_skill_card(
        {
            "skill_id": "skill_projection",
            "version": "1.0.0",
            "name": "投影测试",
            "business_domain": "testing",
            "description": "验证统一 Planner 协议",
            "nodes": [
                {
                    "node_id": "collect",
                    "type": "collect_info",
                    "name": "收集信息",
                    "allowed_actions": ["ask_user"],
                },
                {
                    "node_id": "reply",
                    "type": "response",
                    "name": "回复结果",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [{"source_node_id": "collect", "next_node_id": "reply"}],
            "start_node_id": "collect",
            "terminal_node_ids": ["reply"],
        }
    )

    plan = FormalSopPlanner(definition).create_plan()

    assert [step.step_key for step in plan.steps] == ["collect", "reply"]
    assert plan.steps[1].depends_on == ("collect",)
    assert plan.budget["definition_checksum"] == definition.checksum


def test_expired_worker_cannot_write_plan_step_proposal_or_input_snapshot(tmp_path) -> None:
    """验证 B0.4 全部新增权威写都服从同一 lease/fencing token 并留下隔离审计。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'dynamic-planning-fencing.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as seed_db:
        instance, _ = _start_dynamic(SopExecutionStore(seed_db))
        seed_db.commit()
        instance_id = instance.id

    with Session(engine) as worker_a_db:
        instance_a = worker_a_db.get(SopInstance, instance_id)
        assert instance_a is not None
        store_a = SopExecutionStore(worker_a_db)
        with store_a.owned(instance_a, worker_id="worker-a") as lease_a:
            execution = store_a.enter_node(
                instance_a,
                "read_sales",
                step_key="read_sales",
                plan_revision_id=instance_a.current_plan_revision_id,
                step_kind="tool.read",
            )
            response = CompletedProviderProposal(
                response_id="response-fencing",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL,
                    capability_ref="sales.read",
                    arguments={"region": "east"},
                    rationale="读取数据",
                ),
            )
            proposal, _ = store_a.record_action_proposal(
                instance_a,
                execution,
                provider="openai_compatible",
                model="model-demo",
                model_capability_snapshot={"structured_output": True},
                completed_response=response,
            )
            prepared_operation, _ = store_a.prepare_operation(
                instance_a,
                execution,
                operation_name="sales.read",
                request={"region": "east"},
                logical_action_id="proposal-fencing-prepared",
            )
            execution_id = execution.id
            proposal_id = proposal.id
            resource = ManagedInputResource(
                tenant_id="tenant_demo",
                owner_user_id="user_owner",
                agent_id="agent_ops",
                version="d" * 64,
                filename="sales.txt",
                mime_type="text/plain",
                size_bytes=4,
                content_checksum="d" * 64,
                extraction_checksum="e" * 64,
                ingestion_status="ready",
                storage_locator="tenant/resource/checksum",
            )
            message = Message(
                id="message-fencing",
                tenant_id="tenant_demo",
                session_id="session_dynamic",
                role="user",
                content="读取销售数据",
                metadata_json={
                    "attachments": [
                        {
                            "resource_id": resource.id,
                            "resource_version": resource.version,
                            "content_checksum": resource.content_checksum,
                        }
                    ]
                },
            )
            worker_a_db.add(resource)
            worker_a_db.add(message)
            worker_a_db.commit()

            with Session(engine) as expiry_db:
                expiry_db.exec(
                    update(SopInstance)
                    .where(SopInstance.id == instance_id)
                    .values(lease_expires_at=datetime(2000, 1, 1))
                )
                expiry_db.commit()
            with Session(engine) as worker_b_db:
                instance_b = worker_b_db.get(SopInstance, instance_id)
                assert instance_b is not None
                with SopExecutionStore(worker_b_db).owned(
                    instance_b,
                    worker_id="worker-b",
                ) as lease_b:
                    assert lease_b.fencing_token > lease_a.fencing_token
                worker_b_db.commit()

            next_plan = _plan(
                PlanStep(
                    step_key="read_sales",
                    title="读取销售数据",
                    kind="tool.read",
                    capability_refs=("sales.read",),
                ),
                PlanStep(step_key="summarize_v2", title="形成新摘要", kind="answer"),
            )
            mutations = (
                (
                    "plan.append",
                    lambda: store_a.append_plan_revision(
                        instance_a,
                        plan=next_plan,
                        reason=PlanReason.USER_CONSTRAINT,
                        capability_snapshot={
                            "tools": [{"id": "sales.read", "checksum": "a" * 64}]
                        },
                    ),
                ),
                (
                    "node.complete",
                    lambda: store_a.complete_node(instance_a, execution, output={"late": True}),
                ),
                (
                    "proposal.record",
                    lambda: store_a.record_action_proposal(
                        instance_a,
                        execution,
                        provider="openai_compatible",
                        model="model-demo",
                        model_capability_snapshot={"structured_output": True},
                        completed_response=response.model_copy(
                            update={"response_id": "response-fencing-late"}
                        ),
                    ),
                ),
                (
                    "proposal.consume",
                    lambda: store_a.consume_action_proposal(
                        instance_a,
                        proposal,
                        operation_id=prepared_operation.id,
                    ),
                ),
                (
                    "input.snapshot",
                    lambda: store_a.snapshot_input_resource(
                        instance_a,
                        resource,
                        source_message_id="message-fencing",
                    ),
                ),
            )
            expected_actions = tuple(item[0] for item in mutations)
            for expected_action, mutation in mutations:
                with pytest.raises(SopExecutionFencedError) as caught:
                    mutation()
                assert caught.value.action == expected_action
            worker_a_db.rollback()

    with Session(engine) as verify_db:
        persisted_step = verify_db.get(SopNodeExecution, execution_id)
        persisted_proposal = verify_db.get(ActionProposalRecord, proposal_id)
        rejections = verify_db.exec(
            select(ExecutionMutationRejection).order_by(ExecutionMutationRejection.created_at)
        ).all()

    assert persisted_step is not None and persisted_step.status == "running"
    assert persisted_proposal is not None and persisted_proposal.status == "validated"
    assert [row.action for row in rejections] == list(expected_actions)
