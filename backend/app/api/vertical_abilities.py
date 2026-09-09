"""
@Time       : 2026/09/02 10:00
@Author     : zhanglp8181
@File       : vertical_abilities.py
@CallChain  : React Composer → GET /api/chat/abilities → 内置能力目录
@Description: 为登录用户提供平台内置垂直能力包目录和不泄露密钥的 Provider 就绪状态，不按租户或数字员工绑定过滤。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User
from app.security.auth import get_current_user
from app.vertical_abilities.catalog import VerticalAbilityCatalog, VerticalAbilityCatalogRead
from app.vertical_abilities.credentials import (
    JINA_PROVIDER,
    VerticalAbilityCredentialError,
    resolve_jina_credential,
)
from app.vertical_abilities.provider_settings import find_provider_for_vertical_ability


router = APIRouter(prefix="/api/chat", tags=["chat:vertical-abilities"])


class VerticalAbilityReadinessRead(BaseModel):
    """向聊天用户返回能力包是否具备外部 Provider 配置的安全摘要。"""

    ability_id: str
    configured: bool
    provider: str | None = None
    provider_name: str | None = None
    credential_label: str | None = None
    message: str


@router.get("/abilities", response_model=VerticalAbilityCatalogRead)
def list_vertical_abilities(
    _current_user: User = Depends(get_current_user),
) -> VerticalAbilityCatalogRead:
    """返回当前项目版本全部可选垂直能力包，健康提示不隐藏任何能力。"""

    return VerticalAbilityCatalog().list_public()


@router.get(
    "/abilities/{ability_id}/readiness",
    response_model=VerticalAbilityReadinessRead,
)
def read_vertical_ability_readiness(
    ability_id: str,
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> VerticalAbilityReadinessRead:
    """返回选中能力后的 Provider 配置状态，不返回密钥、掩码或租户私有信息。"""

    try:
        VerticalAbilityCatalog().resolve(ability_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "VERTICAL_ABILITY_NOT_FOUND", "message": "能力包不存在。"},
        ) from exc

    provider = find_provider_for_vertical_ability(ability_id)
    if provider is None:
        return VerticalAbilityReadinessRead(
            ability_id=ability_id,
            configured=True,
            message="能力包已加载，无需额外配置。",
        )
    if provider.provider != JINA_PROVIDER:
        return VerticalAbilityReadinessRead(
            ability_id=ability_id,
            configured=True,
            provider=provider.provider,
            provider_name=provider.provider_name,
            credential_label=provider.credential_label,
            message=f"{provider.provider_name} 配置状态将在能力包执行时确认。",
        )

    try:
        credential = resolve_jina_credential(db, settings=get_settings())
    except VerticalAbilityCredentialError:
        return _jina_readiness(
            ability_id,
            configured=False,
            message="Jina API Key 当前不可用，请联系管理员前往“能力包配置”页面配置。",
        )
    if credential.source == "none":
        return _jina_readiness(
            ability_id,
            configured=False,
            message="Jina API Key 尚未配置，请联系管理员前往“能力包配置”页面配置。",
        )
    return _jina_readiness(
        ability_id,
        configured=True,
        message="Jina API Key 已配置。",
    )


def _jina_readiness(
    ability_id: str,
    *,
    configured: bool,
    message: str,
) -> VerticalAbilityReadinessRead:
    """构造 Jina 就绪摘要，集中保持 Provider 元数据一致且不携带凭据。"""

    provider = find_provider_for_vertical_ability(ability_id)
    if provider is None or provider.provider != JINA_PROVIDER:
        raise RuntimeError("Jina readiness requested for a non-Jina ability")
    return VerticalAbilityReadinessRead(
        ability_id=ability_id,
        configured=configured,
        provider=provider.provider,
        provider_name=provider.provider_name,
        credential_label=provider.credential_label,
        message=message,
    )
