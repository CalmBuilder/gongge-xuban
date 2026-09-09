"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : connection_service.py
@CallChain  : 网站连接 API → VerticalAbilityConnection → 加密凭据/健康探测 → 垂直能力执行器
@Description: 提供用户级网站连接的加密保存、设备授权绑定、撤销和运行时凭据隔离。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.db.models import (
    User, VerticalAbilityBrowserRelaySession, VerticalAbilityConnection, new_id, utc_now,
)
from app.security.encryption import decrypt_secret, encrypt_secret
from app.security.outbound import allowed_hosts_from_settings, prepare_outbound_request
from app.config import Settings
from app.security.dns_resolver import AdaptiveDNSResolver
from app.vertical_abilities.connection_settings import (
    ConnectionAuthMethod,
    ConnectionTransport,
    VerticalAbilityConnectionDefinition,
    find_connection_definition,
)


CONNECTION_CREDENTIAL_MAX_BYTES = 32 * 1024
CONNECTION_FIELD_MAX_CHARS = 16 * 1024
_GITHUB_API_URL = "https://api.github.com/user"
_GITHUB_API_HOST = "api.github.com"


class VerticalAbilityConnectionError(ValueError):
    """表示网站连接输入、归属、并发版本或健康探测失败。"""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        """保存稳定错误码、用户提示和 HTTP 状态码。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def list_user_connections(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> list[VerticalAbilityConnection]:
    """只读取当前租户当前用户的网站连接，不允许跨用户或跨租户查询。"""

    return list(
        db.exec(
            select(VerticalAbilityConnection)
            .where(
                VerticalAbilityConnection.tenant_id == tenant_id,
                VerticalAbilityConnection.user_id == user_id,
                VerticalAbilityConnection.status != "revoked",
            )
            .order_by(VerticalAbilityConnection.platform_id, VerticalAbilityConnection.account_label)
        ).all()
    )


def get_user_connection(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    connection_id: str,
) -> VerticalAbilityConnection:
    """按连接 ID 读取当前用户连接，并在归属不一致时统一返回不存在。"""

    row = db.get(VerticalAbilityConnection, connection_id)
    if (
        row is None
        or row.tenant_id != tenant_id
        or row.user_id != user_id
        or row.status == "revoked"
    ):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_NOT_FOUND",
            "网站连接不存在或已撤销。",
            status_code=404,
        )
    return row


def save_user_connection(
    db: Session,
    *,
    current_user: User,
    platform_id: str,
    account_label: str,
    auth_method: str,
    credentials: Mapping[str, str],
    expected_revision: int = 0,
    transport: ConnectionTransport = "local_loopback",
    commit: bool = True,
) -> VerticalAbilityConnection:
    """校验后加密保存；Relay 可延迟提交，使连接、审计与设备授权在同一事务内生效。"""

    definition = find_connection_definition(platform_id)
    if definition is None:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PLATFORM_NOT_FOUND",
            "网站连接类型不存在。",
            status_code=404,
        )
    normalized_method = str(auth_method or "").strip().casefold()
    if normalized_method not in definition.auth_methods:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_AUTH_METHOD_INVALID",
            f"{definition.platform_name} 不支持该连接方式。",
        )
    normalized_transport = str(transport or "").strip().casefold()
    if normalized_transport not in {"local_loopback", "browser_relay"}:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_TRANSPORT_INVALID",
            "网站连接传输模式无效。",
        )
    if normalized_method != "browser_session" and normalized_transport != "local_loopback":
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_TRANSPORT_INVALID",
            "只有浏览器会话连接可以使用宿主机 Relay。",
        )
    normalized_label = str(account_label or "默认账号").strip() or "默认账号"
    if len(normalized_label) > 64:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_ACCOUNT_LABEL_INVALID",
            "账号名称不能超过 64 个字符。",
        )
    normalized_credentials = _validate_credentials(
        definition,
        normalized_method,  # type: ignore[arg-type]
        credentials,
    )
    if normalized_method == "browser_session":
        _validate_device_pairing_reference(
            db, current_user=current_user, platform_id=definition.platform_id,
            account_label=normalized_label, transport=normalized_transport,
            credentials=normalized_credentials, expected_revision=expected_revision,
        )
    encrypted_payload = _encrypt_credentials(normalized_credentials)
    row = db.exec(
        select(VerticalAbilityConnection)
        .where(
            VerticalAbilityConnection.tenant_id == current_user.tenant_id,
            VerticalAbilityConnection.user_id == current_user.id,
            VerticalAbilityConnection.platform_id == definition.platform_id,
            VerticalAbilityConnection.account_label == normalized_label,
        )
        .with_for_update()
    ).first()
    if row is not None and row.status != "revoked" and row.revision != expected_revision:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            f"{definition.platform_name} 连接已被更新，请刷新后重试。",
            status_code=409,
        )
    if row is None and expected_revision != 0:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            f"{definition.platform_name} 连接状态已变化，请刷新后重试。",
            status_code=409,
        )
    if row is not None and row.status == "revoked" and expected_revision != 0:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            f"{definition.platform_name} 连接状态已变化，请刷新后重试。",
            status_code=409,
        )

    now = utc_now()
    if row is None:
        row = VerticalAbilityConnection(
            id=new_id("vaconnection"),
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            platform_id=definition.platform_id,
            account_label=normalized_label,
            auth_method=normalized_method,
            transport=normalized_transport,
            encrypted_credentials_json=encrypted_payload,
            revision=1,
            status="pending",
            health_status="unverified",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        action_kind = "create"
        before: dict[str, object] = {}
    else:
        before = _audit_view(row)
        row.auth_method = normalized_method
        row.transport = normalized_transport
        row.encrypted_credentials_json = encrypted_payload
        row.revision += 1
        row.status = "pending"
        row.health_status = "unverified"
        row.health_error_code = None
        row.account_identity = None
        row.last_checked_at = None
        row.last_healthy_at = None
        row.updated_at = now
        action_kind = "update"

    db.flush()
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action=f"vertical_ability.connection.{definition.platform_id}.save",
        action_kind=action_kind,
        outcome="success",
        resource_type="vertical_ability_connection",
        resource_id=row.id,
        before=before,
        after=_audit_view(row),
    )
    if commit:
        db.commit()
        db.refresh(row)
    return row


def verify_user_connection(
    db: Session,
    *,
    current_user: User,
    connection_id: str,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> VerticalAbilityConnection:
    """验证凭据健康和设备实际授权，旧全局浏览器引用不能借在线探测升级成有效连接。"""

    row = get_user_connection(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    definition = find_connection_definition(row.platform_id)
    if definition is None:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PLATFORM_NOT_FOUND",
            "网站连接类型不存在。",
            status_code=404,
        )
    credentials = decrypt_connection_credentials(row)
    if row.platform_id == "github" and row.auth_method == "api_token":
        identity = _probe_github_token(credentials, settings=settings, resolver=resolver)
        _mark_healthy(row, account_identity=identity)
        message = f"{definition.platform_name} 连接验证成功。"
    elif row.auth_method == "browser_session":
        if not connection_requires_device_authorization(row):
            try:
                relay_session = _require_relay_session_for_connection(db, row, current_user)
            except VerticalAbilityConnectionError as exc:
                row.last_checked_at = utc_now()
                row.health_status = "unhealthy"
                row.health_error_code = exc.code
                row.status = "pending"
                db.commit()
                raise
            device_label = "本机浏览器" if row.transport == "local_loopback" else "宿主机浏览器 Relay"
            _mark_healthy(row, account_identity=f"{device_label} · {relay_session.platform_id}")
            message = f"{definition.platform_name} {device_label}已验证。"
        else:
            row.last_checked_at = utc_now()
            row.health_status = "unhealthy"
            row.health_error_code = "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED"
            row.status = "needs_reauth"
            db.commit()
            raise _device_authorization_required()
    elif row.auth_method == "cookie":
        identity = _probe_cookie_connection(
            row.platform_id,
            credentials,
            settings=settings,
            resolver=resolver,
        )
        _mark_healthy(row, account_identity=identity)
        message = f"{definition.platform_name} Cookie 连接验证成功，后续请求只在当前用户执行中使用。"
    else:
        row.last_checked_at = utc_now()
        row.health_status = "unverified"
        row.health_error_code = "VERTICAL_ABILITY_CONNECTION_RUNTIME_PENDING"
        row.status = "pending"
        db.commit()
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_RUNTIME_PENDING",
            f"{definition.platform_name} 凭据已保存，但该连接方式需要对应浏览器/平台适配器完成验证。",
            status_code=501,
        )
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action=f"vertical_ability.connection.{definition.platform_id}.verify",
        action_kind="read",
        outcome="success",
        resource_type="vertical_ability_connection",
        resource_id=row.id,
        after={**_audit_view(row), "message": message},
    )
    db.commit()
    db.refresh(row)
    return row


def connect_browser_session(
    db: Session,
    *,
    current_user: User,
    platform_id: str,
    account_label: str,
    expected_revision: int = 0,
) -> VerticalAbilityConnection:
    """复用当前用户已由设备确认的配对；服务端全局浏览器在线本身不构成账号授权。"""

    definition = find_connection_definition(platform_id)
    if definition is None or "browser_session" not in definition.auth_methods:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_BROWSER_PLATFORM_UNSUPPORTED",
            "该网站暂未提供浏览器会话连接方式。",
            status_code=422,
        )
    row = db.exec(
        select(VerticalAbilityConnection).where(
            VerticalAbilityConnection.tenant_id == current_user.tenant_id,
            VerticalAbilityConnection.user_id == current_user.id,
            VerticalAbilityConnection.platform_id == definition.platform_id,
            VerticalAbilityConnection.account_label == (str(account_label or "").strip() or "默认账号"),
            VerticalAbilityConnection.auth_method == "browser_session",
            VerticalAbilityConnection.transport.in_(("local_loopback", "browser_relay")),
            VerticalAbilityConnection.status == "connected",
        ).execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise _device_authorization_required()
    if row.revision != expected_revision:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            "浏览器连接已更新，请刷新连接状态后重试。", status_code=409,
        )
    _require_relay_session_for_connection(db, row, current_user)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action=f"vertical_ability.connection.{definition.platform_id}.browser_session",
        action_kind="read",
        outcome="success",
        resource_type="vertical_ability_connection",
        resource_id=row.id,
        after=_audit_view(row),
    )
    db.commit()
    db.refresh(row)
    return row


def revoke_user_connection(
    db: Session,
    *,
    current_user: User,
    connection_id: str,
) -> VerticalAbilityConnection:
    """撤销当前用户连接并清空可用于运行时读取的密文。"""

    row = get_user_connection(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    before = _audit_view(row)
    row.encrypted_credentials_json = _encrypt_credentials({})
    row.revision += 1
    row.status = "revoked"
    row.health_status = "unhealthy"
    row.health_error_code = "VERTICAL_ABILITY_CONNECTION_REVOKED"
    row.last_checked_at = utc_now()
    row.updated_at = utc_now()
    from app.vertical_abilities.relay_service import revoke_relay_sessions_for_connection

    revoke_relay_sessions_for_connection(db, row.id)
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action=f"vertical_ability.connection.{row.platform_id}.revoke",
        action_kind="delete",
        outcome="success",
        resource_type="vertical_ability_connection",
        resource_id=row.id,
        before=before,
        after=_audit_view(row),
    )
    db.commit()
    db.refresh(row)
    return row


def decrypt_connection_credentials(row: VerticalAbilityConnection) -> dict[str, str]:
    """仅在服务端适配器调用边界解密用户连接，并过滤为字符串键值。"""

    try:
        payload = json.loads(decrypt_secret(row.encrypted_credentials_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_SECRET_INVALID",
            "网站连接密文无法安全读取，请重新连接。",
        ) from exc
    if not isinstance(payload, dict):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_SECRET_INVALID",
            "网站连接密文格式无效，请重新连接。",
        )
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def resolve_user_connection_credentials(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> dict[str, dict[str, str]]:
    """只提供当前用户已验证凭据；浏览器还须保有有效设备授权，不影响无关公共渠道。"""

    rows = db.exec(
        select(VerticalAbilityConnection)
        .where(
            VerticalAbilityConnection.tenant_id == tenant_id,
            VerticalAbilityConnection.user_id == user_id,
            VerticalAbilityConnection.status == "connected",
            VerticalAbilityConnection.health_status == "healthy",
        )
        .order_by(VerticalAbilityConnection.platform_id, VerticalAbilityConnection.updated_at.desc())
        .execution_options(populate_existing=True)
    ).all()
    resolved: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.platform_id in resolved:
            continue
        if row.auth_method == "browser_session":
            if connection_requires_device_authorization(row):
                continue
            owner = db.get(User, user_id)
            if owner is None or owner.tenant_id != tenant_id:
                continue
            try:
                _require_relay_session_for_connection(db, row, owner)
            except VerticalAbilityConnectionError:
                continue
        resolved[row.platform_id] = decrypt_connection_credentials(row)
    return resolved


def connection_status_message(
    definition: VerticalAbilityConnectionDefinition,
    row: VerticalAbilityConnection | None,
) -> str:
    """把连接状态转换为前端可执行的下一步提示，不泄露密文或内部路径。"""

    if row is None:
        return f"尚未连接{definition.platform_name}，可选择对应网站连接方式。"
    if connection_requires_device_authorization(row):
        return _device_authorization_required().message
    if row.status == "connected" and row.health_status == "healthy":
        transport_label = "宿主机浏览器 Relay" if row.transport == "browser_relay" else "本机浏览器"
        return f"{definition.platform_name} 已连接（{transport_label}），可在授权范围内使用。"
    if row.status == "needs_reauth":
        return f"{definition.platform_name} 登录态已失效，请重新连接。"
    if row.auth_method == "browser_session":
        if row.transport == "browser_relay":
            return f"{definition.platform_name} 已保存宿主机浏览器 Relay，等待连接器在线。"
        return f"{definition.platform_name} 已保存本机浏览器会话引用，等待桌面连接器验证。"
    return f"{definition.platform_name} 连接已保存，点击验证后才能用于对应平台读取。"


def connection_requires_device_authorization(row: VerticalAbilityConnection) -> bool:
    """区分历史裸浏览器引用与双拓扑配对引用；真正会话归属仍由运行时再次校验。"""

    if row.status == "revoked" or row.auth_method != "browser_session":
        return False
    if row.transport not in {"local_loopback", "browser_relay"}:
        return True
    try:
        reference = decrypt_connection_credentials(row).get("session_reference", "")
    except VerticalAbilityConnectionError:
        return True
    return not reference.startswith("relay:") or len(reference) <= len("relay:")


def _validate_credentials(
    definition: VerticalAbilityConnectionDefinition,
    auth_method: ConnectionAuthMethod,
    credentials: Mapping[str, str],
) -> dict[str, str]:
    """按平台允许字段和认证方式校验连接输入，拒绝任意 URL、命令和未知密钥注入。"""

    if not isinstance(credentials, Mapping):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID",
            "网站连接凭据格式无效。",
        )
    fields = {field.key: field for field in definition.fields_for(auth_method)}
    unknown = set(credentials) - set(fields)
    if unknown:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID",
            "网站连接包含未支持的凭据字段。",
        )
    normalized: dict[str, str] = {}
    for key, raw_value in credentials.items():
        value = str(raw_value or "").strip()
        if not value:
            continue
        if len(value) > CONNECTION_FIELD_MAX_CHARS:
            raise VerticalAbilityConnectionError(
                "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID",
                "网站连接凭据字段超过长度上限。",
            )
        normalized[key] = value
    if auth_method == "oauth":
        if normalized:
            raise VerticalAbilityConnectionError(
                "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID",
                "OAuth 连接不接受手工凭据字段。",
            )
        return normalized
    if auth_method == "api_token" and not normalized.get("access_token"):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED",
            f"{definition.platform_name} 需要填写访问 Token。",
        )
    if auth_method == "browser_session" and not normalized.get("session_reference"):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED",
            f"{definition.platform_name} 需要桌面连接器提供浏览器会话引用。",
        )
    if auth_method == "cookie":
        if definition.platform_id == "twitter":
            valid = bool(normalized.get("cookie")) or bool(
                normalized.get("auth_token") and normalized.get("ct0")
            )
        elif definition.platform_id == "reddit":
            valid = bool(normalized.get("cookie") or normalized.get("reddit_session"))
        else:
            valid = bool(normalized.get("cookie"))
        if not valid:
            raise VerticalAbilityConnectionError(
                "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED",
                f"{definition.platform_name} 需要填写有效的 Cookie 或登录态字段。",
            )
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > CONNECTION_CREDENTIAL_MAX_BYTES:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_INVALID",
            "网站连接凭据总大小超过安全上限。",
        )
    return normalized


def _encrypt_credentials(credentials: Mapping[str, str]) -> str:
    """将已完成字段白名单校验的连接凭据整体加密，不产生可搜索明文。"""

    payload = json.dumps(credentials, ensure_ascii=False, separators=(",", ":"))
    return encrypt_secret(payload)


def _probe_github_token(
    credentials: Mapping[str, str],
    *,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> str:
    """调用 GitHub 官方用户接口验证 Token，并只返回公开账户标识。"""

    token = str(credentials.get("access_token") or "").strip()
    if not token:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED",
            "GitHub Token 未配置。",
        )
    try:
        target = prepare_outbound_request(
            _GITHUB_API_URL,
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | {_GITHUB_API_HOST},
            resolver=resolver,
        )
        with httpx.Client(timeout=min(float(settings.tool_timeout_seconds), 20.0)) as client:
            response = client.get(
                target.request_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    **target.headers,
                },
                **({"extensions": target.extensions} if target.extensions else {}),
            )
            if response.status_code in {401, 403}:
                raise VerticalAbilityConnectionError(
                    "VERTICAL_ABILITY_CONNECTION_REAUTH_REQUIRED",
                    "GitHub Token 无效或权限不足，请重新连接。",
                    status_code=422,
                )
            response.raise_for_status()
            if len(response.content) > 512 * 1024:
                raise VerticalAbilityConnectionError(
                    "VERTICAL_ABILITY_CONNECTION_RESPONSE_TOO_LARGE",
                    "GitHub 连接验证响应超过安全上限。",
                )
            payload = response.json()
    except VerticalAbilityConnectionError:
        raise
    except httpx.TimeoutException as exc:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_TIMEOUT",
            "GitHub 连接验证超时，请稍后重试。",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_UNAVAILABLE",
            "GitHub 连接验证失败，请检查网络或 Token。",
        ) from exc
    if not isinstance(payload, dict):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_RESPONSE_INVALID",
            "GitHub 连接验证返回格式无效。",
        )
    login = str(payload.get("login") or "").strip()
    if not login or len(login) > 191:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_RESPONSE_INVALID",
            "GitHub 连接验证未返回账户标识。",
        )
    return f"@{login}"


def _probe_cookie_connection(
    platform_id: str,
    credentials: Mapping[str, str],
    *,
    settings: Settings,
    resolver: AdaptiveDNSResolver,
) -> str:
    """请求各平台固定身份/首页接口验证 Cookie，不读取浏览器也不共享 Cookie Jar。"""

    endpoints = {
        "twitter": ("https://x.com/home", {"x.com"}),
        "reddit": ("https://www.reddit.com/api/me.json?raw_json=1", {"www.reddit.com"}),
        "xueqiu": ("https://xueqiu.com/", {"xueqiu.com"}),
    }
    endpoint = endpoints.get(platform_id)
    if endpoint is None:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_RUNTIME_PENDING",
            "该平台 Cookie 连接尚未提供安全的在线验证接口，请连接已登录浏览器。",
            status_code=501,
        )
    cookie = str(credentials.get("cookie") or "").strip()
    if not cookie and platform_id == "reddit":
        cookie = f"reddit_session={str(credentials.get('reddit_session') or '').strip()}"
    if platform_id == "twitter" and not cookie:
        cookie = "; ".join(
            f"{key}={str(credentials.get(key) or '').strip()}"
            for key in ("auth_token", "ct0")
            if str(credentials.get(key) or "").strip()
        )
    if not cookie:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_CREDENTIALS_REQUIRED",
            "Cookie 连接缺少有效的登录 Cookie。",
        )
    try:
        target = prepare_outbound_request(
            endpoint[0],
            allowed_hosts=set(allowed_hosts_from_settings(settings)) | endpoint[1],
            resolver=resolver,
        )
        raw_timeout = getattr(settings, "tool_timeout_seconds", 20.0)
        try:
            timeout_seconds = min(float(raw_timeout or 20.0), 20.0)
        except (TypeError, ValueError):
            timeout_seconds = 20.0
        with httpx.Client(
            timeout=max(timeout_seconds, 1.0),
            follow_redirects=False,
        ) as client:
            response = client.get(
                target.request_url,
                headers={
                    "Accept": "text/html, application/json",
                    "Cookie": cookie,
                    "User-Agent": "gongge-xuban/vertical-ability",
                    **target.headers,
                },
                **({"extensions": target.extensions} if target.extensions else {}),
            )
    except httpx.TimeoutException as exc:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_TIMEOUT",
            f"{platform_id} Cookie 连接验证超时，请稍后重试。",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_UNAVAILABLE",
            f"{platform_id} Cookie 连接验证失败，请检查网络和登录态。",
        ) from exc
    if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_REAUTH_REQUIRED",
            f"{platform_id} Cookie 已失效或未登录，请重新导出登录态。",
        )
    if response.status_code >= 400:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_PROVIDER_HTTP_ERROR",
            f"{platform_id} 连接验证返回异常状态码：{response.status_code}。",
        )
    if len(response.content) > 512 * 1024:
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_CONNECTION_RESPONSE_TOO_LARGE",
            f"{platform_id} 连接验证响应超过安全上限。",
        )
    identity = f"{platform_id} Cookie"
    if platform_id == "reddit":
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("name"):
            identity = f"@{str(payload['name'])[:191]}"
    return identity


def _mark_healthy(row: VerticalAbilityConnection, *, account_identity: str) -> None:
    """把已经完成真实探测的连接更新为健康状态。"""

    now = utc_now()
    row.account_identity = account_identity
    row.status = "connected"
    row.health_status = "healthy"
    row.health_error_code = None
    row.last_checked_at = now
    row.last_healthy_at = now
    row.updated_at = now


def _audit_view(row: VerticalAbilityConnection) -> dict[str, object]:
    """生成不含密文的连接审计投影。"""

    return {
        "platform_id": row.platform_id,
        "account_label": row.account_label,
        "account_identity": row.account_identity,
        "auth_method": row.auth_method,
        "transport": row.transport,
        "revision": row.revision,
        "status": row.status,
        "health_status": row.health_status,
    }


def _require_relay_session_for_connection(
    db: Session,
    row: VerticalAbilityConnection,
    current_user: User,
) -> VerticalAbilityBrowserRelaySession:
    """从浏览器会话密文解析 Relay 引用，并再次核对租户、用户和平台归属。"""

    reference = decrypt_connection_credentials(row).get("session_reference", "")
    if not reference.startswith("relay:"):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_RELAY_SESSION_INVALID",
            "浏览器 Relay 会话引用无效，请重新配对。",
            status_code=503,
        )
    from app.vertical_abilities.relay_service import (
        BrowserRelayError,
        require_live_relay_session,
    )

    try:
        session = require_live_relay_session(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            session_id=reference.removeprefix("relay:"),
        )
    except BrowserRelayError as exc:
        raise VerticalAbilityConnectionError(exc.code, exc.message, status_code=exc.status_code) from exc
    if (
        session.platform_id != row.platform_id
        or session.account_label != row.account_label
        or session.transport != row.transport
        or session.connection_id != row.id
    ):
        raise VerticalAbilityConnectionError(
            "VERTICAL_ABILITY_RELAY_SESSION_INVALID",
            "浏览器 Relay 会话与网站连接不匹配，请重新配对。",
            status_code=503,
        )
    return session


def _device_authorization_required() -> VerticalAbilityConnectionError:
    """提示通过现有设备配对完成账号授权，不把缺少证明误写成桥接故障或能力不可用。"""

    return VerticalAbilityConnectionError(
        "VERTICAL_ABILITY_BROWSER_DEVICE_AUTHORIZATION_REQUIRED",
        "请在网站连接中选择实际本机或远端模式，并在浏览器所在设备启动连接器确认配对。"
        "本机与远程设备均可配对；仅服务端浏览器在线不能证明账号归属，公共渠道仍可使用。",
        status_code=409,
    )


def _validate_device_pairing_reference(
    db: Session, *, current_user: User, platform_id: str, account_label: str,
    transport: str, credentials: Mapping[str, str], expected_revision: int,
) -> None:
    """校验待确认配对的归属和拓扑；实际设备授权仍须通过令牌、来源和会话绑定校验。"""

    reference = credentials.get("session_reference", "")
    if transport not in {"local_loopback", "browser_relay"} or not reference.startswith("relay:"):
        raise _device_authorization_required()
    session = db.get(VerticalAbilityBrowserRelaySession, reference.removeprefix("relay:"))
    if (
        session is None or session.tenant_id != current_user.tenant_id
        or session.user_id != current_user.id or session.platform_id != platform_id
        or session.transport != transport
        or session.account_label != account_label or session.expected_revision != expected_revision
        or session.status != "pending" or session.connection_id is not None
        or session.expires_at is None or session.expires_at <= utc_now()
    ):
        raise _device_authorization_required()
