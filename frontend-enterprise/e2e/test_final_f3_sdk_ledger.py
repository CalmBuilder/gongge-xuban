"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : test_final_f3_sdk_ledger.py
@CallChain  : pytest → 原SDK透传观察 → 私有旁证短写/失效/并发 → 整轮健康
@Description: 验证SDK旁证与跨重启派发预算；合成SDK不计真实质量或远端旅程。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from final_f3_preparation import PreparationError, digest
from final_f3_sdk_ledger import SdkLedger
from final_f3_sdk_observer import observation_health


class SampleResponse:
    """只有摘要用的原对象，测试不能把其正文当实际业务效果。"""

    def model_dump(self, **kwargs):
        """原对象包含私有canary，落盘只能保留摘要。"""
        return {"id": "PRIVATE_LEDGER_RESPONSE", "body": "PRIVATE_LEDGER_CANARY"}


def original_client(create, list_models=None):
    """提供同一原create和原timeout副本接口，绝不发送网络。"""
    list_models = list_models or (lambda **params: SimpleNamespace(data=[]))
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=list_models),
        with_options=lambda **options: original_client(create, list_models),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> SdkLedger:
    """每例使用pytest自有700父目录，不写已有归档、配置或数据库。"""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return SdkLedger(private / "sdk.safe.jsonl")


def read_rows(ledger: SdkLedger) -> list[dict]:
    """读取本例实际文件核JSON行完整性，不用输出替身判完整。"""
    return [json.loads(line) for line in ledger.path.read_text().splitlines()]


def test_short_write_is_completed_before_observation_settles(ledger, monkeypatch) -> None:
    """真实os.write只写前7字节时必须续写，原SDK仍一次且保留返回身份。"""
    original_write = os.write
    writes, sdk_calls = [], []

    def short_write(descriptor, data):
        """只截实际每次写入长度，文件原字节由原os.write产生。"""
        writes.append(len(data))
        return original_write(descriptor, data[:7])

    monkeypatch.setattr(os, "write", short_write)
    response = SampleResponse()
    client = ledger.observe(original_client(lambda **request: sdk_calls.append(1) or response))
    assert client.chat.completions.create(messages=[]) is response
    assert len(read_rows(ledger)) == 1 and len(writes) > 1
    assert ledger.health()["valid"] is True and sdk_calls == [1]
    assert "PRIVATE_LEDGER_CANARY" not in ledger.path.read_text()


def test_interrupted_write_retries_only_receipt_not_sdk(ledger, monkeypatch) -> None:
    """系统写中断只重试文件写，不能吞掉整行或再次调用SDK。"""
    original_write = os.write
    writes = []

    def interrupted(descriptor, data):
        """第一次抛原系统中断，第二次沿原文件写入。"""
        writes.append(1)
        if len(writes) == 1:
            raise InterruptedError("PRIVATE_FILE_INTERRUPT")
        return original_write(descriptor, data)

    monkeypatch.setattr(os, "write", interrupted)
    client = ledger.observe(original_client(lambda **request: SampleResponse()))
    client.chat.completions.create()
    assert ledger.health()["valid"] is True and len(read_rows(ledger)) == 1
    assert len(writes) == 2


@pytest.mark.parametrize("failure", ["zero", "disk_full", "fsync"])
def test_failed_write_invalidates_whole_ledger_but_preserves_sdk(
    ledger, monkeypatch, failure,
) -> None:
    """零写、磁盘故障或同步失败都必须闭合为证据无效，原SDK响应仍原样透传。"""
    response = SampleResponse()

    def failed(*args):
        """抛私有设备故障；公开health不得包含异常正文。"""
        raise OSError("PRIVATE_DISK_PATH_CANARY")

    if failure == "zero":
        monkeypatch.setattr(os, "write", lambda *args: 0)
    else:
        monkeypatch.setattr(os, "fsync" if failure == "fsync" else "write", failed)
    client = ledger.observe(original_client(lambda **request: response))
    assert client.chat.completions.create() is response
    assert observation_health(client)["valid"] is False
    health = ledger.health()
    assert health["valid"] is False and "PRIVATE_DISK_PATH_CANARY" not in str(health)


