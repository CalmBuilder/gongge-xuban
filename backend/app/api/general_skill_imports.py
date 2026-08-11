"""
@Time       : 2026/08/12 01:30
@Author     : zhanglp8181
@File       : general_skill_imports.py
@CallChain  : Skill 导入页面 → FastAPI → GeneralSkillImportService → DB/object store
@Description: 暴露受 feature flag 保护的 S1 导入作业查询、取消和 checksum 确认 API。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlmodel import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.db.models import User
from app.general_skills.import_schema import (
    GeneralSkillImportCancel,
    GeneralSkillImportConfirm,
    GeneralSkillImportJobCreate,
    GeneralSkillImportJobRead,
)
from app.general_skills.import_service import (
    GeneralSkillImportError,
    GeneralSkillImportService,
    ImportQuotaPolicy,
)
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetcher, SecureHttpsFetcher
from app.security.auth import get_current_user


router = APIRouter(
    prefix="/api/enterprise/general-skill-import-jobs",
    tags=["enterprise:general-skill-imports"],
    dependencies=[Depends(get_current_user)],
)


def get_general_skill_remote_fetcher() -> RemoteFetcher:
    """创建生产 fail-closed HTTPS fetcher，并作为测试可替换的供应商边界。"""

    return SecureHttpsFetcher()


@router.get("/capabilities")
def get_import_capabilities(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    """公开当前部署是否启用新导入流及已达到生产契约的来源类型。"""

    return {
        "enabled": settings.general_skill_import_v2_enabled,
        "source_kinds": (
            ["upload", "github", "skillhub"]
            + (["https"] if settings.general_skill_https_allowed_host_set else [])
        )
        if settings.general_skill_import_v2_enabled
        else [],
    }


@router.post("", response_model=GeneralSkillImportJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_import_job(
    request: GeneralSkillImportJobCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    remote_fetcher: RemoteFetcher = Depends(get_general_skill_remote_fetcher),
) -> GeneralSkillImportJobRead:
    """创建上传导入作业并返回可刷新恢复的安全预览或结构化失败终态。"""

    service = _service(db, settings)
    try:
        return service.create_job(
            request,
            idempotency_key=idempotency_key,
            current_user=current_user,
            fetcher=remote_fetcher,
            defer_processing=settings.general_skill_import_async_enabled,
        )
    except GeneralSkillImportError as exc:
        raise _http_error(exc) from exc


@router.get("/{job_id}", response_model=GeneralSkillImportJobRead)
def get_import_job(
    job_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> GeneralSkillImportJobRead:
    """读取当前用户拥有的导入作业，不允许管理员跨租户猜测 ID。"""

    service = _service(db, settings)
    try:
        return service.get_job(job_id, current_user=current_user)
    except GeneralSkillImportError as exc:
        raise _http_error(exc) from exc


@router.post("/{job_id}/cancel", response_model=GeneralSkillImportJobRead)
def cancel_import_job(
    job_id: str,
    request: GeneralSkillImportCancel,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> GeneralSkillImportJobRead:
    """取消本人未终态作业并回收暂存空间。"""

    service = _service(db, settings)
    try:
        return service.cancel_job(
            job_id,
            expected_row_version=request.expected_row_version,
            current_user=current_user,
        )
    except GeneralSkillImportError as exc:
        raise _http_error(exc) from exc


@router.post("/{job_id}/confirm", response_model=GeneralSkillImportJobRead)
def confirm_import_job(
    job_id: str,
    request: GeneralSkillImportConfirm,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> GeneralSkillImportJobRead:
    """按 preview checksum 与行版本确认候选，并原子建立 pinned 绑定。"""

    service = _service(db, settings)
    try:
        return service.confirm_job(job_id, request, current_user=current_user)
    except GeneralSkillImportError as exc:
        raise _http_error(exc) from exc


def _service(db: Session, settings: Settings) -> GeneralSkillImportService:
    """在开关关闭时隐藏新入口，开启后创建请求级领域服务。"""

    if not settings.general_skill_import_v2_enabled:
        raise HTTPException(status_code=404, detail={"error_code": "FEATURE_NOT_AVAILABLE"})
    return GeneralSkillImportService(
        db,
        FileSystemSkillObjectStore(settings.general_skill_object_store_path),
        https_allowed_hosts=settings.general_skill_https_allowed_host_set,
        quota_policy=ImportQuotaPolicy(
            tenant_active_jobs=settings.general_skill_import_tenant_active_limit,
            user_active_jobs=settings.general_skill_import_user_active_limit,
            tenant_staged_bytes=settings.general_skill_import_tenant_staged_bytes,
            user_staged_bytes=settings.general_skill_import_user_staged_bytes,
        ),
    )


def _http_error(exc: GeneralSkillImportError) -> HTTPException:
    """把领域错误映射为不包含内部栈和原始正文的 HTTP detail。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"error_code": exc.error_code, "message": str(exc)},
    )
