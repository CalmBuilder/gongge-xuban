"""
@Time       : 2026/07/27 13:45
@Author     : zhanglp8181
@File       : test_llm_client.py
@CallChain  : pytest → LLMClient → OpenAI 兼容客户端替身
@Description: 验证模型请求协议、输出配额、重试诊断、上下文投影和结构化响应。
"""

import copy
from threading import Event, Thread
from time import monotonic

import pytest

from app.llm.client import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    OPTIONAL_INPUT_PROBE_MAX_TOKENS,
    PROVIDER_CONTENT_PARTS_KEY,
    LLMClient,
    LLMError,
    LLMStreamCancelled,
    _json_repair_output_token_budget,
    _prepare_user_input,
    _request_input_token_budget,
    _request_tokens,
    _thinking_mode_for_model,
)
from app.llm.output_policy import operation_output_tokens, operation_thinking_mode
from app.llm.stage_protocol import TURN_STAGE_MESSAGES_KEY, stage_payload
from app.llm.schemas import ModelConfigCreateRequest
from app.observability.spans import bind_span_sink, llm_operation


class _ForbiddenResponses:
    def create(self, **_kwargs):  # noqa: ANN003
        raise AssertionError("responses.create must not be called for OpenAI-compatible models")


class _FakeChatCompletions:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        message = type("Message", (), {"content": "ok"})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice]})()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeChatCompletions()


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = _ForbiddenResponses()
        self.chat = _FakeChat()


def test_provider_native_parts_are_separated_from_json_and_reject_remote_urls() -> None:
    """验证动态原生附件成为 user content parts，且远程 URL 不会绕过受管资源边界。"""

    context, content = _prepare_user_input(
        {
            "task": {"step": "inspect"},
            PROVIDER_CONTENT_PARTS_KEY: [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                }
            ],
        }
    )
    assert context == []
    assert content[0]["type"] == "text"
    assert PROVIDER_CONTENT_PARTS_KEY not in content[0]["text"]
    assert content[1]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="

    with pytest.raises(LLMError, match="native input"):
        _prepare_user_input(
            {
                "task": {},
                PROVIDER_CONTENT_PARTS_KEY: [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/private.png"},
                    }
                ],
            }
        )


def test_llm_client_uses_600_second_timeout(monkeypatch):
    captured = {}

    def fake_decrypt_secret(_value):  # noqa: ANN001
        return "api-key"

    def fake_openai(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return _FakeOpenAIClient()

    settings = type("Settings", (), {"model_api_timeout_seconds": 600.0})()
    model_config = type(
        "ModelConfig",
        (),
        {
            "api_key_encrypted": "encrypted",
            "base_url": "https://example.test/v1",
            "model": "demo-model",
            "temperature": 0.2,
            "max_output_tokens": 256,
            "extra_body_json": {
                "thinking": {"type": "disabled"},
                "do_sample": False,
            },
        },
    )()
    monkeypatch.setattr("app.llm.client.decrypt_secret", fake_decrypt_secret)
    monkeypatch.setattr("app.llm.client.OpenAI", fake_openai)
    monkeypatch.setattr("app.llm.client.get_settings", lambda: settings)

    client = LLMClient(model_config)

    assert client.timeout_seconds == 600.0
    assert captured["timeout"] == 600.0
    assert captured["max_retries"] == 0
    assert client.extra_body == {
        "thinking": {"type": "disabled"},
        "do_sample": False,
    }
    assert client.thinking_mode == "disabled"


def test_llm_client_allows_scoped_timeout_override(monkeypatch):
    captured = {}
    model_config = type(
        "ModelConfig",
        (),
        {
            "api_key_encrypted": "encrypted",
            "base_url": "https://example.test/v1",
            "model": "demo-model",
            "temperature": 0.2,
            "max_output_tokens": 256,
            "extra_body_json": {},
        },
    )()
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda value: "api-key")
    monkeypatch.setattr(
        "app.llm.client.OpenAI",
        lambda **kwargs: captured.update(kwargs) or _FakeOpenAIClient(),
    )

    client = LLMClient(model_config, timeout_seconds=120)

    assert client.timeout_seconds == 120
    assert captured["timeout"] == 120
    assert captured["max_retries"] == 0


def test_llm_client_clamps_ark_output_budget_to_provider_limit(monkeypatch):
    """验证 Ark 管理端高容量配置不会把超出供应商上限的 max_tokens 发给 provider。"""

    model_config = type(
        "ModelConfig",
        (),
        {
            "api_key_encrypted": "encrypted",
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
            "model": "kimi-k3",
            "temperature": 0.2,
            "max_output_tokens": 819_200,
            "extra_body_json": {},
        },
    )()
    monkeypatch.setattr("app.llm.client.decrypt_secret", lambda value: "api-key")
    monkeypatch.setattr("app.llm.client.OpenAI", lambda **kwargs: _FakeOpenAIClient())

    client = LLMClient(model_config)

    assert client.configured_max_output_tokens == 819_200
    assert client.max_output_tokens == 131_072


def test_model_config_create_defaults_to_8192_output_tokens():
    request = ModelConfigCreateRequest(
        tenant_id="tenant_demo",
        name="demo",
        model="demo-model",
    )

    assert request.max_output_tokens == 8192
    assert request.extra_body == {}


def test_dynamic_preflight_requires_native_structured_output_and_tool_call() -> None:
    """验证动态预检分别发送 JSON mode 和优先强制的工具调用探针。"""

    client = object.__new__(LLMClient)
    client.model = "demo-model"
    calls: list[dict[str, object]] = []

    class Completions:
        """按调用顺序返回结构化对象与工具调用。"""

        def create(self, **kwargs):  # noqa: ANN003, ANN201
            """记录探针请求并返回最小 OpenAI 兼容回复。"""

            calls.append(kwargs)
            if "response_format" in kwargs:
                return _completion_with_content('{"probe":"ready"}')
            function = type(
                "Function", (), {"name": "dynamic_capability_probe", "arguments": "{}"}
            )()
            tool_call = type("ToolCall", (), {"function": function})()
            message = type("Message", (), {"content": None, "tool_calls": [tool_call]})()
            choice = type("Choice", (), {"message": message})()
            return type("Completion", (), {"choices": [choice]})()

    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    result = client.preflight_dynamic_capabilities()

    assert result["structured_output"] is True
    assert result["tool_calling"] is True
    assert result["vision"] is False
    assert result["pdf_input"] is False
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 512
    assert calls[1]["max_tokens"] == 512
    assert calls[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "dynamic_capability_probe"},
    }


