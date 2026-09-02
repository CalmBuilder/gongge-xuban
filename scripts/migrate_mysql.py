#!/usr/bin/env python3
"""
@Time       : 2026/09/02 09:20
@Author     : zhanglp8181
@File       : migrate_mysql.py
@CallChain  : app.sh/管理员 → migrate_mysql.py → Settings/SQLAlchemy/Alembic → MySQL
@Description: 检查并执行 MySQL Alembic 数据库迁移，避免应用启动时反复重启等待。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
ALEMBIC_CONFIG_PATH = BACKEND_DIR / "alembic.ini"
EXIT_OK = 0
EXIT_MIGRATION_REQUIRED = 2
EXIT_CHECK_FAILED = 3


def _prepare_backend_import_path() -> None:
    """把后端源码目录置于导入路径首位，保证脚本可从任意工作目录运行。"""

    backend_path = str(BACKEND_DIR)
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)


@dataclass(frozen=True)
class MigrationStatus:
    """描述一次数据库迁移检查的数据库类型、当前版本和目标版本。"""

    backend: str
    database_url: str
    current_revision: str | None
    head_revision: str | None

    @property
    def needs_migration(self) -> bool:
        """判断数据库是否需要执行 Alembic 迁移。"""

        return self.backend == "mysql" and self.current_revision != self.head_revision


def inspect_database() -> MigrationStatus:
    """读取当前配置对应数据库的迁移状态，不修改任何数据库数据。

    返回：
        MigrationStatus: 数据库方言、隐藏密码后的连接地址、当前修订和迁移头部。
    异常：
        连接异常或 Alembic 配置异常向上抛出，由命令行入口转换为安全提示。
    """

    _prepare_backend_import_path()
    from app.db import engine
    from app.db.migrations import current_revision, head_revision

    backend = engine.url.get_backend_name()
    safe_url = engine.url.render_as_string(hide_password=True)
    if backend != "mysql":
        return MigrationStatus(backend, safe_url, None, None)
    return MigrationStatus(
        backend=backend,
        database_url=safe_url,
        current_revision=current_revision(engine),
        head_revision=head_revision(ALEMBIC_CONFIG_PATH),
    )


def _alembic_config():
    """构造绑定当前应用数据库配置的 Alembic 配置对象。"""

    _prepare_backend_import_path()
    from alembic.config import Config
    from app.config import get_settings

    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = get_settings().database_url
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def upgrade_mysql() -> MigrationStatus:
    """将当前 MySQL 数据库升级到迁移脚本的唯一 head 并再次校验结果。

    返回：
        MigrationStatus: 迁移完成后的数据库状态。
    异常：
        RuntimeError: 当前配置不是 MySQL，或迁移后版本仍未到达 head。
    """

    status = inspect_database()
    if status.backend != "mysql":
        raise RuntimeError(f"当前数据库为 {status.backend}，本脚本只处理 MySQL。")
    if not status.needs_migration:
        return status

    from alembic import command

    command.upgrade(_alembic_config(), "head")
    verified = inspect_database()
    if verified.needs_migration:
        raise RuntimeError(
            "Alembic 迁移命令已返回，但数据库版本仍未到达目标版本 "
            f"{verified.head_revision}。"
        )
    return verified


def _revision_label(revision: str | None) -> str:
    """把空修订版本转换为面向管理员的稳定显示文本。"""

    return revision or "<none>"


def _print_check_result(status: MigrationStatus) -> None:
    """输出不泄露密码的迁移检查结果和必要的人工操作命令。"""

    if status.backend != "mysql":
        print(f"当前数据库为 {status.backend}，跳过 MySQL Alembic 检查。SQLite 由应用启动初始化。")
        return
    if status.needs_migration:
        print(f"检测到 MySQL 数据库需要迁移：{status.database_url}")
        print(f"当前版本：{_revision_label(status.current_revision)}")
        print(f"目标版本：{_revision_label(status.head_revision)}")
        print("请执行：")
        print("  cd backend")
        print("  .venv/bin/python ../scripts/migrate_mysql.py")
        return
    print(
        "数据库迁移检查通过："
        f"{status.database_url} 当前版本 {_revision_label(status.current_revision)}，"
        f"目标版本 {_revision_label(status.head_revision)}。"
    )


def _safe_error_message(error: Exception) -> str:
    """将迁移异常转换为不携带连接密码的简洁诊断信息。"""

    if type(error).__name__ in {"DatabaseConnectionError", "SchemaNotCurrentError"}:
        return str(error)
    return type(error).__name__


def main(argv: list[str] | None = None) -> int:
    """执行 MySQL 迁移检查或管理员主动发起的迁移升级。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查当前版本，不执行迁移；数据库落后时返回退出码 2",
    )
    args = parser.parse_args(argv)

    try:
        status = inspect_database()
        if args.check:
            _print_check_result(status)
            return EXIT_MIGRATION_REQUIRED if status.needs_migration else EXIT_OK
        if status.backend != "mysql":
            _print_check_result(status)
            return EXIT_CHECK_FAILED
        if not status.needs_migration:
            _print_check_result(status)
            return EXIT_OK
        print(
            "开始执行 MySQL Alembic 迁移："
            f"{_revision_label(status.current_revision)} → {_revision_label(status.head_revision)}"
        )
        migrated = upgrade_mysql()
        print(
            "MySQL 数据库迁移完成："
            f"当前版本已是 {_revision_label(migrated.current_revision)}。"
        )
        return EXIT_OK
    except Exception as error:
        print(f"MySQL 数据库迁移处理失败：{_safe_error_message(error)}", file=sys.stderr)
        return EXIT_CHECK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
