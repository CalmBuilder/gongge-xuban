"""
@Time       : 2026/08/10 17:40
@Author     : zhanglp8181
@File       : connection_profiles.py
@CallChain  : 企业连接设置 → FastAPI → ConnectionService → provider adapter/SQLModel
@Description: 提供管理员连接档案、Agent 绑定、健康探测、重新授权和受控读取 API。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.connectors.service import ConnectionError, ConnectionService
from app.connectors.slack import SLACK_OAUTH_AUTHORIZE_URL
from app.connectors.runtime import ConnectorRuntimeError, ConnectorRuntimeService
from app.config import Settings, get_settings
from app.db import get_session
from app.db.models import (
    AgentConnectionBinding,
    ConnectionCommandReceipt,
    ConnectionOAuthState,
    ConnectionProfile,
    ConnectorInboundRoute,
    ConnectorInboundEvent,
    ConnectorPrincipalBinding,
    SopInstance,
    SopWorkItem,
    User,
    utc_now,
)
from app.organization.governance import ensure_governance_permission
from app.security.encryption import decrypt_secret, encrypt_secret
from app.security.auth import get_current_user
from app.sop_runtime.execution_control import ExecutionControlError, ExecutionControlService
from app.sop_runtime.execution_control import canonical_checksum
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore
from app.sop_runtime.state_machine import RevisionConflictError
from app.sop_runtime.work_items import WorkItemError


router = APIRouter(prefix="/api/enterprise/connection-profiles", tags=["enterprise:connections"])
ConnectionScope = Literal["channels:read", "application:read"]
ConnectionAction = Literal["wecom.message_send"]


class ConnectionProfileCreate(BaseModel):
    """创建指定 provider 连接档案并即时验证稳定账号身份的请求。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: Literal[0] = 0
    provider: Literal["slack", "wecom"] = "slack"
    display_name: str = Field(min_length=1, max_length=191)
    token: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    corp_id: SecretStr | None = Field(default=None, min_length=1, max_length=128)
    agent_id: SecretStr | None = Field(default=None, min_length=1, max_length=64)
    corp_secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    callback_token: SecretStr | None = Field(default=None, min_length=1, max_length=128)
    callback_encoding_aes_key: SecretStr | None = Field(default=None, min_length=43, max_length=43)
    required_scopes: set[ConnectionScope] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> "ConnectionProfileCreate":
        """要求 Slack token 与企业微信三元凭据互斥，并绑定各自唯一最小 scope。"""

        wecom_values = (self.corp_id, self.agent_id, self.corp_secret)
        callback_values = (self.callback_token, self.callback_encoding_aes_key)
        if self.provider == "slack":
            if self.token is None or any(
                value is not None for value in (*wecom_values, *callback_values)
            ):
                raise ValueError("INVALID_SLACK_CONNECTION_CREDENTIALS")
            if self.required_scopes != {"channels:read"}:
                raise ValueError("INVALID_SLACK_CONNECTION_SCOPES")
        elif self.token is not None or any(value is None for value in wecom_values):
            raise ValueError("INVALID_WECOM_CONNECTION_CREDENTIALS")
        elif any(value is not None for value in callback_values) and any(
            value is None for value in callback_values
        ):
            raise ValueError("INVALID_WECOM_CALLBACK_CREDENTIALS")
        elif not all(value is not None for value in callback_values):
            raise ValueError("WECOM_CALLBACK_CREDENTIALS_REQUIRED")
        elif self.required_scopes != {"application:read"}:
            raise ValueError("INVALID_WECOM_CONNECTION_SCOPES")
        return self


class ConnectionProfileRead(BaseModel):
    """返回不包含 token、密文或内部密钥定位信息的连接档案投影。"""

    id: str
    tenant_id: str
    provider: str
    account_id: str
    display_name: str
    required_scopes: list[str]
    granted_scopes: list[str]
    tool_allowlist: list[str]
    status: str
    health_status: str
    health_error_code: str | None
    rate_limited_until: str | None
    last_checked_at: str | None
    last_healthy_at: str | None
    secret_revision: int
    revision: int
    has_secret: bool = True
    callback_configured: bool = False
    created_at: str
    updated_at: str


class ConnectionBindingCreate(BaseModel):
    """把当前档案显式绑定给同租户 Agent，并收窄其授权范围。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    agent_id: str
    allowed_scopes: set[ConnectionScope] = Field(min_length=1)


class ConnectionBindingRead(BaseModel):
    """返回不含凭据的 Agent 连接绑定。"""

    id: str
    tenant_id: str
    agent_id: str
    profile_id: str
    allowed_scopes: list[str]
    allowed_actions: list[str]
    enabled: bool
    revision: int
    created_at: str
    updated_at: str


class ConnectionRevisionCommand(BaseModel):
    """以期望修订保护连接档案状态命令。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class WeComCallbackRotate(ConnectionRevisionCommand):
    """以一次性新密钥轮换企业微信公开回调配置的 CAS 命令。"""

    callback_token: SecretStr = Field(min_length=1, max_length=128)
    callback_encoding_aes_key: SecretStr = Field(min_length=43, max_length=43)


class ConnectionBindingStateCommand(ConnectionRevisionCommand):
    """以期望修订启停单个 Agent 连接绑定。"""

    enabled: bool


class ConnectionBindingActionsCommand(BaseModel):
    """以档案和绑定双修订更新受控外部写动作白名单。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    expected_profile_revision: int = Field(ge=1)
    expected_binding_revision: int = Field(ge=1)
    allowed_actions: set[ConnectionAction] = Field(default_factory=set)


class ConnectionReauthorize(ConnectionRevisionCommand):
    """提交 provider 对应的新凭据并验证仍为同一稳定账号。"""

    token: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    corp_id: SecretStr | None = Field(default=None, min_length=1, max_length=128)
    agent_id: SecretStr | None = Field(default=None, min_length=1, max_length=64)
    corp_secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_single_credential_shape(self) -> "ConnectionReauthorize":
        """只接受 Slack token 或完整企业微信三元凭据，不在 DTO 层猜测 provider。"""

        wecom_values = (self.corp_id, self.agent_id, self.corp_secret)
        if self.token is not None and all(value is None for value in wecom_values):
            return self
        if self.token is None and all(value is not None for value in wecom_values):
            return self
        raise ValueError("INVALID_CONNECTION_CREDENTIALS")


class ConnectionAttentionReauthorize(ConnectionReauthorize):
    """原子完成凭据轮换和 reauth Attention 的 CAS 命令信封。"""

    attention_expected_revision: int = Field(ge=0)


class ConnectionReadProbe(BaseModel):
    """执行一次受 scope、allowlist 和 Agent 绑定约束的 provider 只读调用。"""

    tenant_id: str
    agent_id: str
    channel_id: str | None = Field(default=None, pattern=r"^[CG][A-Z0-9]{1,31}$")


class ConnectionReadProbeResult(BaseModel):
    """返回 provider 已结构化的只读响应，不附带请求凭据。"""

    provider: str
    account_id: str
    data: dict[str, object]


class ConnectorPrincipalBindingCreate(BaseModel):
    """通过已验签 inbox 事件授权外部发送者对应的平台用户。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    event_id: str
    user_id: str


