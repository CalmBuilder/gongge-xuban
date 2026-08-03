"""
@Time       : 2026/07/27 15:40
@Author     : zhanglp8181
@File       : 20260727_0014_unique_default_model.py
@CallChain  : Alembic upgrade/downgrade → model_configs 默认标记 → 模型路由
@Description: 清理同租户重复默认模型，并以生成列唯一索引维护双方言一致性。

Revision ID: 20260727_0014
Revises: 20260722_0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_0014"
down_revision: str | None = "20260722_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _clear_duplicate_defaults(connection: sa.Connection) -> None:
    """每个租户保留最近更新的一个默认模型，并清除其余遗留重复标记。"""

    model_configs = sa.table(
        "model_configs",
        sa.column("id", sa.String(length=512)),
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("is_default", sa.Boolean()),
        sa.column("updated_at", sa.DateTime()),
    )
    rows = connection.execute(
        sa.select(model_configs.c.id, model_configs.c.tenant_id)
        .where(model_configs.c.is_default.is_(True))
        .order_by(
            model_configs.c.tenant_id,
            model_configs.c.updated_at.desc(),
            model_configs.c.id,
        )
    ).all()
    seen_tenants: set[str] = set()
    duplicate_ids: list[str] = []
    for model_id, tenant_id in rows:
        if tenant_id in seen_tenants:
            duplicate_ids.append(model_id)
        else:
            seen_tenants.add(tenant_id)
    if duplicate_ids:
        connection.execute(
            sa.update(model_configs)
            .where(model_configs.c.id.in_(duplicate_ids))
            .values(is_default=False)
        )


def upgrade() -> None:
    """清理旧重复值，并增加跨 SQLite/MySQL 可用的默认模型唯一索引。"""

    _clear_duplicate_defaults(op.get_bind())
    op.add_column(
        "model_configs",
        sa.Column(
            "default_tenant_id",
            sa.String(length=128),
            sa.Computed("CASE WHEN is_default THEN tenant_id ELSE NULL END"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_model_configs_tenant_default",
        "model_configs",
        ["default_tenant_id"],
        unique=True,
    )


def downgrade() -> None:
    """移除默认模型唯一索引与其生成列，不恢复已清除的重复默认标记。"""

    op.drop_index("uq_model_configs_tenant_default", table_name="model_configs")
    op.drop_column("model_configs", "default_tenant_id")
