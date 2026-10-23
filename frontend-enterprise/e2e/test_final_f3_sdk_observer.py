"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : test_final_f3_sdk_observer.py
@CallChain  : pytest → F3最终SDK观察器 → 原create/with_options/流关闭 → 安全摘要
@Description: 验证观察器逐字透传、超时副本、流取消和异常身份，不将测试SDK当真实质量。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from final_f3_preparation import PreparationError
from final_f3_sdk_observer import _safe_provider_error_metadata, observe_client


class SampleResponse:
    """合成SDK对象，仅用于检查同一返回对象和秘密不进入摘要。"""

    def model_dump(self, **kwargs):
        """返回私有canary供安全摘要反例使用，不提供任何验收业务回答。"""
        return {"id": "PRIVATE_RESPONSE_ID", "body": "PRIVATE_RESPONSE_CANARY"}


class APIConnectionError(Exception):
    """合成OpenAI兼容SDK连接错误，仅验证安全类别，不访问网络。"""


def client_for(create):
    """构造原create可观察的最小SDK接口，不发送网络请求。"""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                           with_options=lambda **options: client_for(create))


def test_create_keeps_exact_request_and_response_identity() -> None:
    """摘要前后调用原SDK一次，消息不改、返回对象不替换，观察不能声称远端质量。"""
    received, rows = [], []
    response = SampleResponse()

    def create(**request):
        """记录透传请求，返回同一合成对象。"""
        received.append(request)
        return response

    sdk = observe_client(client_for(create), rows.append)
    messages = [{"role": "user", "content": "PRIVATE_REQUEST_CANARY"}]
    assert sdk.chat.completions.create(messages=messages, model="PRIVATE_MODEL") is response
    assert received == [{"messages": messages, "model": "PRIVATE_MODEL"}]
    assert received[0]["messages"] is messages
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert rows[0]["real_provider_quality"] is False
    assert all(value not in str(rows) for value in
               ["PRIVATE_MODEL", "PRIVATE_REQUEST_CANARY", "PRIVATE_RESPONSE_CANARY"])


def test_with_options_clone_keeps_observation_without_duplicate_dispatch() -> None:
    """原超时副本仍逐create观察，同一对象重复安装不导致双重记录或双发送。"""
    calls, rows = [], []

    def create(**request):
        """计数实际合成方法，验证观察器没有重试。"""
        calls.append(1)
        return SampleResponse()

    sdk = observe_client(client_for(create), rows.append)
    assert observe_client(sdk, rows.append) is sdk
    clone = sdk.with_options(timeout=2)
    clone.chat.completions.create(messages=[])
    assert calls == [1] and len(rows) == 1


def test_sdk_exception_is_not_swallowed_or_replaced() -> None:
    """原SDK异常保持对象身份和调用方重试语义；只记录类型，不泄漏错误原文。"""
    rows = []
    error = RuntimeError("PRIVATE_EXCEPTION_CANARY")

    def create(**request):
        """抛出既定原异常，观察器不得返回合成成功。"""
        raise error

    sdk = observe_client(client_for(create), rows.append)
    with pytest.raises(RuntimeError) as caught:
        sdk.chat.completions.create(messages=[])
    assert caught.value is error
    assert rows[0]["status"] == "failed"
    assert rows[0]["provider_status_code"] is None
    assert rows[0]["provider_error_category"] == "unclassified_provider_error"
    assert "PRIVATE_EXCEPTION_CANARY" not in str(rows)


def test_pre_dispatch_rejection_keeps_request_and_identity_in_receipt() -> None:
    """最终SDK外呼前的预算拒绝也必须留下可关联的请求、PID和本轮身份。"""
    rows = []

    def reject_before(_args, _request):
        """模拟持久预算在最终SDK调用前拒绝，不能伪造供应商响应。"""
        raise PreparationError("F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED")

    sdk = observe_client(
        client_for(lambda **_request: SampleResponse()), rows.append,
        before_create=reject_before,
    )
    with pytest.raises(PreparationError, match="F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED"):
        sdk.chat.completions.create(messages=[{"role": "user", "content": "PRIVATE_PROMPT"}],
                                    model="PRIVATE_MODEL")

    assert len(rows) == 1
    assert isinstance(rows[0].get("pid"), int) and rows[0]["pid"] > 0
    request_sha = rows[0].get("request_sha256")
    assert isinstance(request_sha, str) and len(request_sha) == 64
    assert set(request_sha) <= set("0123456789abcdef")
    assert rows[0]["status"] == "failed"
    assert rows[0]["observation_valid"] is True
    assert rows[0]["identity_sha256"] == {}


