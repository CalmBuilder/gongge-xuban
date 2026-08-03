"""
@Time       : 2026/07/22 23:24
@Author     : zhanglp8181
@File       : test_permission_grant_sop.py
@CallChain  : pytest → 权限开通发布定义 → Scheduler/WorkItem/Tool 分支
@Description: 验证普通权限流程委托和高权限结构化审批使用同一确定性元模型。
"""

from __future__ import annotations

from app.db.demo_sop_versions import _permission_grant_deterministic_content
from app.sop_runtime.definition import CompiledSopDefinition
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action
from app.sop_runtime.slot_values import normalize_slot_values


def _definition() -> CompiledSopDefinition:
    """编译零告警的权限开通确定性发布定义。"""

    return compile_legacy_skill_card(_permission_grant_deterministic_content({}))


def test_permission_grant_normalizes_access_level_and_requires_confirmation() -> None:
    """验证只读别名归一为 read，且信息齐全后仍必须等待当前轮确认。"""

    definition = _definition()
    slots = normalize_slot_values(
        definition,
        {
            "employee_id": "E002",
            "system": "CRM",
            "permission": "只读",
            "access_level": "只读",
        },
    )
    plan = plan_next_action(
        definition,
        current_node_id="node_confirm_access_request",
        slots=slots,
    )

    assert definition.meta_model_version == 4
    assert definition.diagnostics == ()
    assert slots["access_level"] == "read"
    assert plan.action == "wait_input"
    assert plan.expected_inputs == ("confirmation",)


def test_permission_grant_routes_read_to_protected_tool_with_explicit_arguments() -> None:
    """验证普通 read 分支调用唯一权限工具，不携带模型自由生成字段。"""

    definition = _definition()
    routed = plan_next_action(
        definition,
        current_node_id="node_route_access_level",
        slots={"access_level": "read"},
    )
    tool_plan = plan_next_action(
        definition,
        current_node_id="node_call_permission_grant",
        slots={
            "employee_id": "E002",
            "system": "CRM",
            "permission": "只读",
            "access_level": "read",
        },
    )

    assert routed.next_node_id == "node_call_permission_grant"
    assert tool_plan.action == "call_tool"
    assert tool_plan.operation_name == "it.grant_permission"
    assert tool_plan.operation_arguments == {
        "employee_id": "E002",
        "system": "CRM",
        "permission": "只读",
        "access_level": "read",
    }


def test_permission_grant_routes_write_and_admin_to_business_role_work_item() -> None:
    """验证 write/admin 不调用工具，而是进入需要业务角色和动作权限的人工任务。"""

    definition = _definition()
    review_node = next(
        node for node in definition.nodes if node.node_id == "node_high_access_review"
    )

    for access_level in ("write", "admin"):
        plan = plan_next_action(
            definition,
            current_node_id="node_route_access_level",
            slots={"access_level": access_level},
        )
        assert plan.next_node_id == "node_high_access_review"

    assert review_node.config.candidate_role_codes == ("it_access_approver",)
    assert review_node.config.completion_policy.claim_required is True
    assert review_node.config.exclude_initiator is True
    assert review_node.config.action_permissions == {
        "outcome:approved": "it.access_request.approve",
        "outcome:rejected": "it.access_request.reject",
    }


def test_permission_grant_high_access_decision_resumes_protected_grant() -> None:
    """验证高权限批准恢复到统一开通工具，拒绝则直接进入拒绝终态。"""

    approved = plan_next_action(
        _definition(),
        current_node_id="node_high_access_review",
        slots={},
        work_items={"status": "completed", "outcome": "approved"},
    )
    rejected = plan_next_action(
        _definition(),
        current_node_id="node_high_access_review",
        slots={},
        work_items={"status": "completed", "outcome": "rejected"},
    )

    assert approved.next_node_id == "node_call_permission_grant"
    assert rejected.next_node_id == "node_access_rejected"


def test_permission_grant_routes_business_receipt_and_failure_separately() -> None:
    """验证 granted/pending/rejected 与传输失败分别进入唯一终态。"""

    expected = {
        "granted": "node_access_granted",
        "pending": "node_access_pending",
        "rejected": "node_access_rejected",
    }
    for status, terminal in expected.items():
        plan = plan_next_action(
            _definition(),
            current_node_id="node_call_permission_grant",
            slots={},
            tool_results={
                "permission_grant": {
                    "status": "succeeded",
                    "data": {"status": status},
                }
            },
        )
        assert plan.next_node_id == terminal

    failure = plan_next_action(
        _definition(),
        current_node_id="node_call_permission_grant",
        slots={},
        tool_results={"permission_grant": {"status": "failed", "data": {}}},
    )
    assert failure.next_node_id == "node_tool_failure"
