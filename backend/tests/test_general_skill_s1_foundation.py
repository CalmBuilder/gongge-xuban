"""
@Time       : 2026/08/11 23:25
@Author     : zhanglp8181
@File       : test_general_skill_s1_foundation.py
@CallChain  : pytest → Skill S1 models/lifecycle/Alembic → SQLite
@Description: 验证不可变修订、导入作业、乐观锁状态机及 expand 迁移的首个生产切片。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import GeneralSkillImportJob, GeneralSkillRevision, utc_now
from app.general_skills.lifecycle import (
    GeneralSkillLifecycleError,
    ImportJobStatus,
    RevisionStatus,
    transition_import_job,
    transition_revision,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _memory_engine():
    """创建跨 Session 复用同一连接的 SQLite 约束测试引擎。"""

    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _revision(*, checksum: str = "a" * 64, number: int = 1) -> GeneralSkillRevision:
    """构造满足数据库必填字段的不可变修订样本。"""

    return GeneralSkillRevision(
        tenant_id="tenant_a",
        skill_id="genskill_a",
        revision_number=number,
        content_checksum=checksum,
        manifest_checksum="b" * 64,
        normalized_skill_markdown="# Skill\n",
        created_by="user_a",
    )


def _import_job() -> GeneralSkillImportJob:
    """构造处于 created 状态且尚未占用暂存配额的导入作业。"""

    return GeneralSkillImportJob(
        tenant_id="tenant_a",
        owner_user_id="user_a",
        target_agent_id="agent_a",
        source_kind="upload",
        idempotency_key="idem_a",
        expires_at=utc_now() + timedelta(hours=24),
    )


def test_revision_constraints_reject_duplicate_identity_and_unknown_status() -> None:
    """证明同 Skill 的修订号与内容 checksum 均不可重复且状态枚举封闭。"""

    engine = _memory_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(_revision())
        db.commit()
        db.add(_revision(checksum="c" * 64))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        invalid = _revision(checksum="d" * 64, number=2)
        invalid.status = "future-state"
        db.add(invalid)
        with pytest.raises(IntegrityError):
            db.commit()


def test_import_job_unique_attempt_and_closed_enums_are_database_enforced() -> None:
    """证明幂等 attempt、来源与状态不能被调用方用自由字符串绕过。"""

    engine = _memory_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(_import_job())
        db.commit()
        db.add(_import_job())
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        invalid = _import_job()
        invalid.idempotency_key = "idem_b"
        invalid.source_kind = "local-path"
        db.add(invalid)
        with pytest.raises(IntegrityError):
            db.commit()


def test_import_job_requires_ordered_progress_and_never_revives_terminal_state() -> None:
    """验证主链逐步推进、任意非终态可失败以及终态不可复活。"""

    job = _import_job()
    for target in (
        ImportJobStatus.FETCHING,
        ImportJobStatus.FETCHED,
        ImportJobStatus.NORMALIZING,
        ImportJobStatus.NORMALIZED,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.AWAITING_APPROVAL,
        ImportJobStatus.CONFIRMING,
        ImportJobStatus.INSTALLED,
    ):
        transition_import_job(job, target, expected_row_version=job.row_version)
    assert job.status == "installed"
    assert job.confirmed_at is not None
    assert job.terminal_at is not None
    with pytest.raises(GeneralSkillLifecycleError, match="illegal import job transition"):
        transition_import_job(
            job,
            ImportJobStatus.FETCHING,
            expected_row_version=job.row_version,
        )


def test_import_job_rejects_skipped_stage_and_stale_row_version() -> None:
    """验证不能跳过预览阶段且旧写者无法覆盖新状态。"""

    job = _import_job()
    with pytest.raises(GeneralSkillLifecycleError, match="illegal import job transition"):
        transition_import_job(
            job,
            ImportJobStatus.AWAITING_APPROVAL,
            expected_row_version=job.row_version,
        )
    transition_import_job(job, ImportJobStatus.FETCHING, expected_row_version=1)
    with pytest.raises(GeneralSkillLifecycleError, match="row version"):
        transition_import_job(job, ImportJobStatus.FETCHED, expected_row_version=1)


def test_import_job_failure_records_only_redacted_terminal_detail() -> None:
    """验证失败路径设置稳定错误码、脱敏摘要和终态时间。"""

    job = _import_job()
    transition_import_job(
        job,
        ImportJobStatus.FAILED,
        expected_row_version=1,
        error_code="GENERAL_SKILL_PACKAGE_INVALID",
        error_detail_redacted="archive member path is invalid",
    )
    assert job.status == "failed"
    assert job.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert job.error_detail_redacted == "archive member path is invalid"
    assert job.terminal_at is not None


def test_revision_lifecycle_sets_timestamps_and_rejects_revival() -> None:
    """验证审核、发布、撤销时间可审计且 revoked 修订永不复活。"""

    revision = _revision()
    transition_revision(revision, RevisionStatus.REVIEWING, expected_row_version=1)
    transition_revision(revision, RevisionStatus.PUBLISHED, expected_row_version=2)
    assert revision.published_at is not None
    transition_revision(revision, RevisionStatus.REVOKED, expected_row_version=3)
    assert revision.revoked_at is not None
    with pytest.raises(GeneralSkillLifecycleError, match="illegal revision transition"):
        transition_revision(revision, RevisionStatus.REVIEWING, expected_row_version=4)


def test_0049_migration_expands_and_safely_downgrades_empty_foundation(tmp_path) -> None:
    """验证 0049 可重入、回填旧行、创建约束，并只在无新事实时允许降级。"""

    database_url = f"sqlite:///{tmp_path / 'skill-revision-foundation.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260811_0048')"))
        connection.execute(
            text(
                "CREATE TABLE general_skills (id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_resource_bindings "
                "(id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO general_skills (id, tenant_id, status) "
                "VALUES ('skill_a', 'tenant_a', 'published')"
            )
        )

    command.upgrade(config, "20260811_0049")
    command.upgrade(config, "20260811_0049")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {"general_skill_revisions", "general_skill_import_jobs"}.issubset(
            inspector.get_table_names()
        )
        row = connection.execute(
            text("SELECT visibility_scope, row_version FROM general_skills")
        ).one()
        assert row == ("tenant_gallery", 1)
        assert "ck_general_skill_revision_status" in {
            item["name"]
            for item in inspector.get_check_constraints("general_skill_revisions")
        }
        assert "uq_general_skill_import_attempt" in {
            item["name"]
            for item in inspector.get_unique_constraints("general_skill_import_jobs")
        }

    command.downgrade(config, "20260811_0048")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "general_skill_revisions" not in inspector.get_table_names()
        assert "owner_user_id" not in {
            item["name"] for item in inspector.get_columns("general_skills")
        }


def test_0049_migration_refuses_downgrade_when_revision_exists(tmp_path) -> None:
    """验证已产生不可变修订后禁止 down migration 丢失生产事实。"""

    database_url = f"sqlite:///{tmp_path / 'skill-revision-downgrade.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260811_0048')"))
        connection.execute(
            text(
                "CREATE TABLE general_skills (id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_resource_bindings "
                "(id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL)"
            )
        )
    command.upgrade(config, "20260811_0049")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO general_skill_revisions "
                "(id, tenant_id, skill_id, revision_number, content_checksum, manifest_checksum, "
                "normalized_skill_markdown, parsed_metadata_json, resource_manifest_json, "
                "requested_capabilities_json, source_snapshot_json, status, created_by, row_version, "
                "created_at) VALUES ('gsrev_a', 'tenant_a', 'skill_a', 1, :content_checksum, "
                ":manifest_checksum, '# Skill', '{}', '[]', '{}', '{}', 'draft', 'user_a', 1, "
                "'2026-08-11 00:00:00')"
            ),
            {"content_checksum": "a" * 64, "manifest_checksum": "b" * 64},
        )
    with pytest.raises(RuntimeError, match="general_skill_revisions"):
        command.downgrade(config, "20260811_0048")
