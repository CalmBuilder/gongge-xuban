"""
@Time       : 2026/08/10 23:55
@Author     : zhanglp8181
@File       : 20260811_0048_dynamic_task_quota_leases.py
@CallChain  : Alembic upgrade/downgrade → dynamic_task_quota_leases → 动态任务并发门禁
@Description: 增加跨进程四级并发唯一槽，租约不保存业务载荷且仅服务活动执行。

Revision ID: 20260811_0048
Revises: 20260811_0047
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0048"
down_revision: str | None = "20260811_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入创建不含业务正文的动态任务并发槽位表。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("sop_instances"):
        raise RuntimeError("dynamic task quota leases require sop_instances")
    if not sa.inspect(bind).has_table("dynamic_task_quota_leases"):
        op.create_table(
            "dynamic_task_quota_leases",
            sa.Column("id", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("scope_type", sa.String(64), nullable=False),
            sa.Column("scope_ref", sa.String(512), nullable=False),
            sa.Column("slot_number", sa.Integer(), nullable=False),
            sa.Column("holder_type", sa.String(64), nullable=False),
            sa.Column("holder_id", sa.String(512), nullable=False),
            sa.Column("acquired_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "scope_type IN ('tenant', 'agent', 'user', 'tool')",
                name="ck_dynamic_quota_scope_type",
            ),
            sa.CheckConstraint(
                "holder_type IN ('execution', 'operation')",
                name="ck_dynamic_quota_holder_type",
            ),
            sa.CheckConstraint(
                "slot_number >= 0",
                name="ck_dynamic_quota_slot_nonnegative",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "scope_type",
                "scope_ref",
                "slot_number",
                name="uq_dynamic_quota_scope_slot",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "holder_type",
                "holder_id",
                "scope_type",
                name="uq_dynamic_quota_holder_scope",
            ),
        )
    _ensure_indexes(bind)
    _replace_execution_signal_type_check(bind, include_capacity_retry=True)


def downgrade() -> None:
    """仅允许在没有活动配额租约时降级，避免绕过仍运行实例的容量边界。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("dynamic_task_quota_leases"):
        return
    count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM dynamic_task_quota_leases")).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with active dynamic task quota leases")
    capacity_signals = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM execution_signals "
                "WHERE signal_type = 'capacity_retry'"
            )
        ).scalar_one()
    )
    if capacity_signals:
        raise RuntimeError("cannot downgrade with dynamic task capacity retry signals")
    op.drop_table("dynamic_task_quota_leases")
    _replace_execution_signal_type_check(bind, include_capacity_retry=False)


def _ensure_indexes(bind: sa.Connection) -> None:
    """幂等创建按租户、scope 和 holder 查询租约所需索引。"""

    existing = {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes("dynamic_task_quota_leases")
    }
    definitions = {
        "ix_dynamic_task_quota_leases_tenant_id": ["tenant_id"],
        "ix_dynamic_task_quota_leases_scope_type": ["scope_type"],
        "ix_dynamic_task_quota_leases_scope_ref": ["scope_ref"],
        "ix_dynamic_task_quota_leases_holder_type": ["holder_type"],
        "ix_dynamic_task_quota_leases_holder_id": ["holder_id"],
        "ix_dynamic_quota_holder": ["tenant_id", "holder_type", "holder_id"],
    }
    for name, columns in definitions.items():
        if name not in existing:
            op.create_index(name, "dynamic_task_quota_leases", columns)


def _replace_execution_signal_type_check(
    bind: sa.Connection,
    *,
    include_capacity_retry: bool,
) -> None:
    """扩展统一 Signal 类型表达配额退避，降级时恢复上一 revision 契约。"""

    if not sa.inspect(bind).has_table("execution_signals"):
        raise RuntimeError("dynamic task quota leases require execution_signals")
    checks = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("execution_signals")
    }
    if "ck_execution_signal_type" in checks:
        with op.batch_alter_table("execution_signals") as batch:
            batch.drop_constraint("ck_execution_signal_type", type_="check")
    allowed = (
        "'command', 'attention_decided', 'timer', 'operation_settled', "
        "'external_event', 'publication_retry', 'scheduled_start'"
    )
    if include_capacity_retry:
        allowed += ", 'capacity_retry'"
    with op.batch_alter_table("execution_signals") as batch:
        batch.create_check_constraint(
            "ck_execution_signal_type",
            f"signal_type IN ({allowed})",
        )
