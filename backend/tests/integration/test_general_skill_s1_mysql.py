"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : test_general_skill_s1_mysql.py
@CallChain  : pytest MySQL fixture → Alembic head → Skill import/confirm/cancel services
@Description: 验证 S1 在隔离 MySQL 8.4 上的迁移、复合键与并发终态契约。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier
from zipfile import ZipFile

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkillImportJob,
    GeneralSkillImportQuota,
    GeneralSkillRevision,
    ManagementAuditLog,
    Tenant,
    User,
    utc_now,
)
from app.general_skills.import_schema import (
    GeneralSkillImportConfirm,
    GeneralSkillImportJobCreate,
)
from app.general_skills.import_service import GeneralSkillImportError, GeneralSkillImportService
from app.general_skills.object_store import FileSystemSkillObjectStore


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


class _UnusedFetcher:
    """确保 deferred upload 的 worker 测试不会意外进入远程抓取。"""

    def fetch(self, *_args, **_kwargs):
        """若上传任务错误触发远程抓取则立即使测试失败。"""

        raise AssertionError("deferred upload must not use a remote fetcher")


def _upgrade(database_url: str, revision: str = "head") -> None:
    """把隔离数据库升级到指定 Alembic revision，默认使用当前 head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, revision)


def _package(name: str) -> bytes:
    """构造确定性单 Skill ZIP 供 MySQL 事务测试使用。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: MySQL production contract.\n---\n# {name}\n",
        )
    return payload.getvalue()


def _request(name: str) -> GeneralSkillImportJobCreate:
    """构造指向固定验收 Agent 的上传请求。"""

    return GeneralSkillImportJobCreate(
        tenant_id="tenant_skill_mysql",
        target_agent_id="agent_skill_mysql",
        source_kind="upload",
        filename=f"{name}.zip",
        content_base64=base64.b64encode(_package(name)).decode(),
    )


def _seed_identity(engine) -> None:
    """写入并发测试所需的最小租户、用户和本人数字员工。"""

    with Session(engine) as db:
        db.add(Tenant(id="tenant_skill_mysql", name="Skill MySQL Tenant"))
        db.add(
            User(
                id="user_skill_mysql",
                tenant_id="tenant_skill_mysql",
                username="skill-mysql-owner",
                role="member",
                password_hash="unused",
            )
        )
        db.add(
            AgentProfile(
                id="agent_skill_mysql",
                tenant_id="tenant_skill_mysql",
                name="Skill MySQL Agent",
                owner_user_id="user_skill_mysql",
            )
        )
        db.commit()


def test_s1_mysql_head_is_reentrant_and_uses_bounded_index_columns(
    mysql_database_url: str,
) -> None:
    """验证空库可重复升级到 0054，关键复合键列不会超过 utf8mb4 索引预算。"""

    _upgrade(mysql_database_url)
    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    inspector = inspect(engine)

    assert {
        "general_skill_revisions",
        "general_skill_import_jobs",
        "general_skill_dependencies",
        "general_skill_import_quotas",
        "general_skill_source_credentials",
    } <= set(inspector.get_table_names())
    revision_columns = {
        item["name"]: item["type"] for item in inspector.get_columns("general_skill_revisions")
    }
    assert revision_columns["tenant_id"].length == 128
    assert revision_columns["skill_id"].length == 128
    credential_columns = {
        item["name"]: item["type"]
        for item in inspector.get_columns("general_skill_source_credentials")
    }
    assert credential_columns["id"].length == 128
    assert credential_columns["allowed_host"].length == 253
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260812_0055"
        )
    engine.dispose()


def test_s1_mysql_historical_active_job_is_backfilled_once_before_head(
    mysql_database_url: str,
) -> None:
    """验证停在 0050 的活动作业升级后生成两级配额，重复升级不会重复计数。"""

    _upgrade(mysql_database_url, "20260812_0050")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO general_skill_import_jobs "
                "(id, tenant_id, owner_user_id, target_agent_id, source_kind, "
                "source_reference_redacted, credential_reference, raw_checksum, "
                "normalized_checksum, preview_checksum, status, attempt, parent_job_id, "
                "idempotency_key, quota_bytes, error_code, error_detail_redacted, "
                "staging_manifest_json, preview_json, installed_revision_ids_json, row_version, "
                "expires_at, created_at, updated_at, fetched_at, normalized_at, analyzed_at, "
                "confirmed_at, terminal_at) VALUES "
                "('gsjob_mysql_history', 'tenant_mysql_history', 'user_mysql_history', "
                "'agent_mysql_history', 'upload', 'history.zip', NULL, NULL, NULL, NULL, "
                "'awaiting_approval', 1, NULL, 'mysql-history-001', 321, NULL, NULL, "
                "JSON_ARRAY(), JSON_OBJECT(), JSON_ARRAY(), 1, "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), NULL, NULL, NULL, NULL, NULL)"
            )
        )
    _upgrade(mysql_database_url)
    _upgrade(mysql_database_url)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT scope_kind, scope_id, active_jobs, staged_bytes "
                "FROM general_skill_import_quotas ORDER BY scope_kind"
            )
        ).all()
        assert rows == [
            ("tenant", "tenant_mysql_history", 1, 321),
            ("user", "user_mysql_history", 1, 321),
        ]
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260812_0055"
        )
    engine.dispose()