def test_dynamic_preflight_retries_without_tool_choice_when_protocol_rejects_it() -> None:
    """验证 thinking provider 只拒绝 tool_choice 时仍以真实工具返回证明能力。"""

    client = object.__new__(LLMClient)
    client.model = "opaque-thinking-model"
    client.thinking_mode = "enabled"
    client.extra_body = {}
    calls: list[dict[str, object]] = []

    class ToolChoiceError(Exception):
        """模拟 provider 对 thinking + tool_choice 返回的明确协议错误。"""

        status_code = 400
        body = {"error": {"message": "Thinking mode does not support this tool_choice"}}

    class Completions:
        """先拒绝强制选择，再接受由提示驱动的同一工具探针。"""

        def create(self, **kwargs):  # noqa: ANN003, ANN201
            """记录请求并按协议分支返回结构化或工具响应。"""

            calls.append(kwargs)
            if "response_format" in kwargs:
                return _completion_with_content('{"probe":"ready"}')
            if "tool_choice" in kwargs:
                raise ToolChoiceError("Thinking mode does not support this tool_choice")
            if "tools" in kwargs:
                function = type(
                    "Function", (), {"name": "dynamic_capability_probe", "arguments": "{}"}
                )()
                tool_call = type("ToolCall", (), {"function": function})()
                message = type("Message", (), {"content": None, "tool_calls": [tool_call]})()
                choice = type("Choice", (), {"message": message})()
                return type("Completion", (), {"choices": [choice]})()
            return _completion_with_content("unsupported optional input")

    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    result = client.preflight_dynamic_capabilities()

    assert result["tool_calling"] is True
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "tool_choice" in calls[1]
    assert "tool_choice" not in calls[2]
    assert calls[2]["tools"] == calls[1]["tools"]


def test_dynamic_preflight_retries_for_provider_invalid_parameter_without_field_echo() -> None:
    """兼容 provider 仅返回 InvalidParameter、未回显 tool_choice 字段的协议错误。"""

    client = object.__new__(LLMClient)
    client.model = "ark-kimi-k3"
    calls: list[dict[str, object]] = []

    class InvalidParameterError(Exception):
        """模拟 Ark 对强制 tool_choice 返回无字段名的错误体。"""

        status_code = 400
        body = {"error": {"code": "InvalidParameter", "message": "A parameter is not valid"}}

    class Completions:
        """强制选择失败后，允许提示驱动的真实工具调用。"""

        def create(self, **kwargs):  # noqa: ANN003, ANN201
            """记录探针请求并返回结构化/工具结果。"""

            calls.append(kwargs)
            if "response_format" in kwargs:
                return _completion_with_content('{"probe":"ready"}')
            if "tool_choice" in kwargs:
                raise InvalidParameterError("invalid parameter")
            function = type(
                "Function", (), {"name": "dynamic_capability_probe", "arguments": "{}"}
            )()
            tool_call = type("ToolCall", (), {"function": function})()
            message = type("Message", (), {"content": None, "tool_calls": [tool_call]})()
            choice = type("Choice", (), {"message": message})()
            return type("Completion", (), {"choices": [choice]})()

    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    result = client.preflight_dynamic_capabilities()

    assert result["tool_calling"] is True
    assert "tool_choice" in calls[1]
    assert "tool_choice" not in calls[2]


def test_dynamic_preflight_retries_compatible_tool_probe_after_text_only_response() -> None:
    """provider偶发返回普通文本时，预检有界重试但不把文本当作工具能力。"""

    client = object.__new__(LLMClient)
    client.model = "ark-kimi-k3"
    calls: list[dict[str, object]] = []
    fallback_attempts = 0

    class InvalidParameterError(Exception):
        """模拟强制 tool_choice 被 provider 拒绝。"""

        status_code = 400
        body = {"error": {"code": "InvalidParameter"}}

    class Completions:
        """记录降级探针，并在最后一次返回真实 tool call。"""

        def create(self, **kwargs):  # noqa: ANN003, ANN201
            """按探针阶段返回结构化、协议错误、文本和工具响应。"""

            nonlocal fallback_attempts
            calls.append(kwargs)
            if "response_format" in kwargs:
                return _completion_with_content('{"probe":"ready"}')
            if "tool_choice" in kwargs:
                raise InvalidParameterError("invalid parameter")
            fallback_attempts += 1
            if fallback_attempts < 3:
                return _completion_with_content("not a tool call")
            function = type(
                "Function", (), {"name": "dynamic_capability_probe", "arguments": "{}"}
            )()
            tool_call = type("ToolCall", (), {"function": function})()
            message = type("Message", (), {"content": None, "tool_calls": [tool_call]})()
            choice = type("Choice", (), {"message": message})()
            return type("Completion", (), {"choices": [choice]})()

    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    result = client.preflight_dynamic_capabilities()

    assert result["tool_calling"] is True
    assert len(calls) == 7
    assert calls[3]["messages"][0]["content"].startswith("Emit exactly one")


def test_dynamic_preflight_freezes_successful_optional_image_and_pdf_probes() -> None:
    """验证原生图片/PDF 能力来自真实内容探针结果，而不是按模型名称猜测。"""

    client = object.__new__(LLMClient)
    client.model = "opaque-model-name"
    calls = 0

    class Completions:
        """按四个探针顺序返回核心协议与两种原生输入证据。"""

        def create(self, **kwargs):  # noqa: ANN003, ANN201
            """根据请求形态返回对应能力探针结果。"""

            nonlocal calls
            calls += 1
            if "response_format" in kwargs:
                return _completion_with_content('{"probe":"ready"}')
            if "tools" in kwargs:
                function = type(
                    "Function", (), {"name": "dynamic_capability_probe", "arguments": "{}"}
                )()
                tool_call = type("ToolCall", (), {"function": function})()
                message = type("Message", (), {"content": None, "tool_calls": [tool_call]})()
                choice = type("Choice", (), {"message": message})()
                return type("Completion", (), {"choices": [choice]})()
            content = kwargs["messages"][0]["content"]
            if any(part.get("type") == "image_url" for part in content):
                return _completion_with_content("red left, blue right")
            return _completion_with_content("PDFCAP7")

    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    result = client.preflight_dynamic_capabilities()

    assert calls == 4
    assert result["vision"] is True
    assert result["pdf_input"] is True


def test_dynamic_preflight_rejects_text_instead_of_required_tool_call() -> None:
    """验证 provider 即使支持 JSON，未返回强制工具调用仍不可用。"""

    client = object.__new__(LLMClient)
    client.model = "demo-model"
    responses = iter(
        [_completion_with_content('{"probe":"ready"}'), _completion_with_content("done")]
    )
    completions = type(
        "Completions",
        (),
        {"create": lambda self, **kwargs: next(responses)},  # noqa: ARG005
    )()
    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()

    with pytest.raises(LLMError, match="no required tool call"):
        client.preflight_dynamic_capabilities()


def _completion_with_content(content):  # noqa: ANN001
    return type(
        "Completion",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {"message": type("Message", (), {"content": content})()},
                )()
            ]
        },
    )()


def test_generate_text_uses_chat_completions_only():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text("system prompt", {"hello": "world"})

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["model"] == "demo-model"
    assert call["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": '{"hello": "world"}'},
    ]
    assert call["max_tokens"] == 256


def test_connection_probe_reserves_reasoning_budget_before_expect_text() -> None:
    """验证连接探针给推理模型留出足够配额，避免16 token导致空正文假阴性。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "reasoning-model"

    assert client.probe_text_connection() == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["temperature"] == 0
    assert call["max_tokens"] == 256


def test_connection_and_optional_input_probes_keep_provider_extra_body() -> None:
    """验证连接、图片与PDF探针沿用管理端参数，避免思考模型产生空正文假失败。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "thinking-vision-model"
    client.thinking_mode = ""
    client.extra_body = {"enable_thinking": False}

    assert client.probe_text_connection() == "ok"
    client._probe_optional_dynamic_inputs()

    calls = client.client.chat.completions.calls
    assert len(calls) == 3
    assert all(call["extra_body"] == {"enable_thinking": False} for call in calls)
    assert calls[1]["max_tokens"] == OPTIONAL_INPUT_PROBE_MAX_TOKENS
    assert calls[2]["max_tokens"] == OPTIONAL_INPUT_PROBE_MAX_TOKENS


