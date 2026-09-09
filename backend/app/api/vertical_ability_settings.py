"""
@Time       : 2026/09/02 14:30
@Author     : zhanglp8181
@File       : vertical_ability_settings.py
@CallChain  : React 能力包配置页 → platform vertical ability settings API → PlatformSecret
@Description: 提供数据驱动的能力包 Provider 配置卡片 API，并安全维护 Jina、Groq、OpenAI 等平台级密钥。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User
from app.security.auth import get_current_user
from app.security.dns_resolver import default_adaptive_resolver
from app.security.permissions import is_admin_user
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
    execute_vertical_provider_probe,
)
from app.vertical_abilities.credentials import (
    JINA_PROVIDER,
    VerticalAbilityCredentialError,
    jina_credential_view,
    mark_jina_credential_health,
    mark_provider_credential_health,
    provider_credential_view,
    resolve_jina_credential,
    resolve_provider_credential,
    save_jina_credential,
    save_platform_credential,
)
from app.vertical_abilities.provider_settings import (
    PLATFORM_VERTICAL_ABILITY_PROVIDERS,
    VerticalAbilityProviderDefinition,
    find_vertical_ability_provider,
)
from app.vertical_abilities.catalog import (
    VerticalAbilityCatalog,
    VerticalAbilityDefinition,
    VerticalAbilityResourceDefinition,
    VerticalAbilityRead,
)


router = APIRouter(
    prefix="/api/enterprise/vertical-ability-settings",
    tags=["enterprise:vertical-ability-settings"],
)


class VerticalAbilityProviderRead(BaseModel):
    """设置页可读取的 Provider 卡片状态，不包含完整凭据。"""

    provider: str
    ability_ids: list[str]
    ability_names: list[str]
    provider_name: str
    description: str
    credential_label: str
    credential_placeholder: str
    credential_setup_text: str | None = None
    credential_setup_url: str | None = None
    credential_setup_link_label: str | None = None
    supports_verification: bool = True
    configured: bool
    source: Literal["database", "environment", "none"]
    credential_masked: str | None = None
    revision: int = Field(ge=0)
    health_status: Literal["unverified", "healthy", "degraded", "unhealthy"]
    health_error_code: str | None = None
    last_checked_at: datetime | None = None
    last_healthy_at: datetime | None = None
    updated_at: datetime | None = None
    verification_success: bool | None = None
    message: str | None = None


class VerticalAbilitySettingsRead(BaseModel):
    """返回能力包配置首页的能力包卡片及兼容的 Provider 状态。"""

    abilities: list["VerticalAbilityConfigurationSummaryRead"] = Field(default_factory=list)
    providers: list[VerticalAbilityProviderRead] = Field(default_factory=list)


class VerticalAbilityConfigurationSummaryRead(BaseModel):
    """返回能力包首页卡片所需的版本、平台和配置汇总状态。"""

    id: str
    name: str
    category: str
    icon_key: str
    description: str
    platform_tags: list[str] = Field(default_factory=list)
    version: str
    checksum: str
    configuration_status: Literal["ready", "partial", "needs_configuration"]
    configuration_message: str
    resource_count: int = Field(ge=0)
    ready_resource_count: int = Field(ge=0)
    provider_ids: list[str] = Field(default_factory=list)


class VerticalAbilityResourceRead(BaseModel):
    """返回能力包详情页中的平台服务、连接、运行时和公共资源状态。"""

    resource_id: str
    resource_type: Literal["platform_secret", "connection", "runtime", "public"]
    name: str
    description: str
    provider: str | None = None
    status: Literal["ready", "needs_configuration", "not_connected", "diagnostic"]
    status_message: str


class VerticalAbilityConfigurationDetailRead(BaseModel):
    """返回单个能力包配置详情及其 Provider 卡片。"""

    ability: VerticalAbilityRead
    configuration_status: Literal["ready", "partial", "needs_configuration"]
    configuration_message: str
    resources: list[VerticalAbilityResourceRead] = Field(default_factory=list)
    providers: list[VerticalAbilityProviderRead] = Field(default_factory=list)


class JinaCredentialRead(VerticalAbilityProviderRead):
    """兼容既有 Jina 单项接口的强类型响应。"""

    provider: Literal["jina"] = JINA_PROVIDER


class VerticalAbilityCredentialUpdate(BaseModel):
    """接收一次性 Provider 凭据和乐观锁修订号。"""

    credential: SecretStr = Field(min_length=1, max_length=4096)
    expected_revision: int = Field(default=0, ge=0)


@router.get("", response_model=VerticalAbilitySettingsRead)
def read_vertical_ability_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilitySettingsRead:
    """返回平台能力包配置中心的卡片目录，页面不绑定某一个能力包。"""

    _ensure_platform_admin(current_user)
    try:
        providers = _provider_reads(db)
        return VerticalAbilitySettingsRead(
            abilities=[
                _to_configuration_summary(definition, providers)
                for definition in VerticalAbilityCatalog().definitions()
            ],
            providers=providers,
        )
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc


@router.get("/abilities/{ability_id}", response_model=VerticalAbilityConfigurationDetailRead)
def read_vertical_ability_configuration(
    ability_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityConfigurationDetailRead:
    """返回能力包配置详情，页面根据资源类型决定展示 Provider 或状态卡片。"""

    _ensure_platform_admin(current_user)
    try:
        definition = VerticalAbilityCatalog().resolve(ability_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VERTICAL_ABILITY_NOT_FOUND",
                "message": "能力包不存在。",
            },
        ) from exc
    try:
        providers = _provider_reads(db)
        summary = _to_configuration_summary(definition, providers)
        resources = [
            _to_resource_read(resource, providers)
            for resource in definition.configuration_resources
        ]
        return VerticalAbilityConfigurationDetailRead(
            ability=definition.public_read(),
            configuration_status=summary.configuration_status,
            configuration_message=summary.configuration_message,
            resources=resources,
            providers=[
                provider
                for provider in providers
                if definition.id in provider.ability_ids
            ],
        )
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc


@router.get("/jina", response_model=JinaCredentialRead)
def read_jina_credential(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> JinaCredentialRead:
    """返回管理员可见的 Jina 配置状态，普通用户不需要访问凭据页面。"""

    _ensure_platform_admin(current_user)
    try:
        return _to_read(db)
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc


@router.put("/{provider}", response_model=VerticalAbilityProviderRead)
def update_vertical_ability_provider(
    provider: str,
    payload: VerticalAbilityCredentialUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityProviderRead:
    """按配置卡片的 Provider 标识保存凭据，保存后立即供后续能力执行读取。"""

    _ensure_platform_admin(current_user)
    definition = _require_provider(provider)
    try:
        if definition.provider == JINA_PROVIDER:
            save_jina_credential(
                db,
                api_key=payload.credential.get_secret_value(),
                current_user=current_user,
                expected_revision=payload.expected_revision,
            )
        else:
            save_platform_credential(
                db,
                provider=definition.provider,
                api_key=payload.credential.get_secret_value(),
                current_user=current_user,
                expected_revision=payload.expected_revision,
                credential_label=definition.credential_label,
            )
        return _to_provider_read(
            db,
            definition=definition,
            message=f"{definition.credential_label} 已安全保存，可供{_ability_names(definition)}使用。",
        )
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc


@router.post("/{provider}/verify", response_model=VerticalAbilityProviderRead)
def verify_vertical_ability_provider(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityProviderRead:
    """按配置卡片的 Provider 标识执行最小连接验证并持久化健康状态。"""

    _ensure_platform_admin(current_user)
    definition = _require_provider(provider)
    if definition.provider == JINA_PROVIDER:
        return _verify_jina_credential(db, definition=definition)
    return _verify_platform_provider(db, definition=definition)


@router.get("/{provider}", response_model=VerticalAbilityProviderRead)
def read_vertical_ability_provider(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityProviderRead:
    """按配置卡片的 Provider 标识读取单项状态，兼容页面局部刷新。"""

    _ensure_platform_admin(current_user)
    definition = _require_provider(provider)
    try:
        return _to_provider_read(db, definition=definition)
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc


def _provider_reads(db: Session) -> list[VerticalAbilityProviderRead]:
    """读取配置目录中的全部 Provider 状态，供首页和详情页复用。"""

    return [
        _to_provider_read(db, definition=definition)
        for definition in PLATFORM_VERTICAL_ABILITY_PROVIDERS
    ]


def _to_configuration_summary(
    definition: VerticalAbilityDefinition,
    providers: list[VerticalAbilityProviderRead],
) -> VerticalAbilityConfigurationSummaryRead:
    """把能力包定义和资源状态汇总成首页卡片，不改变能力可见性。"""

    resources = [
        _to_resource_read(resource, providers)
        for resource in definition.configuration_resources
    ]
    platform_resources = [
        resource for resource in resources if resource.resource_type != "connection"
    ]
    ready_count = sum(resource.status == "ready" for resource in platform_resources)
    if not platform_resources or ready_count == len(platform_resources):
        status: Literal["ready", "partial", "needs_configuration"] = "ready"
        message = "平台依赖已满足；网站登录连接由用户按需在对话框中完成。"
    elif ready_count > 0:
        status = "partial"
        message = "部分平台依赖尚未配置；公共渠道仍可用，网站登录连接由用户按需完成。"
    else:
        status = "needs_configuration"
        message = "请进入详情页完成平台 Provider 配置；能力包仍会显示在对话框中，网站连接由用户按需完成。"
    return VerticalAbilityConfigurationSummaryRead(
        id=definition.id,
        name=definition.name,
        category=definition.category,
        icon_key=definition.icon_key,
        description=definition.description,
        platform_tags=list(definition.platform_tags),
        version=definition.version,
        checksum=definition.checksum,
        configuration_status=status,
        configuration_message=message,
        resource_count=len(resources),
        ready_resource_count=ready_count,
        provider_ids=list(
            dict.fromkeys(
                resource.provider
                for resource in definition.configuration_resources
                if resource.provider
            )
        ),
    )


def _to_resource_read(
    resource: VerticalAbilityResourceDefinition,
    providers: list[VerticalAbilityProviderRead],
) -> VerticalAbilityResourceRead:
    """将能力包资源定义映射为详情页可展示的状态，不暴露凭据内容。"""

    provider = next(
        (item for item in providers if item.provider == resource.provider),
        None,
    )
    if resource.resource_type == "platform_secret":
        if provider is None:
            return VerticalAbilityResourceRead(
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                name=resource.name,
                description=resource.description,
                provider=resource.provider,
                status="needs_configuration",
                status_message="该平台服务尚未登记配置卡片。",
            )
        return VerticalAbilityResourceRead(
            resource_id=resource.resource_id,
            resource_type=resource.resource_type,
            name=resource.name,
            description=resource.description,
            provider=resource.provider,
            status="ready" if provider.configured else "needs_configuration",
            status_message=(
                "已配置，可在下方 Provider 卡片中验证连接。"
                if provider.configured
                else "尚未配置，请在下方 Provider 卡片中保存服务凭据。"
            ),
        )
    if resource.resource_type == "public":
        return VerticalAbilityResourceRead(
            resource_id=resource.resource_id,
            resource_type=resource.resource_type,
            name=resource.name,
            description=resource.description,
            provider=resource.provider,
            status="ready",
            status_message="平台运行时已提供，无需额外配置。",
        )
    if resource.resource_type == "connection":
        return VerticalAbilityResourceRead(
            resource_id=resource.resource_id,
            resource_type=resource.resource_type,
            name=resource.name,
            description=resource.description,
            provider=resource.provider,
            status="not_connected",
            status_message="该连接资源由用户在对话框的网站接入流程中完成授权，不属于平台公共密钥配置。",
        )
    return VerticalAbilityResourceRead(
        resource_id=resource.resource_id,
        resource_type=resource.resource_type,
        name=resource.name,
        description=resource.description,
        provider=resource.provider,
        status="diagnostic",
        status_message="该运行时资源由平台部署环境提供，详情页仅展示诊断状态。",
    )


def _verify_jina_credential(
    db: Session,
    *,
    definition: VerticalAbilityProviderDefinition,
) -> JinaCredentialRead:
    """验证 Jina 当前凭据并返回关联配置卡片状态。"""

    try:
        credential = resolve_jina_credential(db, settings=get_settings())
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc
    if credential.source == "none":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
                "message": "请先配置 Jina API Key。",
            },
        )
    try:
        execute_vertical_adapter(
            "jina.reader",
            {"url": "https://example.com/"},
            settings=get_settings(),
            resolver=default_adaptive_resolver(),
            jina_api_key=credential.value,
        )
    except VerticalAbilityAdapterError as exc:
        if credential.source == "database":
            mark_jina_credential_health(
                db,
                revision=credential.revision,
                health_status="unhealthy",
                health_error_code=exc.code,
            )
        return _to_read(
            db,
            definition=definition,
            verification_success=False,
            message=exc.message,
        )
    except Exception:
        if credential.source == "database":
            mark_jina_credential_health(
                db,
                revision=credential.revision,
                health_status="unhealthy",
                health_error_code="VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
            )
        return _to_read(
            db,
            definition=definition,
            verification_success=False,
            message="Jina 连接验证失败，请检查网络或 API Key。",
        )

    if credential.source == "database":
        mark_jina_credential_health(
            db,
            revision=credential.revision,
            health_status="healthy",
            health_error_code=None,
        )
    return _to_read(
        db,
        definition=definition,
        verification_success=True,
        message=f"{definition.provider_name} 连接验证成功，{_ability_names(definition)}已可使用。",
    )


def _verify_platform_provider(
    db: Session,
    *,
    definition: VerticalAbilityProviderDefinition,
) -> VerticalAbilityProviderRead:
    """验证 Groq/OpenAI 平台服务的 API Key，并只持久化脱敏健康状态。"""

    try:
        credential = resolve_provider_credential(
            db,
            definition.provider,
            settings=get_settings(),
        )
    except VerticalAbilityCredentialError as exc:
        raise _credential_http_error(exc) from exc
    if credential.source == "none":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VERTICAL_ABILITY_CREDENTIAL_NOT_CONFIGURED",
                "message": f"请先配置 {definition.credential_label}。",
            },
        )
    try:
        execute_vertical_provider_probe(
            definition.provider,
            credential.value,
            settings=get_settings(),
            resolver=default_adaptive_resolver(),
        )
    except VerticalAbilityAdapterError as exc:
        if credential.source == "database":
            mark_provider_credential_health(
                db,
                provider=definition.provider,
                revision=credential.revision,
                health_status="unhealthy",
                health_error_code=exc.code,
            )
        return _to_provider_read(
            db,
            definition=definition,
            verification_success=False,
            message=exc.message,
        )
    except Exception:
        if credential.source == "database":
            mark_provider_credential_health(
                db,
                provider=definition.provider,
                revision=credential.revision,
                health_status="unhealthy",
                health_error_code="VERTICAL_ABILITY_PROVIDER_UNAVAILABLE",
            )
        return _to_provider_read(
            db,
            definition=definition,
            verification_success=False,
            message=f"{definition.provider_name} 连接验证失败，请检查网络或 API Key。",
        )

    if credential.source == "database":
        mark_provider_credential_health(
            db,
            provider=definition.provider,
            revision=credential.revision,
            health_status="healthy",
            health_error_code=None,
        )
    return _to_provider_read(
        db,
        definition=definition,
        verification_success=True,
        message=f"{definition.provider_name} 连接验证成功，{_ability_names(definition)}已可使用。",
    )


def _ensure_platform_admin(current_user: User) -> None:
    """限制平台密钥管理操作只由管理员完成，但不影响普通用户使用能力包。"""

    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage platform credentials")


def _require_provider(provider: str) -> VerticalAbilityProviderDefinition:
    """解析配置卡片的 Provider 标识，拒绝未注册的动态路径。"""

    definition = find_vertical_ability_provider(provider)
    if definition is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VERTICAL_ABILITY_PROVIDER_NOT_FOUND",
                "message": "能力包 Provider 配置不存在。",
            },
        )
    return definition


def _provider_not_implemented(definition: VerticalAbilityProviderDefinition) -> HTTPException:
    """为已登记但尚未接入保存处理器的 Provider 返回稳定错误。"""

    return HTTPException(
        status_code=501,
        detail={
            "code": "VERTICAL_ABILITY_PROVIDER_NOT_IMPLEMENTED",
            "message": f"{definition.provider_name} 配置暂未接入。",
        },
    )


def _ability_names(definition: VerticalAbilityProviderDefinition) -> str:
    """把配置卡片关联的能力包名称合并成用户可读短语。"""

    return "、".join(definition.ability_names) or "相关能力包"


def _to_provider_read(
    db: Session,
    *,
    definition: VerticalAbilityProviderDefinition,
    verification_success: bool | None = None,
    message: str | None = None,
) -> VerticalAbilityProviderRead:
    """把 Provider 凭据状态和卡片元数据组合成安全的配置项响应。"""

    view = (
        jina_credential_view(db, settings=get_settings())
        if definition.provider == JINA_PROVIDER
        else provider_credential_view(db, definition.provider, settings=get_settings())
    )
    return VerticalAbilityProviderRead(
        provider=definition.provider,
        ability_ids=list(definition.ability_ids),
        ability_names=list(definition.ability_names),
        provider_name=definition.provider_name,
        description=definition.description,
        credential_label=definition.credential_label,
        credential_placeholder=definition.credential_placeholder,
        credential_setup_text=definition.credential_setup_text,
        credential_setup_url=definition.credential_setup_url,
        credential_setup_link_label=definition.credential_setup_link_label,
        supports_verification=definition.supports_verification,
        configured=view.configured,
        source=view.source,
        credential_masked=view.api_key_masked,
        revision=view.revision,
        health_status=view.health_status,
        health_error_code=view.health_error_code,
        last_checked_at=view.last_checked_at,
        last_healthy_at=view.last_healthy_at,
        updated_at=view.updated_at,
        verification_success=verification_success,
        message=message,
    )


def _to_read(
    db: Session,
    *,
    definition: VerticalAbilityProviderDefinition | None = None,
    verification_success: bool | None = None,
    message: str | None = None,
) -> JinaCredentialRead:
    """把 Jina 凭据领域状态转换成兼容旧接口且不泄露密钥的响应。"""

    effective_definition = definition or _require_provider(JINA_PROVIDER)
    provider_read = _to_provider_read(
        db,
        definition=effective_definition,
        verification_success=verification_success,
        message=message,
    )
    return JinaCredentialRead.model_validate(provider_read.model_dump())


def _credential_http_error(error: VerticalAbilityCredentialError) -> HTTPException:
    """把凭据领域错误转换成稳定且不包含密钥的 API 错误。"""

    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )
