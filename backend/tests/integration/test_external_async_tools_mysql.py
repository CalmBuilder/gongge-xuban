"""
@Time       : 2026/09/28
@Author     : zhanglp8181
@File       : test_external_async_tools_mysql.py
@CallChain  : pytest mysql → Alembic head → Operation/Attempt/inbox → 外部回执事实
@Description: 在隔离 MySQL 验证 provider_task 等待、回执去重和终态结算与 SQLite 语义一致。
"""

from pathlib import Path
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, create_engine, select

from app.db.models import ExternalTaskEventInbox, SopOperationAttempt, Tenant, utc_now
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.external_identity import external_task_callback_token
from app.sop_runtime.external_tasks import (
    apply_pending_external_task_events,
    ingest_external_task_event,
    poll_due_external_operations,
)


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把随机 MySQL 库迁移到当前 head，确保测试不绕过 Alembic。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def test_mysql_provider_task_waiting_and_inbox_recovery(
    mysql_database_url: str,
) -> None:
    """MySQL 上受理只进入等待，回执 append-once 后结算原 Operation。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as db:
        tenant_id = "tenant_sd4_mysql_external"
        db.add(Tenant(id=tenant_id, name="SD4 MySQL 外部任务隔离租户"))
        db.commit()

        store = SopExecutionStore(db)
        instance, created = store.start_instance(
            tenant_id=tenant_id,
            session_id="session_sd4_mysql_external",
            skill_id="skill_sd4_mysql_external",
            skill_version_id="skillver_sd4_mysql_external",
            skill_version="1.0.0",
            definition_checksum="b" * 64,
            start_node_id="submit",
        )
        assert created is True
        with store.owned(instance, worker_id="sd4-mysql-external"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            operation, created = store.prepare_operation(
                instance,
                execution,
                operation_name="orders.submit",
                request={"order_id": "mysql-A-1"},
                effect_kind="external_write",
                capability_snapshot={
                    "contract": {"async_strategy": "provider_task"},
                },
            )
            assert created is True
            store.start_operation(operation)
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-mysql-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://provider.invalid/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()

        db.refresh(operation)
        assert operation.status == "waiting_external"
        assert operation.external_task_id == "provider-mysql-42"
        assert operation.external_observation_revision == 1
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == operation.id)
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "waiting_external"

        callback_token = external_task_callback_token(operation.remote_idempotency_key or "")
        first = ingest_external_task_event(
            db,
            tenant_id=tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-mysql-42",
            provider_event_id="provider-mysql-event-1",
            provider_status="completed",
            observation_revision=2,
            payload={"result": {"order_id": "mysql-A-1", "status": "done"}},
            callback_token=callback_token,
        )
        duplicate = ingest_external_task_event(
            db,
            tenant_id=tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-mysql-42",
            provider_event_id="provider-mysql-event-1",
            provider_status="completed",
            observation_revision=2,
            payload={"result": {"order_id": "mysql-A-1", "status": "done"}},
            callback_token=callback_token,
        )
        assert duplicate == first
        db.commit()

        assert apply_pending_external_task_events(db) == 1
        db.refresh(operation)
        db.refresh(instance)
        inbox = db.exec(
            select(ExternalTaskEventInbox).where(
                ExternalTaskEventInbox.provider_event_id == "provider-mysql-event-1"
            )
        ).one()

        assert operation.status == "succeeded"
        assert operation.result_json == {
            "data": {"order_id": "mysql-A-1", "status": "done"}
        }
        assert instance.status == "waiting"
        assert inbox.status == "applied"
        assert inbox.attempt_count == 1


def test_mysql_provider_poll_does_not_hold_database_transaction(
    mysql_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证真实 MySQL 查询事务在 provider GET 开始前已经结束。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    transaction_states: list[bool] = []
    session_holder: list[Session] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        """记录 GET 外呼时 SQLAlchemy 会话是否仍处于事务中。"""

        transaction_states.append(session_holder[0].in_transaction())
        return httpx.Response(
            200,
            json={"status": "working"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.sop_runtime.external_tasks.httpx.get", fake_get)
    with Session(engine) as db:
        session_holder.append(db)
        tenant_id = "tenant_sd4_mysql_poll_txn"
        db.add(Tenant(id=tenant_id, name="SD4 MySQL 轮询事务隔离租户"))
        db.commit()

        store = SopExecutionStore(db)
        instance, created = store.start_instance(
            tenant_id=tenant_id,
            session_id="session_sd4_mysql_poll_txn",
            skill_id="skill_sd4_mysql_poll_txn",
            skill_version_id="skillver_sd4_mysql_poll_txn",
            skill_version="1.0.0",
            definition_checksum="c" * 64,
            start_node_id="submit",
        )
        assert created is True
        with store.owned(instance, worker_id="sd4-mysql-poll-txn"):
            execution = store.enter_node(instance, "submit", input_snapshot={})
            operation, created = store.prepare_operation(
                instance,
                execution,
                operation_name="orders.poll_transaction",
                request={"order_id": "mysql-poll-transaction"},
                effect_kind="external_write",
                capability_snapshot={
                    "contract": {
                        "async_strategy": "provider_task",
                        "status_field": "status",
                        "result_field": "result",
                        "poll_interval_seconds": 1,
                    },
                },
            )
            assert created is True
            store.start_operation(operation)
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-mysql-poll-txn",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
                poll_interval_seconds=1,
            )
            operation.external_next_poll_at = utc_now() - timedelta(seconds=1)
            db.add(operation)
        db.commit()

        assert poll_due_external_operations(db) == 1
        db.refresh(operation)

        assert operation.status == "waiting_external"
        assert operation.external_status == "working"
        assert transaction_states == [False]
