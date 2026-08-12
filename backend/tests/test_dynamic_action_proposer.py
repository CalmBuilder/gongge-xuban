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
        self.payload = None

    def generate_json_with_metadata(self, system_prompt, user_payload):
        """模拟 provider 的完整 stop 响应。"""

        self.payload = user_payload
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


class _AnswerClient(_Client):
    """返回合法最终结果并保留模型可见的 answer arguments 契约。"""

    def generate_json_with_metadata(self, system_prompt, user_payload):
        """模拟 answer 步骤的完整 provider 响应。"""

        self.payload = user_payload
        return (
            {
                "action_kind": "answer",
                "arguments": {
                    "markdown": "# 验收结果",
                    "criterion_evidence": {"criterion_01": ["query_contract"]},
                    "pending_questions": [],
                },
                "rationale": "形成可验证结果",
            },
            {"response_id": "response_answer", "finish_reason": "stop"},
        )


def _view(*, include_result_facts: bool = False):
    """构造已验证协议下的最小 provider execution view。"""

    execution_context = {"execution_id": "exec_1", "plan_checksum": "a" * 64}
    if include_result_facts:
        execution_context.update(
            {
                "success_criteria": [
                    {"id": "criterion_01", "type": "assertion", "spec": {}}
                ],
                "completed_steps": [{"step_key": "query_contract"}],
            }
        )
    return build_provider_execution_view(
        execution_context=execution_context,
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

    client = _Client()
    completed = DynamicActionProposer(client).propose(
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
    assert set(client.payload["output_contract"]) == {
        "action_kind",
        "arguments",
        "capability_ref",
        "expected_output_schema",
        "rationale",
    }


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


def test_answer_step_receives_exact_dynamic_result_arguments_contract() -> None:
    """验证最终步骤明确要求 Markdown、逐标准证据和未决问题，而非自由 content。"""

    client = _AnswerClient()
    completed = DynamicActionProposer(client).propose(
        view=_view(include_result_facts=True),
        step=PlanStep(
            step_key="final_answer",
            title="形成验收结果",
            kind="answer",
            depends_on=("query_contract",),
        ),
    )

    assert completed.proposal.arguments["markdown"] == "# 验收结果"
    assert set(client.payload["output_contract"]["arguments"]) == {
        "markdown",
        "criterion_evidence",
        "pending_questions",
    }
    assert set(client.payload["output_contract"]) == {
        "action_kind",
        "arguments",
        "capability_ref",
        "expected_output_schema",
        "rationale",
    }
    assert "禁止使用" in client.payload["output_contract"]["arguments"]["markdown"]
    evidence_contract = client.payload["output_contract"]["arguments"][
        "criterion_evidence"
    ]
    assert set(evidence_contract) == {"criterion_01"}
    assert "query_contract" in evidence_contract["criterion_01"]
    assert "final_answer" in evidence_contract["criterion_01"]
    assert "required_criterion_ids" not in evidence_contract
    assert "value_contract" not in evidence_contract
