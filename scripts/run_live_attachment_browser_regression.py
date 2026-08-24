"""
@Time       : 2026/08/14 21:45
@Author     : zhanglp8181
@File       : run_live_attachment_browser_regression.py
@CallChain  : 管理数据库默认ModelConfig → 密钥仅注入隔离后端 → 无密钥Playwright LIVE附件套件
@Description: 只读选择管理端权威模型，隔离密钥并落不含凭据的可审计浏览器认证证据。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from hashlib import sha256
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend-enterprise"


class _LauncherTermination(KeyboardInterrupt):
    """把可捕获的外部终止信号转换为会执行finally的中断。"""


def main() -> int:
    """从业务配置库读取启用模型，密钥仅注入隔离后端而不进入浏览器子树。"""

    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ModelConfig
    from app.security.encryption import decrypt_secret

    with Session(engine) as db:
        requested_model_id = os.environ.get(
            "LIVE_ATTACHMENT_SOURCE_MODEL_CONFIG_ID", ""
        ).strip()
        statement = select(ModelConfig).where(
            ModelConfig.tenant_id == "tenant_demo",
            ModelConfig.enabled == True,  # noqa: E712 - SQLModel布尔表达式。
            ModelConfig.preflight_status == "ready",
        )
        if requested_model_id:
            statement = statement.where(ModelConfig.id == requested_model_id)
        else:
            statement = statement.where(
                ModelConfig.is_default == True,  # noqa: E712 - 管理端默认配置是权威来源。
            )
        model = db.exec(statement).first()
        if model is None:
            target = "指定" if requested_model_id else "默认"
            raise RuntimeError(f"tenant_demo没有已启用且通过预检的{target}ModelConfig")
        api_key = decrypt_secret(model.api_key_encrypted)
        base_url = str(model.base_url or "").strip()
        model_name = str(model.model or "").strip()
        model_id = model.id
        capability_checksum = str(model.capability_checksum or "")
        model_temperature = float(model.temperature)
        model_max_output_tokens = int(model.max_output_tokens)
    if not api_key or not base_url or not model_name:
        raise RuntimeError("管理端默认ModelConfig缺少可运行连接信息")

    server_env = os.environ.copy()
    server_env.update(
        {
            "LIVE_ATTACHMENT_E2E": "1",
            "LIVE_ATTACHMENT_MODEL_API_KEY": api_key,
            "LIVE_ATTACHMENT_MODEL_BASE_URL": base_url,
            "LIVE_ATTACHMENT_PROVIDER_ENDPOINT": _public_provider_endpoint(base_url),
            "LIVE_ATTACHMENT_MODEL_NAME": model_name,
            "LIVE_ATTACHMENT_MODEL_DISPLAY_NAME": str(model.name),
            "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": json.dumps(
                dict(model.extra_body_json or {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "LIVE_ATTACHMENT_SOURCE_MODEL_CONFIG_ID": model_id,
            "LIVE_ATTACHMENT_MODEL_CAPABILITY_CHECKSUM": capability_checksum,
            "LIVE_ATTACHMENT_MODEL_TEMPERATURE": str(model_temperature),
            "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS": str(model_max_output_tokens),
            "LIVE_ATTACHMENT_REQUIRE_ACCOUNT_CHECK": (
                "1" if "api.deepseek.com" in base_url.lower() else "0"
            ),
            "LIVE_ATTACHMENT_CERTIFICATION_FINGERPRINT_JSON": json.dumps(
                _certification_fingerprints(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    port = int(os.environ.get("FULLSTACK_E2E_PORT", str(39_000 + os.getpid() % 1_000)))
    runtime_dir = Path(
        os.environ.get(
            "FULLSTACK_E2E_RUNTIME_DIR",
            f"/tmp/gongge-live-attachment-{port}",
        )
    )
    server_env.update(
        {
            "FULLSTACK_E2E_PORT": str(port),
            "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
            "LIVE_ATTACHMENT_EVIDENCE_FILE": _evidence_filename(sys.argv[1:]),
        }
    )
    server: subprocess.Popen[bytes] | None = None
    process_group: int | None = None
    with _termination_signal_guard():
        try:
            server = subprocess.Popen(
                [
                    str(BACKEND / ".venv" / "bin" / "python"),
                    str(FRONTEND / "e2e" / "start_fullstack_server.py"),
                ],
                cwd=ROOT,
                env=server_env,
                start_new_session=True,
            )
            process_group = server.pid
            _wait_for_health(port, server)
            browser_env = _browser_environment(server_env)
            browser_env.update(
                {
                    "FULLSTACK_E2E_REUSE_EXISTING_SERVER": "1",
                    "PRESERVE_FULLSTACK_E2E": "1",
                }
            )
            completed = subprocess.run(
                [
                    "npx",
                    "playwright",
                    "test",
                    "e2e/attachment-analysis-live.fullstack.e2e.ts",
                    "--config=playwright.fullstack.config.ts",
                    "--project=chromium",
                    *sys.argv[1:],
                ],
                cwd=FRONTEND,
                env=browser_env,
                check=False,
            )
            return completed.returncode
        finally:
            try:
                if server is not None and process_group is not None:
                    _terminate_process_group(server, process_group=process_group)
            finally:
                _remove_runtime_dir(runtime_dir)


@contextmanager
def _termination_signal_guard() -> Iterator[None]:
    """将SIGTERM/SIGHUP转换成Python中断，保证LIVE服务和运行目录完成清理。"""

    watched = tuple(
        candidate
        for candidate in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
        if candidate is not None
    )
    previous = {candidate: signal.getsignal(candidate) for candidate in watched}

    def interrupt(signum: int, _frame: object) -> None:
        raise _LauncherTermination(f"LIVE附件回归收到终止信号: {signum}")

    try:
        for candidate in watched:
            signal.signal(candidate, interrupt)
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def _terminate_process_group(
    server: subprocess.Popen[bytes], *, process_group: int, timeout: float = 10.0
) -> None:
    """终止并确认完整LIVE服务进程组消失，避免组长先退后遗留worker。"""

    if not _process_group_exists(process_group):
        server.wait()
        return
    os.killpg(process_group, signal.SIGTERM)
    if not _wait_for_process_group_exit(server, process_group, timeout):
        os.killpg(process_group, signal.SIGKILL)
        if not _wait_for_process_group_exit(server, process_group, timeout):
            raise RuntimeError("LIVE附件全栈服务进程组无法回收")
    server.wait()


def _process_group_exists(process_group: int) -> bool:
    """以信号0检查LIVE服务进程组是否仍有成员。"""

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
    """轮询完整LIVE进程组终态，同时回收launcher直接拥有的组长。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server.poll()
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.05)
    return not _process_group_exists(process_group)


