"""Canonical product identity and runtime settings."""

from __future__ import annotations

import os

PRODUCT_NAME = "共格·序伴"
PRODUCT_SLUG = "gongge-xuban"
DESKTOP_APP_ID = "cn.gongge.xuban.desktop"
MCP_CLIENT_NAME = "Gongge-Xuban"


def health_payload() -> dict[str, str]:
    """Return the stable identity used by launchers and health clients."""
    return {"status": "ok", "product_id": PRODUCT_SLUG, "app": PRODUCT_NAME}


def desktop_env_value(suffix: str, default: str = "") -> str:
    """Read a canonical desktop setting."""
    return os.environ.get(f"GONGGE_XUBAN_{suffix}", default)


def site_chat_upstream() -> str:
    return os.environ.get(
        "GONGGE_XUBAN_SITE_CHAT_UPSTREAM",
        "http://127.0.0.1:10187",
    ).rstrip("/")


def headless_enabled() -> bool:
    value = os.environ.get("GONGGE_XUBAN_HEADLESS", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}
