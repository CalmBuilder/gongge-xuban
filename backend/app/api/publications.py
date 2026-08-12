"""
@Time       : 2026/08/13 21:10
@Author     : zhanglp8181
@File       : publications.py
@CallChain  : 资源页/组织广场/待我处理 → FastAPI → PublicationService
@Description: 暴露 Skill/Agent 提交发布、管理员审核、Release 查询和用户主动采用接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.db.models import User
from app.general_skills.publication import PublicationError, PublicationService
from app.general_skills.publication_schema import (
    PublicationAdoptRead,
    PublicationAdoptRequest,
    PublicationReleaseRead,
    PublicationRequestRead,
    PublicationReviewRequest,
    PublicationReleaseTransitionRequest,
    PublicationSubmitRequest,
)
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/enterprise/publications", tags=["enterprise:publications"])


@router.post("", response_model=PublicationRequestRead)
def submit_publication(
    request: PublicationSubmitRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PublicationRequestRead:
    """提交本人 Skill 或 Agent 当前快照进入组织审核。"""

    try:
        return PublicationService(db).submit(
            request.resource_type,
            request.resource_id,
            request.expected_resource_revision,
            current_user,
        )
    except PublicationError as exc:
        raise _http_error(exc) from exc


@router.post("/{request_id}/review", response_model=PublicationRequestRead)
def review_publication(
    request_id: str,
    request: PublicationReviewRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PublicationRequestRead:
    """由职责分离管理员批准或拒绝冻结申请。"""

    try:
        return PublicationService(db).review(
            request_id,
            command=request.command,
            command_id=request.command_id,
            expected_request_row_version=request.expected_request_row_version,
            expected_attention_revision=request.expected_attention_revision,
            reviewer=current_user,
            comment=request.comment,
        )
    except PublicationError as exc:
        raise _http_error(exc) from exc


@router.get("/releases", response_model=list[PublicationReleaseRead])
def list_publication_releases(
    resource_type: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[PublicationReleaseRead]:
    """列出租户内 active Skill/Agent Release。"""

    try:
        return PublicationService(db).list_releases(current_user.tenant_id, resource_type)
    except PublicationError as exc:
        raise _http_error(exc) from exc


@router.post("/releases/{release_id}/adopt", response_model=PublicationAdoptRead)
def adopt_publication_release(
    release_id: str,
    request: PublicationAdoptRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PublicationAdoptRead:
    """用户主动采用 Skill Release 或克隆 Agent Release。"""

    try:
        return PublicationService(db).adopt(
            release_id,
            request.target_agent_id,
            request.idempotency_key,
            current_user,
        )
    except PublicationError as exc:
        raise _http_error(exc) from exc


@router.post("/releases/{release_id}/transition", response_model=PublicationReleaseRead)
def transition_publication_release(
    release_id: str,
    request: PublicationReleaseTransitionRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PublicationReleaseRead:
    """由管理员普通下架或安全撤销组织发布物。"""

    try:
        return PublicationService(db).transition_release(
            release_id,
            command=request.command,
            command_id=request.command_id,
            expected_row_version=request.expected_row_version,
            actor=current_user,
            reason=request.reason,
        )
    except PublicationError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: PublicationError) -> HTTPException:
    """把稳定发布错误映射为不泄漏资源存在性的响应。"""

    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})
