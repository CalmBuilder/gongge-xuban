"""
@Time       : 2026/07/28 18:20
@Author     : zhanglp8181
@File       : conftest.py
@CallChain  : pytest mysql 标记 → 隔离数据库/运行账号夹具 → MySQL 8.4
@Description: 安全创建并清理随机 MySQL 测试数据库和最小运行账号。
"""

from collections.abc import Iterator
import os
from secrets import token_hex

import pytest
from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.pool import NullPool


@pytest.fixture
def mysql_database_url() -> Iterator[str]:
    """创建独立 MySQL 数据库和随机运行账号，结束后清理全部临时资源。"""

    admin_url_text = os.environ.get("MYSQL_TEST_ADMIN_URL")
    if not admin_url_text:
        pytest.skip("MYSQL_TEST_ADMIN_URL is required for MySQL integration tests")

    suffix = token_hex(6)
    database_name = f"gongge_xuban_test_{suffix}"
    username = f"st_test_{suffix}"
    password = token_hex(16)
    account_host = os.environ.get("MYSQL_TEST_CLIENT_HOST", "%")
    admin_url = make_url(admin_url_text)
    admin_engine = create_engine(admin_url, poolclass=NullPool)

    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE {database_name} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            connection.execute(
                text(
                    f"CREATE USER '{username}'@'{account_host}' "
                    "IDENTIFIED BY :password"
                ),
                {"password": password},
            )
            connection.execute(
                text(
                    f"GRANT ALL PRIVILEGES ON {database_name}.* "
                    f"TO '{username}'@'{account_host}'"
                )
            )

        runtime_url = URL.create(
            "mysql+pymysql",
            username=username,
            password=password,
            host=admin_url.host or "127.0.0.1",
            port=admin_url.port or 3306,
            database=database_name,
            query={"charset": "utf8mb4"},
        )
        yield runtime_url.render_as_string(hide_password=False)
    finally:
        try:
            with admin_engine.begin() as connection:
                connection.execute(
                    text(f"DROP USER IF EXISTS '{username}'@'{account_host}'")
                )
        finally:
            try:
                with admin_engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"DROP DATABASE IF EXISTS {database_name}"
                    )
            finally:
                admin_engine.dispose()