def test_receipt_write_failure_blocks_followup_model_dispatch(tmp_path, monkeypatch):
    """原SDK回执持久化失败后保留本次原响应，并在下一次模型外呼前关闭账本。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "receipt-write-failure-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("F3_MODEL_BUDGET_PROFILE", "i2-sop-first-three-v1")
    sdk_calls = []

    def fail_receipt(*_args, **_kwargs):
        """模拟账本设备写故障，原SDK请求仍只由观察器调用一次。"""
        raise OSError("SYNTHETIC_RECEIPT_DEVICE_FAILURE")

    monkeypatch.setattr(ledger_module, "append_private_jsonl_bytes", fail_receipt)
    ledger = SdkLedger(runtime / "sdk.safe.jsonl")
    client = ledger.observe(original_client(
        lambda **_request: sdk_calls.append(1) or SampleResponse(),
    ))

    assert isinstance(client.chat.completions.create(messages=[], max_tokens=1), SampleResponse)
    with pytest.raises(PreparationError, match="F3_SDK_LEDGER_INVALID_BLOCKING_DISPATCH"):
        client.chat.completions.create(messages=[], max_tokens=1)

    assert sdk_calls == [1]
    assert ledger.health()["valid"] is False


def test_all_clients_and_inflight_streams_are_covered(ledger) -> None:
    """新client和原timeout副本全部计数，在途流不可被剩余完整行掩盖。"""
    first = ledger.observe(original_client(lambda **request: SampleResponse()))
    second = ledger.observe(original_client(lambda **request: iter([SampleResponse()])))
    first.with_options(timeout=2).chat.completions.create()
    stream = second.chat.completions.create(stream=True)
    health = ledger.health()
    assert health["valid"] is False and health["client_state_count"] == 2
    assert health["started_count"] == 2 and health["in_flight_count"] == 1
    assert list(stream)
    assert ledger.health()["valid"] is True and len(read_rows(ledger)) == 2


def test_repeated_install_and_timeout_clone_do_not_double_count(ledger) -> None:
    """同一client或共享状态的原timeout副本重安装，不得重复统计已有SDK。"""
    original = original_client(lambda **request: SampleResponse())
    client = ledger.observe(original)
    assert ledger.observe(client) is client
    clone = ledger.observe(client.with_options(timeout=2))
    clone.chat.completions.create()
    health = ledger.health()
    assert health["valid"] is True and health["client_state_count"] == 1
    assert health["started_count"] == health["settled_count"] == 1


def test_provider_rejection_receipt_stores_only_safe_status_and_category(ledger) -> None:
    """供应商拒绝保留原异常语义，实际文件只记录固定状态类别且旁证仍完整。"""
    error = RuntimeError("tool_choice unsupported PRIVATE_PROVIDER_CANARY")
    error.status_code = 400
    error.body = {"error": {"code": "InvalidParameter", "message": str(error)}}

    def reject(**request):
        """模拟原SDK一次供应商拒绝，不能返回合成成功对象。"""
        raise error

    client = ledger.observe(original_client(reject))
    with pytest.raises(RuntimeError) as caught:
        client.chat.completions.create(messages=[{"role": "user", "content": "PRIVATE_PROMPT"}])

    assert caught.value is error
    rows = read_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["provider_status_code"] == 400
    assert rows[0]["provider_error_category"] == "unsupported_tool_choice"
    assert "PRIVATE_PROVIDER_CANARY" not in ledger.path.read_text()
    assert "InvalidParameter" not in ledger.path.read_text()
    assert "PRIVATE_PROMPT" not in ledger.path.read_text()
    assert ledger.health()["valid"] is True


def test_provider_connection_error_receipt_is_transport_only(ledger) -> None:
    """OpenAI兼容连接异常保留原对象，只把稳定传输类别写入私有账本。"""
    class APIConnectionError(Exception):
        """模拟SDK传输错误，不持有任何真实请求内容。"""

    error = APIConnectionError("PRIVATE_CONNECTION_DETAILS_CANARY")

    def reject(**request):
        """模拟原SDK在连接阶段抛错。"""
        raise error

    client = ledger.observe(original_client(reject))
    with pytest.raises(APIConnectionError) as caught:
        client.chat.completions.create(messages=[])

    assert caught.value is error
    row = read_rows(ledger)[0]
    assert row["exception_type"] == "APIConnectionError"
    assert row["provider_status_code"] is None
    assert row["provider_error_category"] == "provider_transport_error"
    assert "PRIVATE_CONNECTION_DETAILS_CANARY" not in ledger.path.read_text()
    assert ledger.health()["valid"] is True


def test_sdk_receipt_row_budget_is_shared_across_pid_ledgers(tmp_path, monkeypatch):
    """进程重启后新PID文件仍受运行级SDK与memory-wire合计行预算约束。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "shared-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(ledger_module, "MAX_SDK_LEDGER_ROWS", 1, raising=False)
    first = SdkLedger(runtime / "sdk-observations-1001.safe.jsonl")
    second = SdkLedger(runtime / "sdk-observations-1002.safe.jsonl")
    receipt = {"call_id": "f3_sdk_" + "1" * 32, "observation_valid": True,
               "request_sha256": "a" * 64, "model_sha256": None,
               "observation_layer": "final_sdk_create", "request_class": "chat_completion",
               "real_provider_quality": False, "provider_identity_qualified_by_observer": False,
               "status": "failed", "exception_type": "RuntimeError", "duration_ms": 0.0}
    first(receipt)
    second_receipt = {**receipt, "call_id": "f3_sdk_" + "2" * 32}
    with pytest.raises(PreparationError, match="F3_SDK_RECEIPT_ROW_LIMIT"):
        second(second_receipt)
    assert sum(len(path.read_text().splitlines()) for path in runtime.glob("sdk-observations-*.safe.jsonl")) == 1


