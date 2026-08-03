"""
@Time       : 2026/07/22 05:09
@Author     : zhanglp8181
@File       : contracts.py
@CallChain  : SOP 定义/命令处理器 → Runtime 契约 → 持久化/领域事件
@Description: 定义统一 SOP Runtime 的状态、命令、事件和版本化策略契约。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """生成带 UTC 时区的契约时间，避免跨系统传递无时区时间。"""

    return datetime.now(UTC)


class SopInstanceStatus(StrEnum):
    """SOP 实例状态。"""

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class NodeExecutionStatus(StrEnum):
    """节点单次执行状态；重试通过新的 attempt 表达。"""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class WorkItemStatus(StrEnum):
    """结构化人工工作项的生命周期状态。"""

    OFFERED = "offered"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OperationStatus(StrEnum):
    """外部工具或副作用操作的执行状态。"""

    PREPARED = "prepared"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeliveryStatus(StrEnum):
    """领域事件投递状态。"""

    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


class CompletionMode(StrEnum):
    """人工任务参与者的完成规则。"""

    SINGLE = "single"
    ANY = "any"
    ALL = "all"
    QUORUM = "quorum"


class BackoffStrategy(StrEnum):
    """失败重试的退避策略。"""

    FIXED = "fixed"
    EXPONENTIAL = "exponential"


class TimeoutAction(StrEnum):
    """达到超时时间后的确定性处置。"""

    FAIL = "fail"
    RETRY = "retry"
    RECONCILE = "reconcile"
    ESCALATE = "escalate"


class IdempotencyScope(StrEnum):
    """幂等键生效范围。"""

    INSTANCE = "instance"
    BUSINESS = "business"
    TENANT = "tenant"


class RuntimeContract(BaseModel):
    """所有持久化或跨进程契约的严格基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RetryPolicy(RuntimeContract):
    """节点或投递的版本化重试策略。"""

    max_attempts: int = Field(default=1, ge=1, le=100)
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_seconds: float = Field(default=1.0, ge=0, le=86400)
    multiplier: float = Field(default=2.0, ge=1, le=100)
    max_delay_seconds: float = Field(default=300.0, ge=0, le=604800)
    retryable_error_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_delay_range(self) -> "RetryPolicy":
        """确保最大退避时间不会小于初始退避时间。"""

        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be greater than or equal to initial_delay_seconds")
        return self

    def allows_attempt(self, attempt: int) -> bool:
        """判断从 1 开始计数的执行序号是否仍在总执行次数预算内。"""

        if attempt < 1:
            raise ValueError("attempt must be greater than or equal to one")
        return attempt <= self.max_attempts


class TimeoutPolicy(RuntimeContract):
    """节点或工作项的版本化超时策略。"""

    timeout_seconds: int = Field(gt=0, le=31536000)
    action: TimeoutAction = TimeoutAction.FAIL


class IdempotencyPolicy(RuntimeContract):
    """副作用操作的业务幂等策略。"""

    required: bool = True
    scope: IdempotencyScope = IdempotencyScope.INSTANCE
    key_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_required_key_fields(self) -> "IdempotencyPolicy":
        """业务级幂等必须声明参与生成键的稳定业务字段。"""

        if self.required and self.scope is IdempotencyScope.BUSINESS and not self.key_fields:
            raise ValueError("business idempotency requires at least one key field")
        return self


class WorkItemCompletionPolicy(RuntimeContract):
    """人工任务的认领与多人完成规则。"""

    mode: CompletionMode = CompletionMode.SINGLE
    claim_required: bool = False
    required_count: int | None = Field(default=None, ge=1)
    distinct_actors: bool = True

    @model_validator(mode="after")
    def validate_required_count(self) -> "WorkItemCompletionPolicy":
        """只有 quorum 使用显式门槛，其他模式由参与者集合决定。"""

        if self.mode is CompletionMode.QUORUM and self.required_count is None:
            raise ValueError("quorum completion requires required_count")
        if self.mode is not CompletionMode.QUORUM and self.required_count is not None:
            raise ValueError("required_count is only valid for quorum completion")
        return self


class CommandEnvelope(RuntimeContract):
    """进入 Runtime 的结构化命令信封。"""

    schema_version: int = Field(default=1, ge=1)
    command_id: str = Field(min_length=1, max_length=128)
    command_type: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=512)
    actor_user_id: str | None = Field(default=None, max_length=512)
    expected_revision: int | None = Field(default=None, ge=0)
    correlation_id: str | None = Field(default=None, max_length=512)
    causation_id: str | None = Field(default=None, max_length=512)
    issued_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class DomainEventEnvelope(RuntimeContract):
    """可持久化、可演进和可去重消费的领域事件信封。"""

    event_id: str = Field(min_length=1, max_length=512)
    event_type: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(default=1, ge=1)
    tenant_id: str = Field(min_length=1, max_length=128)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=512)
    aggregate_revision: int = Field(ge=1)
    correlation_id: str | None = Field(default=None, max_length=512)
    causation_id: str | None = Field(default=None, max_length=512)
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
