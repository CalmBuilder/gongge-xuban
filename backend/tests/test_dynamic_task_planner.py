"""
@Time       : 2026/08/04 02:25
@Author     : zhanglp8181
@File       : test_dynamic_task_planner.py
@CallChain  : pytest → DynamicTaskPlanner → JSON client/NormalizedPlan
@Description: 验证动态规划能力最小披露、任务契约冻结和预算收紧。
"""

from __future__ import annotations

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.planning import SuccessCriterion


class _Client:
    """记录模型可见 payload 并返回完整计划草案。"""

    def __init__(self) -> None:
        self.payload: dict | None = None

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """模拟 provider 完整 JSON 响应，并尝试改写目标供服务端纠正。"""

        self.payload = user_payload
        return {
            "goal": "模型擅自改写的目标",
            "success_criteria": [
                {"id": "model_changed", "type": "assertion", "spec": {"required": False}}
            ],
            "steps": [
                {
                    "draft_id": "contracts",
                    "title": "查询合同",
                    "kind": "tool.read",
                    "capability_refs": ["contract.query"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成简报",
                    "kind": "answer",
                    "depends_on": ["contracts"],
                },
            ],
        }


def _snapshot() -> CapabilitySnapshot:
    """返回包含模型视图与审计视图差异的只读能力。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "tool_contract",
        "tenant_id": "tenant_demo",
        "name": "contract.query",
        "contract": {"risk_class": "read"},
        "model_view": {"name": "contract.query", "input_schema": {"partner": "string"}},
        "user_view": {"name": "合同查询"},
        "audit_view": {"url": "https://internal.invalid", "authorization": "secret"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


def test_planner_only_discloses_model_view_and_freezes_server_contract() -> None:
    """验证审计/连接信息不进模型，目标、成功标准、预算和 step key 均由服务端裁决。"""

    client = _Client()
    criterion = SuccessCriterion(
        id="brief_ready",
        type="assertion",
        spec={"required": True},
    )
    plan = DynamicTaskPlanner(
        client,
        max_steps=4,
        max_tool_calls=2,
        max_model_calls=5,
    ).create_plan(
        goal="生成续约风险简报",
        success_criteria=(criterion,),
        capabilities=(_snapshot(),),
    )

    assert client.payload is not None
    assert client.payload["capabilities"] == [
        {"name": "contract.query", "input_schema": {"partner": "string"}}
    ]
    assert "internal.invalid" not in str(client.payload)
    assert "authorization" not in str(client.payload)
    assert plan.goal == "生成续约风险简报"
    assert plan.success_criteria == (criterion,)
    assert plan.budget == {
        "max_steps": 4,
        "max_tool_calls": 2,
        "max_model_calls": 5,
        "max_input_tokens": 120_000,
        "max_output_tokens": 24_000,
        "max_total_tokens": 144_000,
        "max_runtime_seconds": 900,
    }
    assert plan.steps[0].step_key.startswith("step_01_")
