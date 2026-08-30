"""
@Time       : 2026/08/29 15:30
@Author     : zhanglp8181
@File       : 20260829_0073_general_skill_catalog_commands.py
@CallChain  : Alembic upgrade/downgrade → GeneralSkillCatalogCommand → S1-B 快照入库幂等回执
@Description: 为项目内置 Skill 快照导入建立租户隔离、可重放的命令回执表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "20260829_0073"
down_revision: str | None = "20260828_0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建内置 Skill 快照导入命令回执表，允许 MySQL 中断后重试。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("general_skill_catalog_commands"):
        return
    op.create_table(
        "general_skill_catalog_commands",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("command_type", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("command_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("request_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("source_revision", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('committed', 'failed')",
            name="ck_general_skill_catalog_command_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "command_type",
            "command_id",
            name="uq_general_skill_catalog_command",
        ),
    )
    for column in (
        "tenant_id",
        "command_type",
        "command_id",
        "request_checksum",
        "source_revision",
        "status",
        "error_code",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_general_skill_catalog_commands_{column}"),
            "general_skill_catalog_commands",
            [column],
            unique=False,
        )
    op.create_index(
        op.f("ix_general_skill_catalog_command_tenant_created"),
        "general_skill_catalog_commands",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """仅在没有命令回执时删除内置 Skill 快照导入账本。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("general_skill_catalog_commands"):
        return
    count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM general_skill_catalog_commands")
        ).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with general skill catalog command receipts")
    op.drop_table("general_skill_catalog_commands")
