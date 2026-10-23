"""
@Time       : 2026/09/28 16:40
@Author     : zhanglp8181
@File       : 20260928_0084_external_operation_waiting.py
@CallChain  : Alembic upgrade/downgrade → SopOperation/Attempt → provider_task 等待事实
@Description: 为原 Operation/Attempt 增加 waiting_external 状态和 provider 查询检查点。

Revision ID: 20260928_0084
Revises: 20260906_0083
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260928_0084"
down_revision: str | None = "20260906_0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_CHECK = "ck_sop_operation_status"
_ATTEMPT_CHECK = "ck_sop_operation_attempt_status"
_OPERATION_STATUS = (
    "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', "
    "'unknown', 'waiting_external', 'cancelled')"
)
_ATTEMPT_STATUS = (
    "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', 'unknown', "
    "'waiting_external', 'cancelled', 'reused')"
)


def _column_names(table_name: str) -> set[str]:
    """读取目标表现有列，允许 SQLite legacy schema 幂等升级。"""

    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    """读取目标表现有索引，避免重复执行迁移时重建索引。"""

    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _replace_check(table_name: str, constraint_name: str, expression: str) -> None:
    """替换 SQLite/MySQL 共有的状态 CHECK。"""

    bind = op.get_bind()
    names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(table_name)
        if item.get("name")
        and (
            str(item["name"]) == constraint_name
            or "status" in str(item.get("sqltext") or "")
        )
    }
    with op.batch_alter_table(table_name) as batch:
        for name in names:
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(constraint_name, expression)


def upgrade() -> None:
    """加入 provider_task 等待合同、查询检查点并扩展原账本状态约束。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "sop_operations" not in tables or "sop_operation_attempts" not in tables:
        # 某些历史迁移测试和桌面兼容库只持有目录子集；本迁移不能为
        # 不存在的 Operation 账本伪造表结构，待账本迁移实际存在时再扩展。
        return

    operation_columns = _column_names("sop_operations")
    with op.batch_alter_table("sop_operations") as batch:
        additions = {
            "external_strategy": sa.Column("external_strategy", sa.String(64), nullable=True),
            "external_provider": sa.Column("external_provider", sa.String(128), nullable=True),
            "external_submission_id": sa.Column("external_submission_id", sa.String(512), nullable=True),
            "external_task_id": sa.Column("external_task_id", sa.String(512), nullable=True),
            "external_status_url": sa.Column("external_status_url", sa.String(2000), nullable=True),
            "external_status": sa.Column("external_status", sa.String(128), nullable=True),
            "external_next_poll_at": sa.Column("external_next_poll_at", sa.DateTime(), nullable=True),
            "external_deadline_at": sa.Column("external_deadline_at", sa.DateTime(), nullable=True),
            "external_observation_revision": sa.Column(
                "external_observation_revision", sa.Integer(), nullable=False, server_default="0"
            ),
            "external_last_event_id": sa.Column("external_last_event_id", sa.String(512), nullable=True),
            "external_callback_token_hash": sa.Column(
                "external_callback_token_hash", sa.String(128), nullable=True
            ),
            "external_receipt_json": sa.Column("external_receipt_json", sa.JSON(), nullable=True),
            "external_wait_started_at": sa.Column("external_wait_started_at", sa.DateTime(), nullable=True),
        }
        for name, column in additions.items():
            if name not in operation_columns:
                batch.add_column(column)

    attempt_columns = _column_names("sop_operation_attempts")
    with op.batch_alter_table("sop_operation_attempts") as batch:
        additions = {
            "external_task_id": sa.Column("external_task_id", sa.String(512), nullable=True),
            "external_status": sa.Column("external_status", sa.String(128), nullable=True),
            "external_observation_revision": sa.Column(
                "external_observation_revision", sa.Integer(), nullable=False, server_default="0"
            ),
            "external_receipt_json": sa.Column("external_receipt_json", sa.JSON(), nullable=True),
            "waiting_at": sa.Column("waiting_at", sa.DateTime(), nullable=True),
        }
        for name, column in additions.items():
            if name not in attempt_columns:
                batch.add_column(column)

    _replace_check("sop_operations", _OPERATION_CHECK, _OPERATION_STATUS)
    _replace_check("sop_operation_attempts", _ATTEMPT_CHECK, _ATTEMPT_STATUS)

    operation_indexes = _index_names("sop_operations")
    for name, column in (
        ("ix_sop_operations_external_submission_id", "external_submission_id"),
        ("ix_sop_operations_external_task_id", "external_task_id"),
        ("ix_sop_operations_external_next_poll_at", "external_next_poll_at"),
    ):
        if name not in operation_indexes:
            op.create_index(name, "sop_operations", [column])
    attempt_indexes = _index_names("sop_operation_attempts")
    for name, column in (
        ("ix_sop_operation_attempts_external_task_id", "external_task_id"),
        ("ix_sop_operation_attempts_external_status", "external_status"),
    ):
        if name not in attempt_indexes:
            op.create_index(name, "sop_operation_attempts", [column])


def downgrade() -> None:
    """有外部等待事实时拒绝回退，以免丢失远端身份和不重复派发证据。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "sop_operations" not in tables or "sop_operation_attempts" not in tables:
        return
    active = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM sop_operations WHERE status = 'waiting_external'")
        ).scalar_one()
    )
    if active:
        raise RuntimeError("cannot downgrade while external operations are waiting")

    for table_name, indexes in (
        (
            "sop_operations",
            (
                "ix_sop_operations_external_submission_id",
                "ix_sop_operations_external_task_id",
                "ix_sop_operations_external_next_poll_at",
            ),
        ),
        (
            "sop_operation_attempts",
            (
                "ix_sop_operation_attempts_external_task_id",
                "ix_sop_operation_attempts_external_status",
            ),
        ),
    ):
        existing = _index_names(table_name)
        for name in indexes:
            if name in existing:
                op.drop_index(name, table_name=table_name)

    with op.batch_alter_table("sop_operations") as batch:
        for name in (
            "external_strategy",
            "external_provider",
            "external_submission_id",
            "external_task_id",
            "external_status_url",
            "external_status",
            "external_next_poll_at",
            "external_deadline_at",
            "external_observation_revision",
            "external_last_event_id",
            "external_callback_token_hash",
            "external_receipt_json",
            "external_wait_started_at",
        ):
            if name in _column_names("sop_operations"):
                batch.drop_column(name)
    with op.batch_alter_table("sop_operation_attempts") as batch:
        for name in (
            "external_task_id",
            "external_status",
            "external_observation_revision",
            "external_receipt_json",
            "waiting_at",
        ):
            if name in _column_names("sop_operation_attempts"):
                batch.drop_column(name)

    _replace_check(
        "sop_operations",
        _OPERATION_CHECK,
        "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', 'unknown', 'cancelled')",
    )
    _replace_check(
        "sop_operation_attempts",
        _ATTEMPT_CHECK,
        "status IN ('prepared', 'running', 'retry_wait', 'succeeded', 'failed', 'unknown', 'cancelled', 'reused')",
    )
