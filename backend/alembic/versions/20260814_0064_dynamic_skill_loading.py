"""
@Time       : 2026/08/12 22:35
@Author     : zhanglp8181
@File       : 20260814_0064_dynamic_skill_loading.py
@CallChain  : Alembic upgrade/downgrade → ExecutionCommand runtime Skill loading contract
@Description: 扩展持久 Execution 命令类型，使运行中加载 Skill 可经 CAS 与 worker 恢复。

Revision ID: 20260814_0064
Revises: 20260814_0063
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260814_0064"
down_revision: str | None = "20260814_0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "execution_commands"
_CONSTRAINT = "ck_execution_command_type"
_EXPANDED = "command_type IN ('cancel', 'steer', 'add_skill')"
_LEGACY = "command_type IN ('cancel', 'steer')"


def upgrade() -> None:
    """把 add_skill 加入持久命令白名单，并允许中断后安全续跑。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError("dynamic Skill loading requires execution_commands")
    _replace_constraint(bind, _EXPANDED)


def downgrade() -> None:
    """存在运行中 Skill 命令事实时拒绝回退，避免丢失可恢复语义。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError("dynamic Skill loading downgrade requires execution_commands")
    count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM execution_commands "
                "WHERE command_type = 'add_skill'"
            )
        ).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with add_skill execution commands")
    _replace_constraint(bind, _LEGACY)


def _replace_constraint(bind: sa.Connection, condition: str) -> None:
    """按方言安全替换命名检查约束，SQLite 使用 batch 重建表。"""

    names = {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item.get("name")
    }
    with op.batch_alter_table(_TABLE) as batch:
        if _CONSTRAINT in names:
            batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, condition)
