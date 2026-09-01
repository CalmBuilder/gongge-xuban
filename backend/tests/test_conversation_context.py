"""
@Time       : 2026/09/01 10:00
@Author     : zhanglp8181
@File       : test_conversation_context.py
@CallChain  : pytest → build_conversation_context → outbound 会话上下文投影
@Description: 验证上下文压缩的正向结果、失败降级、确定性和 canonical 历史保护契约。
"""

from copy import deepcopy

from app.core.conversation_context import build_conversation_context


def test_conversation_context_keeps_full_history_under_budget() -> None:
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "我是 hx，我要买 A2"},
        {"role": "assistant", "content": "请问买几个？"},
        {"role": "user", "content": "买两个"},
    ]

    context = build_conversation_context(messages, token_budget=1_000)

    assert context["messages"] == messages
    assert context["metadata"]["compacted"] is False
    assert context["metadata"]["total_messages"] == 5
    assert context["metadata"]["omitted_messages"] == 0


def test_conversation_context_compacts_only_after_budget_is_exceeded() -> None:
    messages = [
        {"role": "user", "content": f"old user message {index} " + "x" * 80}
        if index % 2 == 0
        else {"role": "assistant", "content": f"old assistant message {index} " + "y" * 80}
        for index in range(20)
    ]

    context = build_conversation_context(messages, token_budget=500)
    projected = context["messages"]

    assert context["metadata"]["compacted"] is True
    assert context["metadata"]["omitted_messages"] > 0
    assert projected[0]["role"] == "user"
    assert "历史的信息可以被总结为" in projected[0]["content"]
    assert "近期的历史信息总结为" in projected[1]["content"]
    assert projected[-1]["content"] == messages[-1]["content"]
    assert context["metadata"]["estimated_tokens"] <= 500


def test_context_rotates_medium_history_into_long_history_on_next_threshold() -> None:
    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"round {index} " + ("中" * 120),
            "created_at": f"2026-07-13T12:{index:02d}:00",
        }
        for index in range(20)
    ]
    summaries: list[tuple[str, str]] = []

    def summarize(label: str, source: str, _budget: int) -> str:
        summaries.append((label, source))
        return f"{label}摘要：{source[:120]}"

    first = build_conversation_context(
        messages, token_budget=700, summary_builder=summarize
    )
    first_state = first["context_state"]

    assert first_state["compaction_count"] == 1
    assert first_state["long_term_summary"] == ""
    assert first_state["medium_term_summary"].startswith("近期历史信息摘要")
    assert first["messages"][0]["content"].startswith("历史的信息可以被总结为：")
    assert first["messages"][1]["content"].startswith("近期的历史信息总结为：")

    more_messages = [
        *messages,
        *[
            {
                "id": f"message_{index}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"new round {index} " + ("新" * 120),
                "created_at": f"2026-07-13T13:{index - 20:02d}:00",
            }
            for index in range(20, 36)
        ],
    ]
    second = build_conversation_context(
        more_messages,
        token_budget=700,
        context_state=first_state,
        summary_builder=summarize,
    )
    second_state = second["context_state"]

    assert second_state["compaction_count"] == 2
    assert second_state["long_term_summary"].startswith("长期历史信息摘要")
    assert first_state["medium_term_summary"] in summaries[-2][1]
    assert second_state["medium_term_summary"].startswith("近期历史信息摘要")
    assert second["metadata"]["current_turn_time"] == "2026-07-13T13:15:00"
    assert second["metadata"]["estimated_tokens"] <= 700


def test_context_summary_failure_falls_back_without_losing_the_latest_turn() -> None:
    """摘要器失败时使用确定性降级，并保留当前用户轮次和有界投影。"""

    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"历史轮次 {index} " + ("x" * 100),
        }
        for index in range(14)
    ]

    def failing_summary(_label: str, _source: str, _budget: int) -> str:
        """模拟摘要服务不可用，验证本地 transcript fallback 不依赖外部模型。"""

        raise RuntimeError("summary provider unavailable")

    context = build_conversation_context(
        messages,
        token_budget=500,
        summary_builder=failing_summary,
    )

    assert context["metadata"]["compacted"] is True
    assert context["metadata"]["estimated_tokens"] <= 500
    assert context["messages"][-1]["content"] == messages[-1]["content"]
    assert context["context_state"]["medium_term_summary"]


def test_context_projection_is_deterministic_and_does_not_mutate_canonical_messages() -> None:
    """同一输入重复投影结果一致，且压缩只生成 outbound view 不改写原始消息。"""

    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第 {index} 轮内容 " + ("中" * 90),
            "created_at": f"2026-09-01T00:00:{index:02d}Z",
            "metadata": {"private": "must stay canonical"},
            "images": [{"id": "image-1"}] if index == 12 else [],
        }
        for index in range(14)
    ]
    canonical_before = deepcopy(messages)

    first = build_conversation_context(messages, token_budget=500)
    second = build_conversation_context(messages, token_budget=500)

    assert messages == canonical_before
    assert first["messages"] == second["messages"]
    assert first["context_state"] == second["context_state"]
    assert all(set(message) <= {"role", "content", "images"} for message in first["messages"])
    assert first["messages"][-1]["content"] == messages[-1]["content"]


def test_assistant_only_history_is_bounded_without_inventing_a_user_round() -> None:
    """只有 assistant 历史时仍能稳定收敛到预算内，不凭空生成 user 轮次。"""

    messages = [
        {"id": f"assistant_{index}", "role": "assistant", "content": "历史回复 " + "y" * 100}
        for index in range(10)
    ]

    context = build_conversation_context(messages, token_budget=120)

    assert context["metadata"]["estimated_tokens"] <= 120
    assert all(message["role"] == "assistant" for message in context["messages"])
    assert context["messages"][-1]["content"] == messages[-1]["content"]


def test_missing_compaction_cursor_rebuilds_state_instead_of_reusing_stale_summary() -> None:
    """后端历史被替换后，失效游标会清空旧摘要并从当前 canonical 历史重建。"""

    context = build_conversation_context(
        [
            {"id": "new-1", "role": "user", "content": "新会话问题"},
            {"id": "new-2", "role": "assistant", "content": "新会话回答"},
        ],
        context_state={
            "long_term_summary": "旧租户摘要",
            "medium_term_summary": "旧近期摘要",
            "summarized_through_message_id": "deleted-message",
            "compaction_count": 9,
        },
        token_budget=1_000,
    )

    assert context["context_state"] == {
        "long_term_summary": "",
        "medium_term_summary": "",
        "summarized_through_message_id": "",
        "compaction_count": 0,
    }
    assert context["messages"] == [
        {"role": "user", "content": "新会话问题"},
        {"role": "assistant", "content": "新会话回答"},
    ]
