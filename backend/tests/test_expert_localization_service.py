from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, Tenant, User
from app.experts import localization_service
from app.experts.localization_schema import LocalizationManifest, LocalizedExpert


def session_factory():
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
        db.add(
            AgentProfile(
                id="agent_expert",
                tenant_id="tenant_demo",
                name="Offline Expert",
                description="Think carefully.",
                persona_prompt="# Expert\nThink carefully.",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "expert_category": "工程研发",
                    "upstream_path": "engineering/offline.md",
                    "upstream_commit": "b" * 40,
                    "import_batch_id": "batch_source",
                    "published_to_gallery": False,
                    "expert_capability_manifest": {"capability_type": "P0"},
                },
            )
        )
        db.commit()
    return factory


def package_values():
    expert = LocalizedExpert(
        upstream_path="engineering/offline.md",
        source_batch_id="batch_source",
        source_commit="b" * 40,
        source_content_sha256="c" * 64,
        original_name="Offline Expert",
        original_description="Think carefully.",
        original_prompt="# Expert\nThink carefully.",
        localized_name="离线专家",
        localized_description="谨慎分析问题。",
        localized_prompt="# 专家\n谨慎思考。",
        category_zh="工程研发",
        chunks=[],
        translation_sha256="d" * 64,
    )
    manifest = LocalizationManifest(
        generated_at=datetime.now(timezone.utc),
        tenant_id="tenant_demo",
        source_batch_id="batch_source",
        source_commit="b" * 40,
        model_config_id="model_deepseek",
        model_name="deepseek-v4-flash",
        selected_count=1,
        verified_count=1,
        failed_count=0,
        experts=[],
        manifest_sha256="e" * 64,
    )
    return manifest, [expert]


def test_apply_preserves_english_and_updates_chinese_idempotently(
    monkeypatch, tmp_path: Path
) -> None:
    factory = session_factory()
    monkeypatch.setattr(
        localization_service,
        "load_and_verify_localization_package",
        lambda *args: package_values(),
    )
    first = localization_service.apply_localization_package(
        factory, tmp_path, "tenant_demo", "admin"
    )
    assert first.updated_count == 1
    with factory() as db:
        agent = db.get(AgentProfile, "agent_expert")
        assert agent is not None
        assert (agent.name, agent.description, agent.persona_prompt) == (
            "离线专家",
            "谨慎分析问题。",
            "# 专家\n谨慎思考。",
        )
        assert agent.original_name == "Offline Expert"
        assert agent.original_description == "Think carefully."
        assert agent.original_persona_prompt == "# Expert\nThink carefully."
        assert agent.original_locale == "en-US"
        assert agent.metadata_json["role_name"] == "工程研发"
        assert agent.metadata_json["expert_capability_manifest"] == {"capability_type": "P0"}
        assert agent.metadata_json["published_to_gallery"] is False

    repeated = localization_service.apply_localization_package(
        factory, tmp_path, "tenant_demo", "admin"
    )
    assert repeated.skipped_count == 1
    assert repeated.items[0].status == "skipped_existing_translation"


def test_apply_skips_expert_modified_after_import(monkeypatch, tmp_path: Path) -> None:
    factory = session_factory()
    with factory() as db:
        agent = db.get(AgentProfile, "agent_expert")
        assert agent is not None
        agent.description = "Locally edited"
        db.add(agent)
        db.commit()
    monkeypatch.setattr(
        localization_service,
        "load_and_verify_localization_package",
        lambda *args: package_values(),
    )
    result = localization_service.apply_localization_package(
        factory, tmp_path, "tenant_demo", "admin"
    )
    assert result.items[0].status == "skipped_modified"


def test_rollback_restores_runtime_english_but_keeps_original_columns(
    monkeypatch, tmp_path: Path
) -> None:
    factory = session_factory()
    monkeypatch.setattr(
        localization_service,
        "load_and_verify_localization_package",
        lambda *args: package_values(),
    )
    applied = localization_service.apply_localization_package(
        factory, tmp_path, "tenant_demo", "admin"
    )
    rolled_back = localization_service.rollback_localization_result(
        factory, applied.result_path, "tenant_demo", "admin"
    )
    assert rolled_back.restored_count == 1
    with factory() as db:
        agent = db.get(AgentProfile, "agent_expert")
        assert agent is not None
        assert agent.name == agent.original_name == "Offline Expert"
        assert agent.persona_prompt == agent.original_persona_prompt
        assert agent.metadata_json["expert_translation_status"] == "rolled_back"
