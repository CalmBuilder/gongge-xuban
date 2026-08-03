from __future__ import annotations

from app.experts.capability import (
    build_capability_manifest,
    estimate_input_tokens,
    prompt_budget_warning,
)
from app.experts.parser import DeclaredService, ParsedExpert
from app.experts.schema import CapabilityAnalysis


def _parsed_expert(
    *,
    tools: list[str] | None = None,
    services: list[DeclaredService] | None = None,
    source_markdown: str = "Build a useful deliverable.",
) -> ParsedExpert:
    return ParsedExpert(
        upstream_path="engineering/frontend.md",
        name="Frontend Developer",
        description="Builds interfaces.",
        category_original="engineering",
        tools=tools or [],
        services=services or [],
        source_markdown=source_markdown,
        source_sha256="a" * 64,
    )


def _analysis(**overrides: object) -> CapabilityAnalysis:
    values = {
        "required_capabilities": ["prompt_reasoning"],
        "orchestration_required": False,
        "core_execution_requires_external_capability": False,
        "evidence": ["Build a useful deliverable."],
    }
    values.update(overrides)
    return CapabilityAnalysis.model_validate(values)


def test_declared_tools_deterministically_map_to_p2_partial() -> None:
    parsed = _parsed_expert(
        tools=["WebFetch", "WebSearch", "Read", "Write", "Edit", "Bash"]
    )
    manifest = build_capability_manifest(parsed, _analysis())
    assert manifest.capability_type == "P2"
    assert manifest.readiness == "partial"
    assert manifest.required_capabilities == [
        "prompt_reasoning",
        "web_fetch",
        "web_search",
        "workspace_read",
        "workspace_write",
        "workspace_edit",
        "shell_execute",
    ]
    assert manifest.resolved_capabilities == ["prompt_reasoning"]
    assert "shell_execute" in manifest.unresolved_requirements


def test_unknown_tool_and_service_are_preserved_as_external_requirements() -> None:
    parsed = _parsed_expert(
        tools=["CustomTool"],
        services=[DeclaredService(name="Example Service", url="https://example.com")],
    )
    manifest = build_capability_manifest(parsed, _analysis())
    assert "external_tool:custom-tool" in manifest.required_capabilities
    assert "external_service:example-service" in manifest.required_capabilities


def test_explicit_multi_agent_evidence_maps_to_p3_blocked() -> None:
    source = "Spawn a developer agent, hand off context, and retry failed QA up to 3 times."
    analysis = _analysis(
        required_capabilities=["expert_orchestration"],
        orchestration_required=True,
        core_execution_requires_external_capability=True,
        evidence=[source],
    )
    manifest = build_capability_manifest(_parsed_expert(source_markdown=source), analysis)
    assert (manifest.capability_type, manifest.readiness) == ("P3", "blocked")


def test_prompt_token_warning_uses_runtime_utf8_estimator() -> None:
    assert estimate_input_tokens("中" * 32_000) == 24_000
    assert prompt_budget_warning(23_999) is None
    assert prompt_budget_warning(24_000) == "high"
