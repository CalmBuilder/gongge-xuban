from __future__ import annotations

import os
import sys
from pathlib import Path

from app.brand import desktop_env_value


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    # 开发态：backend/ 目录（app/paths.py 的上上级）
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return app_root()


def _platform_data_parent() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home()))
    return Path.home() / ".local" / "share"


def user_data_dir() -> Path:
    override = desktop_env_value("DATA_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        base = _platform_data_parent() / "Gongge-Xuban"
    base.mkdir(parents=True, exist_ok=True)
    return base
