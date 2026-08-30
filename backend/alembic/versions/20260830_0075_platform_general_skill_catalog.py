"""
@Time       : 2026/08/30 10:30
@Author     : zhanglp8181
@File       : 20260830_0075_platform_general_skill_catalog.py
@CallChain  : Alembic upgrade → 租户内置 Skill 快照合并 → 项目级 GeneralSkill 目录
@Description: 把内置 Skill 主体从租户副本迁移为项目级唯一资产，保留租户绑定和历史命令边界。

Revision ID: 20260830_0075
Revises: 20260829_0074
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
import json

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0075"
down_revision: str | None = "20260829_0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_SCOPE = "platform"
TENANT_SCOPE = "tenant"
PLATFORM_SCOPE_KEY = "platform"


def upgrade() -> None:
    """增加范围契约、合并一致的租户快照并建立项目级唯一约束。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not all(
        inspector.has_table(name)
        for name in ("general_skills", "general_skill_revisions", "general_skill_catalog_commands")
    ):
        return
    _add_scope_columns(bind)
    _backfill_scope_defaults(bind)
    _expand_visibility_constraint(bind)
    _merge_legacy_catalog_rows(bind)
    _tighten_scope_columns(bind)
    _create_scope_constraints_and_indexes(bind)


def downgrade() -> None:
    """仅在项目级目录和平台命令均为空时回退范围字段，拒绝破坏唯一资产。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("general_skills"):
        return
    platform_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skills "
                "WHERE catalog_scope = 'platform' OR tenant_id IS NULL"
            )
        ).scalar_one()
    )
    revision_platform_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skill_revisions "
                "WHERE catalog_scope = 'platform' OR tenant_id IS NULL"
            )
        ).scalar_one()
    ) if inspector.has_table("general_skill_revisions") else 0
    command_platform_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM general_skill_catalog_commands "
                "WHERE catalog_scope = 'platform' OR tenant_id IS NULL"
            )
        ).scalar_one()
    ) if inspector.has_table("general_skill_catalog_commands") else 0
    if platform_count or revision_platform_count or command_platform_count:
        raise RuntimeError(
            "cannot downgrade while platform general skill catalog contains assets or receipts"
        )
    _drop_scope_constraints_and_indexes(bind)
    _drop_scope_columns(bind)


def _add_scope_columns(bind: sa.Connection) -> None:
    """以可重入方式增加目录范围列，并先保持旧行可写。"""

    additions: dict[str, tuple[sa.Column[object], ...]] = {
        "general_skills": (
            sa.Column("catalog_scope", sa.String(64), nullable=True, server_default=TENANT_SCOPE),
            sa.Column("catalog_key", sa.String(128), nullable=True),
        ),
        "general_skill_revisions": (
            sa.Column("catalog_scope", sa.String(64), nullable=True, server_default=TENANT_SCOPE),
        ),
        "general_skill_catalog_commands": (
            sa.Column("catalog_scope", sa.String(64), nullable=True, server_default=TENANT_SCOPE),
            sa.Column("scope_key", sa.String(128), nullable=True),
        ),
    }
    for table_name, columns in additions.items():
        existing = _column_names(bind, table_name)
        missing = [column for column in columns if str(column.name) not in existing]
        if missing:
            with op.batch_alter_table(table_name) as batch:
                for column in missing:
                    batch.add_column(column)

    if "tenant_id" in _column_names(bind, "general_skills"):
        with op.batch_alter_table("general_skills") as batch:
            batch.alter_column(
                "tenant_id",
                existing_type=sa.String(128),
                nullable=True,
            )
    if "tenant_id" in _column_names(bind, "general_skill_revisions"):
        with op.batch_alter_table("general_skill_revisions") as batch:
            batch.alter_column(
                "tenant_id",
                existing_type=sa.String(128),
                nullable=True,
            )
    if "tenant_id" in _column_names(bind, "general_skill_catalog_commands"):
        with op.batch_alter_table("general_skill_catalog_commands") as batch:
            batch.alter_column(
                "tenant_id",
                existing_type=sa.String(128),
                nullable=True,
            )


def _backfill_scope_defaults(bind: sa.Connection) -> None:
    """为历史租户行建立明确范围和命令作用域键。"""

    bind.execute(
        sa.text(
            "UPDATE general_skills SET catalog_scope = :scope "
            "WHERE catalog_scope IS NULL"
        ),
        {"scope": TENANT_SCOPE},
    )
    bind.execute(
        sa.text(
            "UPDATE general_skill_revisions SET catalog_scope = :scope "
            "WHERE catalog_scope IS NULL"
        ),
        {"scope": TENANT_SCOPE},
    )
    bind.execute(
        sa.text(
            "UPDATE general_skill_catalog_commands SET catalog_scope = :scope, "
            "scope_key = tenant_id WHERE catalog_scope IS NULL OR scope_key IS NULL"
        ),
        {"scope": TENANT_SCOPE},
    )


def _expand_visibility_constraint(bind: sa.Connection) -> None:
    """先允许 platform_gallery，再执行把旧行提升为平台资产的数据更新。"""

    checks = _constraint_names(bind, "general_skills")
    if "ck_general_skill_visibility_scope" not in checks:
        return
    with op.batch_alter_table("general_skills") as batch:
        batch.drop_constraint("ck_general_skill_visibility_scope", type_="check")
        batch.create_check_constraint(
            "ck_general_skill_visibility_scope",
            "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery', 'platform_gallery')",
        )


def _merge_legacy_catalog_rows(bind: sa.Connection) -> None:
    """合并内容完全一致的旧内置副本，冲突时中止而不是覆盖租户事实。"""

    skill_rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, slug, status, metadata_json, "
            "current_published_revision_id FROM general_skills "
            "WHERE catalog_scope = 'tenant'"
        )
    ).mappings().all()
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in skill_rows:
        metadata = _json_dict(row["metadata_json"])
        if metadata.get("managed_catalog") is True and isinstance(metadata.get("catalog_key"), str):
            groups[str(metadata["catalog_key"])].append(row)

    skill_map: dict[str, str] = {}
    revision_map: dict[str, str] = {}
    canonical_rows: list[tuple[Mapping[str, object], str]] = []
    for catalog_key, rows in sorted(groups.items()):
        _ensure_same_skill_content(catalog_key, rows)
        canonical = sorted(
            rows,
            key=lambda row: (str(row["status"]) != "published", str(row["id"])),
        )[0]
        canonical_id = str(canonical["id"])
        canonical_rows.append((canonical, catalog_key))
        for row in rows:
            skill_id = str(row["id"])
            skill_map[skill_id] = canonical_id

    for canonical, catalog_key in canonical_rows:
        canonical_id = str(canonical["id"])
        group = groups[catalog_key]
        revisions_by_skill = {
            str(row["id"]): _skill_revisions(bind, str(row["id"]))
            for row in group
        }
        _ensure_same_revision_shapes(catalog_key, revisions_by_skill)
        canonical_revisions = revisions_by_skill[canonical_id]
        canonical_by_shape = {
            (int(row["revision_number"]), str(row["content_checksum"])): row
            for row in canonical_revisions
        }
        for row in canonical_revisions:
            revision_map[str(row["id"])] = str(row["id"])
        for skill_id, revisions in revisions_by_skill.items():
            for row in revisions:
                shape = (int(row["revision_number"]), str(row["content_checksum"]))
                target = canonical_by_shape.get(shape)
                if target is None:
                    raise RuntimeError(
                        f"platform Skill catalog revision conflict for catalog key {catalog_key}"
                    )
                revision_map[str(row["id"])] = str(target["id"])

        published_revision_id = _published_revision_id(group, revisions_by_skill, revision_map)
        skill_status = "published" if any(str(row["status"]) == "published" for row in group) else str(
            canonical["status"]
        )
        metadata = _json_dict(canonical["metadata_json"])
        metadata["catalog_key"] = catalog_key
        metadata["catalog_scope"] = PLATFORM_SCOPE
        bind.execute(
            sa.text(
                "UPDATE general_skills SET tenant_id = NULL, catalog_scope = :scope, "
                "catalog_key = :catalog_key, owner_user_id = NULL, "
                "visibility_scope = 'platform_gallery', status = :status, "
                "current_published_revision_id = :revision_id, metadata_json = :metadata "
                "WHERE id = :skill_id"
            ),
            {
                "scope": PLATFORM_SCOPE,
                "catalog_key": catalog_key,
                "status": skill_status,
                "revision_id": published_revision_id,
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "skill_id": canonical_id,
            },
        )
        for row in canonical_revisions:
            revision_id = str(row["id"])
            bind.execute(
                sa.text(
                    "UPDATE general_skill_revisions SET tenant_id = NULL, "
                    "catalog_scope = :scope WHERE id = :revision_id"
                ),
                {"scope": PLATFORM_SCOPE, "revision_id": revision_id},
            )

    _remap_catalog_references(bind, skill_map, revision_map)
    duplicate_revision_ids = [
        old_id for old_id, new_id in revision_map.items() if old_id != new_id
    ]
    for revision_id in duplicate_revision_ids:
        bind.execute(
            sa.text("DELETE FROM general_skill_revisions WHERE id = :revision_id"),
            {"revision_id": revision_id},
        )
    duplicate_skill_ids = [old_id for old_id, new_id in skill_map.items() if old_id != new_id]
    for skill_id in duplicate_skill_ids:
        bind.execute(
            sa.text("DELETE FROM general_skills WHERE id = :skill_id"),
            {"skill_id": skill_id},
        )

    # 普通租户 Skill 不得借用平台可见性；这条更新只修复旧行，不触碰普通正文。
    bind.execute(
        sa.text(
            "UPDATE general_skills SET catalog_scope = 'tenant' "
            "WHERE catalog_scope IS NULL"
        )
    )


def _ensure_same_skill_content(catalog_key: str, rows: Sequence[Mapping[str, object]]) -> None:
    """确保同一来源键的旧副本正文摘要一致。"""

    fingerprints = {
        (
            _json_dict(row["metadata_json"]).get("content_checksum"),
            _json_dict(row["metadata_json"]).get("source_normalized_checksum"),
        )
        for row in rows
    }
    if len(fingerprints) > 1:
        raise RuntimeError(
            f"platform Skill catalog content conflict for catalog key {catalog_key}"
        )


def _skill_revisions(bind: sa.Connection, skill_id: str) -> list[Mapping[str, object]]:
    """读取一个旧 Skill 的全部 revision，避免把 JSON 过滤交给方言。"""

    return bind.execute(
        sa.text(
            "SELECT id, revision_number, content_checksum, manifest_checksum, status, "
            "published_at FROM general_skill_revisions WHERE skill_id = :skill_id "
            "AND catalog_scope = 'tenant' ORDER BY revision_number, id"
        ),
        {"skill_id": skill_id},
    ).mappings().all()


def _ensure_same_revision_shapes(
    catalog_key: str,
    revisions_by_skill: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """确保重复快照具有同一组修订，避免丢失租户已产生的目录版本。"""

    shapes = {
        tuple(
            (int(row["revision_number"]), str(row["content_checksum"]), str(row["manifest_checksum"]))
            for row in revisions
        )
        for revisions in revisions_by_skill.values()
    }
    if len(shapes) > 1:
        raise RuntimeError(
            f"platform Skill catalog revision conflict for catalog key {catalog_key}"
        )


def _published_revision_id(
    group: Sequence[Mapping[str, object]],
    revisions_by_skill: Mapping[str, Sequence[Mapping[str, object]]],
    revision_map: Mapping[str, str],
) -> str | None:
    """选择已发布旧指针映射后的 canonical revision。"""

    candidates: list[str] = []
    for skill in group:
        if str(skill["status"]) != "published":
            continue
        pointer = skill.get("current_published_revision_id")
        if pointer:
            candidates.append(revision_map.get(str(pointer), str(pointer)))
        for revision in revisions_by_skill.get(str(skill["id"]), ()):
            if str(revision["status"]) == "published":
                candidates.append(revision_map.get(str(revision["id"]), str(revision["id"])))
    return sorted(set(candidates))[0] if candidates else None


def _remap_catalog_references(
    bind: sa.Connection,
    skill_map: Mapping[str, str],
    revision_map: Mapping[str, str],
) -> None:
    """先重指向绑定、发布、使用和提案，再删除重复主体，保持历史可追溯。"""

    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    column_map = {
        "skill_id": skill_map,
        "parent_skill_id": skill_map,
        "child_skill_id": skill_map,
        "target_skill_id": skill_map,
        "revision_id": revision_map,
        "parent_revision_id": revision_map,
        "child_revision_id": revision_map,
        "approved_revision_id": revision_map,
        "base_revision_id": revision_map,
    }
    for table_name in sorted(table_names):
        if not (table_name.startswith("general_skill_") or table_name == "session_general_skill_overrides"):
            continue
        if table_name in {"general_skills", "general_skill_revisions"}:
            continue
        columns = _column_names(bind, table_name)
        for column_name, replacements in column_map.items():
            if column_name not in columns:
                continue
            for old_id, new_id in replacements.items():
                if old_id == new_id:
                    continue
                bind.execute(
                    sa.text(
                        f"UPDATE {table_name} SET {column_name} = :new_id "
                        f"WHERE {column_name} = :old_id"
                    ),
                    {"old_id": old_id, "new_id": new_id},
                )

    if "agent_resource_bindings" in table_names:
        for old_id, new_id in skill_map.items():
            if old_id == new_id:
                continue
            bind.execute(
                sa.text(
                    "UPDATE agent_resource_bindings SET resource_id = :new_id "
                    "WHERE resource_type = 'general_skill' AND resource_id = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )
    if "publication_releases" in table_names:
        for old_id, new_id in skill_map.items():
            if old_id == new_id:
                continue
            bind.execute(
                sa.text(
                    "UPDATE publication_releases SET resource_id = :new_id "
                    "WHERE resource_type = 'general_skill' AND resource_id = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )

    for table_name, json_column in (
        ("general_skill_catalog_commands", "result_json"),
        ("general_skill_install_intents", "installed_revision_ids_json"),
    ):
        if table_name not in table_names:
            continue
        rows = bind.execute(
            sa.text(f"SELECT id, {json_column} FROM {table_name}")
        ).mappings().all()
        for row in rows:
            original = _json_value(row[json_column])
            replaced = _replace_ids(original, skill_map, revision_map)
            if replaced == original:
                continue
            bind.execute(
                sa.text(f"UPDATE {table_name} SET {json_column} = :payload WHERE id = :id"),
                {"id": row["id"], "payload": json.dumps(replaced, ensure_ascii=False)},
            )


def _replace_ids(value: object, skill_map: Mapping[str, str], revision_map: Mapping[str, str]) -> object:
    """递归替换历史命令 JSON 中已合并的 Skill/Revision 标识。"""

    if isinstance(value, str):
        return revision_map.get(value, skill_map.get(value, value))
    if isinstance(value, list):
        return [_replace_ids(item, skill_map, revision_map) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_ids(item, skill_map, revision_map)
            for key, item in value.items()
        }
    return value


def _tighten_scope_columns(bind: sa.Connection) -> None:
    """在数据合并后收紧范围列，防止新代码写入无范围命令或修订。"""

    for table_name, columns in (
        ("general_skills", ("catalog_scope",)),
        ("general_skill_revisions", ("catalog_scope",)),
        ("general_skill_catalog_commands", ("catalog_scope", "scope_key")),
    ):
        with op.batch_alter_table(table_name) as batch:
            for column_name in columns:
                batch.alter_column(
                    column_name,
                    existing_type=sa.String(128 if column_name == "scope_key" else 64),
                    nullable=False,
                    server_default=None,
                )


def _create_scope_constraints_and_indexes(bind: sa.Connection) -> None:
    """创建平台/租户互斥约束与跨方言查询索引。"""

    checks = _constraint_names(bind, "general_skills")
    with op.batch_alter_table("general_skills") as batch:
        if "ck_general_skill_visibility_scope" not in checks:
            batch.create_check_constraint(
                "ck_general_skill_visibility_scope",
                "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery', 'platform_gallery')",
            )
        if "ck_general_skill_catalog_scope" not in checks:
            batch.create_check_constraint(
                "ck_general_skill_catalog_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL AND owner_user_id IS NULL "
                "AND visibility_scope = 'platform_gallery' AND catalog_key IS NOT NULL) OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL "
                "AND visibility_scope <> 'platform_gallery')",
            )
        if "uq_general_skill_catalog_key" not in _unique_constraint_names(bind, "general_skills"):
            batch.create_unique_constraint("uq_general_skill_catalog_key", ["catalog_key"])

    revision_checks = _constraint_names(bind, "general_skill_revisions")
    revision_uniques = _unique_constraint_names(bind, "general_skill_revisions")
    with op.batch_alter_table("general_skill_revisions") as batch:
        if "ck_general_skill_revision_catalog_scope" not in revision_checks:
            batch.create_check_constraint(
                "ck_general_skill_revision_catalog_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL) OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL)",
            )
        if "uq_general_skill_revision_scope_number" not in revision_uniques:
            batch.create_unique_constraint(
                "uq_general_skill_revision_scope_number",
                ["catalog_scope", "skill_id", "revision_number"],
            )
        if "uq_general_skill_revision_scope_checksum" not in revision_uniques:
            batch.create_unique_constraint(
                "uq_general_skill_revision_scope_checksum",
                ["catalog_scope", "skill_id", "content_checksum"],
            )

    command_checks = _constraint_names(bind, "general_skill_catalog_commands")
    command_uniques = _unique_constraint_names(bind, "general_skill_catalog_commands")
    with op.batch_alter_table("general_skill_catalog_commands") as batch:
        if "ck_general_skill_catalog_command_scope" not in command_checks:
            batch.create_check_constraint(
                "ck_general_skill_catalog_command_scope",
                "(catalog_scope = 'platform' AND tenant_id IS NULL AND scope_key = 'platform') OR "
                "(catalog_scope = 'tenant' AND tenant_id IS NOT NULL AND scope_key = tenant_id)",
            )
        if "uq_general_skill_catalog_scope_command" not in command_uniques:
            batch.create_unique_constraint(
                "uq_general_skill_catalog_scope_command",
                ["scope_key", "command_type", "command_id"],
            )

    _create_index_if_missing(bind, "ix_general_skills_catalog_scope", "general_skills", ["catalog_scope"])
    _create_index_if_missing(
        bind,
        "ix_general_skill_catalog_scope_status",
        "general_skills",
        ["catalog_scope", "status", "catalog_key"],
    )
    _create_index_if_missing(
        bind,
        "ix_general_skill_revisions_catalog_scope",
        "general_skill_revisions",
        ["catalog_scope"],
    )
    _create_index_if_missing(
        bind,
        "ix_general_skill_catalog_commands_catalog_scope",
        "general_skill_catalog_commands",
        ["catalog_scope"],
    )


def _drop_scope_constraints_and_indexes(bind: sa.Connection) -> None:
    """回退辅助约束和索引，保持降级操作可验证且不删除主体数据。"""

    for name, table_name in (
        ("ix_general_skill_catalog_commands_catalog_scope", "general_skill_catalog_commands"),
        ("ix_general_skill_revisions_catalog_scope", "general_skill_revisions"),
        ("ix_general_skill_catalog_scope_status", "general_skills"),
        ("ix_general_skills_catalog_scope", "general_skills"),
    ):
        if name in _index_names(bind, table_name):
            op.drop_index(name, table_name=table_name)
    for table_name, constraints in (
        (
            "general_skill_catalog_commands",
            ("uq_general_skill_catalog_scope_command", "ck_general_skill_catalog_command_scope"),
        ),
        (
            "general_skill_revisions",
            (
                "uq_general_skill_revision_scope_checksum",
                "uq_general_skill_revision_scope_number",
                "ck_general_skill_revision_catalog_scope",
            ),
        ),
        ("general_skills", ("uq_general_skill_catalog_key", "ck_general_skill_catalog_scope")),
    ):
        existing_checks = _constraint_names(bind, table_name)
        existing_uniques = _unique_constraint_names(bind, table_name)
        with op.batch_alter_table(table_name) as batch:
            for name in constraints:
                if name in existing_checks:
                    batch.drop_constraint(name, type_="check")
                if name in existing_uniques:
                    batch.drop_constraint(name, type_="unique")
            if table_name == "general_skills" and "ck_general_skill_visibility_scope" in existing_checks:
                batch.drop_constraint("ck_general_skill_visibility_scope", type_="check")
                batch.create_check_constraint(
                    "ck_general_skill_visibility_scope",
                    "visibility_scope IN ('user_private', 'agent_private', 'tenant_gallery')",
                )


def _drop_scope_columns(bind: sa.Connection) -> None:
    """移除范围列并恢复旧租户必填语义。"""

    for table_name, columns in (
        ("general_skill_catalog_commands", ("scope_key", "catalog_scope")),
        ("general_skill_revisions", ("catalog_scope",)),
        ("general_skills", ("catalog_key", "catalog_scope")),
    ):
        existing_columns = _column_names(bind, table_name)
        columns_to_drop = [column_name for column_name in columns if column_name in existing_columns]
        if not columns_to_drop:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column_name in columns_to_drop:
                batch.drop_column(column_name)
    for table_name in ("general_skills", "general_skill_revisions", "general_skill_catalog_commands"):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                "tenant_id",
                existing_type=sa.String(128),
                nullable=False,
            )


def _create_index_if_missing(
    bind: sa.Connection,
    name: str,
    table_name: str,
    columns: list[str],
) -> None:
    """只在目标索引缺失时创建，兼容 MySQL DDL 中断后重试。"""

    if name not in _index_names(bind, table_name):
        op.create_index(name, table_name, columns)


def _json_value(value: object) -> object:
    """解析 SQLite 文本 JSON 与 MySQL 原生 JSON 的共同输入。"""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _json_dict(value: object) -> dict[str, object]:
    """把数据库 JSON 安全限制为字典，损坏数据由上层冲突校验发现。"""

    parsed = _json_value(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回指定表当前列名。"""

    return {str(item["name"]) for item in sa.inspect(bind).get_columns(table_name)}


def _constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回指定表具名检查约束。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(table_name)
        if item.get("name")
    }


def _unique_constraint_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回指定表具名唯一约束。"""

    return {
        str(item["name"])
        for item in sa.inspect(bind).get_unique_constraints(table_name)
        if item.get("name")
    }


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    """返回指定表普通索引名。"""

    return {str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)}
