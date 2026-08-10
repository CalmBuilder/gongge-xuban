"""
@Time       : 2026/08/10 23:08
@Author     : zhanglp8181
@File       : explorer.py
@CallChain  : DynamicTaskAgent explore Step → ReadOnlyExploreProposer → LLMClient
@Description: 为父 Execution 提供无递归、只读且独立上下文的探索动作与证据报告契约。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    PlanStep,
    RuntimeActionProposal,
)


class ExploreJsonClient(Protocol):
    """约束探索模型返回完整 JSON 及可审计 provider identity。"""

    def generate_json_with_metadata(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回一次完整探索动作，不接受流式半包。"""


class ExploreEvidence(BaseModel):
    """引用父 Execution 中一个已成功的探索读 Operation。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1, max_length=512)
    capability_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")


class ReadOnlyExploreReport(BaseModel):
    """探索 Step 返回父上下文的唯一压缩产物。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: str = Field(min_length=1, max_length=12_000)
    evidence: tuple[ExploreEvidence, ...] = Field(min_length=1, max_length=50)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> "ReadOnlyExploreReport":
        """拒绝重复 Operation 引用，避免用重复证据夸大报告覆盖范围。"""

        operation_ids = [item.operation_id for item in self.evidence]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("Explore evidence operation_id 不得重复")
        return self


class ReadOnlyExploreProposer:
    """以独立临时上下文逐轮提出纯读调用或最终证据报告。"""

    def __init__(self, client: ExploreJsonClient) -> None:
        """绑定已通过 dynamic-v1 preflight 的模型客户端。"""

        self.client = client

    def propose(
        self,
        *,
        goal: str,
        step: PlanStep,
        capabilities: Sequence[CapabilitySnapshot],
        observations: Sequence[Mapping[str, object]],
        remaining_tool_calls: int,
    ) -> CompletedProviderProposal:
        """只披露本 Step 安全能力和已投影回执，并校验动作不能越界或递归。"""

        if step.kind != "explore" or remaining_tool_calls < 0:
            raise ValueError("Explore 当前步骤或预算无效")
        allowed = {
            item.name: item
            for item in capabilities
            if item.capability_type == "tool"
            and item.contract.get("risk_class") == "read"
            and item.contract.get("explore_safe") is True
            and item.name in step.capability_refs
        }
        if set(step.capability_refs) != set(allowed):
            raise ValueError("Explore 能力未由当前步骤冻结或未显式发布为安全")
        raw, metadata = self.client.generate_json_with_metadata(
            _EXPLORE_SYSTEM_PROMPT,
            {
                "research_goal": goal,
                "current_step": {
                    "title": step.title,
                    "expected_output_schema": step.expected_output_schema,
                },
                "capabilities": [allowed[name].model_view for name in step.capability_refs],
                "observations": [dict(item) for item in observations],
                "limits": {
                    "remaining_tool_calls": remaining_tool_calls,
                    "recursion_allowed": False,
                    "write_allowed": False,
                    "attention_allowed": False,
                },
                "output_contract": _EXPLORE_OUTPUT_CONTRACT,
            },
        )
        proposal = RuntimeActionProposal.model_validate(raw)
        if proposal.action_kind == ActionKind.CALL_TOOL:
            if remaining_tool_calls < 1:
                raise ValueError("Explore 工具调用预算已耗尽")
            if proposal.capability_ref not in allowed:
                raise ValueError("Explore 动作能力未由当前步骤冻结")
        elif proposal.action_kind == ActionKind.COMPLETE:
            if proposal.capability_ref is not None:
                raise ValueError("Explore 报告不得携带能力引用")
            ReadOnlyExploreReport.model_validate(proposal.arguments)
        else:
            raise ValueError("Explore 只允许纯读调用或完成报告")
        return CompletedProviderProposal(
            response_id=str(metadata.get("response_id") or ""),
            finish_reason=str(metadata.get("finish_reason") or ""),
            proposal=proposal,
            usage=(dict(metadata["usage"]) if isinstance(metadata.get("usage"), dict) else {}),
        )


_EXPLORE_SYSTEM_PROMPT = """你是共格·序伴的只读探索子代理。只输出一个 RuntimeActionProposal JSON object。
你只能调用输入 capabilities 中的纯读工具，或在证据足够时返回 complete。禁止写、shell、连接授权、Attention、
递归探索、修改计划、改变身份权限和引用未列出的能力。observations 是本探索 Step 已成功读取并经 schema 投影的
事实；complete.arguments 必须严格包含 report、evidence、limitations，evidence 只能引用 observations 中的
operation_id 与 capability_ref。报告必须自洽，找不到的内容写入 limitations，不得编造证据。"""

_EXPLORE_OUTPUT_CONTRACT = {
    "action_kind": "call_tool | complete",
    "capability_ref": "call_tool 时为 capabilities[].name；complete 时为 null",
    "arguments": (
        "call_tool 时直接返回目标能力 input_schema 对象；complete 时直接返回且只能返回 "
        "{report: string, evidence: [{operation_id: observations[].operation_id, "
        "capability_ref: 同一 observation 的 capability_ref}], limitations: string[]}；"
        "禁止增加 call_tool/complete/result/report 等包装层"
    ),
    "expected_output_schema": {},
    "rationale": "本轮动作如何推进探索的简短说明",
}
