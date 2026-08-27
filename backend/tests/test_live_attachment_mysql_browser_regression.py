"""
@Time       : 2026/08/25
@Author     : zhanglp8181
@File       : test_live_attachment_mysql_browser_regression.py
@CallChain  : pytest → MySQL LIVE回归包装器 → 管理连接解析
@Description: 验证MySQL浏览器回归工具不会把服务监听地址误当本机root客户端地址。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "run_live_attachment_mysql_browser_regression.py"
SPEC = importlib.util.spec_from_file_location("run_live_attachment_mysql_browser_regression", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_admin_url_defaults_to_loopback_not_bind_address(tmp_path, monkeypatch) -> None:
    """验证Compose监听地址只用于服务发布，管理员客户端默认走回环地址。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MYSQL_ROOT_PASSWORD=fixture-only-placeholder\nMYSQL_BIND_ADDRESS=192.168.124.236\nMYSQL_PORT=3306\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "BACKEND_ENV", tmp_path / "missing-backend.env")
    monkeypatch.setattr(MODULE, "ROOT_ENV", env_file)
    monkeypatch.delenv("MYSQL_TEST_ADMIN_URL", raising=False)
    monkeypatch.delenv("MYSQL_TEST_ADMIN_HOST", raising=False)

    parsed = make_url(MODULE._admin_url())

    assert parsed.host == "127.0.0.1"
    assert parsed.port == 3306
    assert parsed.username == "root"


def test_admin_url_honors_explicit_test_host(tmp_path, monkeypatch) -> None:
    """验证显式测试主机仍可用于远程MySQL，不被默认回环策略覆盖。"""

    env_file = tmp_path / ".env"
    env_file.write_text("MYSQL_ROOT_PASSWORD=fixture-only-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(MODULE, "BACKEND_ENV", tmp_path / "missing-backend.env")
    monkeypatch.setattr(MODULE, "ROOT_ENV", env_file)
    monkeypatch.delenv("MYSQL_TEST_ADMIN_URL", raising=False)
    monkeypatch.setenv("MYSQL_TEST_ADMIN_HOST", "db.internal")

    assert make_url(MODULE._admin_url()).host == "db.internal"
