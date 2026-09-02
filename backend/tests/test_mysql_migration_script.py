"""
@Time       : 2026/09/02 09:20
@Author     : zhanglp8181
@File       : test_mysql_migration_script.py
@CallChain  : pytest → migrate_mysql.main → MigrationStatus/迁移命令边界
@Description: 验证 MySQL 迁移脚本的检查、提示、SQLite 跳过和升级入口。
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT_DIR / "scripts" / "migrate_mysql.py"


def _load_migration_script():
    """加载迁移脚本模块，避免测试通过安装包路径获取项目源码。"""

    module_name = "gongge_xuban_mysql_migration_script"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_mysql_check_returns_distinct_code_when_schema_is_stale(capsys, monkeypatch) -> None:
    """MySQL 当前版本落后时只检查模式应返回专用退出码和迁移命令。"""

    migration = _load_migration_script()
    monkeypatch.setattr(
        migration,
        "inspect_database",
        lambda: migration.MigrationStatus(
            backend="mysql",
            database_url="mysql+pymysql://user:***@127.0.0.1:3306/app",
            current_revision="20260830_0077",
            head_revision="20260901_0078",
        ),
    )

    assert migration.main(["--check"]) == migration.EXIT_MIGRATION_REQUIRED
    output = capsys.readouterr().out
    assert "数据库需要迁移" in output
    assert "../scripts/migrate_mysql.py" in output
    assert "20260830_0077" in output
    assert "20260901_0078" in output


def test_mysql_check_accepts_current_schema(capsys, monkeypatch) -> None:
    """MySQL 当前版本等于 head 时检查应返回成功。"""

    migration = _load_migration_script()
    monkeypatch.setattr(
        migration,
        "inspect_database",
        lambda: migration.MigrationStatus(
            backend="mysql",
            database_url="mysql+pymysql://user:***@127.0.0.1:3306/app",
            current_revision="20260901_0078",
            head_revision="20260901_0078",
        ),
    )

    assert migration.main(["--check"]) == migration.EXIT_OK
    assert "检查通过" in capsys.readouterr().out


def test_sqlite_is_skipped_by_check_but_rejected_by_upgrade(capsys, monkeypatch) -> None:
    """SQLite 检查应明确跳过，误用升级命令时应返回失败而不修改数据库。"""

    migration = _load_migration_script()
    monkeypatch.setattr(
        migration,
        "inspect_database",
        lambda: migration.MigrationStatus(
            backend="sqlite",
            database_url="sqlite:///gongge_xuban.db",
            current_revision=None,
            head_revision=None,
        ),
    )

    assert migration.main(["--check"]) == migration.EXIT_OK
    assert migration.main([]) == migration.EXIT_CHECK_FAILED
    assert "SQLite" in capsys.readouterr().out


def test_upgrade_path_runs_once_and_rechecks_head(monkeypatch) -> None:
    """主动迁移命令应调用升级入口并以迁移后的 head 状态作为成功依据。"""

    migration = _load_migration_script()
    stale = migration.MigrationStatus("mysql", "mysql://user:***@host/db", "old", "new")
    current = migration.MigrationStatus("mysql", "mysql://user:***@host/db", "new", "new")
    states = iter((stale, current))
    upgrade_calls: list[str] = []

    monkeypatch.setattr(migration, "inspect_database", lambda: next(states))

    class FakeCommand:
        """记录 Alembic upgrade 目标而不连接测试数据库。"""

        @staticmethod
        def upgrade(_config, revision: str) -> None:
            """记录要求升级到唯一 head。"""

            upgrade_calls.append(revision)

    monkeypatch.setattr(migration, "_alembic_config", lambda: object())
    original_alembic = sys.modules.get("alembic")
    fake_alembic = ModuleType("alembic")
    setattr(fake_alembic, "command", FakeCommand)
    sys.modules["alembic"] = fake_alembic
    try:
        result = migration.upgrade_mysql()
    finally:
        if original_alembic is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = original_alembic

    assert result == current
    assert upgrade_calls == ["head"]
