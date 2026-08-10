"""
@Time       : 2026/08/01 21:30
@Author     : zhanglp8181
@File       : schema.py
@CallChain  : 定时任务 API/聊天确认 → Pydantic schema → ScheduledTask service
@Description: 定义定时任务、草稿、运行记录及服务端分页的请求响应契约。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ScheduleType = Literal["once", "daily", "weekly", "monthly"]
ScheduledTaskStatus = Literal["active", "paused", "completed", "archived"]
ConcurrencyPolicy = Literal["forbid", "allow"]
MisfirePolicy = Literal["coalesce", "skip"]


class ScheduledTaskBase(BaseModel):
    tenant_id: str
    agent_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    status: ScheduledTaskStatus = "active"
    concurrency_policy: ConcurrencyPolicy = "forbid"
    misfire_policy: MisfirePolicy = "coalesce"
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduledTaskCreateRequest(ScheduledTaskBase):
    pass


class ScheduledTaskUpdateRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    schedule_type: Optional[ScheduleType] = None
    schedule: Optional[dict[str, Any]] = None
    timezone: Optional[str] = None
    rrule: Optional[str] = None
    status: Optional[ScheduledTaskStatus] = None
    concurrency_policy: Optional[ConcurrencyPolicy] = None
    misfire_policy: Optional[MisfirePolicy] = None
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ScheduledTaskDraftRequest(BaseModel):
    tenant_id: str
    agent_id: str
    session_id: Optional[str] = None
    message: str
    timezone: Optional[str] = None


class ScheduledTaskDraftRead(BaseModel):
    should_create: bool
    tenant_id: str
    agent_id: str
    title: str = ""
    prompt: str = ""
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None
    source_session_id: Optional[str] = None


class ScheduledTaskRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    created_by_user_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: str
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str
    rrule: Optional[str] = None
    status: str
    concurrency_policy: str
    misfire_policy: str
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    run_count: int
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ScheduledTaskPageRead(BaseModel):
    """定时任务定义分页响应，状态计数限定在当前访问范围且排除归档任务。"""

    items: list[ScheduledTaskRead]
    total: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    page: int
    page_size: int


class ScheduledTaskOverviewRead(BaseModel):
    """员工档案使用的启用任务轻量概览，仅携带总数和少量最近任务。"""

    active_count: int
    active_items: list[ScheduledTaskRead] = Field(default_factory=list)


class ScheduledTaskRunRead(BaseModel):
    id: str
    tenant_id: str
    scheduled_task_id: str
    task_title: Optional[str] = None
    task_status: Optional[str] = None
    agent_id: str
    user_id: str
    session_id: Optional[str] = None
    execution_id: Optional[str] = None
    source_kind: str
    source_ref: str
    source_checksum: str
    scheduled_for: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result_summary: Optional[str] = None
    error: Optional[str] = None
    trace: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ScheduledTaskRunPageRead(BaseModel):
    """定时任务运行记录分页响应，run_total 表示当前任务范围内未按状态过滤的总量。"""

    items: list[ScheduledTaskRunRead]
    total: int
    run_total: int
    page: int
    page_size: int
