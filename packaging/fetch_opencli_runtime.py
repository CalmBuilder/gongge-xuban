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
import subprocess


DEFAULT_OPENCLI_VERSION = "1.8.7"
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


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
    npm = os.environ.get("NPM_COMMAND", "npm").strip() or "npm"
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