@pytest.mark.parametrize(("status", "code", "message", "category"), [
    (400, "InvalidParameter", "tool_choice is unsupported PRIVATE_PROVIDER_CANARY", "unsupported_tool_choice"),
    (400, "InvalidParameter", "bad request PRIVATE_PROVIDER_CANARY", "invalid_request"),
    (429, "RateLimitExceeded", "PRIVATE_PROVIDER_CANARY", "rate_limited"),
    (503, "Unavailable", "PRIVATE_PROVIDER_CANARY", "provider_server_error"),
])
def test_provider_error_classification_is_safe_and_deterministic(
    status: int, code: str, message: str, category: str,
) -> None:
    """仅输出状态和稳定分类，禁止供应商原始消息或代码进入可留存回执。"""
    error = RuntimeError(message)
    error.status_code = status
    error.code = code
    error.body = {"error": {"code": code, "message": message}}

    metadata = _safe_provider_error_metadata(error)

    assert metadata == {"provider_status_code": status, "provider_error_category": category}
    assert "PRIVATE_PROVIDER_CANARY" not in str(metadata)
    assert code not in str(metadata)


def test_provider_error_without_safe_status_or_code_is_unclassified() -> None:
    """未知供应商异常的自由正文只参与进程内分类，不会被返回或记录。"""
    error = RuntimeError("PRIVATE_PROVIDER_CANARY")

    metadata = _safe_provider_error_metadata(error)

    assert metadata == {
        "provider_status_code": None,
        "provider_error_category": "unclassified_provider_error",
    }
    assert "PRIVATE_PROVIDER_CANARY" not in str(metadata)


def test_openai_compatible_connection_error_gets_transport_category() -> None:
    """SDK连接错误只记录固定传输类别，不落供应商连接异常正文。"""
    error = APIConnectionError("PRIVATE_CONNECTION_DETAILS_CANARY")

    metadata = _safe_provider_error_metadata(error)

    assert metadata == {
        "provider_status_code": None,
        "provider_error_category": "provider_transport_error",
        "provider_transport_cause": "unknown_transport_error",
        "provider_os_errno": None,
    }
    assert "PRIVATE_CONNECTION_DETAILS_CANARY" not in str(metadata)


def test_provider_error_metadata_is_persisted_without_changing_original_exception() -> None:
    """SDK观察只附加固定错误分类，并原样抛回供应商异常且不重试。"""
    rows = []
    calls = []
    error = RuntimeError("tool_choice unsupported PRIVATE_PROVIDER_CANARY")
    error.status_code = 400
    error.body = {"error": {"code": "InvalidParameter", "message": str(error)}}

    def create(**request):
        """模拟一次实际SDK异常，保留异常对象身份。"""
        calls.append(request)
        raise error

    sdk = observe_client(client_for(create), rows.append)
    with pytest.raises(RuntimeError) as caught:
        sdk.chat.completions.create(messages=[{"role": "user", "content": "PRIVATE_PROMPT"}])

    assert caught.value is error
    assert len(calls) == len(rows) == 1
    assert rows[0]["provider_status_code"] == 400
    assert rows[0]["provider_error_category"] == "unsupported_tool_choice"
    assert "PRIVATE_PROVIDER_CANARY" not in str(rows)
    assert "InvalidParameter" not in str(rows)
    assert "PRIVATE_PROMPT" not in str(rows)


def test_stream_chunks_are_original_and_close_preserves_cancellation() -> None:
    """原流chunk对象原样返回，提前关闭只记录取消并调用原close，不伪造完成。"""
    rows, closed = [], []
    chunk = SampleResponse()
    class OriginalStream:
        """保持原chunk和关闭记录的合成流。"""

        def __iter__(self):
            """提供两个原对象，模拟提前取消时仍有未消费内容。"""
            return iter([chunk, chunk])

        def close(self):
            """仅记录原流关闭。"""
            closed.append(1)

    sdk = observe_client(client_for(lambda **request: OriginalStream()), rows.append)
    observed = sdk.chat.completions.create(stream=True, messages=[])
    assert next(iter(observed)) is chunk
    observed.close()
    assert closed == [1]
    assert rows[0]["status"] == "cancelled"


def test_fully_consumed_stream_records_completion_once() -> None:
    """只有真正耗尽的原流才记完成，多次close不重复终态账本。"""
    rows, closed = [], []
    chunks = [SampleResponse(), SampleResponse()]

    class OriginalStream:
        """仅用原迭代/关闭边界验证最终观察。"""

        def __iter__(self):
            """透传两个已有chunk，不生成额外对象。"""
            return iter(chunks)

        def close(self):
            """原流资源回收计数。"""
            closed.append(1)

    sdk = observe_client(client_for(lambda **request: OriginalStream()), rows.append)
    observed = sdk.chat.completions.create(stream=True, messages=[])
    assert list(observed) == chunks
    observed.close()
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert closed == [1]


