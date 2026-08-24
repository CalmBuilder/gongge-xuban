"""
@Time       : 2026/08/15 00:25
@Author     : zhanglp8181
@File       : test_live_model_browser_launchers.py
@CallChain  : pytest → LIVE launcher browser environment helper → Node/Chromium subprocess boundary
@Description: 验证真实模型密钥只保留在隔离后端，不传给浏览器子树。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/run_live_attachment_browser_regression.py",
        "scripts/run_live_stream_cancel_browser_regression.py",
    ),
)
def test_browser_subprocess_environment_does_not_inherit_model_secrets(
    relative_path: str,
) -> None:
    """确保 Playwright/Node/Chromium 仅获得非密配置，密钥仍留在 server env。"""

    module = _load_script(relative_path)
    server_environment = {
        "PATH": "/usr/bin",
        "LIVE_ATTACHMENT_E2E": "1",
        "LIVE_ATTACHMENT_MODEL_API_KEY": "live-secret",
        "DEMO_MODEL_API_KEY": "derived-secret",
        "LIVE_ATTACHMENT_MODEL_NAME": "model-name",
        "LIVE_ATTACHMENT_MODEL_BASE_URL": "https://user:url-secret@example.invalid/v1?token=secret",
        "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": '{"authorization":"extra-secret"}',
        "LIVE_ATTACHMENT_PROVIDER_ENDPOINT": "https://example.invalid/v1",
        "UNRELATED_DEPLOY_SECRET": "parent-secret",
    }

    browser_environment = module._browser_environment(server_environment)

    assert server_environment["LIVE_ATTACHMENT_MODEL_API_KEY"] == "live-secret"
    assert server_environment["DEMO_MODEL_API_KEY"] == "derived-secret"
    assert "LIVE_ATTACHMENT_MODEL_API_KEY" not in browser_environment
    assert "DEMO_MODEL_API_KEY" not in browser_environment
    assert "LIVE_ATTACHMENT_MODEL_BASE_URL" not in browser_environment
    assert "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON" not in browser_environment
    assert "UNRELATED_DEPLOY_SECRET" not in browser_environment
    assert browser_environment["PATH"] == "/usr/bin"
    if relative_path.endswith("run_live_attachment_browser_regression.py"):
        assert browser_environment["LIVE_ATTACHMENT_E2E"] == "1"
        assert browser_environment["LIVE_ATTACHMENT_MODEL_NAME"] == "model-name"
        assert browser_environment["LIVE_ATTACHMENT_PROVIDER_ENDPOINT"] == (
            "https://example.invalid/v1"
        )
    else:
        assert "LIVE_ATTACHMENT_E2E" not in browser_environment
        assert "LIVE_ATTACHMENT_MODEL_NAME" not in browser_environment


def test_attachment_launcher_sanitizes_provider_endpoint() -> None:
    """审计证据只保留公开endpoint，查询参数被去除且userinfo凭据被拒绝。"""

    module = _load_script("scripts/run_live_attachment_browser_regression.py")

    assert module._public_provider_endpoint("https://api.example.com/v1/?region=cn#fragment") == (
        "https://api.example.com/v1"
    )
    with pytest.raises(RuntimeError, match="userinfo"):
        module._public_provider_endpoint("https://user:secret@api.example.com/v1")


def test_attachment_launcher_reaps_process_group_with_term_ignoring_child() -> None:
    """LIVE launcher必须在组长先退出后升级KILL并回收忽略TERM的worker。"""

    module = _load_script("scripts/run_live_attachment_browser_regression.py")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
                "time.sleep(60)"
            ),
        ],
        start_new_session=True,
    )
    time.sleep(0.2)

    module._terminate_process_group(process, process_group=process.pid, timeout=0.2)

    assert process.poll() is not None
    assert module._process_group_exists(process.pid) is False


@pytest.mark.parametrize("signal_name", ("SIGTERM", "SIGHUP"))
def test_attachment_launcher_signal_guard_reaches_cleanup_and_restores_handler(
    signal_name: str,
) -> None:
    """单独终止LIVE launcher时必须转换为中断并恢复调用方信号处理器。"""

    module = _load_script("scripts/run_live_attachment_browser_regression.py")
    target = getattr(signal, signal_name)
    original = signal.getsignal(target)
    cleanup_reached = False

    try:
        with pytest.raises(module._LauncherTermination):
            with module._termination_signal_guard():
                try:
                    os.kill(os.getpid(), target)
                finally:
                    cleanup_reached = True
    finally:
        signal.signal(target, original)

    assert cleanup_reached is True
    assert signal.getsignal(target) == original


def test_attachment_launcher_runtime_cleanup_fails_closed_when_directory_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE运行目录删除器静默失效时必须报错，不能带残留返回成功。"""

    module = _load_script("scripts/run_live_attachment_browser_regression.py")
    with tempfile.TemporaryDirectory() as parent:
        runtime_dir = Path(parent) / "runtime"
        runtime_dir.mkdir()
        with monkeypatch.context() as cleanup_patch:
            cleanup_patch.setattr(module.shutil, "rmtree", lambda _path: None)

            with pytest.raises(RuntimeError, match="运行目录清理失败"):
                module._remove_runtime_dir(runtime_dir)


def _load_script(relative_path: str) -> ModuleType:
    """按文件路径加载无包脚本，避免执行其 main 入口。"""

    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load launcher: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
