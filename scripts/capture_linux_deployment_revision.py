"""
@Time       : 2026/08/25
@Author     : zhanglp8181
@File       : capture_linux_deployment_revision.py
@CallChain  : 当前Linux app.sh → health/端口/Alembic → 脱敏部署revision证据
@Description: 记录当前开发机部署的可复现源码、进程、健康和迁移指纹，不把未提交工作树冒充正式发布版本。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "manuals" / "evidence"
TRACKED_PATHS = (
    "backend/app/dynamic_tasks/agent.py",
    "backend/app/dynamic_tasks/planner_service.py",
    "backend/app/dynamic_tasks/result_verifier.py",
    "backend/app/llm/client.py",
    "backend/app/core/agent_loop.py",
    "frontend-enterprise/src",
    "app.sh",
)


def main() -> int:
    """采集本机Linux服务的脱敏部署证据并按健康状态返回退出码。"""

    args = _parse_args()
    port = int(args.port)
    health = _health(port)
    status = _run(("./app.sh", "status"), timeout=30)
    migration = _run(("backend/.venv/bin/alembic", "-c", "backend/alembic.ini", "current"), timeout=30)
    heads = _run(("backend/.venv/bin/alembic", "-c", "backend/alembic.ini", "heads"), timeout=30)
    git_head = _run(("git", "rev-parse", "HEAD"), timeout=10)
    git_status = _run(("git", "status", "--short"), timeout=10)
    tracked_digest = _tracked_digest()
    local_health_passed = (
        health["status"] == 200
        and status["returncode"] == 0
        and migration["returncode"] == 0
        and heads["returncode"] == 0
    )
    working_tree_clean = not bool(git_status["stdout"].strip())
    report = {
        "schema_version": "linux-deployment-revision-v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "platform": {"system": os.uname().sysname, "release": os.uname().release,
                      "machine": os.uname().machine, "python": _run(("python3", "--version"), timeout=10)["stdout"]},
        "service": {"port": port, "health": health, "app_status": status},
        "database_migration": {"current": migration, "heads": heads},
        "source": {"git_head": git_head, "working_tree_status": git_status,
                   "tracked_scope_sha256": tracked_digest,
                   "scope": list(TRACKED_PATHS),
                   "working_tree_is_clean": working_tree_clean},
        "local_health_gate": "passed" if local_health_passed else "not_passed",
        "formal_deployment_revision_gate": "passed" if local_health_passed and working_tree_clean
        else "not_passed",
        "release_gate": "passed" if local_health_passed and working_tree_clean else "not_passed",
        "interpretation": (
            "当前Linux开发部署可健康复现，但工作树/进程证据不等于外部正式部署发布"
        ),
    }
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "local_health_gate": report["local_health_gate"],
                      "formal_deployment_revision_gate": report["formal_deployment_revision_gate"]},
                     ensure_ascii=False))
    return 0 if report["local_health_gate"] == "passed" else 1


def _parse_args() -> argparse.Namespace:
    """解析本地端口和证据输出路径。"""

    parser = argparse.ArgumentParser(description="采集Linux部署revision脱敏证据")
    parser.add_argument("--port", type=int, default=5137)
    parser.add_argument("--output", default="docs/manuals/evidence/q1-current-linux-deployment-revision-20260825.json")
    return parser.parse_args()


def _run(command: tuple[str, ...], *, timeout: int) -> dict[str, object]:
    """运行只读命令并截断输出，避免把配置或凭据写入证据。"""

    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 124, "stdout": "", "stderr": type(exc).__name__}
    return {"returncode": completed.returncode, "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-2000:]}


def _health(port: int) -> dict[str, object]:
    """读取本机health响应，仅保存JSON健康体和HTTP状态。"""

    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:  # noqa: S310
            body = response.read(4000).decode("utf-8", errors="replace")
            try:
                body_value: object = json.loads(body)
            except json.JSONDecodeError:
                body_value = body
            return {"status": response.status, "body": body_value}
    except (OSError, URLError) as exc:
        return {"status": 0, "body": None, "error": type(exc).__name__}


def _tracked_digest() -> str:
    """对部署关键路径内容做稳定摘要，不把文件正文写入报告。"""

    digest = sha256()
    for relative in TRACKED_PATHS:
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        elif path.is_dir():
            for child in sorted(item for item in path.rglob("*") if item.is_file()):
                digest.update(str(child.relative_to(ROOT)).encode("utf-8"))
                digest.update(child.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
