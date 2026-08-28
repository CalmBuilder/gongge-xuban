"""
@Time       : 2026/08/28 13:00
@Author     : zhanglp8181
@File       : test_cancellation_and_outbound_boundaries.py
@CallChain  : pytest → cancellation/outbound → MCP、HTTP 探测与 General Skill runner
@Description: 验证同步阻塞阶段的取消传播、runner 进程组收敛及临时探测出网边界。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cancellation import TurnCancellationRequested, run_cancellable
from app.general_skills import runner
from app.general_skills.schema import GeneralSkillExecutionPlan
from app.security.outbound import OutboundTargetError, prepare_outbound_request


def test_run_cancellable_returns_completed_operation() -> None:
    """未取消时同步阶段应原样返回结果，不改变已有阻塞调用语义。"""

    assert run_cancellable(lambda: {"ok": True}, lambda: False) == {"ok": True}


def test_run_cancellable_interrupts_wait_and_invokes_close_hook() -> None:
    """取消发生在阻塞调用期间时，消费者应及时退出并执行资源关闭钩子。"""

    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    close_called = threading.Event()
    outcome: list[BaseException] = []

    def operation() -> str:
        """模拟无法直接安全强杀、但可通过 close 钩子收敛的同步 SDK 调用。"""

        started.set()
        release.wait(timeout=5)
        return "late-result"

    def consume() -> None:
        """在独立消费者线程中观察可取消阶段的异常。"""

        try:
            run_cancellable(
                operation,
                cancelled.is_set,
                on_cancel=close_called.set,
                poll_seconds=0.01,
            )
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert started.wait(timeout=2)
    cancelled.set()
    consumer.join(timeout=2)
    release.set()

    assert not consumer.is_alive()
    assert close_called.is_set()
    assert len(outcome) == 1
    assert isinstance(outcome[0], TurnCancellationRequested)


def test_public_domain_is_resolved_and_pinned() -> None:
    """未列入白名单的公网域名应固定到单次解析结果并保留原始 Host/SNI。"""

    def resolver(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        """返回稳定的公网解析结果，避免测试依赖环境 DNS。"""

        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    target = prepare_outbound_request(
        "https://api.example.test/v1/items?active=true",
        resolver=resolver,
    )

    assert target.request_url == "https://93.184.216.34/v1/items?active=true"
    assert target.headers == {"Host": "api.example.test"}
    assert target.extensions == {"sni_hostname": "api.example.test"}


def test_private_literal_and_dns_rebinding_targets_are_rejected() -> None:
    """私网字面量及公网/私网混合解析结果都必须在连接前被拒绝。"""

    with pytest.raises(OutboundTargetError):
        prepare_outbound_request("http://127.0.0.1:8080/health")

    def rebinding_resolver(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        """模拟同一域名同时返回公网和 loopback 地址的 DNS 重绑定结果。"""

        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    with pytest.raises(OutboundTargetError):
        prepare_outbound_request("http://api.example.test/health", resolver=rebinding_resolver)


def test_explicit_admin_allowlist_preserves_internal_virtual_host() -> None:
    """显式批准的企业内网主机仍需固定解析地址，同时保留逻辑 Host/SNI。"""

    target = prepare_outbound_request(
        "https://internal.example.test/health",
        allowed_hosts={"internal.example.test"},
        resolver=lambda host, port, **_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.0.8", port))
        ],
    )

    assert target.request_url == "https://10.10.0.8/health"
    assert target.headers == {"Host": "internal.example.test"}
    assert target.extensions == {"sni_hostname": "internal.example.test"}


def test_general_skill_runner_completes_real_python_subprocess() -> None:
    """未取消时 runner 应真实启动受限 Python 解释器并解析结构化 stdout。"""

    skill = SimpleNamespace(
        slug="boundary-positive",
        name="边界正向 Skill",
        skill_markdown="",
        skill_files_json=[],
    )
    plan = GeneralSkillExecutionPlan(
        runtime="python",
        code='import json; print(json.dumps({"success": True, "value": 42}))',
    )

    stdout, stderr, structured = runner.GeneralSkillRunner()._execute_plan(
        skill,
        "返回 42",
        plan,
        "user_demo",
        [],
        attempt=1,
    )

    assert stderr == ""
    assert '"value": 42' in stdout
    assert structured == {"success": True, "value": 42}


@pytest.mark.skipif(os.name != "posix", reason="进程组断言依赖 POSIX /proc 语义")
def test_general_skill_runner_cancellation_kills_child_process_group(tmp_path: Path) -> None:
    """取消 runner 时应连同其派生子进程一起终止，不能留下孤儿执行。"""

    child_pid_file = tmp_path / "child.pid"
    skill = SimpleNamespace(
        slug="boundary-negative",
        name="边界取消 Skill",
        skill_markdown="",
        skill_files_json=[],
    )
    code = (
        "import pathlib, subprocess, sys, time\n"
        f"pid_file = pathlib.Path({str(child_pid_file)!r})\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pid_file.write_text(str(child.pid), encoding='utf-8')\n"
        "print('child-started', flush=True)\n"
        "time.sleep(30)\n"
    )
    plan = GeneralSkillExecutionPlan(runtime="python", code=code)
    cancelled = threading.Event()

    def request_cancel() -> None:
        """等待真实子进程创建后发出取消，确保覆盖进程树正在运行的窗口。"""

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.01)
        cancelled.set()

    watcher = threading.Thread(target=request_cancel, daemon=True)
    watcher.start()
    with pytest.raises(TurnCancellationRequested):
        runner.GeneralSkillRunner()._execute_plan(
            skill,
            "执行一个会阻塞的 Skill",
            plan,
            "user_demo",
            [],
            attempt=1,
            is_cancelled=cancelled.is_set,
        )
    watcher.join(timeout=1)

    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _pid_is_running(child_pid):
        time.sleep(0.02)
    assert _pid_is_running(child_pid) is False


def _pid_is_running(pid: int) -> bool:
    """判断 POSIX 进程是否仍处于可执行/等待状态，允许已退出的僵尸短暂存在。"""

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return False
    state = stat.rsplit(") ", 1)[-1][:1]
    return state in {"R", "S", "D", "T", "I"}
