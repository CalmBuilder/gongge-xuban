"""
@Time       : 2026/08/04 18:40
@Author     : zhanglp8181
@File       : artifacts.py
@CallChain  : Execution card/Chat → Artifact API → ArtifactService
@Description: 以 Artifact id 和显式 ACL 提供元数据、受限预览与完整下载。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ExecutionArtifact, User
from app.dynamic_tasks.artifacts import (
    PREVIEWABLE_MIME_TYPES,
    ArtifactAccessDenied,
    ArtifactService,
)
from app.security.auth import ensure_current_user_tenant, get_current_user


router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])
MAX_TEXT_PREVIEW_BYTES = 500_000


class ArtifactRead(BaseModel):
    """返回不暴露存储路径的 Artifact 权威元数据和 lineage。"""

    id: str
    execution_id: str
    source_step_key: str
    artifact_key: str
    filename: str
    mime_type: str
    size_bytes: int
    content_checksum: str
    status: str
    input_snapshot_ids: list[str]
    created_at: str


class ArtifactPreview(BaseModel):
    """返回有界文本预览，二进制交付物必须走下载接口。"""

    artifact: ArtifactRead
    content: str
    truncated: bool


@router.get("", response_model=list[ArtifactRead])
def list_artifacts(
    execution_id: str = Query(...),
    tenant_id: str = Query(...),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ArtifactRead]:
    """列出当前用户对指定 Execution 可读取的已登记 Artifact。"""

    ensure_current_user_tenant(tenant_id, current_user)
    service = ArtifactService(db)
    rows = db.exec(
        select(ExecutionArtifact).where(
            ExecutionArtifact.tenant_id == tenant_id,
            ExecutionArtifact.execution_id == execution_id,
            ExecutionArtifact.status == "ready",
        ).order_by(ExecutionArtifact.created_at, ExecutionArtifact.id).offset(offset).limit(limit)
    ).all()
    visible: list[ArtifactRead] = []
    for row in rows:
        try:
            service.authorize(row.id, tenant_id=tenant_id, actor_user_id=current_user.id)
        except ArtifactAccessDenied:
            continue
        visible.append(_artifact_read(service, row))
    return visible


@router.get("/{artifact_id}", response_model=ArtifactRead)
def get_artifact(
    artifact_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ArtifactRead:
    """读取单个 Artifact 元数据；越权与不存在统一返回 404。"""

    service, artifact, _ = _resolve(db, artifact_id, tenant_id, current_user)
    return _artifact_read(service, artifact)


@router.get("/{artifact_id}/preview", response_model=ArtifactPreview)
def preview_artifact(
    artifact_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ArtifactPreview:
    """对允许的文本 MIME 返回 UTF-8 有界预览，拒绝二进制和损坏内容。"""

    service, artifact, data = _resolve(db, artifact_id, tenant_id, current_user)
    if artifact.mime_type not in PREVIEWABLE_MIME_TYPES:
        raise HTTPException(status_code=415, detail="ARTIFACT_PREVIEW_UNSUPPORTED")
    preview = data[:MAX_TEXT_PREVIEW_BYTES]
    try:
        content = preview.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="ARTIFACT_TEXT_INVALID") from exc
    if artifact.mime_type == "application/json":
        try:
            json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="ARTIFACT_JSON_INVALID") from exc
    return ArtifactPreview(
        artifact=_artifact_read(service, artifact),
        content=content,
        truncated=len(data) > len(preview),
    )


@router.get("/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> Response:
    """在完整性复核后下载 Artifact，不接受客户端文件路径。"""

    _, artifact, data = _resolve(db, artifact_id, tenant_id, current_user)
    filename = quote(artifact.filename, safe="")
    return Response(
        content=data,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolve(
    db: Session,
    artifact_id: str,
    tenant_id: str,
    current_user: User,
) -> tuple[ArtifactService, ExecutionArtifact, bytes]:
    """统一 tenant 校验和 404 防枚举错误映射。"""

    ensure_current_user_tenant(tenant_id, current_user)
    service = ArtifactService(db)
    try:
        artifact, data = service.resolve(
            artifact_id,
            tenant_id=tenant_id,
            actor_user_id=current_user.id,
        )
    except ArtifactAccessDenied as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service, artifact, data


def _artifact_read(service: ArtifactService, artifact: ExecutionArtifact) -> ArtifactRead:
    """映射安全元数据并从关系表读取实际 lineage，不信任展示 JSON。"""

    return ArtifactRead(
        id=artifact.id,
        execution_id=artifact.execution_id,
        source_step_key=artifact.source_step_key,
        artifact_key=artifact.artifact_key,
        filename=artifact.filename,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        content_checksum=artifact.content_checksum,
        status=artifact.status,
        input_snapshot_ids=[item.input_snapshot_id for item in service.lineage(artifact)],
        created_at=artifact.created_at.isoformat(),
    )
