"""
@Time       : 2026/07/22 17:05
@Author     : zhanglp8181
@File       : test_clause_modification_sop.py
@CallChain  : pytest → 条款修改发布定义 → Scheduler/Tool/参考资料终态
@Description: 验证受限合同类型、相关资料检索、证据边界和法务数字员工受控执行。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.demo_sop_versions import (
    CLAUSE_MODIFICATION_DETERMINISTIC_VERSION,
    CLAUSE_MODIFICATION_SKILL_ID,
    LEGAL_CONTRACT_REFERENCE_SKILL_IDS,
    LEGAL_CONTRACT_RESEARCHER_ROLE,
    _clause_modification_deterministic_content,
)
from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    Skill,
    SkillVersion,
    Tool,
)
from app.db.seed import seed_demo_data
from app.sop_runtime.definition import CompiledSopDefinition
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action
from app.sop_runtime.slot_values import normalize_slot_values


def _definition() -> CompiledSopDefinition:
    """编译零告警的条款修改建议确定性发布定义。"""

    return compile_legacy_skill_card(_clause_modification_deterministic_content({}))


def test_clause_modification_normalizes_contract_type_and_collects_complete_context() -> None:
    """验证合同类型使用发布版本别名，且三项信息均由统一收集节点管理。"""

    definition = _definition()
    slots = normalize_slot_values(
        definition,
        {
            "contract_type": "软件采购合同",
            "clause_content": "供应商承担无限责任。",
            "modification_request": "改为责任有明确上限。",
        },
    )
    collect_node = next(
        node for node in definition.nodes if node.node_id == "node_collect_clause_request"
    )

    assert definition.meta_model_version == 3
    assert definition.diagnostics == ()
    assert slots["contract_type"] == "software_procurement"
    assert collect_node.config.required_inputs == (
        "contract_type",
        "clause_content",
        "modification_request",
    )


def test_clause_modification_maps_only_original_clause_to_reference_query() -> None:
    """验证检索参数来自原条款冻结槽位，不由模型自由拼接或生成引用。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="node_query_clause_reference",
        slots={
            "contract_type": "software_procurement",
            "clause_content": "供应商对任何违约承担无限责任。",
            "modification_request": "设置累计责任上限。",
        },
    )

    assert plan.action == "call_tool"
    assert plan.operation_name == "contract.archive_query"
    assert plan.operation_arguments == {"query": "供应商对任何违约承担无限责任。"}
    assert plan.result_key == "contract_reference"


def test_clause_modification_routes_match_empty_and_failure_to_distinct_terminals() -> None:
    """验证有匹配、零匹配和传输失败不会共用同一含糊回复终态。"""

    matched = plan_next_action(
        _definition(),
        current_node_id="node_query_clause_reference",
        slots={},
        tool_results={
            "contract_reference": {"status": "succeeded", "data": {"total": 2}}
        },
    )
    empty = plan_next_action(
        _definition(),
        current_node_id="node_query_clause_reference",
        slots={},
        tool_results={
            "contract_reference": {"status": "succeeded", "data": {"total": 0}}
        },
    )
    failure = plan_next_action(
        _definition(),
        current_node_id="node_query_clause_reference",
        slots={},
        tool_results={"contract_reference": {"status": "failed", "data": {}}},
    )

    assert matched.next_node_id == "node_clause_suggestion_with_reference"
    assert empty.next_node_id == "node_clause_suggestion_without_reference"
    assert failure.next_node_id == "node_clause_suggestion_failure"


def test_clause_modification_terminal_freezes_evidence_and_legal_review_boundary() -> None:
    """验证有依据终态禁止编造引用，并明确建议不等于正式法务意见。"""

    terminal = next(
        node
        for node in _definition().nodes
        if node.node_id == "node_clause_suggestion_with_reference"
    )

    assert "只能引用本次" in terminal.instruction
    assert "不得编造法律条文、案号" in terminal.instruction
    assert "正式签署前" in terminal.instruction


def test_clause_modification_seed_freezes_shared_tool_authorization_and_version() -> None:
    """验证发布版本、共享工具白名单和法务数字员工 execute 职责一起落库。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()

        skill = db.exec(
            select(Skill).where(Skill.skill_id == CLAUSE_MODIFICATION_SKILL_ID)
        ).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == CLAUSE_MODIFICATION_SKILL_ID,
                SkillVersion.version == CLAUSE_MODIFICATION_DETERMINISTIC_VERSION,
            )
        ).one()
        tool = db.exec(
            select(Tool).where(Tool.name == "contract.archive_query")
        ).one()
        role = db.exec(
            select(BusinessRole).where(
                BusinessRole.role_code == LEGAL_CONTRACT_RESEARCHER_ROLE
            )
        ).one()
        legal_agent = db.exec(
            select(AgentProfile).where(AgentProfile.name == "法务")
        ).one()
        binding = db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.agent_id == legal_agent.id,
                AgentRoleBinding.business_role_id == role.id,
            )
        ).one()

        assert skill.version == CLAUSE_MODIFICATION_DETERMINISTIC_VERSION
        assert version.meta_model_version == 3
        assert compile_legacy_skill_card(version.content_json).diagnostics == ()
        assert tool.allowed_skills_json == list(LEGAL_CONTRACT_REFERENCE_SKILL_IDS)
        assert tool.required_permission_code == "legal.contract_reference.query"
        assert tool.permission_authorization_mode == "workflow_delegated"
        assert role.permissions_json == ["legal.contract_reference.query"]
        assert binding.assignment_mode == "execute"
        assert binding.supervisor_employee_profile_id is not None
