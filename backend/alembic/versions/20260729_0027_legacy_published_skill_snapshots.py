"""
@Time       : 2026/07/29 14:35
@Author     : zhanglp8181
@File       : 20260729_0027_legacy_published_skill_snapshots.py
@CallChain  : Alembic upgrade → 历史演示 SOP 来源校验 → SkillVersion 不可变快照
@Description: 仅为内容指纹可证明的四个历史已发布种子 SOP 幂等补齐版本快照。

Revision ID: 20260729_0027
Revises: 20260729_0026
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision: str = "20260729_0027"
down_revision: str | None = "20260729_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PROVEN_SEED_SNAPSHOTS: dict[str, dict[str, object]] = {
    "after_sales_exchange": {
        "content_checksums": {
            "7e4d8c496b667c9ce8aff17b35673d81b3d2ecb37aaaec846489a7e5f7402d67",
            "caf5cfac246f319558e8a6736e37b20cba610cba8efbf5067ece10cb5d6bb79e",
        },
        "compiled_checksum": (
            "9423fb29457169cde72994f2d1b745106201704cfb977a75f80b6227d2357b60"
        ),
    },
    "after_sales_refund": {
        "content_checksums": {
            "3d50abd27ab43a4b92af2fc16617e25fffd16ce11d41595f783043900d6cef43",
            "8ac5f5452f5d7d713b4aab29cd4c2069669b1ffc02ab8b40a5393f327358caf1",
        },
        "compiled_checksum": (
            "2128486636b6b7170c5e9b8578fdcceaeb37aaeb100e5e1f48d939d632405264"
        ),
    },
    "skill_price_compare_001": {
        "content_checksums": {
            "8bb60d4d63c018aa60bb1c023a075f1e5ac6a207fc80b9a378f88ba902f11271"
        },
        "compiled_checksum": (
            "8f352ae15c49596e89aeca2b8c3406ff51e6b2ea512328db13cfe94120a11f59"
        ),
    },
    "skill_purchase_001": {
        "content_checksums": {
            "03cd5b138f57ddc926fb48aabe72f581f61646156542b56ecfff2aab296a6f6c",
            "361f933bb718d975196cd58eb05bd2d9896eb92cc8bbeeea15a31e44c11f66da",
        },
        "compiled_checksum": (
            "28c1c972af5e14c37c88eda4c03bfa0dd20c08a1a75563881380188a95439571"
        ),
    },
}


def upgrade() -> None:
    """仅在租户、版本、发布状态和内容指纹全部匹配时补写历史不可变快照。"""

    bind = op.get_bind()
    metadata = sa.MetaData()
    skills = sa.Table("skills", metadata, autoload_with=bind)
    skill_versions = sa.Table("skill_versions", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(skills).where(
            skills.c.tenant_id == "tenant_demo",
            skills.c.skill_id.in_(tuple(_PROVEN_SEED_SNAPSHOTS)),
            skills.c.version == "1.0.0",
            skills.c.status == "published",
        )
    ).mappings()
    for row in rows:
        evidence = _PROVEN_SEED_SNAPSHOTS[str(row["skill_id"])]
        content = _json_object(row["content_json"])
        content_checksum = _content_checksum(content)
        if content_checksum not in evidence["content_checksums"]:
            continue
        existing_id = bind.execute(
            sa.select(skill_versions.c.id).where(
                skill_versions.c.tenant_id == row["tenant_id"],
                skill_versions.c.skill_id == row["skill_id"],
                skill_versions.c.version == row["version"],
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            continue
        bind.execute(
            skill_versions.insert().values(
                id=f"skillver_m55b_{row['skill_id']}_1_0_0",
                tenant_id=row["tenant_id"],
                skill_id=row["skill_id"],
                version=row["version"],
                name=row["name"],
                business_domain=row["business_domain"],
                description=row["description"],
                content_json=content,
                status="published",
                content_checksum=content_checksum,
                compiled_definition_checksum=evidence["compiled_checksum"],
                meta_model_version=1,
                source_schema_version=2,
                published_at=row["created_at"],
                derived_from_version_id=None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )


def downgrade() -> None:
    """保留已经证明的发布事实；降级只回退代码版本，不删除不可变历史快照。"""


def _json_object(value: object) -> dict[str, object]:
    """把双方言 JSON 返回值规范为对象，拒绝非对象历史内容。"""

    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, dict) else {}


def _content_checksum(content: dict[str, object]) -> str:
    """按现有版本服务的稳定 JSON 规则计算发布内容指纹。"""

    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