class ConnectorPrincipalBindingRead(BaseModel):
    """返回不含原始外部发送者标识的主体绑定投影。"""

    id: str
    tenant_id: str
    provider: str
    profile_id: str
    user_id: str
    enabled: bool
    revision: int


class ConnectorInboundRouteSet(BaseModel):
    """以档案修订保护入站 Agent 路由设置命令。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    agent_id: str


class ConnectorInboundRouteRead(BaseModel):
    """返回档案唯一入站 Agent 路由。"""

    id: str
    tenant_id: str
    provider: str
    profile_id: str
    agent_id: str
    enabled: bool
    revision: int


class ConnectorInboundEventRead(BaseModel):
    """返回待处理入站事实，不包含发送者摘要、正文、密文或外部消息 ID。"""

    id: str
    profile_id: str
    event_type: str
    status: str
    attempt_count: int
    last_error_code: str | None
    principal_bound: bool
    created_at: str


class SlackOAuthStart(BaseModel):
    """创建一次由有权用户发起的 Slack OAuth v2 安装或重授权上下文。"""

    tenant_id: str
    command_id: str = Field(min_length=1, max_length=128)
    flow_type: Literal["create", "reauthorize", "reauthorize_attention"]
    display_name: str | None = Field(default=None, max_length=191)
    profile_id: str | None = None
    attention_id: str | None = None
    expected_profile_revision: int = Field(default=0, ge=0)
    expected_attention_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_flow_fields(self) -> "SlackOAuthStart":
        """拒绝流程类型与资源修订不匹配，避免 callback 猜测目标。"""

        if self.flow_type == "create":
            if (
                not (self.display_name or "").strip()
                or self.profile_id is not None
                or self.attention_id is not None
                or self.expected_profile_revision != 0
                or self.expected_attention_revision is not None
            ):
                raise ValueError("INVALID_SLACK_OAUTH_CREATE_CONTEXT")
        elif self.flow_type == "reauthorize":
            if (
                not self.profile_id
                or self.attention_id is not None
                or self.expected_profile_revision < 1
                or self.expected_attention_revision is not None
            ):
                raise ValueError("INVALID_SLACK_OAUTH_REAUTHORIZE_CONTEXT")
        elif (
            not self.profile_id
            or not self.attention_id
            or self.expected_profile_revision < 1
            or self.expected_attention_revision is None
        ):
            raise ValueError("INVALID_SLACK_OAUTH_ATTENTION_CONTEXT")
        return self


class SlackOAuthStartRead(BaseModel):
    """返回固定 Slack authorize URL 和短期过期时间，不回显服务端 client secret。"""

    authorize_url: str
    expires_at: str


def get_connection_service(db: Session = Depends(get_session)) -> ConnectionService:
    """构造绑定当前请求事务的连接服务，便于测试替换 provider adapter。"""

    return ConnectionService(db)


@router.post("/slack/oauth/start", response_model=SlackOAuthStartRead)
def start_slack_oauth(
    request: SlackOAuthStart,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> SlackOAuthStartRead:
    """为交互式 Slack 安装创建一次性 state；无人值守 Runtime 不调用此入口。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    settings = get_settings()
    _ensure_connection_secret_backend(settings)
    if not settings.slack_oauth_configured:
        raise HTTPException(status_code=503, detail="SLACK_OAUTH_NOT_CONFIGURED")
    existing = service.db.exec(
        select(ConnectionOAuthState).where(
            ConnectionOAuthState.tenant_id == request.tenant_id,
            ConnectionOAuthState.command_id == request.command_id,
        )
    ).first()
    if existing is not None:
        if not _oauth_state_matches(existing, request, current_user.id):
            raise HTTPException(status_code=409, detail="CONNECTION_COMMAND_ID_REUSED")
        if existing.status != "pending" or existing.expires_at <= utc_now():
            raise HTTPException(status_code=409, detail="SLACK_OAUTH_STATE_NOT_PENDING")
        raw_state = decrypt_secret(existing.encrypted_state)
        return SlackOAuthStartRead(
            authorize_url=_slack_authorize_url(raw_state, existing, settings),
            expires_at=existing.expires_at.isoformat(),
        )
    profile = None
    if request.profile_id:
        profile = service.db.get(ConnectionProfile, request.profile_id)
        if profile is None or profile.tenant_id != request.tenant_id:
            raise HTTPException(status_code=404, detail="CONNECTION_PROFILE_NOT_FOUND")
        if profile.revision != request.expected_profile_revision:
            raise HTTPException(status_code=409, detail="CONNECTION_REVISION_CONFLICT")
    if request.attention_id:
        attention = service.db.get(SopWorkItem, request.attention_id)
        if (
            attention is None
            or attention.tenant_id != request.tenant_id
            or attention.attention_kind != "reauth"
            or str(attention.payload_json.get("profile_id") or "") != request.profile_id
            or attention.revision != request.expected_attention_revision
        ):
            raise HTTPException(status_code=404, detail="CONNECTION_REAUTH_ATTENTION_NOT_FOUND")
    raw_state = secrets.token_urlsafe(32)
    now = utc_now()
    row = ConnectionOAuthState(
        state_hash=hashlib.sha256(raw_state.encode("utf-8")).hexdigest(),
        encrypted_state=encrypt_secret(raw_state),
        tenant_id=request.tenant_id,
        actor_user_id=current_user.id,
        flow_type=request.flow_type,
        profile_id=request.profile_id,
        attention_id=request.attention_id,
        display_name=(request.display_name or "").strip() or None,
        command_id=request.command_id,
        expected_profile_revision=request.expected_profile_revision,
        expected_attention_revision=request.expected_attention_revision,
        required_scopes_json=["channels:read"],
        expires_at=now + timedelta(minutes=10),
    )
    service.db.add(row)
    try:
        service.db.flush()
    except IntegrityError as exc:
        service.db.rollback()
        concurrent = service.db.exec(
            select(ConnectionOAuthState).where(
                ConnectionOAuthState.tenant_id == request.tenant_id,
                ConnectionOAuthState.command_id == request.command_id,
            )
        ).first()
        if (
            concurrent is not None
            and _oauth_state_matches(concurrent, request, current_user.id)
            and concurrent.status == "pending"
            and concurrent.expires_at > utc_now()
        ):
            raw_state = decrypt_secret(concurrent.encrypted_state)
            return SlackOAuthStartRead(
                authorize_url=_slack_authorize_url(raw_state, concurrent, settings),
                expires_at=concurrent.expires_at.isoformat(),
            )
        raise HTTPException(status_code=409, detail="CONNECTION_COMMAND_ID_REUSED") from exc
    append_user_management_audit(
        service.db,
        current_user=current_user,
        tenant_id=request.tenant_id,
        permission_code="connection_profile.manage",
        action="connection_profile.oauth_start",
        action_kind="create",
        outcome="succeeded",
        resource_type="connection_oauth_state",
        resource_id=row.id,
        detail={
            "flow_type": row.flow_type,
            "profile_id": row.profile_id,
            "attention_id": row.attention_id,
            "required_scopes": row.required_scopes_json,
        },
    )
    service.db.commit()
    return SlackOAuthStartRead(
        authorize_url=_slack_authorize_url(raw_state, row, settings),
        expires_at=row.expires_at.isoformat(),
    )


