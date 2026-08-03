from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, Tenant, User
from app.experts.taxonomy_schema import AGENCY_AGENTS_SOURCE_COMMIT
from app.experts.taxonomy_service import (
    ExpertTaxonomyApplyError,
    apply_taxonomy,
    check_taxonomy,
)


def _factory():
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
                name="数据工程师",
                description="构建数据管道",
                persona_prompt="# 数据工程师",
                original_name="Data Engineer",
                original_description="Build data pipelines",
                original_persona_prompt="# Data Engineer",
                original_locale="en-US",
                status="active",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "expert_category": "工程研发",
                    "upstream_path": "engineering/engineering-data-engineer.md",
                    "published_to_gallery": False,
                    "keep": {"nested": True},
                },
            )
        )
        db.add(
            AgentProfile(
                id="ordinary",
                tenant_id="tenant_demo",
                name="普通员工",
                persona_prompt="help",
                published_to_gallery=True,
                metadata_json={"published_to_gallery": True, "keep": "ordinary"},
            )
        )
        db.commit()
    return factory


def _taxonomy(tmp_path: Path, *, category: str = "工程研发") -> Path:
    path = tmp_path / "taxonomy.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_code": "agency-agents",
                "source_commit": AGENCY_AGENTS_SOURCE_COMMIT,
                "experts": [
                    {
                        "upstream_path": "engineering/engineering-data-engineer.md",
                        "category": category,
                        "subcategory": "数据与数据库",
                        "subcategory_original": "",
                        "basis": "curated_role_mapping",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_check_is_read_only_and_apply_only_changes_taxonomy_metadata(tmp_path: Path) -> None:
    factory = _factory()
    taxonomy = _taxonomy(tmp_path)
    with factory() as db:
        before = db.get(AgentProfile, "agent_expert")
        ordinary = db.get(AgentProfile, "ordinary")
        assert before is not None and ordinary is not None
        fields_before = (
            before.name,
            before.description,
            before.persona_prompt,
            before.original_persona_prompt,
            before.status,
        )
        ordinary_before = ordinary.metadata_json.copy()

    checked = check_taxonomy(
        factory, taxonomy, "tenant_demo", "admin", expected_count=None
    )
    assert checked.counts == {"ready": 1}
    with factory() as db:
        unchanged = db.get(AgentProfile, "agent_expert")
        assert unchanged is not None
        assert "expert_subcategory" not in unchanged.metadata_json

    applied = apply_taxonomy(
        factory, taxonomy, "tenant_demo", "admin", expected_count=None
    )
    assert applied.counts == {"updated": 1}
    with factory() as db:
        expert = db.get(AgentProfile, "agent_expert")
        ordinary = db.get(AgentProfile, "ordinary")
        assert expert is not None and ordinary is not None
        assert expert.metadata_json["expert_subcategory"] == "数据与数据库"
        assert expert.metadata_json["expert_subcategory_original"] == ""
        assert expert.metadata_json["expert_subcategory_basis"] == "curated_role_mapping"
        assert expert.metadata_json["expert_taxonomy_version"] == 1
        assert expert.metadata_json["keep"] == {"nested": True}
        assert (
            expert.name,
            expert.description,
            expert.persona_prompt,
            expert.original_persona_prompt,
            expert.status,
        ) == fields_before
        assert ordinary.metadata_json == ordinary_before

    repeated = apply_taxonomy(
        factory, taxonomy, "tenant_demo", "admin", expected_count=None
    )
    assert repeated.counts == {"skipped_unchanged": 1}


def test_apply_reports_category_mismatch_without_writing(tmp_path: Path) -> None:
    factory = _factory()
    taxonomy = _taxonomy(tmp_path, category="产品管理")
    value = json.loads(taxonomy.read_text(encoding="utf-8"))
    value["experts"][0]["subcategory"] = "产品战略"
    taxonomy.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    result = apply_taxonomy(
        factory, taxonomy, "tenant_demo", "admin", expected_count=None
    )

    assert result.counts == {"category_mismatch": 1}
    with factory() as db:
        agent = db.get(AgentProfile, "agent_expert")
        assert agent is not None
        assert "expert_subcategory" not in agent.metadata_json


def test_check_reports_missing_and_unmapped_agents(tmp_path: Path) -> None:
    factory = _factory()
    taxonomy = _taxonomy(tmp_path)
    value = json.loads(taxonomy.read_text(encoding="utf-8"))
    value["experts"][0]["upstream_path"] = "engineering/missing.md"
    taxonomy.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    result = check_taxonomy(
        factory, taxonomy, "tenant_demo", "admin", expected_count=None
    )

    assert result.counts == {"missing": 1, "unmapped_agent": 1}


def test_taxonomy_requires_tenant_admin(tmp_path: Path) -> None:
    factory = _factory()

    with pytest.raises(ExpertTaxonomyApplyError, match="administrator"):
        check_taxonomy(
            factory, _taxonomy(tmp_path), "tenant_demo", "missing", expected_count=None
        )
