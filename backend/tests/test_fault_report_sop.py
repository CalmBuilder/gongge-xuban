"""
@Time       : 2026/07/22 21:40
@Author     : zhanglp8181
@File       : test_fault_report_sop.py
@CallChain  : pytest → 故障报修发布定义 → Scheduler 工具命令/工单状态分支
@Description: 验证报修字段映射、类别归一、明确确认和工单业务状态的确定性闭环。
"""

from __future__ import annotations

import pytest

from app.db.demo_sop_versions import _fault_report_lifecycle_content
from app.sop_runtime.definition import CompiledSopDefinition
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action
from app.sop_runtime.slot_values import normalize_slot_values


def _definition() -> CompiledSopDefinition:
    """编译零告警的故障报修确定性发布定义。"""

    return compile_legacy_skill_card(_fault_report_lifecycle_content({}))


def test_fault_report_normalizes_category_and_maps_only_explicit_ticket_fields() -> None:
    """验证 VPN 类别归一并显式绑定工单字段，不让模型生成优先级和 SLA。"""

    definition = _definition()
    slots = normalize_slot_values(
        definition,
        {
            "employee_id": "E002",
            "category": "VPN",
            "title": "VPN 无法连接",
            "description": "VPN 无法连接，影响远程办公。",
        },
    )

    plan = plan_next_action(
        definition,
        current_node_id="node_call_ticket_create",
        slots=slots,
    )

    assert definition.meta_model_version == 4
    assert definition.diagnostics == ()
    assert slots["category"] == "network"
    assert plan.action == "call_tool"
    assert plan.operation_name == "it.ticket_create"
    assert plan.operation_arguments == {
        "employee_id": "E002",
        "category": "network",
        "title": "VPN 无法连接",
        "description": "VPN 无法连接，影响远程办公。",
    }
    assert "priority" not in plan.operation_arguments


@pytest.mark.parametrize(
    ("ticket_status", "expected_terminal"),
    [
        ("created", "node_engineer_resolution"),
        ("assigned", "node_engineer_resolution"),
        ("pending", "node_engineer_resolution"),
    ],
)
def test_fault_report_routes_each_ticket_status(
    ticket_status: str,
    expected_terminal: str,
) -> None:
    """验证三种工单受理进度各自进入唯一终态，且不等同于故障解决。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="node_call_ticket_create",
        slots={},
        tool_results={
            "ticket_create": {
                "status": "succeeded",
                "data": {"status": ticket_status},
            }
        },
    )

    assert plan.action == "advance"
    assert plan.next_node_id == expected_terminal


def test_fault_report_routes_tool_failure_to_safe_default() -> None:
    """验证建单失败进入安全默认终态，不复用历史工单号伪装成功。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="node_call_ticket_create",
        slots={},
        tool_results={
            "ticket_create": {
                "status": "failed",
                "data": {},
                "error": {"code": "UPSTREAM_TIMEOUT"},
            }
        },
    )

    assert plan.action == "advance"
    assert plan.next_node_id == "node_ticket_failure"


def test_fault_report_uses_engineer_role_and_non_approval_outcomes() -> None:
    """验证工程处理使用真实业务角色和解决/升级结果，而不是批准/拒绝审批。"""

    definition = _definition()
    engineer_node = next(
        node for node in definition.nodes if node.node_id == "node_engineer_resolution"
    )

    assert engineer_node.config.candidate_role_codes == ("it_support_engineer",)
    assert engineer_node.config.completion_policy.claim_required is True
    assert engineer_node.config.allowed_outcomes == ("resolved", "escalated")
    assert [option.label for option in engineer_node.config.outcome_options] == [
        "标记已解决",
        "升级处理",
    ]
    assert all(option.comment_required for option in engineer_node.config.outcome_options)


def test_fault_report_waits_for_requester_then_maps_ticket_close() -> None:
    """验证工程师解决后等待报修人确认，并以创建回执中的工单号执行关闭。"""

    definition = _definition()
    resolved = plan_next_action(
        definition,
        current_node_id="node_engineer_resolution",
        slots={},
        work_items={"status": "completed", "outcome": "resolved"},
    )
    waiting = plan_next_action(
        definition,
        current_node_id="node_confirm_resolution",
        slots={},
    )
    closing = plan_next_action(
        definition,
        current_node_id="node_call_ticket_close",
        slots={"employee_id": "E002"},
        tool_results={
            "ticket_create": {
                "status": "succeeded",
                "data": {"ticket_id": "TICKET-001", "status": "created"},
            }
        },
    )

    assert resolved.next_node_id == "node_confirm_resolution"
    assert waiting.action == "wait_input"
    assert waiting.expected_inputs == ("resolution_confirmation",)
    assert closing.action == "call_tool"
    assert closing.operation_name == "it.ticket_close"
    assert closing.operation_arguments == {
        "ticket_id": "TICKET-001",
        "requester_employee_id": "E002",
    }


def test_fault_report_routes_close_and_reopen_receipts_without_conflation() -> None:
    """验证关闭与重开使用独立业务回执，未恢复路径不能伪装成闭环成功。"""

    definition = _definition()
    closed = plan_next_action(
        definition,
        current_node_id="node_call_ticket_close",
        slots={},
        tool_results={
            "ticket_close": {"status": "succeeded", "data": {"status": "closed"}}
        },
    )
    reopened = plan_next_action(
        definition,
        current_node_id="node_call_ticket_reopen",
        slots={},
        tool_results={
            "ticket_reopen": {"status": "succeeded", "data": {"status": "reopened"}}
        },
    )

    assert closed.next_node_id == "node_ticket_closed"
    assert reopened.next_node_id == "node_ticket_reopened"
