"""
@Time       : 2026/08/11 00:35
@Author     : zhanglp8181
@File       : 20260811_0046_scheduled_dynamic_runs.py
@CallChain  : Alembic upgrade/downgrade → ScheduledTaskRun → Dynamic Execution 恢复关联
@Description: 为调度运行增加稳定来源快照、动态执行关联和等待恢复状态约束。

Revision ID: 20260811_0046
Revises: 20260810_0045
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0046"
down_revision: str | None = "20260810_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以可恢复顺序增加来源字段，回填历史运行后再建立非空、唯一和状态约束。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("scheduled_task_runs"):
        raise RuntimeError("scheduled dynamic runs require scheduled_task_runs")
    columns = {str(column["name"]) for column in inspector.get_columns("scheduled_task_runs")}
    additions: tuple[tuple[str, sa.types.TypeEngine], ...] = (
        ("execution_id", sa.String(128)),
        ("source_kind", sa.String(64)),
        ("source_ref", sa.String(512)),
        ("source_snapshot_json", sa.JSON()),
        ("source_checksum", sa.String(128)),
    )
    for name, type_ in additions:
        if name not in columns:
            op.add_column(
                "scheduled_task_runs",
                sa.Column(name, type_, nullable=True),
            )

    rows = bind.execute(
        sa.text(
            "SELECT id, source_kind, source_ref, source_snapshot_json, source_checksum "
            "FROM scheduled_task_runs"
        )
    ).mappings()
    for row in rows:
        run_id = str(row["id"])
        source_kind = str(row["source_kind"] or "legacy")
        source_ref = str(row["source_ref"] or f"legacy:{run_id}")
        snapshot = row["source_snapshot_json"]
        if snapshot is None:
            snapshot = json.dumps({"legacy_run_id": run_id})
        checksum = str(
            row["source_checksum"]
            or hashlib.sha256(source_ref.encode("utf-8")).hexdigest()
        )
        bind.execute(
            sa.text(
                "UPDATE scheduled_task_runs SET source_kind=:source_kind, "
                "source_ref=:source_ref, source_snapshot_json=:snapshot, "
                "source_checksum=:checksum WHERE id=:run_id"
            ),
            {
                "source_kind": source_kind,
                "source_ref": source_ref,
                "snapshot": snapshot,
                "checksum": checksum,
                "run_id": run_id,
            },
        )

    refreshed = {
        str(column["name"]): column
        for column in sa.inspect(bind).get_columns("scheduled_task_runs")
    }
    with op.batch_alter_table("scheduled_task_runs") as batch:
        for name, type_ in additions[1:]:
            if bool(refreshed[name].get("nullable")):
                batch.alter_column(name, existing_type=type_, nullable=False)

    _create_unique_if_missing(
        bind,
        "uq_scheduled_task_run_source_ref",
        ["tenant_id", "source_ref"],
    )
    _create_unique_if_missing(
        bind,
        "uq_scheduled_task_run_execution",
        ["tenant_id", "execution_id"],
    )
    _create_check_if_missing(
        bind,
        "ck_scheduled_task_run_status",
        "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'skipped')",
    )
    _create_check_if_missing(
        bind,
        "ck_scheduled_task_run_source_kind",
        "source_kind IN ('schedule', 'manual', 'legacy')",
    )
    _replace_execution_signal_type_check(bind)
    for column in (
        "execution_id",
        "source_kind",
        "source_ref",
        "source_checksum",
    ):
        _create_index_if_missing(
            bind,
            f"ix_scheduled_task_runs_{column}",
            [column],
        )


def downgrade() -> None:
    """存在新调度来源或动态执行关联时拒绝丢弃恢复证据。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {str(column["name"]) for column in inspector.get_columns("scheduled_task_runs")}
    if "source_kind" not in columns:
        return
    facts = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM scheduled_task_runs "
            "WHERE execution_id IS NOT NULL OR source_kind <> 'legacy'"
        )
    ).scalar_one()
    if int(facts) > 0:
        raise RuntimeError("cannot downgrade scheduled dynamic runs with source facts")
    signal_facts = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM execution_signals WHERE signal_type = 'scheduled_start'"
        )
    ).scalar_one()
    if int(signal_facts) > 0:
        raise RuntimeError("cannot downgrade scheduled dynamic runs with start signals")
    if _named_check_exists(bind, "execution_signals", "ck_execution_signal_type"):
        with op.batch_alter_table("execution_signals") as batch:
            batch.drop_constraint("ck_execution_signal_type", type_="check")
    with op.batch_alter_table("execution_signals") as batch:
        batch.create_check_constraint(
            "ck_execution_signal_type",
            "signal_type IN ('command', 'attention_decided', 'timer', 'operation_settled', "
            "'external_event', 'publication_retry')",
        )
    existing_indexes = {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes("scheduled_task_runs")
    }
    for name in (
        "ix_scheduled_task_runs_source_checksum",
        "ix_scheduled_task_runs_source_ref",
        "ix_scheduled_task_runs_source_kind",
        "ix_scheduled_task_runs_execution_id",
    ):
        if name in existing_indexes:
            op.drop_index(name, table_name="scheduled_task_runs")
    with op.batch_alter_table("scheduled_task_runs") as batch:
        for name in (
            "ck_scheduled_task_run_source_kind",
            "ck_scheduled_task_run_status",
        ):
            if _check_exists(bind, name):
                batch.drop_constraint(name, type_="check")
        for name in (
            "uq_scheduled_task_run_execution",
            "uq_scheduled_task_run_source_ref",
        ):
            if _unique_exists(bind, name):
                batch.drop_constraint(name, type_="unique")
        for name in (
            "source_checksum",
            "source_snapshot_json",
            "source_ref",
            "source_kind",
            "execution_id",
        ):
            batch.drop_column(name)


