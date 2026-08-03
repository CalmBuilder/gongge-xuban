from app.experts.localization_integrity import (
    restore_chunk_boundaries,
    split_markdown,
    validate_translation,
)


SOURCE = """# Expert

Explain version 2.4.1 with at least 95% confidence using `config.yaml`.

## Command

```bash
python app.py --input ${SOURCE_PATH} --limit 10
```

| Key | Value |
| --- | --- |
| API | https://example.com/v1/items |

Read [the guide](https://example.com/docs) from `/workspace/docs` and keep {{ user_id }}.
"""


def test_split_markdown_preserves_exact_text_and_protected_blocks() -> None:
    chunks = split_markdown(SOURCE, max_chars=90)
    assert "".join(item.source_text for item in chunks) == SOURCE
    assert len(chunks) > 1
    assert any("```bash\npython app.py --input ${SOURCE_PATH} --limit 10\n```" in item.source_text for item in chunks)
    assert any("| Key | Value |\n| --- | --- |\n| API | https://example.com/v1/items |" in item.source_text for item in chunks)
    assert [item.index for item in chunks] == list(range(len(chunks)))
    assert all(len(item.source_sha256) == 64 for item in chunks)


def test_validate_translation_accepts_chinese_with_protected_tokens_unchanged() -> None:
    translated = SOURCE.replace("# Expert", "# 专家").replace(
        "Explain version 2.4.1 with at least 95% confidence using",
        "请以至少 95% 的置信度解释版本 2.4.1，并使用",
    ).replace("## Command", "## 命令").replace("Read ", "读取 ").replace(" and keep ", "并保留")
    report = validate_translation(SOURCE, translated)
    assert report.valid is True
    assert report.mismatches == {}


def test_validate_translation_reports_lost_url_number_and_heading_level() -> None:
    translated = SOURCE.replace("# Expert", "## 专家").replace(
        "https://example.com/docs", "https://example.com/wrong"
    ).replace("95%", "90%")
    report = validate_translation(SOURCE, translated)
    assert report.valid is False
    assert {"headings", "urls", "numbers"} <= set(report.mismatches)


def test_validate_translation_rejects_unchanged_english_prose() -> None:
    report = validate_translation(SOURCE, SOURCE)
    assert report.valid is False
    assert "language" in report.mismatches


def test_validate_translation_allows_chinese_prose_inside_markdown_template() -> None:
    source = """```markdown
# Project report

**Latency target**: 150ms
**Status**: [PASS/NEEDS_WORK]
```"""
    translated = """```markdown
# 项目报告

**延迟目标**：150 ms
**状态**：[PASS/NEEDS_WORK]
```"""

    report = validate_translation(source, translated)

    assert report.valid is True
    assert report.mismatches == {}


def test_validate_translation_still_requires_executable_code_to_be_exact() -> None:
    translated = SOURCE.replace("# Expert", "# 专家").replace(
        "python app.py --input ${SOURCE_PATH} --limit 10",
        "python app.py --input ${SOURCE_PATH} --limit 20",
    )

    report = validate_translation(SOURCE, translated)

    assert report.valid is False
    assert "fenced_code" in report.mismatches


def test_restore_chunk_boundaries_preserves_source_edge_newlines() -> None:
    assert restore_chunk_boundaries("\n# Source\n\n", "\n\n# 中文\n") == "\n# 中文\n\n"


def test_validate_translation_accepts_unchanged_code_only_chunk() -> None:
    code = """```python
value = calculate_total(10)
```"""
    assert validate_translation(code, code).valid is True


def test_validate_translation_allows_translated_shell_comments() -> None:
    source = """```bash
# Verify output
ffmpeg -i input.mp4 -f null -  # real pipeline
```"""
    translated = """```bash
# 验证输出
ffmpeg -i input.mp4 -f null -  # 实际管线
```"""
    assert validate_translation(source, translated).valid is True


def test_validate_translation_does_not_include_chinese_punctuation_in_url() -> None:
    source = "Read (https://picsum.photos/) now."
    translated = "立即读取（https://picsum.photos/）。"
    assert validate_translation(source, translated).valid is True


def test_validate_translation_preserves_compact_currency_units() -> None:
    source = "Budget is $500K, then $1M, with $8B total."
    translated = "预算为 $500K，随后为 $1M，总额为 $8B。"
    assert validate_translation(source, translated).valid is True