def test_generate_text_can_disable_provider_thinking():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.thinking_mode = "disabled"

    assert client.generate_text("system prompt", "hello") == "ok"

    call = client.client.chat.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}


def test_generate_text_passes_model_extra_body_and_preserves_thinking_options():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "glm-5.2"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.thinking_mode = "disabled"
    client.extra_body = {
        "thinking": {"type": "disabled", "clear_thinking": True},
        "do_sample": False,
    }

    assert client.generate_text("system prompt", "hello") == "ok"

    call = client.client.chat.completions.calls[0]
    assert call["extra_body"] == {
        "thinking": {"type": "disabled", "clear_thinking": True},
        "do_sample": False,
    }


def test_thinking_mode_can_be_scoped_to_specific_models():
    assert _thinking_mode_for_model("disabled", "glm-5.2", "glm-5.2") == "disabled"
    assert _thinking_mode_for_model("disabled", "glm-5.2", "deepseek-v4-pro") == ""


def test_generate_text_preserves_plain_user_content_without_json_encoding():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    content = "技能标题：新SOP\n原始流程：\n收集报销事由并提交审批。"

    assert client.generate_text("system prompt", content) == "ok"
    assert client.client.chat.completions.calls[0]["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": content},
    ]


def test_generate_text_persists_provider_request_metrics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        with llm_operation("router.scene"):
            assert client.generate_text("system prompt", {"hello": "world"}) == "ok"

    assert [event_type for event_type, _ in events] == [
        "llm_call_started",
        "llm_call_finished",
    ]
    started, finished = events[0][1], events[1][1]
    assert started["span_id"] == finished["span_id"]
    assert finished["operation"] == "router.scene"
    assert finished["model"] == "demo-model"
    assert finished["attempt"] == 1
    assert finished["retry_count"] == 0
    assert finished["output_chars"] == 2
    assert finished["duration_ms"] >= 0
    assert finished["ttft_ms"] >= 0
    assert finished["system_prompt_chars"] == len("system prompt")
    assert finished["context_message_count"] == 0
    assert finished["context_text_chars"] == 0
    assert finished["payload_chars"] == len('{"hello": "world"}')
    assert finished["request_text_chars"] == len("system prompt") + len(
        '{"hello": "world"}'
    )
    assert finished["request_message_roles"] == ["system", "user"]
    assert len(finished["request_prefix_fingerprints"]) == 2


def test_generate_text_persists_provider_cache_usage_metrics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []
    message = type("Message", (), {"content": "ok"})()
    choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
    prompt_details = type("PromptDetails", (), {"cached_tokens": 448})()
    usage = type(
        "Usage",
        (),
        {
            "prompt_tokens": 3217,
            "completion_tokens": 848,
            "total_tokens": 4065,
            "prompt_tokens_details": prompt_details,
        },
    )()
    completion = type(
        "Completion",
        (),
        {"id": "as-demo", "choices": [choice], "usage": usage},
    )()
    client.client.chat.completions.create = lambda **_kwargs: completion

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        assert client.generate_text("system", {"hello": "world"}) == "ok"

    finished = next(
        payload for event_type, payload in events if event_type == "llm_call_finished"
    )
    assert finished["input_tokens"] == 3217
    assert finished["cached_input_tokens"] == 448
    assert finished["uncached_input_tokens"] == 2769


def test_generate_text_retries_empty_response():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    contents = iter(["", None, "ok"])

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(contents))

    client.client.chat.completions.create = fake_create

    assert client.generate_text("system prompt", {"hello": "world"}) == "ok"
    assert len(client.client.chat.completions.calls) == 3


def test_generate_text_retries_transient_provider_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 临时过载时只做有界退避重试，恢复后返回正文而不改写业务输入。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    calls = 0

    class ServerOverloaded(Exception):
        """模拟 OpenAI-compatible provider 的 429 过载错误。"""

        status_code = 429
        body = {"error": {"code": "ServerOverloaded"}}

    def fake_create(**kwargs):  # noqa: ANN003
        nonlocal calls
        calls += 1
        client.client.chat.completions.calls.append(kwargs)
        if calls < 3:
            raise ServerOverloaded("temporarily overloaded")
        return _completion_with_content("ok")

    monkeypatch.setattr("app.llm.client._wait_before_provider_retry", lambda _attempt: None)
    client.client.chat.completions.create = fake_create

    assert client.generate_text("system prompt", {"hello": "world"}) == "ok"
    assert calls == 3


def test_generate_text_records_each_empty_response_retry():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    contents = iter(["", None, "ok"])
    events: list[tuple[str, dict]] = []

    client.client.chat.completions.create = lambda **_kwargs: _completion_with_content(
        next(contents)
    )
    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        assert client.generate_text("system prompt", {"hello": "world"}) == "ok"

    finished = [payload for event_type, payload in events if event_type == "llm_call_finished"]
    assert [item["status"] for item in finished] == ["empty", "empty", "success"]
    assert [item["attempt"] for item in finished] == [1, 2, 3]
    assert [item["retry_count"] for item in finished] == [0, 1, 2]


def test_generate_text_empty_response_reports_provider_diagnostics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://user:secret@example.test/v1?token=hidden"
    client.timeout_seconds = 600.0
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        message = type(
            "Message",
            (),
            {
                "content": None,
                "reasoning_content": "provider-side reasoning",
                "refusal": None,
                "tool_calls": [],
            },
        )()
        choice = type("Choice", (), {"message": message, "finish_reason": "length"})()
        usage = type("Usage", (), {"completion_tokens": 256})()
        return type("Completion", (), {"id": "resp_demo", "choices": [choice], "usage": usage})()

    client.client.chat.completions.create = fake_create

    with pytest.raises(LLMError) as error:
        client.generate_text("system prompt", {"hello": "world"})

    detail = str(error.value)
    assert "Model returned an empty response after 3 attempts" in detail
    assert "provider returned no usable message.content" in detail
    assert "model=demo-model" in detail
    assert "endpoint=https://example.test/v1" in detail
    assert "finish_reason=length" in detail
    assert "reasoning_chars=23" in detail
    assert "completion_tokens=256" in detail
    assert "secret" not in detail
    assert "hidden" not in detail


def test_generate_text_reads_text_from_structured_content_parts():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    part = type("ContentPart", (), {"text": "structured answer"})()

    client.client.chat.completions.create = lambda **_kwargs: _completion_with_content([part])

    assert client.generate_text("system prompt", {"hello": "world"}) == "structured answer"


def test_generate_text_stream_reports_empty_stream_diagnostics():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.timeout_seconds = 600.0
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        delta = type("Delta", (), {"content": None, "reasoning_content": "reasoning only"})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": "stop"})()
        chunk = type("Chunk", (), {"id": "chunk_demo", "choices": [choice]})()
        return iter([chunk])

    client.client.chat.completions.create = fake_create

    with pytest.raises(LLMError) as error:
        list(client.generate_text_stream("system prompt", {"hello": "world"}))

    detail = str(error.value)
    assert "stream_chunks=1" in detail
    assert "finish_reason=stop" in detail
    assert "reasoning_chars=14" in detail
    assert len(client.client.chat.completions.calls) == 3
    assert all(call["messages"][0] == {"role": "system", "content": "system prompt"} for call in client.client.chat.completions.calls)


