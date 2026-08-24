"""
@Time       : 2026/08/14 17:22
@Author     : zhanglp8181
@File       : test_input_purge_worker.py
@CallChain  : pytest → pending/过期PurgeJob → 恢复worker → 资源墓碑与在线blob
@Description: 验证附件销毁在请求进程崩溃后可接管，并以fencing幂等收敛在线副本。
"""

from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AttachmentUploadCleanupJob,
    InputResourcePurgeJob,
    ManagedInputResource,
    ResourceSessionBinding,
    utc_now,
)
from app.session.input_purge_worker import run_purge_maintenance_once
from app.session.managed_resources import ManagedInputResourceService


def test_expired_purge_job_is_fenced_and_recovered(tmp_path, monkeypatch) -> None:
    """旧worker租约过期后新worker只执行一次物理清理并保留成功作业事实。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.session.input_purge_worker.engine", engine)
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        resource, _ = ManagedInputResourceService(db).persist_upload(
            tenant_id="tenant-purge",
            owner_user_id="user-purge",
            agent_id="agent-purge",
            filename="delete.txt",
            content_type="text/plain",
            data=b"delete me",
        )
        db.add(
            ResourceSessionBinding(
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                resource_version=resource.version,
                owner_user_id=resource.owner_user_id,
                session_id="session-purge",
                agent_id="agent-purge",
            )
        )
        db.add(
            InputResourcePurgeJob(
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                resource_version=resource.version,
                session_id="session-purge",
                requested_by_user_id="user-purge",
                status="purging",
                attempt_no=1,
                lease_owner="dead-worker",
                fencing_token=1,
                lease_expires_at=utc_now() - timedelta(seconds=1),
            )
        )
        db.commit()
        resource_id = resource.id
        locator = tmp_path / "input-resources" / resource.storage_locator
        assert locator.exists()

    assert run_purge_maintenance_once() == 1

    with Session(engine) as db:
        job = db.exec(select(InputResourcePurgeJob)).one()
        resource = db.get(ManagedInputResource, resource_id)
        bindings = db.exec(select(ResourceSessionBinding)).all()
    assert job.status == "succeeded"
    assert job.attempt_no == 2
    assert job.fencing_token == 2
    assert job.lease_owner is None
    assert resource is not None and resource.destruction_status == "purged"
    assert bindings == []
    assert not locator.exists()


def test_background_worker_recovers_failed_upload_cleanup_job(tmp_path, monkeypatch) -> None:
    """后台维护入口可重新领取failed上传作业并删除blob、收敛资源墓碑。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.session.input_purge_worker.engine", engine)
    monkeypatch.setattr("app.session.managed_resources.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        service = ManagedInputResourceService(db)
        resource, _ = service.persist_upload(
            tenant_id="tenant-upload-cleanup",
            owner_user_id="user-upload-cleanup",
            filename="failed.txt",
            content_type="text/plain",
            data=b"recover me",
            upload_binding_id="binding-upload-cleanup",
        )
        db.commit()
        job = service.schedule_upload_failure_cleanup(
            [resource],
            tenant_id=resource.tenant_id,
            owner_user_id=resource.owner_user_id,
            upload_binding_id="binding-upload-cleanup",
        )
        job.status = "failed"
        db.add(job)
        db.commit()
        resource_id = resource.id
        locator = tmp_path / "input-resources" / resource.storage_locator
        assert locator.exists()

    assert run_purge_maintenance_once() == 1

    with Session(engine) as db:
        job = db.exec(select(AttachmentUploadCleanupJob)).one()
        resource = db.get(ManagedInputResource, resource_id)
    assert job.status == "succeeded"
    assert job.attempt_no == 1
    assert job.fencing_token == 1
    assert resource is not None and resource.destruction_status == "purged"
    assert resource.access_status == "revoked"
    assert not locator.exists()
