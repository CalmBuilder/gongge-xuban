"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : vertical_ability_connections.py
@CallChain  : React 网站接入向导 → 用户连接 API → connection_service → 网站 Provider/浏览器 Runtime
@Description: 为登录用户提供按网站选择连接方式、加密保存、验证和撤销个人网站连接的 HTTP 契约。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User, VerticalAbilityConnection
from app.security.auth import get_current_user
from app.security.dns_resolver import default_adaptive_resolver
from app.vertical_abilities.connection_service import (
    VerticalAbilityConnectionError,
    connection_requires_device_authorization,
    connection_status_message,
    connect_browser_session,
    list_user_connections,
    revoke_user_connection,
    save_user_connection,
    verify_user_connection,
)
from app.vertical_abilities.connection_settings import (
    ConnectionAuthMethod,
    ConnectionTransport,
    find_connection_definition,
    list_connection_definitions,
)


router = APIRouter(
    prefix="/api/chat/vertical-ability-connections",
    tags=["chat:vertical-ability-connections"],
)

ConnectionStatus = Literal["pending", "connected", "needs_reauth", "revoked", "not_connected"]
ConnectionHealthStatus = Literal["unverified", "healthy", "degraded", "unhealthy"]


class VerticalAbilityConnectionFieldRead(BaseModel):
    """向连接向导披露单个认证字段的安全元数据，不包含字段值。"""

    key: str
    label: str
    placeholder: str
    description: str
    secret: bool
    required: bool


class VerticalAbilityConnectionTransportRead(BaseModel):
    """向浏览器会话连接卡片披露可用的跨机器传输模式。"""

    transport: ConnectionTransport
    label: str
    description: str


class VerticalAbilityConnectionPlatformRead(BaseModel):
    """返回一个网站连接卡片及其可选认证方式。"""

    platform_id: str
    platform_name: str
    description: str
    capability_ids: list[str] = Field(default_factory=list)
    auth_methods: list[ConnectionAuthMethod] = Field(default_factory=list)
    auth_method_labels: dict[str, str] = Field(default_factory=dict)
    fields_by_method: dict[str, list[VerticalAbilityConnectionFieldRead]] = Field(default_factory=dict)
    browser_transports: list[VerticalAbilityConnectionTransportRead] = Field(default_factory=list)
    setup_url: str | None = None
    setup_link_label: str | None = None
    implementation_status: Literal["available", "adapter_pending"]
    implementation_message: str
    status: ConnectionStatus
    status_message: str
    connection_ids: list[str] = Field(default_factory=list)


class VerticalAbilityConnectionRead(BaseModel):
    """返回当前用户网站连接的脱敏投影，不序列化 Cookie、Token 或密码。"""

    id: str
    platform_id: str
    platform_name: str
    account_label: str
    account_identity: str | None = None
    auth_method: ConnectionAuthMethod
    transport: ConnectionTransport
    status: Literal["pending", "connected", "needs_reauth", "revoked"]
    health_status: ConnectionHealthStatus
    health_error_code: str | None = None
    has_credentials: bool
    revision: int
    last_checked_at: datetime | None = None
    last_healthy_at: datetime | None = None
    updated_at: datetime
    message: str
    verification_success: bool | None = None


class VerticalAbilityConnectionCatalogRead(BaseModel):
    """返回网站连接中心的全量平台卡片和当前用户连接状态。"""

    platforms: list[VerticalAbilityConnectionPlatformRead] = Field(default_factory=list)
    connections: list[VerticalAbilityConnectionRead] = Field(default_factory=list)


class VerticalAbilityConnectionUpsert(BaseModel):
    """接收一次网站连接凭据提交，敏感字段只在请求生命周期内使用。"""

    model_config = ConfigDict(extra="forbid")

    platform_id: str = Field(min_length=1, max_length=64)
    account_label: str = Field(default="默认账号", min_length=1, max_length=64)
    auth_method: ConnectionAuthMethod
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)


