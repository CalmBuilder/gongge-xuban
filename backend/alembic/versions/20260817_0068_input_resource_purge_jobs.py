"""
@Time       : 2026/08/14 16:45
@Author     : zhanglp8181
@File       : 20260817_0068_input_resource_purge_jobs.py
@CallChain  : Alembic upgrade/downgrade → 输入资源销毁worker持久契约
@Description: 增加带租约和fencing的输入资源在线副本销毁作业，支持崩溃恢复。

Revision ID: 20260817_0068
Revises: 20260816_0067
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260817_0068"
down_revision: str | None = "20260816_0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建输入资源销毁作业表及恢复扫描所需索引。"""

    op.create_table(
        "input_resource_purge_jobs",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("resource_version", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=128), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column(
            "lease_expires_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("error_detail_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'purging', 'succeeded', 'failed', "
            "'dead_letter')",
            name="ck_input_resource_purge_job_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "resource_id",
            "resource_version",
            name="uq_input_resource_purge_job_resource",
        ),
    )
    for column in (
        "tenant_id",
        "resource_id",
        "session_id",
        "requested_by_user_id",
        "status",
        "lease_owner",
        "lease_expires_at",
    ):
        op.create_index(
            op.f(f"ix_input_resource_purge_jobs_{column}"),
            "input_resource_purge_jobs",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """删除输入资源销毁作业表，缺表时拒绝伪造降级成功。"""

    inspector = sa.inspect(op.get_bind())
    if "input_resource_purge_jobs" not in inspector.get_table_names():
        raise RuntimeError("input_resource_purge_jobs is required for downgrade")
    op.drop_table("input_resource_purge_jobs")
