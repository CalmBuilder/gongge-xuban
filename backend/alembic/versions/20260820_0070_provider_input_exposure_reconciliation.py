"""
@Time       : 2026/08/20 16:20
@Author     : zhanglp8181
@File       : 20260820_0070_provider_input_exposure_reconciliation.py
@CallChain  : Alembic upgrade/downgrade → provider输入暴露对账作业
@Description: 建立租户隔离的第三方文件对账、删除、unknown和Attention账本。
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260820_0070"
down_revision: str | None = "20260818_0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建第三方输入文件暴露对账和删除作业表。"""

    precise_time = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table(
        "provider_input_exposure_reconciliation_jobs",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("dispatch_group_id", sa.String(length=128), nullable=False),
        sa.Column("dispatch_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("job_kind", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("provider_file_id", sa.String(length=256), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("dispatch_token", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", precise_time, nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "job_kind IN ('reconcile_exposure', 'delete_file')",
            name="ck_provider_exposure_job_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'reconciled', 'deleted', 'not_found', "
            "'unknown', 'retry_wait', 'dead_letter', 'attention')",
            name="ck_provider_exposure_job_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "dispatch_receipt_id",
            "job_kind",
            name="uq_provider_exposure_receipt_kind",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_file_id",
            "job_kind",
            name="uq_provider_exposure_file_kind",
        ),
    )
    for column in (
        "tenant_id",
        "dispatch_group_id",
        "dispatch_receipt_id",
        "job_kind",
        "provider_file_id",
        "provider_request_id",
        "status",
        "lease_owner",
        "lease_expires_at",
    ):
        op.create_index(
            op.f(f"ix_provider_input_exposure_reconciliation_jobs_{column}"),
            "provider_input_exposure_reconciliation_jobs",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """删除第三方输入文件暴露对账作业表。"""

    op.drop_table("provider_input_exposure_reconciliation_jobs")
