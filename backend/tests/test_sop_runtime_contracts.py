"""
@Time       : 2026/07/22 05:09
@Author     : zhanglp8181
@File       : test_sop_runtime_contracts.py
@CallChain  : pytest → SOP Runtime 契约/状态机 → 确定性行为断言
@Description: 验证统一 SOP Runtime 契约、乐观并发和终态保护。
"""

from datetime import UTC

import pytest
from pydantic import ValidationError

from app.sop_runtime import (
    CommandEnvelope,
    CompletionMode,
    DeliveryStatus,
    DomainEventEnvelope,
    IdempotencyPolicy,
    IdempotencyScope,
    NodeExecutionStatus,
    OperationStatus,
    RetryPolicy,
    RevisionConflictError,
    SopInstanceStatus,
    StateTransitionError,
    WorkItemCompletionPolicy,
    WorkItemStatus,
    transition_delivery,
    transition_instance,
    transition_node,
    transition_operation,
    transition_work_item,
)


def test_command_and_event_envelopes_are_strict_and_versioned() -> None:
    """验证命令和事件信封拒绝额外字段，并携带 UTC 时间及契约版本。"""

    command = CommandEnvelope(
        command_id="cmd_1",
        command_type="start_instance",
        tenant_id="tenant_demo",
        aggregate_type="sop_instance",
        aggregate_id="instance_1",
        expected_revision=0,
    )
    event = DomainEventEnvelope(
        event_id="event_1",
        event_type="sop.instance.started",
        tenant_id="tenant_demo",
        aggregate_type="sop_instance",
        aggregate_id="instance_1",
        aggregate_revision=1,
        causation_id=command.command_id,
    )

    assert command.issued_at.tzinfo is UTC
    assert command.schema_version == 1
    assert event.occurred_at.tzinfo is UTC
    assert event.schema_version == 1

    with pytest.raises(ValidationError):
        CommandEnvelope(
            command_id="cmd_2",
            command_type="start_instance",
            tenant_id="tenant_demo",
            aggregate_type="sop_instance",
            aggregate_id="instance_2",
            unknown_field=True,
        )
    with pytest.raises(ValidationError):
        CommandEnvelope(
            schema_version=0,
            command_id="cmd_3",
            command_type="start_instance",
            tenant_id="tenant_demo",
            aggregate_type="sop_instance",
            aggregate_id="instance_3",
        )


def test_retry_policy_rejects_inverted_delay_range() -> None:
    """验证重试策略拒绝最大延迟小于初始延迟的无效配置。"""

    with pytest.raises(ValidationError, match="max_delay_seconds"):
        RetryPolicy(initial_delay_seconds=10, max_delay_seconds=5)


def test_retry_policy_counts_max_attempts_as_total_executions() -> None:
    """验证 max_attempts 包含首次执行，并按从 1 开始的总执行次数计数。"""

    no_retry = RetryPolicy(max_attempts=1)
    three_attempts = RetryPolicy(max_attempts=3)

    assert no_retry.allows_attempt(1) is True
    assert no_retry.allows_attempt(2) is False
    assert three_attempts.allows_attempt(3) is True
    assert three_attempts.allows_attempt(4) is False
    with pytest.raises(ValueError, match="greater than or equal to one"):
        three_attempts.allows_attempt(0)


def test_business_idempotency_requires_stable_key_fields() -> None:
    """验证业务级幂等策略必须声明用于构造稳定键的业务字段。"""

    with pytest.raises(ValidationError, match="business idempotency"):
        IdempotencyPolicy(scope=IdempotencyScope.BUSINESS)

    policy = IdempotencyPolicy(
        scope=IdempotencyScope.BUSINESS,
        key_fields=("employee_id", "request_date"),
    )
    assert policy.key_fields == ("employee_id", "request_date")


@pytest.mark.parametrize(
    ("mode", "required_count"),
    [
        (CompletionMode.SINGLE, None),
        (CompletionMode.ANY, None),
        (CompletionMode.ALL, None),
        (CompletionMode.QUORUM, 2),
    ],
)
def test_work_item_completion_policy_accepts_valid_modes(
    mode: CompletionMode, required_count: int | None
) -> None:
    """验证单人、或签、会签和法定人数四种合法完成规则。"""

    policy = WorkItemCompletionPolicy(mode=mode, required_count=required_count)
    assert policy.mode is mode


def test_work_item_completion_policy_rejects_ambiguous_thresholds() -> None:
    """验证法定人数门槛只允许与 quorum 完成模式配套使用。"""

    with pytest.raises(ValidationError, match="quorum completion"):
        WorkItemCompletionPolicy(mode=CompletionMode.QUORUM)
    with pytest.raises(ValidationError, match="only valid for quorum"):
        WorkItemCompletionPolicy(mode=CompletionMode.ALL, required_count=2)


