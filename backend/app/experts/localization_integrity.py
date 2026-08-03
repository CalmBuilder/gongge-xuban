"""专家 Markdown 中文化的安全分块与确定性保真校验。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field


FENCED_BLOCK_RE = re.compile(r"```([^\n]*)\n([\s\S]*?)```", re.MULTILINE)
URL_RE = re.compile(r"https?://[^\s)>\]}`|），。；：！？】》」』]+")
LINK_TARGET_RE = re.compile(r"\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
PLACEHOLDER_RE = re.compile(r"\$\{[^}\n]+\}|\{\{[^}\n]+\}\}")
FLAG_RE = re.compile(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9_-]*")
PATH_RE = re.compile(r"(?<![:\w])(?:\.{0,2}/|/)[A-Za-z0-9_@.+~/-]+")
NUMBER_RE = re.compile(
    r"(?<![\w])\d+(?:\.\d+)*(?:\s*(?:%|ms|s|px|KB|MB|GB|TB|K|M|B))?(?![\w])",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


class MarkdownChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    source_text: str
    source_sha256: str


class FidelityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    mismatches: dict[str, list[str]] = Field(default_factory=dict)


class ProtectedMarkdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    replacements: dict[str, str]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _semantic_blocks(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    in_table = False
    for line in lines:
        stripped = line.lstrip()
        is_fence = stripped.startswith("```")
        is_table = stripped.startswith("|")
        if current and not in_fence and in_table and not is_table:
            blocks.append("".join(current))
            current = []
        if current and not in_fence and not in_table and (is_fence or is_table):
            blocks.append("".join(current))
            current = []
        current.append(line)
        if is_fence:
            in_fence = not in_fence
        in_table = is_table and not in_fence
        if not in_fence and not in_table and not line.strip():
            blocks.append("".join(current))
            current = []
    if current:
        blocks.append("".join(current))
    return blocks


def split_markdown(text: str, max_chars: int = 12_000) -> list[MarkdownChunk]:
    if not text:
        raise ValueError("Markdown source is empty")
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    packed: list[str] = []
    current = ""
    for block in _semantic_blocks(text):
        if current and len(current) + len(block) > max_chars:
            packed.append(current)
            current = ""
        if len(block) > max_chars and not block.lstrip().startswith(("```", "|")):
            for line in block.splitlines(keepends=True):
                if current and len(current) + len(line) > max_chars:
                    packed.append(current)
                    current = ""
                current += line
            continue
        current += block
    if current:
        packed.append(current)
    return [
        MarkdownChunk(index=index, source_text=value, source_sha256=_sha256(value))
        for index, value in enumerate(packed)
    ]


def _counter_mismatch(source: list[str], translated: list[str]) -> list[str]:
    left = Counter(source)
    right = Counter(translated)
    values = sorted(set(left) | set(right))
    return [f"{value}: source={left[value]} translated={right[value]}" for value in values if left[value] != right[value]]


TRANSLATABLE_FENCE_LANGUAGES = {
    "",
    "bash",
    "console",
    "markdown",
    "md",
    "plaintext",
    "sh",
    "shell",
    "text",
}
SHELL_FENCE_LANGUAGES = {"bash", "console", "sh", "shell"}
QUOTED_SHELL_TEXT_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _fence_language(value: str) -> str:
    return value.strip().split(maxsplit=1)[0].casefold() if value.strip() else ""


def _exact_fenced_blocks(text: str) -> list[str]:
    return [
        match.group(0)
        for match in FENCED_BLOCK_RE.finditer(text)
        if _fence_language(match.group(1)) not in TRANSLATABLE_FENCE_LANGUAGES
    ]


def _without_exact_fences(text: str) -> str:
    return FENCED_BLOCK_RE.sub(
        lambda match: ""
        if _fence_language(match.group(1)) not in TRANSLATABLE_FENCE_LANGUAGES
        else match.group(2),
        text,
    )


def _shell_structure(text: str) -> list[str]:
    structure: list[str] = []
    for match in FENCED_BLOCK_RE.finditer(text):
        if _fence_language(match.group(1)) not in SHELL_FENCE_LANGUAGES:
            continue
        for line in match.group(2).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            normalized = QUOTED_SHELL_TEXT_RE.sub('"<TEXT>"', stripped)
            structure.append(re.sub(r"\s+#.*$", "", normalized))
    return structure


def _normalized_numbers(text: str) -> list[str]:
    return [re.sub(r"\s+", "", value) for value in NUMBER_RE.findall(text)]


def _heading_text(text: str) -> str:
    return FENCED_BLOCK_RE.sub(
        lambda match: match.group(2)
        if _fence_language(match.group(1))
        in {"", "markdown", "md", "plaintext", "text"}
        else "",
        text,
    )


PROTECTED_TOKEN_RE = re.compile(
    "|".join(
        f"(?:{pattern.pattern})"
        for pattern in (
            URL_RE,
            INLINE_CODE_RE,
            PLACEHOLDER_RE,
            FLAG_RE,
            PATH_RE,
            NUMBER_RE,
        )
    )
)


def _alpha_index(index: int) -> str:
    value = ""
    current = index
    while True:
        current, remainder = divmod(current, 26)
        value = chr(ord("A") + remainder) + value
        if current == 0:
            return value
        current -= 1


def protect_translation_tokens(text: str) -> ProtectedMarkdown:
    """在送入模型前掩码不可翻译的结构值，返回可逆映射。"""

    replacements: dict[str, str] = {}

    def replace(value: str) -> str:
        marker = f"⟪GG:{_alpha_index(len(replacements))}⟫"
        replacements[marker] = value
        return marker

    masked_fences = FENCED_BLOCK_RE.sub(
        lambda match: replace(match.group(0))
        if _fence_language(match.group(1)) not in TRANSLATABLE_FENCE_LANGUAGES
        else match.group(0),
        text,
    )
    masked = PROTECTED_TOKEN_RE.sub(lambda match: replace(match.group(0)), masked_fences)
    return ProtectedMarkdown(text=masked, replacements=replacements)


def restore_translation_tokens(text: str, replacements: dict[str, str]) -> str:
    """恢复模型保留的掩码；部分丢失或重复时拒绝结果。"""

    present = [marker for marker in replacements if marker in text]
    if not present:
        return text
    invalid = [marker for marker in replacements if text.count(marker) != 1]
    if invalid:
        raise ValueError(f"protected markers were changed: {invalid}")
    restored = text
    for marker, value in replacements.items():
        restored = restored.replace(marker, value)
    return restored


def restore_chunk_boundaries(source: str, translated: str) -> str:
    """恢复源分块边缘的换行，避免拼接时吞掉后续 Markdown 结构。"""

    leading = len(source) - len(source.lstrip("\n"))
    trailing = len(source) - len(source.rstrip("\n"))
    return "\n" * leading + translated.strip("\n") + "\n" * trailing


def validate_translation(source: str, translated: str) -> FidelityReport:
    mismatches: dict[str, list[str]] = {}
    comparable_source = _without_exact_fences(source)
    comparable_translation = _without_exact_fences(translated)
    fenced_source = [match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(source)]
    fenced_translation = [
        match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(translated)
    ]
    checks = {
        "fenced_code": (
            fenced_source + _exact_fenced_blocks(source) + _shell_structure(source),
            fenced_translation
            + _exact_fenced_blocks(translated)
            + _shell_structure(translated),
        ),
        "urls": (URL_RE.findall(source), URL_RE.findall(translated)),
        "link_targets": (
            LINK_TARGET_RE.findall(comparable_source),
            LINK_TARGET_RE.findall(comparable_translation),
        ),
        "inline_code": (
            INLINE_CODE_RE.findall(comparable_source),
            INLINE_CODE_RE.findall(comparable_translation),
        ),
        "placeholders": (
            PLACEHOLDER_RE.findall(comparable_source),
            PLACEHOLDER_RE.findall(comparable_translation),
        ),
        "flags": (FLAG_RE.findall(comparable_source), FLAG_RE.findall(comparable_translation)),
        "paths": (PATH_RE.findall(comparable_source), PATH_RE.findall(comparable_translation)),
        "numbers": (
            _normalized_numbers(comparable_source),
            _normalized_numbers(comparable_translation),
        ),
        "headings": (
            HEADING_RE.findall(_heading_text(source)),
            HEADING_RE.findall(_heading_text(translated)),
        ),
    }
    for name, (expected, actual) in checks.items():
        mismatch = _counter_mismatch(expected, actual)
        if mismatch:
            mismatches[name] = mismatch
    requires_translation = bool(re.search(r"[A-Za-z]", comparable_source))
    if not translated.strip() or (
        requires_translation
        and (translated == source or not CHINESE_RE.search(translated))
    ):
        mismatches["language"] = ["translated prose must contain Chinese and differ from source"]
    return FidelityReport(valid=not mismatches, mismatches=mismatches)
