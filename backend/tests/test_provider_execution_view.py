"""
@Time       : 2026/08/04 01:20
@Author     : zhanglp8181
@File       : test_provider_execution_view.py
@CallChain  : pytest → ProviderExecutionViewBuilder → provider payload
@Description: 验证动态执行消息序列、sidecar 剥离、compaction 和模型切换边界。
"""

from __future__ import annotations

import pytest

from app.dynamic_tasks.provider_view import (
    ProviderExecutionViewError,
    build_provider_execution_view,
)


def _context() -> dict[str, object]:
    """返回不得被 compaction 或 provider 转换覆盖的机械执行事实。"""

    return {
        "execution_id": "exec_1",
        "execution_revision": 3,
        "plan_revision_id": "plan_2",
        "plan_checksum": "a" * 64,
        "completed_steps": [{"step_key": "query_contract"}],
        "pending_action": None,
        "input_resources": [],
    }


def _capabilities(**updates: object) -> dict[str, object]:
    """返回已通过 B0.3 preflight 的最小 provider 能力快照。"""

    values: dict[str, object] = {
        "protocol_version": "dynamic-v1",
        "sdk_available": True,
        "credentials_verified": True,
        "structured_output": True,
        "tool_calling": True,
    }
    values.update(updates)
    return values


def test_view_strips_sidecars_and_keeps_execution_context_outside_compaction() -> None:
    """验证摘要只能替换对话视图，不能删除机械计划、步骤或输入身份。"""

    canonical = [
        {"role": "user", "content": "分析合同", "source": {"secret": "x"}, "usage": {}},
        {
            "role": "assistant",
            "content": "查询中",
            "action_calls": [
                {"id": "call_1", "name": "contract.query", "arguments": {"id": "C1"}}
            ],
            "reasoning": "private",
            "_openai": {"items": ["provider-private"]},
        },
        {
            "role": "tool",
            "action_call_id": "call_1",
            "content": {"status": "succeeded", "count": 1},
            "audit": {"credential": "hidden"},
        },
    ]
    compacted = [{"role": "user", "content": "历史摘要：已选择合同 C1"}]

    view = build_provider_execution_view(
        execution_context=_context(),
        canonical_messages=canonical,
        compacted_messages=compacted,
        model_capabilities=_capabilities(),
    )

    assert view.execution_context["plan_checksum"] == "a" * 64
    assert [message.role for message in view.messages] == ["system", "user"]
    serialized = view.model_dump_json()
    assert "provider-private" not in serialized
    assert "credential" not in serialized
    assert "reasoning" not in serialized
    assert "query_contract" in serialized


def test_view_rejects_orphan_result_and_user_before_required_results() -> None:
    """验证非法 provider history 在外呼前失败，不交给模型猜测修复。"""

    with pytest.raises(ProviderExecutionViewError, match="孤立"):
        build_provider_execution_view(
            execution_context=_context(),
            canonical_messages=[
                {"role": "tool", "action_call_id": "unknown", "content": {"ok": True}}
            ],
            model_capabilities=_capabilities(),
        )

    with pytest.raises(ProviderExecutionViewError, match="结果尚未补齐"):
        build_provider_execution_view(
            execution_context=_context(),
            canonical_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "action_calls": [
                        {"id": "call_1", "name": "contract.query", "arguments": {}}
                    ],
                },
                {"role": "user", "content": "追加条件"},
            ],
            model_capabilities=_capabilities(),
        )


def test_view_fills_declared_interrupted_actions_before_steering() -> None:
    """验证中断动作先形成结构化结果，steering 才能进入下一安全消息边界。"""

    view = build_provider_execution_view(
        execution_context=_context(),
        canonical_messages=[
            {
                "role": "assistant",
                "content": "",
                "action_calls": [
                    {"id": "call_1", "name": "contract.query", "arguments": {}}
                ],
            },
            {"role": "user", "content": "排除已过期合同", "message_kind": "steering"},
        ],
        interrupted_action_ids={"call_1"},
        model_capabilities=_capabilities(),
    )

    assert [message.role for message in view.messages] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    assert view.messages[2].action_call_id == "call_1"
    assert view.messages[2].content == {"error": "action_interrupted"}


def test_view_requires_verified_protocol_and_does_not_infer_pdf_support() -> None:
    """验证模型切换只能依据冻结 preflight，不能从模型名称猜测协议或 PDF 能力。"""

    with pytest.raises(ProviderExecutionViewError, match="preflight"):
        build_provider_execution_view(
            execution_context=_context(),
            canonical_messages=[{"role": "user", "content": "继续"}],
            model_capabilities=_capabilities(tool_calling=False, model="looks-capable"),
        )


def test_view_rejects_duplicate_action_identity_and_unfinished_tail() -> None:
    """验证 action identity 不可重复，且 provider 请求前不能遗留无结果调用。"""

    duplicated = [
        {
            "role": "assistant",
            "content": "",
            "action_calls": [
                {"id": "call_1", "name": "contract.query", "arguments": {}},
                {"id": "call_1", "name": "partner.query", "arguments": {}},
            ],
        }
    ]
    with pytest.raises(ProviderExecutionViewError, match="重复"):
        build_provider_execution_view(
            execution_context=_context(),
            canonical_messages=duplicated,
            model_capabilities=_capabilities(),
        )
    with pytest.raises(ProviderExecutionViewError, match="未闭合"):
        build_provider_execution_view(
            execution_context=_context(),
            canonical_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "action_calls": [
                        {"id": "call_2", "name": "contract.query", "arguments": {}}
                    ],
                }
            ],
            model_capabilities=_capabilities(),
        )
