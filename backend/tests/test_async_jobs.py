"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_async_jobs.py
@CallChain  : pytest → AgentLoop → 持久记忆捕获任务/唤醒通知
@Description: 验证通用异步队列及 SD-6B 记忆登记不再依赖内存写入旁路。
"""

from threading import Event
from time import sleep

from app.async_jobs import AsyncJobQueue
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureJob,
    Message,
    ModelConfig,
    User,
)
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def test_async_job_queue_runs_job_without_calling_inline() -> None:
    queue = AsyncJobQueue(max_workers=1)
    started = Event()
    release = Event()

    def job_func() -> None:
        started.set()
        release.wait(1)

    try:
        job = queue.enqueue("test.job", job_func)

        assert job.status in {"queued", "running"}
        assert started.wait(1)
        release.set()
        assert _eventually_succeeded(queue, job.id)
    finally:
        release.set()
        queue.shutdown()


def test_agent_loop_registers_memory_capture_before_commit_and_wakes_after_commit() -> None:
    """显式选择原模型后在完成事务落任务，提交成功才唤醒，避免来源或通知丢失。"""

    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            id="user_async_memory",
            tenant_id="tenant_async_memory",
            username="async-memory",
            password_hash="x",
        )
        agent = AgentProfile(
            id="agent_async_memory",
            tenant_id=user.tenant_id,
            name="Async Memory Agent",
            owner_user_id=user.id,
            status="active",
        )
        session = ChatSession(
            id="session_async_memory",
            tenant_id=user.tenant_id,
            user_id=user.id,
            agent_id=agent.id,
        )
        model = ModelConfig(
            id="model_async_memory",
            tenant_id=user.tenant_id,
            name="Async Memory Model",
            api_key_encrypted="encrypted",
            model="demo",
        )
        db.add_all([user, agent, session, model])
        db.commit()
        loop = AgentLoop(db)
        request = ChatTurnRequest(
            tenant_id=user.tenant_id,
            session_id=session.id,
            agent_id=agent.id,
            user_id=user.id,
            model_config_id=model.id,
            message="我叫 hm",
        )
        user_message, _ = loop._append_user_input_message(request, session.id)
        assert user_message.metadata_json["memory_capture_source"]["model_config_id"] == model.id
        assistant = Message(
            tenant_id=user.tenant_id,
            session_id=session.id,
            role="assistant",
            content="记住了。",
            metadata_json={"turn_id": user_message.id},
        )
        db.add(assistant)
        db.flush()

        result = loop._enqueue_memory_capture(
            request,
            session,
            StepAgentResult(),
            None,
            model,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            commit=False,
        )

        assert result and result[0]["job_name"] == "memory.capture_turn"
        assert db.exec(select(MemoryCaptureJob)).one().status == "pending"
        db.commit()
        loop._flush_memory_capture_wakeups()
        assert db.exec(select(MemoryCaptureJob)).one().source_message_id == user_message.id


def _eventually_succeeded(queue: AsyncJobQueue, job_id: str) -> bool:
    for _ in range(20):
        job = queue.get(job_id)
        if job and job.status == "succeeded":
            return True
        sleep(0.01)
    return False


class _FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
