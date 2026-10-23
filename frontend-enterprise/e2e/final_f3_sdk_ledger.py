"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : final_f3_sdk_ledger.py
@CallChain  : F3自有服务 → 原SDK观察器 → 持久派发预算与私有旁证文件
@Description: 为限定诊断在最终SDK外呼前持久预留次数/token，并核对完整调用旁证。
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from threading import RLock
import time

from final_f3_preparation import PreparationError
from final_f3_private_jsonl import PrivateJsonlLimitError, append_private_jsonl_bytes
from final_f3_sdk_identity import IDENTITY_FIELDS
from final_f3_sdk_observer import observation_health, observe_client

MAX_SDK_LEDGER_BYTES = 32 * 1024 * 1024
MAX_SDK_LEDGER_ROWS = 8192
MAX_SDK_LEDGER_FILES = 64
MAX_MODEL_INVOCATIONS = 64
MAX_MODEL_PROMPT_TOKENS_PER_CALL = 128_000
MAX_MODEL_PROMPT_TOKENS = 8_192_000
MAX_MODEL_COMPLETION_TOKENS_PER_CALL = 8192
MAX_MODEL_COMPLETION_TOKENS = 524_288
_MODEL_BUDGET_PROFILE = "memory-dta-freeze-v1"
_SOP_FIRST_THREE_MODEL_BUDGET_PROFILE = "i2-sop-first-three-v1"
MAX_SOP_FIRST_THREE_MODEL_INVOCATIONS = 48
MAX_SOP_FIRST_THREE_PROMPT_TOKENS = 4_096_000
MAX_SOP_FIRST_THREE_COMPLETION_TOKENS = 262_144
_OBSERVATION_PATTERNS = (
    "sdk-observations-*.safe.jsonl", "memory-wire-*.safe.jsonl", "sdk.safe.jsonl",
)

_FIELDS = frozenset({
    "call_id", "observation_valid", "request_sha256", "model_sha256", "observation_layer",
    "request_class", "catalog_count", "provider_transport_cause", "provider_os_errno",
    "real_provider_quality", "provider_identity_qualified_by_observer", "pid", "started_at_utc",
    "phase_sha256", "identity_sha256", "kind", "source_sha256",
    "diagnostic_scope",
    "business_identity_qualified_by_observer", "phase_source", "sdk_thread_phase_sha256",
    "status", "exception_type", "provider_status_code", "provider_error_category",
    "model_budget_profile", "prompt_tokens_reserved", "completion_tokens_reserved",
    "duration_ms", "response_sha256", "response_id_sha256", "usage",
})
_BOOLEANS = frozenset({
    "observation_valid", "real_provider_quality", "provider_identity_qualified_by_observer",
    "business_identity_qualified_by_observer",
})
_USAGE = frozenset({"prompt_tokens", "completion_tokens", "total_tokens"})
_PROVIDER_ERROR_CATEGORIES = frozenset({
    "authentication_rejected", "provider_timeout", "rate_limited",
    "provider_transient_rejection", "provider_server_error", "unsupported_tool_choice",
    "unsupported_response_format", "context_limit", "invalid_request",
    "provider_request_rejected", "provider_transport_error", "unclassified_provider_error",
})
_REQUEST_CLASSES = frozenset({
    "chat_completion", "structured_output", "tool_call", "vision_input", "file_input",
    "model_catalog",
})
_TRANSPORT_CAUSES = frozenset({
    "connection_refused", "timeout", "connection_reset", "host_unreachable",
    "network_unreachable", "dns_temporary_failure", "dns_name_not_found", "dns_error",
    "tls_error", "remote_protocol_error", "unknown_transport_error",
})
_SAFE_PROVIDER_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ECONNRESET, errno.EHOSTUNREACH,
    errno.ENETUNREACH, errno.EPIPE, socket.EAI_AGAIN, socket.EAI_NONAME,
})