def test_sdk_receipt_file_budget_blocks_new_pid_at_exact_limit(tmp_path, monkeypatch):
    """运行级观察文件达到上限时，新PID不能再创建第N+1个账本文件。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "shared-file-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(ledger_module, "MAX_SDK_LEDGER_FILES", 1, raising=False)
    first = SdkLedger(runtime / "sdk-observations-1001.safe.jsonl")
    second = SdkLedger(runtime / "sdk-observations-1002.safe.jsonl")
    receipt = {"call_id": "f3_sdk_" + "1" * 32, "observation_valid": True,
               "request_sha256": "a" * 64, "model_sha256": None,
               "observation_layer": "final_sdk_create", "request_class": "chat_completion",
               "real_provider_quality": False, "provider_identity_qualified_by_observer": False,
               "status": "failed", "exception_type": "RuntimeError", "duration_ms": 0.0}
    first(receipt)
    first({**receipt, "call_id": "f3_sdk_" + "3" * 32})

    with pytest.raises(PreparationError, match="F3_SDK_RECEIPT_FILE_LIMIT"):
        second({**receipt, "call_id": "f3_sdk_" + "2" * 32})

    assert len(list(runtime.glob("sdk-observations-*.safe.jsonl"))) == 1
    assert len(first.path.read_text().splitlines()) == 2


def test_sdk_ledger_accepts_hash_only_model_diagnostic_separate_from_business_identity(ledger):
    """原SDK账本可写独立预检scope，但不混入Composer身份或任意诊断正文。"""
    diagnostic_scope = {
        "schema": "model-preflight-v1",
        "invocation_sha256": "1" * 64,
        "tenant_sha256": "2" * 64,
        "admin_actor_sha256": "3" * 64,
        "model_config_sha256": "4" * 64,
        "config_fingerprint_sha256": "5" * 64,
        "probe_class": "native_capabilities",
    }
    receipt = {
        "call_id": "f3_sdk_" + "6" * 32,
        "observation_valid": True,
        "request_sha256": "7" * 64,
        "model_sha256": "8" * 64,
        "observation_layer": "final_sdk_create",
        "request_class": "structured_output",
        "real_provider_quality": False,
        "provider_identity_qualified_by_observer": False,
        "pid": os.getpid(),
        "started_at_utc": "2026-10-13T10:11:12+00:00",
        "identity_sha256": {},
        "diagnostic_scope": diagnostic_scope,
        "status": "completed",
        "duration_ms": 1.0,
    }

    ledger(receipt)

    rows = read_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["identity_sha256"] == {}
    assert rows[0]["diagnostic_scope"] == diagnostic_scope


def test_sdk_ledger_rejects_unbounded_model_diagnostic_scope_field(ledger):
    """模型诊断账本拒绝任意新增字段，避免端点、提示或凭据进入安全旁证。"""
    diagnostic_scope = {
        "schema": "model-preflight-v1",
        "invocation_sha256": "1" * 64,
        "tenant_sha256": "2" * 64,
        "admin_actor_sha256": "3" * 64,
        "model_config_sha256": "4" * 64,
        "config_fingerprint_sha256": "5" * 64,
        "probe_class": "model_connection",
        "private_request": "PRIVATE_MODEL_PROMPT_CANARY",
    }
    receipt = {
        "call_id": "f3_sdk_" + "7" * 32,
        "observation_valid": True,
        "request_sha256": "8" * 64,
        "model_sha256": "9" * 64,
        "observation_layer": "final_sdk_create",
        "request_class": "chat_completion",
        "real_provider_quality": False,
        "provider_identity_qualified_by_observer": False,
        "identity_sha256": {},
        "diagnostic_scope": diagnostic_scope,
    }

    with pytest.raises(PreparationError, match="F3_SDK_RECEIPT_SCHEMA_INVALID"):
        ledger(receipt)

    assert ledger.health()["write_failed"] is True
    assert not ledger.path.exists()


@pytest.mark.parametrize(("maximum_calls", "maximum_completion_tokens", "expected_error"), [
    (1, 8192, "F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED"),
    (64, 1, "F3_MODEL_DISPATCH_TOKEN_BUDGET_EXHAUSTED"),
])
def test_model_dispatch_budget_is_persisted_before_sdk_across_restart(
    tmp_path, monkeypatch, maximum_calls, maximum_completion_tokens, expected_error,
):
    """冻结诊断先持久预留实际SDK调用与completion上限，重启后超额调用零网络派发。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "model-budget-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("F3_MODEL_BUDGET_PROFILE", "memory-dta-freeze-v1")
    monkeypatch.setattr(ledger_module, "MAX_MODEL_INVOCATIONS", maximum_calls, raising=False)
    monkeypatch.setattr(ledger_module, "MAX_MODEL_COMPLETION_TOKENS", maximum_completion_tokens, raising=False)
    first_calls, second_calls = [], []
    first = SdkLedger(runtime / "sdk-observations-1001.safe.jsonl")
    first_client = first.observe(original_client(
        lambda **_request: first_calls.append(1) or SampleResponse(),
    ))
    first_client.chat.completions.create(
        messages=[{"role": "user", "content": "synthetic prompt"}], max_tokens=1,
    )
    first_budget = first.health()["model_dispatch_budget"]
    assert first_budget["valid"] is True and first_budget["calls"] == 1
    assert first_budget["completion_tokens"] == 1
    assert first_budget["request_metadata_fields_included"] is True
    assert first_budget["maximum_model_response_body_bytes"] == 8 * 1024 * 1024
    assert first_budget["model_response_body_limit_enforced_by_backend_client"] is True
    first_receipt = read_rows(first)[0]
    assert first_receipt["model_budget_profile"] == "memory-dta-freeze-v1"
    assert first_receipt["completion_tokens_reserved"] == 1
    assert first_receipt["prompt_tokens_reserved"] > 0
    restarted = SdkLedger(runtime / "sdk-observations-1002.safe.jsonl")
    restarted_client = restarted.observe(original_client(
        lambda **_request: second_calls.append(1) or SampleResponse(),
    ))
    with pytest.raises(PreparationError, match=expected_error):
        restarted_client.chat.completions.create(
            messages=[{"role": "user", "content": "synthetic prompt"}], max_tokens=1,
        )
    assert first_calls == [1] and second_calls == []


