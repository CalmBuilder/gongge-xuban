"""
@Time       : 2026/07/22 10:23
@Author     : zhanglp8181
@File       : test_sop_work_item_coordinator.py
@CallChain  : pytest → DeterministicSopCoordinator → SopWorkItemService → SOP 恢复
@Description: 验证结构化人工节点从候选工作项等待到审批结果恢复和终态收口。
"""

from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentEvent,
    BusinessRole,
    ChatSession,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Message,
    Skill,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
    utc_now,
)
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.work_items import SopWorkItemService
from app.tools.tool_schema import ToolResult


def test_coordinator_waits_for_work_item_then_resumes_to_approved_terminal() -> None:
    """验证人工节点创建候选快照、持久等待并由完成回执恢复到批准终态。"""

    with _test_session() as db:
        skill, chat_session = _seed_approval_runtime(db, with_approver=True)
        coordinator = DeterministicSopCoordinator(db)

        waiting_result = coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
        )
        db.commit()
        work_item = db.exec(select(SopWorkItem)).one()
        instance = db.exec(select(SopInstance)).one()
        service = SopWorkItemService(db)

        service.claim(
            work_item,
            actor_user_id="approver",
            command_id="claim-approval-test",
        )
        service.complete(
            work_item,
            actor_user_id="approver",
            command_id="complete-approval-test",
            outcome="approved",
            comment="同意",
        )
        resume_plan = coordinator.resume_completed_work_item(work_item)
        replay_plan = coordinator.resume_completed_work_item(work_item)
        db.commit()

        db.refresh(instance)
        executions = db.exec(select(SopNodeExecution).order_by(SopNodeExecution.created_at)).all()
        event_types = {event.event_type for event in db.exec(select(AgentEvent)).all()}
        completion_messages = db.exec(select(Message).where(Message.role == "assistant")).all()

        assert waiting_result.reply == "申请已提交人工处理，当前正在等待有权限的处理人。"
        assert waiting_result.is_runtime_control_reply() is True
        assert work_item.initiator_user_id == "applicant"
        assert work_item.candidate_snapshot_json == [
            {
                "user_id": "approver",
                "employee_profile_id": "profile_approver",
                "source_role_codes": ["seal.approver"],
                "source_types": ["business_role"],
            }
        ]
        assert resume_plan.action == "complete"
        assert replay_plan.action == "complete"
        assert instance.status == "succeeded"
        assert [execution.status for execution in executions] == ["succeeded", "succeeded"]
        assert {"sop_work_item_offered", "sop_work_item_resumed"} <= event_types
        assert [message.content for message in completion_messages] == [
            "您的申请已审批通过，流程已完成。"
        ]
        assert completion_messages[0].metadata_json["render_policy"] == "verbatim"


def test_coordinator_fails_deterministically_when_no_eligible_candidate_exists() -> None:
    """验证参与者解析为空时失败实例，而不是产生任何人都能处理的开放任务。"""

    with _test_session() as db:
        skill, chat_session = _seed_approval_runtime(db, with_approver=False)

        result = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
        )
        db.commit()
        instance = db.exec(select(SopInstance)).one()
        execution = db.exec(select(SopNodeExecution)).one()

        assert result.action == "reply"
        assert "WORK_ITEM_NO_ELIGIBLE_CANDIDATE" in result.reply
        assert instance.status == "failed"
        assert execution.status == "failed"
        assert db.exec(select(SopWorkItem)).all() == []


