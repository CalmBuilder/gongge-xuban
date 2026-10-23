"""
@Time       : 2026/09/28 19:30
@Author     : zhanglp8181
@File       : external_tasks.py
@CallChain  : provider callback → FastAPI → trusted inbox/Signal → SOP/DTA worker
@Description: 接收显式 provider_task 回执并保持租户、Operation 与回调凭据绑定。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.db import get_session
from app.db.models import SopOperation
from app.sop_runtime.external_tasks import (
    ExternalTaskEventConflict,
    ExternalTaskEventRejected,
    apply_pending_external_task_events,
    ingest_external_task_event,
)


router = APIRouter(prefix="/api/external-tasks", tags=["external-tasks"])


class ExternalTaskCallbackRequest(BaseModel):
    """provider 回执输入；业务归属由 URL Operation 和服务端账本决定。"""

    provider_task_id: str = Field(min_length=1, max_length=512)
    provider_event_id: str = Field(min_length=1, max_length=512)
    provider_status: str = Field(min_length=1, max_length=128)
    observation_revision: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    external_submission_id: str | None = Field(default=None, max_length=512)
    event_type: str = Field(default="status", min_length=1, max_length=64)


@router.post("/{operation_id}/callback", status_code=202)
def receive_external_task_callback(
    operation_id: str,
    request: ExternalTaskCallbackRequest,
    callback_token: str | None = Header(default=None, alias="X-Gongge-Xuban-External-Task-Token"),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    """验证回调凭据并入箱；可立即应用已进入等待的 Operation。"""

    operation = db.get(SopOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="EXTERNAL_OPERATION_NOT_FOUND")
    try:
        receipt = ingest_external_task_event(
            db,
            tenant_id=operation.tenant_id,
            operation_id=operation.id,
            provider_task_id=request.provider_task_id,
            provider_event_id=request.provider_event_id,
            provider_status=request.provider_status,
            observation_revision=request.observation_revision,
            payload=request.payload,
            callback_token=callback_token or "",
            external_submission_id=request.external_submission_id,
            event_type=request.event_type,
        )
        applied = apply_pending_external_task_events(db, limit=1)
        db.commit()
    except ExternalTaskEventConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except ExternalTaskEventRejected as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=exc.code) from exc
    return {"event_id": receipt.id, "status": receipt.status, "applied": applied}
