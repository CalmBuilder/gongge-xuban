from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.experts.localization_integrity import split_markdown
from app.experts.localization_translation import (
    LocalizationTranslationError,
    LocalizationTranslator,
    NameCandidate,
    load_upstream_name_map,
    resolve_localized_names,
)
from app.experts.parser import ParsedExpert


class FakeLLM:
    def __init__(self, responses: list[dict[str, object] | str]) -> None:
        self.responses = responses
        self.calls: list[object] = []

    def generate_text(self, system_prompt: str, user_payload: object) -> str:
        self.calls.append(user_payload)
        response = self.responses.pop(0)
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


def parsed() -> ParsedExpert:
    return ParsedExpert(
        upstream_path="engineering/frontend.md",
        name="Frontend Developer",
        description="Build interfaces.",
        category_original="engineering",
        tools=[],
        services=[],
        source_markdown="# Expert\nRun `npm test` at https://example.com with 95% confidence.",
        source_sha256="a" * 64,
    )


def test_upstream_mapping_has_priority(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    path.write_text(
        json.dumps({"Frontend Developer": {"name": "前端开发工程师", "description": "前端专家"}}),
        encoding="utf-8",
    )
    mapping = load_upstream_name_map(path)
    assert mapping["Frontend Developer"].name_zh == "前端开发工程师"
    assert mapping["Frontend Developer"].description_zh == "前端专家"


def test_translator_validates_chunk_identity_and_fidelity() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    translated = "# 专家\n请以 95% 的置信度在 https://example.com 运行 `npm test`。"
    llm = FakeLLM(
        [{
            "upstream_path": parsed().upstream_path,
            "chunk_index": chunk.index,
            "source_sha256": chunk.source_sha256,
            "translated_markdown": translated,
        }]
    )
    result = LocalizationTranslator(llm).translate_chunk(parsed(), chunk, "前端开发工程师")
    assert result.translated_markdown == translated
    assert result.fidelity.valid is True


def test_translator_masks_and_restores_protected_tokens() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    masked_translation = (
        "# 专家\n请在 ⟪GG:B⟫ 使用 ⟪GG:A⟫ 运行，"
        "置信度为 ⟪GG:C⟫。"
    )
    llm = FakeLLM(
        [{
            "upstream_path": parsed().upstream_path,
            "chunk_index": chunk.index,
            "source_sha256": chunk.source_sha256,
            "translated_markdown": masked_translation,
        }]
    )

    result = LocalizationTranslator(llm).translate_chunk(parsed(), chunk, "前端开发工程师")

    payload = llm.calls[0]
    assert isinstance(payload, dict)
    assert "`npm test`" not in str(payload["markdown"])
    assert "https://example.com" not in str(payload["markdown"])
    assert "95%" not in str(payload["markdown"])
    assert "`npm test`" in result.translated_markdown
    assert "https://example.com" in result.translated_markdown
    assert "95%" in result.translated_markdown


def test_translator_retries_invalid_response_then_stops() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    bad = {
        "upstream_path": "wrong/path.md",
        "chunk_index": 0,
        "source_sha256": chunk.source_sha256,
        "translated_markdown": "# 中文",
    }
    llm = FakeLLM(["not-json", bad, bad])
    with pytest.raises(LocalizationTranslationError, match="3 attempts"):
        LocalizationTranslator(llm).translate_chunk(parsed(), chunk, "前端开发工程师")
    assert len(llm.calls) == 3
    assert isinstance(llm.calls[1], dict)
    assert "previous_error" in llm.calls[1]


def test_translator_uses_line_array_fallback_after_chunk_retries() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    bad = {
        "upstream_path": parsed().upstream_path,
        "chunk_index": chunk.index,
        "source_sha256": chunk.source_sha256,
        "translated_markdown": "# 中文",
    }
    fallback = {
        "upstream_path": parsed().upstream_path,
        "chunk_index": chunk.index,
        "source_sha256": chunk.source_sha256,
        "translated_lines": [
            "# 专家",
            "请在 ⟪GG:B⟫ 使用 ⟪GG:A⟫ 运行，置信度为 ⟪GG:C⟫。",
        ],
    }
    llm = FakeLLM([bad, fallback])

    result = LocalizationTranslator(
        llm, max_attempts=1, line_fallback_attempts=1
    ).translate_chunk(parsed(), chunk, "前端开发工程师")

    assert result.fidelity.valid is True
    assert result.attempts == 2
    assert isinstance(llm.calls[1], dict)
    assert llm.calls[1]["task"] == "translate_markdown_lines"


def test_translator_uses_individual_line_fallback_after_array_failure() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    identity = {
        "upstream_path": parsed().upstream_path,
        "chunk_index": chunk.index,
        "source_sha256": chunk.source_sha256,
    }
    llm = FakeLLM(
        [
            {**identity, "translated_markdown": "# 中文"},
            {**identity, "translated_lines": ["行数错误"]},
            {**identity, "translated_markdown": "# 专家"},
            {
                **identity,
                "translated_markdown": (
                    "请在 ⟪GG:B⟫ 使用 ⟪GG:A⟫ 运行，置信度为 ⟪GG:C⟫。"
                ),
            },
        ]
    )

    result = LocalizationTranslator(
        llm,
        max_attempts=1,
        line_fallback_attempts=1,
        single_line_fallback_attempts=1,
    ).translate_chunk(parsed(), chunk, "前端开发工程师")

    assert result.fidelity.valid is True
    assert isinstance(llm.calls[2], dict)
    assert llm.calls[2]["task"] == "translate_single_markdown_line"


def test_translator_uses_raw_line_when_marker_line_is_rewritten() -> None:
    chunk = split_markdown(parsed().source_markdown)[0]
    identity = {
        "upstream_path": parsed().upstream_path,
        "chunk_index": chunk.index,
        "source_sha256": chunk.source_sha256,
    }
    llm = FakeLLM(
        [
            {**identity, "translated_markdown": "# 中文"},
            {**identity, "translated_lines": ["行数错误"]},
            {**identity, "translated_markdown": "# 专家"},
            {**identity, "translated_markdown": "省略了标记"},
            {
                **identity,
                "translated_markdown": (
                    "请以 95% 的置信度在 https://example.com 运行 `npm test`。"
                ),
            },
        ]
    )

    result = LocalizationTranslator(
        llm,
        max_attempts=1,
        line_fallback_attempts=1,
        single_line_fallback_attempts=1,
        raw_line_fallback_attempts=1,
    ).translate_chunk(parsed(), chunk, "前端开发工程师")

    assert result.fidelity.valid is True
    assert isinstance(llm.calls[4], dict)
    assert llm.calls[4]["task"] == "translate_single_raw_line"


def test_missing_upstream_mapping_uses_identity_translation() -> None:
    expert = parsed().model_copy(update={"name": "API Platform Engineer"})
    llm = FakeLLM(
        [{
            "upstream_path": expert.upstream_path,
            "source_sha256": expert.source_sha256,
            "name_zh": "API 平台工程师",
            "description_zh": "建设可靠的 API 平台",
        }]
    )
    identity = LocalizationTranslator(llm).translate_identity(expert)
    assert identity.name_zh == "API 平台工程师"


def test_name_resolution_uses_three_deterministic_levels() -> None:
    resolved = resolve_localized_names(
        [
            NameCandidate(upstream_path="a.md", original_name="Anthropologist", localized_name="学术人类学家"),
            NameCandidate(upstream_path="b.md", original_name="Academic Anthropologist", localized_name="学术人类学家"),
            NameCandidate(upstream_path="c.md", original_name="Other", localized_name="已有员工"),
        ],
        occupied={"已有员工", "已有员工（Other）"},
    )
    assert resolved["a.md"] == "学术人类学家"
    assert resolved["b.md"] == "学术人类学家（Academic Anthropologist）"
    assert resolved["c.md"] == "已有员工（Other · Agency Agents）"