def test_completed_work_item_resumes_and_executes_following_tool(monkeypatch) -> None:
    """验证人工任务批准后由 Runtime 执行后继工具、记录回执并以工具结果通知申请人。"""

    with _test_session() as db:
        skill, chat_session = _seed_approval_runtime(db, with_approver=True)
        content = _approval_then_tool_content()
        definition = compile_legacy_skill_card(content)
        skill.version = "2.0.0"
        skill.content_json = content
        version = db.get(SkillVersion, "skill_version_approval_test")
        assert version is not None
        version.version = skill.version
        version.content_json = content
        version.compiled_definition_checksum = definition.checksum
        db.add(skill)
        db.add(version)
        db.commit()

        observed_execution_scope: list[str | None] = []

        def execute_tool(*_args, **kwargs) -> ToolResult:
            """返回确定性授权回执，隔离本用例与 HTTP 传输。"""

            observed_execution_scope.append(kwargs.get("execution_org_unit_id"))

            return ToolResult(
                tool_name="demo.grant",
                success=True,
                data={"status": "granted", "grant_id": "GRANT-TEST"},
            )

        monkeypatch.setattr(
            "app.sop_runtime.coordinator.ToolExecutor.execute",
            execute_tool,
        )
        coordinator = DeterministicSopCoordinator(db)
        coordinator.prepare_step(chat_session, skill, StepAgentResult())
        work_item = db.exec(select(SopWorkItem)).one()
        service = SopWorkItemService(db)
        service.claim(work_item, actor_user_id="approver", command_id="claim-tool")
        service.complete(
            work_item,
            actor_user_id="approver",
            command_id="approve-tool",
            outcome="approved",
            comment="范围合规",
        )
        work_item.participant_scope_snapshot_json = {
            "schema_version": 1,
            "resolver": "explicit_org",
            "root_org_unit_id": "org_finance",
            "organization_unit_ids": ["org_finance"],
        }
        db.add(work_item)
        db.flush()

        plan = coordinator.resume_completed_work_item(work_item)
        db.commit()

        instance = db.exec(select(SopInstance)).one()
        operation = db.exec(select(SopOperation)).one()
        messages = db.exec(select(Message).where(Message.role == "assistant")).all()
        events = db.exec(select(AgentEvent)).all()
        assert plan.action == "complete"
        assert instance.status == "succeeded"
        assert operation.status == "succeeded"
        assert operation.result_json["grant_id"] == "GRANT-TEST"
        assert messages[-1].content == "审批通过；状态 granted；单号 GRANT-TEST；意见：范围合规。"
        assert [event.event_type for event in events].count("tool_call_started") == 1
        assert [event.event_type for event in events].count("tool_call_finished") == 1
        assert observed_execution_scope == ["org_finance"]


def test_due_work_item_times_out_waiting_instance_and_notifies_applicant() -> None:
    """验证后台到期扫描把工作项、节点和实例统一推进为超时终态并原样通知。"""

    with _test_session() as db:
        skill, chat_session = _seed_approval_runtime(db, with_approver=True)
        coordinator = DeterministicSopCoordinator(db)
        coordinator.prepare_step(chat_session, skill, StepAgentResult())
        work_item = db.exec(select(SopWorkItem)).one()
        work_item.expires_at = utc_now() - timedelta(seconds=1)
        db.add(work_item)
        db.flush()

        expired = SopWorkItemService(db).expire_due(now=utc_now())
        coordinator.timeout_expired_work_item(expired[0])
        db.commit()

        instance = db.exec(select(SopInstance)).one()
        execution = db.exec(select(SopNodeExecution)).one()
        messages = db.exec(select(Message).where(Message.role == "assistant")).all()
        assert work_item.status == "expired"
        assert work_item.timeout_action == "fail"
        assert instance.status == "timed_out"
        assert execution.status == "timed_out"
        assert messages[-1].content == (
            "您的申请因超过处理时限未完成，流程已终止，请重新发起或联系负责人。"
        )
        assert messages[-1].metadata_json["render_policy"] == "verbatim"


def test_completion_template_does_not_describe_pending_or_failure_as_granted() -> None:
    """验证人工批准后的业务待处理和工具失败会原样进入通知，不被写成已开通。"""

    template = "审批通过；状态 {business_status}；单号 {grant_id}；意见：{comment}。"
    with _test_session() as db:
        coordinator = DeterministicSopCoordinator(db)
        pending_instance = SopInstance(
            tenant_id="tenant_demo",
            session_id="session_pending",
            skill_id="approval_test",
            skill_version_id="version_pending",
            skill_version="2.0.0",
            definition_checksum="checksum",
            context_json={
                "tool_results": {
                    "grant": {
                        "status": "succeeded",
                        "data": {"status": "pending", "grant_id": "GRANT-PENDING"},
                    }
                }
            },
        )
        failed_instance = SopInstance(
            tenant_id="tenant_demo",
            session_id="session_failed",
            skill_id="approval_test",
            skill_version_id="version_failed",
            skill_version="2.0.0",
            definition_checksum="checksum",
            context_json={
                "tool_results": {
                    "grant": {"status": "failed", "data": {}, "error": {"code": "TIMEOUT"}}
                }
            },
        )

        assert coordinator._render_completion_template(  # noqa: SLF001
            template, "等待外部系统", pending_instance
        ) == "审批通过；状态 pending；单号 GRANT-PENDING；意见：等待外部系统。"
        assert coordinator._render_completion_template(  # noqa: SLF001
            template, "等待重试", failed_instance
        ) == "审批通过；状态 failed；单号 无；意见：等待重试。"


