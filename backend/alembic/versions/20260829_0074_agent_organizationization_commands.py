"""
@Time       : 2026/08/29 18:10
@Author     : zhanglp8181
@File       : 20260829_0074_agent_organizationization_commands.py
@CallChain  : Alembic upgrade/downgrade → AgentOrganizationizationCommand → 组织化原子配置回执
@Description: 为数字员工组织化配置建立租户隔离、版本校验和可重放的命令回执表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "20260829_0074"
down_revision: str | None = "20260829_0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建组织化原子配置命令回执表，允许请求超时后安全重放。"""

    bind = op.get_bind()
    if sa.inspect(bind).has_table("agent_organizationization_commands"):
        return
    op.create_table(
        "agent_organizationization_commands",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("agent_id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("command_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("request_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("expected_profile_revision", sa.Integer(), nullable=False),
        sa.Column(
            "expected_relationship_checksum",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column(
            "active_role_binding_id",
            sqlmodel.sql.sqltypes.AutoString(length=512),
            nullable=True,
        ),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('committed', 'failed')",
            name="ck_agent_organizationization_command_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "command_id",
            name="uq_agent_organizationization_command",
        ),
    )
    for column in (
        "tenant_id",
        "agent_id",
        "command_id",
        "request_checksum",
        "expected_profile_revision",
        "expected_relationship_checksum",
        "active_role_binding_id",
        "status",
        "error_code",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_agent_organizationization_commands_{column}"),
            "agent_organizationization_commands",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """仅在没有组织化命令回执时删除命令账本。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_organizationization_commands"):
        return
    count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM agent_organizationization_commands")
        ).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade with agent organizationization command receipts")
    op.drop_table("agent_organizationization_commands")