def test_generate_text_stream_records_ttft_and_output_volume():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    events: list[tuple[str, dict]] = []

    def chunk(content, finish_reason=None):  # noqa: ANN001
        delta = type("Delta", (), {"content": content, "reasoning_content": None})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()
        return type("Chunk", (), {"id": "chunk_demo", "choices": [choice]})()

    client.client.chat.completions.create = lambda **_kwargs: iter(
        [chunk("你"), chunk("好", "stop")]
    )

    with bind_span_sink(lambda event_type, payload: events.append((event_type, payload))):
        with llm_operation("response.generate_stream"):
            assert "".join(client.generate_text_stream("system", {"hello": "world"})) == "你好"

    finished = next(
        payload for event_type, payload in events if event_type == "llm_call_finished"
    )
    assert finished["operation"] == "response.generate_stream"
    assert finished["stream"] is True
    assert finished["ttft_ms"] is not None
    assert finished["output_chars"] == 2
    assert finished["stream_chunks"] == 2
    assert finished["finish_reasons"] == ["stop"]


def test_generate_text_stream_cancels_before_first_token_and_closes_provider() -> None:
    """验证 provider 首 token 阻塞时取消仍及时生效，并尝试关闭远程流。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    cancelled = Event()
    provider_created = Event()
    provider_closed = Event()

    class BlockingStream:
        """模拟在首 token 前永久阻塞且支持 close 的 provider 流。"""

        def __iter__(self):
            provider_created.set()
            provider_closed.wait(timeout=5)
            return iter(())

        def close(self) -> None:
            """记录上层已尝试关闭 provider 流。"""

            provider_closed.set()

    client.client.chat.completions.create = lambda **_kwargs: BlockingStream()
    errors: list[BaseException] = []

    def consume() -> None:
        """在独立线程消费流，便于测试线程触发取消。"""

        try:
            list(
                client.generate_text_stream(
                    "system", {}, is_cancelled=cancelled.is_set
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=consume)
    started = monotonic()
    worker.start()
    assert provider_created.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert monotonic() - started < 1.5
    assert provider_closed.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], LLMStreamCancelled)


def test_generate_text_stream_cancels_while_generator_is_executing() -> None:
    """真实 generator 正在另一线程执行时，跨线程 close 失败不得覆盖取消终态。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    cancelled = Event()
    generator_entered = Event()
    release_generator = Event()

    def blocking_generator():
        """停在 generator frame 内，复现跨线程 close 的 ValueError 边界。"""

        generator_entered.set()
        release_generator.wait(timeout=5)
        if False:
            yield None

    client.client.chat.completions.create = lambda **_kwargs: blocking_generator()
    errors: list[BaseException] = []

    def consume() -> None:
        """消费 provider 流并记录对调用方可见的唯一终态。"""

        try:
            list(client.generate_text_stream("system", {}, is_cancelled=cancelled.is_set))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=consume)
    worker.start()
    assert generator_entered.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=1)
    release_generator.set()

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LLMStreamCancelled)


def test_generate_text_stream_cancels_midstream_without_emitting_late_chunk() -> None:
    """流中取消保留已交付 chunk，provider 稍后返回的内容不得再向上游发送。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    cancelled = Event()
    release_late = Event()
    closed = Event()

    def chunk(content: str):
        """构造含正文的 provider chunk。"""

        delta = type("Delta", (), {"content": content, "reasoning_content": None})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": None})()
        return type("Chunk", (), {"id": "chunk_demo", "choices": [choice]})()

    class TwoStageStream:
        """先返回一块正文，再阻塞模拟延迟的下一块。"""

        def __iter__(self):
            yield chunk("已发送")
            release_late.wait(timeout=5)
            yield chunk("不应发送")

        def close(self) -> None:
            """释放阻塞并记录关闭。"""

            closed.set()
            release_late.set()

    client.client.chat.completions.create = lambda **_kwargs: TwoStageStream()
    stream = client.generate_text_stream("system", {}, is_cancelled=cancelled.is_set)

    assert next(stream) == "已发送"
    cancelled.set()
    with pytest.raises(LLMStreamCancelled):
        next(stream)
    assert closed.is_set()


def test_generate_text_stream_does_not_wait_for_blocking_provider_close() -> None:
    """provider.close 卡死时，本地取消仍必须在有界时间内返回。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    cancelled = Event()
    provider_created = Event()
    release_close = Event()

    class BlockingCloseStream:
        """模拟首 token 前阻塞，且 close 本身也阻塞的恶劣 provider。"""

        def __iter__(self):
            provider_created.set()
            release_close.wait(timeout=5)
            return iter(())

        def close(self) -> None:
            """持续阻塞直到测试显式释放。"""

            release_close.wait(timeout=5)

    client.client.chat.completions.create = lambda **_kwargs: BlockingCloseStream()
    errors: list[BaseException] = []

    def consume() -> None:
        """消费模型流并收集取消异常。"""

        try:
            list(client.generate_text_stream("system", {}, is_cancelled=cancelled.is_set))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=consume)
    worker.start()
    assert provider_created.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=0.5)
    release_close.set()

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LLMStreamCancelled)


def test_generate_text_projects_conversation_context_messages():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "user_message": "买两个",
            "execution_state": {
                "active_skill_id": "purchase",
                "active_step_id": None,
                "slots": {},
                "pending_tasks": [],
            },
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "我是 hx，我要买 A2"},
                    {"role": "assistant", "content": "请问买几个？"},
                    {"role": "user", "content": "买两个"},
                ],
                "metadata": {"total_messages": 3},
            },
        },
    )

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["messages"][0] == {"role": "system", "content": "system prompt"}
    assert sum(message["role"] == "system" for message in call["messages"]) == 1
    assert call["messages"][1:4] == [
        {"role": "user", "content": "我是 hx，我要买 A2"},
        {"role": "assistant", "content": "请问买几个？"},
        {"role": "user", "content": "买两个"},
    ]
    current_input = call["messages"][-1]
    assert current_input["role"] == "user"
    assert current_input["content"].startswith("本轮输入（仅用于当前调用，不写入对话历史）：")
    assert '"execution_state": {"active_skill_id": "purchase"}' in current_input["content"]
    assert '"user_message"' not in current_input["content"]
    assert '"conversation_context"' not in current_input["content"]
    assert '"active_step_id"' not in current_input["content"]
    assert '"slots"' not in current_input["content"]
    assert '"pending_tasks"' not in current_input["content"]


