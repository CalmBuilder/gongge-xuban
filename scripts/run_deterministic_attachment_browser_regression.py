"""
@Time       : 2026/08/15 03:08
@Author     : zhanglp8181
@File       : run_deterministic_attachment_browser_regression.py
@CallChain  : 开发者命令 → Playwright确定性附件套件 → 无密钥持久审计报告
@Description: 独占运行真实浏览器/文件/Runtime回归，并冻结测试结果与关键源码指纹。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend-enterprise"
EVIDENCE = ROOT / "docs/manuals/evidence/attachment-analysis-deterministic-report.json"


class _LauncherTermination(KeyboardInterrupt):
    """把可捕获的外部终止信号转换为会执行finally的中断。"""


def main() -> int:
    """以唯一端口和运行目录执行19项Chromium套件，成功后原子发布精简证据。"""

    port = 40_000 + os.getpid() % 1_000
    runtime_dir = Path(f"/tmp/gongge-deterministic-attachment-{port}")
    _write_evidence({"status": "running", "started_at": datetime.now(UTC).isoformat()})
    with tempfile.TemporaryDirectory(prefix="gongge-playwright-json-") as temp_dir:
        raw_report = Path(temp_dir) / "report.json"
        environment = _minimal_environment()
        environment.update(
            {
                "FULLSTACK_E2E_PORT": str(port),
                "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
                "FULLSTACK_E2E_REUSE_EXISTING_SERVER": "1",
                "PRESERVE_FULLSTACK_E2E": "1",
                "PLAYWRIGHT_JSON_OUTPUT_NAME": str(raw_report),
            }
        )
        server: subprocess.Popen[bytes] | None = None
        process_group: int | None = None
        with _termination_signal_guard():
            try:
                server = subprocess.Popen(
                    [str(ROOT / "backend/.venv/bin/python"), "e2e/start_fullstack_server.py"],
                    cwd=FRONTEND,
                    env=environment,
                    start_new_session=True,
                )
                process_group = server.pid
                _wait_for_server(port, server)
                completed = subprocess.run(
                    [
                        "npx",
                        "playwright",
                        "test",
                        "e2e/attachment-analysis-f1.fullstack.e2e.ts",
                        "--config=playwright.fullstack.config.ts",
                        "--project=chromium",
                        "--reporter=line,json",
                    ],
                    cwd=FRONTEND,
                    env=environment,
                    check=False,
                )
            finally:
                try:
                    if server is not None and process_group is not None:
                        _terminate_process_group(server, process_group=process_group)
                finally:
                    _remove_runtime_dir(runtime_dir)
        if completed.returncode != 0:
            _write_evidence(
                {
                    "status": "failed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "exit_code": completed.returncode,
                    "certification_fingerprints": _certification_fingerprints(),
                }
            )
            return completed.returncode
        report = json.loads(raw_report.read_text(encoding="utf-8"))
        results = _collect_results(report.get("suites", []))
        if len(results) != 19 or any(item["status"] != "passed" for item in results):
            _write_evidence(
                {
                    "status": "failed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "test_count": len(results),
                    "certification_fingerprints": _certification_fingerprints(),
                }
            )
            raise RuntimeError("确定性附件套件证据不是19项全部通过")
        payload = {
            "status": "passed",
            "completed_at": datetime.now(UTC).isoformat(),
            "test_count": len(results),
            "duration_ms": sum(int(item["duration_ms"]) for item in results),
            "boundary": {
                "browser": "real_chromium",
                "frontend_backend_runtime": "production_code",
                "binary_fixtures": "real_files",
                "llm_and_github": "deterministic_protocol_substitutes",
            },
            "certification_fingerprints": _certification_fingerprints(),
            "tests": results,
        }
        _write_evidence(payload)
    return 0


def _minimal_environment() -> dict[str, str]:
    """只投影运行Python、npm和Chromium所需的公开环境，拒绝继承部署密钥。"""

    names = (
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
        "CI",
        "FORCE_COLOR",
        "NO_COLOR",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


@contextmanager
def _termination_signal_guard() -> Iterator[None]:
    """将SIGTERM/SIGHUP转换成Python中断，保证服务和运行目录进入finally清理。"""

    watched = tuple(
        candidate
        for candidate in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
        if candidate is not None
    )
    previous = {candidate: signal.getsignal(candidate) for candidate in watched}

    def interrupt(signum: int, _frame: object) -> None:
        raise _LauncherTermination(f"确定性附件回归收到终止信号: {signum}")

    try:
        for candidate in watched:
            signal.signal(candidate, interrupt)
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def _write_evidence(payload: dict[str, object]) -> None:
    """原子写入当前运行状态，避免失败后继续保留同一源码树的旧PASS证据。"""

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    staging = EVIDENCE.with_suffix(".json.tmp")
    staging.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staging.replace(EVIDENCE)


def _wait_for_server(port: int, server: subprocess.Popen[bytes], timeout: float = 180.0) -> None:
    """等待专属全栈服务健康；服务提前退出或超时都阻止浏览器启动。"""

    deadline = time.monotonic() + timeout
    health_url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        return_code = server.poll()
        if return_code is not None:
            raise RuntimeError(f"确定性全栈服务提前退出: {return_code}")
        try:
            with urlopen(health_url, timeout=1.0) as response:  # noqa: S310
                if 200 <= response.status < 300:
                    return
        except (OSError, URLError):
            time.sleep(0.2)
    raise RuntimeError("等待确定性全栈服务健康超时")


def _terminate_process_group(
    server: subprocess.Popen[bytes], *, process_group: int, timeout: float = 10.0
) -> None:
    """终止并确认完整服务进程组消失，不能只等待可能先退出的组长。"""

    if not _process_group_exists(process_group):
        server.wait()
        return
    os.killpg(process_group, signal.SIGTERM)
    if not _wait_for_process_group_exit(server, process_group, timeout):
        os.killpg(process_group, signal.SIGKILL)
        if not _wait_for_process_group_exit(server, process_group, timeout):
            raise RuntimeError("确定性全栈服务进程组无法回收")
    server.wait()


def _process_group_exists(process_group: int) -> bool:
    """以信号0检查进程组是否仍有成员，不把权限错误误判为已退出。"""

    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    server: subprocess.Popen[bytes], process_group: int, timeout: float
) -> bool:
    """轮询完整进程组终态，同时及时回收launcher直接拥有的组长。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server.poll()
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.05)
    return not _process_group_exists(process_group)