@router.get("/slack/oauth/callback", response_class=RedirectResponse)
def complete_slack_oauth(
    state: str = Query(..., min_length=16, max_length=512),
    code: str | None = Query(default=None, min_length=1, max_length=2048),
    error: str | None = Query(default=None, max_length=128),
    service: ConnectionService = Depends(get_connection_service),
) -> RedirectResponse:
    """验证一次性 state、交换 OAuth code，并原子完成建档或原 Execution 重授权。"""

    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    oauth_state = service.db.exec(
        select(ConnectionOAuthState).where(ConnectionOAuthState.state_hash == state_hash)
    ).first()
    if oauth_state is None:
        return _oauth_redirect("error", "SLACK_OAUTH_STATE_INVALID", False)
    to_attention = oauth_state.flow_type == "reauthorize_attention"
    if oauth_state.status != "pending" or oauth_state.expires_at <= utc_now():
        return _oauth_redirect("error", "SLACK_OAUTH_STATE_NOT_PENDING", to_attention)
    if not _claim_oauth_state(service.db, oauth_state):
        return _oauth_redirect("error", "SLACK_OAUTH_STATE_NOT_PENDING", to_attention)
    if error or not code:
        _fail_oauth_state(service.db, oauth_state, "SLACK_OAUTH_DENIED")
        return _oauth_redirect("error", "SLACK_OAUTH_DENIED", to_attention)
    settings = get_settings()
    if not settings.connection_secret_backend_configured:
        _fail_oauth_state(service.db, oauth_state, "CONNECTION_SECRET_BACKEND_NOT_CONFIGURED")
        return _oauth_redirect(
            "error", "CONNECTION_SECRET_BACKEND_NOT_CONFIGURED", to_attention
        )
    if not settings.slack_oauth_configured:
        _fail_oauth_state(service.db, oauth_state, "SLACK_OAUTH_NOT_CONFIGURED")
        return _oauth_redirect("error", "SLACK_OAUTH_NOT_CONFIGURED", to_attention)
    exchanged = service.slack.exchange_oauth_code(
        code=code,
        client_id=settings.slack_oauth_client_id,
        client_secret=settings.slack_oauth_client_secret,
        redirect_uri=settings.slack_oauth_redirect_uri,
    )
    if not exchanged.success:
        error_code = exchanged.error_code or "SLACK_OAUTH_EXCHANGE_FAILED"
        _fail_oauth_state(service.db, oauth_state, error_code)
        return _oauth_redirect("error", error_code, to_attention)
    if set(oauth_state.required_scopes_json or []) - set(exchanged.granted_scopes):
        _fail_oauth_state(service.db, oauth_state, "CONNECTION_SCOPE_MISSING")
        return _oauth_redirect("error", "CONNECTION_SCOPE_MISSING", to_attention)
    try:
        actor = service.db.get(User, oauth_state.actor_user_id)
        if actor is None or actor.tenant_id != oauth_state.tenant_id:
            raise ConnectionError("SLACK_OAUTH_ACTOR_UNAVAILABLE")
        _ensure_connection_manager(service.db, oauth_state.tenant_id, actor)
        row = _apply_oauth_connection(service, oauth_state, exchanged.token, actor)
        if row.account_id != exchanged.account_id:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        locked_state = service.db.exec(
            select(ConnectionOAuthState)
            .where(ConnectionOAuthState.id == oauth_state.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if locked_state.status != "processing" or locked_state.expires_at <= utc_now():
            raise ConnectionError("SLACK_OAUTH_STATE_NOT_PENDING")
        locked_state.status = "consumed"
        locked_state.consumed_at = utc_now()
        service.db.add(locked_state)
        service.db.commit()
        return _oauth_redirect("success", None, to_attention)
    except (
        ConnectionError,
        HTTPException,
        ExecutionControlError,
        SopExecutionConflictError,
        RevisionConflictError,
        WorkItemError,
    ) as exc:
        service.db.rollback()
        if isinstance(exc, ConnectionError):
            error_code = exc.code
        elif isinstance(exc, HTTPException):
            error_code = "SLACK_OAUTH_FORBIDDEN"
        else:
            error_code = getattr(exc, "code", "SLACK_OAUTH_TRANSACTION_CONFLICT")
        refreshed = service.db.exec(
            select(ConnectionOAuthState).where(ConnectionOAuthState.id == oauth_state.id)
        ).first()
        if refreshed is not None:
            _fail_oauth_state(service.db, refreshed, error_code)
        return _oauth_redirect("error", error_code, to_attention)


@router.get("", response_model=list[ConnectionProfileRead])
def list_connection_profiles(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ConnectionProfileRead]:
    """仅允许租户管理员列出本租户连接档案。"""

    _ensure_connection_manager(db, tenant_id, current_user)
    rows = db.exec(
        select(ConnectionProfile)
        .where(ConnectionProfile.tenant_id == tenant_id)
        .order_by(ConnectionProfile.provider, ConnectionProfile.display_name, ConnectionProfile.id)
    ).all()
    return [_profile_read(row) for row in rows]


@router.post("", response_model=ConnectionProfileRead, status_code=201)
def create_connection_profile(
    request: ConnectionProfileCreate,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """验证管理员权限和 provider 真实身份后创建连接档案。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    _ensure_connection_secret_backend(get_settings())
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_profile.create"
    )
    if replay is not None:
        return ConnectionProfileRead.model_validate(replay)
    try:
        if request.provider == "slack":
            row = service.create_slack_profile(
                tenant_id=request.tenant_id,
                display_name=request.display_name,
                token=_secret_value(request.token),
                required_scopes=set(request.required_scopes),
                actor_user_id=current_user.id,
            )
        else:
            row = service.create_wecom_profile(
                tenant_id=request.tenant_id,
                display_name=request.display_name,
                corp_id=_secret_value(request.corp_id),
                agent_id=_secret_value(request.agent_id),
                corp_secret=_secret_value(request.corp_secret),
                callback_token=_optional_secret_value(request.callback_token),
                callback_encoding_aes_key=_optional_secret_value(
                    request.callback_encoding_aes_key
                ),
                actor_user_id=current_user.id,
            )
        _audit_profile(service.db, current_user, row, "connection_profile.create")
        result = _profile_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_profile.create",
            "connection_profile",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post("/{profile_id}/bindings", response_model=ConnectionBindingRead, status_code=201)
def create_connection_binding(
    profile_id: str,
    request: ConnectionBindingCreate,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionBindingRead:
    """创建显式 Agent/账号绑定，不提供 provider 默认账号回退。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_binding.create"
    )
    if replay is not None:
        return ConnectionBindingRead.model_validate(replay)
    try:
        row = service.bind_agent(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            agent_id=request.agent_id,
            allowed_scopes=set(request.allowed_scopes),
            expected_profile_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
        append_user_management_audit(
            service.db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connection_binding.create",
            action_kind="create",
            outcome="succeeded",
            resource_type="agent_connection_binding",
            resource_id=row.id,
            after={
                "profile_id": profile_id,
                "agent_id": row.agent_id,
                "allowed_scopes": row.allowed_scopes_json,
            },
        )
        result = _binding_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_binding.create",
            "agent_connection_binding",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.get("/{profile_id}/bindings", response_model=list[ConnectionBindingRead])
def list_connection_bindings(
    profile_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ConnectionBindingRead]:
    """仅向租户管理员返回指定档案的 Agent 绑定和当前修订。"""

    _ensure_connection_manager(db, tenant_id, current_user)
    profile = db.get(ConnectionProfile, profile_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="CONNECTION_PROFILE_NOT_FOUND")
    rows = db.exec(
        select(AgentConnectionBinding)
        .where(
            AgentConnectionBinding.tenant_id == tenant_id,
            AgentConnectionBinding.profile_id == profile_id,
        )
        .order_by(AgentConnectionBinding.created_at, AgentConnectionBinding.id)
    ).all()
    return [_binding_read(row) for row in rows]


@router.post(
    "/inbound/principal-bindings",
    response_model=ConnectorPrincipalBindingRead,
    status_code=201,
)
def bind_connector_principal(
    request: ConnectorPrincipalBindingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ConnectorPrincipalBindingRead:
    """从本租户已验签事件绑定平台用户，禁止管理员输入原始外部用户 ID。"""

    _ensure_connection_manager(db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        db, request, current_user, "connector_principal_binding.create"
    )
    if replay is not None:
        return ConnectorPrincipalBindingRead.model_validate(replay)
    try:
        row = ConnectorRuntimeService(db).bind_principal_from_event(
            tenant_id=request.tenant_id,
            event_id=request.event_id,
            user_id=request.user_id,
            actor_user_id=current_user.id,
        )
        result = _principal_binding_read(row)
        append_user_management_audit(
            db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connector_principal_binding.create",
            action_kind="create",
            outcome="succeeded",
            resource_type="connector_principal_binding",
            resource_id=row.id,
            after={
                "profile_id": row.profile_id,
                "user_id": row.user_id,
                "enabled": row.enabled,
            },
        )
        _record_connection_command(
            db,
            request,
            current_user,
            "connector_principal_binding.create",
            "connector_principal_binding",
            row.id,
            result.model_dump(mode="json"),
        )
        db.commit()
        return result
    except ConnectorRuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc


@router.post(
    "/{profile_id}/inbound-route",
    response_model=ConnectorInboundRouteRead,
)
def set_connector_inbound_route(
    profile_id: str,
    request: ConnectorInboundRouteSet,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ConnectorInboundRouteRead:
    """设置档案唯一 Agent 路由，并要求档案修订及 Agent 连接绑定仍有效。"""

    _ensure_connection_manager(db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        db, request, current_user, "connector_inbound_route.set"
    )
    if replay is not None:
        return ConnectorInboundRouteRead.model_validate(replay)
    profile = db.get(ConnectionProfile, profile_id)
    if profile is None or profile.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="CONNECTION_PROFILE_NOT_FOUND")
    if profile.revision != request.expected_revision:
        raise HTTPException(status_code=409, detail="CONNECTION_REVISION_CONFLICT")
    try:
        row = ConnectorRuntimeService(db).configure_route(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            agent_id=request.agent_id,
            actor_user_id=current_user.id,
        )
        result = _inbound_route_read(row)
        append_user_management_audit(
            db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connector_inbound_route.set",
            action_kind="update",
            outcome="succeeded",
            resource_type="connector_inbound_route",
            resource_id=row.id,
            after={"profile_id": profile_id, "agent_id": row.agent_id, "enabled": row.enabled},
        )
        _record_connection_command(
            db,
            request,
            current_user,
            "connector_inbound_route.set",
            "connector_inbound_route",
            row.id,
            result.model_dump(mode="json"),
        )
        db.commit()
        return result
    except ConnectorRuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc


@router.get(
    "/{profile_id}/inbound-route",
    response_model=ConnectorInboundRouteRead | None,
)
def get_connector_inbound_route(
    profile_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ConnectorInboundRouteRead | None:
    """读取档案当前入站 Agent 路由；未配置时明确返回 null。"""

    _ensure_connection_manager(db, tenant_id, current_user)
    profile = db.get(ConnectionProfile, profile_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="CONNECTION_PROFILE_NOT_FOUND")
    row = db.exec(
        select(ConnectorInboundRoute).where(
            ConnectorInboundRoute.tenant_id == tenant_id,
            ConnectorInboundRoute.profile_id == profile_id,
        )
    ).first()
    return _inbound_route_read(row) if row is not None else None


@router.get(
    "/{profile_id}/inbound-events",
    response_model=list[ConnectorInboundEventRead],
)
def list_connector_inbound_events(
    profile_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ConnectorInboundEventRead]:
    """列出最近待授权/失败事件的安全投影，供管理员从事件绑定平台用户。"""

    _ensure_connection_manager(db, tenant_id, current_user)
    profile = db.get(ConnectionProfile, profile_id)
    if profile is None or profile.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="CONNECTION_PROFILE_NOT_FOUND")
    rows = db.exec(
        select(ConnectorInboundEvent)
        .where(
            ConnectorInboundEvent.tenant_id == tenant_id,
            ConnectorInboundEvent.profile_id == profile_id,
            ConnectorInboundEvent.status.in_(("pending", "failed", "dead_letter")),
        )
        .order_by(ConnectorInboundEvent.created_at.desc(), ConnectorInboundEvent.id.desc())
        .limit(50)
    ).all()
    bindings = db.exec(
        select(ConnectorPrincipalBinding).where(
            ConnectorPrincipalBinding.tenant_id == tenant_id,
            ConnectorPrincipalBinding.profile_id == profile_id,
            ConnectorPrincipalBinding.enabled.is_(True),
        )
    ).all()
    bound_hashes = {item.sender_ref_hash for item in bindings}
    return [
        ConnectorInboundEventRead(
            id=row.id,
            profile_id=row.profile_id,
            event_type=row.event_type,
            status=row.status,
            attempt_count=row.attempt_count,
            last_error_code=row.last_error_code,
            principal_bound=bool(row.sender_ref_hash and row.sender_ref_hash in bound_hashes),
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@router.post(
    "/{profile_id}/bindings/{binding_id}/state",
    response_model=ConnectionBindingRead,
)
def set_connection_binding_state(
    profile_id: str,
    binding_id: str,
    request: ConnectionBindingStateCommand,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionBindingRead:
    """以 CAS 启停 Agent 绑定，并追加不含凭据的管理审计。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_binding.state_change"
    )
    if replay is not None:
        return ConnectionBindingRead.model_validate(replay)
    try:
        row = service.set_binding_enabled(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            binding_id=binding_id,
            enabled=request.enabled,
            expected_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
        append_user_management_audit(
            service.db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connection_binding.state_change",
            action_kind="update",
            outcome="succeeded",
            resource_type="agent_connection_binding",
            resource_id=row.id,
            after={"enabled": row.enabled, "revision": row.revision},
        )
        result = _binding_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_binding.state_change",
            "agent_connection_binding",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/{profile_id}/bindings/{binding_id}/actions",
    response_model=ConnectionBindingRead,
)
def set_connection_binding_actions(
    profile_id: str,
    binding_id: str,
    request: ConnectionBindingActionsCommand,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionBindingRead:
    """显式启停单个 Agent 的审批后外部写，绝不沿用只读绑定推导授权。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        service.db,
        request,
        current_user,
        "connection_binding.actions_change",
    )
    if replay is not None:
        return ConnectionBindingRead.model_validate(replay)
    try:
        profile, binding = service.set_binding_actions(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            binding_id=binding_id,
            allowed_actions=set(request.allowed_actions),
            expected_profile_revision=request.expected_profile_revision,
            expected_binding_revision=request.expected_binding_revision,
            actor_user_id=current_user.id,
        )
        append_user_management_audit(
            service.db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connection_binding.actions_change",
            action_kind="update",
            outcome="succeeded",
            resource_type="agent_connection_binding",
            resource_id=binding.id,
            after={
                "profile_revision": profile.revision,
                "binding_revision": binding.revision,
                "allowed_actions": binding.allowed_actions_json,
            },
        )
        result = _binding_read(binding)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_binding.actions_change",
            "agent_connection_binding",
            binding.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post("/{profile_id}/health", response_model=ConnectionProfileRead)
def check_connection_health(
    profile_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """执行真实 provider 健康探测并持久化可诊断状态。"""

    _ensure_connection_manager(service.db, tenant_id, current_user)
    try:
        row = service.check_health(tenant_id=tenant_id, profile_id=profile_id)
        _audit_profile(service.db, current_user, row, "connection_profile.health_check")
        service.db.commit()
        service.db.refresh(row)
        return _profile_read(row)
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post("/{profile_id}/reauthorize", response_model=ConnectionProfileRead)
def reauthorize_connection_profile(
    profile_id: str,
    request: ConnectionReauthorize,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """以 provider 新凭据新增密钥修订，禁止账号漂移和旧修订复活。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    _ensure_connection_secret_backend(get_settings())
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_profile.reauthorize"
    )
    if replay is not None:
        return ConnectionProfileRead.model_validate(replay)
    try:
        profile = service.db.get(ConnectionProfile, profile_id)
        if profile is None or profile.tenant_id != request.tenant_id:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        if profile.provider == "slack":
            if request.token is None:
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            row = service.rotate_slack_secret(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                token=request.token.get_secret_value(),
                expected_revision=request.expected_revision,
                actor_user_id=current_user.id,
            )
        elif profile.provider == "wecom":
            if request.token is not None:
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            row = service.rotate_wecom_secret(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                corp_id=_secret_value(request.corp_id),
                agent_id=_secret_value(request.agent_id),
                corp_secret=_secret_value(request.corp_secret),
                expected_revision=request.expected_revision,
                actor_user_id=current_user.id,
            )
        else:
            raise ConnectionError("CONNECTION_PROVIDER_UNSUPPORTED")
        _audit_profile(service.db, current_user, row, "connection_profile.reauthorize")
        result = _profile_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_profile.reauthorize",
            "connection_profile",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/{profile_id}/reauthorize-attention/{attention_id}",
    response_model=ConnectionProfileRead,
)
def reauthorize_connection_attention(
    profile_id: str,
    attention_id: str,
    request: ConnectionAttentionReauthorize,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """先无锁验证 provider 凭据，再在 Execution lease 内原子轮换并决定 Attention。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    _ensure_connection_secret_backend(get_settings())
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_profile.reauthorize_attention"
    )
    if replay is not None:
        return ConnectionProfileRead.model_validate(replay)
    try:
        profile = service.db.get(ConnectionProfile, profile_id)
        if profile is None or profile.tenant_id != request.tenant_id:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        if profile.provider == "slack":
            if request.token is None:
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            validated = service.validate_slack_reauthorization(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                token=request.token.get_secret_value(),
                expected_revision=request.expected_revision,
            )
        elif profile.provider == "wecom":
            if request.token is not None:
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            validated = service.validate_wecom_reauthorization(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                corp_id=_secret_value(request.corp_id),
                agent_id=_secret_value(request.agent_id),
                corp_secret=_secret_value(request.corp_secret),
                expected_revision=request.expected_revision,
            )
        else:
            raise ConnectionError("CONNECTION_PROVIDER_UNSUPPORTED")
        attention = service.db.get(SopWorkItem, attention_id)
        if (
            attention is None
            or attention.tenant_id != request.tenant_id
            or attention.attention_kind != "reauth"
            or str(attention.payload_json.get("profile_id") or "") != profile_id
        ):
            raise ConnectionError("CONNECTION_REAUTH_ATTENTION_NOT_FOUND")
        instance = service.db.get(SopInstance, attention.instance_id)
        if instance is None or instance.tenant_id != request.tenant_id:
            raise ConnectionError("CONNECTION_REAUTH_ATTENTION_NOT_FOUND")
        store = SopExecutionStore(service.db)
        control = ExecutionControlService(service.db, store)
        with store.owned(instance, worker_id=f"reauth:{attention.id[-16:]}"):
            if profile.provider == "slack":
                row = service.apply_slack_reauthorization(
                    validated,
                    actor_user_id=current_user.id,
                )
            else:
                row = service.apply_wecom_reauthorization(
                    validated,
                    actor_user_id=current_user.id,
                )
            control.resolve_attention(
                instance,
                attention,
                actor_user_id=current_user.id,
                command_id=request.command_id,
                command="reauthorize",
                expected_revision=request.attention_expected_revision,
            )
            _audit_profile(service.db, current_user, row, "connection_profile.reauthorize")
            result = _profile_read(row)
            _record_connection_command(
                service.db,
                request,
                current_user,
                "connection_profile.reauthorize_attention",
                "connection_profile",
                row.id,
                result.model_dump(mode="json"),
            )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc
    except (
        ExecutionControlError,
        SopExecutionConflictError,
        RevisionConflictError,
        WorkItemError,
    ) as exc:
        service.db.rollback()
        raise HTTPException(
            status_code=409,
            detail=getattr(exc, "code", "CONNECTION_REAUTH_ATTENTION_CONFLICT"),
        ) from exc


@router.post("/{profile_id}/disable", response_model=ConnectionProfileRead)
def disable_connection_profile(
    profile_id: str,
    request: ConnectionRevisionCommand,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """以 CAS 停用档案，使后续运行时解析立即失败。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_profile.disable"
    )
    if replay is not None:
        return ConnectionProfileRead.model_validate(replay)
    try:
        row = service.disable_profile(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            expected_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
        _audit_profile(service.db, current_user, row, "connection_profile.disable")
        result = _profile_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_profile.disable",
            "connection_profile",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post("/{profile_id}/wecom-callback", response_model=ConnectionProfileRead)
def rotate_wecom_callback(
    profile_id: str,
    request: WeComCallbackRotate,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionProfileRead:
    """轮换档案专属回调 Token/AESKey，不要求重新提交或暴露 CorpSecret。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    _ensure_connection_secret_backend(get_settings())
    replay = _replayed_connection_command(
        service.db, request, current_user, "connection_profile.wecom_callback.rotate"
    )
    if replay is not None:
        return ConnectionProfileRead.model_validate(replay)
    try:
        row = service.rotate_wecom_callback(
            tenant_id=request.tenant_id,
            profile_id=profile_id,
            callback_token=request.callback_token.get_secret_value(),
            callback_encoding_aes_key=request.callback_encoding_aes_key.get_secret_value(),
            expected_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
        _audit_profile(service.db, current_user, row, "connection_profile.wecom_callback.rotate")
        result = _profile_read(row)
        _record_connection_command(
            service.db,
            request,
            current_user,
            "connection_profile.wecom_callback.rotate",
            "connection_profile",
            row.id,
            result.model_dump(mode="json"),
        )
        service.db.commit()
        service.db.refresh(row)
        return result
    except ConnectionError as exc:
        service.db.rollback()
        raise _http_error(exc) from exc


@router.post("/{profile_id}/probe-read", response_model=ConnectionReadProbeResult)
def probe_connection_read(
    profile_id: str,
    request: ConnectionReadProbe,
    current_user: User = Depends(get_current_user),
    service: ConnectionService = Depends(get_connection_service),
) -> ConnectionReadProbeResult:
    """通过明确 Agent 绑定执行一次生产同构只读调用，用于验收授权闭环。"""

    _ensure_connection_manager(service.db, request.tenant_id, current_user)
    try:
        profile = service.db.get(ConnectionProfile, profile_id)
        if profile is None or profile.tenant_id != request.tenant_id:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        if profile.provider == "slack":
            if request.channel_id is None:
                raise ConnectionError("CONNECTION_READ_INPUT_REQUIRED")
            data = service.read_slack_channel(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                agent_id=request.agent_id,
                actor_user_id=current_user.id,
                channel_id=request.channel_id,
            )
        elif profile.provider == "wecom":
            if request.channel_id is not None:
                raise ConnectionError("CONNECTION_READ_INPUT_UNEXPECTED")
            data = service.read_wecom_application(
                tenant_id=request.tenant_id,
                profile_id=profile_id,
                agent_id=request.agent_id,
                actor_user_id=current_user.id,
            )
        else:
            raise ConnectionError("CONNECTION_PROVIDER_UNSUPPORTED")
        append_user_management_audit(
            service.db,
            current_user=current_user,
            tenant_id=request.tenant_id,
            permission_code="connection_profile.manage",
            action="connection_profile.probe_read",
            action_kind="read",
            outcome="succeeded",
            resource_type="connection_profile",
            resource_id=profile.id,
            detail={"provider": profile.provider, "account_id": profile.account_id},
        )
        service.db.commit()
        return ConnectionReadProbeResult(
            provider=profile.provider,
            account_id=profile.account_id,
            data=data,
        )
    except ConnectionError as exc:
        service.db.commit()
        raise _http_error(exc) from exc


def _profile_read(row: ConnectionProfile) -> ConnectionProfileRead:
    """构造严格白名单响应，永不序列化 secret reference 或密文。"""

    return ConnectionProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        account_id=row.account_id,
        display_name=row.display_name,
        required_scopes=list(row.required_scopes_json or []),
        granted_scopes=list(row.granted_scopes_json or []),
        tool_allowlist=list(row.tool_allowlist_json or []),
        status=row.status,
        health_status=row.health_status,
        health_error_code=row.health_error_code,
        rate_limited_until=_iso(row.rate_limited_until),
        last_checked_at=_iso(row.last_checked_at),
        last_healthy_at=_iso(row.last_healthy_at),
        secret_revision=row.secret_revision,
        revision=row.revision,
        callback_configured=row.callback_configured,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _binding_read(row: AgentConnectionBinding) -> ConnectionBindingRead:
    """构造 Agent 绑定的非敏感投影。"""

    return ConnectionBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        profile_id=row.profile_id,
        allowed_scopes=list(row.allowed_scopes_json or []),
        allowed_actions=list(row.allowed_actions_json or []),
        enabled=row.enabled,
        revision=row.revision,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _principal_binding_read(
    row: ConnectorPrincipalBinding,
) -> ConnectorPrincipalBindingRead:
    """投影主体绑定且永不返回发送者摘要或加密回复目标。"""

    return ConnectorPrincipalBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        profile_id=row.profile_id,
        user_id=row.user_id,
        enabled=row.enabled,
        revision=row.revision,
    )


def _inbound_route_read(row: ConnectorInboundRoute) -> ConnectorInboundRouteRead:
    """投影入站路由的 Agent 和修订信息。"""

    return ConnectorInboundRouteRead(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        profile_id=row.profile_id,
        agent_id=row.agent_id,
        enabled=row.enabled,
        revision=row.revision,
    )


def _audit_profile(
    db: Session,
    current_user: User,
    row: ConnectionProfile,
    action: str,
) -> None:
    """追加不含 token 和 secret reference 的连接档案管理审计。"""

    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=row.tenant_id,
        permission_code="connection_profile.manage",
        action=action,
        action_kind=action.rsplit(".", 1)[-1],
        outcome="succeeded",
        resource_type="connection_profile",
        resource_id=row.id,
        after={
            "provider": row.provider,
            "account_id": row.account_id,
            "required_scopes": sorted(row.required_scopes_json or []),
            "granted_scopes": sorted(row.granted_scopes_json or []),
            "tool_allowlist": sorted(row.tool_allowlist_json or []),
            "status": row.status,
            "health_status": row.health_status,
            "secret_revision": row.secret_revision,
            "revision": row.revision,
        },
    )


def _ensure_connection_manager(db: Session, tenant_id: str, current_user: User) -> None:
    """按稳定治理权限校验连接管理权，并保留平台管理员兼容授权来源。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="connection_profile.manage",
    )


def _ensure_connection_secret_backend(settings: Settings) -> None:
    """在任何凭据写入前拒绝开发占位主密钥，避免可预测密文进入生产数据库。"""

    if not settings.connection_secret_backend_configured:
        raise HTTPException(
            status_code=503,
            detail="CONNECTION_SECRET_BACKEND_NOT_CONFIGURED",
        )


def _replayed_connection_command(
    db: Session,
    request: BaseModel,
    current_user: User,
    command_type: str,
) -> dict[str, object] | None:
    """命中同一管理命令时返回冻结成功响应，语义或 actor 变化则拒绝复用。"""

    tenant_id = str(getattr(request, "tenant_id"))
    command_id = str(getattr(request, "command_id"))
    row = db.exec(
        select(ConnectionCommandReceipt).where(
            ConnectionCommandReceipt.tenant_id == tenant_id,
            ConnectionCommandReceipt.command_id == command_id,
        )
    ).first()
    if row is None:
        return None
    if (
        row.command_type != command_type
        or row.actor_user_id != current_user.id
        or row.payload_checksum != _connection_command_checksum(request)
    ):
        raise HTTPException(status_code=409, detail="CONNECTION_COMMAND_ID_REUSED")
    return dict(row.result_json or {})


def _record_connection_command(
    db: Session,
    request: BaseModel,
    current_user: User,
    command_type: str,
    resource_type: str,
    resource_id: str,
    result: dict[str, object],
) -> ConnectionCommandReceipt:
    """在业务事务内保存不含原始凭据的命令回执，失败时整体回滚。"""

    row = ConnectionCommandReceipt(
        tenant_id=str(getattr(request, "tenant_id")),
        command_id=str(getattr(request, "command_id")),
        command_type=command_type,
        actor_user_id=current_user.id,
        payload_checksum=_connection_command_checksum(request),
        resource_type=resource_type,
        resource_id=resource_id,
        result_json=result,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConnectionError("CONNECTION_COMMAND_ID_REUSED") from exc
    return row


def _connection_command_checksum(request: BaseModel) -> str:
    """绑定命令全部业务语义；凭据只参与不可逆摘要，不进入回执或审计正文。"""

    secret_fields = (
        "token",
        "corp_id",
        "agent_id",
        "corp_secret",
        "callback_token",
        "callback_encoding_aes_key",
    )
    payload = request.model_dump(mode="json", exclude={"command_id", *secret_fields})
    for field_name in secret_fields:
        value = getattr(request, field_name, None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            payload[f"{field_name}_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return canonical_checksum(payload)


def _secret_value(value: SecretStr | None) -> str:
    """读取已经过 Pydantic provider 形状校验的敏感字段，缺失时按领域错误拒绝。"""

    if value is None:
        raise ConnectionError("CONNECTION_SECRET_REQUIRED")
    return value.get_secret_value()


def _optional_secret_value(value: SecretStr | None) -> str | None:
    """读取可选敏感字段；未配置时保持 None 供领域层执行成对校验。"""

    return value.get_secret_value() if value is not None else None


def _apply_oauth_connection(
    service: ConnectionService,
    oauth_state: ConnectionOAuthState,
    token: str,
    actor: User,
) -> ConnectionProfile:
    """在 callback 事务中完成建档、普通重授权或 Attention 原子重授权。"""

    if oauth_state.flow_type == "create":
        request = ConnectionProfileCreate(
            tenant_id=oauth_state.tenant_id,
            command_id=oauth_state.command_id,
            expected_revision=0,
            display_name=oauth_state.display_name or "Slack Workspace",
            token=token,
            required_scopes=set(oauth_state.required_scopes_json or []),
        )
        row = service.create_slack_profile(
            tenant_id=request.tenant_id,
            display_name=request.display_name,
            token=request.token.get_secret_value(),
            required_scopes=set(request.required_scopes),
            actor_user_id=actor.id,
        )
        action = "connection_profile.create"
    else:
        if not oauth_state.profile_id:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        request = ConnectionAttentionReauthorize(
            tenant_id=oauth_state.tenant_id,
            command_id=oauth_state.command_id,
            expected_revision=oauth_state.expected_profile_revision,
            attention_expected_revision=oauth_state.expected_attention_revision or 0,
            token=token,
        )
        validated = service.validate_slack_reauthorization(
            tenant_id=request.tenant_id,
            profile_id=oauth_state.profile_id,
            token=request.token.get_secret_value(),
            expected_revision=request.expected_revision,
        )
        if oauth_state.flow_type == "reauthorize":
            row = service.apply_slack_reauthorization(validated, actor_user_id=actor.id)
        else:
            attention = service.db.get(SopWorkItem, oauth_state.attention_id or "")
            if (
                attention is None
                or attention.tenant_id != oauth_state.tenant_id
                or attention.attention_kind != "reauth"
                or str(attention.payload_json.get("profile_id") or "")
                != oauth_state.profile_id
            ):
                raise ConnectionError("CONNECTION_REAUTH_ATTENTION_NOT_FOUND")
            instance = service.db.get(SopInstance, attention.instance_id)
            if instance is None or instance.tenant_id != oauth_state.tenant_id:
                raise ConnectionError("CONNECTION_REAUTH_ATTENTION_NOT_FOUND")
            store = SopExecutionStore(service.db)
            control = ExecutionControlService(service.db, store)
            with store.owned(instance, worker_id=f"oauth:{attention.id[-16:]}"):
                row = service.apply_slack_reauthorization(validated, actor_user_id=actor.id)
                control.resolve_attention(
                    instance,
                    attention,
                    actor_user_id=actor.id,
                    command_id=request.command_id,
                    command="reauthorize",
                    expected_revision=request.attention_expected_revision,
                )
        action = "connection_profile.reauthorize"
    _audit_profile(service.db, actor, row, action)
    result = _profile_read(row)
    _record_connection_command(
        service.db,
        request,
        actor,
        (
            "connection_profile.create"
            if oauth_state.flow_type == "create"
            else f"connection_profile.{oauth_state.flow_type}"
        ),
        "connection_profile",
        row.id,
        result.model_dump(mode="json"),
    )
    return row


def _oauth_state_matches(
    row: ConnectionOAuthState,
    request: SlackOAuthStart,
    actor_user_id: str,
) -> bool:
    """判断 OAuth start 重放是否保持同一 actor、目标、修订和显示语义。"""

    return (
        row.actor_user_id == actor_user_id
        and row.flow_type == request.flow_type
        and row.profile_id == request.profile_id
        and row.attention_id == request.attention_id
        and (row.display_name or None) == ((request.display_name or "").strip() or None)
        and row.expected_profile_revision == request.expected_profile_revision
        and row.expected_attention_revision == request.expected_attention_revision
        and row.required_scopes_json == ["channels:read"]
    )


def _slack_authorize_url(
    raw_state: str,
    oauth_state: ConnectionOAuthState,
    settings: Settings,
) -> str:
    """只使用服务端固定 Slack 域名、client 和 redirect 配置构造最小 scope 授权地址。"""

    params = {
        "client_id": settings.slack_oauth_client_id,
        "scope": ",".join(oauth_state.required_scopes_json or []),
        "redirect_uri": settings.slack_oauth_redirect_uri,
        "state": raw_state,
    }
    return f"{SLACK_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def _fail_oauth_state(db: Session, row: ConnectionOAuthState, error_code: str) -> None:
    """把 callback 失败持久化为稳定代码并消费 state，禁止 code/state 重放。"""

    if row.status in {"pending", "processing"}:
        row.status = "failed"
        row.error_code = error_code[:128]
        row.consumed_at = utc_now()
        db.add(row)
        db.commit()


def _claim_oauth_state(db: Session, row: ConnectionOAuthState) -> bool:
    """在任何 Slack 外呼前原子抢占 state，禁止并发 callback 重复兑换 code。"""

    now = utc_now()
    result = db.execute(
        update(ConnectionOAuthState)
        .where(
            ConnectionOAuthState.id == row.id,
            ConnectionOAuthState.status == "pending",
            ConnectionOAuthState.expires_at > now,
        )
        .values(status="processing")
    )
    claimed = result.rowcount == 1
    db.commit()
    if claimed:
        db.refresh(row)
    return claimed


def _oauth_redirect(
    outcome: Literal["success", "error"],
    error_code: str | None,
    to_attention: bool,
) -> RedirectResponse:
    """把 OAuth 结果送回固定同源页面，不接收 callback 或租户提供的任意返回地址。"""

    path = "/enterprise/work-items" if to_attention else "/enterprise/connections"
    query = {"slack_oauth": outcome}
    if error_code:
        query["code"] = error_code
    return RedirectResponse(url=f"{path}?{urlencode(query)}", status_code=303)


def _iso(value: object) -> str | None:
    """把可选日期转为 ISO 文本，不引入本地时区。"""

    return value.isoformat() if hasattr(value, "isoformat") else None


def _http_error(exc: ConnectionError) -> HTTPException:
    """把稳定领域错误映射为不会泄漏 provider 原始正文的 HTTP 状态。"""

    not_found = {
        "CONNECTION_PROFILE_NOT_FOUND",
        "CONNECTION_AGENT_NOT_FOUND",
        "CONNECTION_BINDING_NOT_FOUND",
        "CONNECTION_REAUTH_ATTENTION_NOT_FOUND",
    }
    conflict = {
        "CONNECTION_ACCOUNT_ALREADY_EXISTS",
        "CONNECTION_BINDING_ALREADY_EXISTS",
        "CONNECTION_REVISION_CONFLICT",
        "CONNECTION_ACCOUNT_CHANGED",
        "CONNECTION_COMMAND_ID_REUSED",
    }
    forbidden = {
        "CONNECTION_DISABLED",
        "CONNECTION_PROFILE_NOT_ACTIVE",
        "CONNECTION_SCOPE_DENIED",
        "CONNECTION_ACTION_DENIED",
        "CONNECTION_ACTOR_PERMISSION_REQUIRED",
    }
    reauth = {
        "CONNECTION_REAUTH_REQUIRED",
        "CONNECTION_SCOPE_MISSING",
        "CONNECTION_INVALID_AUTH",
        "CONNECTION_TOKEN_EXPIRED",
        "CONNECTION_TOKEN_REVOKED",
        "CONNECTION_ACCOUNT_INACTIVE",
    }
    if exc.code in not_found:
        status_code = 404
    elif exc.code in conflict:
        status_code = 409
    elif exc.code in forbidden:
        status_code = 403
    elif exc.code in reauth:
        status_code = 422
    elif exc.code in {"SLACK_RATE_LIMITED", "CONNECTION_RATE_LIMITED"}:
        status_code = 429
    else:
        status_code = 502
    return HTTPException(status_code=status_code, detail=exc.code)
