from __future__ import annotations

from pathlib import Path

from app.experts.capability import build_capability_manifest
from app.experts.localization_integrity import FidelityReport
from app.experts.localization_package import (
    load_and_verify_localization_package,
    prepare_localization_package,
)
from app.experts.localization_translation import (
    LocalizedIdentity,
    UpstreamNameMapping,
    VerifiedChunkTranslation,
)
from app.experts.local_source import LocalSource
from app.experts.package import prepare_expert, write_preview_package
from app.experts.parser import ParsedExpert
from app.experts.schema import CapabilityAnalysis, ExpertTranslation


class FakeTranslator:
    def __init__(self) -> None:
        self.chunk_calls = 0
        self.identity_calls = 0

    def translate_identity(self, expert: ParsedExpert) -> LocalizedIdentity:
        self.identity_calls += 1
        return LocalizedIdentity(
            upstream_path=expert.upstream_path,
            source_sha256=expert.source_sha256,
            name_zh="离线专家",
            description_zh="谨慎分析问题",
        )

    def translate_chunk(self, expert, chunk, localized_name):  # noqa: ANN001, ANN201
        self.chunk_calls += 1
        return VerifiedChunkTranslation(
            chunk_index=chunk.index,
            source_sha256=chunk.source_sha256,
            translated_markdown=chunk.source_text.replace("Expert", "专家").replace(
                "Think carefully.", "谨慎思考。"
            ),
            fidelity=FidelityReport(valid=True),
            attempts=1,
        )


def source_package(tmp_path: Path) -> Path:
    parsed = ParsedExpert(
        upstream_path="engineering/offline.md",
        name="Offline Expert",
        description="Think carefully.",
        category_original="engineering",
        tools=[],
        services=[],
        source_markdown="# Expert\nThink carefully.",
        source_sha256="a" * 64,
    )
    analysis = CapabilityAnalysis(
        required_capabilities=["prompt_reasoning"], evidence=["Think carefully."]
    )
    translation = ExpertTranslation(
        name_zh=parsed.name,
        description_zh=parsed.description,
        category_zh="工程研发",
        tags_zh=[],
        markdown_zh=parsed.source_markdown,
        high_risk=False,
        capability_analysis=analysis,
    )
    prepared = prepare_expert(
        parsed,
        translation,
        build_capability_manifest(parsed, analysis),
        10,
        source_commit="b" * 40,
    )
    root = tmp_path / "source"
    root.mkdir()
    package = tmp_path / "import"
    write_preview_package(
        package,
        LocalSource(
            root=root,
            commit_sha="b" * 40,
            remote_url="https://github.com/msitarzewski/agency-agents.git",
            verified=True,
        ),
        "tenant_demo",
        [prepared],
        [],
    )
    return package


def test_prepare_resumes_verified_chunks_and_package_detects_tampering(tmp_path: Path) -> None:
    source = source_package(tmp_path)
    output = tmp_path / "zh"
    translator = FakeTranslator()
    manifest = prepare_localization_package(
        source,
        output,
        "tenant_demo",
        "model_deepseek",
        "deepseek-v4-flash",
        translator,
        {},
    )
    assert manifest.verified_count == 1
    assert translator.identity_calls == 1
    assert translator.chunk_calls == 1
    loaded_manifest, experts = load_and_verify_localization_package(output, "tenant_demo")
    assert loaded_manifest.manifest_sha256 == manifest.manifest_sha256
    assert experts[0].localized_prompt == "# 专家\n谨慎思考。"

    resumed = FakeTranslator()
    prepare_localization_package(
        source,
        output,
        "tenant_demo",
        "model_deepseek",
        "deepseek-v4-flash",
        resumed,
        {},
    )
    assert resumed.identity_calls == 0
    assert resumed.chunk_calls == 0

    expert_file = next((output / "experts").glob("*.json"))
    expert_file.write_text("{}", encoding="utf-8")
    try:
        load_and_verify_localization_package(output, "tenant_demo")
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("tampering must be rejected")


def test_upstream_identity_mapping_skips_identity_model_call(tmp_path: Path) -> None:
    source = source_package(tmp_path)
    translator = FakeTranslator()
    manifest = prepare_localization_package(
        source,
        tmp_path / "zh",
        "tenant_demo",
        "model_deepseek",
        "deepseek-v4-flash",
        translator,
        {
            "Offline Expert": UpstreamNameMapping(
                name_zh="上游中文名", description_zh="上游中文简介"
            )
        },
    )
    assert manifest.verified_count == 1
    assert translator.identity_calls == 0
