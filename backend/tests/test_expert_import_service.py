from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    ModelConfig,
    Tenant,
    User,
)
from app.experts.capability import build_capability_manifest, estimate_input_tokens
from app.experts.import_service import (
    ExpertImportError,
    apply_package,
    rollback_apply_result,
)
from app.experts.local_source import LocalSource
from app.experts.package import prepare_expert, write_preview_package
from app.experts.parser import DeclaredService, ParsedExpert
from app.experts.schema import CapabilityAnalysis, ExpertTranslation


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
                display_name="Administrator",
                role="admin",
                password_hash="x",
            )
        )
        db.add(
            User(
                id="user_member",
                tenant_id="tenant_demo",
                username="member",
                role="member",
                password_hash="x",
            )
        )
        db.commit()
    return factory


def _prepared():
    parsed = ParsedExpert(
        upstream_path="engineering/frontend.md",
        name="Frontend Developer",
        description="Build interfaces.",
        category_original="engineering",
        emoji="🎨",
        color="#336699",
        vibe="Ships accessible interfaces",
        author="Example Author",
        tools=["WebFetch", "WebSearch"],
        services=[DeclaredService(name="Example MCP", url="https://example.com/mcp")],
        source_markdown="# Frontend Developer\nBuild interfaces.",
        source_sha256="a" * 64,
    )
    analysis = CapabilityAnalysis(
        required_capabilities=["prompt_reasoning"],
        orchestration_required=False,
        core_execution_requires_external_capability=True,
        evidence=["Build interfaces."],
    )
    translation = ExpertTranslation(
        name_zh="前端开发专家",
        description_zh="构建前端界面",
        category_zh="工程研发",
        tags_zh=["React", "可访问性"],
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


def _package(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    source = LocalSource(
        root=source_root,
        commit_sha="b" * 40,
        remote_url="https://github.com/msitarzewski/agency-agents.git",
        verified=True,
    )
    output = tmp_path / "preview"
    write_preview_package(output, source, "tenant_demo", [_prepared()], [])
    return output


def test_apply_creates_blank_active_unpublished_expert_owned_by_admin(tmp_path: Path) -> None:
    factory = _session_factory()
    result = apply_package(factory, _package(tmp_path), "tenant_demo", "admin")
    assert result.created_count == 1
    with factory() as db:
        agent = db.get(AgentProfile, result.items[0].agent_id)
        assert agent is not None
        assert (agent.name, agent.status, agent.is_overall) == ("前端开发专家", "active", False)
        assert agent.persona_prompt.startswith("# 前端开发专家")
        assert agent.metadata_json["published_to_gallery"] is False
        assert agent.metadata_json["owner_username"] == "admin"
        assert agent.metadata_json["expert_declared_tools"] == ["WebFetch", "WebSearch"]
        assert agent.metadata_json["expert_services"][0]["name"] == "Example MCP"
        assert agent.metadata_json["expert_capability_manifest"]["capability_type"] == "P2"
        assert agent.metadata_json["expert_capability_manifest"]["readiness"] == "partial"
        assert agent.metadata_json["expert_prompt_estimated_tokens"] > 0
        assert not db.exec(
            select(AgentResourceBinding).where(AgentResourceBinding.agent_id == agent.id)
        ).all()
        assert not db.exec(
            select(AgentModelBinding).where(AgentModelBinding.agent_id == agent.id)
        ).all()


def test_apply_repeats_as_skip_without_overwriting_local_changes(tmp_path: Path) -> None:
    factory = _session_factory()
    package = _package(tmp_path)
    first = apply_package(factory, package, "tenant_demo", "admin")
    with factory() as db:
        agent = db.get(AgentProfile, first.items[0].agent_id)
        assert agent is not None
        agent.persona_prompt = "本地维护版本"
        db.add(agent)
        db.commit()

    second = apply_package(factory, package, "tenant_demo", "admin")
    assert second.skipped_count == 1
    with factory() as db:
        assert db.get(AgentProfile, first.items[0].agent_id).persona_prompt == "本地维护版本"


def test_apply_rejects_non_admin_before_writing(tmp_path: Path) -> None:
    factory = _session_factory()
    with pytest.raises(ExpertImportError, match="administrator"):
        apply_package(factory, _package(tmp_path), "tenant_demo", "member")
    with factory() as db:
        assert not db.exec(select(AgentProfile)).all()


def test_apply_uses_stable_suffix_then_reports_name_conflict(tmp_path: Path) -> None:
    factory = _session_factory()
    with factory() as db:
        db.add(AgentProfile(tenant_id="tenant_demo", name="前端开发专家"))
        db.add(AgentProfile(tenant_id="tenant_demo", name="前端开发专家（Agency Agents）"))
        db.commit()
    result = apply_package(factory, _package(tmp_path), "tenant_demo", "admin")
    assert result.items[0].status == "failed_name_conflict"


def test_rollback_deletes_only_unchanged_unused_unpublished_imports(tmp_path: Path) -> None:
    factory = _session_factory()
    applied = apply_package(factory, _package(tmp_path), "tenant_demo", "admin")
    agent_id = applied.items[0].agent_id
    result = rollback_apply_result(factory, applied.result_path, "tenant_demo", "admin")
    assert result.deleted_count == 1
    with factory() as db:
        assert db.get(AgentProfile, agent_id) is None


@pytest.mark.parametrize(
    "mutation",
    ["edited", "published", "resource_bound", "model_bound", "used", "has_session"],
)
def test_rollback_skips_modified_or_used_expert(tmp_path: Path, mutation: str) -> None:
    factory = _session_factory()
    applied = apply_package(factory, _package(tmp_path), "tenant_demo", "admin")
    agent_id = applied.items[0].agent_id
    with factory() as db:
        agent = db.get(AgentProfile, agent_id)
        assert agent is not None
        if mutation == "edited":
            agent.updated_at += timedelta(seconds=1)
            db.add(agent)
        elif mutation == "published":
            agent.published_to_gallery = True
            agent.metadata_json = {**agent.metadata_json, "published_to_gallery": True}
            db.add(agent)
        elif mutation == "resource_bound":
            db.add(
                AgentResourceBinding(
                    tenant_id="tenant_demo",
                    agent_id=agent_id,
                    resource_type="tool",
                    resource_id="tool_x",
                )
            )
        elif mutation == "model_bound":
            db.add(
                ModelConfig(
                    id="model_x",
                    tenant_id="tenant_demo",
                    name="Model",
                    api_key_encrypted="x",
                    model="model",
                )
            )
            db.add(
                AgentModelBinding(
                    tenant_id="tenant_demo",
                    agent_id=agent_id,
                    model_config_id="model_x",
                )
            )
        elif mutation == "used":
            db.add(
                AgentUsage(
                    tenant_id="tenant_demo",
                    user_id="user_admin",
                    agent_id=agent_id,
                )
            )
        else:
            db.add(ChatSession(id="session_x", tenant_id="tenant_demo", agent_id=agent_id))
        db.commit()

    result = rollback_apply_result(factory, applied.result_path, "tenant_demo", "admin")
    assert result.items[0].status == "skipped_modified_or_used"
    with factory() as db:
        assert db.get(AgentProfile, agent_id) is not None
