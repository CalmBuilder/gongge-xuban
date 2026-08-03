from __future__ import annotations

import json

import pytest

from app.experts.parser import ParsedExpert
from app.experts.translation import (
    ExpertTranslationError,
    ExpertTranslator,
    is_high_risk,
    preserve_original_translation,
)


class FakeLLM:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def generate_text(self, system_prompt: str, user_payload: object) -> str:
        assert "evidence" in system_prompt
        assert "Frontend Developer" in str(user_payload)
        return json.dumps(self.payload, ensure_ascii=False)


def _parsed() -> ParsedExpert:
    return ParsedExpert(
        upstream_path="engineering/frontend.md",
        name="Frontend Developer",
        description="Build accessible interfaces.",
        category_original="engineering",
        tools=[],
        services=[],
        source_markdown=(
            "# Frontend Developer\nBuild accessible interfaces.\n"
            "```bash\nnpm test\n```\nhttps://react.dev"
        ),
        source_sha256="a" * 64,
    )


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name_zh": "前端开发专家",
        "description_zh": "构建可访问的前端界面",
        "category_zh": "工程研发",
        "tags_zh": ["React", "可访问性"],
        "markdown_zh": "# 前端开发专家\n```bash\nnpm test\n```\nhttps://react.dev",
        "capability_analysis": {
            "required_capabilities": ["prompt_reasoning"],
            "orchestration_required": False,
            "core_execution_requires_external_capability": False,
            "evidence": ["Build accessible interfaces."],
        },
    }
    payload.update(overrides)
    return payload


def test_translation_preserves_code_blocks_commands_urls_and_evidence() -> None:
    translated = ExpertTranslator(FakeLLM(_valid_payload())).translate(_parsed())
    assert translated.name_zh == "前端开发专家"
    assert "npm test" in translated.markdown_zh
    assert "https://react.dev" in translated.markdown_zh
    assert translated.capability_analysis.evidence == ["Build accessible interfaces."]


def test_translation_rejects_lost_source_tokens() -> None:
    payload = _valid_payload(markdown_zh="# 前端开发专家")
    with pytest.raises(ExpertTranslationError, match="preserve"):
        ExpertTranslator(FakeLLM(payload)).translate(_parsed())


def test_translation_rejects_capability_evidence_not_in_source() -> None:
    payload = _valid_payload(
        capability_analysis={
            "required_capabilities": ["prompt_reasoning"],
            "orchestration_required": False,
            "core_execution_requires_external_capability": False,
            "evidence": ["invented requirement"],
        }
    )
    with pytest.raises(ExpertTranslationError, match="evidence"):
        ExpertTranslator(FakeLLM(payload)).translate(_parsed())


@pytest.mark.parametrize("category", ["法律", "财务", "医疗健康", "安全与风控"])
def test_translation_marks_high_risk_categories(category: str) -> None:
    assert is_high_risk(category, []) is True


def test_preserve_original_translation_needs_no_model_and_keeps_exact_prompt() -> None:
    parsed = _parsed().model_copy(
        update={
            "source_markdown": (
                _parsed().source_markdown
                + "\nSpawn a specialist agent and automatically retry a failed handoff."
            )
        }
    )
    translated = preserve_original_translation(parsed)
    assert translated.name_zh == "Frontend Developer"
    assert translated.category_zh == "工程研发"
    assert translated.markdown_zh == parsed.source_markdown
    assert translated.capability_analysis.orchestration_required is True
    assert translated.capability_analysis.evidence[-1] in parsed.source_markdown


def test_preserve_original_does_not_treat_human_agents_or_output_schema_as_orchestration() -> None:
    parsed = _parsed().model_copy(
        update={
            "source_markdown": (
                "Coordinate with listing agents and client availability.\n"
                "Agent handoff: structured JSON consumable by other pipelines."
            )
        }
    )
    translated = preserve_original_translation(parsed)
    assert translated.capability_analysis.orchestration_required is False
