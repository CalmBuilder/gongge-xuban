"""
@Time       : 2026/09/01 10:10
@Author     : zhanglp8181
@File       : test_app_scripts.py
@CallChain  : pytest → app/app_supervisor readiness → HTTP 状态与进程生命周期
@Description: 验证统一启动器的正向就绪、连接提前关闭和失败边界。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT_DIR / "scripts"


def _load_process_utils_dependency() -> None:
    if "process_utils" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "process_utils",
        SCRIPTS_DIR / "process_utils.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["process_utils"] = module
    spec.loader.exec_module(module)


_load_process_utils_dependency()


def _load_script(name: str, module_name: str | None = None):
    module_name = module_name or f"gongge_xuban_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_supervisor_uses_platform_specific_executables() -> None:
    supervisor = _load_script("app_supervisor")

    assert supervisor._backend_python("win32") == ROOT_DIR / "backend/.venv/Scripts/python.exe"
    assert supervisor._backend_python("linux") == ROOT_DIR / "backend/.venv/bin/python"
    assert supervisor._vite_executable("win32") == ROOT_DIR / "frontend-enterprise/node_modules/.bin/vite.cmd"
    assert supervisor._vite_executable("darwin") == ROOT_DIR / "frontend-enterprise/node_modules/.bin/vite"


def test_pid_alive_recognizes_current_process() -> None:
    process_utils = _load_script("process_utils")

    assert process_utils.pid_alive(os.getpid())


def test_launcher_defaults_come_from_shared_settings() -> None:
    supervisor = _load_script("app_supervisor")

    assert supervisor.APP_HOST == "0.0.0.0"
    assert supervisor.APP_PORT == "5137"
    command = supervisor.build_services()[0].command
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert command[command.index("--port") + 1] == "5137"


def test_app_cli_terminates_only_exact_port_listeners(monkeypatch) -> None:
    lifecycle = _load_script("app", "gongge_xuban_app_cli")
    availability = iter((False, True))
    terminated: list[int] = []
    monkeypatch.setattr(lifecycle, "_port_available", lambda _host, _port: next(availability))
    monkeypatch.setattr(lifecycle, "_listening_pids", lambda port: [41, 42] if port == 5137 else [])
    monkeypatch.setattr(lifecycle, "_terminate_pid", terminated.append)

    lifecycle._free_configured_port("0.0.0.0", 5137)

    assert terminated == [41, 42]


def test_app_cli_fails_when_configured_port_stays_occupied(monkeypatch) -> None:
    lifecycle = _load_script("app", "gongge_xuban_app_cli_blocked")
    monkeypatch.setattr(lifecycle, "_port_available", lambda _host, _port: False)
    monkeypatch.setattr(lifecycle, "_listening_pids", lambda _port: [41])
    monkeypatch.setattr(lifecycle, "_terminate_pid", lambda _pid: None)
    monkeypatch.setattr(lifecycle, "_wait_for_port_available", lambda _host, _port: False)

    import pytest

    with pytest.raises(RuntimeError, match="5137"):
        lifecycle._free_configured_port("0.0.0.0", 5137)


def test_app_cli_waits_for_socket_release_without_listener_pid(monkeypatch) -> None:
    lifecycle = _load_script("app", "gongge_xuban_app_cli_releasing")
    monkeypatch.setattr(lifecycle, "_port_available", lambda _host, _port: False)
    monkeypatch.setattr(lifecycle, "_listening_pids", lambda _port: [])
    monkeypatch.setattr(
        lifecycle,
        "_wait_for_port_available",
        lambda host, port: host == "0.0.0.0" and port == 5137,
    )

    lifecycle._free_configured_port("0.0.0.0", 5137)


def test_app_cli_blocks_startup_when_database_migration_is_required(monkeypatch) -> None:
    """MySQL 版本落后时应在启动 supervisor 前快速给出迁移命令。"""

    lifecycle = _load_script("app", "gongge_xuban_app_database_migration_required")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        """模拟迁移检查子进程返回版本落后结果。"""

        calls.append(command)
        return SimpleNamespace(
            returncode=2,
            stdout="检测到 MySQL 数据库需要迁移：mysql+pymysql://user:***@127.0.0.1/db",
            stderr="当前版本：20260830_0077\n目标版本：20260901_0078",
        )

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)

    import pytest

    with pytest.raises(RuntimeError, match="数据库需要迁移"):
        lifecycle._check_database_migration()

    assert calls == [[sys.executable, str(ROOT_DIR / "scripts" / "migrate_mysql.py"), "--check"]]


def test_app_cli_stops_quickly_when_database_check_times_out(monkeypatch) -> None:
    """MySQL 不可达时检查超时应立即终止启动并给出可执行诊断。"""

    lifecycle = _load_script("app", "gongge_xuban_app_database_migration_timeout")

    def timeout_run(*_args, **_kwargs):
        """模拟数据库检查超过启动前置检查的时间预算。"""

        raise subprocess.TimeoutExpired("migrate_mysql.py", lifecycle.DATABASE_CHECK_TIMEOUT_SECONDS)

    monkeypatch.setattr(lifecycle.subprocess, "run", timeout_run)

    import pytest

    with pytest.raises(RuntimeError, match="超过 10 秒"):
        lifecycle._check_database_migration()


def test_app_cli_accepts_current_database_schema(monkeypatch) -> None:
    """MySQL 已经位于 Alembic head 时启动前置检查应正常通过。"""

    lifecycle = _load_script("app", "gongge_xuban_app_database_current")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="数据库迁移检查通过：当前版本 20260901_0078。",
            stderr="",
        ),
    )

    lifecycle._check_database_migration()


def test_port_probe_uses_reusable_server_socket_semantics(monkeypatch) -> None:
    lifecycle = _load_script("app", "gongge_xuban_app_cli_socket_probe")

    class ProbeSocket:
        def __init__(self) -> None:
            self.options: list[tuple[int, int, int]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def setsockopt(self, level: int, option: int, value: int) -> None:
            self.options.append((level, option, value))

        def bind(self, _address) -> None:
            return None

    probe = ProbeSocket()
    monkeypatch.setattr(lifecycle.socket, "socket", lambda *_args: probe)

    assert lifecycle._port_available("0.0.0.0", 5137) is True
    assert (
        lifecycle.socket.SOL_SOCKET,
        lifecycle.socket.SO_REUSEADDR,
        1,
    ) in probe.options


def test_supervisor_does_not_restart_during_startup_grace(monkeypatch) -> None:
    supervisor = _load_script("app_supervisor")

    class RunningProcess:
        def poll(self):
            return None

    service = supervisor.Service(name="app", cwd=ROOT_DIR, command=["unused"])
    service.health_url = "http://127.0.0.1:5173/api/health"
    service.process = RunningProcess()
    service.startup_deadline = 100.0
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(service, "healthy", lambda: False)

    service.poll()

    assert service.unhealthy_count == 0
    assert service.restart_count == 0


def test_app_readiness_accepts_status_before_response_body_finishes(monkeypatch) -> None:
    """对端已返回成功状态但读取 body 提前关闭时，app readiness 仍应通过。"""

    lifecycle = _load_script("app", "gongge_xuban_app_readiness_closed")

    class EarlyClosedResponse:
        status = 200

        def __enter__(self):
            """返回模拟响应本身，兼容旧实现的上下文管理器调用。"""

            return self

        def __exit__(self, *_args):
            """模拟响应关闭阶段抛出连接异常，验证新实现会吞掉该异常。"""

            self.close()

        def read(self):
            """模拟对端在 body 尚未读取完时主动关闭连接。"""

            raise OSError("peer closed response body")

        def close(self):
            """模拟 close 阶段的可忽略网络异常。"""

            raise OSError("peer closed during close")

    monkeypatch.setattr(
        lifecycle.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: EarlyClosedResponse(),
    )

    assert lifecycle._url_ready("http://127.0.0.1:5137/api/health") is True


def test_app_readiness_rejects_server_error_without_masking_status(monkeypatch) -> None:
    """HTTP 5xx 仍是明确未就绪，不能因探测只看连接成功而误判通过。"""

    lifecycle = _load_script("app", "gongge_xuban_app_readiness_5xx")

    class ErrorResponse:
        status = 503

        def __enter__(self):
            """返回模拟 5xx 响应以覆盖兼容上下文管理路径。"""

            return self

        def __exit__(self, *_args):
            """关闭模拟响应，不改变 5xx 的未就绪判定。"""

            self.close()

        def close(self):
            """模拟正常释放 5xx 响应。"""

            return None

    monkeypatch.setattr(
        lifecycle.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ErrorResponse(),
    )

    assert lifecycle._url_ready("http://127.0.0.1:5137/api/health") is False


def test_supervisor_health_accepts_peer_close_after_success_status(monkeypatch) -> None:
    """supervisor health 与 app readiness 使用同一关闭容错契约。"""

    supervisor = _load_script("app_supervisor", "gongge_xuban_supervisor_health_closed")

    class EarlyClosedResponse:
        status = 204

        def __enter__(self):
            """返回模拟响应本身，兼容旧 supervisor 实现。"""

            return self

        def __exit__(self, *_args):
            """模拟响应 close 阶段的连接异常。"""

            self.close()

        def read(self):
            """模拟 body 尚未读完时连接被对端关闭。"""

            raise OSError("peer closed response body")

        def close(self):
            """模拟 close 阶段异常应被 health 探测忽略。"""

            raise OSError("peer closed during close")

    monkeypatch.setattr(
        supervisor.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: EarlyClosedResponse(),
    )
    service = supervisor.Service(
        name="app",
        cwd=ROOT_DIR,
        command=["unused"],
        health_url="http://127.0.0.1:5137/api/health",
    )

    assert service.healthy() is True


def test_supervisor_health_rejects_server_error(monkeypatch) -> None:
    """supervisor 不得把明确的 5xx 健康检查响应当成可用服务。"""

    supervisor = _load_script("app_supervisor", "gongge_xuban_supervisor_health_5xx")

    class ErrorResponse:
        status = 503

        def __enter__(self):
            """返回模拟 5xx 响应，覆盖 supervisor 上下文管理路径。"""

            return self

        def __exit__(self, *_args):
            """关闭模拟 5xx 响应。"""

            self.close()

        def close(self):
            """模拟正常释放响应资源。"""

            return None

    monkeypatch.setattr(
        supervisor.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ErrorResponse(),
    )
    service = supervisor.Service(
        name="app",
        cwd=ROOT_DIR,
        command=["unused"],
        health_url="http://127.0.0.1:5137/api/health",
    )

    assert service.healthy() is False


def test_detached_startup_failure_cleans_the_supervisor_it_started(monkeypatch) -> None:
    """后台服务探活失败时应清理本次刚启动的 supervisor，避免残留进程污染下次启动。"""

    lifecycle = _load_script("app", "gongge_xuban_app_startup_cleanup")
    cleanup_calls: list[bool] = []

    class FakeSupervisor:
        SINGLE_PORT = True
        APP_HOST = "127.0.0.1"
        APP_PORT = "5137"

        @staticmethod
        def validate_prerequisites() -> None:
            """模拟依赖检查通过。"""

        @staticmethod
        def build_services() -> list[SimpleNamespace]:
            """返回一个需要探活的模拟服务。"""

            return [
                SimpleNamespace(
                    name="app",
                    health_url="http://127.0.0.1:5137/api/health",
                    log_file=ROOT_DIR / ".runtime-test-app.log",
                )
            ]

        @staticmethod
        def url_host(host: str) -> str:
            """返回可拼接到健康 URL 的主机名。"""

            return host

    monkeypatch.setattr(lifecycle, "_load_supervisor", lambda: FakeSupervisor)
    monkeypatch.setattr(lifecycle, "_check_database_migration", lambda: None)
    monkeypatch.setattr(
        lifecycle,
        "stop_services",
        lambda verbose=True: cleanup_calls.append(bool(verbose)),
    )
    monkeypatch.setattr(lifecycle, "_service_ports", lambda _supervisor: [])
    monkeypatch.setattr(lifecycle, "_free_configured_port", lambda _host, _port: None)
    monkeypatch.setattr(lifecycle, "_build_frontend", lambda: None)
    monkeypatch.setattr(lifecycle, "_start_detached", lambda _supervisor: 9876)

    def fail_readiness(_label: str, _url: str, _log_file: Path) -> None:
        """模拟首个服务探活失败。"""

        raise RuntimeError("app did not become ready")

    monkeypatch.setattr(lifecycle, "_wait_for_url", fail_readiness)

    import pytest

    with pytest.raises(RuntimeError, match="did not become ready"):
        lifecycle.command_up(detach_flag=True, mode="production")

    assert cleanup_calls == [False, False]


def test_app_shell_exposes_the_unified_command_contract() -> None:
    script = (ROOT_DIR / "app.sh").read_text(encoding="utf-8")

    assert 'scripts/app.py" up --mode production --detach' in script
    assert 'scripts/app.py" up --mode development' in script
    assert 'scripts/app.py" status' in script
    assert 'scripts/app.py" down' in script


def test_db_shell_exposes_the_mysql_migration_command_contract() -> None:
    """短命名数据库脚本应同时支持主动迁移和只读检查。"""

    script = (ROOT_DIR / "db.sh").read_text(encoding="utf-8")

    assert 'scripts/migrate_mysql.py"' in script
    assert 'scripts/migrate_mysql.py" --check' in script
    assert 'Usage: ./db.sh [check|migrate]' in script
    result = subprocess.run(
        ["bash", "-n", str(ROOT_DIR / "db.sh")],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_app_powershell_exposes_the_unified_command_contract() -> None:
    script = (ROOT_DIR / "app.ps1").read_text(encoding="utf-8")

    assert 'Prefix = @("-3.11")' in script
    assert 'Prefix = @("-3")' in script
    assert '@("up", "--mode", "production", "--detach")' in script
    assert '@("up", "--mode", "development")' in script
    assert '"status" { @("status") }' in script
    assert '"stop" { @("down") }' in script
    assert "$LifecycleArgs = @($LifecycleArgs)" in script
    assert '"scripts\\app.py"' in script


def test_app_shell_reexecutes_with_bash_when_invoked_through_sh() -> None:
    result = subprocess.run(
        ["sh", str(ROOT_DIR / "app.sh"), "status"],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Processes:" in result.stdout


def test_legacy_launchers_and_docx_generator_are_removed() -> None:
    removed = (
        "dev.py",
        "dev_" + "supervisor.py",
        "dev.ps1",
        "dev_" + "up.sh",
        "dev_" + "down.sh",
        "dev_" + "status.sh",
        "dev_up.ps1",
        "dev_down.ps1",
        "dev_status.ps1",
        "generate_long_knowledge_docx.py",
    )

    assert all(not (SCRIPTS_DIR / name).exists() for name in removed)
    assert (SCRIPTS_DIR / "process_utils.py").is_file()