def test_generate_text_uses_128k_context_budget_from_conversation_metadata() -> None:
    """验证 128K 租户配置会传到最终请求裁剪层，而不是退回旧的 32K。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"历史消息 {index}：" + "中" * 8_000,
        }
        for index in range(10)
    ]

    assert DEFAULT_INPUT_TOKEN_BUDGET == 128_000
    assert _request_input_token_budget(
        {"conversation_context": {"metadata": {"token_budget": 128_000}}}
    ) == 128_000
    client.generate_text(
        "system prompt",
        {
            "user_message": "继续处理当前任务",
            "conversation_context": {
                "messages": history,
                "metadata": {"token_budget": 128_000},
            },
        },
    )

    request = client.client.chat.completions.calls[0]["messages"]
    assert len(request) == len(history) + 2
    assert _request_tokens(request) > 32_000


def test_generate_text_preserves_explicit_32k_context_budget() -> None:
    """验证已有租户显式 32K 配置仍然生效，不被新默认值覆盖。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"历史消息 {index}：" + "中" * 8_000,
        }
        for index in range(10)
    ]

    assert _request_input_token_budget(
        {"conversation_context": {"metadata": {"token_budget": 32_000}}}
    ) == 32_000
    client.generate_text(
        "system prompt",
        {
            "user_message": "继续处理当前任务",
            "conversation_context": {
                "messages": history,
                "metadata": {"token_budget": 32_000},
            },
        },
    )

    request = client.client.chat.completions.calls[0]["messages"]
    assert len(request) < len(history) + 2
    assert _request_tokens(request) <= 32_000