def test_sop_first_three_model_budget_is_persisted_and_blocks_overage(tmp_path, monkeypatch):
    """JRN08三案共享的持久调用额度耗尽后不得再次派发原SDK。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "sop-model-budget-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("F3_MODEL_BUDGET_PROFILE", "i2-sop-first-three-v1")
    monkeypatch.setattr(ledger_module, "MAX_SOP_FIRST_THREE_MODEL_INVOCATIONS", 1, raising=False)
    first_calls, second_calls = [], []
    first = SdkLedger(runtime / "sdk-observations-1001.safe.jsonl")
    first_client = first.observe(original_client(
        lambda **_request: first_calls.append(1) or SampleResponse(),
    ))
    first_client.chat.completions.create(
        messages=[{"role": "user", "content": "synthetic prompt"}], max_tokens=1,
    )
    receipt = read_rows(first)[0]
    assert receipt["model_budget_profile"] == "i2-sop-first-three-v1"
    assert first.health()["model_dispatch_budget"]["calls"] == 1

    restarted = SdkLedger(runtime / "sdk-observations-1002.safe.jsonl")
    restarted_client = restarted.observe(original_client(
        lambda **_request: second_calls.append(1) or SampleResponse(),
    ))
    with pytest.raises(PreparationError, match="F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED"):
        restarted_client.chat.completions.create(
            messages=[{"role": "user", "content": "synthetic prompt"}], max_tokens=1,
        )
    assert first_calls == [1] and second_calls == []
    assert restarted.health()["model_dispatch_budget"]["calls"] == 1


@pytest.mark.parametrize("request_field", ["tools", "tool_choice", "response_format"])
def test_model_request_token_reservation_counts_sdk_metadata(request_field: str) -> None:
    """SDK预算估值除messages外还须覆盖工具和结构化响应元数据。"""
    import final_f3_sdk_ledger as ledger_module

    base_request = {
        "model": "synthetic-model",
        "messages": [{"role": "user", "content": "synthetic prompt"}],
        "max_tokens": 32,
    }
    request = dict(base_request)
    if request_field == "tools":
        request[request_field] = [{
            "type": "function",
            "function": {"name": "synthetic_lookup", "description": "x" * 512},
        }]
    elif request_field == "tool_choice":
        request[request_field] = {"type": "function", "function": {"name": "synthetic_lookup"}}
    else:
        request[request_field] = {
            "type": "json_schema",
            "json_schema": {"name": "synthetic_result", "schema": {"description": "x" * 512}},
        }

    base_tokens, _ = ledger_module._model_request_token_reservation((), base_request)
    metadata_tokens, _ = ledger_module._model_request_token_reservation((), request)

    assert metadata_tokens > base_tokens


def test_model_budget_state_deletion_after_use_fails_closed(tmp_path):
    """已初始化预算状态被删除后必须拒绝新SDK派发，不能重新获得零额度。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "deleted-model-budget-runtime"
    runtime.mkdir(mode=0o700)
    limits = ledger_module._model_budget_limits("i2-sop-first-three-v1")
    assert limits is not None
    first = ledger_module._PersistentModelDispatchBudget(
        runtime, "i2-sop-first-three-v1", limits,
    )
    first.reserve(1, 1)
    first.path.unlink()

    restarted = ledger_module._PersistentModelDispatchBudget(
        runtime, "i2-sop-first-three-v1", limits,
    )
    with pytest.raises(PreparationError, match="F3_MODEL_DISPATCH_BUDGET_STATE_MISSING"):
        restarted.reserve(1, 1)


