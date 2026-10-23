"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : final_f3_sdk_observer.py
@CallChain  : F3自有测试服务 → 最终SDK models.list/create/with_options → 安全旁证
@Description: 透传原目录、响应及异常；观察故障单列证据无效，不改变原重试或远端效果。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import errno
import hashlib
import re
import socket
import ssl
import time
from uuid import uuid4

from final_f3_preparation import digest, safe_sdk_receipt
from final_f3_sdk_identity import sdk_identity

_TRANSPORT_ERRNOS = {
    errno.ECONNREFUSED: "connection_refused",
    errno.ETIMEDOUT: "timeout",
    errno.ECONNRESET: "connection_reset",
    errno.EHOSTUNREACH: "host_unreachable",
    errno.ENETUNREACH: "network_unreachable",
    errno.EPIPE: "connection_reset",
    socket.EAI_AGAIN: "dns_temporary_failure",
    socket.EAI_NONAME: "dns_name_not_found",
}


def _safe_transport_cause(error: Exception) -> dict[str, object]:
    """遍历有界异常链，仅投影固定传输类别和平台白名单errno。"""
    current: BaseException | None = error
    seen: set[int] = set()
    category = "unknown_transport_error"
    safe_errno: int | None = None
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        raw_errno = getattr(current, "errno", None)
        if type(raw_errno) is int and raw_errno in _TRANSPORT_ERRNOS:
            category = _TRANSPORT_ERRNOS[raw_errno]
            safe_errno = raw_errno
            break
        if isinstance(current, (TimeoutError,)) or name in {
            "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "APITimeoutError",
        }:
            category = "timeout"
            break
        if isinstance(current, ssl.SSLError) or name in {"SSLError", "TLSHandshakeError"}:
            category = "tls_error"
            break
        if name in {"RemoteProtocolError", "ProtocolError"}:
            category = "remote_protocol_error"
            break
        if name in {"gaierror", "ResolverError"}:
            category = "dns_error"
            break
        current = current.__cause__ or current.__context__
    return {"provider_transport_cause": category, "provider_os_errno": safe_errno}


def _request_class(request: dict[str, object]) -> str:
    """仅查看有限SDK结构键，按请求能力分类而不读取或复制正文值。"""
    if request.get("response_format") is not None:
        return "structured_output"
    if request.get("tools") is not None or request.get("tool_choice") is not None:
        return "tool_call"
    messages = request.get("messages")
    if not isinstance(messages, (list, tuple)):
        return "chat_completion"
    content_types: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, (list, tuple)) else (content,)
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("type"), str):
                content_types.add(block["type"])
    if "file" in content_types or "input_file" in content_types:
        return "file_input"
    if "image_url" in content_types or "image" in content_types or "input_image" in content_types:
        return "vision_input"
    return "chat_completion"


def _safe_provider_error_metadata(error: Exception) -> dict[str, object]:
    """只保留HTTP状态、固定错误类别及安全传输根因，不持久化自由错误内容。"""
    raw_status = getattr(error, "status_code", None)
    status = raw_status if type(raw_status) is int and 100 <= raw_status <= 599 else None
    body = getattr(error, "body", None)
    error_body = body.get("error") if isinstance(body, dict) else None
    error_body = error_body if isinstance(error_body, dict) else {}
    code_value = error_body.get("code") or error_body.get("type") or getattr(error, "code", None)
    normalized_code = re.sub(r"[^a-z0-9]", "", str(code_value or "").casefold())
    message_parts = [str(error), str(error_body.get("message") or "")]
    message = " ".join(message_parts).casefold()

    exception_name = type(error).__name__
    if status in {401, 403}:
        category = "authentication_rejected"
    elif status == 408 or isinstance(error, TimeoutError) or exception_name == "APITimeoutError":
        category = "provider_timeout"
    elif status == 429 or normalized_code in {"ratelimit", "ratelimitexceeded"}:
        category = "rate_limited"
    elif status in {409, 425} or normalized_code in {"serveroverloaded", "serviceunavailable"}:
        category = "provider_transient_rejection"
    elif status is not None and 500 <= status <= 599:
        category = "provider_server_error"
    elif "tool_choice" in message and any(token in message for token in (
        "unsupported", "not support", "unknown parameter", "unrecognized", "invalid parameter",
    )):
        category = "unsupported_tool_choice"
    elif "response_format" in message and any(token in message for token in (
        "unsupported", "not support", "unknown parameter", "unrecognized", "invalid parameter",
    )):
        category = "unsupported_response_format"
    elif normalized_code in {"contextlengthexceeded", "maximumcontextlength"} or any(
        token in message for token in ("context length", "too many tokens", "maximum context")
    ):
        category = "context_limit"
    elif normalized_code in {"invalidparameter", "invalidrequesterror", "badrequest"}:
        category = "invalid_request"
    elif status in {400, 422}:
        category = "provider_request_rejected"
    elif (isinstance(error, (ConnectionError, OSError))
          or exception_name in {"APIConnectionError", "ConnectError", "RemoteProtocolError"}):
        category = "provider_transport_error"
    else:
        category = "unclassified_provider_error"
    metadata: dict[str, object] = {
        "provider_status_code": status,
        "provider_error_category": category,
    }
    if category == "provider_transport_error":
        metadata.update(_safe_transport_cause(error))
    return metadata


