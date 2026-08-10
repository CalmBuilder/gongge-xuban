"""
@Time       : 2026/08/03 23:58
@Author     : zhanglp8181
@File       : 20260803_0038_dynamic_capability_catalog.py
@CallChain  : Alembic upgrade/downgrade → Tool/GeneralSkill/ModelConfig/Operation capability contract
@Description: 扩展动态能力可靠性发布、不可变快照与模型预检事实。

Revision ID: 20260803_0038
Revises: 20260803_0037
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0038"
down_revision: str | None = "20260803_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_ADDITIONS: dict[str, tuple[sa.Column[Any], ...]] = {
    "tools": (
        sa.Column("reliability_contract_json", sa.JSON(), nullable=True),
        sa.Column("reliability_checksum", sa.String(64), nullable=True),
        sa.Column("reliability_published_at", sa.DateTime(), nullable=True),
    ),
    "general_skills": (
        sa.Column("usage_mode", sa.String(64), nullable=True),
        sa.Column("planning_guidance_json", sa.JSON(), nullable=True),
        sa.Column("planning_guidance_checksum", sa.String(64), nullable=True),
        sa.Column("planning_guidance_published_at", sa.DateTime(), nullable=True),
    ),
    "model_configs": (
        sa.Column("capability_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("capability_checksum", sa.String(64), nullable=True),
        sa.Column("preflight_status", sa.String(64), nullable=True),
        sa.Column("preflight_error", sa.Text(), nullable=True),
        sa.Column("capability_verified_at", sa.DateTime(), nullable=True),
    ),
    "sop_operations": (
        sa.Column("capability_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("capability_checksum", sa.String(64), nullable=True),
    ),
}


def upgrade() -> None:
    """以可续跑的 expand/backfill/validate/constraint 顺序建立 B0.3 能力事实。"""

    bind = op.get_bind()
    _preflight(bind)
    _add_missing_columns(bind)
    _backfill_defaults(bind)
    _make_required(bind)
    _create_constraints_and_indexes(bind)


def downgrade() -> None:
    """只允许未发布新契约、未预检模型且未冻结 Operation 快照的库回退。"""

    bind = op.get_bind()
    _preflight_downgrade(bind)
    for table_name, columns in reversed(tuple(TABLE_ADDITIONS.items())):
        if not sa.inspect(bind).has_table(table_name):
            continue
        indexes = _index_names(bind, table_name)
        for name in (
            f"ix_{table_name}_reliability_checksum",
            f"ix_{table_name}_planning_guidance_checksum",
            f"ix_{table_name}_capability_checksum",
            f"ix_{table_name}_usage_mode",
            f"ix_{table_name}_preflight_status",
        ):
            if name in indexes:
                op.drop_index(name, table_name=table_name)
        constraints = _constraint_names(bind, table_name)
        for name in (
            "ck_general_skill_usage_mode",
            "ck_model_config_preflight_status",
        ):
            if name in constraints:
                with op.batch_alter_table(table_name) as batch:
                    batch.drop_constraint(name, type_="check")
        existing = _column_names(bind, table_name)
        with op.batch_alter_table(table_name) as batch:
            for column in reversed(columns):
                if column.name in existing:
                    batch.drop_column(str(column.name))


def _preflight(bind: sa.Connection) -> None:
    """确认所有 B0.3 依赖表存在，禁止在不完整基线上部分启用。"""

    missing = [name for name in TABLE_ADDITIONS if not sa.inspect(bind).has_table(name)]
    if missing:
        raise RuntimeError("dynamic capability migration requires tables: " + ",".join(missing))


def _add_missing_columns(bind: sa.Connection) -> None:
    """逐表添加缺失列，允许 MySQL 非事务 DDL 在中断后继续。"""

    for table_name, additions in TABLE_ADDITIONS.items():
        existing = _column_names(bind, table_name)
        missing = [column for column in additions if column.name not in existing]
        if not missing:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column in missing:
                batch.add_column(column)


def _backfill_defaults(bind: sa.Connection) -> None:
    """为存量工具/模型写入 fail-closed 默认，保留旧 GeneralSkill 的原子执行语义。"""

    metadata = sa.MetaData()
    tools = sa.Table("tools", metadata, autoload_with=bind)
    skills = sa.Table("general_skills", metadata, autoload_with=bind)
    models = sa.Table("model_configs", metadata, autoload_with=bind)
    operations = sa.Table("sop_operations", metadata, autoload_with=bind)
    bind.execute(
        tools.update()
        .where(tools.c.reliability_contract_json.is_(None))
        .values(reliability_contract_json={})
    )
    bind.execute(
        skills.update()
        .where(skills.c.usage_mode.is_(None))
        .values(usage_mode="atomic_execution")
    )
    bind.execute(
        skills.update()
        .where(skills.c.planning_guidance_json.is_(None))
        .values(planning_guidance_json={})
    )
    bind.execute(
        models.update()
        .where(models.c.capability_snapshot_json.is_(None))
        .values(capability_snapshot_json={})
    )
    bind.execute(
        models.update()
        .where(models.c.preflight_status.is_(None))
        .values(preflight_status="unverified")
    )
    bind.execute(
        operations.update()
        .where(operations.c.capability_snapshot_json.is_(None))
        .values(capability_snapshot_json={})
    )


def _make_required(bind: sa.Connection) -> None:
    """收紧存量回填后的目录列，防止 NULL 被解释为已发布。"""

    required = {
        "tools": (("reliability_contract_json", sa.JSON(), None),),
        "general_skills": (
            ("usage_mode", sa.String(64), "atomic_execution"),
            ("planning_guidance_json", sa.JSON(), None),
        ),
        "model_configs": (
            ("capability_snapshot_json", sa.JSON(), None),
            ("preflight_status", sa.String(64), "unverified"),
        ),
        "sop_operations": (("capability_snapshot_json", sa.JSON(), None),),
    }
    for table_name, columns in required.items():
        with op.batch_alter_table(table_name) as batch:
            for name, column_type, default in columns:
                batch.alter_column(
                    name,
                    existing_type=column_type,
                    nullable=False,
                    server_default=default,
                )


def _create_constraints_and_indexes(bind: sa.Connection) -> None:
    """建立使用模式/预检状态约束与 checksum 定位索引。"""

    definitions = {
        "general_skills": (
            "ck_general_skill_usage_mode",
            "usage_mode IN ('atomic_execution', 'planning_guidance')",
        ),
        "model_configs": (
            "ck_model_config_preflight_status",
            "preflight_status IN ('unverified', 'ready', 'failed')",
        ),
    }
    for table_name, (name, condition) in definitions.items():
        if name not in _constraint_names(bind, table_name):
            with op.batch_alter_table(table_name) as batch:
                batch.create_check_constraint(name, condition)
    for table_name, column_name in (
        ("tools", "reliability_checksum"),
        ("general_skills", "usage_mode"),
        ("general_skills", "planning_guidance_checksum"),
        ("model_configs", "preflight_status"),
        ("model_configs", "capability_checksum"),
        ("sop_operations", "capability_checksum"),
    ):
        name = f"ix_{table_name}_{column_name}"
        if name not in _index_names(bind, table_name):
            op.create_index(name, table_name, [column_name], unique=False)


def _preflight_downgrade(bind: sa.Connection) -> None:
    """检查新列中是否已有不可在 0037 表达的生产事实。"""

    violations: list[str] = []
    for table_name, json_column, extra_columns in (
        ("tools", "reliability_contract_json", ("reliability_checksum",)),
        (
            "general_skills",
            "planning_guidance_json",
            ("planning_guidance_checksum", "planning_guidance_published_at"),
        ),
        (
            "model_configs",
            "capability_snapshot_json",
            ("capability_checksum", "capability_verified_at", "preflight_error"),
        ),
        ("sop_operations", "capability_snapshot_json", ("capability_checksum",)),
    ):
        if json_column not in _column_names(bind, table_name):
            continue
        table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
        for row in bind.execute(sa.select(table)).mappings():
            payload = _decode_json(row[json_column])
            if payload or any(row.get(name) is not None for name in extra_columns):
                violations.append(f"{table_name}:{row.get('id', '?')}")
    if "usage_mode" in _column_names(bind, "general_skills"):
        count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skills "
                "WHERE usage_mode <> 'atomic_execution'"
            )
        ).scalar_one()
        if count:
            violations.append("general_skills:planning_guidance")
    if "preflight_status" in _column_names(bind, "model_configs"):
        count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM model_configs "
                "WHERE preflight_status <> 'unverified'"
            )
        ).scalar_one()
        if count:
            violations.append("model_configs:preflight")
    if violations:
        raise RuntimeError(
            "cannot downgrade capability catalog with managed history: "
            + ",".join(violations[:20])
        )


def _decode_json(value: object) -> object:
    """兼容数据库驱动返回的 JSON 对象或原始字符串。"""

    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, (Mapping, list)):
        return value
    raise RuntimeError(f"unsupported JSON value: {type(value).__name__}")


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表的实时列名，用于部分 DDL 续跑。"""

    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """汇总表上可命名的唯一、检查和外键约束。"""

    inspector = sa.inspect(bind)
    items = [
        *inspector.get_unique_constraints(table_name),
        *inspector.get_check_constraints(table_name),
        *inspector.get_foreign_keys(table_name),
    ]
    return {str(item["name"]) for item in items if item.get("name")}


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回表上已建索引名，避免续跑时重复创建。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_indexes(table_name)
        if item.get("name")
    }
