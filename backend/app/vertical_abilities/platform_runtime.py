"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : platform_runtime.py
@CallChain  : 垂直能力适配器 → 固定平台命令/OpenCLI → 网站登录态数据
@Description: 将服务端用户设备调度与宿主机固定命令执行分离，保留取消和输出安全边界。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from app.cancellation import TurnCancellationRequested, raise_if_cancelled
from app.vertical_abilities.browser_runtime import (
    BrowserRuntimeError,
    normalize_opencli_profile,
    require_opencli_browser_bridge,
)


MAX_PLATFORM_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_PLATFORM_TIMEOUT_SECONDS = 120.0


class PlatformRuntimeError(RuntimeError):
    """表示平台命令缺失、超时、取消或输出无法安全解析。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定运行时错误码和不包含凭据的用户提示。"""

        super().__init__(message)
        self.code = code
        self.message = message


def run_opencli(
    platform_id: str,
    action: str,
    positional_args: Sequence[str] = (),
    *,
    credentials: Mapping[str, str],
    limit: int | None = None,
    settings: object | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    relay_executor: Callable[..., bytes] | None = None,
    executable_path: str | None = None,
) -> object:
    """经用户已授权的设备执行固定命令并解析回执，拒绝服务端借用无归属全局浏览器。"""

    normalized_platform, normalized_action, _action_spec = _validated_opencli_action(
        platform_id,
        action,
    )
    session_reference = str(credentials.get("session_reference") or "").strip()
    values = _validate_positional_args(positional_args)
    bounded_limit = _bounded_limit(limit) if limit is not None else None
    timeout_seconds = _runtime_timeout(settings)
    if session_reference.startswith("relay:"):
        if relay_executor is None:
            raise PlatformRuntimeError(
                "VERTICAL_ABILITY_RELAY_NOT_CONNECTED",
                "远程浏览器 Relay 执行器未建立，请重新连接宿主机浏览器。",
            )
        try:
            output = relay_executor(
                normalized_platform,
                normalized_action,
                values,
                bounded_limit,
                timeout_seconds=timeout_seconds,
                is_cancelled=is_cancelled,
            )
        except (PlatformRuntimeError, TurnCancellationRequested):
            raise
        except Exception as exc:
            raise PlatformRuntimeError(
                "VERTICAL_ABILITY_RELAY_NOT_CONNECTED",
                "宿主机浏览器 Relay 执行失败，请检查连接器状态。",
            ) from exc
    elif session_reference.startswith("opencli:"):
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED",
            "请在浏览器所在设备完成自己的 Relay 配对；服务端全局浏览器不能代替用户授权。",
        )
    else:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_CONNECTION_REQUIRED",
            "该网站操作需要先连接已登录的浏览器会话。",
        )
    if normalized_action == "subtitle":
        return output.decode("utf-8", errors="replace").strip()
    return _parse_structured_output(output)


def run_opencli_raw(
    platform_id: str,
    action: str,
    positional_args: Sequence[str] = (),
    *,
    credentials: Mapping[str, str],
    limit: int | None = None,
    settings: object | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    executable_path: str | None = None,
    opencli_profile: str | None = None,
) -> bytes:
    """仅供用户显式启动的宿主机连接器执行固定平台命令，并固定到目标浏览器 Profile。"""

    normalized_platform, normalized_action, action_spec = _validated_opencli_action(
        platform_id,
        action,
    )
    session_reference = str(credentials.get("session_reference") or "").strip()
    if session_reference != f"opencli:{normalized_platform}":
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_CONNECTION_REQUIRED",
            "该网站操作需要先连接已登录的浏览器会话。",
        )
    try:
        selected_profile = normalize_opencli_profile(opencli_profile)
        if selected_profile is None:
            require_opencli_browser_bridge(normalized_platform)
        else:
            require_opencli_browser_bridge(
                normalized_platform,
                opencli_profile=selected_profile,
            )
    except BrowserRuntimeError as exc:
        raise PlatformRuntimeError(exc.code, exc.message) from exc
    values = _validate_positional_args(positional_args)
    executable = (
        _find_executable("opencli", "OpenCLI")
        if executable_path is None
        else _find_executable("opencli", "OpenCLI", explicit_path=executable_path)
    )
    command = [str(executable)]
    if selected_profile is not None:
        command.extend(("--profile", selected_profile))
    command.extend((normalized_platform, *action_spec, *values))
    if limit is not None:
        bounded_limit = _bounded_limit(limit)
        if normalized_platform == "twitter":
            command.extend(("-n", str(bounded_limit)))
        else:
            command.extend(("--limit", str(bounded_limit)))
    if normalized_action != "subtitle":
        command.extend(("-f", "yaml"))
    return _run_command(
        command,
        timeout_seconds=_runtime_timeout(settings),
        is_cancelled=is_cancelled,
    )