class VerticalAbilityBrowserSessionConnect(BaseModel):
    """请求连接当前设备已登录浏览器，不接收用户名、密码或浏览器 Cookie。"""

    model_config = ConfigDict(extra="forbid")

    platform_id: str = Field(min_length=1, max_length=64)
    account_label: str = Field(default="默认账号", min_length=1, max_length=64)
    expected_revision: int = Field(default=0, ge=0)


@router.get("", response_model=VerticalAbilityConnectionCatalogRead)
def read_vertical_ability_connections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConnectionCatalogRead:
    """返回当前用户可连接的网站清单和已保存连接状态，平台目录不按连接状态过滤。"""

    connections = list_user_connections(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )
    by_platform: dict[str, list[VerticalAbilityConnection]] = {}
    for row in connections:
        by_platform.setdefault(row.platform_id, []).append(row)
    return VerticalAbilityConnectionCatalogRead(
        platforms=[
            _platform_read(definition, by_platform.get(definition.platform_id, []))
            for definition in list_connection_definitions()
        ],
        connections=[_connection_read(row) for row in connections],
    )


@router.post("", response_model=VerticalAbilityConnectionRead, status_code=status.HTTP_201_CREATED)
def create_or_update_vertical_ability_connection(
    payload: VerticalAbilityConnectionUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConnectionRead:
    """保存当前用户选择的网站连接，并返回待验证状态而不回显任何敏感值。"""

    try:
        row = save_user_connection(
            db,
            current_user=current_user,
            platform_id=payload.platform_id,
            account_label=payload.account_label,
            auth_method=payload.auth_method,
            credentials={
                key: value.get_secret_value()
                for key, value in payload.credentials.items()
            },
            expected_revision=payload.expected_revision,
        )
    except VerticalAbilityConnectionError as exc:
        raise _connection_http_error(exc) from exc
    return _connection_read(row, message="连接信息已加密保存，请点击验证连接。")


@router.post("/browser-session", response_model=VerticalAbilityConnectionRead)
def connect_vertical_ability_browser_session(
    payload: VerticalAbilityBrowserSessionConnect,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConnectionRead:
    """复用当前用户已确认的设备连接，不将服务端浏览器的在线状态视为用户账号授权。"""

    try:
        row = connect_browser_session(
            db,
            current_user=current_user,
            platform_id=payload.platform_id,
            account_label=payload.account_label,
            expected_revision=payload.expected_revision,
        )
    except VerticalAbilityConnectionError as exc:
        raise _connection_http_error(exc) from exc
    return _connection_read(
        row, message="已连接当前用户授权的浏览器设备，首次读取时会再次检查网站登录态。",
        verification_success=True,
    )


@router.post("/{connection_id}/verify", response_model=VerticalAbilityConnectionRead)
def verify_vertical_ability_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConnectionRead:
    """验证当前用户的网站连接，暂不把未接入浏览器 Runtime 的保存状态伪装成健康。"""

    try:
        row = verify_user_connection(
            db,
            current_user=current_user,
            connection_id=connection_id,
            settings=get_settings(),
            resolver=default_adaptive_resolver(),
        )
    except VerticalAbilityConnectionError as exc:
        raise _connection_http_error(exc) from exc
    return _connection_read(row, verification_success=True)


@router.delete("/{connection_id}", response_model=VerticalAbilityConnectionRead)
def revoke_vertical_ability_connection(
    connection_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConnectionRead:
    """撤销当前用户网站连接并清空运行时可读取的密文。"""

    try:
        row = revoke_user_connection(
            db,
            current_user=current_user,
            connection_id=connection_id,
        )
    except VerticalAbilityConnectionError as exc:
        raise _connection_http_error(exc) from exc
    return _connection_read(row, message="网站连接已撤销。")


def _platform_read(
    definition,
    connections: list[VerticalAbilityConnection],
) -> VerticalAbilityConnectionPlatformRead:
    """按安全连接投影组合卡片，历史全局浏览器不能继续显示成已授权健康。"""

    methods = list(definition.auth_methods)
    connection_views = [_connection_read(item) for item in connections]
    states = [item.status for item in connection_views]
    if any(item.status == "connected" and item.health_status == "healthy" for item in connection_views):
        status_value: ConnectionStatus = "connected"
    elif "needs_reauth" in states:
        status_value = "needs_reauth"
    elif connections:
        status_value = "pending"
    else:
        status_value = "not_connected"
    if states and status_value == "not_connected":
        status_value = "pending"
    return VerticalAbilityConnectionPlatformRead(
        platform_id=definition.platform_id,
        platform_name=definition.platform_name,
        description=definition.description,
        capability_ids=list(definition.capability_ids),
        auth_methods=methods,
        auth_method_labels={method: _auth_method_label(method) for method in methods},
        fields_by_method={
            method: [
                VerticalAbilityConnectionFieldRead(
                    key=field.key,
                    label=field.label,
                    placeholder=field.placeholder,
                    description=field.description,
                    secret=field.secret,
                    required=field.required,
                )
                for field in definition.fields_for(method)
            ]
            for method in methods
        },
        browser_transports=(
            [
                VerticalAbilityConnectionTransportRead(
                    transport="local_loopback",
                    label="本机连接（local_loopback）",
                    description="浏览器和后端同机；仍须先经设备配对确认账号归属。",
                ),
                VerticalAbilityConnectionTransportRead(
                    transport="browser_relay",
                    label="宿主机连接（browser_relay）",
                    description="用户授权的浏览器设备连接；本机、VM 或远程部署均可使用。",
                ),
            ]
            if "browser_session" in methods
            else []
        ),
        setup_url=definition.setup_url,
        setup_link_label=definition.setup_link_label,
        implementation_status=definition.implementation_status,
        implementation_message=definition.implementation_message,
        status=status_value,
        status_message=connection_status_message(
            definition,
            connections[0] if connections else None,
        ),
        connection_ids=[item.id for item in connections],
    )


def _connection_read(
    row: VerticalAbilityConnection,
    *,
    message: str | None = None,
    verification_success: bool | None = None,
) -> VerticalAbilityConnectionRead:
    """生成无敏感值的连接安全投影；旧全局引用显示待重新授权但不隐式修改历史数据。"""

    definition = find_connection_definition(row.platform_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="网站连接类型不存在。")
    device_authorization_required = connection_requires_device_authorization(row)
    return VerticalAbilityConnectionRead(
        id=row.id,
        platform_id=row.platform_id,
        platform_name=definition.platform_name,
        account_label=row.account_label,
        account_identity=row.account_identity,
        auth_method=row.auth_method,
        transport=row.transport,
        status="needs_reauth" if device_authorization_required else row.status,
        health_status="unhealthy" if device_authorization_required else row.health_status,
        health_error_code=(
            "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
            if device_authorization_required else row.health_error_code
        ),
        has_credentials=row.status != "revoked" and bool(row.encrypted_credentials_json),
        revision=row.revision,
        last_checked_at=row.last_checked_at,
        last_healthy_at=row.last_healthy_at,
        updated_at=row.updated_at,
        message=message or connection_status_message(definition, row),
        verification_success=verification_success,
    )


def _auth_method_label(method: ConnectionAuthMethod) -> str:
    """将稳定认证方式键转换为用户可理解的连接方式名称。"""

    return {
        "api_token": "API Token",
        "cookie": "导入登录 Cookie",
        "browser_session": "连接已登录浏览器",
        "oauth": "官方授权（OAuth）",
    }[method]


def _connection_http_error(exc: VerticalAbilityConnectionError) -> HTTPException:
    """把领域连接错误转换为统一结构化 HTTP 错误。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )
