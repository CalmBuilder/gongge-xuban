"""
@Time       : 2026/08/20 19:20
@Author     : zhanglp8181
@File       : 20260820_0071_input_resource_purge_tombstones.py
@CallChain  : Alembic upgrade/downgrade → input资源销毁墓碑审计表
@Description: 保存最小租户/资源版本审计身份，支持备份恢复后的幂等墓碑重放。
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "20260820_0071"
down_revision: str | None = "20260820_0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建不可携带文件定位信息的输入资源永久销毁墓碑。"""

    op.create_table(
        "input_resource_purge_tombstones",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("resource_version", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=False),
        sa.Column("purge_job_id", sa.String(length=512), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=128), nullable=True),
        sa.Column("event_kind", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('session_purge', 'upload_cleanup', 'composer_discard', 'replay')",
            name="ck_input_resource_purge_tombstone_event",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "resource_id",
            "resource_version",
            name="uq_input_resource_purge_tombstone_resource",
        ),
    )
    for column in (
        "tenant_id",
        "resource_id",
        "resource_version",
        "purge_job_id",
        "session_id",
        "requested_by_user_id",
        "event_kind",
    ):
        op.create_index(
            op.f(f"ix_input_resource_purge_tombstones_{column}"),
            "input_resource_purge_tombstones",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """删除输入资源销毁墓碑表。"""

    op.drop_table("input_resource_purge_tombstones")
