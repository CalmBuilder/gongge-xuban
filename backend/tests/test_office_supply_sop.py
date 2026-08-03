"""
@Time       : 2026/07/22 20:30
@Author     : zhanglp8181
@File       : test_office_supply_sop.py
@CallChain  : pytest → 办公用品发布定义 → Scheduler 工具命令/业务回执分支
@Description: 验证用品对象数组参数、明确确认契约和四类业务状态的确定性闭环。
"""

from __future__ import annotations

import pytest

from app.db.demo_sop_versions import _office_supply_deterministic_content
from app.sop_runtime.definition import CompiledSopDefinition
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import plan_next_action


def _definition() -> CompiledSopDefinition:
    """编译零告警的用品申领确定性发布定义。"""

    return compile_legacy_skill_card(_office_supply_deterministic_content({}))


def test_office_supply_definition_uses_shared_confirmation_and_object_array_mapping() -> None:
    """验证用品定义复用元模型第四版，并把对象清单原样映射到工具参数。"""

    definition = _definition()
    confirmation_node = next(
        node for node in definition.nodes if node.node_id == "node_confirm_supply_request"
    )
    items = [
        {"name": "A4纸", "quantity": 2, "unit": "包"},
        {"name": "签字笔", "quantity": 3, "unit": "支"},
    ]

    plan = plan_next_action(
        definition,
        current_node_id="node_call_supply_request",
        slots={"employee_id": "E002", "items": items},
    )

    assert definition.meta_model_version == 4
    assert definition.diagnostics == ()
    assert confirmation_node.config.confirmation_policy is not None
    assert plan.action == "call_tool"
    assert plan.operation_name == "admin.supply_request"
    assert plan.operation_arguments == {"employee_id": "E002", "items": items}


@pytest.mark.parametrize(
    ("business_status", "expected_terminal"),
    [
        ("approved", "node_supply_approved"),
        ("partial", "node_supply_partial"),
        ("pending", "node_supply_pending"),
        ("rejected", "node_supply_rejected"),
    ],
)
def test_office_supply_routes_each_business_status(
    business_status: str,
    expected_terminal: str,
) -> None:
    """验证传输成功不等于业务批准，四种业务状态进入各自终态。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="node_call_supply_request",
        slots={},
        tool_results={
            "supply_request": {
                "status": "succeeded",
                "data": {"status": business_status},
            }
        },
    )

    assert plan.action == "advance"
    assert plan.next_node_id == expected_terminal


def test_office_supply_routes_transport_failure_to_safe_default() -> None:
    """验证工具调用失败进入固定失败终态，不复用历史 SUP 单号伪装成功。"""

    plan = plan_next_action(
        _definition(),
        current_node_id="node_call_supply_request",
        slots={},
        tool_results={
            "supply_request": {
                "status": "failed",
                "data": {},
                "error": {"code": "UPSTREAM_TIMEOUT"},
            }
        },
    )

    assert plan.action == "advance"
    assert plan.next_node_id == "node_supply_failure"
