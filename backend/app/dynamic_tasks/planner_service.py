"""
@Time       : 2026/08/04 02:24
@Author     : zhanglp8181
@File       : planner_service.py
@CallChain  : DynamicTaskAgent → DynamicTaskPlanner → LLMClient → NormalizedPlan
@Description: 仅向模型投影只读能力视图，并把完整计划草案收紧为服务端有界计划。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot
from app.dynamic_tasks.planning import (
    DynamicPlanDraft,
    NormalizedPlan,
    SuccessCriterion,
    normalize_plan_draft,
)


class JsonPlanningClient(Protocol):
    """约束动态规划只使用完整 JSON object 响应。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回完整且可解析的 JSON object，不暴露流式半包。"""


class DynamicTaskPlanner:
    """把受控目标、成功标准和能力模型视图转换为有界规范计划。"""

    def __init__(
        self,
        client: JsonPlanningClient,
        *,
        max_steps: int = 8,
        max_tool_calls: int = 6,
        max_model_calls: int = 12,
    ) -> None:
        """冻结服务端预算；任何 provider 输出都不能扩大这些上限。"""

        self.client = client
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_model_calls = max_model_calls

    def create_plan(
        self,
        *,
        goal: str,
        success_criteria: Sequence[SuccessCriterion],
        capabilities: Sequence[CapabilitySnapshot],
        input_resources: Sequence[dict[str, object]] = (),
    ) -> NormalizedPlan:
        """生成完整草案并覆盖目标/成功标准，防止模型改写用户任务契约。"""

        read_capabilities = [
            snapshot
            for snapshot in capabilities
            if snapshot.contract.get("risk_class") == "read"
        ]
        payload = {
            "goal": goal,
            "success_criteria": [item.model_dump(mode="json") for item in success_criteria],
            "capabilities": [snapshot.model_view for snapshot in read_capabilities],
            "input_resources": [dict(item) for item in input_resources],
            "limits": {
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "max_model_calls": self.max_model_calls,
                "allowed_step_kinds": ["tool.read", "knowledge", "answer", "clarification"],
            },
        }
        raw = self.client.generate_json(_PLANNER_SYSTEM_PROMPT, payload)
        draft = DynamicPlanDraft.model_validate(raw).model_copy(
            update={
                "goal": goal,
                "success_criteria": tuple(success_criteria),
            }
        )
        return normalize_plan_draft(
            draft,
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_model_calls=self.max_model_calls,
        )


_PLANNER_SYSTEM_PROMPT = """你是共格·序伴的受控动态任务规划器。只输出一个完整 JSON object。
你只能使用输入中列出的 read 能力；不得提出写入、执行、删除、发信或权限变更。
步骤只可使用 tool.read、knowledge、answer、clarification。draft_id 只用于本次草案依赖，持久 step key 由服务端生成。
不得输出 tenant、agent、授权结论、凭据、URL、header、预算覆盖或未提供的能力。计划必须有界、无环并覆盖成功标准。"""