def run_twitter_cli(
    action: str,
    positional_args: Sequence[str] = (),
    *,
    credentials: Mapping[str, str],
    limit: int | None = None,
    settings: object | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> object:
    """用每次调用独立的环境变量执行 twitter-cli，不修改服务进程环境。"""

    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in {"search", "tweet", "article", "user-posts", "user"}:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_PLATFORM_ACTION_INVALID",
            "Twitter/X 操作不在能力包允许的范围内。",
        )
    auth_token = str(credentials.get("auth_token") or "").strip()
    ct0 = str(credentials.get("ct0") or "").strip()
    if not auth_token or not ct0:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_CONNECTION_REQUIRED",
            "Twitter/X 需要完整的 auth_token 和 ct0 Cookie，或连接已登录浏览器。",
        )
    values = _validate_positional_args(positional_args)
    executable = _find_executable("twitter", "twitter-cli")
    command = [executable, normalized_action, *values]
    if limit is not None:
        command.extend(("-n", str(_bounded_limit(limit))))
    environment = os.environ.copy()
    environment["TWITTER_AUTH_TOKEN"] = auth_token
    environment["TWITTER_CT0"] = ct0
    output = _run_command(
        command,
        timeout_seconds=_runtime_timeout(settings),
        is_cancelled=is_cancelled,
        environment=environment,
    )
    return _parse_structured_output(output)


_OPENCLI_ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "twitter": {
        "search": ("search",),
        "tweet": ("tweet",),
        "article": ("article",),
        "user-posts": ("user-posts",),
        "user": ("user",),
    },
    "reddit": {
        "search": ("search",),
        "read": ("read",),
        "subreddit": ("subreddit",),
        "hot": ("hot",),
        "popular": ("popular",),
        "subreddit-info": ("subreddit-info",),
    },
    "bilibili": {"subtitle": ("subtitle",)},
    "facebook": {
        "search": ("search",),
        "profile": ("profile",),
        "feed": ("feed",),
        "groups": ("groups",),
    },
    "instagram": {
        "search": ("search",),
        "profile": ("profile",),
        "user": ("user",),
        "explore": ("explore",),
        "saved": ("saved",),
    },
    "xiaohongshu": {
        "search": ("search",),
        "note": ("note",),
        "comments": ("comments",),
        "feed": ("feed",),
        "user": ("user",),
    },
    "xueqiu": {
        "whoami": ("whoami",),
        "search": ("search",),
        "stock": ("stock",),
        "hot": ("hot",),
        "hot-stock": ("hot-stock",),
    },
}


def _find_executable(name: str, display_name: str, *, explicit_path: str | None = None) -> str:
    """优先解析安装包内固定路径，开发环境最后才从 PATH 查找同名运行时。"""

    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    configured_path = os.environ.get("VERTICAL_ABILITY_OPENCLI_PATH", "").strip()
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(str(meipass)))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        candidates.extend(
            root / "runtime" / "opencli" / filename
            for filename in ("opencli.exe", "opencli.cmd", "opencli.bat", "opencli")
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if getattr(sys, "frozen", False):
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_RUNTIME_MISSING",
            f"{display_name} 安装包运行时缺失，请重新安装产品。",
        )
    executable = shutil.which(name)
    if executable is None:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_RUNTIME_MISSING",
            f"{display_name} 运行时未安装，请先安装并启动对应平台运行环境。",
        )
    return executable


def _validated_opencli_action(
    platform_id: str,
    action: str,
) -> tuple[str, str, tuple[str, ...]]:
    """规范化并校验 OpenCLI 平台操作，保证本机连接器不能执行任意 CLI 子命令。"""

    normalized_platform = str(platform_id or "").strip().casefold()
    normalized_action = str(action or "").strip().casefold()
    action_spec = _OPENCLI_ACTIONS.get(normalized_platform, {}).get(normalized_action)
    if action_spec is None:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_PLATFORM_ACTION_INVALID",
            "该网站操作不在能力包允许的范围内。",
        )
    return normalized_platform, normalized_action, action_spec


