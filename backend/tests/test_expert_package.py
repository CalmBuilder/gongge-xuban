from __future__ import annotations

from pathlib import Path

import pytest

from app.experts.capability import build_capability_manifest, estimate_input_tokens
from app.experts.local_source import LocalSource
from app.experts.package import (
    ImportPackageError,
    load_and_verify_package,
    prepare_expert,
    write_preview_package,
)
from app.experts.parser import ParsedExpert
from app.experts.schema import CapabilityAnalysis, ExpertTranslation, PrepareError


def _source(tmp_path: Path) -> LocalSource:
    root = tmp_path / "source"
    root.mkdir()
    return LocalSource(
        root=root,
        commit_sha="b" * 40,
        remote_url="https://github.com/msitarzewski/agency-agents.git",
        verified=True,
    )


def _prepared():
    parsed = ParsedExpert(
        upstream_path="engineering/frontend.md",
        name="Frontend Developer",
        description="Build interfaces.",
        category_original="engineering",
        tools=[],
        services=[],
        source_markdown="# Frontend Developer\nBuild interfaces.",
        source_sha256="a" * 64,
    )
    analysis = CapabilityAnalysis(
        required_capabilities=["prompt_reasoning"],
        orchestration_required=False,
        core_execution_requires_external_capability=False,
        evidence=["Build interfaces."],
    )
    translation = ExpertTranslation(
        name_zh="前端开发专家",
        description_zh="构建前端界面",
        category_zh="工程研发",
        tags_zh=["React"],
        markdown_zh="# 前端开发专家\n构建前端界面。",
        high_risk=False,
        capability_analysis=analysis,
    )
    return prepare_expert(
        parsed,
        translation,
        build_capability_manifest(parsed, analysis),
        estimate_input_tokens(translation.markdown_zh),
        source_commit="b" * 40,
    )


def test_preview_package_round_trips_with_verified_hashes(tmp_path: Path) -> None:
    output = tmp_path / "preview"
    manifest = write_preview_package(
        output,
        _source(tmp_path),
        "tenant_demo",
        [_prepared()],
        [PrepareError(upstream_path="broken.md", stage="parse", message="broken")],
    )
    loaded_manifest, experts = load_and_verify_package(output, "tenant_demo")
    assert loaded_manifest.batch_id == manifest.batch_id
    assert experts[0].translation.name_zh == "前端开发专家"
    assert experts[0].capability_manifest.schema_version == "1"
    assert experts[0].prompt_estimated_tokens > 0
    assert (output / "IMPORT_REPORT.md").is_file()


def test_package_verification_rejects_modified_expert(tmp_path: Path) -> None:
    output = tmp_path / "preview"
    write_preview_package(output, _source(tmp_path), "tenant_demo", [_prepared()], [])
    expert_file = next((output / "experts").glob("*.json"))
    expert_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ImportPackageError, match="SHA-256"):
        load_and_verify_package(output, "tenant_demo")


def test_package_verification_rejects_wrong_tenant(tmp_path: Path) -> None:
    output = tmp_path / "preview"
    write_preview_package(output, _source(tmp_path), "tenant_demo", [_prepared()], [])
    with pytest.raises(ImportPackageError, match="tenant"):
        load_and_verify_package(output, "tenant_other")