def _value(value: object) -> object:
    """原SDK对象只在进程内展开用于摘要，不公开自由正文。"""
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _invalid(row: dict, errors: list[dict], stage: str, error: Exception) -> None:
    """仅记录稳定阶段与异常类型；设备路径、请求及异常正文不进入公开故障。"""
    row["observation_valid"] = False
    errors.append({"call_id": row["call_id"], "stage": stage,
                   "error_type": type(error).__name__})


def _emit(row: dict, state: dict, sink: Callable) -> None:
    """证据写失败使整轮观察无效，同时保留原SDK返回或异常身份。"""
    try:
        sink(row)
    except Exception as error:
        _invalid(row, state["errors"], "receipt_write", error)
    finally:
        state["settled_count"] += 1


def observation_health(client) -> dict[str, object]:
    """读取原对象及超时副本共享的证据健康，未安装或任何故障均不能验收通过。"""
    state = getattr(client.chat.completions.create, "_f3_observation_state", None)
    installed = isinstance(state, dict)
    errors = state["errors"] if installed else []
    in_flight = state["started_count"] - state["settled_count"] if installed else None
    return {"installed": installed, "valid": installed and not errors and in_flight == 0,
            "error_count": len(errors) if installed else None,
            "started_count": state["started_count"] if installed else None,
            "settled_count": state["settled_count"] if installed else None,
            "in_flight_count": in_flight,
            "errors": list(errors) if installed else []}


class ObservedStream:
    """保留原chunk、异常及close语义；观察失败只失去旁证，不截断原消费。"""

    def __init__(self, original, row: dict, sink: Callable, started: float, state: dict):
        """持有本轮原流及共享健康状态，不复制响应或创建成功chunk。"""
        self.original = original
        self.row = row
        self.sink = sink
        self.started = started
        self.state = state
        self.errors = state["errors"]
        self.iterator = iter(original)
        self.checksum = hashlib.sha256()
        self.settled = False
        self.closed = False

    def __iter__(self) -> Iterator:
        """观察器仍是单次原流消费迭代器。"""
        return self

    def __next__(self):
        """逐chunk原样透传，摘要故障不替换StopIteration或原SDK异常。"""
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self._settle("completed")
            raise
        except Exception as error:
            self.row["exception_type"] = type(error).__name__
            self._settle("failed")
            raise
        try:
            value = _value(chunk)
            self.checksum.update(digest(value).encode())
            if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                self.row["usage"] = safe_sdk_receipt(None, None, usage=value["usage"])["usage"]
        except Exception as error:
            _invalid(self.row, self.errors, "stream_summary", error)
        return chunk

    def _settle(self, status: str) -> None:
        """原流状态只结算一次；摘要不完整时不提供伪完整response摘要。"""
        if self.settled:
            return
        self.settled = True
        self.row.update(status=status,
                        response_sha256=self.checksum.hexdigest()
                        if self.row["observation_valid"] else None,
                        duration_ms=round((time.monotonic() - self.started) * 1000, 3))
        _emit(self.row, self.state, self.sink)

    def close(self) -> None:
        """调用原close且保持其异常；明确区分主动取消与原资源关闭失败。"""
        if self.closed:
            return
        self.closed = True
        try:
            self.original.close()
        except Exception as error:
            self.row["exception_type"] = type(error).__name__
            self._settle("failed")
            raise
        else:
            self._settle("cancelled")

    def __enter__(self):
        """原上下文管理器仍负责资源进入，观察器只保留流消费入口。"""
        enter = getattr(self.original, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, exc_type, exc, traceback):
        """保留原退出返回值和异常；流内消费失败不冒充主动取消。"""
        exit_method = getattr(self.original, "__exit__", None)
        status = "failed" if exc_type is not None else "cancelled"
        if exc_type is not None:
            self.row["exception_type"] = exc_type.__name__
        if callable(exit_method):
            try:
                return exit_method(exc_type, exc, traceback)
            except Exception as error:
                status = "failed"
                self.row["exception_type"] = type(error).__name__
                raise
            finally:
                self.closed = True
                self._settle(status)
        if exc_type is not None:
            self._settle(status)
        self.close()
        return None


