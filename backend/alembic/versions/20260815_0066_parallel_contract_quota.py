"""
@Time       : 2026/08/13 00:20
@Author     : zhanglp8181
@File       : 20260815_0066_parallel_contract_quota.py
@CallChain  : Alembic upgrade/downgrade → DynamicTaskQuotaLease parallel read contract
@Description: 扩展数据库并发槽类型，使低风险读取按 tenant+concurrency_key 跨进程限流。

Revision ID: 20260815_0066
Revises: 20260814_0065
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260815_0066"
down_revision: str | None = "20260814_0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dynamic_task_quota_leases"
_CONSTRAINT = "ck_dynamic_quota_scope_type"
_EXPANDED = "scope_type IN ('tenant', 'agent', 'user', 'tool', 'parallel_contract')"
_LEGACY = "scope_type IN ('tenant', 'agent', 'user', 'tool')"


def upgrade() -> None:
    """允许持久并发槽按工具发布的 concurrency_key 仲裁。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError("parallel contract quota requires dynamic_task_quota_leases")
    _replace_constraint(bind, _EXPANDED)


def downgrade() -> None:
    """存在并行契约槽时拒绝降级，防止丢失仍在运行的全局限流事实。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError("parallel contract quota downgrade requires quota table")
    active = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM dynamic_task_quota_leases "
                "WHERE scope_type = 'parallel_contract'"
            )
        ).scalar_one()
    )
    if active:
        raise RuntimeError("cannot downgrade with active parallel contract quota leases")
    _replace_constraint(bind, _LEGACY)


def _replace_constraint(bind: sa.Connection, condition: str) -> None:
    """按方言替换命名检查约束，SQLite 通过 batch 模式重建。"""

    names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item.get("name")
    }
    with op.batch_alter_table(_TABLE) as batch:
        if _CONSTRAINT in names:
            batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, condition)
