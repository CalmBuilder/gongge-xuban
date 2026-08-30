"""
@Time       : 2026/08/29 19:55
@Author     : zhanglp8181
@File       : test_general_skill_catalog_mysql.py
@CallChain  : pytest MySQL fixture → Alembic head → Skill 目录导入/审核 → Agent 绑定
@Description: 验证 Skill 广场候选审核和能力分身安装在 MySQL 8.4 上与 SQLite 使用同一事务契约。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentProfile,
    GeneralSkill,
    GeneralSkillRevision,
    GeneralSkillRevisionLocalization,
    Tenant,
    User,
)
from app.general_skills.builtin_catalog import BuiltinSkillCatalogService
from app.general_skills.catalog_governance import GeneralSkillCatalogGovernanceService


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把隔离 MySQL 数据库升级到当前唯一迁移 head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def test_mysql_catalog_review_and_capability_install_share_sqlite_contract(
    mysql_database_url: str,
) -> None:
    """在 MySQL 8.4 验证候选导入、原子审核、广场发布和能力分身安装闭环。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            tenant_id = "tenant_mysql_catalog"
            admin = User(
                id="user_mysql_catalog_admin",
                tenant_id=tenant_id,
                username="mysql-catalog-admin",
                role="admin",
                password_hash="unused",
            )
            member = User(
                id="user_mysql_catalog_member",
                tenant_id=tenant_id,
                username="mysql-catalog-member",
                role="member",
                password_hash="unused",
            )
            second_tenant = Tenant(id="tenant_mysql_catalog_second", name="MySQL Catalog Second")
            second_admin = User(
                id="user_mysql_catalog_second_admin",
                tenant_id=second_tenant.id,
                username="mysql-catalog-second-admin",
                role="admin",
                password_hash="unused",
            )
            db.add(Tenant(id=tenant_id, name="MySQL Catalog"))
            db.add(admin)
            db.add(member)
            db.add(second_tenant)
            db.add(second_admin)
            db.add(
                AgentProfile(
                    id="agent_mysql_catalog_overall",
                    tenant_id=tenant_id,
                    name="MySQL Catalog Overall",
                    is_overall=True,
                    status="active",
                )
            )
            db.add(
                AgentProfile(
                    id="agent_mysql_catalog_avatar",
                    tenant_id=tenant_id,
                    name="MySQL Catalog Avatar",
                    owner_user_id=member.id,
                    status="active",
                )
            )
            db.commit()

            imported = BuiltinSkillCatalogService(db).import_snapshot(
                tenant_id=tenant_id,
                command_id="mysql-catalog-import-v1",
                actor_user_id=admin.id,
            )
            assert imported.items
            skill = db.exec(
                select(GeneralSkill)
                .where(
                    GeneralSkill.catalog_scope == "platform",
                    GeneralSkill.tenant_id.is_(None),
                )
                .order_by(GeneralSkill.id)
            ).first()
            assert skill is not None
            assert len(
                db.exec(
                    select(GeneralSkill).where(
                        GeneralSkill.catalog_scope == "platform",
                        GeneralSkill.tenant_id.is_(None),
                    )
                ).all()
            ) == 37
            assert not db.exec(
                select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id)
            ).all()
            localizations = db.exec(select(GeneralSkillRevisionLocalization)).all()
            assert len(localizations) == 37
            assert {item.catalog_scope for item in localizations} == {"platform"}
            assert {item.tenant_id for item in localizations} == {None}
            assert {item.translation_status for item in localizations} == {"verified"}
            imported_from_second_tenant = BuiltinSkillCatalogService(db).import_snapshot(
                tenant_id=second_tenant.id,
                command_id="mysql-catalog-import-v2",
                actor_user_id=second_admin.id,
            )
            assert imported_from_second_tenant.existing_count == 37
            assert len(
                db.exec(
                    select(GeneralSkill).where(
                        GeneralSkill.catalog_scope == "platform",
                        GeneralSkill.tenant_id.is_(None),
                    )
                ).all()
            ) == 37
            revision = db.exec(
                select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)
            ).one()

            review = GeneralSkillCatalogGovernanceService(db).review(
                tenant_id=tenant_id,
                command_id="mysql-catalog-review-v1",
                actor_user_id=admin.id,
                items=[
                    {
                        "skill_id": skill.id,
                        "decision": "approve",
                        "expected_skill_row_version": skill.row_version,
                        "expected_revision_row_version": revision.row_version,
                        "review_note": "MySQL catalog contract verified",
                    }
                ],
            )
            assert review.approved_count == 1

            db.expire_all()
            skill = db.get(GeneralSkill, skill.id)
            revision = db.get(GeneralSkillRevision, revision.id)
            assert skill is not None and revision is not None
            assert skill.status == "published"
            assert revision.status == "published"

            first_binding = GeneralSkillCatalogGovernanceService(db).bind(
                current_user=member,
                skill_id=skill.id,
                agent_id="agent_mysql_catalog_avatar",
                mode="install",
                revision_policy="pinned",
                pinned_revision_id=revision.id,
                invocation_policy="model_allowed",
            )
            replay_binding = GeneralSkillCatalogGovernanceService(db).bind(
                current_user=member,
                skill_id=skill.id,
                agent_id="agent_mysql_catalog_avatar",
                mode="install",
                revision_policy="pinned",
                pinned_revision_id=revision.id,
                invocation_policy="model_allowed",
            )
            assert first_binding.action == "created"
            assert replay_binding.action == "unchanged"
            assert first_binding.binding.id == replay_binding.binding.id
            assert first_binding.binding.metadata_json["managed_catalog"] is True
    finally:
        engine.dispose()