def test_generate_text_stream_uses_context_metadata_budget() -> None:
    """验证流式模型请求与非流式请求使用相同的 128K 上下文预算契约。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.base_url = "https://example.test/v1"
    client.temperature = 0.2
    client.max_output_tokens = 256
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"历史消息 {index}：" + "中" * 8_000,
        }
        for index in range(10)
    ]

    def chunk(content: str, finish_reason: str | None = None):
        """构造最小流式响应块，覆盖首正文和正常终态。"""

        delta = type("Delta", (), {"content": content, "reasoning_content": None})()
        choice = type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()
        return type("Chunk", (), {"id": "context-budget-chunk", "choices": [choice]})()

    def fake_create(**kwargs):  # noqa: ANN003
        """记录流式请求，并返回正常结束的单块响应。"""

        client.client.chat.completions.calls.append(kwargs)
        return iter([chunk("ok", "stop")])

    client.client.chat.completions.create = fake_create
    payload = {
        "user_message": "继续处理当前任务",
        "conversation_context": {
            "messages": history,
            "metadata": {"token_budget": 128_000},
        },
    }

    assert "".join(client.generate_text_stream("system prompt", payload)) == "ok"
    request = client.client.chat.completions.calls[0]["messages"]
    assert len(request) == len(history) + 2
    assert _request_tokens(request) > 32_000


def test_stage_input_uses_stable_history_and_puts_memory_time_and_question_first():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    payload = stage_payload(
        phase="Router",
        user_message="我想申请报销",
        conversation_context={
            "messages": [
                {"role": "user", "content": "历史的信息可以被总结为：\n用户是研发人员"},
                {"role": "user", "content": "近期的历史信息总结为：\n正在咨询差旅"},
                {"role": "assistant", "content": "请说明本次需求"},
                {"role": "user", "content": "我想申请报销"},
            ],
            "metadata": {"current_turn_time": "2026-07-13T20:30:00+08:00"},
        },
        memory_context=[{"content": "用户偏好简洁回复", "id": "memory_internal"}],
        instructions="只根据技能摘要路由。",
        stage_data={"available_skills": [{"skill_id": "travel", "name": "差旅报销"}]},
        output_contract={"decision": "start_new_task | answer_only"},
    )

    assert client.generate_text("stable unified system", payload) == "ok"

    messages = client.client.chat.completions.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "stable unified system"}
    assert messages[1:4] == [
        {"role": "user", "content": "历史的信息可以被总结为：\n用户是研发人员"},
        {"role": "user", "content": "近期的历史信息总结为：\n正在咨询差旅"},
        {"role": "assistant", "content": "请说明本次需求"},
    ]
    current = messages[-1]["content"]
    assert current.startswith("用户记忆：\n- 用户偏好简洁回复\n\n本轮时间：")
    assert "本轮时间：\n2026-07-13T20:30:00+08:00" in current
    assert "本轮用户输入：\n我想申请报销" in current
    assert "当前阶段：\nRouter" in current
    assert "思考要求：" in current
    assert "保留完成当前阶段所需的简短思考" in current
    assert "available_skills" in current
    assert "memory_internal" not in current
    assert sum("我想申请报销" in str(message["content"]) for message in messages) == 1


def test_stage_requests_append_each_input_and_output_to_one_turn_context() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    outputs = iter(
        [
            '{"decision":"answer_only","confidence":0.9}',
            '{"reply":"已处理","is_step_completed":true}',
        ]
    )

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(outputs))

    client.client.chat.completions.create = fake_create
    stable_messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "请说明需求"},
        {"role": "user", "content": "查询报销规则"},
    ]
    context = {
        "messages": stable_messages.copy(),
        "metadata": {"current_turn_time": "2026-07-13T21:00:00+08:00"},
    }

    router_payload = stage_payload(
        phase="Router",
        user_message="查询报销规则",
        conversation_context=context,
        memory_context=[],
        instructions="选择处理路径。",
        stage_data={"available_skills": []},
        output_contract={"decision": "answer_only"},
    )
    step_payload = stage_payload(
        phase="Step Agent",
        user_message="查询报销规则",
        conversation_context=context,
        memory_context=[],
        instructions="执行当前步骤。",
        stage_data={"current_step": {"node_id": "start"}},
        output_contract={"reply": "string"},
    )

    assert client.generate_json("stable unified system", router_payload)["decision"] == "answer_only"
    assert client.generate_json("stable unified system", step_payload)["reply"] == "已处理"

    first_request = client.client.chat.completions.calls[0]["messages"]
    second_request = client.client.chat.completions.calls[1]["messages"]
    assert first_request[0] == second_request[0] == {
        "role": "system",
        "content": "stable unified system",
    }
    assert second_request[1:3] == stable_messages[:2]
    assert second_request[3] == first_request[-1]
    assert second_request[4] == {
        "role": "assistant",
        "content": '{"decision":"answer_only","confidence":0.9}',
    }
    assert second_request[5]["role"] == "user"
    assert "当前阶段：\nStep Agent" in second_request[5]["content"]
    assert "本轮用户输入：" not in second_request[5]["content"]
    assert sum(
        "本轮用户输入：" in str(message["content"])
        for message in second_request
    ) == 1
    assert context["messages"] == stable_messages
    assert context[TURN_STAGE_MESSAGES_KEY] == [
        first_request[-1],
        {"role": "assistant", "content": '{"decision":"answer_only","confidence":0.9}'},
        second_request[-1],
        {
            "role": "assistant",
            "content": '{"reply":"已处理","is_step_completed":true}',
        },
    ]


def test_stage_json_repair_continues_in_the_same_turn_context() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    outputs = iter(["not json", '{"decision":"answer_only"}'])

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        return _completion_with_content(next(outputs))

    client.client.chat.completions.create = fake_create
    context = {
        "messages": [{"role": "user", "content": "你好"}],
        "metadata": {"current_turn_time": "2026-07-13T21:20:00+08:00"},
    }
    payload = stage_payload(
        phase="Router",
        user_message="你好",
        conversation_context=context,
        memory_context=[],
        instructions="输出路由 JSON。",
        stage_data={"available_skills": []},
        output_contract={"decision": "answer_only"},
    )

    assert client.generate_json("stable unified system", payload) == {
        "decision": "answer_only"
    }

    first_request = client.client.chat.completions.calls[0]["messages"]
    repair_request = client.client.chat.completions.calls[1]["messages"]
    assert repair_request[1] == first_request[-1]
    assert repair_request[2] == {"role": "assistant", "content": "not json"}
    assert repair_request[-1]["role"] == "user"
    assert '"_json_repair"' in repair_request[-1]["content"]
    assert "本轮用户输入：" not in repair_request[-1]["content"]
    assert context[TURN_STAGE_MESSAGES_KEY] == [
        first_request[-1],
        {"role": "assistant", "content": "not json"},
        repair_request[-1],
        {"role": "assistant", "content": '{"decision":"answer_only"}'},
    ]


def test_generate_text_keeps_append_only_history_prefix_for_kv_cache() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    stable_history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "查询退款规则"},
    ]

    client.generate_text(
        "stable system",
        {
            "conversation_context": {"messages": stable_history},
            "retrieved_knowledge": [{"label": "检索到的知识 1", "content": "七天内"}],
        },
    )
    client.generate_text(
        "stable system",
        {
            "conversation_context": {
                "messages": [
                    *stable_history,
                    {"role": "assistant", "content": "七天内可申请退款。"},
                    {"role": "user", "content": "需要什么材料？"},
                ]
            },
            "slots": {"topic": "退款材料"},
        },
    )

    first_messages = client.client.chat.completions.calls[0]["messages"]
    second_messages = client.client.chat.completions.calls[1]["messages"]
    assert first_messages[:4] == second_messages[:4]
    assert first_messages[0] == {"role": "system", "content": "stable system"}
    assert "检索到的知识 1" in first_messages[-1]["content"]
    assert "检索到的知识 1" not in str(second_messages)


def test_generate_text_projects_conversation_context_images_for_vision_model():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "gpt-4o-mini"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "user_message": "看这张图",
            "conversation_context": {
                "messages": [
                    {
                        "role": "user",
                        "content": "看这张图",
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,AAAA",
                                    "detail": "auto",
                                },
                            }
                        ],
                    }
                ],
            },
        },
    )

    assert output == "ok"
    call = client.client.chat.completions.calls[0]
    assert call["messages"][1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "auto"}},
        ],
    }
    assert '"messages":' not in call["messages"][-1]["content"]


def test_generate_text_keeps_memory_capture_history_as_role_messages() -> None:
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    assert client.generate_text(
        "memory prompt",
        {
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "我32岁"},
                    {"role": "assistant", "content": "已记录"},
                ]
            },
            "existing_memories": "- profile/age: 32",
        },
    ) == "ok"

    messages = client.client.chat.completions.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert sum(message["role"] == "system" for message in messages) == 1
    assert messages[0] == {"role": "system", "content": "memory prompt"}
    assert messages[1:3] == [
        {"role": "user", "content": "我32岁"},
        {"role": "assistant", "content": "已记录"},
    ]
    assert messages[-1]["role"] == "user"
    assert '"existing_memories": "- profile/age: 32"' in messages[-1]["content"]
    assert all('"conversation_context"' not in str(message["content"]) for message in messages)


def test_generate_text_does_not_guess_image_support_from_model_name():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "qwen3-6-27b"
    client.temperature = 0.2
    client.max_output_tokens = 256

    output = client.generate_text(
        "system prompt",
        {
            "conversation_context": {
                "messages": [
                    {
                        "role": "user",
                        "content": "看图",
                        "images": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}],
                    }
                ],
            },
        },
    )

    assert output == "ok"
    assert client.client.chat.completions.calls[0]["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }


def test_generate_json_extracts_fenced_json(monkeypatch):
    client = object.__new__(LLMClient)

    def fake_generate_text(_system_prompt, _payload):
        return '```json\n{"decision": "continue_active"}\n```'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"decision": "continue_active"}


def test_generate_json_requests_json_object_mode():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    client.client.chat.completions.create = lambda **kwargs: (  # noqa: E731
        client.client.chat.completions.calls.append(kwargs)
        or type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": '{"ok": true}'})()},
                    )()
                ]
            },
        )()
    )

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert client.client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_generate_json_with_metadata_preserves_provider_action_identity():
    """验证动态提案取得真实 response id、finish reason 和用量，而非用正文猜造身份。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    completion = _completion_with_content('{"ok": true}')
    completion.id = "response-provider-123"
    completion.choices[0].finish_reason = "stop"
    completion.usage = type(
        "Usage",
        (),
        {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    )()
    client.client.chat.completions.create = lambda **kwargs: completion  # noqa: ARG005

    payload, metadata = client.generate_json_with_metadata("prompt", {})

    assert payload == {"ok": True}
    assert metadata == {
        "response_id": "response-provider-123",
        "finish_reason": "stop",
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }


def test_generate_json_with_metadata_retries_parseable_length_response():
    """JSON虽可解析但以length结束时提高配额重试，不能把半完成动作持久化。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256
    first = _completion_with_content('{"ok": true}')
    first.id = "response-provider-length"
    first.choices[0].finish_reason = "length"
    second = _completion_with_content('{"ok": true}')
    second.id = "response-provider-stop"
    second.choices[0].finish_reason = "stop"
    responses = iter([first, second])

    def fake_create(**kwargs):  # noqa: ANN003
        """记录截断恢复使用的有界输出预算。"""

        client.client.chat.completions.calls.append(kwargs)
        return next(responses)

    client.client.chat.completions.create = fake_create

    payload, metadata = client.generate_json_with_metadata("prompt", {})

    assert payload == {"ok": True}
    assert metadata["response_id"] == "response-provider-stop"
    assert metadata["finish_reason"] == "stop"
    assert [call["max_tokens"] for call in client.client.chat.completions.calls] == [256, 512]
    assert "finish_reason=length" in client.client.chat.completions.calls[1]["messages"][-1]["content"]


def test_internal_json_operation_caps_output_without_mutating_system_prompt():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    client.client.chat.completions.create = lambda **kwargs: (  # noqa: E731
        client.client.chat.completions.calls.append(kwargs)
        or _completion_with_content('{"decision":"answer_only"}')
    )

    with llm_operation("router.scene"):
        assert client.generate_json("router prompt", {}) == {"decision": "answer_only"}

    call = client.client.chat.completions.calls[0]
    assert call["max_tokens"] == 4096
    assert call["messages"][0]["content"] == "router prompt"


def test_internal_output_budget_never_increases_smaller_model_config():
    assert operation_output_tokens("router.scene", 256) == 256


def test_dynamic_plan_disables_thinking_without_changing_answer_profile() -> None:
    """结构化Planner关闭推理耗散，最终答案仍尊重管理端模型配置。"""

    assert operation_thinking_mode("dynamic_task.plan", "enabled") == "disabled"
    assert operation_thinking_mode("dynamic_task.answer", "enabled") == "enabled"


def test_dynamic_plan_request_overrides_configured_thinking_to_disabled() -> None:
    """真实请求参数必须把Planner阶段投影为disabled，而非只修改telemetry标签。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    client.thinking_mode = "enabled"
    client.extra_body = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    client.client.chat.completions.create = lambda **kwargs: (  # noqa: E731
        client.client.chat.completions.calls.append(kwargs)
        or _completion_with_content('{"goal":"ok"}')
    )

    with llm_operation("dynamic_task.plan"):
        assert client.generate_json("planner", {}) == {"goal": "ok"}

    assert client.client.chat.completions.calls[0]["extra_body"]["thinking"] == {
        "type": "disabled",
        "budget_tokens": 1024,
    }


def test_user_visible_response_caps_output_budget_at_4096():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192

    with llm_operation("response.generate"):
        assert client.generate_text("system prompt", {}) == "ok"

    assert client.client.chat.completions.calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("router.scene", 4096),
        ("step_agent.run", 4096),
        ("step_agent.repair", 4096),
        ("response.generate", 4096),
        ("response.generate_stream", 4096),
        ("reflection.review", 2048),
        ("general_skill.select", 2048),
        ("dynamic_task.route_shadow", 2048),
        ("dynamic_task.plan", 8192),
        ("dynamic_task.action", 2048),
        ("dynamic_task.action.write", 8192),
        ("dynamic_task.answer", 8192),
        ("general_skill.review", 2048),
        ("general_skill.reply", 2048),
        ("knowledge.document_route", 2048),
        ("knowledge.bucket_route", 512),
        ("memory.capture", 1024),
        ("session.title", 512),
    ],
)
def test_control_plane_operation_output_budgets(operation, expected):  # noqa: ANN001
    assert operation_output_tokens(operation, 8192) == expected