def test_s1_mysql_workers_claim_one_deferred_job_once(
    mysql_database_url: str,
    tmp_path: Path,
) -> None:
    """验证两个 MySQL worker 并发扫描时只有一个 fencing lease 能处理同一作业。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    _seed_identity(engine)
    object_store = FileSystemSkillObjectStore(tmp_path / "mysql-worker-objects")
    with Session(engine) as setup_db:
        owner = setup_db.get(User, "user_skill_mysql")
        assert owner is not None
        queued = GeneralSkillImportService(setup_db, object_store).create_upload_job(
            _request("mysql-worker"),
            idempotency_key="mysql-worker-001",
            current_user=owner,
            defer_processing=True,
        )
    barrier = Barrier(2)

    def process_once(index: int) -> list[str]:
        """从独立事务同时扫描并尝试领取同一持久任务。"""

        with Session(engine) as db:
            barrier.wait(timeout=10)
            processed = GeneralSkillImportService(db, object_store).process_pending_jobs(
                worker_id=f"gsworker_mysql_{index}",
                fetcher=_UnusedFetcher(),
                now=utc_now(),
                lease_seconds=300,
            )
            return [item.status for item in processed]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(process_once, range(2)))
    assert sorted(outcomes, key=len) == [[], ["awaiting_approval"]]

    with Session(engine) as verify_db:
        completed = verify_db.get(GeneralSkillImportJob, queued.id)
        assert completed is not None
        assert completed.status == "awaiting_approval"
        assert completed.lease_token == 1
        assert completed.worker_id is None
        assert completed.lease_expires_at is None
        assert len(verify_db.exec(select(GeneralSkillRevision)).all()) == 0
        quotas = verify_db.exec(select(GeneralSkillImportQuota)).all()
        assert len(quotas) == 2
        assert all(quota.active_jobs == 1 for quota in quotas)
    engine.dispose()


def test_s1_mysql_concurrent_confirm_and_cancel_have_single_physical_effect(
    mysql_database_url: str,
    tmp_path: Path,
) -> None:
    """验证 MySQL 独立连接并发确认/取消只发布或结算一次且配额最终归零。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    _seed_identity(engine)
    object_store = FileSystemSkillObjectStore(tmp_path / "mysql-skill-objects")
    with Session(engine) as setup_db:
        owner = setup_db.get(User, "user_skill_mysql")
        assert owner is not None
        service = GeneralSkillImportService(setup_db, object_store)
        confirm_preview = service.create_upload_job(
            _request("mysql-confirm"),
            idempotency_key="mysql-confirm-001",
            current_user=owner,
        )
        cancel_preview = service.create_upload_job(
            _request("mysql-cancel"),
            idempotency_key="mysql-cancel-001",
            current_user=owner,
        )
    confirmation = GeneralSkillImportConfirm(
        preview_checksum=confirm_preview.preview_checksum or "",
        candidate_ids=[confirm_preview.candidates[0].candidate_id],
        expected_row_version=confirm_preview.row_version,
    )
    confirm_barrier = Barrier(2)

    def confirm_once() -> tuple[str, str]:
        """从独立 MySQL 连接同时确认同一预览。"""

        with Session(engine) as db:
            owner = db.get(User, "user_skill_mysql")
            assert owner is not None
            confirm_barrier.wait(timeout=10)
            try:
                result = GeneralSkillImportService(db, object_store).confirm_job(
                    confirm_preview.id,
                    confirmation,
                    current_user=owner,
                )
                return "success", result.status
            except GeneralSkillImportError as exc:
                return "error", exc.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirm_outcomes = list(executor.map(lambda _index: confirm_once(), range(2)))
    assert any(outcome == ("success", "installed") for outcome in confirm_outcomes)
    assert all(
        outcome in {("success", "installed"), ("error", "GENERAL_SKILL_STATE_CONFLICT")}
        for outcome in confirm_outcomes
    )

    cancel_barrier = Barrier(2)

    def cancel_once() -> str:
        """从独立 MySQL 连接同时取消同一待审核作业。"""

        with Session(engine) as db:
            owner = db.get(User, "user_skill_mysql")
            assert owner is not None
            cancel_barrier.wait(timeout=10)
            return GeneralSkillImportService(db, object_store).cancel_job(
                cancel_preview.id,
                expected_row_version=cancel_preview.row_version,
                current_user=owner,
            ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_outcomes = list(executor.map(lambda _index: cancel_once(), range(2)))
    assert cancel_outcomes == ["cancelled", "cancelled"]

    with Session(engine) as verify_db:
        assert len(verify_db.exec(select(GeneralSkillRevision)).all()) == 1
        assert len(verify_db.exec(select(AgentResourceBinding)).all()) == 1
        assert verify_db.get(GeneralSkillImportJob, confirm_preview.id).status == "installed"
        assert verify_db.get(GeneralSkillImportJob, cancel_preview.id).status == "cancelled"
        assert all(
            quota.active_jobs == 0 and quota.staged_bytes == 0
            for quota in verify_db.exec(select(GeneralSkillImportQuota)).all()
        )
        assert len(
            verify_db.exec(
                select(ManagementAuditLog).where(
                    ManagementAuditLog.action == "general_skill_import_cancelled"
                )
            ).all()
        ) == 1
    engine.dispose()