def _remove_runtime_dir(runtime_dir: Path) -> None:
    """删除本轮LIVE临时目录，删除失败或残留必须阻止成功退出。"""

    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    if runtime_dir.exists():
        raise RuntimeError(f"LIVE附件运行目录清理失败: {runtime_dir}")


def _browser_environment(server_environment: dict[str, str]) -> dict[str, str]:
    """以最小白名单构造浏览器环境，拒绝继承父进程和模型连接私密字段。"""

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
        "LIVE_ATTACHMENT_E2E",
        "LIVE_ATTACHMENT_EXPECT_VISION",
        "LIVE_ATTACHMENT_SOURCE_MODEL_CONFIG_ID",
        "LIVE_ATTACHMENT_MODEL_NAME",
        "LIVE_ATTACHMENT_MODEL_DISPLAY_NAME",
        "LIVE_ATTACHMENT_MODEL_CAPABILITY_CHECKSUM",
        "LIVE_ATTACHMENT_MODEL_TEMPERATURE",
        "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS",
        "LIVE_ATTACHMENT_PROVIDER_ENDPOINT",
        "LIVE_ATTACHMENT_REQUIRE_ACCOUNT_CHECK",
        "LIVE_ATTACHMENT_CERTIFICATION_FINGERPRINT_JSON",
        "LIVE_ATTACHMENT_EVIDENCE_FILE",
        "FULLSTACK_E2E_PORT",
        "FULLSTACK_E2E_RUNTIME_DIR",
    )
    return {name: server_environment[name] for name in names if name in server_environment}


def _public_provider_endpoint(base_url: str) -> str:
    """生成只含scheme/host/port/path的审计地址，拒绝把凭据或查询参数写入证据。"""

    parsed = urlsplit(base_url)
    if parsed.username or parsed.password:
        raise RuntimeError("ModelConfig base_url 不得包含 userinfo 凭据")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("ModelConfig base_url 不是有效 HTTP(S) 地址")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _certification_fingerprints() -> dict[str, str]:
    """冻结影响附件模型认证的关键提示、解析和渲染实现源码指纹。"""

    relative_paths = (
        "backend/app/core/agent_loop.py",
        "backend/app/core/response_generator.py",
        "backend/app/dynamic_tasks/agent.py",
        "backend/app/dynamic_tasks/budget_policy.py",
        "backend/app/dynamic_tasks/planner_service.py",
        "backend/app/dynamic_tasks/action_proposer.py",
        "backend/app/dynamic_tasks/attachment_evidence.py",
        "backend/app/dynamic_tasks/result_verifier.py",
        "backend/app/session/managed_resources.py",
        "backend/app/session/provider_input_dispatch.py",
        "backend/app/session/input_extraction.py",
        "backend/app/session/input_parser_cli.py",
        "backend/app/session/input_runtime.py",
        "backend/app/llm/prompts/response_generator_prompt.md",
        "backend/app/sop_runtime/execution_store.py",
        "backend/app/dynamic_tasks/artifact_renderer.py",
        "backend/app/sop_runtime/coordinator.py",
        "backend/app/sop_runtime/definition.py",
        "backend/tests/fixtures/attachments/manifest.json",
        "backend/tests/fixtures/attachments/positive/sales_actuals.xlsx",
        "backend/tests/fixtures/attachments/negative/sales_actuals_formula_conflict.xlsx",
        "frontend-enterprise/e2e/attachment-analysis-live.fullstack.e2e.ts",
        "frontend-enterprise/e2e/start_fullstack_server.py",
        "scripts/run_live_attachment_browser_regression.py",
    )
    return {
        relative_path: sha256((ROOT / relative_path).read_bytes()).hexdigest()
        for relative_path in relative_paths
    }


def _evidence_filename(arguments: list[str]) -> str:
    """按完整套件、视觉专项或grep冒烟区分证据文件，避免后跑覆盖前跑。"""

    if os.environ.get("LIVE_ATTACHMENT_EXPECT_VISION") == "1":
        return "live-attachment-visual-report.json"
    if "--grep" in arguments or any(argument.startswith("--grep=") for argument in arguments):
        return "live-attachment-smoke-report.json"
    return "live-attachment-suite-report.json"


def _wait_for_health(port: int, server: subprocess.Popen[bytes]) -> None:
    """有界等待带密钥的隔离后端就绪，不让 Playwright 自行继承密钥启服务。"""

    deadline = time.monotonic() + 180
    health_url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"隔离全栈服务提前退出: {server.returncode}")
        try:
            with urlopen(health_url, timeout=2) as response:  # noqa: S310 - 固定本机健康地址。
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.5)
    raise RuntimeError("隔离全栈服务未在180秒内就绪")


if __name__ == "__main__":
    raise SystemExit(main())
