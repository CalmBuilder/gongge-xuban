"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_sd6b_memory_status.py
@CallChain  : pytest → memories job status API → tenant/Agent scope → safe projection
@Description: 验证 SD-6B 任务状态只读投影的权限、稳定分页和敏感字段边界。
"""

from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.memories import list_memory_capture_jobs
from app.db.models import AgentProfile, MemoryCaptureJob, Tenant, User, utc_now


def _engine():
    """创建状态 API 专用的隔离 SQLite 引擎。"""

    return create_engine("sqlite://", poolclass=StaticPool)


def _seed_status_data(db: Session) -> tuple[User, User, AgentProfile]:
    """建立租户、所有者、普通成员和一个带敏感来源 manifest 的任务。"""

    tenant = Tenant(id="tenant_sd6b_status", name="SD6B status")
    owner = User(
        id="user_sd6b_status_owner",
        tenant_id=tenant.id,
        username="status-owner",
        password_hash="hash",
    )
    viewer = User(
        id="user_sd6b_status_viewer",
        tenant_id=tenant.id,
        username="status-viewer",
        password_hash="hash",
    )
    agent = AgentProfile(
        id="agent_sd6b_status",
        tenant_id=tenant.id,
        name="状态员工",
        owner_user_id=owner.id,
        status="active",
    )
    job = MemoryCaptureJob(
        id="memjob_sd6b_status",
        tenant_id=tenant.id,
        user_id=owner.id,
        agent_id=agent.id,
        agent_scope_key=agent.id,
        scope_id="scope_sd6b_status",
        source_message_id="source_secret_not_for_api",
        source_assistant_message_id="assistant_secret_not_for_api",
        source_sequence=3,
        source_manifest_json={"conversation": "secret content must not leave backend"},
        source_hash="a" * 64,
        model_config_id="model_secret_not_for_api",
        model_contract_hash="b" * 64,
        status="pending",
        lease_owner="internal-fence-owner",
        fencing_token=4,
        error_code="INTERNAL_PROVIDER_DETAIL",
        created_at=utc_now() - timedelta(minutes=1),
    )
    db.add_all([tenant, owner, viewer, agent, job])
    db.commit()
    return owner, viewer, agent


def test_memory_job_status_is_scoped_and_does_not_expose_source_or_fence() -> None:
    """任务状态按现有记忆权限投影，不能把原对话、模型或租约细节返回给页面。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        owner, viewer, agent = _seed_status_data(db)

        owner_page = list_memory_capture_jobs(
            tenant_id=owner.tenant_id,
            agent_id=agent.id,
            user_id=None,
            status=None,
            page=1,
            page_size=10,
            current_user=owner,
            db=db,
        )
        assert owner_page.total == 1
        payload = owner_page.items[0].model_dump()
        assert payload["status"] == "pending"
        assert payload["job_id"] == "memjob_sd6b_status"
        assert "source_message_id" not in payload
        assert "source_manifest" not in payload
        assert "model_config_id" not in payload
        assert "lease_owner" not in payload
        assert "fencing_token" not in payload
        assert "error_code" not in payload

        viewer_page = list_memory_capture_jobs(
            tenant_id=viewer.tenant_id,
            agent_id=None,
            user_id=None,
            status=None,
            page=1,
            page_size=10,
            current_user=viewer,
            db=db,
        )
        assert viewer_page.total == 0

        invalid_scope = list_memory_capture_jobs(
            tenant_id=owner.tenant_id,
            agent_id="   ",
            user_id=None,
            status=None,
            page=1,
            page_size=10,
            current_user=owner,
            db=db,
        )
        assert invalid_scope.total == 0


def test_memory_job_status_paginates_independently_from_memory_user_total() -> None:
    """任务分页有自己的 total，不改变记忆用户组分页语义。"""

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        owner, _, agent = _seed_status_data(db)
        for index in range(1, 3):
            db.add(
                MemoryCaptureJob(
                    id=f"memjob_sd6b_status_{index}",
                    tenant_id=owner.tenant_id,
                    user_id=owner.id,
                    agent_id=agent.id,
                    agent_scope_key=agent.id,
                    scope_id="scope_sd6b_status",
                    source_message_id=f"source_status_{index}",
                    source_sequence=index,
                    source_manifest_json={},
                    source_hash=f"{index:064d}",
                    model_config_id="model_status",
                    model_contract_hash="c" * 64,
                    status="completed" if index == 1 else "discarded",
                )
            )
        db.commit()

        page = list_memory_capture_jobs(
            tenant_id=owner.tenant_id,
            agent_id=agent.id,
            user_id=None,
            status=None,
            page=2,
            page_size=1,
            current_user=owner,
            db=db,
        )
        assert page.total == 3
        assert len(page.items) == 1
        assert page.page == 2
        assert page.page_size == 1

        completed = list_memory_capture_jobs(
            tenant_id=owner.tenant_id,
            agent_id=agent.id,
            user_id=None,
            status="succeeded",
            page=1,
            page_size=10,
            current_user=owner,
            db=db,
        )
        assert completed.total == 1
        assert completed.items[0].status == "succeeded"


def test_status_reason_is_whitelisted_and_retry_time_has_correct_state() -> None:
    """状态投影只输出白名单原因和合法重试时间，原始 provider 错误不能外泄。"""
    from app.api.memories import _memory_capture_job_read

    engine = _engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _seed_status_data(db)
        row = db.get(MemoryCaptureJob, 'memjob_sd6b_status')
        row.status = 'discarded'
        row.terminal_reason = 'INTERNAL_PROVIDER_DETAIL'
        row.next_attempt_at = utc_now()
        payload = _memory_capture_job_read(row).model_dump()
        assert payload['reason_code'] == 'capture_contract_changed'
        assert payload['next_attempt_at'] is None
        assert 'INTERNAL_PROVIDER_DETAIL' not in str(payload)
        row.status = 'retry'
        row.terminal_reason = 'retryable_failure'
        payload = _memory_capture_job_read(row).model_dump()
        assert payload['reason_code'] == 'retry_scheduled'
        assert payload['next_attempt_at'] == row.next_attempt_at
    engine.dispose()
