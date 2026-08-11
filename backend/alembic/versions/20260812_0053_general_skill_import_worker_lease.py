"""
@Time       : 2026/08/12 23:50
@Author     : zhanglp8181
@File       : 20260812_0053_general_skill_import_worker_lease.py
@CallChain  : Alembic upgrade/downgrade → import worker claim/renew/fence
@Description: 为 Skill 导入作业增加持久 worker lease 与单调 fencing token。

Revision ID: 20260812_0053
Revises: 20260812_0052
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0053"
down_revision: str | None = "20260812_0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入增加 worker、lease 到期时间、fencing token 与扫描索引。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_import_jobs"):
        return
    columns = {item["name"] for item in inspector.get_columns("general_skill_import_jobs")}
    with op.batch_alter_table("general_skill_import_jobs") as batch_op:
        if "worker_id" not in columns:
            batch_op.add_column(sa.Column("worker_id", sa.String(512), nullable=True))
        if "lease_expires_at" not in columns:
            batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        if "lease_token" not in columns:
            batch_op.add_column(
                sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0")
            )
    inspector = sa.inspect(bind)
    checks = {item["name"] for item in inspector.get_check_constraints("general_skill_import_jobs")}
    if "ck_general_skill_import_lease_token" not in checks:
        with op.batch_alter_table("general_skill_import_jobs") as batch_op:
            batch_op.create_check_constraint(
                "ck_general_skill_import_lease_token",
                "lease_token >= 0",
            )
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("general_skill_import_jobs")}
    with op.batch_alter_table("general_skill_import_jobs") as batch_op:
        if "ix_general_skill_import_jobs_worker_id" not in indexes:
            batch_op.create_index("ix_general_skill_import_jobs_worker_id", ["worker_id"])
        if "ix_general_skill_import_jobs_lease_expires_at" not in indexes:
            batch_op.create_index(
                "ix_general_skill_import_jobs_lease_expires_at",
                ["lease_expires_at"],
            )


def downgrade() -> None:
    """仅在没有活动 lease 时移除 worker 字段，防止降级制造双处理者。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skill_import_jobs"):
        return
    columns = {item["name"] for item in inspector.get_columns("general_skill_import_jobs")}
    if "worker_id" in columns:
        active = int(
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM general_skill_import_jobs "
                    "WHERE worker_id IS NOT NULL OR lease_expires_at IS NOT NULL"
                )
            ).scalar_one()
        )
        if active:
            raise RuntimeError("cannot downgrade with active general skill import worker leases")
    indexes = {item["name"] for item in inspector.get_indexes("general_skill_import_jobs")}
    checks = {item["name"] for item in inspector.get_check_constraints("general_skill_import_jobs")}
    with op.batch_alter_table("general_skill_import_jobs") as batch_op:
        if "ix_general_skill_import_jobs_lease_expires_at" in indexes:
            batch_op.drop_index("ix_general_skill_import_jobs_lease_expires_at")
        if "ix_general_skill_import_jobs_worker_id" in indexes:
            batch_op.drop_index("ix_general_skill_import_jobs_worker_id")
        if "ck_general_skill_import_lease_token" in checks:
            batch_op.drop_constraint("ck_general_skill_import_lease_token", type_="check")
        for column in ("lease_token", "lease_expires_at", "worker_id"):
            if column in columns:
                batch_op.drop_column(column)
