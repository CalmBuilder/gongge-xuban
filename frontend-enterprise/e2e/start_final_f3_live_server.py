"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : start_final_f3_live_server.py
@CallChain  : 自有F3守护进程 → 私有配置与真实SDK → 原应用生命周期/原两分支
@Description: 独立模拟业务来源的真实模型入口；为指定诊断启用持久SDK预算，不装固定模型。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit


class LiveConfigurationError(ValueError):
    """只返回可公开类别，不带配置、账号、端点或密钥。"""


class F3SessionListRequestProbe:
    """测试启动时记录会话列表请求边界，并对持续挂起输出无局部变量线程栈。"""

    def __init__(self, app: Callable[..., Awaitable[None]], runtime: Path,
                 *, stack_after_seconds: float = 10.0) -> None:
        """绑定自有私密目录和原ASGI应用，不保存任何请求身份或查询参数。"""
        self.app = app
        self.runtime = runtime
        self.stack_after_seconds = max(0.01, min(stack_after_seconds, 60.0))
        self.journal = runtime / "session-metadata-diagnostic.safe.jsonl"

    def _append(self, event: str, elapsed_ms: float | None = None,
                status: int | None = None) -> None:
        """只追加固定事件、耗时和HTTP状态，隔离损坏文件并避免影响原请求。"""
        if event not in {"request_start", "request_pending", "response_start", "response_complete"}:
            return
        row: dict[str, int | float | str | None] = {"event": event}
        if elapsed_ms is not None:
            row["elapsed_ms"] = round(min(max(elapsed_ms, 0.0), 120_000.0), 3)
        if status is not None:
            row["status"] = status if 100 <= status <= 599 else None
        try:
            descriptor = os.open(
                self.journal,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    return
                os.write(descriptor, (json.dumps(row, sort_keys=True) + "\n").encode())
            finally:
                os.close(descriptor)
        except OSError:
            return

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[None]]) -> None:
        """仅探测原会话列表GET；其他请求透明透传，超时栈不含局部变量。"""
        if (scope.get("type") != "http" or scope.get("method") != "GET"
                or scope.get("path") != "/api/chat/sessions"):
            await self.app(scope, receive, send)
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        completed = asyncio.Event()
        status: int | None = None
        self._append("request_start")

        async def pending_stack() -> None:
            """若原路由持续挂起，写固定标记并转储进程线程栈供私有诊断。"""
            await asyncio.sleep(self.stack_after_seconds)
            if completed.is_set():
                return
            elapsed = (loop.time() - started) * 1000
            self._append("request_pending", elapsed_ms=elapsed)
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except (OSError, RuntimeError, ValueError):
                return

        watcher = asyncio.create_task(pending_stack())

        async def observed_send(message: dict[str, Any]) -> None:
            """记录ASGI响应头和末段，只保留HTTP状态与相对耗时。"""
            nonlocal status
            now_elapsed = (loop.time() - started) * 1000
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                status = candidate if type(candidate) is int else None
                self._append("response_start", elapsed_ms=now_elapsed, status=status)
            elif message.get("type") == "http.response.body" and not message.get("more_body", False):
                self._append("response_complete", elapsed_ms=now_elapsed, status=status)
                completed.set()
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            completed.set()
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass


