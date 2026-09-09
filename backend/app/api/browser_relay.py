"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : browser_relay.py
@CallChain  : 网站连接中心/宿主机连接器 → browser_relay HTTP API → Relay 服务
@Description: 暴露双拓扑设备配对与固定命令队列，本机连接器按 ASGI 请求来源校验。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.db import get_session
from app.db.models import User, VerticalAbilityBrowserRelaySession
from app.security.auth import get_current_user
from app.vertical_abilities.connection_settings import ConnectionTransport
from app.vertical_abilities.relay_service import (
    BrowserRelayError,
    RELAY_COMMAND_MAX_OUTPUT_BYTES,
    authenticate_relay_token,
    claim_next_relay_command,
    complete_relay_command,
    create_relay_pairing,
    get_user_relay_session,
    revoke_relay_session,
)


user_router = APIRouter(
    prefix="/api/chat/vertical-ability-connections/browser-relay",
    tags=["chat:vertical-ability-browser-relay"],
)
relay_router = APIRouter(
    prefix="/api/browser-relay",
    tags=["browser-ability-relay"],
)

RelaySessionStatus = Literal["pending", "connected", "disconnected", "revoked", "expired"]


class BrowserRelayPairingCreate(BaseModel):
    """接收用户网站设备配对的实际拓扑，未传模式时保持 browser_relay 兼容。"""

    model_config = ConfigDict(extra="forbid")

    platform_id: str = Field(min_length=1, max_length=64)
    account_label: str = Field(default="默认账号", min_length=1, max_length=64)
    expected_revision: int = Field(default=0, ge=0)
    transport: ConnectionTransport = "browser_relay"


class BrowserRelayPairingRead(BaseModel):
    """返回 Relay 会话状态；配对令牌只在创建成功的响应中出现一次。"""

    session_id: str
    platform_id: str
    account_label: str
    transport: ConnectionTransport
    status: RelaySessionStatus
    pairing_token: str | None = None
    expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    connected_at: datetime | None = None
    pairing_token_available: bool
    message: str


class BrowserRelayCommandResult(BaseModel):
    """接收宿主机 OpenCLI 的单次有界文本回执，不接收 Cookie 或任意命令。"""

    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=1, max_length=256)
    success: bool
    output: str = Field(default="", max_length=RELAY_COMMAND_MAX_OUTPUT_BYTES)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=1024)


