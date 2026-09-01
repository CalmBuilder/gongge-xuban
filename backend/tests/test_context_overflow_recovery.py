"""
@Time       : 2026/09/01 11:00
@Author     : zhanglp8181
@File       : test_context_overflow_recovery.py
@CallChain  : pytest → AgentLoop 回复外呼 → provider 超限 → 同 Turn 压缩重试
@Description: 验证 OpenWorker 风格的 outbound 压缩、一次重试和非超限错误直达失败契约。
"""

from collections.abc import Iterator

import pytest

from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession
from app.llm import LLMError
from app.session.session_schema import RouterDecision, StepAgentResult


class _ScriptedResponseGenerator:
    """按脚本返回或抛出结果，并记录每次收到的 outbound 上下文。"""

    def __init__(self, outcomes: list[str | BaseException]) -> None:
        """保存同步/流式回复阶段的预设结果。"""

        self.outcomes = list(outcomes)
        self.contexts: list[dict[str, object]] = []

    @staticmethod
    def _context(args: tuple[object, ...]) -> dict[str, object]:
        """从现有 ResponseGenerator positional 契约中提取会话 outbound view。"""

        context = args[9] if len(args) > 9 else None
        if not isinstance(context, dict):
            raise AssertionError("回复生成器没有收到 conversation_context")
        return context

    def _next(self) -> str | BaseException:
        """取出下一次模型调用结果，防止测试意外发生无限重试。"""

        if not self.outcomes:
            raise AssertionError("测试触发了预期外的模型重试")
        return self.outcomes.pop(0)

    def generate(self, *args: object, **_kwargs: object) -> str:
        """模拟非流式回复生成并记录每次 provider 输入。"""

        self.contexts.append(self._context(args))
        outcome = self._next()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def generate_stream(self, *args: object, **_kwargs: object) -> Iterator[str]:
        """模拟流式回复生成，确保超限发生在消费 stream 时也能被捕获。"""

        self.contexts.append(self._context(args))
        outcome = self._next()
        if isinstance(outcome, BaseException):
            raise outcome
        yield outcome


def _loop_for_reply_recovery(generator: _ScriptedResponseGenerator) -> AgentLoop:
    """建立只保留回复外呼依赖的 AgentLoop，隔离数据库和真实 provider。"""

    loop = object.__new__(AgentLoop)
    loop.response_generator = generator
    loop._refresh_response_input_context = lambda _session, context: context
    return loop


def _reply_args(context: dict[str, object]) -> dict[str, object]:
    """返回同步和流式回复方法共用的最小合法输入。"""

    return {
        "message": "继续分析同一问题",
        "chat_session": ChatSession(id="session_overflow", tenant_id="tenant_demo"),
        "active_skill": None,
        "router_decision": RouterDecision(decision="answer_only"),
        "step_result": StepAgentResult(),
        "tool_result": None,
        "model_config": None,
        "persona_prompt": None,
        "memory_context": [],
        "conversation_context": context,
    }


def test_reply_overflow_compacts_once_and_retries_same_turn_without_mutating_original_view() -> None:
    """provider 明确返回上下文超限时压缩一次并复用同一轮输入，不能重复创建消息。"""

    original = {
        "messages": [{"role": "user", "content": "当前轮用户问题"}],
        "context_state": {"compaction_count": 0},
    }
    compacted = {
        "messages": [{"role": "user", "content": "历史摘要与当前轮用户问题"}],
        "context_state": {"compaction_count": 1},
        "metadata": {"compacted_now": True},
    }
    generator = _ScriptedResponseGenerator(
        [LLMError("context_length_exceeded"), "压缩后恢复的回答"]
    )
    loop = _loop_for_reply_recovery(generator)
    loop._conversation_context = lambda _session, model_config=None: compacted

    args = _reply_args(original)
    reply = loop._generate_reply_segment(**args)  # type: ignore[arg-type]

    assert reply == "压缩后恢复的回答"
    assert generator.contexts == [original, compacted]
    assert original == {
        "messages": [{"role": "user", "content": "当前轮用户问题"}],
        "context_state": {"compaction_count": 0},
    }


def test_stream_reply_overflow_compacts_once_and_does_not_duplicate_recovered_chunks() -> None:
    """流式 provider 超限时只允许一次同 Turn 重试，成功 chunk 只能交付一份。"""

    original = {
        "messages": [{"role": "user", "content": "附件续分析"}],
        "context_state": {"compaction_count": 0},
    }
    compacted = {
        "messages": [{"role": "user", "content": "附件摘要与续分析问题"}],
        "context_state": {"compaction_count": 1},
        "metadata": {"compacted_now": True},
    }
    generator = _ScriptedResponseGenerator(
        [LLMError("maximum context length is 128000 tokens"), "流式恢复回答"]
    )
    loop = _loop_for_reply_recovery(generator)
    loop._conversation_context = lambda _session, model_config=None: compacted

    chunks = list(loop._generate_reply_stream_segment(**_reply_args(original)))  # type: ignore[arg-type]

    assert chunks == ["流式恢复回答"]
    assert generator.contexts == [original, compacted]


def test_non_overflow_reply_error_is_not_compacted_or_retried() -> None:
    """限流或网络等非上下文错误不能误触发压缩，必须原样进入既有失败路径。"""

    original = {
        "messages": [{"role": "user", "content": "当前轮用户问题"}],
        "context_state": {"compaction_count": 0},
    }
    generator = _ScriptedResponseGenerator([LLMError("rate limit exceeded")])
    loop = _loop_for_reply_recovery(generator)
    loop._conversation_context = lambda _session, model_config=None: pytest.fail(
        "非上下文错误不得重建会话上下文"
    )

    with pytest.raises(LLMError, match="rate limit exceeded"):
        loop._generate_reply_segment(**_reply_args(original))  # type: ignore[arg-type]

    assert generator.contexts == [original]
