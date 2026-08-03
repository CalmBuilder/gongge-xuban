"""
@Time       : 2026/08/04 02:59
@Author     : zhanglp8181
@File       : test_dynamic_action_proposer.py
@CallChain  : pytest → DynamicActionProposer → completed JSON client
@Description: 验证单步动作的 provider 身份、步骤类别和能力范围契约。
"""

from __future__ import annotations

import pytest

from app.dynamic_tasks.action_proposer import DynamicActionProposer
from app.dynamic_tasks.planning import PlanStep
from app.dynamic_tasks.provider_view import build_provider_execution_view


class _Client:
    """返回带真实 response metadata 的完整动作 JSON。"""

    def __init__(self, capability_ref: str = "contract.query") -> None:
        self.capability_ref = capability_ref

    def generate_json_with_metadata(self, system_prompt, user_payload):
        """模拟 provider 的完整 stop 响应。"""

        return (
            {
                "action_kind": "call_tool",
                "capability_ref": self.capability_ref,
                "arguments": {"partner": "星海科技"},
                "rationale": "读取合同证据",
            },
            {
                "response_id": "response_1",
                "finish_reason": "stop",
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        )


def _view():
    """构造已验证协议下的最小 provider execution view。"""

    return build_provider_execution_view(
        execution_context={"execution_id": "exec_1", "plan_checksum": "a" * 64},
        canonical_messages=[{"role": "user", "content": "继续当前步骤"}],
        model_capabilities={
            "protocol_version": "dynamic-v1",
            "sdk_available": True,
            "credentials_verified": True,
            "structured_output": True,
            "tool_calling": True,
        },
    )


def test_proposer_preserves_provider_identity_and_current_step_scope() -> None:
    """验证完整响应身份与 token 用量进入提案，能力只能来自当前步骤。"""

    completed = DynamicActionProposer(_Client()).propose(
        view=_view(),
        step=PlanStep(
            step_key="query_contract",
            title="查询合同",
            kind="tool.read",
            capability_refs=("contract.query",),
        ),
    )

    assert completed.response_id == "response_1"
    assert completed.proposal.capability_ref == "contract.query"
    assert completed.usage == {"input_tokens": 20, "output_tokens": 8}


def test_proposer_rejects_capability_not_declared_by_current_step() -> None:
    """验证模型不能借单步提案临时扩大冻结能力范围。"""

    with pytest.raises(ValueError, match="未由当前计划步骤冻结"):
        DynamicActionProposer(_Client("admin.delete")).propose(
            view=_view(),
            step=PlanStep(
                step_key="query_contract",
                title="查询合同",
                kind="tool.read",
                capability_refs=("contract.query",),
            ),
        )
