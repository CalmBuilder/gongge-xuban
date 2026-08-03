from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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


def test_app_shell_exposes_the_unified_command_contract() -> None:
    script = (ROOT_DIR / "app.sh").read_text(encoding="utf-8")

    assert 'scripts/app.py" up --mode production --detach' in script
    assert 'scripts/app.py" up --mode development' in script
    assert 'scripts/app.py" status' in script
    assert 'scripts/app.py" down' in script


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
