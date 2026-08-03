from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from fastapi import Depends
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.db.models import ModelConfig
from app.llm.client import LLMClient


logger = logging.getLogger(__name__)
TextGenerator = Callable[[str, dict[str, Any]], str]
_SENSITIVE_KEYS = {"api_key", "check_code", "headers"}


def polish_text(
    tool_name: str,
    fallback: str,
    context: dict[str, Any],
    generator: TextGenerator | None = None,
) -> str:
    """Polish a text field without allowing model failure to affect tool execution."""
    if not get_settings().public_mock_llm_enabled or generator is None:
        return fallback
    safe_context = {key: value for key, value in context.items() if key not in _SENSITIVE_KEYS}
    try:
        value = generator(tool_name, safe_context)
    except Exception:
        logger.warning("public mock copywriter fallback", extra={"tool_name": tool_name})
        return fallback
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def build_copywriter(db: Session = Depends(get_session)) -> TextGenerator | None:
    """Build the optional text generator from the demo tenant's default model."""
    if not get_settings().public_mock_llm_enabled:
        return None
    model = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == "tenant_demo",
            ModelConfig.is_default.is_(True),
            ModelConfig.enabled.is_(True),
        )
    ).first()
    if model is None:
        return None
    client = LLMClient(model)

    def generate(tool_name: str, context: dict[str, Any]) -> str:
        return client.generate_text(
            "只润色业务提示文案，不改变任何事实、数值、状态、标识或字段。",
            {"tool_name": tool_name, "context": context},
        )

    return generate
