"""
@Time       : 2026/08/12 22:55
@Author     : zhanglp8181
@File       : 20260812_0051_general_skill_import_quotas.py
@CallChain  : Alembic upgrade/downgrade → ImportJob quota reservation → S1 worker cleanup
@Description: 增加 tenant/user 两级原子导入并发与暂存字节计数。

Revision ID: 20260812_0051
Revises: 20260812_0050
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0051"
down_revision: str | None = "20260812_0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可重入创建两级导入配额计数表。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("general_skill_import_quotas"):
        op.create_table(
            "general_skill_import_quotas",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(512), nullable=False),
        sa.Column("scope_kind", sa.String(64), nullable=False),
        sa.Column("scope_id", sa.String(512), nullable=False),
        sa.Column("active_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("staged_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "scope_kind IN ('tenant', 'user')",
            name="ck_general_skill_import_quota_scope_kind",
        ),
        sa.CheckConstraint(
            "active_jobs >= 0 AND staged_bytes >= 0 AND row_version >= 1",
            name="ck_general_skill_import_quota_nonnegative",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "scope_kind",
            "scope_id",
            name="uq_general_skill_import_quota_scope",
        ),
        )
    _backfill_active_jobs(bind)


def downgrade() -> None:
    """仅在所有计数归零时移除配额表，防止掩盖未回收作业。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("general_skill_import_quotas"):
        return
    active = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skill_import_quotas "
                "WHERE active_jobs <> 0 OR staged_bytes <> 0"
            )
        ).scalar_one()
    )
    if active:
        raise RuntimeError("cannot downgrade with active general skill import quota")
    op.drop_table("general_skill_import_quotas")


def _backfill_active_jobs(bind: sa.Connection) -> None:
    """为迁移时已存在的非终态作业建立两级计数，重复执行不叠加。"""

    if not sa.inspect(bind).has_table("general_skill_import_jobs"):
        return
    terminal = "('installed','failed','cancelled','expired')"
    rows = list(
        bind.execute(
            sa.text(
                "SELECT tenant_id, owner_user_id, quota_bytes FROM general_skill_import_jobs "
                f"WHERE status NOT IN {terminal}"
            )
        ).mappings()
    )
    aggregates: dict[tuple[str, str, str], tuple[int, int]] = {}
    for row in rows:
        tenant_id = str(row["tenant_id"])
        owner_user_id = str(row["owner_user_id"])
        quota_bytes = int(row["quota_bytes"] or 0)
        for key in (
            (tenant_id, "tenant", tenant_id),
            (tenant_id, "user", owner_user_id),
        ):
            active, staged = aggregates.get(key, (0, 0))
            aggregates[key] = (active + 1, staged + quota_bytes)
    now = datetime.now(UTC).replace(tzinfo=None)
    for (tenant_id, scope_kind, scope_id), (active_jobs, staged_bytes) in aggregates.items():
        exists = bind.execute(
            sa.text(
                "SELECT id FROM general_skill_import_quotas "
                "WHERE tenant_id = :tenant_id AND scope_kind = :scope_kind "
                "AND scope_id = :scope_id"
            ),
            {
                "tenant_id": tenant_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
            },
        ).first()
        if exists:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO general_skill_import_quotas "
                "(id, tenant_id, scope_kind, scope_id, active_jobs, staged_bytes, "
                "row_version, created_at, updated_at) VALUES "
                "(:id, :tenant_id, :scope_kind, :scope_id, :active_jobs, :staged_bytes, "
                "1, :created_at, :updated_at)"
            ),
            {
                "id": f"gsquota_{uuid4().hex[:16]}",
                "tenant_id": tenant_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "active_jobs": active_jobs,
                "staged_bytes": staged_bytes,
                "created_at": now,
                "updated_at": now,
            },
        )
