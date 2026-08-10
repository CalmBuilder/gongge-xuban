"""
@Time       : 2026/08/10 15:05
@Author     : zhanglp8181
@File       : wecom_callbacks.py
@CallChain  : 企业微信公网回调 → FastAPI → WeComInboundService → 持久 inbox
@Description: 暴露无需登录但强制档案密钥验签的企业微信 URL 验证和消息接收接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from app.connectors.wecom_callback import WECOM_CALLBACK_MAX_BODY_BYTES
from app.connectors.wecom_inbound import WeComInboundError, WeComInboundService, encrypted_element
from app.db import get_session


router = APIRouter(prefix="/api/connectors/wecom", tags=["connectors:wecom-callback"])


@router.get("/{profile_id}/callback", response_class=PlainTextResponse)
def verify_wecom_callback(
    profile_id: str,
    msg_signature: str = Query(..., min_length=40, max_length=40),
    timestamp: str = Query(..., min_length=1, max_length=32),
    nonce: str = Query(..., min_length=1, max_length=128),
    echostr: str = Query(..., min_length=1, max_length=8192),
    db: Session = Depends(get_session),
) -> PlainTextResponse:
    """完成企业微信首次保存回调 URL 时的加密 echo 握手。"""

    try:
        echo = WeComInboundService(db).verify_url(
            profile_id=profile_id,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            echo_str=echostr,
        )
    except WeComInboundError as exc:
        raise _callback_http_error(exc) from exc
    return PlainTextResponse(echo)


@router.post("/{profile_id}/callback", response_class=PlainTextResponse)
async def receive_wecom_callback(
    profile_id: str,
    request: Request,
    msg_signature: str = Query(..., min_length=40, max_length=40),
    timestamp: str = Query(..., min_length=1, max_length=32),
    nonce: str = Query(..., min_length=1, max_length=128),
    db: Session = Depends(get_session),
) -> PlainTextResponse:
    """仅在企业微信事件已验签、解密并持久化后返回 success。"""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > WECOM_CALLBACK_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="WECOM_CALLBACK_PAYLOAD_TOO_LARGE")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="WECOM_CALLBACK_PAYLOAD_INVALID") from exc
    body = await request.body()
    if len(body) > WECOM_CALLBACK_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="WECOM_CALLBACK_PAYLOAD_TOO_LARGE")
    try:
        encrypted = encrypted_element(body)
        WeComInboundService(db).receive(
            profile_id=profile_id,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=encrypted,
        )
        db.commit()
    except WeComInboundError as exc:
        db.rollback()
        raise _callback_http_error(exc) from exc
    return PlainTextResponse("success")


def _callback_http_error(error: WeComInboundError) -> HTTPException:
    """将公开回调错误映射为最小响应，避免泄漏档案和密钥状态。"""

    if error.code == "WECOM_CALLBACK_NOT_FOUND":
        return HTTPException(status_code=404, detail="WECOM_CALLBACK_NOT_FOUND")
    if error.code == "WECOM_CALLBACK_EVENT_ID_CONFLICT":
        return HTTPException(status_code=409, detail=error.code)
    return HTTPException(status_code=403, detail="WECOM_CALLBACK_INVALID")
