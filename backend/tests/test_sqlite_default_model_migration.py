"""
@Time       : 2026/07/27 16:05
@Author     : zhanglp8181
@File       : test_sqlite_default_model_migration.py
@CallChain  : pytest → SQLite 兼容迁移 → model_configs 遗留表
@Description: 验证旧桌面数据库的重复默认清理、生成列和唯一索引补齐。
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.sqlite_legacy import _migrate_unique_default_model


def test_sqlite_legacy_migration_deduplicates_and_enforces_default(tmp_path) -> None:
    """保留最近更新的默认模型，并拒绝后续直接制造同租户重复默认。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-models.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    is_default INTEGER NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO model_configs (id, tenant_id, is_default, updated_at)
                VALUES
                    ('older', 'tenant_a', 1, '2026-07-01 00:00:00'),
                    ('newer', 'tenant_a', 1, '2026-07-02 00:00:00'),
                    ('other', 'tenant_b', 1, '2026-07-01 00:00:00')
                """
            )
        )
        _migrate_unique_default_model(connection, inspect(engine), {"model_configs"})

        defaults = connection.execute(
            text(
                """
                SELECT tenant_id, id
                FROM model_configs
                WHERE is_default = 1
                ORDER BY tenant_id
                """
            )
        ).all()

    assert defaults == [("tenant_a", "newer"), ("tenant_b", "other")]

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO model_configs (id, tenant_id, is_default)
                    VALUES ('duplicate', 'tenant_a', 1)
                    """
                )
            )
    except IntegrityError:
        pass
    else:
        raise AssertionError("SQLite 兼容迁移未建立默认模型唯一索引")
