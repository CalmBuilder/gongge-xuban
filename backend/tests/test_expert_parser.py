from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.experts.local_source import SourceFile
from app.experts.parser import ExpertParseError, parse_expert_markdown


VALID_AGENT = """---
name: Frontend Developer
description: Builds accessible interfaces.
color: '#336699'
emoji: 🎨
vibe: Ships accessible interfaces
author: Example Author
tools: WebFetch, WebSearch, Read, Write, Edit, Read
services:
  - name: Example MCP
    url: https://example.com/mcp
    tier: free
---
# Frontend Developer

Run:

```bash
npm test
```
"""


def _source_file(tmp_path: Path, content: str, path: str = "engineering/frontend.md") -> SourceFile:
    absolute = tmp_path / "frontend.md"
    absolute.write_text(content, encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return SourceFile(
        path=path,
        absolute_path=absolute,
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def test_parse_expert_preserves_markdown_and_frontmatter(tmp_path: Path) -> None:
    parsed = parse_expert_markdown(_source_file(tmp_path, VALID_AGENT))
    assert parsed.name == "Frontend Developer"
    assert parsed.category_original == "engineering"
    assert parsed.emoji == "🎨"
    assert parsed.vibe == "Ships accessible interfaces"
    assert parsed.author == "Example Author"
    assert parsed.tools == ["WebFetch", "WebSearch", "Read", "Write", "Edit"]
    assert parsed.services[0].model_dump() == {
        "name": "Example MCP",
        "url": "https://example.com/mcp",
        "tier": "free",
    }
    assert "npm test" in parsed.source_markdown
    assert len(parsed.source_sha256) == 64


def test_parse_tools_accepts_string_list(tmp_path: Path) -> None:
    content = VALID_AGENT.replace(
        "tools: WebFetch, WebSearch, Read, Write, Edit, Read",
        "tools: [WebSearch, Read, WebSearch]",
    )
    assert parse_expert_markdown(_source_file(tmp_path, content)).tools == ["WebSearch", "Read"]


def test_parse_recovers_unquoted_colon_in_top_level_description(tmp_path: Path) -> None:
    content = VALID_AGENT.replace(
        "description: Builds accessible interfaces.",
        "description: Builds developer tools with great DX: helpful errors and fast startup.",
    )
    parsed = parse_expert_markdown(_source_file(tmp_path, content))
    assert parsed.description == (
        "Builds developer tools with great DX: helpful errors and fast startup."
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("plain markdown", "YAML front matter"),
        ("---\nname: Missing Description\n---\nbody", "name and description"),
        ("---\nname: X\ndescription: Y\nservices: [https://example.com]\n---\nbody", "services"),
        ("x" * 1_000_001, "1 MB"),
    ],
)
def test_parse_expert_rejects_invalid_input(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    with pytest.raises(ExpertParseError, match=message):
        parse_expert_markdown(_source_file(tmp_path, content))