def _remove_runtime_dir(runtime_dir: Path) -> None:
    """删除本轮显式临时目录；任何删除失败或残留都必须阻止PASS证据发布。"""

    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    if runtime_dir.exists():
        raise RuntimeError(f"确定性附件运行目录清理失败: {runtime_dir}")


def _collect_results(suites: list[dict[str, object]]) -> list[dict[str, object]]:
    """递归提取Playwright spec结果，拒绝用页面文案推导测试状态。"""

    collected: list[dict[str, object]] = []
    for suite in suites:
        for spec in suite.get("specs", []):
            if not isinstance(spec, dict):
                continue
            tests = spec.get("tests", [])
            attempts = [
                result
                for test in tests
                if isinstance(test, dict)
                for result in test.get("results", [])
                if isinstance(result, dict)
            ]
            collected.append(
                {
                    "title": str(spec.get("title") or ""),
                    "file": str(spec.get("file") or ""),
                    "status": "passed"
                    if bool(spec.get("ok")) and attempts
                    else "failed",
                    "duration_ms": sum(int(result.get("duration") or 0) for result in attempts),
                }
            )
        children = suite.get("suites", [])
        if isinstance(children, list):
            collected.extend(_collect_results(children))
    return collected


def _certification_fingerprints() -> dict[str, str]:
    """冻结确定性附件浏览器闭环涉及的生产实现、fixture、E2E与launcher。"""

    relative_paths = (
        "backend/app/api/artifacts.py",
        "backend/app/api/chat.py",
        "backend/app/config.py",
        "backend/app/core/agent_loop.py",
        "backend/app/core/response_generator.py",
        "backend/app/dynamic_tasks/agent.py",
        "backend/app/dynamic_tasks/action_proposer.py",
        "backend/app/dynamic_tasks/attachment_evidence.py",
        "backend/app/dynamic_tasks/planner_service.py",
        "backend/app/dynamic_tasks/result_verifier.py",
        "backend/app/dynamic_tasks/artifact_renderer.py",
        "backend/app/main.py",
        "backend/app/session/input_extraction.py",
        "backend/app/session/input_runtime.py",
        "backend/app/llm/prompts/response_generator_prompt.md",
        "backend/app/sop_runtime/execution_store.py",
        "backend/app/session/managed_resources.py",
        "backend/app/session/provider_input_dispatch.py",
        "backend/app/security/bounded_request_body.py",
        "backend/app/sop_runtime/coordinator.py",
        "backend/app/sop_runtime/definition.py",
        "backend/tests/fixtures/attachments/generate_fixtures.py",
        "backend/tests/fixtures/attachments/manifest.json",
        "backend/tests/fixtures/attachments/negative/active_content.pdf",
        "backend/tests/fixtures/attachments/negative/corrupt.docx",
        "backend/tests/fixtures/attachments/negative/empty.csv",
        "backend/tests/fixtures/attachments/negative/forged_extension.pdf",
        "backend/tests/fixtures/attachments/negative/sales_targets_missing_target.csv",
        "backend/tests/fixtures/attachments/positive/contract_text.pdf",
        "backend/tests/fixtures/attachments/positive/launch_review.pptx",
        "backend/tests/fixtures/attachments/positive/product_screen.png",
        "backend/tests/fixtures/attachments/positive/sales_actuals.xlsx",
        "backend/tests/fixtures/attachments/positive/sales_targets.csv",
        "backend/tests/fixtures/attachments/positive/service_manual.docx",
        "backend/tests/fixtures/attachments/negative/sales_actuals_formula_conflict.xlsx",
        "frontend-enterprise/e2e/attachment-analysis-f1.fullstack.e2e.ts",
        "frontend-enterprise/e2e/start_fullstack_server.py",
        "frontend-enterprise/src/pages/chat/useChatSession.ts",
        "scripts/run_deterministic_attachment_browser_regression.py",
    )
    return {
        relative_path: sha256((ROOT / relative_path).read_bytes()).hexdigest()
        for relative_path in relative_paths
    }


if __name__ == "__main__":
    raise SystemExit(main())