def test_dynamic_plan_can_use_configured_long_form_budget_without_inflating_short_models() -> None:
    """复杂计划可使用管理端16K上限，但小模型配置仍不会被宿主放大。"""

    assert operation_output_tokens("dynamic_task.plan", 16_384) == 8_192
    assert operation_output_tokens("dynamic_task.plan", 8_192) == 8_192


def test_step_agent_caps_output_budget_at_4096() -> None:
    assert operation_output_tokens("step_agent.run", 8192) == 4096
    assert operation_output_tokens("step_agent.repair", 8192) == 4096


def test_dynamic_answer_reserves_one_complete_long_form_response() -> None:
    """Dynamic最终交付预留16K，普通动作2K，写入动作保留8K。"""

    assert operation_output_tokens("dynamic_task.answer", 32_768) == 16_384
    assert operation_output_tokens("dynamic_task.action", 32_768) == 2_048
    assert operation_output_tokens("dynamic_task.action.write", 32_768) == 8_192


def test_generate_json_falls_back_when_json_object_mode_is_unsupported():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError("Unsupported parameter: response_format")
        return type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": '{"ok": true}'})()},
                    )()
                ]
            },
        )()

    client.client.chat.completions.create = fake_create

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert "response_format" in client.client.chat.completions.calls[0]
    assert "response_format" not in client.client.chat.completions.calls[1]


def test_generate_json_falls_back_when_json_object_mode_returns_empty():
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 256

    def fake_create(**kwargs):  # noqa: ANN003
        client.client.chat.completions.calls.append(kwargs)
        if "response_format" in kwargs:
            return _completion_with_content("")
        return _completion_with_content('{"ok": true}')

    client.client.chat.completions.create = fake_create

    assert client.generate_json("prompt", {}) == {"ok": True}
    assert all("response_format" in call for call in client.client.chat.completions.calls[:3])
    assert "response_format" not in client.client.chat.completions.calls[3]


def test_generate_json_retries_invalid_json(monkeypatch):
    client = object.__new__(LLMClient)
    calls = iter(["not json", '{"ok": true}'])

    def fake_generate_text(_system_prompt, _payload):
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"ok": True}


def test_generate_json_retries_share_one_deadline(monkeypatch) -> None:
    """JSON语法修复重试必须共享总deadline，不能每轮重新获得完整模型超时。"""

    client = object.__new__(LLMClient)
    client.timeout_seconds = 1.0
    calls = iter(["not json", "still not json"])
    observed_calls: list[str] = []
    moments = iter([0.0, 0.5, 0.6, 2.0])

    def fake_generate_text(_system_prompt, _payload):
        """返回两次坏JSON，第三次调用前由总deadline拒绝继续外呼。"""

        observed_calls.append("called")
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)
    monkeypatch.setattr("app.llm.client.time.monotonic", lambda: next(moments))

    with pytest.raises(LLMError, match="MODEL_CALL_DEADLINE_EXCEEDED"):
        client.generate_json("prompt", {})
    assert observed_calls == ["called", "called"]


def test_generate_json_retry_keeps_original_payload(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []
    calls = iter(["not json", '{"ok": true}'])

    def fake_generate_text(_system_prompt, payload):
        payloads.append(payload)
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {"query": "廊坊天气", "skill": {"slug": "weather-zh"}}) == {"ok": True}
    assert payloads[1]["query"] == "廊坊天气"
    assert payloads[1]["skill"]["slug"] == "weather-zh"
    assert payloads[1]["_json_repair"]["previous_output"] == "not json"


def test_generate_json_truncation_repair_requires_compact_complete_object(monkeypatch):
    """长JSON在尾部截断时，修复轮必须压缩正文而不是重复生成同一份半包。"""

    client = object.__new__(LLMClient)
    payloads: list[dict[str, object]] = []
    outputs = iter(['{"markdown":"' + ("x" * 5_000), '{"ok":true}'])

    def fake_generate_text(system_prompt, user_payload, response_format=None):
        """记录每轮修复载荷并返回先截断、后合法的确定性响应。"""

        payloads.append(copy.deepcopy(user_payload))
        return next(outputs)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {"query": "生成结构化报告"}) == {"ok": True}
    instruction = str(payloads[1]["_json_repair"]["instruction"])
    assert "疑似因输出过长而被截断" in instruction
    assert "显著压缩" in instruction
    assert "完整闭合" in instruction


def test_json_truncation_repair_escalates_budget_but_syntax_repair_does_not() -> None:
    """截断修复逐轮扩到上限，普通JSON语法错误不得无条件增加模型成本。"""

    truncated = "上一轮 JSON 疑似因输出过长而被截断。请显著压缩。"
    assert _json_repair_output_token_budget(
        {"_json_repair": {"attempt": 1, "instruction": truncated}},
        8_192,
    ) == 16_384
    assert _json_repair_output_token_budget(
        {"_json_repair": {"attempt": 2, "instruction": truncated}},
        8_192,
    ) == 32_768
    assert _json_repair_output_token_budget(
        {"_json_repair": {"attempt": 3, "instruction": truncated}},
        8_192,
    ) == 32_768
    assert _json_repair_output_token_budget(
        {"_json_repair": {"attempt": 1, "instruction": "修复逗号和引号。"}},
        8_192,
    ) == 8_192


