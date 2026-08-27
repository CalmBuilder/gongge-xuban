"""
@Time       : 2026/08/25
@Author     : zhanglp8181
@File       : run_live_attachment_mysql_browser_regression.py
@CallChain  : root MySQL admin → 临时库/账号 → LIVE Chromium Skill+附件 → 自动清理
@Description: 在隔离MySQL 8.4库上复验一条真实F4.2浏览器场景，禁止把root账号或临时资源泄露到证据。
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import subprocess
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ENV = ROOT / "backend" / ".env"
ROOT_ENV = ROOT / ".env"
LIVE_LAUNCHER = ROOT / "scripts" / "run_live_attachment_browser_regression.py"


def main() -> int:
    """创建隔离MySQL资源，运行一条真实浏览器Skill附件场景并在finally清理。"""

    admin_url = _admin_url()
    database = f"gongge_xuban_live_{os.getpid()}_{secrets.token_hex(4)}"
    username = f"st_live_{os.getpid()}_{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(24)
    with _temporary_mysql(admin_url, database=database, username=username, password=password):
        app_url = URL.create("mysql+pymysql", username=username, password=password,
                             host=make_url(admin_url).host or "127.0.0.1",
                             port=make_url(admin_url).port or 3306, database=database,
                             query={"charset": "utf8mb4"})
        environment = os.environ.copy()
        environment["FULLSTACK_E2E_DATABASE_URL"] = app_url.render_as_string(hide_password=False)
        environment["FULLSTACK_E2E_PORT"] = os.environ.get("FULLSTACK_E2E_PORT", "39241")
        environment["FULLSTACK_E2E_RUNTIME_DIR"] = os.environ.get(
            "FULLSTACK_E2E_RUNTIME_DIR", f"/tmp/gongge-live-mysql-{os.getpid()}"
        )
        migration_environment = environment.copy()
        migration_environment["DATABASE_URL"] = app_url.render_as_string(hide_password=False)
        subprocess.run(
            [str(ROOT / "backend" / ".venv" / "bin" / "alembic"),
             "-c", str(ROOT / "backend" / "alembic.ini"), "upgrade", "head"],
            cwd=ROOT / "backend", env=migration_environment, check=True,
        )
        completed = subprocess.run(
            [str(ROOT / "backend" / ".venv" / "bin" / "python"), str(LIVE_LAUNCHER),
             "--grep", "真实模型以DynamicTaskAgent和固定Skill分析超预算CSV"],
            cwd=ROOT, env=environment, check=False,
        )
        return completed.returncode


def _admin_url() -> str:
    """解析显式MySQL管理员URL，缺失时从本地配置构造内存URL。

    ``MYSQL_BIND_ADDRESS`` 是服务监听地址，不是管理员客户端地址；在本机
    Docker 发布的 MySQL 上用它连接会把 root 识别成远端账号而被拒绝。因此
    默认走本机回环地址，并允许通过 ``MYSQL_TEST_ADMIN_HOST`` 显式覆盖。
    """

    configured = os.environ.get("MYSQL_TEST_ADMIN_URL", "").strip()
    if configured:
        return configured
    values: dict[str, str] = {}
    for env_path in (BACKEND_ENV, ROOT_ENV):
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    password = values.get("MYSQL_ROOT_PASSWORD", "")
    if not password:
        raise RuntimeError("缺少MYSQL_TEST_ADMIN_URL或MYSQL_ROOT_PASSWORD")
    return URL.create("mysql+pymysql", username="root", password=password,
                      host=os.environ.get("MYSQL_TEST_ADMIN_HOST", "127.0.0.1"),
                      port=int(values.get("MYSQL_PORT", "3306")),
                      query={"charset": "utf8mb4"}).render_as_string(hide_password=False)


@contextmanager
def _temporary_mysql(admin_url: str, *, database: str, username: str, password: str) -> Iterator[None]:
    """以root只创建临时库/账号并在退出时删除，任何失败均不吞掉清理错误。"""

    admin_engine = create_engine(admin_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            connection.exec_driver_sql(
                f"CREATE USER '{username}'@'%%' IDENTIFIED BY %s", (password,)
            )
            connection.exec_driver_sql(f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{username}'@'%%'")
            connection.exec_driver_sql("FLUSH PRIVILEGES")
        yield
    finally:
        try:
            with admin_engine.begin() as connection:
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
                connection.exec_driver_sql(f"DROP USER IF EXISTS '{username}'@'%%'")
                connection.exec_driver_sql("FLUSH PRIVILEGES")
        finally:
            admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
