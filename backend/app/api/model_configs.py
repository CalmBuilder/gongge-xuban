"""
@Time       : 2026/07/27 15:45
@Author     : zhanglp8181
@File       : model_configs.py
@CallChain  : FastAPI 模型配置路由 → 租户级事务锁/SQLModel → LLMClient
@Description: 管理租户模型配置，并以原子切换和数据库约束维护唯一默认模型。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ModelConfig, Tenant, User, utc_now
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.llm import LLMClient, LLMError
from app.llm.schemas import (
    ModelCapabilityPreflightResponse,
    ModelConnectionCheck,
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
    """分阶段验证配置、模型目录、供应商账户状态与最小生成请求。"""

    row = _get_model_config(db, tenant_id, config_id)
    checks = [ModelConnectionCheck(name="配置", status="passed", message="配置与密钥已加载")]
    try:
        client = LLMClient(row)
    except LLMError as exc:
        return _connection_failure(row, exc, checks, stage="配置")
    try:
        model_ids = client.probe_model_catalog()
    except LLMError as exc:
        metadata = _provider_error_metadata(exc)
        if metadata["http_status"] in {404, 405}:
            checks.append(
                ModelConnectionCheck(
                    name="模型目录",
                    status="skipped",
                    message="供应商未实现模型目录接口，继续验证最小生成",
                )
            )
        else:
            return _connection_failure(row, exc, checks, stage="模型目录")
    else:
        if model_ids and row.model not in model_ids:
            checks.append(
                ModelConnectionCheck(
                    name="模型目录",
                    status="failed",
                    message=f"端点可访问，但目录中不存在模型 {row.model}",
                )
            )
            return ModelConfigTestResponse(
                success=False,
                message="模型名称不在供应商目录中",
                error_code="MODEL_NOT_AVAILABLE",
                endpoint=_safe_endpoint(row.base_url),
                model=row.model,
                suggestion="请从供应商模型目录复制准确的模型 ID。",
                checks=checks,
            )
        checks.append(
            ModelConnectionCheck(
                name="模型目录",
                status="passed",
                message=(f"认证成功，已找到模型 {row.model}" if model_ids else "认证成功"),
            )
        )
    balance_failure = _deepseek_balance_failure(row, checks)
    if balance_failure is not None:
        return balance_failure
    try:
        output = client.probe_text_connection()
        checks.append(ModelConnectionCheck(name="最小生成", status="passed", message="模型已返回正文"))
        return ModelConfigTestResponse(
            success=True,
            message="模型连接与最小生成均成功",
            output=output,
            endpoint=_safe_endpoint(row.base_url),
            model=row.model,
            checks=checks,
        )
    except LLMError as exc:
        return _connection_failure(row, exc, checks, stage="最小生成")


def _deepseek_balance_failure(
    row: ModelConfig,
    checks: list[ModelConnectionCheck],
) -> ModelConfigTestResponse | None:
    """对DeepSeek官方端点读取当前API Key账户可用性，其他兼容供应商直接跳过。"""

    endpoint = _safe_endpoint(row.base_url)
    if urlsplit(endpoint).hostname != "api.deepseek.com":
        checks.append(
            ModelConnectionCheck(name="账户状态", status="skipped", message="供应商未提供标准余额接口")
        )
        return None
    secret = decrypt_secret(row.api_key_encrypted)
    try:
        response = httpx.get(
            f"{endpoint.rstrip('/')}/user/balance",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=15,
        )
        body = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError):
        checks.append(
            ModelConnectionCheck(name="账户状态", status="skipped", message="余额接口不可用，继续验证最小生成")
        )
        return None
    if response.status_code != 200 or not isinstance(body, dict):
        checks.append(
            ModelConnectionCheck(name="账户状态", status="skipped", message="余额接口未返回可用状态")
        )
        return None
    if body.get("is_available") is True:
        checks.append(ModelConnectionCheck(name="账户状态", status="passed", message="API账户可用于生成请求"))
        return None
    balance = _deepseek_balance_summary(body)
    checks.append(
        ModelConnectionCheck(
            name="账户状态",
            status="failed",
            message=f"该API Key所属账户当前不可用于生成；{balance}",
        )
    )
    checks.append(
        ModelConnectionCheck(
            name="最小生成",
            status="skipped",
            message="账户不可用，未继续消耗额度",
        )
    )
    return ModelConfigTestResponse(
        success=False,
        message="供应商账户不可用于生成请求",
        error_code="BILLING_UNAVAILABLE",
        http_status=402,
        endpoint=endpoint,
        model=row.model,
        suggestion="请确认充值的是这把 API Key 所属账户或项目，充值后重新测试。",
        checks=checks,
    )


def _deepseek_balance_summary(body: dict[str, Any]) -> str:
    """只投影币种与总余额，不回显任何账户或凭据字段。"""

    values = []
    for item in body.get("balance_infos") or []:
        if isinstance(item, dict):
            values.append(f"{item.get('currency', '')}余额 {item.get('total_balance', '')}")
    return "、".join(values) or "供应商未返回余额明细"


def _connection_failure(
    row: ModelConfig,
    exc: LLMError,
    checks: list[ModelConnectionCheck],
    *,
    stage: str,
) -> ModelConfigTestResponse:
    """把供应商异常归一为稳定错误码和可操作建议，并移除明文密钥。"""

    metadata = _provider_error_metadata(exc)
    error_code, suggestion = _connection_error_classification(metadata)
    message = _sanitize_preflight_error(row, str(exc))
    checks.append(ModelConnectionCheck(name=stage, status="failed", message=message[:500]))
    return ModelConfigTestResponse(
        success=False,
        message=message,
        error_code=error_code,
        http_status=metadata["http_status"],
        provider_code=metadata["provider_code"],
        request_id=metadata["request_id"],
        endpoint=_safe_endpoint(row.base_url),
        model=row.model,
        suggestion=suggestion,
        checks=checks,
    )


def _provider_error_metadata(exc: LLMError) -> dict[str, Any]:
    """优先读取原始provider异常字段，缺失时从既有稳定诊断文本回退解析。"""

    cause = exc.__cause__
    status = getattr(cause, "status_code", None)
    body = getattr(cause, "body", None)
    provider_code = None
    if isinstance(body, dict):
        detail = body.get("error") if isinstance(body.get("error"), dict) else body
        provider_code = detail.get("code") or detail.get("type")
    message = str(exc)
    if status is None:
        for fragment in message.split(";"):
            if fragment.strip().startswith("status_code="):
                try:
                    status = int(fragment.split("=", 1)[1])
                except ValueError:
                    pass
    return {
        "http_status": status if isinstance(status, int) else None,
        "provider_code": str(provider_code)[:64] if provider_code else None,
        "request_id": str(getattr(cause, "request_id", ""))[:128] or None,
        "message": message,
    }


def _connection_error_classification(metadata: dict[str, Any]) -> tuple[str, str]:
    """依据HTTP和provider错误把失败映射为用户可操作的稳定分类。"""

    status = metadata.get("http_status")
    message = str(metadata.get("message") or "").lower()
    if status in {401, 403}:
        return "AUTHENTICATION_FAILED", "请核对 API Key、项目权限和供应商账户。"
    if status == 402 or "insufficient balance" in message:
        return "BILLING_UNAVAILABLE", "请确认充值的是该 API Key 所属账户或项目。"
    if status == 404:
        return "MODEL_OR_ENDPOINT_NOT_FOUND", "请核对 Base URL 路径和模型 ID。"
    if status == 429:
        return "RATE_LIMITED", "供应商正在限流，请稍后重试或检查账户配额。"
    if status and status >= 500:
        return "PROVIDER_UNAVAILABLE", "供应商服务异常，请稍后重试。"
    if "timeout" in message:
        return "CONNECTION_TIMEOUT", "请检查网络、代理、Base URL 和超时设置。"
    return "MODEL_CONNECTION_FAILED", "请核对 Base URL、模型名、API Key 和供应商状态。"


def _safe_endpoint(value: str | None) -> str:
    """只返回不含userinfo、query与fragment的模型端点。"""

    parsed = urlsplit(str(value or ""))
    if not parsed.scheme or not parsed.hostname:
        return str(value or "")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}".rstrip("/")


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