def test_generate_text_applies_truncation_repair_budget_to_provider_request() -> None:
    """确认截断预算真正进入 provider 请求，而非只停留在独立辅助函数。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8_192

    result = client.generate_text(
        "system",
        {
            "_json_repair": {
                "attempt": 1,
                "instruction": "上一轮 JSON 疑似因输出过长而被截断。请显著压缩。",
            }
        },
        response_format={"type": "json_object"},
    )

    assert result == "ok"
    assert client.client.chat.completions.calls[0]["max_tokens"] == 16_384


def test_generate_json_repairs_unescaped_string_quotes_without_retry(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []

    def fake_generate_text(_system_prompt, payload, response_format=None):  # noqa: ANN001, ARG001
        payloads.append(payload)
        return (
            '{"decision": "start_new_task", "target_skill_id": "purchase", '
            '"reason": "user_name 在 memory 中已明确为"hm"，不需要追问", '
            '"slot_hints": {"user_name": "hm"}}'
        )

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    result = client.generate_json("prompt", {"query": "我想买东西"})

    assert result == {
        "decision": "start_new_task",
        "target_skill_id": "purchase",
        "reason": 'user_name 在 memory 中已明确为"hm"，不需要追问',
        "slot_hints": {"user_name": "hm"},
    }
    assert len(payloads) == 1
    assert "_json_repair" not in payloads[0]


def test_generate_json_repairs_trailing_commas_and_string_newlines(monkeypatch):
    client = object.__new__(LLMClient)

    def fake_generate_text(_system_prompt, _payload, response_format=None):  # noqa: ANN001, ARG001
        return '{"ok": true, "reason": "第一行\n第二行",}'

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {}) == {"ok": True, "reason": "第一行\n第二行"}


def test_generate_json_allows_multiple_repair_attempts(monkeypatch):
    client = object.__new__(LLMClient)
    payloads = []
    calls = iter(["not json", '{"reason": "用户称呼为"', '{"ok": true}'])

    def fake_generate_text(_system_prompt, payload):
        payloads.append(payload)
        return next(calls)

    monkeypatch.setattr(client, "generate_text", fake_generate_text)

    assert client.generate_json("prompt", {"query": "你好"}) == {"ok": True}
    assert payloads[1]["_json_repair"]["attempt"] == 1
    assert payloads[2]["_json_repair"]["attempt"] == 2
    assert "parser_error" in payloads[2]["_json_repair"]


def _reasoning_length_completion():
    """构造推理内容耗尽配额且没有正文的非流式响应。"""
    message = type(
        "Message",
        (),
        {"content": "", "reasoning_content": "仍在推理"},
    )()
    choice = type("Choice", (), {"message": message, "finish_reason": "length"})()
    return type("Completion", (), {"choices": [choice], "id": "resp_reasoning"})()


def _reasoning_length_stream():
    """构造推理内容耗尽配额且没有正文的流式响应。"""
    delta = type("Delta", (), {"content": None, "reasoning_content": "仍在推理"})()
    choice = type("Choice", (), {"delta": delta, "finish_reason": "length"})()
    chunk = type("Chunk", (), {"id": "chunk_reasoning", "choices": [choice]})()
    return iter([chunk])


@pytest.mark.parametrize(
    ("operation", "configured_tokens", "expected_tokens"),
    [
        ("router.scene", 8192, [4096, 8192, 16384]),
        ("general_skill.reply", 8192, [2048, 4096, 8192]),
    ],
)
def test_generate_text_escalates_from_operation_budget_after_reasoning_length(
    operation,
    configured_tokens,
    expected_tokens,
):  # noqa: ANN001
    """非流式重试从当前 operation 配额逐次扩容，而非绕过首轮配额。"""
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = configured_tokens
    responses = iter(
        [
            _reasoning_length_completion(),
            _reasoning_length_completion(),
            _completion_with_content("ok"),
        ]
    )

    def fake_create(**kwargs):  # noqa: ANN003
        """记录每轮请求配额并返回预设响应。"""
        client.client.chat.completions.calls.append(kwargs)
        return next(responses)

    client.client.chat.completions.create = fake_create

    with llm_operation(operation):
        assert client.generate_text("system prompt", {"hello": "world"}) == "ok"

    assert [
        call["max_tokens"] for call in client.client.chat.completions.calls
    ] == expected_tokens


def test_generate_text_escalates_reasoning_only_empty_stop_response():
    """推理非空但stop无正文时也扩容，避免复杂任务重复撞上同一空响应。"""
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    empty_message = type(
        "Message",
        (),
        {"content": "", "reasoning_content": "推理完成但没有正文"},
    )()
    empty_choice = type(
        "Choice",
        (),
        {"message": empty_message, "finish_reason": "stop"},
    )()
    responses = iter(
        [
            type("Completion", (), {"choices": [empty_choice]})(),
            _completion_with_content("ok"),
        ]
    )

    def fake_create(**kwargs):  # noqa: ANN003
        """记录普通空响应重试使用的配额。"""
        client.client.chat.completions.calls.append(kwargs)
        return next(responses)

    client.client.chat.completions.create = fake_create

    with llm_operation("general_skill.reply"):
        assert client.generate_text("system prompt", {}) == "ok"

    assert [
        call["max_tokens"] for call in client.client.chat.completions.calls
    ] == [2048, 4096]


def test_generate_text_deadline_covers_all_empty_response_retries(monkeypatch) -> None:
    """单次模型阶段总deadline覆盖空响应重试，不能为每轮重置完整超时。"""

    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    client.timeout_seconds = 1.0

    def fake_create(**kwargs):  # noqa: ANN003
        """记录首轮空响应，后续应在再次请求前被总deadline阻断。"""

        client.client.chat.completions.calls.append(kwargs)
        return _reasoning_length_completion()

    client.client.chat.completions.create = fake_create
    moments = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("app.llm.client.time.monotonic", lambda: next(moments))

    with pytest.raises(LLMError, match="MODEL_CALL_DEADLINE_EXCEEDED"):
        client.generate_text("system prompt", {"hello": "world"})
    assert len(client.client.chat.completions.calls) == 1


def test_generate_text_preserves_configured_budget_above_escalation_ceiling():
    """显式配置超过扩容上限时，重试不得反向缩小操作配额。"""
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 65_536
    responses = iter([_reasoning_length_completion(), _completion_with_content("ok")])

    def fake_create(**kwargs):  # noqa: ANN003
        """记录高配额模型的重试请求。"""
        client.client.chat.completions.calls.append(kwargs)
        return next(responses)

    client.client.chat.completions.create = fake_create

    assert client.generate_text("system prompt", {}) == "ok"
    assert [
        call["max_tokens"] for call in client.client.chat.completions.calls
    ] == [65_536, 65_536]


def test_generate_text_stream_escalates_before_emitting_answer_content():
    """流式调用在尚未发出正文时可安全扩容重试，并记录新配额。"""
    client = object.__new__(LLMClient)
    client.client = _FakeOpenAIClient()
    client.model = "demo-model"
    client.temperature = 0.2
    client.max_output_tokens = 8192
    success_delta = type(
        "Delta",
        (),
        {"content": "完成", "reasoning_content": None},
    )()
    success_choice = type(
        "Choice",
        (),
        {"delta": success_delta, "finish_reason": "stop"},
    )()
    success_chunk = type(
        "Chunk",
        (),
        {"id": "chunk_success", "choices": [success_choice]},
    )()
    streams = iter([_reasoning_length_stream(), iter([success_chunk])])

    def fake_create(**kwargs):  # noqa: ANN003
        """记录流式重试请求并返回预设 chunk 迭代器。"""
        client.client.chat.completions.calls.append(kwargs)
        return next(streams)

    client.client.chat.completions.create = fake_create

    with llm_operation("response.generate_stream"):
        assert "".join(client.generate_text_stream("system prompt", {})) == "完成"

    assert [
        call["max_tokens"] for call in client.client.chat.completions.calls
    ] == [4096, 8192]