def digest(value: object) -> str:
    """规范JSON摘要冻结实际私有配置，不输出配置正文。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preserved_server_diagnostic_flags(environment: Mapping[str, str]) -> dict[str, str]:
    """只保留已登记的F3诊断开关，阻断其他父进程变量进入应用。"""
    keys = ("F3_DIAGNOSTIC_SESSION_STACK", "F3_DIAGNOSTIC_DUMP_STACK")
    return {key: "1" for key in keys if environment.get(key) == "1"}


def preserved_model_config_fingerprint(
    environment: Mapping[str, str], *, required: bool = False,
) -> dict[str, str]:
    """只传递登记器冻结的模型配置摘要，拒绝缺失或格式错误的SOP诊断身份。"""
    name = "F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256"
    value = environment.get(name)
    if value is None:
        if required:
            raise LiveConfigurationError("F3_MODEL_CONFIG_FROZEN_FINGERPRINT_REQUIRED")
        return {}
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise LiveConfigurationError("F3_MODEL_CONFIG_FROZEN_FINGERPRINT_INVALID")
    return {name: value}


def register_diagnostic_stack_dump(environment: Mapping[str, str]) -> bool:
    """仅在隔离单例诊断中注册SIGUSR1线程栈转储，不附带局部变量。"""
    diagnostic_signal = getattr(signal, "SIGUSR1", None)
    if environment.get("F3_DIAGNOSTIC_DUMP_STACK") != "1" or diagnostic_signal is None:
        return False
    try:
        faulthandler.register(
            diagnostic_signal,
            file=sys.stderr,
            all_threads=True,
            chain=False,
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def read_private(path: Path) -> dict:
    """要求当前UID私有单链接普通文件，不跟随私有配置链接。"""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise LiveConfigurationError("F3_LIVE_PRIVATE_FILE_REQUIRED")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LiveConfigurationError("F3_LIVE_PRIVATE_OBJECT_REQUIRED") from exc
    if not isinstance(value, dict):
        raise LiveConfigurationError("F3_LIVE_PRIVATE_OBJECT_REQUIRED")
    return value


@dataclass(frozen=True)
class LivePlan:
    """仅持有模拟来源启动环境；秘密不进入repr或安全摘要。"""

    runtime: Path = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    run_id: str
    port: int
    reuse: bool
    configuration_sha256: str

    def safe_summary(self) -> dict:
        """环境只给出键名与完整摘要，不由准备宣称浏览器或供应商通过。"""
        return {"run_id": self.run_id, "port": self.port,
                "configuration_sha256": self.configuration_sha256,
                "environment_keys": sorted(self.environment), "reuse": self.reuse,
                "provider_kind": "simulated_async_service", "real_vendor_qualified": False,
                "real_model_requested": True, "JRN08_passed": False}


def build_live_plan(runtime: Path, configuration: dict, *, reuse: bool = False) -> LivePlan:
    """固定自有测试环境和模拟来源；启用安全Skill导入供真实页面验证，不消除供应商未验项。"""
    required = {"schema_version", "run_id", "port", "provider_kind", "provider_base_url",
                "provider_namespace", "provider_key", "provider_control_key", "model_base_url",
                "model_name", "model_api_key", "model_temperature", "model_max_tokens",
                "model_extra_body", "dialect", "database_url", "initiator_username",
                "reviewer_username", "initiator_password", "reviewer_password", "manual_labels"}
    optional = {"fixture_spec_path", "model_timeout_seconds", "frontend_dist_path", "login_tenant_id"}
    if not isinstance(configuration, dict) or not required <= configuration.keys() \
            or configuration.keys() - required - optional:
        raise LiveConfigurationError("F3_LIVE_REGISTERED_FIELDS_REQUIRED")
    info = runtime.lstat()
    if runtime.is_symlink() or runtime.resolve() != runtime or not runtime.name.startswith("f3-live-") \
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LiveConfigurationError("F3_LIVE_PRIVATE_OWNED_RUNTIME_REQUIRED")
    if configuration["schema_version"] != "f3-live-simulated-v1" \
            or configuration["provider_kind"] != "simulated_async_service":
        raise LiveConfigurationError("F3_LIVE_SIMULATED_SOURCE_REQUIRED")
    run_id, port = configuration["run_id"], configuration["port"]
    if not isinstance(run_id, str) or not re.fullmatch(r"f3-[a-z0-9-]{3,90}", run_id) \
            or type(port) is not int or not 1024 <= port <= 65535:
        raise LiveConfigurationError("F3_LIVE_OWNED_LISTENER_REQUIRED")
    try:
        provider = urlsplit(configuration["provider_base_url"])
        model = urlsplit(configuration["model_base_url"])
        valid_provider = provider.scheme == "http" and provider.hostname == "127.0.0.1" \
            and provider.port is not None and 1024 <= provider.port <= 65535 \
            and provider.port != port and provider.path in {"", "/"}
        valid_model = model.scheme in {"http", "https"} and bool(model.hostname) \
            and model.hostname not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
        if not valid_provider or not valid_model or any(
            endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            for endpoint in (provider, model)
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise LiveConfigurationError("F3_LIVE_SOURCE_ENDPOINT_INVALID") from exc
    namespace = configuration["provider_namespace"]
    if not isinstance(namespace, str) or not re.fullmatch(r"f3-sim-[0-9a-f]{16}", namespace):
        raise LiveConfigurationError("F3_LIVE_SIMULATION_NAMESPACE_REQUIRED")
    for key in ("provider_key", "provider_control_key", "model_api_key"):
        if not isinstance(configuration[key], str) or len(configuration[key]) < 16:
            raise LiveConfigurationError("F3_LIVE_EXPLICIT_PRIVATE_AUTH_REQUIRED")
    if configuration["provider_key"] == configuration["provider_control_key"]:
        raise LiveConfigurationError("F3_LIVE_SEPARATE_CONTROL_AUTH_REQUIRED")
    for key in ("model_name", "initiator_username", "reviewer_username",
                "initiator_password", "reviewer_password"):
        if not isinstance(configuration[key], str) or not configuration[key].strip():
            raise LiveConfigurationError("F3_LIVE_MODEL_AND_ACTORS_REQUIRED")
    if configuration["initiator_username"] == configuration["reviewer_username"]:
        raise LiveConfigurationError("F3_LIVE_SECOND_MANAGER_REQUIRED")
    if type(configuration["model_temperature"]) not in {int, float} \
            or not 0 <= configuration["model_temperature"] <= 2 \
            or type(configuration["model_max_tokens"]) is not int \
            or not 1 <= configuration["model_max_tokens"] <= 65536 \
            or not isinstance(configuration["model_extra_body"], dict) \
            or not isinstance(configuration["manual_labels"], dict):
        raise LiveConfigurationError("F3_LIVE_FROZEN_MODEL_AND_LABELS_REQUIRED")
    timeout = configuration.get("model_timeout_seconds", 180)
    if type(timeout) not in {int, float} or not 5 <= timeout <= 600:
        raise LiveConfigurationError("F3_LIVE_MODEL_DEADLINE_INVALID")
    dialect = configuration["dialect"]
    database = runtime / "journey.sqlite3"
    database_url = configuration["database_url"]
    if dialect == "sqlite":
        if database_url not in {None, f"sqlite:///{database}"}:
            raise LiveConfigurationError("F3_LIVE_OWNED_DATABASE_REQUIRED")
        database_url = f"sqlite:///{database}"
    elif dialect == "mysql":
        try:
            parsed = urlsplit(database_url)
            if parsed.scheme != "mysql+pymysql" or parsed.hostname != "127.0.0.1" \
                    or not parsed.port or parsed.port == 3306 or not parsed.username \
                    or not parsed.username.startswith("st_test_") or not parsed.password \
                    or not parsed.path.startswith("/gongge_xuban_test_") or parsed.fragment:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise LiveConfigurationError("F3_LIVE_RANDOM_MINIMAL_MYSQL_REQUIRED") from exc
    else:
        raise LiveConfigurationError("F3_LIVE_DIALECT_UNSUPPORTED")
    fixture = Path(configuration.get("fixture_spec_path", str(runtime / "fixture.private.json")))
    fixture_sha = None
    fixture_spec = {}
    if "fixture_spec_path" in configuration:
        if fixture.parent != runtime:
            raise LiveConfigurationError("F3_LIVE_OWNED_FIXTURE_SPEC_REQUIRED")
        fixture_spec = read_private(fixture)
        fixture_sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
    frozen_sha = digest({"configuration": configuration, "fixture_sha256": fixture_sha})
    marker_path = runtime / ".f3-live-owned.private.json"
    if reuse:
        marker = read_private(marker_path)
        if marker.get("run_id") != run_id or marker.get("configuration_sha256") != frozen_sha \
                or marker.get("uid") != os.getuid():
            raise LiveConfigurationError("F3_LIVE_RESTART_CONFIGURATION_CHANGED")
        if dialect == "sqlite":
            database_info = database.lstat()
            if not stat.S_ISREG(database_info.st_mode) or database_info.st_uid != os.getuid() \
                    or database_info.st_mode & 0o077 or database_info.st_nlink != 1:
                raise LiveConfigurationError("F3_LIVE_RESTART_OWNED_DATABASE_REQUIRED")
    else:
        if marker_path.exists() or marker_path.is_symlink() or database.exists() or database.is_symlink():
            raise LiveConfigurationError("F3_LIVE_EXISTING_JOURNEY_REQUIRES_REUSE")
        marker = {"uid": os.getuid(), "run_id": run_id, "configuration_sha256": frozen_sha,
                  "app_secret": secrets.token_urlsafe(48)}
        with marker_path.open("x") as target:
            target.write(json.dumps(marker))
        marker_path.chmod(0o600)
    dotenv = runtime / "isolated.env"
    if dotenv.exists():
        if dotenv.is_symlink() or dotenv.read_bytes():
            raise LiveConfigurationError("F3_LIVE_EMPTY_OWNED_DOTENV_REQUIRED")
    else:
        dotenv.touch(mode=0o600)
    environment = {
        "APP_ENV": "test", "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
        "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(port), "SINGLE_PORT": "true",
        "AUTO_RESTART": "false", "APP_SECRET": marker["app_secret"],
        "DATABASE_URL": database_url, "GONGGE_XUBAN_DOTENV": str(dotenv),
        "GONGGE_XUBAN_DATA_DIR": str(runtime / "user-data"),
        "DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT": str(runtime / "managed-workspaces"),
        "GENERAL_SKILL_OBJECT_STORE_PATH": str(runtime / "general-skill-objects"),
        "PUBLIC_MOCK_API_KEY": secrets.token_urlsafe(32), "PUBLIC_MOCK_LLM_ENABLED": "false",
        "DEMO_MODEL_BASE_URL": configuration["model_base_url"],
        "DEMO_MODEL_NAME": configuration["model_name"],
        "DEMO_MODEL_API_KEY": configuration["model_api_key"],
        "MODEL_API_TIMEOUT_SECONDS": str(timeout),
        "TOOL_BASE_URL": configuration["provider_base_url"], "TOOL_OUTBOUND_ALLOWED_HOSTS": "127.0.0.1",
        "DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED": "true", "DYNAMIC_TASK_STANDING_APPROVAL_ENABLED": "false",
        "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD": "100",
        "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD": "25",
        "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD": "25",
        "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD": "100",
        "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS": "3600",
        "DYNAMIC_TASK_DESTRUCTIVE_ENABLED": "false", "GENERAL_SKILL_NETWORK_INSTALL": "false",
        "GENERAL_SKILL_IMPORT_V2_ENABLED": "true",
        "GENERAL_SKILL_RUNTIME_AUTO_INSTALL": "false",
        "CORS_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "FULLSTACK_FINAL_F3_LIVE": "1", "F3_LIVE_RUNTIME": str(runtime),
        "F3_LIVE_FIXTURE_SPEC_PATH": str(fixture), "FULLSTACK_E2E_RUNTIME_DIR": str(runtime),
        "FULLSTACK_E2E_RUN_ID": run_id,
    }
    if re.fullmatch(r"f3-i3-live-sqlite-memory-dta-freeze-capture-diagnostic-[0-9]+", run_id):
        environment["F3_MODEL_BUDGET_PROFILE"] = "memory-dta-freeze-v1"
    if os.environ.get("F3_DIAGNOSTIC_SELECTION") == "sop-first-three":
        if dialect != "sqlite":
            raise LiveConfigurationError("F3_SOP_FIRST_THREE_MODEL_BUDGET_SQLITE_ONLY")
        environment["F3_MODEL_BUDGET_PROFILE"] = "i2-sop-first-three-v1"
    environment.update(preserved_model_config_fingerprint(
        os.environ,
        required=os.environ.get("F3_DIAGNOSTIC_SELECTION") == "sop-first-three",
    ))
    if fixture_spec:
        try:
            tenant_id = fixture_spec["tenant"]["id"]
            agent_id = fixture_spec["agents"]["DTA"]["id"]
            if configuration.get("login_tenant_id", tenant_id) != tenant_id:
                raise ValueError("F3_LIVE_FRONTEND_TENANT_MISMATCH")
            if not isinstance(tenant_id, str) or not tenant_id \
                    or not isinstance(agent_id, str) or not agent_id:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveConfigurationError("F3_LIVE_EXACT_WRITE_SCOPE_REQUIRED") from exc
        environment["DYNAMIC_TASK_EXTERNAL_WRITE_TENANT_ALLOWLIST"] = tenant_id
        environment["DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST"] = agent_id
    if "frontend_dist_path" in configuration:
        environment["F3_LIVE_FRONTEND_DIST"] = str(configuration["frontend_dist_path"])
    return LivePlan(runtime, MappingProxyType(environment), run_id, port, reuse, frozen_sha)


def run_application(plan: LivePlan) -> None:
    """在新自有进程沿原初始化、worker和关停启动，只附加真实SDK旁证。"""
    if any(name in sys.modules for name in ("app.config", "app.db", "single_port_app")):
        raise LiveConfigurationError("F3_LIVE_FRESH_PROCESS_REQUIRED")
    os.umask(0o077)
    diagnostic_flags = preserved_server_diagnostic_flags(os.environ)
    baseline = {key: os.environ[key] for key in
                ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE")
                if key in os.environ}
    os.environ.clear()
    os.environ.update(baseline)
    os.environ.update(plan.environment)
    os.environ.update(diagnostic_flags)
    register_diagnostic_stack_dump(diagnostic_flags)
    os.chdir(plan.runtime)
    backend = Path(__file__).resolve().parents[2] / "backend"
    sys.path.insert(0, str(backend))
    from final_f3_sdk_handoff import sdk_thread_handoff
    from final_f3_sdk_ledger import SdkLedger
    from final_f3_live_identity import LiveWireObserver, live_caller_bindings
    from app.llm import client as llm_client

    ledger = SdkLedger(plan.runtime / f"sdk-observations-{os.getpid()}.safe.jsonl")
    spec = read_private(Path(plan.environment["F3_LIVE_FIXTURE_SPEC_PATH"]))
    wire_observer = LiveWireObserver(plan.runtime, {
        "knowledge_old": spec["knowledge"]["old_content_md"],
        "knowledge_new": spec["knowledge"]["new_content_md"],
        "memories": [row["content"] for row in spec["memories"]],
    })
    memory_observer = None
    memory_context = nullcontext()
    if "memory_quality" in spec:
        from final_f3_live_memory import MemoryWireObserver, memory_dispatch_hook

        memory_observer = MemoryWireObserver(plan.runtime)
        memory_context = memory_dispatch_hook(llm_client, spec, memory_observer)
    original_factory = llm_client.OpenAI

    def observed_factory(*args, **kwargs):
        """原SDK构造和每次请求完整透传；从原usage写安全旁证。"""
        client = original_factory(*args, **kwargs)
        if memory_observer is not None:
            client = memory_observer.observe(client)
        return ledger.observe(wire_observer.observe(client))

    llm_client.OpenAI = observed_factory
    import uvicorn
    import single_port_app
    from single_port_app import app
    from final_f3_live_fixture import install_api, prepare_live

    app.state.f3_sdk_observation_ledger = ledger
    app.state.f3_live_wire_observer = wire_observer
    if memory_observer is not None:
        app.state.f3_live_memory_observer = memory_observer
    if "F3_LIVE_FRONTEND_DIST" in plan.environment:
        from starlette.staticfiles import StaticFiles

        frontend = Path(plan.environment["F3_LIVE_FRONTEND_DIST"]).resolve()
        if not (frontend / "index.html").is_file():
            raise LiveConfigurationError("F3_LIVE_FROZEN_FRONTEND_REQUIRED")
        single_port_app.ENTERPRISE_DIST = frontend
        for route in app.routes:
            if isinstance(getattr(route, "app", None), StaticFiles) \
                    and str(getattr(route, "path", "")).endswith("/assets"):
                route.app.directory = str(frontend / "assets")
                route.app.all_directories = [str(frontend / "assets")]
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def live_lifespan(application):
        """原生命周期初始化后只准备合成资源，重启不重种实例或业务事实。"""
        try:
            async with original_lifespan(application) as state:
                prepare_live(plan.runtime, reuse=plan.reuse)
                install_api(application, plan.runtime)
                yield state
        finally:
            final_health = plan.runtime / f"sdk-final-health-{os.getpid()}.safe.json"
            health = {"pid": os.getpid(), "original_sdk": ledger.health(),
                      "wire_inputs": wire_observer.health(),
                      "provider_kind": "simulated_async_service", "real_vendor_qualified": False}
            if memory_observer is not None:
                health["memory_inputs"] = memory_observer.health()
            final_health.write_text(json.dumps(health))
            final_health.chmod(0o600)

    app.router.lifespan_context = live_lifespan
    served_app = app
    if os.environ.get("F3_DIAGNOSTIC_SESSION_STACK") == "1":
        served_app = F3SessionListRequestProbe(app, plan.runtime)
    with sdk_thread_handoff(llm_client), live_caller_bindings(plan.runtime, spec), memory_context:
        uvicorn.run(served_app, host="127.0.0.1", port=plan.port, log_level="warning", workers=1)


def main() -> int:
    """私有文件为唯一配置入口，命令行不接收模型Key、JWT或数据库URL。"""
    if os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1":
        raise LiveConfigurationError("F3_LIVE_EXPLICIT_TEST_SCOPE_REQUIRED")
    runtime = Path(os.environ["F3_LIVE_RUNTIME"])
    configuration = read_private(Path(os.environ["F3_LIVE_CONFIGURATION_PATH"]))
    plan = build_live_plan(runtime, configuration,
                           reuse=os.environ.get("F3_LIVE_REUSE") == "1")
    run_application(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
