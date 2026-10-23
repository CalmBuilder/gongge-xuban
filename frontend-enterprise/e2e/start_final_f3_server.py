"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : start_final_f3_server.py
@CallChain  : F3独立启动 → 自有SQLite/前置资格 → 原single_port_app生命周期与真实SDK
@Description: 冻结隔离启动计划，缺真实资格/适配时不加载应用；不安装固定回答或本地provider。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from types import MappingProxyType
from urllib.parse import urlsplit

from final_f3_preparation import PreparationError


VENDOR_ADAPTERS: dict[str, Callable] = {}
_MARKER = ".f3-owned-runtime.json"
_MODEL_KEYS = frozenset({
    "DEMO_MODEL_BASE_URL", "DEMO_MODEL_NAME", "DEMO_MODEL_API_KEY",
    "MODEL_API_TIMEOUT_SECONDS", "MODEL_THINKING_MODE", "MODEL_THINKING_MODELS",
})
_CONFIGURATION_KEYS = _MODEL_KEYS | {
    "DYNAMIC_TASK_EXTERNAL_WRITE_TENANT_ALLOWLIST",
    "DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST", "TOOL_OUTBOUND_ALLOWED_HOSTS",
}


def _digest(value: object) -> str:
    """只对规范数据作摘要，不公开私有配置正文。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _private_directory(path: Path) -> None:
    """要求当前UID拥有700目录且无符号链接替身，不接受靠目录名冒充所有权。"""
    if not path.is_absolute():
        raise PreparationError("F3_PRIVATE_OWNED_DIRECTORY_REQUIRED")
    try:
        stat = path.stat()
        invalid = (not path.is_dir() or path.resolve() != path
                   or stat.st_uid != os.getuid() or stat.st_mode & 0o077)
    except OSError as exc:
        raise PreparationError("F3_PRIVATE_OWNED_DIRECTORY_REQUIRED") from exc
    if invalid:
        raise PreparationError("F3_PRIVATE_OWNED_DIRECTORY_REQUIRED")


def _private_json(path: Path, code: str) -> dict:
    """读取本地600对象而不回显解析错误、路径或私有值，符号链接同样拒绝。"""
    try:
        stat = path.stat()
        if (not path.is_file() or path.is_symlink() or stat.st_uid != os.getuid()
                or stat.st_mode & 0o077):
            raise PreparationError(code)
        result = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PreparationError(code) from exc
    if not isinstance(result, dict):
        raise PreparationError(code)
    return result


def _owned_marker(runtime: Path) -> dict:
    """仅识别本入口随机生成的自有SQLite根和私有标记，不删除任何已有数据。"""
    _private_directory(runtime)
    marker = _private_json(runtime / _MARKER, "F3_OWNED_RUNTIME_MARKER_REQUIRED")
    if (not runtime.name.startswith("f3-sqlite-")
            or marker.get("schema_version") != "final-f3-owned-sqlite-v1"
            or marker.get("runtime_name") != runtime.name
            or marker.get("owner_uid") != os.getuid()
            or not isinstance(marker.get("app_secret"), str)
            or len(marker["app_secret"]) < 32):
        raise PreparationError("F3_OWNED_RUNTIME_MARKER_REQUIRED")
    return marker


def _owned_outputs(runtime: Path, *, reuse: bool) -> None:
    """在计划与最终启动两处重核SQLite和三个可写根，拒绝计划后的路径替换。"""
    database = runtime / "journey.sqlite3"
    if database.exists() or database.is_symlink():
        if (not database.is_file() or database.is_symlink()
                or database.stat().st_uid != os.getuid() or database.stat().st_mode & 0o077):
            raise PreparationError("F3_PRIVATE_OWNED_DATABASE_REQUIRED")
        if not reuse:
            raise PreparationError("F3_EXISTING_JOURNEY_REQUIRES_REUSE")
    elif reuse:
        raise PreparationError("F3_EXISTING_DATABASE_REQUIRED_FOR_REUSE")
    for directory in ("user-data", "managed-workspaces", "general-skill-objects"):
        target = runtime / directory
        if target.is_symlink():
            raise PreparationError("F3_OWNED_DATA_PATH_REQUIRED")
        if target.exists():
            try:
                _private_directory(target)
            except PreparationError as exc:
                raise PreparationError("F3_OWNED_DATA_PATH_REQUIRED") from exc


@dataclass(frozen=True)
class StartPlan:
    """不可变e2e启动计划；秘密环境和适配函数不进入repr或公开摘要。"""

    runtime: Path = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    marker_sha256: str
    profile_sha256: str
    qualification_evidence_sha256: str
    adapter_id: str
    adapter: Callable = field(repr=False)
    port: int
    reuse: bool

    def safe_summary(self) -> dict[str, object]:
        """只报告计划指纹与边界，不由启动准备宣称旅程或I1已通过。"""
        visible_configuration = {
            key: value for key, value in self.environment.items()
            if key in _CONFIGURATION_KEYS and key != "DEMO_MODEL_API_KEY"
        }
        return {
            "phase": "I1_server_preparation", "plan_ready": True,
            "I1_passed": False, "JRN08_passed": False,
            "database_dialect": "sqlite", "runtime_sha256": _digest(str(self.runtime)),
            "configuration_sha256": _digest(visible_configuration),
            "profile_sha256": self.profile_sha256,
            "qualification_evidence_sha256": self.qualification_evidence_sha256,
            "adapter_id_sha256": _digest(self.adapter_id), "reuse": self.reuse,
            "database_connected": False, "server_started": False,
            "previous_starter_or_local_provider_loaded": False,
        }


def reserve_sqlite_runtime(parent: Path) -> Path:
    """仅在私有自有父目录保留随机SQLite运行根；不得删除已有旅程数据。"""
    _private_directory(parent)
    runtime = Path(tempfile.mkdtemp(prefix="f3-sqlite-", dir=parent))
    runtime.chmod(0o700)
    marker = {"schema_version": "final-f3-owned-sqlite-v1", "runtime_name": runtime.name,
              "owner_uid": os.getuid(), "app_secret": secrets.token_urlsafe(48)}
    with (runtime / _MARKER).open("x") as handle:
        os.chmod(handle.name, 0o600)
        json.dump(marker, handle)
    return runtime


def build_start_plan(*, runtime: Path, configuration: dict, profile_sha256: str,
                     qualification: dict | None, adapter_id: str | None,
                     port: int = 5166, reuse: bool = False) -> StartPlan:
    """纯数据检查真实资格与适配来源；缺项时必须在导入应用或触发数据库前拒绝。"""
    marker = _owned_marker(runtime)
    if (not isinstance(configuration, dict) or configuration.keys() - _CONFIGURATION_KEYS
            or any(not isinstance(value, str) for value in configuration.values())):
        raise PreparationError("F3_SERVER_CONFIGURATION_FIELD_UNSUPPORTED")
    if (not isinstance(qualification, dict)
            or qualification.get("schema_version") != "final-f3-vendor-qualification-v1"
            or qualification.get("real_provider_qualified") is not True):
        raise PreparationError("F3_REAL_VENDOR_QUALIFICATION_REQUIRED")
    if (not re.fullmatch(r"[a-f0-9]{64}", str(profile_sha256))
            or qualification.get("profile_sha256") != profile_sha256):
        raise PreparationError("F3_VENDOR_QUALIFICATION_PROFILE_CHANGED")
    evidence_hash = qualification.get("qualification_evidence_sha256")
    if not isinstance(evidence_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_hash):
        raise PreparationError("F3_REAL_VENDOR_QUALIFICATION_REQUIRED")
    if (not isinstance(adapter_id, str) or adapter_id not in VENDOR_ADAPTERS
            or not callable(VENDOR_ADAPTERS[adapter_id])):
        raise PreparationError("F3_VENDOR_ADAPTER_NOT_IMPLEMENTED")
    required = ("DEMO_MODEL_BASE_URL", "DEMO_MODEL_NAME", "DEMO_MODEL_API_KEY",
                "DYNAMIC_TASK_EXTERNAL_WRITE_TENANT_ALLOWLIST",
                "DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST", "TOOL_OUTBOUND_ALLOWED_HOSTS")
    if any(not configuration.get(key, "").strip() for key in required):
        raise PreparationError("F3_EXPLICIT_MODEL_AND_WRITE_SCOPE_REQUIRED")
    try:
        endpoint = urlsplit(configuration["DEMO_MODEL_BASE_URL"])
        endpoint.port
    except ValueError as exc:
        raise PreparationError("F3_MODEL_ENDPOINT_INVALID") from exc
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise PreparationError("F3_MODEL_ENDPOINT_INVALID")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise PreparationError("F3_OWNED_LISTEN_PORT_REQUIRED")
    _owned_outputs(runtime, reuse=reuse)
    database = runtime / "journey.sqlite3"
    environment = {
        **configuration, "APP_ENV": "test", "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
        "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(port), "SINGLE_PORT": "true",
        "AUTO_RESTART": "false", "APP_SECRET": marker["app_secret"],
        "DATABASE_URL": f"sqlite:///{database}",
        "GONGGE_XUBAN_DOTENV": str(runtime / "isolated.env"),
        "GONGGE_XUBAN_DATA_DIR": str(runtime / "user-data"),
        "DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT": str(runtime / "managed-workspaces"),
        "GENERAL_SKILL_OBJECT_STORE_PATH": str(runtime / "general-skill-objects"),
        "PUBLIC_MOCK_API_KEY": secrets.token_urlsafe(32), "PUBLIC_MOCK_LLM_ENABLED": "false",
        "TOOL_BASE_URL": "https://f3-unconfigured.invalid",
        "DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED": "true",
        "DYNAMIC_TASK_STANDING_APPROVAL_ENABLED": "false",
        "DYNAMIC_TASK_DESTRUCTIVE_ENABLED": "false", "GENERAL_SKILL_NETWORK_INSTALL": "false",
        "GENERAL_SKILL_RUNTIME_AUTO_INSTALL": "false",
        "CORS_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
    }
    return StartPlan(
        runtime, MappingProxyType(environment), _digest(marker), profile_sha256, evidence_hash,
        adapter_id, VENDOR_ADAPTERS[adapter_id], port, reuse,
    )


def start_owned_server(plan: StartPlan) -> None:
    """仅执行已绑定的自有启动计划；真实适配尚缺时禁止进入原服务。"""
    if VENDOR_ADAPTERS.get(plan.adapter_id) is not plan.adapter:
        raise PreparationError("F3_VENDOR_ADAPTER_CHANGED")
    if _digest(_owned_marker(plan.runtime)) != plan.marker_sha256:
        raise PreparationError("F3_OWNED_RUNTIME_MARKER_CHANGED")
    if plan.environment["DATABASE_URL"] != f"sqlite:///{plan.runtime}/journey.sqlite3":
        raise PreparationError("F3_PRIVATE_OWNED_DATABASE_REQUIRED")
    _owned_outputs(plan.runtime, reuse=plan.reuse)
    _run_original_application(plan)


def _run_original_application(plan: StartPlan) -> None:
    """未来完整适配后仅进入原应用和原SDK，不创建第二Runtime或成功fixture。"""
    if any(name in sys.modules for name in ("app.config", "app.db", "single_port_app")):
        raise PreparationError("F3_FRESH_ISOLATED_SERVER_PROCESS_REQUIRED")
    baseline = {key: os.environ[key] for key in
                ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE")
                if key in os.environ}
    os.umask(0o077)
    dotenv = plan.runtime / "isolated.env"
    if dotenv.exists():
        if dotenv.is_symlink() or dotenv.read_bytes() != b"" or dotenv.stat().st_mode & 0o077:
            raise PreparationError("F3_EMPTY_OWNED_DOTENV_REQUIRED")
    else:
        with dotenv.open("x"):
            pass
    os.environ.clear()
    os.environ.update(baseline)
    os.environ.update(plan.environment)
    os.chdir(plan.runtime)
    backend = Path(__file__).resolve().parents[2] / "backend"
    sys.path.insert(0, str(backend))

    from contextlib import asynccontextmanager

    from final_f3_sdk_ledger import SdkLedger
    from final_f3_sdk_handoff import sdk_thread_handoff
    from app.llm import client as llm_client

    original_factory = llm_client.OpenAI
    sdk_ledger = SdkLedger(plan.runtime / f"sdk-observations-{os.getpid()}.safe.jsonl")

    def observed_factory(*args, **kwargs):
        """原OpenAI构造器与参数完整保留；只附加透传观察，不安装固定回答。"""
        return sdk_ledger.observe(original_factory(*args, **kwargs))

    llm_client.OpenAI = observed_factory
    import uvicorn
    from single_port_app import app

    app.state.f3_sdk_observation_ledger = sdk_ledger

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def owned_lifespan(application):
        """原初始化/worker/关停完整执行；其后才准备已核供应商的自有测试资源。"""
        async with original_lifespan(application) as state:
            plan.adapter(plan, application)
            yield state

    app.router.lifespan_context = owned_lifespan
    with sdk_thread_handoff(llm_client):
        uvicorn.run(app, host="127.0.0.1", port=plan.port, log_level="warning", workers=1)


def main() -> int:
    """私有文件先生成安全启动计划；无已注册真实适配时拒绝，不接收密钥参数。"""
    parser = argparse.ArgumentParser(description="F3自有SQLite启动准备；当前真实vendor adapter待实现")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--adapter")
    parser.add_argument("--port", type=int, default=5166)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        plan = build_start_plan(
            runtime=args.runtime,
            configuration=_private_json(args.configuration, "F3_PRIVATE_CONFIGURATION_REQUIRED"),
            profile_sha256=args.profile_sha256,
            qualification=_private_json(args.qualification, "F3_REAL_VENDOR_QUALIFICATION_REQUIRED"),
            adapter_id=args.adapter, port=args.port, reuse=args.reuse,
        )
        if args.execute:
            start_owned_server(plan)
        else:
            print(json.dumps(plan.safe_summary(), ensure_ascii=False))
        return 0
    except PreparationError as exc:
        print(exc.code)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
