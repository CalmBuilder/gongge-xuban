"""
@Time       : 2026/08/15 07:24
@Author     : zhanglp8181
@File       : test_bounded_attachment_body.py
@CallChain  : pytest → raw ASGI receive chunks → BoundedAttachmentBodyMiddleware
@Description: 验证附件multipart在框架spool前按真实请求块限流，且不影响其他入口。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from collections.abc import Iterable

import uvicorn

from app.security.bounded_request_body import BoundedAttachmentBodyMiddleware


def _scope(*, path: str = "/api/chat/attachments", headers=()) -> dict[str, object]:
    """构造不依赖HTTP客户端自动补Content-Length的原始ASGI请求scope。"""

    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": list(headers),
    }


def _run(
    chunks: Iterable[bytes],
    *,
    limit: int,
    scope: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[bytes]]:
    """执行中间件并返回响应消息及真正抵达下游的请求块。"""

    queued = list(chunks)
    received: list[bytes] = []
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        """逐块模拟无Content-Length的chunked上传。"""

        body = queued.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queued)}

    async def send(message: dict[str, object]) -> None:
        """收集ASGI响应消息供机械断言。"""

        sent.append(message)

    async def downstream(
        _scope: dict[str, object],
        bounded_receive,
        downstream_send,
    ) -> None:
        """模拟multipart parser持续读取，证明超限块不会继续进入下游。"""

        while True:
            message = await bounded_receive()
            received.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        await downstream_send({"type": "http.response.start", "status": 204, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b""})

    asyncio.run(
        BoundedAttachmentBodyMiddleware(downstream, max_body_bytes=limit)(
            scope or _scope(), receive, send
        )
    )
    return sent, received


def test_chunked_upload_without_content_length_stops_before_unbounded_spool() -> None:
    """无Content-Length时累计真实chunk，超过硬上限立即413且超限块不交给multipart。"""

    sent, received = _run([b"1234", b"5678", b"9", b"late"], limit=8)

    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"] == "本次上传请求体超过限制"
    assert received == [b"1234", b"5678"]


def test_content_length_above_limit_is_rejected_before_downstream_read() -> None:
    """可信与否不影响安全性，但明显超限Content-Length应在读取任何body前廉价拒绝。"""

    sent, received = _run(
        [b"never-read"],
        limit=8,
        scope=_scope(headers=((b"content-length", b"9"),)),
    )

    assert sent[0]["status"] == 413
    assert received == []


def test_under_limit_and_non_attachment_requests_are_unchanged() -> None:
    """合法附件和其他POST入口保持下游原语义，避免全局请求体行为回退。"""

    attachment_sent, attachment_received = _run([b"1234", b"5678"], limit=8)
    other_sent, other_received = _run(
        [b"123456789"],
        limit=8,
        scope=_scope(path="/api/chat/sessions/session-1/turns"),
    )

    assert attachment_sent[0]["status"] == 204
    assert attachment_received == [b"1234", b"5678"]
    assert other_sent[0]["status"] == 204
    assert other_received == [b"123456789"]


def test_real_http_chunked_upload_without_content_length_is_rejected() -> None:
    """经真实Uvicorn socket发送chunked请求，确认413发生在下游累计超限块之前。"""

    downstream_bytes: list[bytes] = []

    async def downstream(_scope, receive, send) -> None:
        """模拟框架multipart层，记录实际获准进入下游的请求块。"""

        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            downstream_bytes.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BoundedAttachmentBodyMiddleware(downstream, max_body_bytes=8)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="off", access_log=False)
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", "/api/chat/attachments")
        connection.putheader("Content-Type", "multipart/form-data; boundary=real-boundary")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"8\r\n12345678\r\n")
        connection.send(b"1\r\n9\r\n")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 413
        assert payload["detail"] == "本次上传请求体超过限制"
        delivered = b"".join(downstream_bytes)
        assert len(delivered) <= 8
        assert b"12345678".startswith(delivered)
    finally:
        connection.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()
