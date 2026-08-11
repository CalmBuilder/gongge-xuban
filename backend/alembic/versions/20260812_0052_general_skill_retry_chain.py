"""
@Time       : 2026/08/12 23:20
@Author     : zhanglp8181
@File       : 20260812_0052_general_skill_retry_chain.py
@CallChain  : Alembic upgrade/downgrade → GeneralSkillImportJob retry attempt creation
@Description: 约束每个失败导入父作业只能产生一条线性重试 attempt。

Revision ID: 20260812_0052
Revises: 20260812_0051
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0052"
down_revision: str | None = "20260812_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_general_skill_import_retry_attempt"


def upgrade() -> None:
    """在不存在同名唯一约束时为 retry parent/attempt 增加跨进程仲裁。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_import_jobs"):
        return
    unique_names = {
        str(item.get("name"))
        for item in inspector.get_unique_constraints("general_skill_import_jobs")
    }
    if INDEX_NAME in unique_names:
        return
    with op.batch_alter_table("general_skill_import_jobs") as batch_op:
        batch_op.create_unique_constraint(
            INDEX_NAME,
            ["tenant_id", "parent_job_id", "attempt"],
        )


def downgrade() -> None:
    """移除 retry 线性唯一约束，不删除任何导入作业。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_import_jobs"):
        return
    unique_names = {
        str(item.get("name"))
        for item in inspector.get_unique_constraints("general_skill_import_jobs")
    }
    if INDEX_NAME not in unique_names:
        return
    with op.batch_alter_table("general_skill_import_jobs") as batch_op:
        batch_op.drop_constraint(INDEX_NAME, type_="unique")