def test_instance_wait_resume_and_success_are_revision_guarded() -> None:
    """验证实例暂停、恢复和完成均按期望 revision 顺序推进。"""

    started = transition_instance(
        SopInstanceStatus.CREATED,
        SopInstanceStatus.RUNNING,
        actual_revision=0,
        expected_revision=0,
    )
    waiting = transition_instance(
        started.status,
        SopInstanceStatus.WAITING,
        actual_revision=started.revision,
        expected_revision=1,
    )
    resumed = transition_instance(
        waiting.status,
        SopInstanceStatus.RUNNING,
        actual_revision=waiting.revision,
        expected_revision=2,
    )
    completed = transition_instance(
        resumed.status,
        SopInstanceStatus.SUCCEEDED,
        actual_revision=resumed.revision,
        expected_revision=3,
    )

    assert completed.revision == 4
    assert completed.status is SopInstanceStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SopInstanceStatus.SUCCEEDED, SopInstanceStatus.RUNNING),
        (SopInstanceStatus.FAILED, SopInstanceStatus.RUNNING),
        (SopInstanceStatus.CANCELLED, SopInstanceStatus.RUNNING),
        (SopInstanceStatus.TIMED_OUT, SopInstanceStatus.RUNNING),
    ],
)
def test_instance_terminal_states_cannot_be_overwritten(
    current: SopInstanceStatus, target: SopInstanceStatus
) -> None:
    """验证成功、失败、取消和超时终态均不能被后续命令覆盖。"""

    with pytest.raises(StateTransitionError) as caught:
        transition_instance(current, target, actual_revision=9, expected_revision=9)

    assert caught.value.code == "INVALID_STATE_TRANSITION"


def test_revision_conflict_is_checked_before_state_transition() -> None:
    """验证状态转换前优先检查期望 revision，避免并发写入覆盖。"""

    with pytest.raises(RevisionConflictError) as caught:
        transition_instance(
            SopInstanceStatus.RUNNING,
            SopInstanceStatus.WAITING,
            actual_revision=4,
            expected_revision=3,
        )

    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.actual_revision == 4


def test_node_failure_is_terminal_and_retry_requires_a_new_attempt() -> None:
    """验证失败节点执行为终态，重试必须创建新的执行 attempt。"""

    failed = transition_node(
        NodeExecutionStatus.RUNNING,
        NodeExecutionStatus.FAILED,
        actual_revision=1,
        expected_revision=1,
    )
    assert failed.status is NodeExecutionStatus.FAILED

    with pytest.raises(StateTransitionError):
        transition_node(
            failed.status,
            NodeExecutionStatus.SCHEDULED,
            actual_revision=failed.revision,
            expected_revision=2,
        )


def test_work_item_supports_optional_claim_and_unclaim() -> None:
    """验证人工工作项既支持直接完成，也支持认领后退回候选池。"""

    direct = transition_work_item(
        WorkItemStatus.OFFERED,
        WorkItemStatus.COMPLETED,
        actual_revision=0,
    )
    assert direct.status is WorkItemStatus.COMPLETED

    claimed = transition_work_item(
        WorkItemStatus.OFFERED,
        WorkItemStatus.CLAIMED,
        actual_revision=0,
    )
    unclaimed = transition_work_item(
        claimed.status,
        WorkItemStatus.OFFERED,
        actual_revision=claimed.revision,
    )
    assert unclaimed.status is WorkItemStatus.OFFERED


def test_unknown_operation_requires_reconciliation_before_terminal_result() -> None:
    """验证结果未知的副作用操作须经对账才能进入成功或失败终态。"""

    unknown = transition_operation(
        OperationStatus.RUNNING,
        OperationStatus.UNKNOWN,
        actual_revision=1,
    )
    reconciled = transition_operation(
        unknown.status,
        OperationStatus.SUCCEEDED,
        actual_revision=unknown.revision,
    )

    assert reconciled.status is OperationStatus.SUCCEEDED
    with pytest.raises(StateTransitionError):
        transition_operation(
            OperationStatus.SUCCEEDED,
            OperationStatus.RUNNING,
            actual_revision=reconciled.revision,
        )


def test_delivery_can_retry_and_dead_letter_can_be_manually_requeued() -> None:
    """验证事件投递失败可重试，死信可由人工重新入队。"""

    delivering = transition_delivery(
        DeliveryStatus.PENDING,
        DeliveryStatus.DELIVERING,
        actual_revision=0,
    )
    retrying = transition_delivery(
        delivering.status,
        DeliveryStatus.PENDING,
        actual_revision=delivering.revision,
    )
    dead = transition_delivery(
        retrying.status,
        DeliveryStatus.DEAD_LETTER,
        actual_revision=retrying.revision,
    )
    requeued = transition_delivery(
        dead.status,
        DeliveryStatus.PENDING,
        actual_revision=dead.revision,
    )

    assert requeued.status is DeliveryStatus.PENDING


def test_delivered_event_is_terminal() -> None:
    """验证已成功投递的领域事件不能再次回到待投递状态。"""

    with pytest.raises(StateTransitionError):
        transition_delivery(
            DeliveryStatus.DELIVERED,
            DeliveryStatus.PENDING,
            actual_revision=4,
        )
