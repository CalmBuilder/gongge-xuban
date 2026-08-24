"""
@Time       : 2026/08/14 12:20
@Author     : zhanglp8181
@File       : test_artifact_renderer_worker.py
@CallChain  : pytest → renderer maintenance worker → expired lease/retry → Artifact publish
@Description: 验证后台Renderer能恢复崩溃作业，并把不可恢复输入在有界次数后送入死信。
"""

from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    ExecutionArtifact,
    ExecutionResult,
    SopInstance,
    SopNodeExecution,
    User,
    utc_now,
)
from app.dynamic_tasks.artifact_renderer import DOCX_MIME, ArtifactRendererService
from app.dynamic_tasks.artifact_renderer_worker import run_renderer_maintenance_once
from app.dynamic_tasks.artifacts import ArtifactService


def _engine(tmp_path: Path):  # noqa: ANN202
    """创建允许后台维护使用独立Session的文件SQLite。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'renderer-worker.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(db: Session) -> tuple[SopInstance, SopNodeExecution, ExecutionResult]:
    """持久化可由独立worker恢复的用户、Execution、answer节点和验证结果。"""

    user = User(
        id="renderer-worker-user",
        tenant_id="renderer-worker-tenant",
        username="renderer-worker-user",
        password_hash="test",
        membership_status="active",
    )
    instance = SopInstance(
        id="renderer-worker-execution",
        tenant_id=user.tenant_id,
        session_id="renderer-worker-session",
        initiator_user_id=user.id,
        kind="sop",
        skill_id="renderer-worker-sop",
        skill_version_id="renderer-worker-sop-v1",
        skill_version="1.0.0",
        definition_checksum="b" * 64,
        status="running",
        active_slot_key="renderer-worker-execution",
    )
    node = SopNodeExecution(
        id="renderer-worker-node",
        tenant_id=user.tenant_id,
        instance_id=instance.id,
        node_id="answer",
        step_key="answer",
        status="running",
    )
    result = ExecutionResult(
        id="renderer-worker-result",
        tenant_id=user.tenant_id,
        execution_id=instance.id,
        status="verified",
        result_json={"markdown": "# 恢复报告\n关键值 100"},
        verification_json={"passed": True},
        checksum="a" * 64,
    )
    db.add_all([user, instance, node, result])
    db.commit()
    return instance, node, result


def test_background_worker_recovers_expired_renderer_job(tmp_path: Path, monkeypatch) -> None:
    """模拟claim提交后进程死亡，后台worker必须换租约并发布同一确定性Artifact。"""

    engine = _engine(tmp_path)
    artifact_root = tmp_path / "execution-artifacts"
    monkeypatch.setattr("app.dynamic_tasks.artifact_renderer_worker.engine", engine)
    monkeypatch.setattr("app.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        instance, node, result = _seed(db)
        service = ArtifactRendererService(db)
        job, _ = service.ensure_job(
            instance=instance,
            result_id=result.id,
            result_checksum=result.checksum,
            source_node=node,
            artifact_key="recovered",
            filename="recovered.docx",
            mime_type=DOCX_MIME,
            required=True,
        )
        service.claim(job, worker_id="dead-worker")
        job.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(job)
        db.commit()
        job_id = job.id

    assert run_renderer_maintenance_once(worker_id="recovery-worker") == 2

    with Session(engine) as db:
        job = db.get(type(job), job_id)
        assert job is not None and job.status == "ready" and job.attempt_no == 2
        artifact, content = ArtifactService(db, storage_root=artifact_root).resolve(
            job.artifact_id or "",
            tenant_id="renderer-worker-tenant",
            actor_user_id="renderer-worker-user",
        )
        assert artifact.content_checksum == job.staged_checksum
        assert content.startswith(b"PK")


def test_background_worker_dead_letters_invalid_result_after_three_attempts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """结果身份损坏时只追加有界尝试，第三次后死信且绝不生成Artifact。"""

    engine = _engine(tmp_path)
    monkeypatch.setattr("app.dynamic_tasks.artifact_renderer_worker.engine", engine)
    monkeypatch.setattr("app.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        instance, node, result = _seed(db)
        service = ArtifactRendererService(db)
        job, _ = service.ensure_job(
            instance=instance,
            result_id=result.id,
            result_checksum=result.checksum,
            source_node=node,
            artifact_key="invalid",
            filename="invalid.docx",
            mime_type=DOCX_MIME,
            required=True,
        )
        db.commit()
        result.status = "rejected"
        db.add(result)
        db.commit()
        job_id = job.id

    assert [run_renderer_maintenance_once(worker_id="renderer-worker") for _ in range(3)] == [1, 1, 1]

    with Session(engine) as db:
        job = db.get(type(job), job_id)
        assert job is not None
        assert job.status == "dead_letter"
        assert job.attempt_no == 3
        assert job.error_code == "ARTIFACT_RENDER_RESULT_INVALID"
        assert db.get(ExecutionArtifact, job.artifact_id or "") is None


def test_expired_third_renderer_attempt_is_dead_lettered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """第三次物理渲染期间崩溃时必须直接死信，不能留下永远无法领取的retry_wait。"""

    engine = _engine(tmp_path)
    monkeypatch.setattr("app.dynamic_tasks.artifact_renderer_worker.engine", engine)
    monkeypatch.setattr("app.paths.user_data_dir", lambda: tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        instance, node, result = _seed(db)
        service = ArtifactRendererService(db)
        job, _ = service.ensure_job(
            instance=instance,
            result_id=result.id,
            result_checksum=result.checksum,
            source_node=node,
            artifact_key="exhausted-worker",
            filename="exhausted-worker.docx",
            mime_type=DOCX_MIME,
            required=True,
        )
        job.status = "rendering"
        job.attempt_no = 3
        job.lease_owner = "dead-third-worker"
        job.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.add(job)
        db.commit()
        job_id = job.id

    assert run_renderer_maintenance_once(worker_id="recovery-worker") == 1

    with Session(engine) as db:
        exhausted = db.get(type(job), job_id)
        assert exhausted is not None
        assert exhausted.status == "dead_letter"
        assert exhausted.attempt_no == 3
        assert exhausted.lease_owner is None
        assert exhausted.retry_at is None
        assert exhausted.error_code == "ARTIFACT_RENDER_WORKER_LOST"
        assert db.get(ExecutionArtifact, exhausted.artifact_id or "") is None
