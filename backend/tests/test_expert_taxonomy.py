from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.experts.taxonomy_schema import (
    AGENCY_AGENTS_SOURCE_COMMIT,
    ExpertTaxonomyError,
    load_agency_agents_taxonomy,
)


def _write_taxonomy(path: Path, experts: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_code": "agency-agents",
                "source_commit": AGENCY_AGENTS_SOURCE_COMMIT,
                "experts": experts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _entry(path: str = "engineering/engineering-data-engineer.md") -> dict[str, object]:
    return {
        "upstream_path": path,
        "category": "工程研发",
        "subcategory": "数据与数据库",
        "subcategory_original": "",
        "basis": "curated_role_mapping",
    }


def test_v1_taxonomy_covers_all_imported_experts() -> None:
    taxonomy = load_agency_agents_taxonomy()

    assert taxonomy.version == 1
    assert taxonomy.source_commit == AGENCY_AGENTS_SOURCE_COMMIT
    assert len(taxonomy.experts) == 263
    assert len({item.upstream_path for item in taxonomy.experts}) == 263
    assert len({item.category for item in taxonomy.experts}) == 17
    assert all(item.category and item.subcategory for item in taxonomy.experts)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows + [rows[0]], "Duplicate upstream path"),
        (
            lambda rows: [{**rows[0], "subcategory": "不存在的方向"}],
            "Invalid category/subcategory pair",
        ),
        (
            lambda rows: [
                {
                    **rows[0],
                    "basis": "upstream_directory",
                    "subcategory_original": "engineering",
                }
            ],
            "Invalid upstream directory basis",
        ),
    ],
)
def test_taxonomy_rejects_invalid_entries(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    path = _write_taxonomy(tmp_path / "taxonomy.json", mutate([_entry()]))

    with pytest.raises(ExpertTaxonomyError, match=message):
        load_agency_agents_taxonomy(path, expected_count=None)


def test_taxonomy_rejects_wrong_count(tmp_path: Path) -> None:
    path = _write_taxonomy(tmp_path / "taxonomy.json", [_entry()])

    with pytest.raises(ExpertTaxonomyError, match="Expected 263 taxonomy entries"):
        load_agency_agents_taxonomy(path)


def test_taxonomy_accepts_real_upstream_subdirectory(tmp_path: Path) -> None:
    row = {
        "upstream_path": "game-development/unity/unity-architect.md",
        "category": "游戏开发",
        "subcategory": "Unity",
        "subcategory_original": "unity",
        "basis": "upstream_directory",
    }
    path = _write_taxonomy(tmp_path / "taxonomy.json", [row])

    taxonomy = load_agency_agents_taxonomy(path, expected_count=None)

    assert taxonomy.experts[0].subcategory_original == "unity"
