"""
@Time       : 2026/08/15 03:08
@Author     : zhanglp8181
@File       : test_deterministic_attachment_browser_launcher.py
@CallChain  : pytest → 确定性附件浏览器launcher报告解析
@Description: 验证嵌套Playwright JSON只以机械spec状态生成审计结果。
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


ROOT = Path(__file__).resolve().parents[2]


def _launcher_module():
    """从仓库脚本路径加载launcher，避免把scripts变成生产Python包。"""

    path = ROOT / "scripts/run_deterministic_attachment_browser_regression.py"
    spec = importlib.util.spec_from_file_location("deterministic_attachment_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_results_requires_playwright_ok_and_real_attempt() -> None:
    """嵌套suite只有ok且含执行attempt时才记为passed，并累加真实耗时。"""

    module = _launcher_module()
    results = module._collect_results(
        [
            {
                "specs": [],
                "suites": [
                    {
                        "specs": [
                            {
                                "title": "正向",
                                "file": "example.ts",
                                "ok": True,
                                "tests": [{"results": [{"status": "passed", "duration": 12}]}],
                            },
                            {
                                "title": "无执行不得假绿",
                                "file": "example.ts",
                                "ok": True,
                                "tests": [],
                            },
                        ]
                    }
                ],
            }
        ]
    )

    assert results == [
        {"title": "正向", "file": "example.ts", "status": "passed", "duration_ms": 12},
        {
            "title": "无执行不得假绿",
            "file": "example.ts",
            "status": "failed",
            "duration_ms": 0,
        },
    ]


def test_terminate_process_group_reaps_owned_server() -> None:
    """launcher必须终止并回收自己创建的独立服务进程组，避免残留孤儿服务。"""

    module = _launcher_module()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )

    module._terminate_process_group(process, process_group=process.pid, timeout=2.0)

    assert process.poll() is not None


def test_terminate_process_group_kills_child_that_ignores_sigterm() -> None:
    """组长先退出时仍须升级SIGKILL，回收忽略SIGTERM的同组子进程。"""

    module = _launcher_module()
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


def test_minimal_environment_rejects_parent_secrets(monkeypatch) -> None:
    """确定性服务与浏览器只能继承公开运行字段，不能扩散父进程部署密钥。"""

    module = _launcher_module()
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("LIVE_ATTACHMENT_MODEL_API_KEY", "sentinel-model-secret")
    monkeypatch.setenv("LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON", '{"authorization":"secret"}')
    monkeypatch.setenv("UNRELATED_DEPLOY_SECRET", "sentinel-ambient-secret")
    monkeypatch.setenv("LIVE_ATTACHMENT_E2E", "1")

    environment = module._minimal_environment()

    assert "PATH" in environment
    assert "LIVE_ATTACHMENT_MODEL_API_KEY" not in environment
    assert "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON" not in environment
    assert "UNRELATED_DEPLOY_SECRET" not in environment
    assert "LIVE_ATTACHMENT_E2E" not in environment


def test_remove_runtime_dir_fails_closed_when_directory_remains(monkeypatch) -> None:
    """目录删除器静默不工作时必须抛错，不能继续发布绿色浏览器证据。"""

    module = _launcher_module()
    with tempfile.TemporaryDirectory() as parent:
        runtime_dir = Path(parent) / "runtime"
        runtime_dir.mkdir()
        with monkeypatch.context() as cleanup_patch:
            cleanup_patch.setattr(module.shutil, "rmtree", lambda _path: None)
            try:
                module._remove_runtime_dir(runtime_dir)
            except RuntimeError as error:
                assert "运行目录清理失败" in str(error)
            else:
                raise AssertionError("残留运行目录必须阻止验收证据发布")


def test_termination_signal_guard_converts_sigterm_and_restores_handler() -> None:
    """单独终止launcher PID时也必须转成可展开finally的中断，并恢复原信号处理器。"""

    module = _launcher_module()
    original = signal.getsignal(signal.SIGTERM)
    cleanup_reached = False

    try:
        try:
            with module._termination_signal_guard():
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                finally:
                    cleanup_reached = True
        except module._LauncherTermination:
            pass
        else:
            raise AssertionError("SIGTERM必须转换为launcher中断")
    finally:
        signal.signal(signal.SIGTERM, original)

    assert cleanup_reached is True
    assert signal.getsignal(signal.SIGTERM) == original