def _approval_content() -> dict[str, object]:
    """构造只包含结构化人工任务和批准、拒绝终态的验收定义。"""

    return {
        "skill_id": "approval_test",
        "name": "审批验收",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "condition_schemas": {
            "work_item": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "outcome": {"type": "string"},
                },
            }
        },
        "nodes": [
            {
                "node_id": "human_review",
                "type": "human_task",
                "name": "人工审批",
                "metadata": {
                    "participant_policy": {
                        "candidate_role_codes": ["seal.approver"],
                        "completion_mode": "any",
                        "claim_required": True,
                        "exclude_initiator": True,
                        "allowed_outcomes": ["approved", "rejected"],
                    }
                },
            },
            {"node_id": "approved", "type": "terminal", "name": "审批通过"},
            {"node_id": "rejected", "type": "terminal", "name": "审批拒绝"},
        ],
        "edges": [
            {
                "source_node_id": "human_review",
                "next_node_id": "approved",
                "condition": {
                    "op": "eq",
                    "left": {"path": "work_item.outcome"},
                    "right": {"value": "approved"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "human_review",
                "next_node_id": "rejected",
                "condition": {"op": "always"},
                "priority": 0,
            },
        ],
        "start_node_id": "human_review",
        "terminal_node_ids": ["approved", "rejected"],
    }


def _approval_then_tool_content() -> dict[str, object]:
    """构造人工批准后继续执行工具并按结构化回执结束的确定性定义。"""

    content = _approval_content()
    human_node = content["nodes"][0]
    human_node["metadata"]["participant_policy"]["outcome_options"] = [
        {
            "value": "approved",
            "label": "批准并执行",
            "tone": "success",
            "comment_required": True,
            "completion_message": (
                "审批通过；状态 {business_status}；单号 {grant_id}；意见：{comment}。"
            ),
        },
        {
            "value": "rejected",
            "label": "拒绝",
            "tone": "danger",
            "comment_required": True,
            "completion_message": "审批未通过；意见：{comment}。",
        },
    ]
    content["nodes"] = [
        human_node,
        {
            "node_id": "grant_access",
            "type": "tool_call",
            "name": "执行授权",
            "allowed_actions": ["call_tool:demo.grant"],
            "metadata": {
                "operation_input": {},
                "operation_result_key": "grant",
            },
        },
        {"node_id": "approved", "type": "terminal", "name": "授权完成"},
        {"node_id": "rejected", "type": "terminal", "name": "审批拒绝"},
    ]
    content["edges"][0]["next_node_id"] = "grant_access"
    content["edges"].append(
        {
            "source_node_id": "grant_access",
            "next_node_id": "approved",
            "condition": {"op": "always"},
            "priority": 0,
        }
    )
    return content


def _seed_approval_runtime(
    db: Session,
    *,
    with_approver: bool,
) -> tuple[Skill, ChatSession]:
    """写入审批定义、申请人以及可选审批人业务任职。"""

    content = _approval_content()
    definition = compile_legacy_skill_card(content)
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="applicant",
            tenant_id="tenant_demo",
            username="applicant",
            password_hash="hash",
        )
    )
    role = BusinessRole(
        id="role_seal_approver",
        tenant_id="tenant_demo",
        role_code="seal.approver",
        name="用章审批人",
    )
    db.add(role)
    if with_approver:
        db.add(
            User(
                id="approver",
                tenant_id="tenant_demo",
                username="approver",
                password_hash="hash",
            )
        )
        db.add(
            EmployeeProfile(
                id="profile_approver",
                tenant_id="tenant_demo",
                user_id="approver",
                employee_id="E200",
            )
        )
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id="profile_approver",
                business_role_id=role.id,
            )
        )
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="approval_test",
        version="1.0.0",
        name="审批验收",
        content_json=content,
        status="published",
    )
    db.add(skill)
    db.add(
        SkillVersion(
            id="skill_version_approval_test",
            tenant_id="tenant_demo",
            skill_id=skill.skill_id,
            version=skill.version,
            name=skill.name,
            content_json=content,
            status="published",
            compiled_definition_checksum=definition.checksum,
        )
    )
    chat_session = ChatSession(
        id="session_approval_test",
        tenant_id="tenant_demo",
        user_id="applicant",
        active_skill_id=skill.skill_id,
        active_step_id="human_review",
    )
    db.add(chat_session)
    db.commit()
    return skill, chat_session


def _test_session() -> Session:
    """创建加载全部 SQLModel 表的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
