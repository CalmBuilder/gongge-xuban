from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.async_jobs import AsyncJob, enqueue_async_job
from app.db import engine
from app.feedback.service import FeedbackAnalysisService
from app.observability import EventLog


def enqueue_feedback_analysis(tenant_id: str, feedback_id: str, session_id: str | None = None) -> AsyncJob:
    return enqueue_async_job(
        "feedback.analyze",
        run_feedback_analysis_job,
        {"tenant_id": tenant_id, "feedback_id": feedback_id, "session_id": session_id},
        metadata={"tenant_id": tenant_id, "feedback_id": feedback_id, "session_id": session_id},
    )


def run_feedback_analysis_job(payload: dict[str, Any]) -> None:
    tenant_id = str(payload.get("tenant_id") or "")
    feedback_id = str(payload.get("feedback_id") or "")
    session_id = str(payload.get("session_id") or "")
    with Session(engine) as db:
        events = EventLog(db)
        row = FeedbackAnalysisService(db).analyze_feedback(feedback_id)
        if row:
            events.record(
                row.tenant_id,
                row.session_id,
                "feedback_analysis_completed",
                {
                    "feedback_id": row.id,
                    "message_id": row.message_id,
                    "rating": row.rating,
                    "bucket": row.analysis_bucket,
                    "status": row.analysis_status,
                    "confidence": row.analysis_confidence,
                },
            )
            db.commit()
            return
        if tenant_id and session_id:
            events.record(
                tenant_id,
                session_id,
                "feedback_analysis_error",
                {"feedback_id": feedback_id, "message": "Feedback row not found"},
            )
            db.commit()
