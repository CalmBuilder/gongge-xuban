"""安全解析 Agency Agents 的 YAML Front Matter 与 Markdown 正文。"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict

from app.experts.local_source import MAX_SOURCE_FILE_BYTES, SourceFile


MAX_FRONTMATTER_BYTES = 32_768
MAX_FRONTMATTER_FIELDS = 64
MAX_YAML_DEPTH = 6
RECOVERABLE_SCALAR_RE = re.compile(
    r"^(name|description|color|emoji|vibe|author):[ \t]*(.*)$"
)


class ExpertParseError(ValueError):
    """专家 Markdown 不符合导入数据契约。"""


class DeclaredService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    url: str
    tier: str | None = None


class ParsedExpert(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    name: str
    description: str
    category_original: str
    color: str | None = None
    emoji: str | None = None
    vibe: str | None = None
    author: str | None = None
    tools: list[str]
    services: list[DeclaredService]
    source_markdown: str
    source_sha256: str


def _normalized_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ExpertParseError("Expert Markdown must be UTF-8") from exc


def _yaml_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_YAML_DEPTH:
        return depth
    if isinstance(value, dict):
        return max((_yaml_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_yaml_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _optional_text(value: object, field: str, *, max_length: int = 2_000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExpertParseError(f"{field} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ExpertParseError(f"{field} exceeds {max_length} characters")
    return normalized


def _required_text(value: object, field: str, *, max_length: int) -> str:
    normalized = _optional_text(value, field, max_length=max_length)
    if not normalized:
        raise ExpertParseError("Front Matter requires non-empty name and description")
    return normalized


def _parse_tools(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        candidates = value
    else:
        raise ExpertParseError("tools must be comma-separated text or a string list")
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tool = candidate.strip()
        if not tool or tool in seen:
            continue
        if len(tool) > 128:
            raise ExpertParseError("tools entry exceeds 128 characters")
        seen.add(tool)
        result.append(tool)
    return result


def _parse_services(value: object) -> list[DeclaredService]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExpertParseError("services must be an object list")
    result: list[DeclaredService] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"name", "url", "tier"}:
            raise ExpertParseError("services entries may only contain name, url and tier")
        name = _required_text(entry.get("name"), "services.name", max_length=191)
        url = _required_text(entry.get("url"), "services.url", max_length=2_000)
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ExpertParseError("services.url must be an HTTPS URL")
        tier = _optional_text(entry.get("tier"), "services.tier", max_length=64)
        result.append(DeclaredService(name=name, url=url, tier=tier))
    return result


def _quote_unquoted_top_level_scalars(frontmatter: str) -> str:
    """只为上游常见的未加引号单行文本提供受限 YAML 兼容。"""
    recovered: list[str] = []
    for line in frontmatter.splitlines():
        match = RECOVERABLE_SCALAR_RE.fullmatch(line)
        if not match:
            recovered.append(line)
            continue
        field, value = match.groups()
        if value.lstrip().startswith(("'", '"', "[", "{", "|", ">")):
            recovered.append(line)
            continue
        recovered.append(f"{field}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(recovered)


def parse_expert_markdown(file: SourceFile) -> ParsedExpert:
    raw = file.absolute_path.read_bytes()
    if len(raw) > MAX_SOURCE_FILE_BYTES:
        raise ExpertParseError("Expert Markdown exceeds 1 MB")
    text = _normalized_text(raw)
    normalized_bytes = text.encode("utf-8")
    source_sha256 = hashlib.sha256(normalized_bytes).hexdigest()
    if source_sha256 != file.sha256:
        raise ExpertParseError("Source file changed since discovery")
    if not text.startswith("---\n"):
        raise ExpertParseError("Expert Markdown requires YAML front matter")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise ExpertParseError("Expert Markdown requires closed YAML front matter")
    frontmatter_text = text[4:closing]
    if len(frontmatter_text.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ExpertParseError("YAML front matter exceeds 32 KB")
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as original_exc:
        try:
            data = yaml.safe_load(_quote_unquoted_top_level_scalars(frontmatter_text))
        except yaml.YAMLError as exc:
            raise ExpertParseError(f"Invalid YAML front matter: {original_exc}") from exc
    if not isinstance(data, dict):
        raise ExpertParseError("YAML front matter must be an object")
    if len(data) > MAX_FRONTMATTER_FIELDS or _yaml_depth(data) > MAX_YAML_DEPTH:
        raise ExpertParseError("YAML front matter is too complex")
    body = text[closing + 5 :].strip()
    if not body:
        raise ExpertParseError("Expert Markdown body is empty")
    category = file.path.split("/", 1)[0]
    return ParsedExpert(
        upstream_path=file.path,
        name=_required_text(data.get("name"), "name", max_length=191),
        description=_required_text(data.get("description"), "description", max_length=20_000),
        category_original=category,
        color=_optional_text(data.get("color"), "color", max_length=64),
        emoji=_optional_text(data.get("emoji"), "emoji", max_length=32),
        vibe=_optional_text(data.get("vibe"), "vibe"),
        author=_optional_text(data.get("author"), "author"),
        tools=_parse_tools(data.get("tools")),
        services=_parse_services(data.get("services")),
        source_markdown=body,
        source_sha256=source_sha256,
    )
