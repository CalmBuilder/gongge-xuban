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
                "provider_execution_view": view.model_dump(mode="json"),
                "current_step": step.model_dump(mode="json"),
            },
        )
        proposal = RuntimeActionProposal.model_validate(raw)
        allowed_kinds = {
            "tool.read": {ActionKind.CALL_TOOL},
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
tool.read 只可 call_tool，knowledge 只可 query_knowledge，answer 只可 answer/complete，clarification 只可等待输入。
arguments 必须符合能力 schema；不得输出授权结论、风险等级、凭据、URL、header 或 provider sidecar。"""