def _safe_value(key: str, value: object) -> bool:
    """逐字段收窄安全旁证值，SHA/枚举/非负计数不能被自由正文替代。"""
    if key == "identity_sha256":
        return (isinstance(value, dict) and not value.keys() - IDENTITY_FIELDS
                and all(isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
                        for item in value.values()))
    if key == "diagnostic_scope":
        if value is None:
            return True
        fields = {
            "schema", "invocation_sha256", "tenant_sha256", "admin_actor_sha256",
            "model_config_sha256", "config_fingerprint_sha256", "probe_class",
        }
        return (isinstance(value, dict) and set(value) == fields
                and value.get("schema") == "model-preflight-v1"
                and value.get("probe_class") in {"model_connection", "native_capabilities"}
                and all(isinstance(value.get(field), str)
                        and re.fullmatch(r"[a-f0-9]{64}", value[field])
                        for field in fields - {"schema", "probe_class"}))
    if key == "usage":
        return (isinstance(value, dict) and not value.keys() - _USAGE
                and all(item is None or type(item) is int and item >= 0
                        for item in value.values()))
    if key.endswith("_sha256"):
        return value is None or isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))
    if key in _BOOLEANS:
        return type(value) is bool and (key == "observation_valid" or value is False)
    if key == "pid":
        return type(value) is int and value > 0
    if key == "duration_ms":
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    if key == "provider_status_code":
        return value is None or type(value) is int and 100 <= value <= 599
    if key == "provider_error_category":
        return value in _PROVIDER_ERROR_CATEGORIES
    if key == "provider_transport_cause":
        return value in _TRANSPORT_CAUSES
    if key == "provider_os_errno":
        return value is None or type(value) is int and value in _SAFE_PROVIDER_ERRNOS
    if key == "request_class":
        return value in _REQUEST_CLASSES
    if key == "catalog_count":
        return type(value) is int and value >= 0
    if key == "model_budget_profile":
        return value in {_MODEL_BUDGET_PROFILE, _SOP_FIRST_THREE_MODEL_BUDGET_PROFILE}
    if key in {"prompt_tokens_reserved", "completion_tokens_reserved"}:
        return type(value) is int and value >= 0
    if key == "started_at_utc":
        return isinstance(value, str) and bool(re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00", value,
        ))
    if key == "exception_type":
        return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z_]\w{0,127}", value))
    if key == "kind":
        return value in (None, "sop", "dynamic_task")
    if key == "status":
        return value in ("completed", "failed", "cancelled")
    if key == "phase_source":
        return value in ("original_current_llm_operation", "original_caller_observation_handoff")
    if key == "observation_layer":
        return value in {"final_sdk_create", "final_sdk_models_list"}
    return key == "call_id"