def test_sop_first_three_budget_covers_registered_three_case_group() -> None:
    """三案诊断的共享重启预算须覆盖已测上界，同时仍低于全局安全上限。"""
    import final_f3_sdk_ledger as ledger_module

    limits = ledger_module._model_budget_limits("i2-sop-first-three-v1")
    assert limits is not None
    assert limits["calls"] == 48
    assert limits["calls"] <= ledger_module.MAX_MODEL_INVOCATIONS


@pytest.mark.parametrize(("limit_name", "limit_value", "request_tokens"), [
    ("MAX_SOP_FIRST_THREE_PROMPT_TOKENS", 1, 1),
    ("MAX_SOP_FIRST_THREE_COMPLETION_TOKENS", 1, 2),
])
def test_sop_first_three_token_budget_refuses_sdk_before_network(
        tmp_path, monkeypatch, limit_name, limit_value, request_tokens):
    """累计输入或输出额度耗尽时在最终SDK调用前稳定拒绝且预算不可伪造成功。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "sop-token-budget-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("F3_MODEL_BUDGET_PROFILE", "i2-sop-first-three-v1")
    monkeypatch.setattr(ledger_module, limit_name, limit_value, raising=False)
    sdk_calls = []
    ledger = SdkLedger(runtime / "sdk-observations-1001.safe.jsonl")
    client = ledger.observe(original_client(
        lambda **_request: sdk_calls.append(1) or SampleResponse(),
    ))

    with pytest.raises(PreparationError, match="F3_MODEL_DISPATCH_TOKEN_BUDGET_EXHAUSTED"):
        client.chat.completions.create(
            messages=[{"role": "user", "content": "synthetic prompt"}],
            max_tokens=request_tokens,
        )

    assert sdk_calls == []
    assert ledger.health()["model_dispatch_budget"]["calls"] == 0
    assert ledger.health()["valid"] is False


def test_concurrent_model_dispatches_share_one_atomic_call_reservation(tmp_path, monkeypatch):
    """并发SDK client争抢最后一个额度时恰有一个原SDK被调用。"""
    import final_f3_sdk_ledger as ledger_module

    runtime = tmp_path / "concurrent-model-budget-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("F3_MODEL_BUDGET_PROFILE", "memory-dta-freeze-v1")
    monkeypatch.setattr(ledger_module, "MAX_MODEL_INVOCATIONS", 1, raising=False)
    calls = []
    clients = []
    for pid in (2001, 2002):
        ledger = SdkLedger(runtime / f"sdk-observations-{pid}.safe.jsonl")
        clients.append(ledger.observe(original_client(
            lambda **_request: calls.append(1) or SampleResponse(),
        )))

    def dispatch(client):
        """每线程只提出一份相同的短请求，返回成功或稳定预算拒绝类别。"""
        try:
            client.chat.completions.create(
                messages=[{"role": "user", "content": "synthetic"}], max_tokens=1,
            )
            return "sent"
        except PreparationError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, clients))
    assert results.count("sent") == 1
    assert results.count("F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED") == 1
    assert calls == [1]


def test_transport_receipt_distinguishes_allowlisted_underlying_cause(ledger) -> None:
    """仅把传输异常链归为固定类别及系统errno，原消息和地址不能入账。"""
    class APIConnectionError(Exception):
        """模拟SDK外层连接异常。"""

    error = APIConnectionError("PRIVATE_PROXY_URL_AND_TOKEN_CANARY")
    cause = OSError(errno.ECONNREFUSED, "PRIVATE_SOCKET_PATH_CANARY")

    def reject(**request):
        """保留外层SDK异常身份并链接底层系统拒绝连接。"""
        raise error from cause

    client = ledger.observe(original_client(reject))
    with pytest.raises(APIConnectionError) as caught:
        client.chat.completions.create(messages=[])

    assert caught.value is error
    receipt = read_rows(ledger)[0]
    assert receipt["provider_transport_cause"] == "connection_refused"
    assert receipt["provider_os_errno"] == errno.ECONNREFUSED
    assert "PRIVATE_PROXY_URL_AND_TOKEN_CANARY" not in ledger.path.read_text()
    assert "PRIVATE_SOCKET_PATH_CANARY" not in ledger.path.read_text()
    assert ledger.health()["valid"] is True


@pytest.mark.parametrize(("sdk_request", "expected"), [
    ({"messages": [{"role": "user", "content": "PRIVATE_PROMPT_CANARY"}]}, "chat_completion"),
    ({"messages": [], "response_format": {"type": "json_object"}}, "structured_output"),
    ({"messages": [], "tools": [{"type": "function"}]}, "tool_call"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "private"}}]}]}, "vision_input"),
    ({"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_data": "private"}}]}]}, "file_input"),
])
def test_sdk_receipt_records_only_finite_request_shape(ledger, sdk_request, expected) -> None:
    """只依据结构投影有限请求类别，不落模型输入、文件地址或图片内容。"""
    client = ledger.observe(original_client(lambda **params: SampleResponse()))
    client.chat.completions.create(**sdk_request)
    receipt = read_rows(ledger)[0]
    assert receipt["request_class"] == expected
    assert "PRIVATE_PROMPT_CANARY" not in ledger.path.read_text()
    assert "private" not in ledger.path.read_text()
    assert ledger.health()["valid"] is True


def test_model_catalog_observation_records_count_without_model_identifiers(ledger) -> None:
    """模型目录读取被纳入同一SDK健康计数，只保留列表条数和状态。"""
    models = SimpleNamespace(data=[SimpleNamespace(id="PRIVATE_CATALOG_MODEL_CANARY")])
    calls = []
    client = ledger.observe(original_client(
        lambda **request: SampleResponse(),
        lambda **params: calls.append(params) or models,
    ))

    assert client.models.list() is models
    receipt = read_rows(ledger)[0]
    assert receipt["request_class"] == "model_catalog"
    assert receipt["request_sha256"] == digest({"args": (), "request": {}})
    assert receipt["catalog_count"] == 1
    assert receipt["status"] == "completed"
    assert "PRIVATE_CATALOG_MODEL_CANARY" not in ledger.path.read_text()
    assert ledger.health()["started_count"] == ledger.health()["settled_count"] == 1
    assert ledger.health()["valid"] is True and calls == [{}]


def test_model_catalog_transport_failure_is_observed_and_transparent(ledger) -> None:
    """目录请求失败也写安全阶段回执，保留原异常对象且不漏出目录内容。"""
    class APIConnectionError(Exception):
        """模拟模型目录传输失败。"""

    error = APIConnectionError("PRIVATE_CATALOG_ENDPOINT_CANARY")
    cause = OSError(errno.ETIMEDOUT, "PRIVATE_HOST_CANARY")

    def reject(**params):
        """一次性抛出原目录异常。"""
        raise error from cause

    client = ledger.observe(original_client(lambda **request: SampleResponse(), reject))
    with pytest.raises(APIConnectionError) as caught:
        client.models.list()

    assert caught.value is error
    receipt = read_rows(ledger)[0]
    assert receipt["request_class"] == "model_catalog"
    assert receipt["status"] == "failed"
    assert receipt["provider_transport_cause"] == "timeout"
    assert receipt["provider_os_errno"] == errno.ETIMEDOUT
    assert "PRIVATE_CATALOG_ENDPOINT_CANARY" not in ledger.path.read_text()
    assert "PRIVATE_HOST_CANARY" not in ledger.path.read_text()
    assert ledger.health()["valid"] is True


def test_openai_sdk_model_list_and_completion_share_observation_state(ledger) -> None:
    """原OpenAI SDK同步资源可被包裹，目录与生成共用账本且不访问网络。"""
    import httpx
    from openai import OpenAI

    def respond(request):
        """仅通过进程内HTTPX传输返回合成目录/完成响应，不接触供应商。"""
        if request.url.path.endswith("/models"):
            payload = {"object": "list", "data": [{"id": "PRIVATE_MODEL_NAME_CANARY", "object": "model"}]}
        else:
            payload = {
                "id": "PRIVATE_RESPONSE_ID_CANARY", "object": "chat.completion", "created": 1,
                "model": "synthetic-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "synthetic reply"},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(respond)
    http_client = httpx.Client(transport=transport)
    client = OpenAI(api_key="TEST_ONLY_NOT_A_CREDENTIAL", base_url="http://127.0.0.1/v1",
                    http_client=http_client)
    observed = ledger.observe(client)
    try:
        assert len(observed.models.list().data) == 1
        observed.chat.completions.create(model="synthetic-model", messages=[])
    finally:
        observed.close()

    rows = read_rows(ledger)
    assert [row["request_class"] for row in rows] == ["model_catalog", "chat_completion"]
    assert rows[0]["catalog_count"] == 1
    assert ledger.health()["valid"] is True
    assert "PRIVATE_MODEL_NAME_CANARY" not in ledger.path.read_text()
    assert "PRIVATE_RESPONSE_ID_CANARY" not in ledger.path.read_text()


@pytest.mark.parametrize("damage", ["truncate", "delete", "duplicate"])
def test_current_file_damage_cannot_pass_from_client_counters_alone(ledger, damage) -> None:
    """完成后文件截断、缺失或重复行须由独立实际读回发现，不能只信观察器计数。"""
    client = ledger.observe(original_client(lambda **request: SampleResponse()))
    client.chat.completions.create()
    assert ledger.health()["valid"] is True
    data = ledger.path.read_bytes()
    if damage == "delete":
        ledger.path.unlink()
    else:
        ledger.path.write_bytes(data[:13] if damage == "truncate" else data + data)
    assert ledger.health()["valid"] is False


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "public_file"])
def test_output_replacement_is_refused_without_touching_target(
    ledger, replacement,
) -> None:
    """旁证链接或公开文件不能被追加，禁止写入自有根之外的替身目标。"""
    outside = ledger.path.parent.parent / "outside.txt"
    outside.write_bytes(b"UNTOUCHED_PRIVATE_CANARY")
    outside.chmod(0o600)
    if replacement == "symlink":
        ledger.path.symlink_to(outside)
    elif replacement == "hardlink":
        os.link(outside, ledger.path)
    else:
        ledger.path.write_bytes(b"UNTOUCHED_PUBLIC_FILE")
        ledger.path.chmod(0o644)
    prior = ledger.path.read_bytes()
    client = ledger.observe(original_client(lambda **request: SampleResponse()))
    client.chat.completions.create()
    assert ledger.health()["valid"] is False
    assert outside.read_bytes() == b"UNTOUCHED_PRIVATE_CANARY"
    assert ledger.path.read_bytes() == prior


def test_concurrent_short_writes_are_complete_noninterleaved_rows(ledger, monkeypatch) -> None:
    """原工作线程并发结算时只串行旁证字节，不锁SDK外呼或改变原模型并发。"""
    import time
    original_write = os.write

    def short_write(descriptor, data):
        """扩大短写交错窗口，由原os.write写真实文件。"""
        written = original_write(descriptor, data[:11])
        time.sleep(0.0001)
        return written

    monkeypatch.setattr(os, "write", short_write)
    clients = [ledger.observe(original_client(lambda **request: SampleResponse())) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda client: client.chat.completions.create(), clients))
    assert len(outputs) == len(read_rows(ledger)) == 4
    assert ledger.health()["valid"] is True
    assert ledger.path.stat().st_mode & 0o077 == 0


def test_extra_private_fields_are_never_serialized(ledger) -> None:
    """写入契约外自由字段时在序列化前拒绝，私有请求或错误正文不得落公共形状旁证。"""
    with pytest.raises(PreparationError, match="F3_SDK_RECEIPT_SCHEMA_INVALID"):
        ledger({"call_id": "f3_sdk_" + "a" * 32, "secret": "PRIVATE_UNEXPECTED_FIELD"})
    assert not ledger.path.exists() or "PRIVATE_UNEXPECTED_FIELD" not in ledger.path.read_text()


def test_empty_ledger_never_claims_business_or_observation_pass(ledger) -> None:
    """没有任何原SDK客户端及记录时只能待验，不能由空计数宣称完整观察。"""
    health = ledger.health()
    assert health["valid"] is False and health["JRN08_passed"] is False


@pytest.mark.parametrize("field,value", [
    ("usage", {"prompt_tokens": 1, "private_body": "PRIVATE_NESTED_CANARY"}),
    ("identity_sha256", {"user_id": "PRIVATE_UNHASHED_ID_CANARY"}),
    ("response_sha256", "PRIVATE_UNHASHED_RESPONSE_CANARY"),
    ("exception_type", "PRIVATE_PATH/AND_ERROR_CANARY"),
])
def test_safe_field_names_cannot_smuggle_private_values(ledger, field, value) -> None:
    """白名单字段仍需收窄值和嵌套结构，不能靠安全键名保存未摘要私密内容。"""
    client = ledger.observe(original_client(lambda **request: SampleResponse()))
    client.chat.completions.create()
    receipt = read_rows(ledger)[0]
    receipt.update(call_id="f3_sdk_" + "b" * 32)
    receipt[field] = value
    prior = ledger.path.read_bytes()
    with pytest.raises(PreparationError, match="F3_SDK_RECEIPT_SCHEMA_INVALID"):
        ledger(receipt)
    assert ledger.path.read_bytes() == prior


def test_ledger_refuses_client_observed_by_a_different_sink(ledger) -> None:
    """原客户端已有另一个旁证sink时不能冒领其健康为本文件覆盖，避免静默缺行。"""
    from final_f3_sdk_observer import observe_client
    rows = []
    client = observe_client(original_client(lambda **request: SampleResponse()), rows.append)
    with pytest.raises(PreparationError, match="F3_SDK_CLIENT_ALREADY_OBSERVED_ELSEWHERE"):
        ledger.observe(client)
    assert ledger.health()["valid"] is False and not ledger.path.exists()
