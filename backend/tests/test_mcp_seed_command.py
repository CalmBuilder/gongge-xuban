from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import paths
from app.db import seed
from app.db.models import MCPServer


def _seed_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_stdio_mcp_command_is_bundled_python_when_frozen(monkeypatch, tmp_path) -> None:
    bundled = tmp_path / "runtime" / "bin" / "python3"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(seed, "_stdio_mcp_python", lambda: str(bundled))
    with _seed_session() as db:
        seed._seed_mcp_servers(db)
        db.commit()
        row = db.exec(select(MCPServer).where(MCPServer.name == "stdio_demo")).first()
        assert row is not None
        assert row.command == str(bundled)


def test_stdio_mcp_command_is_portable_in_dev(monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    with _seed_session() as db:
        seed._seed_mcp_servers(db)
        db.commit()
        row = db.exec(select(MCPServer).where(MCPServer.name == "stdio_demo")).first()
        assert row.command == "python3"
        assert row.args_json == ["mock_servers/mcp_stdio_server.py"]
