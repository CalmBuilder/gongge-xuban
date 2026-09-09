"""
@Time       : 2026/09/02 14:20
@Author     : zhanglp8181
@File       : credentials.py
@CallChain  : 垂直能力 API/ToolExecutor → PlatformSecret → Fernet 密文 → Provider 请求
@Description: 管理平台级垂直能力 Provider 凭据，支持数据库密文优先和环境变量兼容回退。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlmodel import Session, select

from app.audit.service import append_user_management_audit
from app.config import Settings, get_settings
from app.db.models import PlatformSecret, User, utc_now
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret


PLATFORM_SCOPE_KEY = "platform"
JINA_PROVIDER = "jina"
GROQ_PROVIDER = "groq"
OPENAI_PROVIDER = "openai"
JINA_SECRET_KIND = "api_key"
CredentialSource = Literal["database", "environment", "none"]
HealthStatus = Literal["unverified", "healthy", "degraded", "unhealthy"]

_PROVIDER_ENV_FIELDS = {
    JINA_PROVIDER: "vertical_ability_jina_api_key",
    GROQ_PROVIDER: "vertical_ability_groq_api_key",
    OPENAI_PROVIDER: "vertical_ability_openai_api_key",
}


class VerticalAbilityCredentialError(RuntimeError):
    """表示平台凭据缺失、解密失败或发生并发修订冲突。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        """保存可供 API 和 ToolExecutor 使用的稳定错误码。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class VerticalAbilityCredential:
    """表示一次 Provider 调用实际使用的凭据来源，不暴露给日志的原始值。"""

    value: str
    source: CredentialSource
    revision: int


@dataclass(frozen=True, slots=True)
class JinaCredentialView:
    """表示设置页可见的 Jina 凭据状态，永不携带完整密钥。"""

    provider: str
    configured: bool
    source: CredentialSource
    api_key_masked: str | None
    revision: int
    health_status: HealthStatus
    health_error_code: str | None
    last_checked_at: datetime | None
    last_healthy_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class VerticalAbilityCredentialView:
    """表示任意平台 Provider 的配置状态，永不携带完整密钥。"""

    provider: str
    configured: bool
    source: CredentialSource
    api_key_masked: str | None
    revision: int
    health_status: HealthStatus
    health_error_code: str | None
    last_checked_at: datetime | None
    last_healthy_at: datetime | None
    updated_at: datetime | None


def resolve_provider_credential(
    db: Session | None,
    provider: str,
    *,
    settings: Settings | None = None,
) -> VerticalAbilityCredential:
    """按数据库密文、对应环境变量、空值顺序解析任意平台 Provider 密钥。"""

    normalized_provider = _normalize_provider(provider)
    row = get_platform_secret(db, normalized_provider) if db is not None else None
    if row is not None and row.status == "active" and row.encrypted_value:
        try:
            value = decrypt_secret(row.encrypted_value).strip()
        except ValueError as exc:
            raise VerticalAbilityCredentialError(
                "VERTICAL_ABILITY_CREDENTIAL_DECRYPT_FAILED",
                f"{normalized_provider} 平台凭据无法解密，请重新保存。",
                status_code=500,
            ) from exc
        if value:
            return VerticalAbilityCredential(value=value, source="database", revision=row.revision)

    effective_settings = settings or get_settings()
    field_name = _PROVIDER_ENV_FIELDS[normalized_provider]
    value = str(getattr(effective_settings, field_name, "") or "").strip()
    if value:
        return VerticalAbilityCredential(value=value, source="environment", revision=0)
    return VerticalAbilityCredential(value="", source="none", revision=0)


def get_platform_secret(db: Session, provider: str) -> PlatformSecret | None:
    """读取当前生效的任意平台 Provider API Key 密文记录。"""

    normalized_provider = _normalize_provider(provider)
    return db.exec(
        select(PlatformSecret).where(
            PlatformSecret.scope_key == PLATFORM_SCOPE_KEY,
            PlatformSecret.provider == normalized_provider,
            PlatformSecret.secret_kind == JINA_SECRET_KIND,
            PlatformSecret.status == "active",
        )
    ).first()


def save_platform_credential(
    db: Session,
    *,
    provider: str,
    api_key: str,
    current_user: User,
    expected_revision: int = 0,
    credential_label: str | None = None,
) -> PlatformSecret:
    """加密保存任意平台 Provider 密钥，并用 revision 防止并发覆盖。"""

    normalized_provider = _normalize_provider(provider)
    value = str(api_key or "").strip()
    label = credential_label or f"{normalized_provider} API Key"
    if not value:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_INVALID",
            f"{label} 不能为空。",
        )
    if len(value) > 4096:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_INVALID",
            f"{label} 超出长度上限。",
        )
    row = db.exec(
        select(PlatformSecret)
        .where(
            PlatformSecret.scope_key == PLATFORM_SCOPE_KEY,
            PlatformSecret.provider == normalized_provider,
            PlatformSecret.secret_kind == JINA_SECRET_KIND,
        )
        .with_for_update()
    ).first()
    if row is not None and row.revision != expected_revision:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_REVISION_CONFLICT",
            f"{label} 已被其他管理员更新，请刷新后重试。",
            status_code=409,
        )
    if row is None and expected_revision != 0:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_REVISION_CONFLICT",
            f"{label} 状态已变化，请刷新后重试。",
            status_code=409,
        )

    now = utc_now()
    if row is None:
        row = PlatformSecret(
            scope_key=PLATFORM_SCOPE_KEY,
            provider=normalized_provider,
            secret_kind=JINA_SECRET_KIND,
            encrypted_value=encrypt_secret(value),
            revision=1,
            status="active",
            health_status="unverified",
            created_by_user_id=current_user.id,
            updated_by_user_id=current_user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        action_kind = "create"
        before: dict[str, object] = {}
    else:
        before = {
            "provider": row.provider,
            "secret_kind": row.secret_kind,
            "revision": row.revision,
            "configured": True,
            "health_status": row.health_status,
        }
        row.encrypted_value = encrypt_secret(value)
        row.revision += 1
        row.status = "active"
        row.health_status = "unverified"
        row.health_error_code = None
        row.last_checked_at = None
        row.last_healthy_at = None
        row.updated_by_user_id = current_user.id
        row.updated_at = now
        action_kind = "update"

    db.flush()
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action=f"vertical_ability.{normalized_provider}_credential.save",
        action_kind=action_kind,
        outcome="success",
        resource_type="platform_vertical_ability_credential",
        resource_id=f"{normalized_provider}:{JINA_SECRET_KIND}",
        before=before,
        after={
            "provider": normalized_provider,
            "secret_kind": JINA_SECRET_KIND,
            "revision": row.revision,
            "configured": True,
            "health_status": row.health_status,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def mark_provider_credential_health(
    db: Session,
    *,
    provider: str,
    revision: int,
    health_status: HealthStatus,
    health_error_code: str | None = None,
) -> PlatformSecret | None:
    """仅更新仍匹配本次验证 revision 的 Provider 健康状态。"""

    row = get_platform_secret(db, provider)
    if row is None or row.revision != revision:
        return row
    now = utc_now()
    row.health_status = health_status
    row.health_error_code = health_error_code
    row.last_checked_at = now
    if health_status == "healthy":
        row.last_healthy_at = now
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def provider_credential_view(
    db: Session,
    provider: str,
    *,
    settings: Settings | None = None,
) -> VerticalAbilityCredentialView:
    """组装任意 Provider 的脱敏状态，供配置卡片安全展示。"""

    normalized_provider = _normalize_provider(provider)
    row = get_platform_secret(db, normalized_provider)
    credential = resolve_provider_credential(db, normalized_provider, settings=settings)
    if row is not None and credential.source == "database":
        return VerticalAbilityCredentialView(
            provider=normalized_provider,
            configured=True,
            source="database",
            api_key_masked=mask_secret(credential.value),
            revision=row.revision,
            health_status=row.health_status,
            health_error_code=row.health_error_code,
            last_checked_at=row.last_checked_at,
            last_healthy_at=row.last_healthy_at,
            updated_at=row.updated_at,
        )
    if credential.source == "environment":
        return VerticalAbilityCredentialView(
            provider=normalized_provider,
            configured=True,
            source="environment",
            api_key_masked=mask_secret(credential.value),
            revision=0,
            health_status="unverified",
            health_error_code=None,
            last_checked_at=None,
            last_healthy_at=None,
            updated_at=None,
        )
    return VerticalAbilityCredentialView(
        provider=normalized_provider,
        configured=False,
        source="none",
        api_key_masked=None,
        revision=0,
        health_status="unverified",
        health_error_code=None,
        last_checked_at=None,
        last_healthy_at=None,
        updated_at=None,
    )


def _normalize_provider(provider: str) -> str:
    """校验 Provider 标识只来自已支持的平台密钥字段。"""

    normalized_provider = str(provider or "").strip().casefold()
    if normalized_provider not in _PROVIDER_ENV_FIELDS:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_PROVIDER_NOT_FOUND",
            "能力包 Provider 配置不存在。",
            status_code=404,
        )
    return normalized_provider


def resolve_jina_credential(
    db: Session | None,
    *,
    settings: Settings | None = None,
) -> VerticalAbilityCredential:
    """按数据库密文、环境变量、空值顺序解析本次 Jina 请求的密钥。"""

    row = get_jina_secret(db) if db is not None else None
    if row is not None and row.status == "active" and row.encrypted_value:
        try:
            value = decrypt_secret(row.encrypted_value).strip()
        except ValueError as exc:
            raise VerticalAbilityCredentialError(
                "VERTICAL_ABILITY_CREDENTIAL_DECRYPT_FAILED",
                "Jina 平台凭据无法解密，请重新保存。",
                status_code=500,
            ) from exc
        if value:
            return VerticalAbilityCredential(value=value, source="database", revision=row.revision)

    effective_settings = settings or get_settings()
    value = str(getattr(effective_settings, "vertical_ability_jina_api_key", "") or "").strip()
    if value:
        return VerticalAbilityCredential(value=value, source="environment", revision=0)
    return VerticalAbilityCredential(value="", source="none", revision=0)


def get_jina_secret(db: Session) -> PlatformSecret | None:
    """读取平台当前生效的 Jina API Key 密文记录。"""

    return db.exec(
        select(PlatformSecret).where(
            PlatformSecret.scope_key == PLATFORM_SCOPE_KEY,
            PlatformSecret.provider == JINA_PROVIDER,
            PlatformSecret.secret_kind == JINA_SECRET_KIND,
            PlatformSecret.status == "active",
        )
    ).first()


def save_jina_credential(
    db: Session,
    *,
    api_key: str,
    current_user: User,
    expected_revision: int = 0,
) -> PlatformSecret:
    """加密保存平台 Jina 密钥并用 revision 防止设置页覆盖并发修改。"""

    value = str(api_key or "").strip()
    if not value:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_INVALID",
            "Jina API Key 不能为空。",
        )
    if len(value) > 4096:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_INVALID",
            "Jina API Key 超出长度上限。",
        )
    row = db.exec(
        select(PlatformSecret)
        .where(
            PlatformSecret.scope_key == PLATFORM_SCOPE_KEY,
            PlatformSecret.provider == JINA_PROVIDER,
            PlatformSecret.secret_kind == JINA_SECRET_KIND,
        )
        .with_for_update()
    ).first()
    if row is not None and row.revision != expected_revision:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_REVISION_CONFLICT",
            "Jina 凭据已被其他管理员更新，请刷新后重试。",
            status_code=409,
        )
    if row is None and expected_revision != 0:
        raise VerticalAbilityCredentialError(
            "VERTICAL_ABILITY_CREDENTIAL_REVISION_CONFLICT",
            "Jina 凭据状态已变化，请刷新后重试。",
            status_code=409,
        )

    now = utc_now()
    if row is None:
        row = PlatformSecret(
            scope_key=PLATFORM_SCOPE_KEY,
            provider=JINA_PROVIDER,
            secret_kind=JINA_SECRET_KIND,
            encrypted_value=encrypt_secret(value),
            revision=1,
            status="active",
            health_status="unverified",
            created_by_user_id=current_user.id,
            updated_by_user_id=current_user.id,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        action_kind = "create"
        before: dict[str, object] = {}
    else:
        before = {
            "provider": row.provider,
            "secret_kind": row.secret_kind,
            "revision": row.revision,
            "configured": True,
            "health_status": row.health_status,
        }
        row.encrypted_value = encrypt_secret(value)
        row.revision += 1
        row.status = "active"
        row.health_status = "unverified"
        row.health_error_code = None
        row.last_checked_at = None
        row.last_healthy_at = None
        row.updated_by_user_id = current_user.id
        row.updated_at = now
        action_kind = "update"

    db.flush()
    append_user_management_audit(
        db,
        current_user=current_user,
        tenant_id=current_user.tenant_id,
        permission_code=None,
        action="vertical_ability.jina_credential.save",
        action_kind=action_kind,
        outcome="success",
        resource_type="platform_vertical_ability_credential",
        resource_id=f"{JINA_PROVIDER}:{JINA_SECRET_KIND}",
        before=before,
        after={
            "provider": JINA_PROVIDER,
            "secret_kind": JINA_SECRET_KIND,
            "revision": row.revision,
            "configured": True,
            "health_status": row.health_status,
        },
    )
    db.commit()
    db.refresh(row)
    return row


def mark_jina_credential_health(
    db: Session,
    *,
    revision: int,
    health_status: HealthStatus,
    health_error_code: str | None = None,
) -> PlatformSecret | None:
    """仅更新仍匹配本次验证 revision 的密钥健康状态，避免旧验证覆盖新密钥。"""

    row = get_jina_secret(db)
    if row is None or row.revision != revision:
        return row
    now = utc_now()
    row.health_status = health_status
    row.health_error_code = health_error_code
    row.last_checked_at = now
    if health_status == "healthy":
        row.last_healthy_at = now
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def jina_credential_view(
    db: Session,
    *,
    settings: Settings | None = None,
) -> JinaCredentialView:
    """组装设置页状态，只返回掩码、修订号和健康信息。"""

    row = get_jina_secret(db)
    credential = resolve_jina_credential(db, settings=settings)
    if row is not None and credential.source == "database":
        configured = True
        masked = mask_secret(credential.value)
        revision = row.revision
        health_status = row.health_status
        health_error_code = row.health_error_code
        last_checked_at = row.last_checked_at
        last_healthy_at = row.last_healthy_at
        updated_at = row.updated_at
    elif credential.source == "environment":
        configured = True
        masked = mask_secret(credential.value)
        revision = 0
        health_status = "unverified"
        health_error_code = None
        last_checked_at = None
        last_healthy_at = None
        updated_at = None
    else:
        configured = False
        masked = None
        revision = 0
        health_status = "unverified"
        health_error_code = None
        last_checked_at = None
        last_healthy_at = None
        updated_at = None
    return JinaCredentialView(
        provider=JINA_PROVIDER,
        configured=configured,
        source=credential.source,
        api_key_masked=masked,
        revision=revision,
        health_status=health_status,
        health_error_code=health_error_code,
        last_checked_at=last_checked_at,
        last_healthy_at=last_healthy_at,
        updated_at=updated_at,
    )
