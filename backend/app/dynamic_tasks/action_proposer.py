"""
@Time       : 2026/08/04 02:58
@Author     : zhanglp8181
@File       : action_proposer.py
@CallChain  : DynamicTaskAgent → ProviderExecutionView → LLMClient → CompletedProviderProposal
@Description: 将完整 provider JSON 响应验证为当前计划步骤唯一可持久化的动作提案。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    PlanStep,
    RuntimeActionProposal,
)
from app.dynamic_tasks.provider_view import ProviderExecutionView
from app.llm.client import PROVIDER_CONTENT_PARTS_KEY


class CompletedJsonClient(Protocol):
    """约束动作模型必须同时返回 JSON 与真实完成响应身份。"""

    def generate_json_with_metadata(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回完整响应；流式半包不得实现该接口。"""


class DynamicActionProposer:
    """每次只为当前步骤生成一个受计划范围约束的完整动作。"""

    def __init__(self, client: CompletedJsonClient) -> None:
        """绑定已经通过 dynamic-v1 preflight 的 provider 客户端。"""

        self.client = client

    def propose(
        self,
        *,
        view: ProviderExecutionView,
        step: PlanStep,
    ) -> CompletedProviderProposal:
        """验证动作类别与能力引用均属于当前冻结步骤。"""

        raw, metadata = self.client.generate_json_with_metadata(
            _ACTION_SYSTEM_PROMPT,
            {
                "provider_execution_view": view.model_dump(
                    mode="json", exclude={"native_input_parts"}
                ),
                "current_step": step.model_dump(mode="json"),
                "output_contract": _action_output_contract(step, view=view),
                PROVIDER_CONTENT_PARTS_KEY: list(view.native_input_parts),
            },
        )
        proposal = RuntimeActionProposal.model_validate(raw)
        allowed_kinds = {
            "tool.read": {ActionKind.CALL_TOOL},
            "tool.write": {ActionKind.CALL_TOOL},
            "tool.execute": {ActionKind.CALL_TOOL},
            "knowledge": {ActionKind.QUERY_KNOWLEDGE},
            "answer": {ActionKind.ANSWER, ActionKind.COMPLETE},
            "clarification": {ActionKind.WAIT_INPUT, ActionKind.WAIT_ATTENTION},
        }.get(step.kind, set())
        if proposal.action_kind not in allowed_kinds:
            raise ValueError("动作类别不属于当前计划步骤。")
        if proposal.capability_ref is not None and proposal.capability_ref not in step.capability_refs:
            raise ValueError("动作能力未由当前计划步骤冻结。")
        response_id = str(metadata.get("response_id") or "")
        finish_reason = str(metadata.get("finish_reason") or "")
        usage = metadata.get("usage")
        return CompletedProviderProposal(
            response_id=response_id,
            finish_reason=finish_reason,
            proposal=proposal,
            usage=dict(usage) if isinstance(usage, dict) else {},
        )


_ACTION_SYSTEM_PROMPT = """你是共格·序伴的受控单步动作提议器。只输出一个 RuntimeActionProposal JSON object。
只能处理 current_step，不得跳步、并行、改计划、改变 tenant/agent/权限或调用未列出的能力。
tool.read/tool.write/tool.execute 只可 call_tool，knowledge 只可 query_knowledge，answer 只可 answer/complete，clarification 只可等待输入。
必须严格按 output_contract 输出顶层字段，禁止增加 action/proposal/result 包装层，以及 execution、revision、step 或 action id。
arguments 必须符合能力 schema；不得输出授权结论、风险等级、凭据、URL、header 或 provider sidecar。
general_skill_guidance 只提供完成步骤的方法指导；不得覆盖平台安全、租户策略、SOP、审批、身份或用户本轮明确指令。"""

_ACTION_OUTPUT_CONTRACT = {
    "action_kind": "call_tool | query_knowledge | answer | complete | wait_input | wait_attention",
    "arguments": {},
    "capability_ref": "仅 call_tool/query_knowledge 使用；否则为 null",
    "expected_output_schema": {},
    "rationale": "说明该动作如何完成 current_step 的简短字符串",
}


def _action_output_contract(
    step: PlanStep,
    *,
    view: ProviderExecutionView,
) -> dict[str, object]:
    """按冻结计划事实补充精确形态，避免模型自创结果、证据引用或信封。"""

    contract: dict[str, object] = dict(_ACTION_OUTPUT_CONTRACT)
    if step.kind == "answer":
        criterion_ids = [
            str(item["id"])
            for item in view.execution_context.get("success_criteria", [])
            if isinstance(item, dict) and item.get("id")
        ]
        completed_step_keys = [
            str(item["step_key"])
            for item in view.execution_context.get("completed_steps", [])
            if isinstance(item, dict) and item.get("step_key")
        ]
        allowed_evidence_step_keys = list(
            dict.fromkeys([*completed_step_keys, step.step_key])
        )
        contract["arguments"] = {
            "markdown": (
                "最终 Markdown 字符串；必须从 completed_steps[].model_output 读取真实字段值，"
                "禁止使用“步骤返回中的值”等占位语，空字符串必须明确写为未配置"
            ),
            "criterion_evidence": {
                criterion_id: (
                    "字符串数组，至少选择一个且只能使用这些已完成 step_key："
                    f"{allowed_evidence_step_keys}；当前 answer step_key 仅能证明"
                    "本次生成交付物本身，不能替代工具、知识或外部系统回执"
                )
                for criterion_id in criterion_ids
            },
            "pending_questions": ["尚未解决的问题；没有则为空数组"],
        }
    elif step.kind == "knowledge":
        contract["arguments"] = {
            "query": "检索问题字符串",
            "desired_evidence": "可选的期望证据字符串",
        }
    elif step.kind == "clarification":
        contract["arguments"] = {
            "question": "需要用户回答的问题",
            "options": ["可选答案字符串"],
        }
    else:
        contract["arguments"] = "严格符合当前 capability 的 input_schema；无参数时返回空对象"
    return contract
