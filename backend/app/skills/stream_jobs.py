from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from app.db.models import new_id, utc_now


@dataclass
class SkillStreamEvent:
    seq: int
    event: str
    data: dict[str, Any]


@dataclass
class SkillStreamJob:
    id: str
    name: str
    tenant_id: str
    user_id: str
    status: str = "queued"
    events: list[SkillStreamEvent] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())
    cancel_requested: bool = False


class SkillStreamJobStore:
    def __init__(self, max_jobs: int = 200):
        self._lock = Lock()
        self._jobs: dict[str, SkillStreamJob] = {}
        self._max_jobs = max_jobs

    def create(self, name: str, tenant_id: str, user_id: str) -> SkillStreamJob:
        job = SkillStreamJob(id=new_id("skilljob"), name=name, tenant_id=tenant_id, user_id=user_id)
        with self._lock:
            self._jobs[job.id] = job
            self._trim_locked()
        return job

    def start(self, job_id: str) -> None:
        self._update(job_id, status="running")

    def append(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.events.append(SkillStreamEvent(seq=len(job.events) + 1, event=event, data=data))
            job.updated_at = utc_now().isoformat()

    def complete(self, job_id: str) -> None:
        self._update(job_id, status="succeeded")

    def fail(self, job_id: str, error: str) -> None:
        self.append(job_id, "error", {"message": error})
        self._update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> None:
        self._update(job_id, cancel_requested=True)

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    def get(self, job_id: str) -> SkillStreamJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return SkillStreamJob(
                id=job.id,
                name=job.name,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                status=job.status,
                events=list(job.events),
                error=job.error,
                created_at=job.created_at,
                updated_at=job.updated_at,
                cancel_requested=job.cancel_requested,
            )

    def snapshot(self, job_id: str, after: int = 0) -> tuple[SkillStreamJob | None, list[SkillStreamEvent]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None, []
            events = [event for event in job.events if event.seq > after]
            copy = SkillStreamJob(
                id=job.id,
                name=job.name,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                status=job.status,
                events=[],
                error=job.error,
                created_at=job.created_at,
                updated_at=job.updated_at,
                cancel_requested=job.cancel_requested,
            )
            return copy, events

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now().isoformat()

    def _trim_locked(self) -> None:
        overflow = len(self._jobs) - self._max_jobs
        if overflow <= 0:
            return
        removable = sorted(
            (job for job in self._jobs.values() if job.status in {"succeeded", "failed"}),
            key=lambda item: item.updated_at,
        )
        for job in removable[:overflow]:
            self._jobs.pop(job.id, None)


stream_jobs = SkillStreamJobStore()
