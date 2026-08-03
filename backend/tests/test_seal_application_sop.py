"""
@Time       : 2026/07/27 19:45
@Author     : zhanglp8181
@File       : test_seal_application_sop.py
@CallChain  : pytest → 用章定义构造/编译 → 统一 Scheduler/人工任务契约
@Description: 验证第十四个 SOP 的申请、分级审批、状态回写和本人查询图。
"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.demo_sop_versions import (
    SEAL_APPLICATION_DETERMINISTIC_VERSION,
    _seal_application_deterministic_content,
)
from app.db.models import AgentProfile, BusinessRole, Skill, Tool
from app.db.seed import seed_demo_data
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def _definition():
    """编译用章确定性定义并确保没有兼容诊断。"""

    definition = compile_legacy_skill_card(_seal_application_deterministic_content())
    assert definition.diagnostics == ()
    return definition


def test_seal_definition_uses_immutable_v2_and_typed_human_roles() -> None:
    """验证普通和重要申请使用不同候选角色且禁止申请人自审。"""

    definition = _definition()
    assert definition.skill_version == SEAL_APPLICATION_DETERMINISTIC_VERSION
    nodes = {node.node_id: node for node in definition.nodes}
    normal = nodes["normal_seal_approval"].config
    important = nodes["important_seal_approval"].config
    assert normal.candidate_role_codes == ("admin_seal_approver",)
    assert important.candidate_role_codes == ("admin_seal_senior_approver",)
    assert normal.exclude_initiator is True
    assert important.exclude_initiator is True
    assert normal.allowed_outcomes == ("approved", "rejected")


def test_seal_create_waits_for_policy_evidence_and_current_confirmation() -> None:
    """验证零证据和未确认都不能创建用章申请。"""

    definition = _definition()
    no_evidence = plan_next_action(
        definition,
        current_node_id="query_seal_policy",
        slots={},
        node_outputs={
            "seal_policy": {
                "status": "succeeded",
                "data": {"outcome": "no_match"},
            }
        },
    )
    assert no_evidence.next_node_id == "seal_policy_unavailable"
    evidence = plan_next_action(
        definition,
        current_node_id="query_seal_policy",
        slots={},
        node_outputs={
            "seal_policy": {
                "status": "succeeded",
                "data": {"outcome": "evidence_found"},
            }
        },
    )
    assert evidence.next_node_id == "confirm_seal_application"

    waiting = plan_next_action(
        definition,
        current_node_id="confirm_seal_application",
        slots={},
    )
    assert waiting.action is RuntimeAction.WAIT_INPUT
    assert waiting.expected_inputs == ("confirmation",)


def test_seal_application_level_routes_from_tool_receipt() -> None:
    """验证审批级别只由创建工具回执决定，不读取模型自由文本。"""

    definition = _definition()
    important = plan_next_action(
        definition,
        current_node_id="route_seal_approval_level",
        slots={},
        tool_results={
            "seal_application": {
                "status": "succeeded",
                "data": {"status": "pending", "approval_level": "important"},
            }
        },
    )
    normal = plan_next_action(
        definition,
        current_node_id="route_seal_approval_level",
        slots={},
        tool_results={
            "seal_application": {
                "status": "succeeded",
                "data": {"status": "pending", "approval_level": "normal"},
            }
        },
    )
    assert important.next_node_id == "important_seal_approval"
    assert normal.next_node_id == "normal_seal_approval"


def test_seal_human_outcome_routes_to_distinct_finalize_tools() -> None:
    """验证批准和驳回走不同权威回写工具，不能由回复节点直接结束。"""

    definition = _definition()
    approved = plan_next_action(
        definition,
        current_node_id="normal_seal_approval",
        slots={},
        work_items={"status": "completed", "outcome": "approved"},
    )
    rejected = plan_next_action(
        definition,
        current_node_id="normal_seal_approval",
        slots={},
        work_items={"status": "completed", "outcome": "rejected"},
    )
    assert approved.next_node_id == "approve_seal_application"
    assert rejected.next_node_id == "reject_seal_application"


def test_seed_publishes_seal_v2_roles_tools_and_builtin_create() -> None:
    """验证种子发布 v2、分级角色和受控内置创建工具并可实际执行。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == "seal_application_approval")
        ).one()
        roles = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code.in_(
                    (
                        "admin_seal_operator",
                        "admin_seal_approver",
                        "admin_seal_senior_approver",
                    )
                )
            )
        ).all()
        tools = db.exec(
            select(Tool).where(
                Tool.name.in_(
                    (
                        "admin.seal_application_create",
                        "admin.seal_application_approve",
                        "admin.seal_application_reject",
                        "admin.seal_application_query",
                    )
                )
            )
        ).all()
        agent = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "行政",
            )
        ).one()

        assert skill.version == SEAL_APPLICATION_DETERMINISTIC_VERSION
        assert skill.status == "published"
        assert {role.role_code for role in roles} == {
            "admin_seal_operator",
            "admin_seal_approver",
            "admin_seal_senior_approver",
        }
        assert {tool.tool_type for tool in tools} == {"builtin"}
        result = ToolExecutor(db).execute(
            "tenant_demo",
            ToolCall(
                name="admin.seal_application_create",
                arguments={
                    "employee_id": "E002",
                    "employee_name": "演示员工",
                    "seal_type": "company",
                    "seal_purpose": "客户资质证明",
                    "document_name": "合作资质证明",
                    "document_type": "ordinary_document",
                },
            ),
            active_skill_id="seal_application_approval",
            agent_id=agent.id,
            actor_user_id="user_demo",
        )

        assert result.success is True
        assert result.data["status"] == "pending"
        assert result.data["approval_level"] == "normal"
