from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.expert_taxonomy import (
    ExpertTaxonomyAssignmentRequest,
    assign_expert_taxonomy,
    get_expert_taxonomy,
)
from app.db.models import AgentProfile, Tenant, User


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
        db.add(Tenant(id="tenant_other", name="Other"))
        db.add_all(
            [
                User(id="admin", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x"),
                User(id="member", tenant_id="tenant_demo", username="member", role="member", password_hash="x"),
                User(id="other", tenant_id="tenant_other", username="other", role="admin", password_hash="x"),
            ]
        )
        for index in range(2):
            db.add(
                AgentProfile(
                    id=f"expert_{index}",
                    tenant_id="tenant_demo",
                    name=f"专家{index}",
                    description="中文简介",
                    persona_prompt="中文提示词",
                    original_name="Expert",
                    original_description="English description",
                    original_persona_prompt="English prompt",
                    original_locale="en-US",
                    published_to_gallery=True,
                    agent_category_code="professional",
                    metadata_json={
                        "employee_type": "expert",
                        "expert_source_code": "agency-agents",
                        "expert_category": "工程研发",
                        "expert_category_original": "engineering",
                        "expert_subcategory": "前端与客户端",
                        "expert_subcategory_original": "frontend",
                        "expert_taxonomy_version": 1,
                        "expert_capability_manifest": {"readiness": "ready"},
                        "published_to_gallery": True,
                        "keep": {"nested": True},
                    },
                )
            )
        db.add(
            AgentProfile(
                id="ordinary",
                tenant_id="tenant_demo",
                name="普通员工",
                persona_prompt="ordinary",
                metadata_json={"employee_type": "employee"},
            )
        )
        db.commit()
    return factory


def _request(*agent_ids: str) -> ExpertTaxonomyAssignmentRequest:
    return ExpertTaxonomyAssignmentRequest(
        tenant_id="tenant_demo",
        agent_ids=list(agent_ids),
        category="工程研发",
        subcategory="AI 与智能体",
    )


def test_taxonomy_read_returns_stably_sorted_version_one_categories() -> None:
    factory = _factory()
    with factory() as db:
        admin = db.get(User, "admin")
        assert admin is not None
        result = get_expert_taxonomy("tenant_demo", db=db, current_user=admin)
    assert result.version == 1
    assert [item.name for item in result.categories] == sorted(item.name for item in result.categories)
    engineering = next(item for item in result.categories if item.name == "工程研发")
    assert engineering.subcategories == sorted(engineering.subcategories)
    assert "AI 与智能体" in engineering.subcategories


def test_assignment_updates_only_classification_and_audit_fields() -> None:
    factory = _factory()
    with factory() as db:
        admin = db.get(User, "admin")
        expert = db.get(AgentProfile, "expert_0")
        assert admin is not None and expert is not None
        immutable_before = (
            expert.name,
            expert.description,
            expert.persona_prompt,
            expert.original_name,
            expert.original_description,
            expert.original_persona_prompt,
            expert.status,
            expert.metadata_json["expert_category_original"],
            expert.metadata_json["expert_subcategory_original"],
            expert.metadata_json["expert_taxonomy_version"],
            expert.metadata_json["expert_capability_manifest"],
            expert.metadata_json["published_to_gallery"],
            expert.metadata_json["keep"],
        )
        result = assign_expert_taxonomy(_request("expert_0"), db=db, current_user=admin)
        db.refresh(expert)
        assert result.updated_count == 1
        assert expert.metadata_json["expert_category"] == "工程研发"
        assert expert.metadata_json["expert_subcategory"] == "AI 与智能体"
        assert expert.metadata_json["role_name"] == "工程研发"
        assert expert.metadata_json["expert_taxonomy_manually_edited"] is True
        assert expert.metadata_json["expert_taxonomy_updated_by"] == "admin"
        datetime.fromisoformat(expert.metadata_json["expert_taxonomy_updated_at"].replace("Z", "+00:00"))
        immutable_after = (
            expert.name,
            expert.description,
            expert.persona_prompt,
            expert.original_name,
            expert.original_description,
            expert.original_persona_prompt,
            expert.status,
            expert.metadata_json["expert_category_original"],
            expert.metadata_json["expert_subcategory_original"],
            expert.metadata_json["expert_taxonomy_version"],
            expert.metadata_json["expert_capability_manifest"],
            expert.metadata_json["published_to_gallery"],
            expert.metadata_json["keep"],
        )
        assert immutable_after == immutable_before


def test_assignment_rejects_member_invalid_pair_missing_and_non_expert_atomically() -> None:
    factory = _factory()
    with factory() as db:
        admin = db.get(User, "admin")
        member = db.get(User, "member")
        expert = db.get(AgentProfile, "expert_0")
        assert admin is not None and member is not None and expert is not None
        original = dict(expert.metadata_json)
        with pytest.raises(HTTPException) as member_error:
            assign_expert_taxonomy(_request("expert_0"), db=db, current_user=member)
        assert member_error.value.status_code == 403
        invalid = _request("expert_0")
        invalid.subcategory = "不存在"
        with pytest.raises(HTTPException) as pair_error:
            assign_expert_taxonomy(invalid, db=db, current_user=admin)
        assert pair_error.value.status_code == 400
        with pytest.raises(HTTPException) as missing_error:
            assign_expert_taxonomy(_request("missing"), db=db, current_user=admin)
        assert missing_error.value.status_code == 404
        with pytest.raises(HTTPException) as ordinary_error:
            assign_expert_taxonomy(_request("expert_0", "ordinary"), db=db, current_user=admin)
        assert ordinary_error.value.status_code == 400
        db.refresh(expert)
        assert expert.metadata_json == original


def test_assignment_deduplicates_ids_and_limits_distinct_targets() -> None:
    request = _request("expert_0", "expert_0", "expert_1")
    assert request.agent_ids == ["expert_0", "expert_1"]
    with pytest.raises(ValidationError):
        ExpertTaxonomyAssignmentRequest(
            tenant_id="tenant_demo",
            agent_ids=[f"expert_{index}" for index in range(501)],
            category="工程研发",
            subcategory="AI 与智能体",
        )


def test_taxonomy_rejects_cross_tenant_access() -> None:
    factory = _factory()
    with factory() as db:
        other = db.get(User, "other")
        assert other is not None
        with pytest.raises(HTTPException) as error:
            get_expert_taxonomy("tenant_demo", db=db, current_user=other)
        assert error.value.status_code == 403