def _validate_positional_args(values: Sequence[str]) -> list[str]:
    """校验传给固定平台命令的位置参数，阻断参数注入和超长输出诱导。"""

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise PlatformRuntimeError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "平台命令参数格式无效。")
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or len(text) > 2048 or text.startswith("-"):
            raise PlatformRuntimeError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "平台命令参数无效。")
        normalized.append(text)
    if len(normalized) > 3:
        raise PlatformRuntimeError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "平台命令参数数量超出范围。")
    return normalized


def _bounded_limit(value: int) -> int:
    """将平台命令的返回数量收敛到统一上限。"""

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PlatformRuntimeError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "平台返回数量无效。") from exc
    if not 1 <= number <= 50:
        raise PlatformRuntimeError("VERTICAL_ABILITY_ARGUMENTS_INVALID", "平台返回数量超出范围。")
    return number


def _runtime_timeout(settings: object | None) -> float:
    """读取平台命令总超时，并限制在可控范围内。"""

    raw = getattr(settings, "tool_timeout_seconds", DEFAULT_PLATFORM_TIMEOUT_SECONDS)
    try:
        value = float(raw or DEFAULT_PLATFORM_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_PLATFORM_TIMEOUT_SECONDS
    return max(1.0, min(value, 300.0))


def _run_command(
    command: list[str],
    *,
    timeout_seconds: float,
    is_cancelled: Callable[[], bool] | None,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """运行固定命令并并发收集 stdout/stderr，处理超时、取消、进程组和输出上限。"""

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment) if environment is not None else None,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        raise PlatformRuntimeError("VERTICAL_ABILITY_RUNTIME_MISSING", "平台运行时无法启动。") from exc

    stdout_state = _StreamState()
    stderr_state = _StreamState()
    readers = [
        threading.Thread(target=_read_stream, args=(process.stdout, stdout_state), daemon=True),
        threading.Thread(target=_read_stream, args=(process.stderr, stderr_state), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            raise_if_cancelled(is_cancelled)
            if stdout_state.overflow or stderr_state.overflow:
                _terminate_process(process)
                raise PlatformRuntimeError(
                    "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
                    "平台运行时输出超过安全上限。",
                )
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise PlatformRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "平台运行时执行超时。")
            time.sleep(0.05)
        return_code = process.returncode
        for reader in readers:
            reader.join(timeout=1.0)
        if stdout_state.overflow or stderr_state.overflow:
            raise PlatformRuntimeError(
                "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
                "平台运行时输出超过安全上限。",
            )
        if return_code != 0:
            raise PlatformRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
                "平台运行时返回失败，请检查平台登录态和运行环境。",
            )
        return bytes(stdout_state.data)
    except TurnCancellationRequested:
        _terminate_process(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class _StreamState:
    """保存单个子进程输出流的有界内存缓冲。"""

    def __init__(self) -> None:
        """初始化线程安全要求之外的单写入者缓冲。"""

        self.data = bytearray()
        self.overflow = False


def _read_stream(stream: Any, state: _StreamState) -> None:
    """在线程中读取子进程流并在超限后丢弃后续数据，避免管道阻塞。"""

    if stream is None:
        return
    while True:
        chunk = stream.read(16 * 1024)
        if not chunk:
            return
        if not isinstance(chunk, bytes):
            chunk = str(chunk).encode("utf-8", errors="replace")
        previous_length = len(state.data)
        if previous_length < MAX_PLATFORM_OUTPUT_BYTES:
            remaining = MAX_PLATFORM_OUTPUT_BYTES - previous_length
            state.data.extend(chunk[:remaining])
        if previous_length + len(chunk) > MAX_PLATFORM_OUTPUT_BYTES:
            state.overflow = True


def _parse_structured_output(output: bytes) -> object:
    """优先解析安全 YAML，兼容 JSON 和少量纯文本回执，不接受空结果。"""

    text = output.decode("utf-8", errors="replace").strip()
    if not text:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "平台运行时返回了空结果。",
        )
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
    if parsed is None:
        raise PlatformRuntimeError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "平台运行时返回了空结果。",
        )
    return parsed


def _process_group_kwargs() -> dict[str, int | bool]:
    """创建独立进程组，确保取消和超时时不会遗留平台子进程。"""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """跨平台终止固定平台命令的进程组并回收资源。"""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
