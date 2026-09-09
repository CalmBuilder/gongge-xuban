"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : relay_service.py
@CallChain  : 连接中心/宿主机连接器 → browser relay API → 会话与命令队列 → OpenCLI
@Description: 提供同事务设备绑定、撤销优先的状态条件更新、短事务轮询和不可覆盖终态的租约回执。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
import hashlib
import ipaddress
import json
import secrets
import time

from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.cancellation import TurnCancellationRequested, raise_if_cancelled
from app.db.models import (
    User,
    VerticalAbilityBrowserRelayCommand,
    VerticalAbilityBrowserRelaySession,
    VerticalAbilityConnection,
    new_id,
    utc_now,
)
from app.vertical_abilities.connection_settings import ConnectionTransport, find_connection_definition


RELAY_TOKEN_BYTES = 32
RELAY_PAIRING_TTL_SECONDS = 10 * 60
RELAY_COMMAND_MAX_TIMEOUT_SECONDS = 300.0
RELAY_COMMAND_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
RELAY_SESSION_STALE_SECONDS = 45.0
RELAY_LEASE_GRACE_SECONDS = 30.0


class BrowserRelayError(RuntimeError):
    """表示 Relay 配对、认证、命令队列或宿主机回执失败。"""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        """保存不包含配对令牌和平台凭据的稳定错误信息。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def create_relay_pairing(
    db: Session,
    *,
    current_user: User,
    platform_id: str,
    account_label: str,
    expected_revision: int,
    transport: ConnectionTransport = "browser_relay",
) -> tuple[VerticalAbilityBrowserRelaySession, str]:
    """将用户、平台、账号、修订及实际拓扑绑定到配对；缺省模式兼容旧远端请求。"""

    if transport not in {"local_loopback", "browser_relay"}:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_CONNECTION_TRANSPORT_INVALID",
            "浏览器设备配对模式无效。",
        )
    definition = find_connection_definition(platform_id)
    if definition is None or "browser_session" not in definition.auth_methods:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_BROWSER_PLATFORM_UNSUPPORTED",
            "该网站暂未提供浏览器会话 Relay 连接方式。",
        )
    label = str(account_label or "默认账号").strip() or "默认账号"
    if len(label) > 64:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_CONNECTION_ACCOUNT_LABEL_INVALID",
            "账号名称不能超过 64 个字符。",
        )
    if expected_revision < 0:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            "网站连接修订号无效，请刷新后重试。",
            status_code=409,
        )
    current = db.exec(
        select(VerticalAbilityConnection).where(
            VerticalAbilityConnection.tenant_id == current_user.tenant_id,
            VerticalAbilityConnection.user_id == current_user.id,
            VerticalAbilityConnection.platform_id == definition.platform_id,
            VerticalAbilityConnection.account_label == label,
            VerticalAbilityConnection.status != "revoked",
        )
    ).first()
    current_revision = current.revision if current is not None else 0
    if current_revision != expected_revision:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_CONNECTION_REVISION_CONFLICT",
            f"{definition.platform_name} 连接状态已变化，请刷新后重试。",
            status_code=409,
        )

    now = utc_now()
    old_sessions = db.exec(
        select(VerticalAbilityBrowserRelaySession).where(
            VerticalAbilityBrowserRelaySession.tenant_id == current_user.tenant_id,
            VerticalAbilityBrowserRelaySession.user_id == current_user.id,
            VerticalAbilityBrowserRelaySession.platform_id == definition.platform_id,
            VerticalAbilityBrowserRelaySession.account_label == label,
            VerticalAbilityBrowserRelaySession.status != "revoked",
        )
    ).all()
    for old_session in old_sessions:
        old_session.status = "revoked"
        old_session.disconnected_at = now
        old_session.updated_at = now
        _cancel_relay_commands(
            db,
            old_session.id,
            now=now,
            error_code="VERTICAL_ABILITY_RELAY_PAIRING_REPLACED",
            error_message="浏览器 Relay 配对已被新的配对替换。",
        )

    pairing_token = secrets.token_urlsafe(RELAY_TOKEN_BYTES)
    session = VerticalAbilityBrowserRelaySession(
        id=new_id("varelay"),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        platform_id=definition.platform_id,
        account_label=label,
        expected_revision=expected_revision,
        token_hash=_hash_token(pairing_token),
        transport=transport,
        status="pending",
        expires_at=now + timedelta(seconds=RELAY_PAIRING_TTL_SECONDS),
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, pairing_token


def get_user_relay_session(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> VerticalAbilityBrowserRelaySession:
    """按租户和用户读取 Relay 会话，避免通过可猜测 ID 访问其他用户状态。"""

    session = db.get(VerticalAbilityBrowserRelaySession, session_id)
    if (
        session is None
        or session.tenant_id != tenant_id
        or session.user_id != user_id
    ):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_SESSION_NOT_FOUND",
            "浏览器 Relay 会话不存在。",
            status_code=404,
        )
    if _expire_pending_session(db, session):
        db.commit()
        db.refresh(session)
    return session


def authenticate_relay_token(
    db: Session,
    pairing_token: str,
    *,
    peer_host: str | None = None,
) -> VerticalAbilityBrowserRelaySession:
    """校验设备后按连接→配对锁序原子恢复健康，任何已提交撤销或修订替换都不能被覆盖。"""

    raw_token = str(pairing_token or "").strip()
    if not raw_token or len(raw_token) > 256:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
            "浏览器 Relay 配对令牌无效或已过期。",
            status_code=401,
        )
    session = db.exec(
        select(VerticalAbilityBrowserRelaySession).where(
            VerticalAbilityBrowserRelaySession.token_hash == _hash_token(raw_token),
        ).execution_options(populate_existing=True)
    ).first()
    if session is None:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
            "浏览器 Relay 配对令牌无效或已过期。",
            status_code=401,
        )
    if session.status in {"revoked", "expired"}:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
            "浏览器 Relay 配对令牌无效或已过期。",
            status_code=401,
        )
    _require_connector_peer(session, peer_host)
    if session.status == "pending" and _expire_pending_session(db, session):
        db.commit()
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_TOKEN_EXPIRED",
            "浏览器 Relay 配对令牌已过期，请在连接中心重新配对。",
            status_code=410,
        )
    old_connection_id = session.connection_id
    old_revision = session.expected_revision
    try:
        # 绑定准备不得提前提交或隐式写 Session；最后的 CAS 才能授予设备访问权。
        with db.no_autoflush:
            connection = (
                _attach_relay_connection(db, session) if old_connection_id is None
                else _require_relay_connection_binding(db, session)
            )
            now = utc_now()
            connection_update = db.execute(
                update(VerticalAbilityConnection)
                .where(
                    VerticalAbilityConnection.id == connection.id,
                    VerticalAbilityConnection.tenant_id == session.tenant_id,
                    VerticalAbilityConnection.user_id == session.user_id,
                    VerticalAbilityConnection.platform_id == session.platform_id,
                    VerticalAbilityConnection.account_label == session.account_label,
                    VerticalAbilityConnection.revision == connection.revision,
                    VerticalAbilityConnection.transport == session.transport,
                    VerticalAbilityConnection.auth_method == "browser_session",
                    VerticalAbilityConnection.status.in_(("connected", "pending")),
                    select(User.id).where(
                        User.id == session.user_id, User.tenant_id == session.tenant_id,
                        User.membership_status == "active",
                    ).exists(),
                )
                .values(
                    status="connected", health_status="healthy", health_error_code=None,
                    last_checked_at=now, last_healthy_at=now, updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if connection_update.rowcount != 1:
                raise BrowserRelayError(
                    "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT",
                    "网站连接已撤销或更新，请刷新连接状态后重试。", status_code=409,
                )
            session_update = db.execute(
                update(VerticalAbilityBrowserRelaySession)
                .where(
                    VerticalAbilityBrowserRelaySession.id == session.id,
                    VerticalAbilityBrowserRelaySession.token_hash == _hash_token(raw_token),
                    VerticalAbilityBrowserRelaySession.tenant_id == session.tenant_id,
                    VerticalAbilityBrowserRelaySession.user_id == session.user_id,
                    VerticalAbilityBrowserRelaySession.platform_id == session.platform_id,
                    VerticalAbilityBrowserRelaySession.transport == session.transport,
                    VerticalAbilityBrowserRelaySession.expected_revision == old_revision,
                    VerticalAbilityBrowserRelaySession.connection_id == old_connection_id,
                    VerticalAbilityBrowserRelaySession.status.in_(("pending", "connected", "disconnected")),
                    or_(
                        VerticalAbilityBrowserRelaySession.status != "pending",
                        VerticalAbilityBrowserRelaySession.expires_at > now,
                    ),
                )
                .values(
                    status="connected", connection_id=connection.id,
                    # 仅首次授权记录复用账号的实际单调修订；后续心跳不能追认新凭据。
                    expected_revision=(connection.revision - 1 if old_connection_id is None else old_revision),
                    connected_at=func.coalesce(VerticalAbilityBrowserRelaySession.connected_at, now),
                    expires_at=None, last_seen_at=now, disconnected_at=None, updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if session_update.rowcount != 1:
                raise BrowserRelayError(
                    "VERTICAL_ABILITY_RELAY_TOKEN_INVALID",
                    "浏览器配对已撤销、过期或变化，请重新确认设备。", status_code=401,
                )
        db.commit()
    except BrowserRelayError:
        db.rollback()
        raise
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        database_code = getattr(exc.orig, "args", (None,))[0]
        sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
        if isinstance(exc, OperationalError) and database_code not in {1205, 1213} and sqlite_code not in {5, 6}:
            raise
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT",
            "浏览器连接正在更新，请稍后重试；旧授权不会被重新启用。", status_code=409,
        ) from exc
    db.refresh(session)
    db.refresh(connection)
    if session.status in {"revoked", "expired"}:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_TOKEN_INVALID", "浏览器配对已撤销或过期。", status_code=401,
        )
    return session


def relay_session_is_live(session: VerticalAbilityBrowserRelaySession) -> bool:
    """判断会话是否已经配对且最近仍收到宿主机心跳。"""

    if session.status != "connected" or session.last_seen_at is None:
        return False
    age = (utc_now() - session.last_seen_at).total_seconds()
    return age <= RELAY_SESSION_STALE_SECONDS


def require_live_relay_session(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> VerticalAbilityBrowserRelaySession:
    """确认在线授权；失活标记绑定观察到的心跳，不能覆盖新心跳或已经提交的撤销。"""

    session = get_user_relay_session(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    if not relay_session_is_live(session):
        if session.status == "connected":
            now = utc_now()
            db.execute(
                update(VerticalAbilityBrowserRelaySession)
                .where(
                    VerticalAbilityBrowserRelaySession.id == session.id,
                    VerticalAbilityBrowserRelaySession.status == "connected",
                    VerticalAbilityBrowserRelaySession.last_seen_at == session.last_seen_at,
                    or_(
                        VerticalAbilityBrowserRelaySession.last_seen_at.is_(None),
                        VerticalAbilityBrowserRelaySession.last_seen_at <= now - timedelta(seconds=RELAY_SESSION_STALE_SECONDS),
                    ),
                )
                .values(status="disconnected", disconnected_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            db.refresh(session)
            if relay_session_is_live(session):
                _require_relay_connection_binding(db, session)
                return session
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_NOT_CONNECTED",
            "宿主机浏览器连接器未在线，请启动连接器后重试。",
            status_code=503,
        )
    _require_relay_connection_binding(db, session)
    return session


def revoke_relay_session(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> VerticalAbilityBrowserRelaySession:
    """撤销当前用户的一次 Relay 会话及其配对令牌。"""

    session = get_user_relay_session(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    now = utc_now()
    session.status = "revoked"
    session.disconnected_at = now
    session.updated_at = now
    _cancel_relay_commands(
        db,
        session.id,
        now=now,
        error_code="VERTICAL_ABILITY_RELAY_SESSION_REVOKED",
        error_message="浏览器 Relay 会话已撤销。",
    )
    db.commit()
    db.refresh(session)
    return session


def revoke_relay_sessions_for_connection(db: Session, connection_id: str) -> None:
    """在用户撤销网站连接时同步吊销关联 Relay，避免旧令牌继续执行平台命令。"""

    sessions = db.exec(
        select(VerticalAbilityBrowserRelaySession).where(
            VerticalAbilityBrowserRelaySession.connection_id == connection_id,
            VerticalAbilityBrowserRelaySession.status != "revoked",
        )
    ).all()
    now = utc_now()
    for session in sessions:
        session.status = "revoked"
        session.disconnected_at = now
        session.updated_at = now
        _cancel_relay_commands(
            db,
            session.id,
            now=now,
            error_code="VERTICAL_ABILITY_RELAY_CONNECTION_REVOKED",
            error_message="网站连接已撤销。",
        )
    if sessions:
        db.flush()


def claim_next_relay_command(
    db: Session,
    *,
    pairing_token: str,
    peer_host: str | None = None,
) -> dict[str, object] | None:
    """以条件更新领取当前用户网站的命令，SQLite 无行锁时也不复活取消或覆盖其他租约。"""

    session = authenticate_relay_token(db, pairing_token, peer_host=peer_host)
    now = utc_now()
    eligible = and_(
        VerticalAbilityBrowserRelayCommand.relay_session_id == session.id,
        VerticalAbilityBrowserRelayCommand.tenant_id == session.tenant_id,
        VerticalAbilityBrowserRelayCommand.user_id == session.user_id,
        VerticalAbilityBrowserRelayCommand.platform_id == session.platform_id,
        or_(
            VerticalAbilityBrowserRelayCommand.status == "queued",
            and_(
                VerticalAbilityBrowserRelayCommand.status == "leased",
                VerticalAbilityBrowserRelayCommand.lease_expires_at.is_not(None),
                VerticalAbilityBrowserRelayCommand.lease_expires_at <= now,
            ),
        ),
        VerticalAbilityBrowserRelayCommand.deadline_at > now,
    )
    candidate = db.exec(
        select(VerticalAbilityBrowserRelayCommand)
        .where(eligible)
        .order_by(VerticalAbilityBrowserRelayCommand.created_at)
        .with_for_update()
    ).first()
    if candidate is None:
        return None
    lease_token = secrets.token_urlsafe(RELAY_TOKEN_BYTES)
    lease_hash = _hash_token(lease_token)
    try:
        arguments = json.loads(candidate.arguments_json)
        if not isinstance(arguments, list) or any(not isinstance(value, str) for value in arguments):
            raise ValueError("Relay 参数必须为字符串数组")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        db.execute(
            update(VerticalAbilityBrowserRelayCommand)
            .where(VerticalAbilityBrowserRelayCommand.id == candidate.id, eligible)
            .values(
                status="failed", error_code="VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
                error_message="Relay 命令参数无法读取。", updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_COMMAND_INVALID",
            "Relay 命令参数无法读取。",
        ) from exc
    payload = {
        "command_id": candidate.id,
        "platform_id": candidate.platform_id,
        "action": candidate.action,
        "positional_args": arguments,
        "limit": candidate.command_limit,
        "timeout_seconds": max(1.0, (candidate.deadline_at - now).total_seconds()),
        "lease_token": lease_token,
    }
    result = db.execute(
        update(VerticalAbilityBrowserRelayCommand)
        .where(VerticalAbilityBrowserRelayCommand.id == candidate.id, eligible)
        .values(
            status="leased", lease_token_hash=lease_hash,
            lease_expires_at=min(
                candidate.deadline_at,
                now + timedelta(seconds=RELAY_LEASE_GRACE_SECONDS + RELAY_COMMAND_MAX_TIMEOUT_SECONDS),
            ),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return payload if result.rowcount == 1 else None


def complete_relay_command(
    db: Session,
    *,
    pairing_token: str,
    command_id: str,
    lease_token: str,
    success: bool,
    output: str = "",
    error_code: str | None = None,
    error_message: str | None = None,
    peer_host: str | None = None,
) -> VerticalAbilityBrowserRelayCommand:
    """以租约 CAS 保存有界回执，取消、超时或其他回执先提交时不得覆盖终态。"""

    session = authenticate_relay_token(db, pairing_token, peer_host=peer_host)
    command = db.get(VerticalAbilityBrowserRelayCommand, command_id)
    if (
        command is None
        or command.relay_session_id != session.id
        or command.tenant_id != session.tenant_id
        or command.user_id != session.user_id
        or command.platform_id != session.platform_id
    ):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_COMMAND_NOT_FOUND",
            "Relay 命令不存在。",
            status_code=404,
        )
    if command.status in {"succeeded", "failed", "cancelled", "expired"}:
        return command
    if command.status != "leased" or not secrets.compare_digest(
        command.lease_token_hash or "",
        _hash_token(lease_token),
    ):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_LEASE_INVALID",
            "Relay 命令租约无效或已被回收。",
            status_code=409,
        )
    normalized_output = str(output or "")
    if len(normalized_output.encode("utf-8")) > RELAY_COMMAND_MAX_OUTPUT_BYTES:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
            "宿主机浏览器回执超过安全上限。",
        )
    now = utc_now()
    if now >= command.deadline_at:
        values = {
            "status": "expired", "result_json": None,
            "error_code": "VERTICAL_ABILITY_RELAY_COMMAND_TIMEOUT",
            "error_message": "宿主机浏览器命令执行超时。",
        }
    elif command.lease_expires_at is None or now >= command.lease_expires_at:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_LEASE_INVALID",
            "Relay 命令租约已过期，请重新领取。",
            status_code=409,
        )
    elif success:
        values = {
            "status": "succeeded",
            "result_json": json.dumps(
                {"output": normalized_output}, ensure_ascii=False, separators=(",", ":"),
            ),
            "error_code": None, "error_message": None,
        }
    else:
        values = {
            "status": "failed", "result_json": None,
            "error_code": _safe_error_code(error_code),
            "error_message": _safe_error_message(error_message),
        }
    result = db.execute(
        update(VerticalAbilityBrowserRelayCommand)
        .where(
            VerticalAbilityBrowserRelayCommand.id == command.id,
            VerticalAbilityBrowserRelayCommand.status == "leased",
            VerticalAbilityBrowserRelayCommand.lease_token_hash == _hash_token(lease_token),
        )
        .values(**values, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(command)
    if result.rowcount != 1 and command.status not in {
        "succeeded", "failed", "cancelled", "expired",
    }:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_LEASE_INVALID",
            "Relay 命令租约已变化，请重新领取。",
            status_code=409,
        )
    return command


def enqueue_and_wait_for_relay_command(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    platform_id: str,
    action: str,
    positional_args: Sequence[str],
    limit: int | None,
    timeout_seconds: float,
    is_cancelled: Callable[[], bool] | None = None,
) -> bytes:
    """按当前用户平台授权入队，每轮用独立短事务读取回执并以 CAS 处理超时和取消。"""

    session = require_live_relay_session(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    if platform_id != session.platform_id:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_PLATFORM_MISMATCH",
            "该浏览器配对未授权当前网站，请为对应网站建立自己的连接。",
            status_code=403,
        )
    normalized_arguments = [str(value) for value in positional_args]
    if len(normalized_arguments) > 3 or any(len(value) > 2048 for value in normalized_arguments):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "Relay 平台命令参数无效。",
        )
    if limit is not None and not 1 <= int(limit) <= 50:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_ARGUMENTS_INVALID",
            "Relay 平台命令返回数量超出范围。",
        )
    try:
        bounded_timeout = max(1.0, min(float(timeout_seconds), RELAY_COMMAND_MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_PROVIDER_TIMEOUT",
            "Relay 平台命令超时配置无效。",
        ) from exc
    now = utc_now()
    command = VerticalAbilityBrowserRelayCommand(
        id=new_id("varelaycmd"),
        relay_session_id=session.id,
        tenant_id=tenant_id,
        user_id=user_id,
        platform_id=platform_id,
        action=str(action),
        arguments_json=json.dumps(normalized_arguments, ensure_ascii=False, separators=(",", ":")),
        command_limit=int(limit) if limit is not None else None,
        status="queued",
        deadline_at=now + timedelta(seconds=bounded_timeout),
        created_at=now,
        updated_at=now,
    )
    command_id = command.id
    db.add(command)
    db.commit()
    try:
        while True:
            raise_if_cancelled(is_cancelled)
            # expire_all 只清理 ORM 缓存，不结束 MySQL REPEATABLE READ 快照。
            # 独立会话也避免轮询 rollback 意外丢弃调用方尚未提交的工作。
            with Session(db.get_bind()) as poll_db:
                current = poll_db.get(VerticalAbilityBrowserRelayCommand, command_id)
                if current is None:
                    raise BrowserRelayError(
                        "VERTICAL_ABILITY_RELAY_COMMAND_NOT_FOUND",
                        "Relay 命令在等待期间丢失。",
                    )
                if current.status == "succeeded":
                    return _result_output(current)
                if current.status == "failed":
                    raise BrowserRelayError(
                        current.error_code or "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
                        current.error_message or "宿主机浏览器命令执行失败。",
                    )
                if current.status == "cancelled":
                    raise BrowserRelayError(
                        "VERTICAL_ABILITY_RELAY_COMMAND_CANCELLED",
                        "宿主机浏览器命令已取消。",
                    )
                if current.status == "expired":
                    raise BrowserRelayError(
                        "VERTICAL_ABILITY_PROVIDER_TIMEOUT",
                        "宿主机浏览器命令执行超时，请检查连接器和浏览器状态。",
                    )
                now = utc_now()
                if now >= current.deadline_at:
                    poll_db.execute(
                        update(VerticalAbilityBrowserRelayCommand)
                        .where(
                            VerticalAbilityBrowserRelayCommand.id == command_id,
                            VerticalAbilityBrowserRelayCommand.status.in_(("queued", "leased")),
                            VerticalAbilityBrowserRelayCommand.deadline_at <= now,
                        )
                        .values(
                            status="expired", updated_at=now,
                            error_code="VERTICAL_ABILITY_PROVIDER_TIMEOUT",
                            error_message="宿主机浏览器命令执行超时。",
                        )
                        .execution_options(synchronize_session=False)
                    )
                    poll_db.commit()
                    # CAS 可能输给成功回执；新一轮读取真实终态后再返回。
                    continue
            time.sleep(0.1)
    except TurnCancellationRequested:
        with Session(db.get_bind()) as cancellation_db:
            cancellation_db.execute(
                update(VerticalAbilityBrowserRelayCommand)
                .where(
                    VerticalAbilityBrowserRelayCommand.id == command_id,
                    VerticalAbilityBrowserRelayCommand.status.in_(("queued", "leased")),
                )
                .values(
                    status="cancelled", updated_at=utc_now(),
                    error_code="VERTICAL_ABILITY_RELAY_COMMAND_CANCELLED",
                    error_message="宿主机浏览器命令已取消。",
                )
                .execution_options(synchronize_session=False)
            )
            cancellation_db.commit()
        raise


def _attach_relay_connection(
    db: Session,
    session: VerticalAbilityBrowserRelaySession,
) -> VerticalAbilityConnection:
    """在调用方事务中准备连接但不写配对行，实际设备绑定只能由最终状态 CAS 授予。"""

    from app.vertical_abilities.connection_service import save_user_connection

    expected = session.expected_revision
    if session.connection_id:
        return _require_relay_connection_binding(db, session)
    user = db.get(User, session.user_id)
    if user is None or user.tenant_id != session.tenant_id:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_OWNER_NOT_FOUND",
            "浏览器 Relay 所属用户不存在。",
            status_code=401,
        )
    try:
        row = save_user_connection(
            db,
            current_user=user,
            platform_id=session.platform_id,
            account_label=session.account_label,
            auth_method="browser_session",
            credentials={"session_reference": f"relay:{session.id}"},
            expected_revision=expected,
            transport=session.transport,
            commit=False,
        )
    except ValueError as exc:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT",
            "网站连接状态已变化，请在连接中心重新配对。",
            status_code=409,
        ) from exc
    row.transport = session.transport
    row.status = "connected"
    row.health_status = "healthy"
    row.health_error_code = None
    device_label = "本机浏览器" if session.transport == "local_loopback" else "宿主机浏览器 Relay"
    row.account_identity = f"{device_label} · {session.platform_id}"
    row.last_checked_at = utc_now()
    row.last_healthy_at = row.last_checked_at
    row.updated_at = row.last_checked_at
    db.flush()
    return row


def _require_relay_connection_binding(
    db: Session, session: VerticalAbilityBrowserRelaySession,
) -> VerticalAbilityConnection:
    """确认设备授权仍绑定原用户、网站、账号和凭据修订，旧令牌不能接管新连接。"""

    from app.vertical_abilities.connection_service import decrypt_connection_credentials

    connection = db.get(VerticalAbilityConnection, session.connection_id) if session.connection_id else None
    owner = db.get(User, session.user_id)
    if (
        owner is None or owner.tenant_id != session.tenant_id or owner.membership_status != "active"
        or connection is None
        or connection.tenant_id != session.tenant_id
        or connection.user_id != session.user_id
        or connection.platform_id != session.platform_id
        or connection.account_label != session.account_label
        or session.transport not in {"local_loopback", "browser_relay"}
        or connection.transport != session.transport
        or connection.auth_method != "browser_session"
        or connection.status not in {"connected", "pending"}
        or connection.revision != session.expected_revision + 1
        or decrypt_connection_credentials(connection).get("session_reference") != f"relay:{session.id}"
    ):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_RELAY_CONNECTION_CONFLICT",
            "浏览器授权对应的网站连接已撤销或变化，请重新配对自己的设备。",
            status_code=409,
        )
    return connection


def _require_connector_peer(
    session: VerticalAbilityBrowserRelaySession, peer_host: str | None,
) -> None:
    """本机配对只接受 API 提供的 loopback 来源；缺失、域名或远端地址均不能确认设备。"""

    if session.transport == "browser_relay":
        return
    address = None
    if isinstance(peer_host, str):
        try:
            address = ipaddress.ip_address(peer_host)
        except ValueError:
            pass
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if session.transport != "local_loopback" or address is None or not address.is_loopback:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_LOCAL_CONNECTOR_REQUIRED",
            "本机配对仅接受后端同一台设备的连接器，请在该设备连接本机服务地址；"
            "浏览器在其他设备时请选择远端 browser_relay 模式。",
            status_code=403,
        )


def _expire_pending_session(db: Session, session: VerticalAbilityBrowserRelaySession) -> bool:
    """仅过期数据库中仍在等待且已到期的配对，不能覆盖并发确认或撤销。"""

    if session.status != "pending" or session.expires_at is None or session.expires_at > utc_now():
        return False
    now = utc_now()
    result = db.execute(
        update(VerticalAbilityBrowserRelaySession)
        .where(
            VerticalAbilityBrowserRelaySession.id == session.id,
            VerticalAbilityBrowserRelaySession.status == "pending",
            VerticalAbilityBrowserRelaySession.expires_at <= now,
        )
        .values(status="expired", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(session)
    return result.rowcount == 1


def _result_output(command: VerticalAbilityBrowserRelayCommand) -> bytes:
    """从已完成命令的 JSON 回执中取出 UTF-8 原始输出，并拒绝空/坏结构。"""

    try:
        payload = json.loads(command.result_json or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BrowserRelayError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "宿主机浏览器回执格式无效。",
        ) from exc
    output = payload.get("output") if isinstance(payload, Mapping) else None
    if not isinstance(output, str):
        raise BrowserRelayError(
            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
            "宿主机浏览器回执缺少文本结果。",
        )
    return output.encode("utf-8")


def _hash_token(value: str) -> str:
    """只保存配对令牌的 SHA-256 摘要，数据库和审计输出不出现令牌明文。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_error_code(value: str | None) -> str:
    """把宿主机错误码限制为稳定短标识，避免把任意日志写入结构化错误字段。"""

    normalized = str(value or "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE").strip()
    if not normalized or len(normalized) > 64 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in normalized
    ):
        return "VERTICAL_ABILITY_PROVIDER_UNAVAILABLE"
    return normalized


def _safe_error_message(value: str | None) -> str:
    """限制宿主机错误摘要长度并移除换行，避免污染 API 日志与页面布局。"""

    normalized = " ".join(str(value or "宿主机浏览器命令执行失败。").split())
    return normalized[:1024]


def _cancel_relay_commands(
    db: Session,
    session_id: str,
    *,
    now,
    error_code: str,
    error_message: str,
) -> None:
    """按非终态条件原子取消队列，避免撤销与成功回执的竞争覆盖已经完成的数据。"""

    db.execute(
        update(VerticalAbilityBrowserRelayCommand).where(
            VerticalAbilityBrowserRelayCommand.relay_session_id == session_id,
            VerticalAbilityBrowserRelayCommand.status.in_(("queued", "leased")),
        )
        .values(
            status="cancelled", lease_token_hash=None, lease_expires_at=None,
            error_code=error_code, error_message=error_message, updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
