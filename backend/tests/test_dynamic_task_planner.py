"""
@Time       : 2026/08/04 02:25
@Author     : zhanglp8181
@File       : test_dynamic_task_planner.py
@CallChain  : pytest → DynamicTaskPlanner → JSON client/NormalizedPlan
@Description: 验证动态规划能力最小披露、任务契约冻结和预算收紧。
"""

from __future__ import annotations

import pytest

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


def _guidance_snapshot(*, invocation_policy: str = "model_allowed") -> CapabilitySnapshot:
    """构造模型目录含描述但内部快照含敏感正文的固定指导能力。"""

    payload = {
        "capability_type": "general_skill",
        "capability_id": "genskill_diagnose",
        "tenant_id": "tenant_demo",
        "name": "diagnosing-bugs",
        "contract": {
            "usage_mode": "planning_guidance",
            "revision_id": "gsrev_diagnose_1",
            "invocation_policy": invocation_policy,
        },
        "model_view": {
            "id": "genskill_diagnose",
            "slug": "diagnosing-bugs",
            "name": "诊断缺陷",
            "description": "先复现，再形成可证伪假设。",
            "usage_mode": "planning_guidance",
            "revision_id": "gsrev_diagnose_1",
            "revision_number": 1,
            "skill_markdown": "# 不应在选择阶段披露的完整正文",
            "resources": [{"path": "references/debug.md"}],
        },
        "user_view": {"name": "诊断缺陷"},
        "audit_view": {"content_checksum": "secret-checksum"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


class _GuidanceSelectionClient:
    """记录无正文选择 payload，并返回固定 Skill 或越权 user-only 名称。"""

    def __init__(self, selected_name: str) -> None:
        """保存模型将提出的名称。"""

        self.selected_name = selected_name
        self.payload: dict | None = None

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回单个选择并保留供应商实际可见内容。"""

        self.payload = user_payload
        return {
            "selected_skill_names": [self.selected_name],
            "reason": "该任务需要严格诊断纪律",
        }


def test_guidance_selector_discloses_catalog_only_and_rejects_user_only() -> None:
    """动态选择阶段看不到正文/资源，且 user-only 即使模型点名也不进入自动选择。"""

    client = _GuidanceSelectionClient("diagnosing-bugs")
    criterion = SuccessCriterion(id="fixed", type="assertion", spec={"required": True})
    selection = DynamicTaskPlanner(client).select_guidance_skills(
        goal="诊断并修复问题",
        success_criteria=(criterion,),
        catalog=(_guidance_snapshot(),),
    )

    assert selection.selected_skill_names == ("diagnosing-bugs",)
    assert client.payload is not None
    assert "skill_markdown" not in str(client.payload)
    assert "resources" not in str(client.payload)
    assert "secret-checksum" not in str(client.payload)

    user_only_client = _GuidanceSelectionClient("diagnosing-bugs")
    user_only = DynamicTaskPlanner(user_only_client).select_guidance_skills(
        goal="诊断并修复问题",
        success_criteria=(criterion,),
        catalog=(_guidance_snapshot(invocation_policy="user_only"),),
    )
    assert user_only.selected_skill_names == ()
    assert user_only_client.payload is None


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
    assert set(client.payload["output_contract"]) == {
        "goal",
        "success_criteria",
        "constraints",
        "assumptions",
        "steps",
    }
    assert "draft_id" in client.payload["output_contract"]["steps"][0]
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


class _UnavailableCapabilityClient:
    """模拟模型在无知识目录时仍虚构 knowledge 步骤。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """确认动态 allowed kinds 已移除 knowledge，但仍返回越权草案供服务端拒绝。"""

        assert "knowledge" not in user_payload["limits"]["allowed_step_kinds"]
        assert "knowledge" not in user_payload["output_contract"]["steps"][0]["kind"]
        return {
            "goal": "生成简报",
            "success_criteria": [
                {"id": "brief_ready", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "search",
                    "title": "检索知识",
                    "kind": "knowledge",
                    "capability_refs": [],
                },
                {
                    "draft_id": "answer",
                    "title": "形成简报",
                    "kind": "answer",
                    "depends_on": ["search"],
                },
            ],
        }


def test_planner_rejects_knowledge_step_when_capability_is_not_frozen() -> None:
    """无 knowledge snapshot 时即使模型违约也不得持久化不可执行计划。"""

    criterion = SuccessCriterion(
        id="brief_ready",
        type="assertion",
        spec={"required": True},
    )
    with pytest.raises(ValueError, match="未冻结的知识能力"):
        DynamicTaskPlanner(_UnavailableCapabilityClient()).create_plan(
            goal="生成简报",
            success_criteria=(criterion,),
            capabilities=(_snapshot(),),
        )


class _NonConvergentClient:
    """返回缺少结果汇聚步骤的模型计划。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """构造只有能力调用、没有最终 answer 的违约草案。"""

        return {
            "goal": "查询合同",
            "success_criteria": [
                {"id": "query_done", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "contracts",
                    "title": "查询合同",
                    "kind": "tool.read",
                    "capability_refs": ["contract.query"],
                }
            ],
        }


def test_planner_rejects_plan_without_terminal_answer() -> None:
    """模型省略结果步骤时必须在持久化前拒绝，不能生成无唤醒 running 孤儿。"""

    criterion = SuccessCriterion(
        id="query_done",
        type="assertion",
        spec={"required": True},
    )
    with pytest.raises(ValueError, match="required answer"):
        DynamicTaskPlanner(_NonConvergentClient()).create_plan(
            goal="查询合同",
            success_criteria=(criterion,),
            capabilities=(_snapshot(),),
        )
