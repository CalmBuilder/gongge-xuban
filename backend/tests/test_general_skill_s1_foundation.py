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


def test_0051_migration_backfills_active_import_quota_without_double_counting(tmp_path) -> None:
    """验证升级窗口中的待确认作业会形成两级计数，迁移重入不会重复占额。"""

    database_url = f"sqlite:///{tmp_path / 'skill-import-quota.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    now = utc_now()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0050')"))
        connection.execute(
            text(
                "CREATE TABLE general_skill_import_jobs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "owner_user_id VARCHAR NOT NULL, target_agent_id VARCHAR NOT NULL, "
                "source_kind VARCHAR NOT NULL, status VARCHAR NOT NULL, attempt INTEGER NOT NULL, "
                "idempotency_key VARCHAR NOT NULL, quota_bytes INTEGER NOT NULL, "
                "staging_manifest_json JSON NOT NULL, preview_json JSON NOT NULL, "
                "installed_revision_ids_json JSON NOT NULL, row_version INTEGER NOT NULL, "
                "expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO general_skill_import_jobs "
                "(id, tenant_id, owner_user_id, target_agent_id, source_kind, status, attempt, "
                "idempotency_key, quota_bytes, staging_manifest_json, preview_json, "
                "installed_revision_ids_json, row_version, expires_at, created_at, updated_at) "
                "VALUES ('gsjob_backfill0001', 'tenant_a', 'user_a', 'agent_a', 'upload', "
                "'awaiting_approval', 1, 'quota-backfill-001', 321, '[]', '{}', '[]', 7, "
                ":expires_at, :created_at, :updated_at)"
            ),
            {
                "expires_at": now + timedelta(hours=1),
                "created_at": now,
                "updated_at": now,
            },
        )
    command.upgrade(config, "20260812_0051")
    command.upgrade(config, "20260812_0051")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT scope_kind, scope_id, active_jobs, staged_bytes "
                "FROM general_skill_import_quotas ORDER BY scope_kind"
            )
        ).all()
    assert rows == [
        ("tenant", "tenant_a", 1, 321),
        ("user", "user_a", 1, 321),
    ]


def test_0052_migration_constraint_survives_later_heads(tmp_path) -> None:
    """验证从 0051 升至当前 head 后，线性重试约束仍存在且重复升级安全。"""

    database_url = f"sqlite:///{tmp_path / 'skill-import-retry.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0051')"))
        connection.execute(
            text(
                "CREATE TABLE general_skill_import_jobs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "parent_job_id VARCHAR, attempt INTEGER NOT NULL)"
            )
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    constraints = {
        str(item.get("name"))
        for item in inspect(engine).get_unique_constraints("general_skill_import_jobs")
    }

    assert "uq_general_skill_import_retry_attempt" in constraints
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260813_0058"
        )
    engine.dispose()


def test_0053_migration_adds_idempotent_worker_lease_contract(tmp_path) -> None:
    """验证 0053 可重入增加 worker lease、fencing token、索引和非负约束。"""

    database_url = f"sqlite:///{tmp_path / 'skill-import-worker-lease.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0052')"))
        connection.execute(
            text(
                "CREATE TABLE general_skill_import_jobs ("
                "id VARCHAR PRIMARY KEY, worker_id VARCHAR, lease_expires_at DATETIME, "
                "lease_token INTEGER NOT NULL DEFAULT 0)"
            )
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    inspector = inspect(engine)

    assert {item["name"] for item in inspector.get_columns("general_skill_import_jobs")} >= {
        "worker_id",
        "lease_expires_at",
        "lease_token",
    }
    assert {item["name"] for item in inspector.get_indexes("general_skill_import_jobs")} >= {
        "ix_general_skill_import_jobs_worker_id",
        "ix_general_skill_import_jobs_lease_expires_at",
    }
    assert "ck_general_skill_import_lease_token" in {
        item["name"] for item in inspector.get_check_constraints("general_skill_import_jobs")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260813_0058"
        )
    engine.dispose()


def test_0053_downgrade_refuses_active_worker_lease(tmp_path) -> None:
    """验证存在活动 worker 时降级会失败，清除 lease 后才允许移除队列字段。"""

    database_url = f"sqlite:///{tmp_path / 'skill-import-worker-downgrade.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0052')"))
        connection.execute(
            text(
                "CREATE TABLE general_skill_import_jobs ("
                "id VARCHAR PRIMARY KEY, worker_id VARCHAR, lease_expires_at DATETIME, "
                "lease_token INTEGER NOT NULL DEFAULT 0)"
            )
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO general_skill_import_jobs "
                "(id, worker_id, lease_expires_at, lease_token) "
                "VALUES ('job-active', 'worker-a', '2026-08-13 00:00:00', 1)"
            )
        )

    with pytest.raises(RuntimeError, match="active general skill import worker leases"):
        command.downgrade(config, "20260812_0052")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE general_skill_import_jobs SET worker_id = NULL, lease_expires_at = NULL"
            )
        )
    command.downgrade(config, "20260812_0052")
    assert {item["name"] for item in inspect(engine).get_columns("general_skill_import_jobs")} == {
        "id"
    }
    engine.dispose()


def test_0054_migration_creates_reentrant_user_source_credential_profile(tmp_path) -> None:
    """验证 0054 档案表可重复升级，并具备用户、状态、版本和来源约束。"""

    database_url = f"sqlite:///{tmp_path / 'skill-source-credential.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260812_0053')"))
    command.upgrade(config, "20260812_0054")
    command.upgrade(config, "20260812_0054")
    inspector = inspect(engine)

    assert inspector.has_table("general_skill_source_credentials")
    assert "ix_general_skill_source_credential_owner_status" in {
        item["name"] for item in inspector.get_indexes("general_skill_source_credentials")
    }
    assert {
        "ck_general_skill_source_credential_kind",
        "ck_general_skill_source_credential_status",
        "ck_general_skill_source_secret_revision",
        "ck_general_skill_source_row_version",
    } <= {
        item["name"]
        for item in inspector.get_check_constraints("general_skill_source_credentials")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260812_0054"
        )
    command.downgrade(config, "20260812_0053")
    assert not inspect(engine).has_table("general_skill_source_credentials")
    engine.dispose()