def observe_client(
    client,
    sink: Callable[[dict[str, object]], None],
    *,
    _state=None,
    before_create: Callable[[tuple, dict], dict[str, object] | None] | None = None,
):
    """包装自有SDK及原超时副本；原create一次，证据故障独立于原响应和异常。"""
    original_create = client.chat.completions.create
    if getattr(original_create, "_f3_observed", False):
        return client
    original_options = client.with_options
    state = {"errors": [], "started_count": 0, "settled_count": 0} if _state is None else _state
    errors = state["errors"]

    def create(*args, **request):
        """最终发送层仅观察，不重试或过滤参数；摘要及sink故障均显式失去验收资格。"""
        started = time.monotonic()
        state["started_count"] += 1
        row: dict[str, object] = {
            "call_id": "f3_sdk_" + uuid4().hex,
            "observation_valid": True, "request_sha256": None, "model_sha256": None,
            "request_class": "chat_completion", "observation_layer": "final_sdk_create",
            "real_provider_quality": False,
            "provider_identity_qualified_by_observer": False,
        }
        try:
            row.update(sdk_identity())
            row["request_class"] = _request_class(request)
            row["request_sha256"] = digest({"args": args, "request": request})
            row["model_sha256"] = digest(request.get("model"))
        except Exception as error:
            _invalid(row, errors, "request_summary", error)
        try:
            if before_create is not None:
                row.update(before_create(args, request) or {})
        except Exception as error:
            row.update(status="failed", exception_type=type(error).__name__,
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
            _emit(row, state, sink)
            raise
        try:
            response = original_create(*args, **request)
        except Exception as error:
            row.update(status="failed", exception_type=type(error).__name__,
                       **_safe_provider_error_metadata(error),
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
            _emit(row, state, sink)
            raise
        if request.get("stream"):
            return ObservedStream(response, row, sink, started, state)
        row.update(status="completed", response_sha256=None,
                   duration_ms=round((time.monotonic() - started) * 1000, 3))
        try:
            value = _value(response)
            row["response_sha256"] = digest(value)
            if isinstance(value, dict):
                row["response_id_sha256"] = digest(value.get("id"))
                row["usage"] = safe_sdk_receipt(None, None, usage=value.get("usage"))["usage"]
        except Exception as error:
            _invalid(row, errors, "response_summary", error)
        _emit(row, state, sink)
        return response

    create._f3_observed = True
    create._f3_observation_state = state

    def with_options(*args, **options):
        """观察原SDK合法配置副本，共享证据故障，不改timeout或额外配置。"""
        return observe_client(original_options(*args, **options), sink, _state=state,
                              before_create=before_create)

    client.chat.completions.create = create
    client.with_options = with_options
    models = getattr(client, "models", None)
    original_list = getattr(models, "list", None)
    if callable(original_list) and not getattr(original_list, "_f3_observed", False):
        def list_models(*args, **params):
            """观察模型目录GET状态与条数，不记录模型ID或分页正文。"""
            started = time.monotonic()
            state["started_count"] += 1
            row: dict[str, object] = {
                "call_id": "f3_sdk_" + uuid4().hex,
                "observation_valid": True,
                "request_sha256": None,
                "model_sha256": None,
                "request_class": "model_catalog",
                "observation_layer": "final_sdk_models_list",
                "real_provider_quality": False,
                "provider_identity_qualified_by_observer": False,
            }
            try:
                row.update(sdk_identity())
                row["request_sha256"] = digest({"args": args, "request": params})
            except Exception as error:
                _invalid(row, errors, "request_summary", error)
            try:
                response = original_list(*args, **params)
            except Exception as error:
                row.update(status="failed", exception_type=type(error).__name__,
                           **_safe_provider_error_metadata(error),
                           duration_ms=round((time.monotonic() - started) * 1000, 3))
                _emit(row, state, sink)
                raise
            try:
                catalog = getattr(response, "data", None)
                if not isinstance(catalog, (list, tuple)):
                    raise TypeError("model catalog page data is not a list")
                row["catalog_count"] = len(catalog)
            except Exception as error:
                _invalid(row, errors, "catalog_summary", error)
            row.update(status="completed",
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
            _emit(row, state, sink)
            return response

        list_models._f3_observed = True
        list_models._f3_observation_state = state
        models.list = list_models
    return client
