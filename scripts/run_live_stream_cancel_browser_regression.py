"""
@Time       : 2026/08/14 23:55
@Author     : zhanglp8181
@File       : run_live_stream_cancel_browser_regression.py
@CallChain  : 管理数据库ModelConfig → 隔离全栈服务 → Chromium取消/断连回归
@Description: 不输出密钥地启动真实模型流式回归，并在结束时关闭隔离服务。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend-enterprise"


def main() -> int:
    """从管理库取得权威默认模型，运行真实流取消与断连 Chromium 验收。"""

    model_env = _management_model_environment()
    port = int(os.environ.get("LIVE_STREAM_CANCEL_PORT", "39913"))
    runtime_dir = Path(
        os.environ.get("LIVE_STREAM_CANCEL_RUNTIME_DIR", f"/tmp/gongge-live-cancel-{port}")
    )
    child_env = os.environ.copy()
    child_env.update(model_env)
    child_env.update(
        {
            "LIVE_ATTACHMENT_E2E": "1",
            "FULLSTACK_E2E_PORT": str(port),
            "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
        }
    )
    server = subprocess.Popen(
        [
            str(BACKEND / ".venv" / "bin" / "python"),
            str(FRONTEND / "e2e" / "start_fullstack_server.py"),
        ],
        cwd=ROOT,
        env=child_env,
    )
    try:
        _wait_for_health(port, server)
        browser_env = _browser_environment(child_env)
        browser_env.update(
            {
                "BROWSER_TEST_BASE_URL": f"http://127.0.0.1:{port}",
                "BROWSER_TEST_USERNAME": "member",
                "BROWSER_TEST_PASSWORD": "member",
                "BROWSER_TEST_AGENT_ID": "agent_e2e_member_employee",
            }
        )
        completed = subprocess.run(
            ["node", "scripts/live_stream_cancel_browser_regression.mjs"],
            cwd=ROOT,
            env=browser_env,
            check=False,
        )
        return completed.returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _management_model_environment() -> dict[str, str]:
    """只读解析管理端默认模型，返回仅供子进程使用的环境字段。"""

    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ModelConfig
    from app.security.encryption import decrypt_secret

    with Session(engine) as db:
        model = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == "tenant_demo",
                ModelConfig.enabled == True,  # noqa: E712 - SQLModel布尔表达式。
                ModelConfig.is_default == True,  # noqa: E712 - 管理端默认记录为权威来源。
                ModelConfig.preflight_status == "ready",
            )
        ).first()
        if model is None:
            raise RuntimeError("tenant_demo没有已启用且通过预检的默认ModelConfig")
        api_key = decrypt_secret(model.api_key_encrypted)
        if not api_key:
            raise RuntimeError("管理端默认ModelConfig的密钥为空")
        return {
            "LIVE_ATTACHMENT_MODEL_API_KEY": api_key,
            "LIVE_ATTACHMENT_MODEL_BASE_URL": str(model.base_url or "").strip(),
            "LIVE_ATTACHMENT_MODEL_NAME": str(model.model or "").strip(),
            "LIVE_ATTACHMENT_MODEL_DISPLAY_NAME": str(model.name),
            "LIVE_ATTACHMENT_MODEL_EXTRA_BODY_JSON": json.dumps(
                dict(model.extra_body_json or {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "LIVE_ATTACHMENT_MODEL_TEMPERATURE": str(float(model.temperature)),
            "LIVE_ATTACHMENT_MODEL_MAX_OUTPUT_TOKENS": str(int(model.max_output_tokens)),
        }


def _browser_environment(server_environment: dict[str, str]) -> dict[str, str]:
    """以运行Node/Chromium所需最小白名单构造环境，不复制模型或父进程秘密。"""

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
    return {name: server_environment[name] for name in names if name in server_environment}


def _wait_for_health(port: int, server: subprocess.Popen[bytes]) -> None:
    """有界等待隔离全栈健康，服务提前退出时立即报错。"""

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
