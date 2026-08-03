"""校验本地 Agency Agents Git 工作树并发现候选专家文件。"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


MAX_SOURCE_FILE_BYTES = 1_000_000
EXCLUDED_ROOTS = {
    ".github",
    "docs",
    "examples",
    "integrations",
    "localizations",
    "scripts",
}
EXCLUDED_PREFIXES = (
    "strategy/coordination/",
    "strategy/playbooks/",
    "strategy/runbooks/",
)
EXCLUDED_FILENAMES = {
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING_zh-CN.md",
    "SECURITY.md",
}
FRONTMATTER_REQUIRED_RE = re.compile(
    r"\A---\s*\n(?=[\s\S]{0,32768}?^name\s*:)(?=[\s\S]{0,32768}?^description\s*:)",
    re.MULTILINE,
)


class LocalSourceError(ValueError):
    """本地来源不满足安全或可追溯约束。"""


@dataclass(frozen=True)
class LocalSource:
    root: Path
    commit_sha: str
    remote_url: str
    verified: bool


@dataclass(frozen=True)
class SourceFile:
    path: str
    absolute_path: Path
    sha256: str


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalSourceError(f"Git command failed: {exc}") from exc
    if completed.returncode != 0:
        raise LocalSourceError(completed.stderr.strip() or "Git command failed")
    return completed.stdout.strip()


def inspect_local_source(path: Path, allow_unverified: bool = False) -> LocalSource:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LocalSourceError(f"Source directory does not exist: {path}") from exc
    if not root.is_dir():
        raise LocalSourceError("Source must be a directory")
    top_level = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top_level != root:
        raise LocalSourceError("Source must be the Git worktree root")
    if _git(root, "status", "--porcelain"):
        raise LocalSourceError("Source must have a clean worktree")
    commit_sha = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha):
        raise LocalSourceError("Source HEAD must resolve to a 40-character commit SHA")
    try:
        remote_url = _git(root, "remote", "get-url", "origin")
    except LocalSourceError:
        remote_url = ""
    verified = "msitarzewski/agency-agents" in remote_url
    if not verified and not allow_unverified:
        raise LocalSourceError("Source remote must be msitarzewski/agency-agents")
    return LocalSource(
        root=root,
        commit_sha=commit_sha.lower(),
        remote_url=remote_url,
        verified=verified,
    )


def _is_excluded(relative: Path) -> bool:
    posix = relative.as_posix()
    return (
        relative.name in EXCLUDED_FILENAMES
        or relative.parts[0] in EXCLUDED_ROOTS
        or any(posix.startswith(prefix) for prefix in EXCLUDED_PREFIXES)
    )


def _normalized_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_FILE_BYTES:
        return raw
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def discover_source_files(source: LocalSource) -> list[SourceFile]:
    items: list[SourceFile] = []
    for candidate in sorted(source.root.rglob("*.md")):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise LocalSourceError(f"Cannot resolve source file: {candidate}") from exc
        if not resolved.is_relative_to(source.root):
            raise LocalSourceError(f"Source file resolves outside source root: {candidate}")
        relative = candidate.relative_to(source.root)
        if _is_excluded(relative) or not resolved.is_file():
            continue
        raw = _normalized_bytes(resolved)
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            continue
        try:
            preview = raw[:32768].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not FRONTMATTER_REQUIRED_RE.search(preview):
            continue
        items.append(
            SourceFile(
                path=relative.as_posix(),
                absolute_path=resolved,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return items
