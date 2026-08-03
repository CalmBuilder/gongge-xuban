"""Agency Agents 一次性导入的确定性离线验收。"""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, Tenant, User
from app.experts.capability import build_capability_manifest, estimate_input_tokens
from app.experts.import_service import apply_package, rollback_apply_result
from app.experts.local_source import discover_source_files, inspect_local_source
from app.experts.package import load_and_verify_package, prepare_expert, write_preview_package
from app.experts.parser import ParsedExpert, parse_expert_markdown
from app.experts.schema import CapabilityAnalysis, ExpertTranslation


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "agency_agents"


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _make_git_source(tmp_path: Path) -> Path:
    source = tmp_path / "agency-agents"
    shutil.copytree(FIXTURE_ROOT, source)
    _run_git(source, "init", "-q")
    _run_git(source, "config", "user.name", "Acceptance Test")
    _run_git(source, "config", "user.email", "acceptance@example.invalid")
    _run_git(source, "remote", "add", "origin", "https://github.com/msitarzewski/agency-agents.git")
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-qm", "fixture")
    return source


def _translation(parsed: ParsedExpert) -> ExpertTranslation:
    if parsed.name == "Unity Architect":
        evidence = (
            "Delegate rendering analysis to another expert, hand off the build result to a "
            "release expert, and automatically retry the failed step once."
        )
        analysis = CapabilityAnalysis(
            required_capabilities=["prompt_reasoning", "expert_orchestration"],
            orchestration_required=True,
            core_execution_requires_external_capability=True,
            evidence=[evidence],
        )
        return ExpertTranslation(
            name_zh="Unity 架构专家",
            description_zh="协调专家处理复杂的 Unity 交付故障",
            category_zh="游戏开发",
            tags_zh=["Unity", "多专家编排"],
            markdown_zh="# Unity 架构专家\n协调专家诊断、交接并验证修复。",
            high_risk=False,
            capability_analysis=analysis,
        )
    analysis = CapabilityAnalysis(
        required_capabilities=["prompt_reasoning"],
        orchestration_required=False,
        core_execution_requires_external_capability=False,
        evidence=["Design accessible product interfaces and review frontend implementation plans."],
    )
    return ExpertTranslation(
        name_zh="前端开发专家",
        description_zh="设计可访问的产品界面并评审前端实施方案",
        category_zh="工程研发",
        tags_zh=["前端", "可访问性"],
        markdown_zh="# 前端开发专家\n设计界面、说明权衡并输出评审清单。",
        high_risk=False,
        capability_analysis=analysis,
    )


def _prepare(tmp_path: Path) -> Path:
    source = inspect_local_source(_make_git_source(tmp_path))
    experts = []
    for source_file in discover_source_files(source):
        parsed = parse_expert_markdown(source_file)
        translation = _translation(parsed)
        experts.append(
            prepare_expert(
                parsed,
                translation,
                build_capability_manifest(parsed, translation.capability_analysis),
                estimate_input_tokens(translation.markdown_zh),
                source_commit=source.commit_sha,
            )
        )
    output = tmp_path / "preview"
    write_preview_package(output, source, "tenant_demo", experts, [])
    return output


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def factory() -> Session:
        return Session(engine)

    with factory() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="user_admin",
                tenant_id="tenant_demo",
                username="admin",
                role="admin",
                password_hash="x",
            )
        )
        db.commit()
    return factory


def test_prepare_apply_repeat_and_rollback_flow(tmp_path: Path) -> None:
    preview = _prepare(tmp_path)
    manifest, experts = load_and_verify_package(preview, "tenant_demo")
    assert len(experts) == 2
    assert manifest.failed_count == 0
    assert {item.capability_manifest.capability_type for item in experts} == {"P0", "P3"}

    factory = _session_factory()
    first = apply_package(factory, preview, "tenant_demo", "admin")
    assert first.created_count == 2
    with factory() as db:
        imported = [db.get(AgentProfile, item.agent_id) for item in first.items]
        assert all(item is not None for item in imported)
        metadata = [item.metadata_json for item in imported if item is not None]
        assert all(item["published_to_gallery"] is False for item in metadata)
        assert all("expert_capability_manifest" in item for item in metadata)
        assert any(item["expert_capability_manifest"]["readiness"] == "blocked" for item in metadata)
        unity = next(item for item in metadata if item["expert_name_original"] == "Unity Architect")
        assert unity["expert_declared_tools"] == ["Read", "Write", "Bash"]
        assert unity["expert_services"][0]["name"] == "Unity Build MCP"
        assert unity["expert_vibe"].startswith("Methodical")
        assert unity["expert_author"] == "共格·序伴 acceptance fixture"

    repeated = apply_package(factory, preview, "tenant_demo", "admin")
    assert repeated.skipped_count == 2
    rollback = rollback_apply_result(factory, first.result_path, "tenant_demo", "admin")
    assert rollback.deleted_count == 2


def test_rollback_preserves_an_expert_edited_after_import(tmp_path: Path) -> None:
    preview = _prepare(tmp_path)
    factory = _session_factory()
    applied = apply_package(factory, preview, "tenant_demo", "admin")
    edited_id = applied.items[0].agent_id
    with factory() as db:
        edited = db.get(AgentProfile, edited_id)
        assert edited is not None
        edited.updated_at += timedelta(seconds=1)
        db.add(edited)
        db.commit()

    rollback = rollback_apply_result(factory, applied.result_path, "tenant_demo", "admin")
    assert rollback.deleted_count == 1
    assert {item.status for item in rollback.items} == {"deleted", "skipped_modified_or_used"}
    with factory() as db:
        assert db.get(AgentProfile, edited_id) is not None
