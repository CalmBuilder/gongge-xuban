from pathlib import Path

from sqlalchemy import create_engine, text

from app import paths
from app.db.sqlite_legacy import (
    _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID,
    _migrate_default_model_output_limit,
)
from app.db.factory import normalize_database_url
from app.config import Settings


def test_default_database_uses_gongge_name(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None, public_mock_api_key="test-key").database_url == (
        "sqlite:///./gongge_xuban.db"
    )


def test_relative_sqlite_url_resolves_under_backend_dir() -> None:
    backend_dir = Path(__file__).resolve().parents[1]

    assert normalize_database_url("sqlite:///./gongge_xuban.db") == (
        f"sqlite:///{backend_dir / 'gongge_xuban.db'}"
    )


def test_absolute_and_memory_sqlite_urls_are_preserved() -> None:
    assert normalize_database_url("sqlite:////tmp/example.db") == "sqlite:////tmp/example.db"
    assert normalize_database_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_frozen_relative_sqlite_resolves_under_user_data_dir(monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    # 与实现一致：_normalize_database_url 返回 .resolve() 后的路径，期望值同样 resolve
    expected = (paths.user_data_dir() / "gongge_xuban.db").resolve()
    assert normalize_database_url("sqlite:///./gongge_xuban.db") == f"sqlite:///{expected}"


def test_frozen_sqlite_honors_data_dir_override(monkeypatch, tmp_path) -> None:
    # 直接断言 _normalize_database_url 返回值（不 importlib.reload 全局 engine）。
    # 期望值加 .resolve()：实现里有 .resolve()，Mac 上 /var→/private/var，
    # 且不依赖 pytest 版本对 tmp_path 是否预 resolve。
    monkeypatch.setenv("GONGGE_XUBAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    result = normalize_database_url("sqlite:///./gongge_xuban.db")
    expected = (tmp_path / "gongge_xuban.db").resolve()
    assert result == f"sqlite:///{expected}"


def test_default_model_output_limit_migration_is_scoped_and_runs_once(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_configs (
                    id VARCHAR PRIMARY KEY,
                    is_default INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO model_configs (id, is_default, max_output_tokens)
                VALUES
                    ('default_legacy', 1, 2048),
                    ('default_custom', 1, 4096),
                    ('secondary_legacy', 0, 2048)
                """
            )
        )

        _migrate_default_model_output_limit(conn, {"model_configs"})

        rows = dict(
            conn.execute(
                text("SELECT id, max_output_tokens FROM model_configs ORDER BY id")
            ).all()
        )
        assert rows == {
            "default_custom": 4096,
            "default_legacy": 8192,
            "secondary_legacy": 2048,
        }
        assert conn.execute(
            text("SELECT id FROM app_data_migrations WHERE id = :id"),
            {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
        ).scalar_one() == _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID

        conn.execute(
            text(
                "UPDATE model_configs SET max_output_tokens = 2048 "
                "WHERE id = 'default_legacy'"
            )
        )
        _migrate_default_model_output_limit(conn, {"model_configs"})

        assert conn.execute(
            text(
                "SELECT max_output_tokens FROM model_configs "
                "WHERE id = 'default_legacy'"
            )
        ).scalar_one() == 2048
