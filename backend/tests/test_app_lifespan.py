from fastapi.testclient import TestClient

from app import main


def test_app_uses_lifespan_without_deprecated_event_handlers() -> None:
    assert main.app.router.on_startup == []
    assert main.app.router.on_shutdown == []


def test_lifespan_preserves_startup_and_shutdown_order(monkeypatch) -> None:
    events: list[str] = []

    class FakeSession:
        def __init__(self, engine) -> None:
            events.append("session")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(main, "init_db", lambda: events.append("init_db"))
    monkeypatch.setattr(main, "Session", FakeSession)
    monkeypatch.setattr(main, "seed_demo_data", lambda db: events.append("seed"))
    monkeypatch.setattr(main, "start_background_worker", lambda: events.append("start"))
    monkeypatch.setattr(main, "stop_background_worker", lambda: events.append("stop"))
    monkeypatch.setattr(main, "shutdown_async_jobs", lambda: events.append("shutdown"))

    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200
        assert events == ["init_db", "session", "seed", "start"]

    assert events == ["init_db", "session", "seed", "start", "stop", "shutdown"]
