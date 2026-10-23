"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : stream_jobs.py
@CallChain  : 原Skill流式任务 → 内存事件缓存 → 当前候选fence校验/续接
@Description: 保存原任务与事件；反馈缓存只携带冻结身份，失效清除正文且序号不倒退。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any

from app.db.models import new_id, utc_now

if TYPE_CHECKING:
    from app.skills.candidate_rewrite import CandidateRewriteFence


@dataclass
class SkillStreamEvent:
    """原SSE序号和数据；序号在失效清空后也不复用。"""
    seq: int
    event: str
    data: dict[str, Any]


@dataclass
class SkillStreamJob:
    """原进程内任务；反馈只附已有候选身份，不建立第二持久任务系统。"""
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
    feedback_fence: CandidateRewriteFence | None = None


class SkillStreamJobStore:
    """以短内存锁保护任务事件；数据库授权由调用方fresh检查。"""
    def __init__(self, max_jobs: int = 200):
        """保留原终态任务容量上限，不清除尚运行的任务。"""
        self._lock = Lock()
        self._jobs: dict[str, SkillStreamJob] = {}
        self._max_jobs = max_jobs

    def create(self, name: str, tenant_id: str, user_id: str) -> SkillStreamJob:
        """创建绑定租户/实际操作者的原任务。"""
        job = SkillStreamJob(id=new_id("skilljob"), name=name, tenant_id=tenant_id, user_id=user_id)
        with self._lock:
            self._jobs[job.id] = job
            self._trim_locked()
        return job

    def start(self, job_id: str) -> None:
        """标记后台任务已经开始。"""
        self._update(job_id, status="running")

    def append(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        """追加已由生产者校验的事件，失效终态不接受迟到模型正文。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.feedback_fence is not None and job.status == 'failed':
                return
            job.events.append(SkillStreamEvent(seq=len(job.events) + 1, event=event, data=data))
            job.updated_at = utc_now().isoformat()

    def complete(self, job_id: str) -> None:
        """标记生成完成；反馈失效终态不可被迟到线程改回成功。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or (job.feedback_fence is not None and job.status == 'failed'):
                return
            job.status = 'succeeded'
            job.updated_at = utc_now().isoformat()

    def fail(self, job_id: str, error: str) -> None:
        """普通任务保留原错误合同；反馈调用方只传安全稳定错误并清缓存。"""
        self.append(job_id, "error", {"message": error})
        self._update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> None:
        """记录原取消请求，由发送/结算守卫阻止晚到结果。"""
        self._update(job_id, cancel_requested=True)

    def is_cancelled(self, job_id: str) -> bool:
        """读取后台取消标记；缺失任务视为不可继续的反馈任务。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    def get(self, job_id: str) -> SkillStreamJob | None:
        """复制身份及事件列表，数据库fence必须由API独立校验。"""
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
                feedback_fence=job.feedback_fence,
            )

    def snapshot(self, job_id: str, after: int = 0) -> tuple[SkillStreamJob | None, list[SkillStreamEvent]]:
        """取得原序号后的事件与冻结身份；不隐式信任曾成功的候选。"""
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
                feedback_fence=job.feedback_fence,
            )
            return copy, events

    def bind_feedback(self, job_id: str, fence: CandidateRewriteFence) -> None:
        """准备及结算后更新同一任务fence；新修订用于后续结果缓存校验。"""
        self._update(job_id, feedback_fence=fence)

    def invalidate_feedback(self, job_id: str, error: str) -> None:
        """撤销清除全部旧正文，保留递增序号的单一安全错误供已连接流结束。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            sequence = max((event.seq for event in job.events), default=0) + 1
            job.events = [SkillStreamEvent(sequence, 'error', {'message': error})]
            job.error = error
            job.status = 'failed'
            job.updated_at = utc_now().isoformat()

    def _update(self, job_id: str, **changes: Any) -> None:
        """短锁内更新原任务元数据，不在锁内外呼或访问数据库。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now().isoformat()

    def _trim_locked(self) -> None:
        """按原更新时间只淘汰超量终态任务，重启丢失任务不能恢复正文。"""
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
