"""把上游能力声明转换为执行后端无关的能力清单。"""

from __future__ import annotations

import hashlib
import math
import re

from app.experts.parser import ParsedExpert
from app.experts.schema import CapabilityAnalysis, CapabilityManifest, CapabilityType


DECLARED_TOOL_CAPABILITIES = {
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Read": "workspace_read",
    "Write": "workspace_write",
    "Edit": "workspace_edit",
    "Bash": "shell_execute",
}
ALLOWED_ANALYSIS_CAPABILITIES = {
    "prompt_reasoning",
    "knowledge_base",
    "general_skill",
    "sop_skill",
    "web_fetch",
    "web_search",
    "workspace_read",
    "workspace_write",
    "workspace_edit",
    "shell_execute",
    "browser_use",
    "http_api",
    "mcp",
    "expert_orchestration",
}
P1_CAPABILITIES = {"knowledge_base", "general_skill", "sop_skill"}
P2_CAPABILITIES = {
    "web_fetch",
    "web_search",
    "workspace_read",
    "workspace_write",
    "workspace_edit",
    "shell_execute",
    "browser_use",
    "http_api",
    "mcp",
}


class CapabilityAnalysisError(ValueError):
    """能力分析包含未受支持或无法验证的内容。"""


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _slug(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    normalized = re.sub(r"[^\w]+", "-", separated.casefold(), flags=re.UNICODE).strip("-_")
    if normalized:
        return normalized
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def estimate_input_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def prompt_budget_warning(tokens: int) -> LiteralWarning:
    return "high" if tokens >= 24_000 else None


LiteralWarning = str | None


def _capability_type(required: list[str]) -> CapabilityType:
    if "expert_orchestration" in required:
        return "P3"
    if any(item in P2_CAPABILITIES or item.startswith("external_") for item in required):
        return "P2"
    if any(item in P1_CAPABILITIES for item in required):
        return "P1"
    return "P0"


def build_capability_manifest(
    parsed: ParsedExpert,
    analysis: CapabilityAnalysis,
) -> CapabilityManifest:
    unknown = [
        item
        for item in analysis.required_capabilities
        if item not in ALLOWED_ANALYSIS_CAPABILITIES
    ]
    if unknown:
        raise CapabilityAnalysisError(f"Unsupported capability names: {', '.join(unknown)}")

    required = ["prompt_reasoning"]
    for capability in analysis.required_capabilities:
        _append_unique(required, capability)
    for tool in parsed.tools:
        mapped = DECLARED_TOOL_CAPABILITIES.get(tool)
        _append_unique(required, mapped or f"external_tool:{_slug(tool)}")
    for service in parsed.services:
        _append_unique(required, f"external_service:{_slug(service.name)}")
    if analysis.orchestration_required:
        _append_unique(required, "expert_orchestration")

    resolved = ["prompt_reasoning"]
    unresolved = [item for item in required if item not in resolved]
    capability_type = _capability_type(required)
    readiness = "ready"
    if unresolved:
        readiness = "partial"
    if (
        capability_type == "P3"
        and analysis.orchestration_required
        and analysis.core_execution_requires_external_capability
    ):
        readiness = "blocked"
    return CapabilityManifest(
        capability_type=capability_type,
        readiness=readiness,
        required_capabilities=required,
        resolved_capabilities=resolved,
        unresolved_requirements=unresolved,
        orchestration_required=analysis.orchestration_required,
        core_execution_requires_external_capability=(
            analysis.core_execution_requires_external_capability
        ),
        evidence=analysis.evidence,
    )