@pytest.mark.parametrize("original_fails", [False, True])
def test_sink_failure_cannot_replace_original_result_or_exception(original_fails: bool) -> None:
    """证据写失败单列无效，不改变原响应/原异常身份或追加SDK重试。"""
    calls = []
    response, error = SampleResponse(), RuntimeError("PRIVATE_SDK_ERROR")

    def create(**request):
        """原SDK只调用一次，分别提供原成功或原异常。"""
        calls.append(1)
        if original_fails:
            raise error
        return response

    def broken_sink(row):
        """模拟证据设备失败；私有路径和异常正文不得进入安全错误。"""
        raise OSError("PRIVATE_SINK_PATH_CANARY")

    sdk = observe_client(client_for(create), broken_sink)
    if original_fails:
        with pytest.raises(RuntimeError) as caught:
            sdk.chat.completions.create(messages=[])
        assert caught.value is error
    else:
        assert sdk.chat.completions.create(messages=[]) is response
    from final_f3_sdk_observer import observation_health
    health = observation_health(sdk)
    assert health["valid"] is False and health["error_count"] == 1
    assert "PRIVATE_SINK_PATH_CANARY" not in str(health)
    assert calls == [1]


@pytest.mark.parametrize("streaming", [False, True])
def test_summary_failure_does_not_discard_original_response_or_chunk(streaming: bool) -> None:
    """SDK对象摘要失败只使证据无效，原回答或流chunk必须继续透传。"""
    rows = []

    class BrokenSummary:
        """原对象可被消费者使用，观察展开单独失败。"""

        def model_dump(self, **kwargs):
            """制造摘要失败，不模拟SDK网络错误。"""
            raise ValueError("PRIVATE_SUMMARY_CANARY")

    response = BrokenSummary()
    sdk = observe_client(client_for(lambda **request: iter([response]) if streaming else response),
                         rows.append)
    output = sdk.chat.completions.create(messages=[], stream=streaming)
    if streaming:
        assert list(output) == [response]
    else:
        assert output is response
    from final_f3_sdk_observer import observation_health
    assert observation_health(sdk)["valid"] is False
    assert rows[0]["observation_valid"] is False
    assert "PRIVATE_SUMMARY_CANARY" not in str(rows)


def test_stream_context_consumer_error_is_failed_not_cancelled() -> None:
    """原with流内消费异常保持原异常及退出行为，旁证区分消费失败和主动取消。"""
    rows, exits = [], []
    error = RuntimeError("PRIVATE_CONSUMER_ERROR")

    class OriginalStream:
        """具备原上下文管理器的合成流，不替换任何业务回答。"""

        def __iter__(self):
            """提供尚未消费完的原流。"""
            return iter([SampleResponse()])

        def __enter__(self):
            """沿原上下文资源入口。"""
            return self

        def __exit__(self, exc_type, exc, traceback):
            """保存原异常身份且不抑制异常。"""
            exits.append(exc)
            return False

    sdk = observe_client(client_for(lambda **request: OriginalStream()), rows.append)
    with pytest.raises(RuntimeError) as caught, sdk.chat.completions.create(stream=True):
        raise error
    assert caught.value is error and exits == [error]
    assert rows[0]["status"] == "failed"


def test_timeout_options_are_forwarded_to_original_clone() -> None:
    """原with_options确实收到timeout及其他配置，返回同一原副本且仍只派发一次。"""
    options, calls, rows = [], [], []
    clone = client_for(lambda **request: calls.append(1) or SampleResponse())
    sdk = client_for(lambda **request: SampleResponse())

    def with_options(**kwargs):
        """记录原配置，不在fake lambda中丢弃timeout。"""
        options.append(kwargs)
        return clone

    sdk.with_options = with_options
    observed = observe_client(sdk, rows.append)
    assert observed.with_options(timeout=2, max_retries=0) is clone
    clone.chat.completions.create(messages=[])
    assert options == [{"timeout": 2, "max_retries": 0}] and calls == [1] and len(rows) == 1


def test_unsettled_stream_is_not_complete_observation() -> None:
    """创建但未消费或关闭的原流保持在途，不能由零记录错误判完整验收。"""
    from final_f3_sdk_observer import observation_health
    rows = []
    sdk = observe_client(client_for(lambda **request: iter([SampleResponse()])), rows.append)
    output = sdk.chat.completions.create(stream=True)
    health = observation_health(sdk)
    assert health["valid"] is False and health["in_flight_count"] == 1
    assert health["started_count"] == 1 and health["settled_count"] == 0
    assert list(output)
    health = observation_health(sdk)
    assert health["valid"] is True and health["in_flight_count"] == 0
    assert health["started_count"] == health["settled_count"] == 1


