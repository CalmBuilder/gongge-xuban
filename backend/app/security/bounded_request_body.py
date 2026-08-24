"""
@Time       : 2026/08/15 07:20
@Author     : zhanglp8181
@File       : bounded_request_body.py
@CallChain  : ASGI Server → BoundedAttachmentBodyMiddleware → FastAPI multipart parser
@Description: 在multipart进入框架spool前限制附件请求总字节，覆盖无Content-Length分块上传。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any


AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[dict[str, Any], AsgiReceive, AsgiSend], Awaitable[None]]


class _AttachmentBodyTooLarge(Exception):
    """标记附件请求在进入multipart解析前已超过硬上限。"""


class BoundedAttachmentBodyMiddleware:
    """只对附件上传入口执行Content-Length早拒和真实receive流累计限额。"""

    def __init__(self, app: AsgiApp, *, max_body_bytes: int) -> None:
        """保存下游ASGI应用和包含multipart开销的请求体硬上限。"""

        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        """在下游读取每个HTTP body chunk时同步计数，超限即返回稳定413。"""

        if scope.get("type") != "http" or not self._is_attachment_upload(scope):
            await self.app(scope, receive, send)
            return
        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._send_too_large(send)
            return
        consumed = 0
        response_started = False

        async def bounded_receive() -> AsgiMessage:
            """累计真实ASGI请求块，不信任Content-Length存在或正确。"""

            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    raise _AttachmentBodyTooLarge
            return message

        async def tracked_send(message: AsgiMessage) -> None:
            """记录下游是否已发送响应头，防止异常路径产生第二个响应。"""

            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except _AttachmentBodyTooLarge:
            if response_started:
                raise
            await self._send_too_large(send)

    @staticmethod
    def _is_attachment_upload(scope: dict[str, Any]) -> bool:
        """只匹配正式附件POST入口，避免改变其他聊天或SSE请求。"""

        return scope.get("method") == "POST" and scope.get("path") == "/api/chat/attachments"

    @staticmethod
    def _content_length(scope: dict[str, Any]) -> int | None:
        """解析可选Content-Length用于廉价早拒；异常值按未知长度继续流式计数。"""

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
        return None

    async def _send_too_large(self, send: AsgiSend) -> None:
        """发送与FastAPI一致的JSON 413，不回显请求内容或内部预算。"""

        payload = json.dumps(
            {"detail": "本次上传请求体超过限制"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
