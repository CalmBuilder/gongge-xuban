from threading import Event
from time import sleep
from types import SimpleNamespace

from app.async_jobs import AsyncJobQueue
from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession, ModelConfig
from app.session.session_schema import ChatTurnRequest, StepAgentResult


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


def test_agent_loop_enqueues_memory_capture_without_running_it_inline(monkeypatch) -> None:
    captured = {}

    def fake_enqueue_memory_capture(*args):  # noqa: ANN002
        captured["args"] = args
        return SimpleNamespace(id="job_memory_1", name="memory.capture_turn")

    monkeypatch.setattr("app.core.agent_loop.enqueue_memory_capture", fake_enqueue_memory_capture)

    loop = object.__new__(AgentLoop)
    loop.events = _FakeEvents()
    loop.db = _FakeDb()

    result = loop._enqueue_memory_capture(
        ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="我叫hm"),
        ChatSession(id="session_test", tenant_id="tenant_demo", user_id="user_demo"),
        StepAgentResult(),
        None,
        ModelConfig(
            id="model_test",
            tenant_id="tenant_demo",
            name="demo",
            api_key_encrypted="encrypted",
            model="demo",
        ),
    )

    assert result == [{"job_id": "job_memory_1", "job_name": "memory.capture_turn"}]
    assert captured["args"][1] == "session_test"
    assert captured["args"][4] == "model_test"
    assert loop.events.records[0][2] == "async_job_enqueued"
    assert loop.db.commits == 1


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
