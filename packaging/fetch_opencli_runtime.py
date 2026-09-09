"""
@Time       : 2026/09/04 12:30
@Author     : zhanglp8181
@File       : fetch_opencli_runtime.py
@CallChain  : Windows 构建脚本 → npm registry → runtime/opencli/node_modules
@Description: 在构建阶段把固定版本的 OpenCLI Node 包安装到产品 runtime，运行时不依赖全局 PATH。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


DEFAULT_OPENCLI_VERSION = "1.8.7"
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def _resolve_npm_command(*, is_windows: bool | None = None) -> str:
    """解析可直接交给 Python 子进程的 npm 启动器，兼容 Windows 的 npm.cmd。"""

    windows = os.name == "nt" if is_windows is None else is_windows
    configured = os.environ.get("NPM_COMMAND", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(("npm.cmd", "npm") if windows else ("npm",))

    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        explicit_path = Path(candidate).expanduser()
        if explicit_path.is_file():
            return str(explicit_path.resolve())

    if configured:
        print(
            "警告：NPM_COMMAND 不是有效的 npm 启动器，已回退到系统 PATH 自动查找。",
            file=sys.stderr,
        )
    requested = configured or ("npm.cmd/npm" if windows else "npm")
    raise FileNotFoundError(
        f"找不到 npm 启动器：{requested}。请安装 Node.js，或设置 NPM_COMMAND 为 npm.cmd 的完整路径。"
    )


def main(argv: list[str] | None = None) -> int:
    """解析固定版本和目标目录，调用 npm 安装 OpenCLI 运行时并校验入口文件。"""

    parser = argparse.ArgumentParser(description="准备产品内置 OpenCLI Node Runtime")
    parser.add_argument("output", type=Path, help="runtime/opencli 目标目录")
    parser.add_argument("--version", default=DEFAULT_OPENCLI_VERSION, help="OpenCLI npm 版本")
    args = parser.parse_args(argv)
    version = str(args.version).strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"OpenCLI 版本格式无效：{version}")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    npm = _resolve_npm_command()
    command = [
        npm,
        "install",
        "--prefix",
        str(output),
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--no-package-lock",
        "--omit=dev",
        f"@jackwener/opencli@{version}",
    ]
    subprocess.run(command, check=True)
    entry = output / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    if not entry.is_file():
        raise RuntimeError(f"OpenCLI Node 入口缺失：{entry}")
    print(f"OpenCLI runtime ready at {output} (version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
