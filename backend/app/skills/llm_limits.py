from __future__ import annotations

from copy import copy

from app.db.models import ModelConfig


SKILL_MAX_OUTPUT_TOKENS = 8192


def skill_model_config(model_config: ModelConfig) -> ModelConfig:
    adjusted = copy(model_config)
    adjusted.max_output_tokens = SKILL_MAX_OUTPUT_TOKENS
    return adjusted