@user_router.post("/pairings", response_model=BrowserRelayPairingRead, status_code=status.HTTP_201_CREATED)
def create_browser_relay_pairing(
    payload: BrowserRelayPairingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BrowserRelayPairingRead:
    """按用户选择的拓扑创建待确认配对，创建设备令牌本身不建立浏览器访问授权。"""

    try:
        session, token = create_relay_pairing(
            db,
            current_user=current_user,
            platform_id=payload.platform_id,
            account_label=payload.account_label,
            expected_revision=payload.expected_revision,
            transport=payload.transport,
        )
    except BrowserRelayError as exc:
        raise _relay_http_error(exc) from exc
    return _pairing_read(session, pairing_token=token)


@user_router.get("/pairings/{session_id}", response_model=BrowserRelayPairingRead)
def read_browser_relay_pairing(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BrowserRelayPairingRead:
    """读取当前用户 Relay 会话状态，不能跨租户查看宿主机在线状态。"""

    try:
        session = get_user_relay_session(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            session_id=session_id,
        )
    except BrowserRelayError as exc:
        raise _relay_http_error(exc) from exc
    return _pairing_read(session)


@user_router.delete("/pairings/{session_id}", response_model=BrowserRelayPairingRead)
def delete_browser_relay_pairing(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> BrowserRelayPairingRead:
    """撤销当前用户的 Relay 配对令牌和远程浏览器命令入口。"""

    try:
        session = revoke_relay_session(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            session_id=session_id,
        )
    except BrowserRelayError as exc:
        raise _relay_http_error(exc) from exc
    return _pairing_read(session)


@relay_router.get("/commands/next", response_model=None)
def claim_browser_relay_command(
    request: Request,
    wait_seconds: float = Query(default=5.0, ge=0.0, le=10.0),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> dict[str, object] | Response:
    """校验连接器真实请求来源后轮询固定命令，本机模式拒绝远端领取及伪造头。"""

    token = _bearer_token(authorization)
    deadline = time.monotonic() + min(wait_seconds, 10.0)
    while True:
        try:
            command = claim_next_relay_command(
                db, pairing_token=token, peer_host=_connector_peer_host(request),
            )
        except BrowserRelayError as exc:
            raise _relay_http_error(exc) from exc
        if command is not None:
            return command
        if time.monotonic() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        time.sleep(0.2)


@relay_router.post("/commands/{command_id}/result")
def complete_browser_relay_command(
    command_id: str,
    payload: BrowserRelayCommandResult,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    """在更新心跳或命令前校验本机请求来源，再保存成功或结构化失败回执。"""

    token = _bearer_token(authorization)
    try:
        command = complete_relay_command(
            db,
            pairing_token=token,
            command_id=command_id,
            lease_token=payload.lease_token,
            success=payload.success,
            output=payload.output,
            error_code=payload.error_code,
            error_message=payload.error_message,
            peer_host=_connector_peer_host(request),
        )
    except BrowserRelayError as exc:
        raise _relay_http_error(exc) from exc
    return {"command_id": command.id, "status": command.status}


@relay_router.post("/heartbeat")
def browser_relay_heartbeat(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    """由实际浏览器设备确认配对并刷新心跳，本机模式要求请求来源是 loopback。"""

    token = _bearer_token(authorization)
    try:
        session = authenticate_relay_token(db, token, peer_host=_connector_peer_host(request))
    except BrowserRelayError as exc:
        raise _relay_http_error(exc) from exc
    return {"session_id": session.id, "status": session.status}


def _pairing_read(
    session: VerticalAbilityBrowserRelaySession, *, pairing_token: str | None = None,
) -> BrowserRelayPairingRead:
    """按持久化的真实拓扑返回配对卡片，默认不返回可重放令牌。"""

    device_label = "本机浏览器" if session.transport == "local_loopback" else "宿主机浏览器"
    if session.status == "pending":
        message = (
            "请在后端所在的同一台设备启动连接器，连接本机服务地址并粘贴此配对令牌。"
            if session.transport == "local_loopback"
            else "请在浏览器所在宿主机启动连接器并粘贴此配对令牌。"
        )
    elif session.status == "connected":
        message = f"{device_label}连接器已在线，可执行对应网站读取。"
    elif session.status == "disconnected":
        message = f"{device_label}连接器暂时离线，重新启动连接器即可恢复。"
    elif session.status == "expired":
        message = "配对令牌已过期，请重新创建 Relay 配对。"
    else:
        message = "Relay 配对已撤销。"
    return BrowserRelayPairingRead(
        session_id=session.id,
        platform_id=session.platform_id,
        account_label=session.account_label,
        transport=session.transport,
        status=session.status,
        pairing_token=pairing_token,
        expires_at=session.expires_at,
        last_seen_at=session.last_seen_at,
        connected_at=session.connected_at,
        pairing_token_available=bool(pairing_token),
        message=message,
    )


def _connector_peer_host(request: Request) -> str | None:
    """只取 ASGI client 地址，不从请求参数或 XFF/Forwarded 头自行推断设备来源。"""

    # Uvicorn ProxyHeadersMiddleware 可改写 client；部署只能信任会正确覆盖并传递
    # 真实来源的反代，不能设 trusted_hosts='*' 或让本地反代隐藏公网请求来源。
    return request.client.host if request.client is not None else None


def _bearer_token(authorization: str | None) -> str:
    """从宿主机连接器请求头提取配对令牌，不接受查询参数中的长期秘密。"""

    value = str(authorization or "").strip()
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
                "message": "浏览器 Relay 配对令牌无效或已过期。",
            },
        )
    return token.strip()


def _relay_http_error(exc: BrowserRelayError) -> HTTPException:
    """把 Relay 领域错误转换为统一的结构化 HTTP 错误。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )
