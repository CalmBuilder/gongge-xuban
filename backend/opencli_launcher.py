"""
@Time       : 2026/09/04 12:45
@Author     : zhanglp8181
@File       : opencli_launcher.py
@CallChain  : packaged runtime/opencli/opencli.exe → shared runtime/youtube-js/node.exe → OpenCLI main.js
@Description: 为 npm 形态的 OpenCLI 提供不依赖系统 PATH 的 Windows 可执行入口。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    """从安装目录定位固定 Node 与 OpenCLI 入口并透传退出码、标准输出和错误输出。"""

    arguments = list(sys.argv[1:] if argv is None else argv)
    root = _runtime_root()
    node = _resolve_node_path(root)
    entry = root / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    if not node.is_file() or not entry.is_file():
        print("Bundled OpenCLI runtime is incomplete.", file=sys.stderr)
        return 2
    completed = subprocess.run([str(node), str(entry), *arguments], check=False)
    return int(completed.returncode)


def _runtime_root() -> Path:
    """解析冻结入口所在的 runtime/opencli 目录，不回退到用户全局 npm 安装。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1] / "runtime" / "opencli"


def _resolve_node_path(root: Path) -> Path:
    """优先兼容旧包的同目录 Node，并回退到新包共用的 YouTube Node。"""

    node_name = "node.exe" if sys.platform == "win32" else "node"
    legacy_node = root / node_name
    if legacy_node.is_file():
        return legacy_node
    return root.parent / "youtube-js" / node_name


if __name__ == "__main__":
    raise SystemExit(main())
