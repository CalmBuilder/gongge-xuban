"""
@Time       : 2026/08/01 22:10
@Author     : zhanglp8181
@File       : 20260801_0033_scheduled_task_pagination_indexes.py
@CallChain  : Alembic upgrade → scheduled_tasks → 管理端任务定义分页/档案概览
@Description: 为管理员和普通用户的员工任务状态分页增加更新时间组合索引。

Revision ID: 20260801_0033
Revises: 20260801_0032
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260801_0033"
down_revision: str | None = "20260801_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMIN_INDEX = "ix_sched_tasks_tenant_agent_status_updated"
_USER_INDEX = "ix_sched_tasks_tenant_agent_creator_status_updated"


def upgrade() -> None:
    """创建覆盖管理员与普通用户任务状态分页条件的组合索引。"""

    op.create_index(
        _ADMIN_INDEX,
        "scheduled_tasks",
        ["tenant_id", "agent_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        _USER_INDEX,
        "scheduled_tasks",
        ["tenant_id", "agent_id", "created_by_user_id", "status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """移除任务定义分页索引，不修改任务状态或运行历史。"""

    op.drop_index(_USER_INDEX, table_name="scheduled_tasks")
    op.drop_index(_ADMIN_INDEX, table_name="scheduled_tasks")
