"""
@Time       : 2026/07/22 04:56
@Author     : zhanglp8181
@File       : state_machine.py
@CallChain  : Runtime 命令处理器 → transition_* → 聚合状态/revision 持久化
@Description: 以纯函数校验 SOP 实例、节点、工作项、操作和事件投递的状态迁移。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from app.sop_runtime.contracts import (
    DeliveryStatus,
    NodeExecutionStatus,
    OperationStatus,
    SopInstanceStatus,
    WorkItemStatus,
)


StatusT = TypeVar("StatusT", bound=StrEnum)


INSTANCE_TRANSITIONS: dict[SopInstanceStatus, frozenset[SopInstanceStatus]] = {
    SopInstanceStatus.CREATED: frozenset(
        {SopInstanceStatus.RUNNING, SopInstanceStatus.CANCELLED}
    ),
    SopInstanceStatus.RUNNING: frozenset(
        {
            SopInstanceStatus.WAITING,
            SopInstanceStatus.SUCCEEDED,
            SopInstanceStatus.FAILED,
            SopInstanceStatus.CANCELLED,
            SopInstanceStatus.TIMED_OUT,
        }
    ),
    SopInstanceStatus.WAITING: frozenset(
        {
            SopInstanceStatus.RUNNING,
            SopInstanceStatus.SUCCEEDED,
            SopInstanceStatus.FAILED,
            SopInstanceStatus.CANCELLED,
            SopInstanceStatus.TIMED_OUT,
        }
    ),
    SopInstanceStatus.SUCCEEDED: frozenset(),
    SopInstanceStatus.FAILED: frozenset(),
    SopInstanceStatus.CANCELLED: frozenset(),
    SopInstanceStatus.TIMED_OUT: frozenset(),
}

NODE_TRANSITIONS: dict[NodeExecutionStatus, frozenset[NodeExecutionStatus]] = {
    NodeExecutionStatus.SCHEDULED: frozenset(
        {
            NodeExecutionStatus.RUNNING,
            NodeExecutionStatus.SKIPPED,
            NodeExecutionStatus.CANCELLED,
        }
    ),
    NodeExecutionStatus.RUNNING: frozenset(
        {
            NodeExecutionStatus.WAITING,
            NodeExecutionStatus.SUCCEEDED,
            NodeExecutionStatus.FAILED,
            NodeExecutionStatus.CANCELLED,
            NodeExecutionStatus.TIMED_OUT,
        }
    ),
    NodeExecutionStatus.WAITING: frozenset(
        {
            NodeExecutionStatus.RUNNING,
            NodeExecutionStatus.SUCCEEDED,
            NodeExecutionStatus.FAILED,
            NodeExecutionStatus.CANCELLED,
            NodeExecutionStatus.TIMED_OUT,
        }
    ),
    NodeExecutionStatus.SUCCEEDED: frozenset(),
    NodeExecutionStatus.FAILED: frozenset(),
    NodeExecutionStatus.SKIPPED: frozenset(),
    NodeExecutionStatus.CANCELLED: frozenset(),
    NodeExecutionStatus.TIMED_OUT: frozenset(),
}

WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.OFFERED: frozenset(
        {
            WorkItemStatus.CLAIMED,
            WorkItemStatus.COMPLETED,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.EXPIRED,
        }
    ),
    WorkItemStatus.CLAIMED: frozenset(
        {
            WorkItemStatus.OFFERED,
            WorkItemStatus.COMPLETED,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.EXPIRED,
        }
    ),
    WorkItemStatus.COMPLETED: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
    WorkItemStatus.EXPIRED: frozenset(),
}

OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PREPARED: frozenset(
        {OperationStatus.RUNNING, OperationStatus.FAILED, OperationStatus.CANCELLED}
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.UNKNOWN,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.UNKNOWN: frozenset({OperationStatus.SUCCEEDED, OperationStatus.FAILED}),
    OperationStatus.SUCCEEDED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
}

DELIVERY_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset(
        {DeliveryStatus.DELIVERING, DeliveryStatus.DEAD_LETTER}
    ),
    DeliveryStatus.DELIVERING: frozenset(
        {DeliveryStatus.DELIVERED, DeliveryStatus.PENDING, DeliveryStatus.DEAD_LETTER}
    ),
    DeliveryStatus.DEAD_LETTER: frozenset({DeliveryStatus.PENDING}),
    DeliveryStatus.DELIVERED: frozenset(),
}


class StateTransitionError(ValueError):
    """状态迁移不在确定性转换表中。"""

    code = "INVALID_STATE_TRANSITION"

    def __init__(self, current: StrEnum, target: StrEnum):
        """记录非法迁移的当前状态和目标状态，生成稳定错误信息。"""

        self.current = current
        self.target = target
        super().__init__(f"invalid state transition: {current.value} -> {target.value}")


class RevisionConflictError(ValueError):
    """命令期望的 revision 与当前聚合 revision 不一致。"""

    code = "REVISION_CONFLICT"

    def __init__(self, expected_revision: int, actual_revision: int):
        """记录命令期望版本和聚合实际版本，供调用方识别并发冲突。"""

        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"revision conflict: expected {expected_revision}, actual {actual_revision}"
        )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """状态转换后的不可变结果；持久化由后续应用服务负责。"""

    previous_status: StrEnum
    status: StrEnum
    previous_revision: int
    revision: int


def _transition(
    current: StatusT,
    target: StatusT,
    transitions: dict[StatusT, frozenset[StatusT]],
    *,
    actual_revision: int,
    expected_revision: int | None,
) -> TransitionResult:
    """统一校验聚合版本和转换表，并返回递增一次 revision 的不可变结果。"""

    if actual_revision < 0:
        raise ValueError("actual_revision must be greater than or equal to zero")
    if expected_revision is not None and expected_revision != actual_revision:
        raise RevisionConflictError(expected_revision, actual_revision)
    if target not in transitions[current]:
        raise StateTransitionError(current, target)
    return TransitionResult(
        previous_status=current,
        status=target,
        previous_revision=actual_revision,
        revision=actual_revision + 1,
    )


def transition_instance(
    current: SopInstanceStatus,
    target: SopInstanceStatus,
    *,
    actual_revision: int,
    expected_revision: int | None = None,
) -> TransitionResult:
    """校验并计算 SOP 实例状态转换。"""

    return _transition(
        current,
        target,
        INSTANCE_TRANSITIONS,
        actual_revision=actual_revision,
        expected_revision=expected_revision,
    )


def transition_node(
    current: NodeExecutionStatus,
    target: NodeExecutionStatus,
    *,
    actual_revision: int,
    expected_revision: int | None = None,
) -> TransitionResult:
    """校验并计算节点 attempt 状态转换。"""

    return _transition(
        current,
        target,
        NODE_TRANSITIONS,
        actual_revision=actual_revision,
        expected_revision=expected_revision,
    )


def transition_work_item(
    current: WorkItemStatus,
    target: WorkItemStatus,
    *,
    actual_revision: int,
    expected_revision: int | None = None,
) -> TransitionResult:
    """校验并计算人工工作项生命周期转换。"""

    return _transition(
        current,
        target,
        WORK_ITEM_TRANSITIONS,
        actual_revision=actual_revision,
        expected_revision=expected_revision,
    )


def transition_operation(
    current: OperationStatus,
    target: OperationStatus,
    *,
    actual_revision: int,
    expected_revision: int | None = None,
) -> TransitionResult:
    """校验并计算副作用操作状态转换。"""

    return _transition(
        current,
        target,
        OPERATION_TRANSITIONS,
        actual_revision=actual_revision,
        expected_revision=expected_revision,
    )


def transition_delivery(
    current: DeliveryStatus,
    target: DeliveryStatus,
    *,
    actual_revision: int,
    expected_revision: int | None = None,
) -> TransitionResult:
    """校验并计算领域事件投递状态转换。"""

    return _transition(
        current,
        target,
        DELIVERY_TRANSITIONS,
        actual_revision=actual_revision,
        expected_revision=expected_revision,
    )