def test_context_consumer_failure_without_original_exit_keeps_failure_status() -> None:
    """只有close的原流仍能原样抛消费异常，旁证不能被close提前改为取消。"""
    rows = []
    error = RuntimeError("PRIVATE_CONSUMER_NO_EXIT")

    class CloseOnlyStream:
        """原流有关闭能力但没有上下文退出方法。"""

        def __iter__(self):
            """提供未耗尽的原chunk。"""
            return iter([SampleResponse()])

        def close(self):
            """原资源关闭不抛出异常。"""
            return None

    sdk = observe_client(client_for(lambda **request: CloseOnlyStream()), rows.append)
    with pytest.raises(RuntimeError) as caught, sdk.chat.completions.create(stream=True):
        raise error
    assert caught.value is error and rows[0]["status"] == "failed"


def test_original_phase_and_bound_persistent_identity_are_safe_and_scoped() -> None:
    """原llm_operation及显式持久身份绑定到本轮SDK，不泄露ID正文或沿用上轮绑定。"""
    from app.observability.spans import llm_operation
    from final_f3_sdk_identity import bind_sdk_identity
    from final_f3_preparation import digest
    rows = []
    sdk = observe_client(client_for(lambda **request: SampleResponse()), rows.append)
    with bind_sdk_identity(tenant_id="PRIVATE_TENANT", user_id="PRIVATE_USER",
                           agent_id="PRIVATE_AGENT", kind="dynamic_task",
                           source_input_id="PRIVATE_SOURCE", operation_id="PRIVATE_OPERATION",
                           source_sha256="a" * 64), llm_operation("dta.planner"):
        sdk.with_options(timeout=2).chat.completions.create(messages=[])
    sdk.chat.completions.create(messages=[])
    first, second = rows
    assert first["phase_sha256"] == digest("dta.planner")
    assert first["identity_sha256"]["operation_id"] == digest("PRIVATE_OPERATION")
    assert first["kind"] == "dynamic_task" and first["source_sha256"] == "a" * 64
    assert first["pid"] > 0 and first["started_at_utc"].endswith("+00:00")
    assert second["identity_sha256"] == {} and second["kind"] is None
    assert "PRIVATE_OPERATION" not in str(rows) and "PRIVATE_TENANT" not in str(rows)


@pytest.mark.parametrize("streaming", [False, True])
def test_original_worker_threads_keep_observation_identity_without_changing_model_context(
    streaming: bool,
) -> None:
    """只将旁证身份传入原取消/流线程；原模型阶段ContextVar和实际SDK对象保持。"""
    from app.llm import client as llm_module
    from app.observability.spans import current_llm_operation, llm_operation
    from final_f3_preparation import digest
    from final_f3_sdk_handoff import sdk_thread_handoff
    from final_f3_sdk_identity import bind_sdk_identity
    rows, original_phases = [], []
    response = SampleResponse()

    class OriginalStream:
        """只供原生产线程透传chunk与关闭，不返回预置业务成功。"""

        def __iter__(self):
            """返回同一原测试chunk，验证原迭代对象身份。"""
            return iter([response])

        def close(self):
            """保留原流资源边界。"""
            return None

    def create(**request):
        """保存原SDK线程阶段，确保旁证handoff没有改变模型请求选择语义。"""
        original_phases.append(current_llm_operation())
        return OriginalStream() if streaming else response

    sdk = observe_client(client_for(create), rows.append)
    with sdk_thread_handoff(llm_module), bind_sdk_identity(
        operation_id="PRIVATE_THREAD_OPERATION", kind="dynamic_task",
    ), llm_operation("dta.planner"):
        if streaming:
            chunks = list(llm_module._interruptible_provider_stream(
                lambda: sdk.chat.completions.create(stream=True), lambda: False,
            ))
            assert chunks == [response]
        else:
            output = llm_module.run_cancellable(
                lambda: sdk.chat.completions.create(messages=[]), is_cancelled=lambda: False,
            )
            assert output is response
    expected_worker_phase = "llm.request" if streaming else "dta.planner"
    assert original_phases == [expected_worker_phase]
    assert rows[0]["phase_sha256"] == digest("dta.planner")
    assert rows[0]["sdk_thread_phase_sha256"] == digest(expected_worker_phase)
    assert rows[0]["identity_sha256"]["operation_id"] == digest("PRIVATE_THREAD_OPERATION")
    assert rows[0]["phase_source"] == "original_caller_observation_handoff"
