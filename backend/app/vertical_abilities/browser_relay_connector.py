"""
@Time       : 2026/09/04 12:00
@Author     : zhanglp8181
@File       : browser_relay_connector.py
@CallChain  : Windows/宿主机连接器 → browser_relay HTTP API → 本机 OpenCLI/浏览器桥
@Description: 在浏览器所在机器轮询并执行服务端下发的固定 OpenCLI 命令，不读取或上传浏览器 Cookie。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from app.vertical_abilities.platform_runtime import PlatformRuntimeError, run_opencli_raw


CONNECTOR_MAX_HTTP_BYTES = 64 * 1024
CONNECTOR_MAX_ERROR_CHARS = 1024
CONNECTOR_DEFAULT_POLL_SECONDS = 2.0


class BrowserRelayConnectorError(RuntimeError):
    """表示宿主机连接器的参数、网络或 Relay 服务端错误。"""

    def __init__(self, code: str, message: str, *, terminal: bool = False) -> None:
        """保存连接器内部错误码和是否应停止轮询的标记。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.terminal = terminal


class BrowserRelayConnector:
    """运行宿主机到服务端的无 Cookie Relay 轮询连接器。"""

    def __init__(
        self,
        *,
        server_url: str,
        pairing_token: str,
        opencli_path: str | None = None,
        poll_seconds: float = CONNECTOR_DEFAULT_POLL_SECONDS,
        once: bool = False,
    ) -> None:
        """校验连接器启动参数并保存到当前进程，不把令牌写入文件或日志。"""

        parsed = urlsplit(str(server_url or "").strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrowserRelayConnectorError(
                "VERTICAL_ABILITY_RELAY_SERVER_URL_INVALID",
                "Relay 服务地址必须是 http 或 https URL。",
                terminal=True,
            )
        token = str(pairing_token or "").strip()
        if not token or len(token) > 256:
            raise BrowserRelayConnectorError(
                "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
                "Relay 配对令牌为空或长度无效。",
                terminal=True,
            )
        try:
            interval = max(0.2, min(float(poll_seconds), 10.0))
        except (TypeError, ValueError) as exc:
            raise BrowserRelayConnectorError(
                "VERTICAL_ABILITY_RELAY_POLL_INTERVAL_INVALID",
                "Relay 轮询间隔无效。",
                terminal=True,
            ) from exc
        self.server_url = parsed.geturl()
        self.pairing_token = token
        self.opencli_path = str(opencli_path).strip() if opencli_path else None
        self.poll_seconds = interval
        self.once = once

    def run(self) -> int:
        """持续轮询服务端命令并回传结果，令牌失效时以非零码退出。"""

        while True:
            try:
                command = self._poll_command()
            except BrowserRelayConnectorError as exc:
                self._log_error(exc)
                if exc.terminal:
                    return 2
                time.sleep(self.poll_seconds)
                continue
            if command is None:
                if self.once:
                    return 0
                continue
            try:
                result = self._execute_command(command)
            except BrowserRelayConnectorError as exc:
                self._log_error(exc)
                return 2 if exc.terminal else 0
            try:
                self._submit_result(
                    command_id=_required_command_text(command, "command_id"),
                    lease_token=_required_command_text(command, "lease_token"),
                    result=result,
                )
            except BrowserRelayConnectorError as exc:
                self._log_error(exc)
                if exc.terminal:
                    return 2
            if self.once:
                return 0

    def _poll_command(self) -> dict[str, object] | None:
        """从服务端取得一条命令，空队列不输出任何原始平台数据。"""

        query = urlencode({"wait_seconds": min(5.0, max(0.0, self.poll_seconds * 2))})
        response = self._request("GET", f"/api/browser-relay/commands/next?{query}")
        if response is None:
            return None
        if not isinstance(response, dict):
            raise BrowserRelayConnectorError(
                "VERTICAL_ABILITY_RELAY_RESPONSE_INVALID",
                "Relay 服务返回了无法识别的命令。",
                terminal=True,
            )
        return response

    def _execute_command(self, command: Mapping[str, object]) -> dict[str, object]:
        """只执行服务端允许的平台动作，并把本机 OpenCLI 异常转换为结构化失败。"""

        try:
            platform_id = _required_command_text(command, "platform_id")
            action = _required_command_text(command, "action")
            command_id = _required_command_text(command, "command_id")
            lease_token = _required_command_text(command, "lease_token")
            raw_args = command.get("positional_args", [])
            if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
                raise BrowserRelayConnectorError(
                    "VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
                    "Relay 命令位置参数格式无效。",
                    terminal=True,
                )
            limit = command.get("limit")
            if limit is not None and not isinstance(limit, int):
                raise BrowserRelayConnectorError(
                    "VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
                    "Relay 命令返回数量格式无效。",
                    terminal=True,
                )
            timeout = command.get("timeout_seconds", 120.0)
            settings = SimpleNamespace(tool_timeout_seconds=timeout)
            output = run_opencli_raw(
                platform_id,
                action,
                raw_args,
                credentials={"session_reference": f"opencli:{platform_id}"},
                limit=limit,
                settings=settings,
                executable_path=self.opencli_path,
            )
            return {
                "success": True,
                "output": output.decode("utf-8", errors="replace"),
                "command_id": command_id,
                "lease_token": lease_token,
            }
        except PlatformRuntimeError as exc:
            return {
                "success": False,
                "output": "",
                "error_code": exc.code,
                "error_message": exc.message,
                "command_id": command.get("command_id", ""),
                "lease_token": command.get("lease_token", ""),
            }
        except BrowserRelayConnectorError:
            raise
        except (TypeError, ValueError):
            return {
                "success": False,
                "output": "",
                "error_code": "VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
                "error_message": "Relay 命令格式无效。",
                "command_id": command.get("command_id", ""),
                "lease_token": command.get("lease_token", ""),
            }

    def _submit_result(
        self,
        *,
        command_id: str,
        lease_token: str,
        result: Mapping[str, object],
    ) -> None:
        """把单次 OpenCLI 回执发送回对应租约，网络失败时保留服务端重试语义。"""

        payload = {
            "lease_token": lease_token,
            "success": bool(result.get("success")),
            "output": str(result.get("output") or ""),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
        }
        self._request(
            "POST",
            f"/api/browser-relay/commands/{command_id}/result",
            payload,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> object | None:
        """使用 Bearer 配对令牌请求 Relay API，并限制错误响应读取大小。"""

        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.pairing_token}",
            "User-Agent": "gongge-xuban-browser-relay/1",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.server_url + path, data=body, headers=headers, method=method)
        timeout = max(10.0, self.poll_seconds + 10.0)
        try:
            with urlopen(request, timeout=timeout) as response:
                response_body = response.read(CONNECTOR_MAX_HTTP_BYTES + 1)
                if len(response_body) > CONNECTOR_MAX_HTTP_BYTES:
                    raise BrowserRelayConnectorError(
                        "VERTICAL_ABILITY_RELAY_RESPONSE_TOO_LARGE",
                        "Relay 服务响应超过安全上限。",
                        terminal=True,
                    )
                if not response_body:
                    return None
                try:
                    return json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BrowserRelayConnectorError(
                        "VERTICAL_ABILITY_RELAY_RESPONSE_INVALID",
                        "Relay 服务响应无法解析。",
                        terminal=True,
                    ) from exc
        except HTTPError as exc:
            response_body = exc.read(CONNECTOR_MAX_HTTP_BYTES)
            message = _remote_error_message(response_body)
            raise BrowserRelayConnectorError(
                _remote_error_code(response_body, exc.code),
                message,
                terminal=exc.code in {401, 403, 410},
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise BrowserRelayConnectorError(
                "VERTICAL_ABILITY_RELAY_NETWORK_ERROR",
                "无法连接 Relay 服务，稍后会自动重试。",
            ) from exc

    @staticmethod
    def _log_error(error: BrowserRelayConnectorError) -> None:
        """输出不包含服务地址令牌和平台回执的连接器诊断信息。"""

        print(f"browser-relay: {error.code}: {error.message}")


def main(argv: Sequence[str] | None = None) -> int:
    """解析宿主机连接器命令行参数并启动 Relay 轮询。"""

    parser = argparse.ArgumentParser(description="共格·序伴宿主机浏览器 Relay 连接器")
    parser.add_argument("--server-url", required=True, help="共格·序伴服务地址")
    parser.add_argument("--pairing-token", required=True, help="连接中心一次显示的 Relay 配对令牌")
    parser.add_argument("--opencli-path", default=None, help="可选的 OpenCLI 固定可执行路径")
    parser.add_argument("--poll-seconds", type=float, default=CONNECTOR_DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="只轮询一条命令，供诊断和测试使用")
    args = parser.parse_args(argv)
    try:
        connector = BrowserRelayConnector(
            server_url=args.server_url,
            pairing_token=args.pairing_token,
            opencli_path=args.opencli_path,
            poll_seconds=args.poll_seconds,
            once=args.once,
        )
        return connector.run()
    except BrowserRelayConnectorError as exc:
        BrowserRelayConnector._log_error(exc)
        return 2


def _required_command_text(command: Mapping[str, object], key: str) -> str:
    """读取 Relay 命令所需短文本字段并限制长度，避免命令响应成为任意数据通道。"""

    value = command.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BrowserRelayConnectorError(
            "VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
            "Relay 命令缺少有效字段。",
            terminal=True,
        )
    return value.strip()


def _remote_error_code(body: bytes, status_code: int) -> str:
    """从服务端错误中提取安全错误码，解析失败时使用 HTTP 状态映射。"""

    try:
        payload = json.loads(body.decode("utf-8"))
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code and len(code) <= 64:
            return code
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    return {
        401: "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
        403: "VERTICAL_ABILITY_RELAY_FORBIDDEN",
        410: "VERTICAL_ABILITY_RELAY_TOKEN_EXPIRED",
    }.get(status_code, "VERTICAL_ABILITY_RELAY_HTTP_ERROR")


def _remote_error_message(body: bytes) -> str:
    """从服务端错误中提取有限用户提示，不回显响应中的令牌或大段日志。"""

    try:
        payload = json.loads(body.decode("utf-8"))
        detail = payload.get("detail") if isinstance(payload, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        if isinstance(message, str) and message.strip():
            return " ".join(message.split())[:CONNECTOR_MAX_ERROR_CHARS]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    return "Relay 服务请求失败。"


if __name__ == "__main__":
    raise SystemExit(main())