def _create_unique_if_missing(bind: sa.Connection, name: str, columns: list[str]) -> None:
    """幂等创建唯一约束，支持 MySQL 非事务 DDL 断点续跑。"""

    if not _unique_exists(bind, name):
        with op.batch_alter_table("scheduled_task_runs") as batch:
            batch.create_unique_constraint(name, columns)


def _create_check_if_missing(bind: sa.Connection, name: str, condition: str) -> None:
    """幂等创建检查约束，避免部分迁移后重复定义。"""

    if not _check_exists(bind, name):
        with op.batch_alter_table("scheduled_task_runs") as batch:
            batch.create_check_constraint(name, condition)


def _create_index_if_missing(
    bind: sa.Connection,
    name: str,
    columns: list[str],
) -> None:
    """幂等创建来源查询索引。"""

    existing = {str(item["name"]) for item in sa.inspect(bind).get_indexes("scheduled_task_runs")}
    if name not in existing:
        op.create_index(name, "scheduled_task_runs", columns)


def _unique_exists(bind: sa.Connection, name: str) -> bool:
    """检查目标唯一约束是否已经存在。"""

    return name in {
        str(item["name"])
        for item in sa.inspect(bind).get_unique_constraints("scheduled_task_runs")
    }


def _check_exists(bind: sa.Connection, name: str) -> bool:
    """检查目标检查约束是否已经存在。"""

    return name in {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("scheduled_task_runs")
    }


def _replace_execution_signal_type_check(bind: sa.Connection) -> None:
    """扩展统一 Signal 类型以表达调度启动，不另建调度唤醒表。"""

    if not sa.inspect(bind).has_table("execution_signals"):
        raise RuntimeError("scheduled dynamic runs require execution_signals")
    if _named_check_exists(bind, "execution_signals", "ck_execution_signal_type"):
        with op.batch_alter_table("execution_signals") as batch:
            batch.drop_constraint("ck_execution_signal_type", type_="check")
    with op.batch_alter_table("execution_signals") as batch:
        batch.create_check_constraint(
            "ck_execution_signal_type",
            "signal_type IN ('command', 'attention_decided', 'timer', 'operation_settled', "
            "'external_event', 'publication_retry', 'scheduled_start')",
        )


def _named_check_exists(bind: sa.Connection, table: str, name: str) -> bool:
    """检查指定表上的命名检查约束。"""

    return name in {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(table)
    }
