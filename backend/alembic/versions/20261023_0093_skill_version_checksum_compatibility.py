"""
@Time       : 2026/10/23
@Author     : zhanglp8181
@File       : 20261023_0093_skill_version_checksum_compatibility.py
@CallChain  : Alembic → 已发布 SkillVersion 兼容校验 → 分支同步/Runtime 恢复
@Description: 记录历史编译校验值与当前编译值的受控兼容关系，不改写正文和版本身份。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0093"
down_revision: str | None = "20261002_0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_object(value: object) -> dict[str, object]:
    """把数据库 JSON 值解析为对象，拒绝损坏的历史兼容记录。"""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SKILL_VERSION_COMPATIBILITY_JSON_INVALID") from exc
        if isinstance(decoded, dict):
            return decoded
    if value is None:
        return {}
    raise RuntimeError("SKILL_VERSION_COMPATIBILITY_JSON_INVALID")


def _compatibility_payload(
    existing: object,
    *,
    previous_checksum: str,
    current_checksum: str,
    content_checksum: str,
) -> dict[str, object]:
    """合并当前编译值和旧值，保持多次编译器升级的兼容链可追溯。"""

    payload = _json_object(existing)
    legacy_checksums = payload.get("legacy_checksums")
    ordered = [
        item for item in legacy_checksums
        if isinstance(item, str) and item
    ] if isinstance(legacy_checksums, list) else []
    for checksum in (previous_checksum,):
        if checksum and checksum != current_checksum and checksum not in ordered:
            ordered.append(checksum)
    existing_current = payload.get("current_checksum")
    if isinstance(existing_current, str) and existing_current:
        if existing_current != current_checksum and existing_current not in ordered:
            ordered.append(existing_current)
    return {
        "contract": "published_skill_compiler_compatibility_v1",
        "current_checksum": current_checksum,
        "legacy_checksums": ordered,
        "content_checksum": content_checksum,
        "migration_revision": revision,
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "reason": "历史发布快照正文未变且可重新编译，仅派生编译校验值随编译器演进发生变化。",
    }


def upgrade() -> None:
    """补充历史编译校验兼容记录，正文、版本号和既有实例身份保持不变。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_versions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("skill_versions")}
    if "compiled_definition_compatibility_json" not in columns:
        op.add_column(
            "skill_versions",
            sa.Column("compiled_definition_compatibility_json", sa.JSON(), nullable=True),
        )

    # 延迟导入生产编译器，避免 Alembic 解析脚本时初始化应用数据库。
    from app.sop_runtime import SopCompilationError, compile_legacy_skill_card
    from app.sop_runtime.versioning import skill_content_checksum

    table = sa.Table("skill_versions", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(table).where(table.c.status == "published")
    ).mappings().all()
    for row in rows:
        content = _json_object(row["content_json"])
        content_checksum = skill_content_checksum(content)
        stored_content_checksum = row.get("content_checksum")
        if stored_content_checksum and stored_content_checksum != content_checksum:
            raise RuntimeError(
                "SKILL_VERSION_CONTENT_CHECKSUM_INVALID: " + str(row["id"])
            )
        try:
            compiled = compile_legacy_skill_card(content)
        except SopCompilationError as exc:
            raise RuntimeError(
                "SKILL_VERSION_COMPILATION_INVALID: " + str(row["id"])
            ) from exc

        values: dict[str, object] = {}
        if not stored_content_checksum:
            values["content_checksum"] = content_checksum
        previous_checksum = row.get("compiled_definition_checksum")
        if not previous_checksum:
            values["compiled_definition_checksum"] = compiled.checksum
        elif previous_checksum != compiled.checksum:
            values["compiled_definition_compatibility_json"] = _compatibility_payload(
                row.get("compiled_definition_compatibility_json"),
                previous_checksum=str(previous_checksum),
                current_checksum=compiled.checksum,
                content_checksum=content_checksum,
            )
        if values:
            bind.execute(
                table.update().where(table.c.id == row["id"]).values(**values)
            )


def downgrade() -> None:
    """存在兼容记录时拒绝降级，避免丢失历史实例可恢复的校验值。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_versions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("skill_versions")}
    if "compiled_definition_compatibility_json" not in columns:
        return
    count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM skill_versions "
            "WHERE compiled_definition_compatibility_json IS NOT NULL"
        )
    ).scalar_one()
    if count:
        raise RuntimeError("SKILL_VERSION_COMPATIBILITY_PRESENT")
    op.drop_column("skill_versions", "compiled_definition_compatibility_json")
