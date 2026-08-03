from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.experts.local_source import (
    LocalSourceError,
    discover_source_files,
    inspect_local_source,
)


VALID_AGENT = """---
name: Frontend Developer
description: Builds accessible interfaces.
---
# Frontend Developer
"""


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path, *, remote: str = "https://github.com/msitarzewski/agency-agents.git") -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Tests")
    _git(root, "remote", "add", "origin", remote)
    (root / "engineering").mkdir()
    (root / "engineering" / "frontend.md").write_text(VALID_AGENT, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def test_inspect_local_source_requires_agency_agents_remote(tmp_path: Path) -> None:
    root = _init_repo(tmp_path, remote="https://github.com/other/repo.git")
    with pytest.raises(LocalSourceError, match="msitarzewski/agency-agents"):
        inspect_local_source(root)

    source = inspect_local_source(root, allow_unverified=True)
    assert source.verified is False
    assert len(source.commit_sha) == 40


def test_inspect_local_source_rejects_dirty_worktree(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    (root / "engineering" / "frontend.md").write_text("changed", encoding="utf-8")
    with pytest.raises(LocalSourceError, match="clean worktree"):
        inspect_local_source(root)


def test_discovery_includes_nested_agents_and_excludes_documents(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    nested = root / "game-development" / "unity"
    nested.mkdir(parents=True)
    (nested / "unity-architect.md").write_text(VALID_AGENT, encoding="utf-8")
    (root / "README.md").write_text(VALID_AGENT, encoding="utf-8")
    docs = root / "integrations" / "codex"
    docs.mkdir(parents=True)
    (docs / "agent.md").write_text(VALID_AGENT, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "nested")

    paths = [item.path for item in discover_source_files(inspect_local_source(root))]
    assert paths == [
        "engineering/frontend.md",
        "game-development/unity/unity-architect.md",
    ]


def test_discovery_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(VALID_AGENT, encoding="utf-8")
    (root / "engineering" / "escape.md").symlink_to(outside)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "symlink")

    with pytest.raises(LocalSourceError, match="outside source root"):
        discover_source_files(inspect_local_source(root))
