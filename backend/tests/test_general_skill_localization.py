"""
@Time       : 2026/08/30 10:45
@Author     : zhanglp8181
@File       : test_general_skill_localization.py
@CallChain  : pytest → 内置 Skill 快照 → revision 中文展示记录 → 目录 API 投影
@Description: 验证中文展示与精确 revision 绑定、checksum 门禁、幂等同步和迁移链契约。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.api.general_skill_catalog import _filter_rows, _item_read
from app.api.general_skills import general_skill_read
from app.db.models import (
    GeneralSkill,
    GeneralSkillRevision,
    GeneralSkillRevisionLocalization,
    Tenant,
    User,
)
from app.general_skills.builtin_catalog import (
    BuiltinSkillCatalogService,
    load_builtin_skill_catalog,
)
from app.general_skills.localization import (
    BUILTIN_SKILL_LOCALIZATIONS,
    is_usable_localization,
    reconcile_builtin_skill_localizations,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _catalog_db() -> Session:
    """创建仅包含平台 Skill 目录所需模型的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_localization", name="Localization Tenant"))
    db.add(
        User(
            id="user_localization_admin",
            tenant_id="tenant_localization",
            username="localization-admin",
            role="admin",
            password_hash="unused",
        )
    )
    db.commit()
    return db


def test_builtin_localizations_are_platform_owned_and_runtime_english_is_unchanged() -> None:
    """验证 37 条摘要属于平台且不会替换英文 revision 正文。"""

    db = _catalog_db()
    catalog = load_builtin_skill_catalog()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_localization",
        command_id="localization-import-v1",
        actor_user_id="user_localization_admin",
    )

    localizations = db.exec(select(GeneralSkillRevisionLocalization)).all()
    assert len(localizations) == len(BUILTIN_SKILL_LOCALIZATIONS) == 37
    assert {item.catalog_scope for item in localizations} == {"platform"}
    assert {item.tenant_id for item in localizations} == {None}
    assert {item.locale for item in localizations} == {"zh-CN"}
    assert {item.translation_status for item in localizations} == {"verified"}
    assert all("运行时" not in item.explanation_markdown for item in localizations)

    skills = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.catalog_scope == "platform",
            GeneralSkill.tenant_id.is_(None),
        )
    ).all()
    assert len(skills) == 37
    catalog_slugs = {item.slug for item in catalog.items}
    assert {item.slug for item in BUILTIN_SKILL_LOCALIZATIONS} == catalog_slugs
    for skill in skills:
        revision = db.exec(
            select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)
        ).one()
        localization = db.exec(
            select(GeneralSkillRevisionLocalization).where(
                GeneralSkillRevisionLocalization.revision_id == revision.id
            )
        ).one()
        assert revision.normalized_skill_markdown == skill.skill_markdown
        assert is_usable_localization(localization, revision)
        assert localization.source_content_checksum == revision.content_checksum
        assert not any("\u4e00" <= char <= "\u9fff" for char in revision.normalized_skill_markdown)
        assert skill.slug in catalog_slugs


def test_builtin_localization_sync_is_idempotent_and_catalog_projection_is_chinese() -> None:
    """验证重复同步不产生重复记录，并让目录列表使用已审核中文摘要。"""

    db = _catalog_db()
    catalog = load_builtin_skill_catalog()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_localization",
        command_id="localization-import-v2",
        actor_user_id="user_localization_admin",
    )
    before = db.exec(select(GeneralSkillRevisionLocalization)).all()
    result = reconcile_builtin_skill_localizations(
        db,
        catalog_items=catalog.items,
        actor_user_id="user_localization_admin",
    )
    after = db.exec(select(GeneralSkillRevisionLocalization)).all()

    assert result.created_count == 0
    assert len(after) == len(before) == 37
    code_review = db.exec(select(GeneralSkill).where(GeneralSkill.slug == "code-review")).one()
    projection = _item_read(db, code_review)
    assert projection.name_zh == "代码审查"
    assert projection.description_zh
    assert projection.localization_status == "verified"
    general_projection = general_skill_read(code_review, db=db)
    assert general_projection.name_zh == "代码审查"
    assert general_projection.description_zh == projection.description_zh
    assert general_projection.skill_markdown == code_review.skill_markdown
    assert _filter_rows(
        db,
        [code_review],
        search="代码审查",
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
    ) == [code_review]


def test_changed_source_checksum_hides_stale_chinese_projection() -> None:
    """验证中文记录与当前 revision checksum 不一致时目录 API 回退英文。"""

    db = _catalog_db()
    catalog = load_builtin_skill_catalog()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_localization",
        command_id="localization-import-v3",
        actor_user_id="user_localization_admin",
    )
    localization = db.exec(select(GeneralSkillRevisionLocalization)).first()
    assert localization is not None
    localization.source_content_checksum = "changed-source-checksum"
    db.add(localization)
    db.commit()

    result = reconcile_builtin_skill_localizations(
        db,
        catalog_items=catalog.items,
        actor_user_id="user_localization_admin",
    )
    db.refresh(localization)
    assert result.stale_count == 1
    assert localization.translation_status == "stale"
    assert not is_usable_localization(
        localization,
        db.get(GeneralSkillRevision, localization.revision_id),
    )


def test_localization_migration_creates_table_and_is_replayable(tmp_path) -> None:
    """验证 0076 在已到 0075 的 SQLite 上创建表并可重复执行。"""

    database_url = f"sqlite:///{tmp_path / 'skill-localization-migration.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260830_0075')"))

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    columns = {item["name"] for item in inspect(engine).get_columns("general_skill_revision_localizations")}
    assert {
        "revision_id",
        "locale",
        "localized_name",
        "explanation_markdown",
        "source_content_checksum",
        "translation_checksum",
    } <= columns