class SdkLedger:
    """私有进程旁证完整性核验；仅串行结算字节，不锁原SDK派发和业务消费者。"""

    def __init__(self, path: Path):
        """持有自有输出路径及原SDK客户端，不发起网络或加载应用。"""
        self.path = path
        self.clients = []
        self._states = set()
        self._lock = RLock()
        self._checksum = hashlib.sha256()
        self._bytes = 0
        self._rows = 0
        self._calls = set()
        self._write_failed = False
        self._budget_failed = False
        profile = os.environ.get("F3_MODEL_BUDGET_PROFILE")
        limits = _model_budget_limits(profile)
        if profile is not None and limits is None:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_PROFILE_UNKNOWN")
        self._model_budget = (
            _PersistentModelDispatchBudget(path.parent, profile, limits)
            if profile is not None and limits is not None else None
        )

    def _open(self, *, write: bool) -> int:
        """通过私有父目录fd锚定文件，不跟随链接或追加到非私有/多链接文件。"""
        parent = self.path.parent
        try:
            if not self.path.is_absolute() or parent.resolve() != parent:
                raise PreparationError("F3_PRIVATE_SDK_LEDGER_REQUIRED")
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PreparationError("F3_PRIVATE_SDK_LEDGER_REQUIRED")
                flags = os.O_NOFOLLOW | os.O_NONBLOCK
                flags |= os.O_WRONLY | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY
                descriptor = os.open(self.path.name, flags, 0o600, dir_fd=directory)
            finally:
                os.close(directory)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size != self._bytes):
                os.close(descriptor)
                raise PreparationError("F3_PRIVATE_SDK_LEDGER_REQUIRED")
            return descriptor
        except OSError as exc:
            raise PreparationError("F3_PRIVATE_SDK_LEDGER_REQUIRED") from exc

    def _encode(self, row: dict) -> bytes:
        """只接收原观察器的安全字段；拒绝自由正文、重复call和非JSON值再落盘。"""
        if (not isinstance(row, dict) or row.keys() - _FIELDS
                or not re.fullmatch(r"f3_sdk_[a-f0-9]{32}", str(row.get("call_id")))
                or row["call_id"] in self._calls
                or not all(_safe_value(key, value) for key, value in row.items())):
            raise PreparationError("F3_SDK_RECEIPT_SCHEMA_INVALID")
        try:
            return (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
        except (ValueError, TypeError) as exc:
            raise PreparationError("F3_SDK_RECEIPT_SCHEMA_INVALID") from exc

    def __call__(self, row: dict) -> None:
        """在锁内续写短写/系统中断并同步；任何失败永久使本进程旁证无效。"""
        with self._lock:
            try:
                data = self._encode(row)
                append_private_jsonl_bytes(
                    self.path.parent, self.path, data,
                    maximum_bytes=MAX_SDK_LEDGER_BYTES,
                    maximum_rows=MAX_SDK_LEDGER_ROWS,
                    maximum_files=MAX_SDK_LEDGER_FILES,
                    patterns=_OBSERVATION_PATTERNS,
                    row_limit_code="F3_SDK_RECEIPT_ROW_LIMIT",
                    byte_limit_code="F3_SDK_RECEIPT_BYTE_LIMIT",
                    file_limit_code="F3_SDK_RECEIPT_FILE_LIMIT",
                )
                self._checksum.update(data)
                self._bytes += len(data)
                self._rows += 1
                self._calls.add(row["call_id"])
            except PrivateJsonlLimitError as error:
                self._write_failed = True
                raise PreparationError(str(error)) from error
            except Exception:
                self._write_failed = True
                raise

    def observe(self, client):
        """沿用原观察器安装，不创建SDK替身或业务成功回执。"""
        with self._lock:
            prior = getattr(client.chat.completions.create, "_f3_observation_state", None)
            if prior is not None and id(prior) not in self._states:
                raise PreparationError("F3_SDK_CLIENT_ALREADY_OBSERVED_ELSEWHERE")
            client = observe_client(
                client, self,
                before_create=self._reserve_model_dispatch,
            )
            state = client.chat.completions.create._f3_observation_state
            if id(state) not in self._states:
                self._states.add(id(state))
                self.clients.append(client)
        return client

    def _reserve_model_dispatch(self, args: tuple, request: dict) -> dict[str, object]:
        """SDK派发前拒绝已失效账本，并在真实最终create外呼前持久预留预算。"""
        if self._write_failed:
            raise PreparationError("F3_SDK_LEDGER_INVALID_BLOCKING_DISPATCH")
        if self._model_budget is None:
            return {}
        try:
            prompt_tokens, completion_tokens = _model_request_token_reservation(args, request)
            self._model_budget.reserve(prompt_tokens, completion_tokens)
            return {
                "model_budget_profile": self._model_budget.profile,
                "prompt_tokens_reserved": prompt_tokens,
                "completion_tokens_reserved": completion_tokens,
            }
        except PreparationError:
            self._budget_failed = True
            raise

    def _file_matches(self) -> bool:
        """流式读回实际完整字节与行数，不把解析成功的剩余行冒充全SDK覆盖。"""
        try:
            if not self.path.exists() and self._bytes == 0:
                return True
            descriptor = self._open(write=False)
            try:
                checksum, size, rows = hashlib.sha256(), 0, 0
                while chunk := os.read(descriptor, 65536):
                    checksum.update(chunk)
                    size += len(chunk)
                    rows += chunk.count(b"\n")
                return (size == self._bytes and rows == self._rows
                        and checksum.digest() == self._checksum.digest())
            finally:
                os.close(descriptor)
        except (OSError, PreparationError):
            return False

    def health(self) -> dict:
        """汇总独立client及共享副本并核实际文件；未知/在途/缺行不能判完整通过。"""
        with self._lock:
            states = [observation_health(client) for client in self.clients]
            settled = sum(state["settled_count"] for state in states)
            file_matches = self._file_matches()
            result = {
                "valid": bool(states) and not self._write_failed and not self._budget_failed
                and file_matches and all(state["valid"] for state in states) and settled == self._rows,
                "client_state_count": len(states),
                "started_count": sum(state["started_count"] for state in states),
                "settled_count": settled,
                "in_flight_count": sum(state["in_flight_count"] for state in states),
                "observation_error_count": sum(state["error_count"] for state in states),
                "receipt_count": self._rows, "file_matches": file_matches,
                "write_failed": self._write_failed, "JRN08_passed": False,
                "business_identity_qualified": False,
            }
            if self._model_budget is not None:
                try:
                    budget = self._model_budget.snapshot()
                    result["model_dispatch_budget"] = {
                        **budget,
                        "profile": self._model_budget.profile,
                        "prompt_token_counting_kind": (
                            "message_estimator_plus_ceil_compact_json_utf8_bytes_divided_by_four_"
                            "for_other_sdk_request_fields"
                        ),
                        "request_metadata_fields_included": True,
                        "provider_reported_usage_is_enforced": False,
                        "maximum_model_response_body_bytes": 8 * 1024 * 1024,
                        "model_response_body_limit_enforced_by_backend_client": True,
                        "model_catalog_requests_budgeted": False,
                        "maximum_calls": self._model_budget.limits["calls"],
                        "maximum_prompt_tokens_per_call": self._model_budget.limits[
                            "prompt_tokens_per_call"
                        ],
                        "maximum_prompt_tokens": self._model_budget.limits["prompt_tokens"],
                        "maximum_completion_tokens_per_call": self._model_budget.limits[
                            "completion_tokens_per_call"
                        ],
                        "maximum_completion_tokens": self._model_budget.limits[
                            "completion_tokens"
                        ],
                        "valid": True,
                    }
                except PreparationError as error:
                    result["model_dispatch_budget"] = {"valid": False, "error_code": str(error)}
                    result["valid"] = False
            return result


class _PersistentModelDispatchBudget:
    """跨应用进程共享调用数、prompt估值及completion请求上限并于派发前fsync预留。"""

    def __init__(self, runtime: Path, profile: str, limits: dict[str, int]):
        """绑定受保护运行根，沿应用真重启继续读取唯一调用额度。"""
        self.runtime = runtime
        self.profile = profile
        self.limits = limits
        suffix = "memory-freeze" if profile == _MODEL_BUDGET_PROFILE else "sop-first-three"
        self.path = runtime / f".{suffix}-model-budget.safe.json"
        self.initialized_path = runtime / f".{suffix}-model-budget.initialized.safe"
        self.lock_name = f".{suffix}-model-budget.lock"

    def _read_state(self, directory: int) -> dict[str, int]:
        """只读取自有小型整型预算文件；初始化锚点存在而状态丢失时拒绝重置额度。"""
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            if self._initialized_marker_exists(directory):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_STATE_MISSING")
            self._create_initialized_marker(directory)
            initial = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            self._write_state(directory, initial)
            return initial
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > 512):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_STATE_INVALID")
            value = json.loads(os.read(descriptor, 513))
        except (OSError, ValueError, TypeError) as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_STATE_INVALID") from error
        finally:
            os.close(descriptor)
        if (not isinstance(value, dict) or value.keys() != {"calls", "prompt_tokens", "completion_tokens"}
                or any(type(value[key]) is not int or value[key] < 0
                       for key in ("calls", "prompt_tokens", "completion_tokens"))):
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_STATE_INVALID")
        self._create_initialized_marker(directory)
        return value

    def _initialized_marker_exists(self, directory: int) -> bool:
        """验证独立初始化锚点的所有权、权限、内容和单链接属性。"""
        try:
            descriptor = os.open(
                self.initialized_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return False
        except OSError as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_INVALID") from error
        try:
            info = os.fstat(descriptor)
            marker = os.read(descriptor, 513)
            expected = json.dumps({
                "profile": self.profile,
                "schema": "gongge_xuban.model_dispatch_budget_initialized.v1",
            }, sort_keys=True, separators=(",", ":")).encode("ascii")
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or marker != expected):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_INVALID")
            return True
        except OSError as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_INVALID") from error
        finally:
            os.close(descriptor)

    def _create_initialized_marker(self, directory: int) -> None:
        """仅当预算状态首次创建或已有状态可读时独占建立初始化锚点。"""
        packet = json.dumps({
            "profile": self.profile,
            "schema": "gongge_xuban.model_dispatch_budget_initialized.v1",
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        try:
            descriptor = os.open(
                self.initialized_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
        except FileExistsError:
            if not self._initialized_marker_exists(directory):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_INVALID")
            return
        except OSError as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_WRITE_FAILED") from error
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_INVALID")
            view = memoryview(packet)
            while view:
                try:
                    written = os.write(descriptor, view)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("marker write returned no bytes")
                view = view[written:]
            os.fsync(descriptor)
            os.fsync(directory)
        except Exception as error:
            try:
                os.unlink(self.initialized_path.name, dir_fd=directory)
            except OSError:
                pass
            if isinstance(error, PreparationError):
                raise
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_MARKER_WRITE_FAILED") from error
        finally:
            os.close(descriptor)

    def _locked_state(self, reserve: tuple[int, int] | None = None) -> dict[str, int]:
        """在非阻塞有截止的进程锁内读取或原子落盘，锁不可得时拒绝外呼。"""
        try:
            if self.runtime.is_symlink() or self.runtime.resolve() != self.runtime:
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_RUNTIME_INVALID")
            directory = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directory_info = os.fstat(directory)
            if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                    or stat.S_IMODE(directory_info.st_mode) != 0o700):
                os.close(directory)
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_RUNTIME_INVALID")
        except OSError as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_RUNTIME_INVALID") from error
        lock_descriptor = -1
        locked = False
        try:
            lock_descriptor = os.open(
                self.lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=directory,
            )
            lock_info = os.fstat(lock_descriptor)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_LOCK_INVALID")
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise PreparationError("F3_MODEL_DISPATCH_BUDGET_LOCK_TIMEOUT") from error
                    time.sleep(0.01)
            state = self._read_state(directory)
            if (state["calls"] > self.limits["calls"]
                    or state["prompt_tokens"] > self.limits["prompt_tokens"]
                    or state["completion_tokens"] > self.limits["completion_tokens"]):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_STATE_INVALID")
            if reserve is None:
                return state
            prompt_tokens, completion_tokens = reserve
            if state["calls"] >= self.limits["calls"]:
                raise PreparationError("F3_MODEL_DISPATCH_CALL_BUDGET_EXHAUSTED")
            if (state["prompt_tokens"] + prompt_tokens > self.limits["prompt_tokens"]
                    or state["completion_tokens"] + completion_tokens
                    > self.limits["completion_tokens"]):
                raise PreparationError("F3_MODEL_DISPATCH_TOKEN_BUDGET_EXHAUSTED")
            updated = {
                "calls": state["calls"] + 1,
                "prompt_tokens": state["prompt_tokens"] + prompt_tokens,
                "completion_tokens": state["completion_tokens"] + completion_tokens,
            }
            self._write_state(directory, updated)
            return updated
        except PreparationError:
            raise
        except OSError as error:
            raise PreparationError("F3_MODEL_DISPATCH_BUDGET_IO_FAILED") from error
        finally:
            if lock_descriptor >= 0:
                if locked:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
            os.close(directory)

    def _write_state(self, directory: int, state: dict[str, int]) -> None:
        """以同目录0600临时文件replace并同步目录，防重启丢额度或半写。"""
        packet = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")
        temporary = f".memory-freeze-model-budget-{os.getpid()}-{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise PreparationError("F3_MODEL_DISPATCH_BUDGET_TEMP_INVALID")
            data = memoryview(packet)
            while data:
                try:
                    written = os.write(descriptor, data)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("F3_MODEL_DISPATCH_BUDGET_WRITE_FAILED")
                data = data[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            os.unlink(temporary, dir_fd=directory)
            raise
        os.close(descriptor)
        os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)

    def reserve(self, prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
        """先验证单请求输入/输出token上限，再不可回滚地记一次真实派发额度。"""
        if (type(prompt_tokens) is not int or prompt_tokens < 0
                or prompt_tokens > self.limits["prompt_tokens_per_call"]
                or type(completion_tokens) is not int or not 1 <= completion_tokens
                or completion_tokens > self.limits["completion_tokens_per_call"]):
            raise PreparationError("F3_MODEL_DISPATCH_TOKEN_BUDGET_EXHAUSTED")
        return self._locked_state((prompt_tokens, completion_tokens))

    def snapshot(self) -> dict[str, int]:
        """返回不含正文的持久调用数与累计输入/输出token安全计数。"""
        return self._locked_state()


def _model_budget_limits(profile: str | None) -> dict[str, int] | None:
    """只为精确登记的诊断选择不可扩大的SDK调用与token额度。"""
    if profile == _MODEL_BUDGET_PROFILE:
        return {
            "calls": MAX_MODEL_INVOCATIONS,
            "prompt_tokens_per_call": MAX_MODEL_PROMPT_TOKENS_PER_CALL,
            "prompt_tokens": MAX_MODEL_PROMPT_TOKENS,
            "completion_tokens_per_call": MAX_MODEL_COMPLETION_TOKENS_PER_CALL,
            "completion_tokens": MAX_MODEL_COMPLETION_TOKENS,
        }
    if profile == _SOP_FIRST_THREE_MODEL_BUDGET_PROFILE:
        return {
            "calls": MAX_SOP_FIRST_THREE_MODEL_INVOCATIONS,
            "prompt_tokens_per_call": MAX_MODEL_PROMPT_TOKENS_PER_CALL,
            "prompt_tokens": MAX_SOP_FIRST_THREE_PROMPT_TOKENS,
            "completion_tokens_per_call": MAX_MODEL_COMPLETION_TOKENS_PER_CALL,
            "completion_tokens": MAX_SOP_FIRST_THREE_COMPLETION_TOKENS,
        }
    return None


def _model_request_token_reservation(args: tuple, request: dict) -> tuple[int, int]:
    """估算完整SDK请求的输入额度，并按最终SDK max_tokens预留completion额度。"""
    if args or not isinstance(request, dict):
        raise PreparationError("F3_MODEL_DISPATCH_REQUEST_BUDGET_FIELDS_INVALID")
    messages = request.get("messages")
    completion_values = [request[key] for key in ("max_tokens", "max_completion_tokens") if key in request]
    if not isinstance(messages, list) or len(completion_values) != 1:
        raise PreparationError("F3_MODEL_DISPATCH_REQUEST_BUDGET_FIELDS_INVALID")
    try:
        from app.llm.client import _request_tokens

        prompt_tokens = _request_tokens(messages)
        metadata = {
            key: value for key, value in request.items()
            if key not in {"messages", "max_tokens", "max_completion_tokens"}
        }
        metadata_json = json.dumps(
            metadata, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
        prompt_tokens += math.ceil(len(metadata_json.encode("utf-8")) / 4)
    except (ImportError, TypeError, ValueError, UnicodeError) as error:
        raise PreparationError("F3_MODEL_DISPATCH_TOKEN_ESTIMATOR_UNAVAILABLE") from error
    completion_tokens = completion_values[0]
    if type(prompt_tokens) is not int or type(completion_tokens) is not int:
        raise PreparationError("F3_MODEL_DISPATCH_REQUEST_BUDGET_FIELDS_INVALID")
    return prompt_tokens, completion_tokens
