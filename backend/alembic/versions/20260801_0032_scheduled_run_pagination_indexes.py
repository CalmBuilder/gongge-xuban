"""
@Time       : 2026/08/01 21:30
@Author     : zhanglp8181
@File       : 20260801_0032_scheduled_run_pagination_indexes.py
@CallChain  : Alembic upgrade → scheduled_task_runs → 管理端运行记录分页
@Description: 为管理员与普通用户的员工运行记录分页增加稳定排序组合索引。

Revision ID: 20260801_0032
Revises: 20260801_0031
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260801_0032"
down_revision: str | None = "20260801_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN_INDEX = "ix_sched_runs_tenant_agent_scheduled"
_USER_INDEX = "ix_sched_runs_tenant_agent_user_scheduled"


def upgrade() -> None:
    """创建分别覆盖管理员和普通用户查询前缀的运行时间排序索引。"""

    op.create_index(
        _ADMIN_INDEX,
        "scheduled_task_runs",
        ["tenant_id", "agent_id", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        _USER_INDEX,
        "scheduled_task_runs",
        ["tenant_id", "agent_id", "user_id", "scheduled_for"],
        unique=False,
    )


def downgrade() -> None:
    """移除运行记录分页组合索引，不修改任何运行历史。"""

    op.drop_index(_USER_INDEX, table_name="scheduled_task_runs")
    op.drop_index(_ADMIN_INDEX, table_name="scheduled_task_runs")
