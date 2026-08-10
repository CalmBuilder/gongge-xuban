"""
@Time       : 2026/07/27 15:45
@Author     : zhanglp8181
@File       : model_configs.py
@CallChain  : FastAPI 模型配置路由 → 租户级事务锁/SQLModel → LLMClient
@Description: 管理租户模型配置，并以原子切换和数据库约束维护唯一默认模型。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ModelConfig, Tenant, User, utc_now
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.llm import LLMClient, LLMError
from app.llm.schemas import (
    ModelCapabilityPreflightResponse,
    ModelConfigCreateRequest,
    ModelConfigRead,
    ModelConfigTestResponse,
    ModelConfigUpdateRequest,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.security.permissions import ensure_tenant_admin, require_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/model-configs",
    tags=["enterprise:model-configs"],
    dependencies=[Depends(get_current_user)],
)


def model_config_read(row: ModelConfig) -> ModelConfigRead:
    api_key = decrypt_secret(row.api_key_encrypted)
    extra_body = row.extra_body_json if isinstance(row.extra_body_json, dict) else {}
    return ModelConfigRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        provider=row.provider,
        base_url=row.base_url,
        api_key_masked=mask_secret(api_key),
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        extra_body=dict(extra_body),
        capability_snapshot=dict(row.capability_snapshot_json or {}),
        capability_checksum=row.capability_checksum,
        preflight_status=row.preflight_status,
        preflight_error=row.preflight_error,
        capability_verified_at=(
            row.capability_verified_at.isoformat() if row.capability_verified_at else None
        ),
        is_default=row.is_default,
        enabled=row.enabled,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ModelConfigRead], dependencies=[Depends(require_current_tenant)])
def list_model_configs(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> list[ModelConfigRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id)).all()
    return [model_config_read(row) for row in rows]


@router.post("", response_model=ModelConfigRead)
def create_model_config(
    request: ModelConfigCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    """创建租户模型，并在首模型或显式默认场景内原子切换默认项。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    _lock_tenant_model_configs(db, request.tenant_id)
    existing_count = len(db.exec(select(ModelConfig).where(ModelConfig.tenant_id == request.tenant_id)).all())
    is_default = request.is_default or existing_count == 0
    if is_default:
        _clear_default(db, request.tenant_id)
    row = ModelConfig(
        tenant_id=request.tenant_id,
        name=request.name,
        provider=request.provider,
        base_url=request.base_url,
        api_key_encrypted=encrypt_secret(request.api_key),
        model=request.model,
        temperature=request.temperature,
        max_output_tokens=request.max_output_tokens,
        extra_body_json=dict(request.extra_body),
        is_default=is_default,
        enabled=request.enabled,
    )
    db.add(row)
    _commit_model_config(db)
    db.refresh(row)
    return model_config_read(row)


@router.put("/{config_id}", response_model=ModelConfigRead)
def update_model_config(
    config_id: str,
    request: ModelConfigUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    """更新租户模型，并在需要时于同一事务中切换唯一默认项。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    _lock_tenant_model_configs(db, request.tenant_id)
    row = _get_model_config(db, request.tenant_id, config_id)
    if request.is_default:
        _clear_default(db, request.tenant_id)
    for field in ("name", "provider", "base_url", "model", "temperature", "max_output_tokens", "enabled"):
        value = getattr(request, field)
        if value is not None:
            setattr(row, field, value)
    if request.api_key is not None:
        row.api_key_encrypted = encrypt_secret(request.api_key)
    if request.extra_body is not None:
        row.extra_body_json = dict(request.extra_body)
    if request.is_default is not None:
        row.is_default = request.is_default
    if request.model_fields_set & {"provider", "base_url", "api_key", "model", "extra_body"}:
        _invalidate_dynamic_preflight(row)
    row.updated_at = utc_now()
    db.add(row)
    _commit_model_config(db)
    db.refresh(row)
    return model_config_read(row)


@router.post(
    "/{config_id}/set-default",
    response_model=ModelConfigRead,
    dependencies=[Depends(require_tenant_admin)],
)
def set_default_model_config(
    config_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> ModelConfigRead:
    """串行化同租户写入，先清除旧默认再设置指定模型。"""

    _lock_tenant_model_configs(db, tenant_id)
    row = _get_model_config(db, tenant_id, config_id)
    _clear_default(db, tenant_id)
    row.is_default = True
    row.updated_at = utc_now()
    db.add(row)
    _commit_model_config(db)
    db.refresh(row)
    return model_config_read(row)


@router.post(
    "/{config_id}/test",
    response_model=ModelConfigTestResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def test_model_config(
    config_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> ModelConfigTestResponse:
    row = _get_model_config(db, tenant_id, config_id)
    try:
        output = LLMClient(row).generate_text(
            "你是一个连接测试助手。请用一句中文回复连接成功。",
            {"message": "ping"},
        )
        return ModelConfigTestResponse(success=True, message="Model connection succeeded", output=output)
    except LLMError as exc:
        return ModelConfigTestResponse(success=False, message=str(exc), output=None)


@router.post(
    "/{config_id}/preflight-dynamic",
    response_model=ModelCapabilityPreflightResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def preflight_dynamic_model_config(
    config_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> ModelCapabilityPreflightResponse:
    """验证原生 structured-output/tool-call 协议并持久化可审计能力快照。"""

    row = _get_model_config(db, tenant_id, config_id)
    if row.provider != "openai_compatible":
        return _record_preflight_failure(
            db,
            row,
            "UNSUPPORTED_PROVIDER_SDK",
        )
    try:
        capabilities = LLMClient(row).preflight_dynamic_capabilities()
    except LLMError as exc:
        return _record_preflight_failure(db, row, _sanitize_preflight_error(row, str(exc)))
    snapshot = {
        **capabilities,
        "provider": row.provider,
        "model": row.model,
    }
    row.capability_snapshot_json = snapshot
    row.capability_checksum = capability_checksum(snapshot)
    row.preflight_status = "ready"
    row.preflight_error = None
    row.capability_verified_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    return ModelCapabilityPreflightResponse(
        success=True,
        status="ready",
        capabilities=snapshot,
        checksum=row.capability_checksum,
        message="Dynamic model capability preflight succeeded",
    )


def _get_model_config(db: Session, tenant_id: str, config_id: str) -> ModelConfig:
    """按租户边界读取模型配置，不接受跨租户标识。"""

    ensure_tenant(db, tenant_id)
    row = db.get(ModelConfig, config_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Model config not found")
    return row


def _clear_default(db: Session, tenant_id: str) -> None:
    """用一条 UPDATE 在设置新默认前持久清除旧默认，避免 ORM flush 顺序冲突。"""

    db.exec(
        update(ModelConfig)
        .where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
        )
        .values(is_default=False, updated_at=utc_now())
    )


def _lock_tenant_model_configs(db: Session, tenant_id: str) -> None:
    """触碰并锁定租户行，使 SQLite/MySQL 的同租户模型写入按事务串行执行。"""

    result = db.exec(
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(updated_at=utc_now())
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=404, detail="Tenant not found")


def _commit_model_config(db: Session) -> None:
    """提交模型配置写入，并把数据库唯一性竞争转换为稳定的 409 契约。"""

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="MODEL_DEFAULT_CONFLICT") from exc


def _invalidate_dynamic_preflight(row: ModelConfig) -> None:
    """连接或协议配置变更时撤销旧能力事实，防止模型切换沿用快照。"""

    row.capability_snapshot_json = {}
    row.capability_checksum = None
    row.preflight_status = "unverified"
    row.preflight_error = None
    row.capability_verified_at = None


def _record_preflight_failure(
    db: Session,
    row: ModelConfig,
    message: str,
) -> ModelCapabilityPreflightResponse:
    """持久化脱敏预检失败事实，且不保留部分成功的能力快照。"""

    row.capability_snapshot_json = {}
    row.capability_checksum = None
    row.preflight_status = "failed"
    row.preflight_error = message[:2000]
    row.capability_verified_at = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    return ModelCapabilityPreflightResponse(
        success=False,
        status="failed",
        capabilities={},
        checksum=None,
        message=message[:2000],
    )


def _sanitize_preflight_error(row: ModelConfig, message: str) -> str:
    """从 provider 错误中移除当前明文 API key，避免落库和返回给前端。"""

    secret = decrypt_secret(row.api_key_encrypted)
    return message.replace(secret, "***") if secret else message
