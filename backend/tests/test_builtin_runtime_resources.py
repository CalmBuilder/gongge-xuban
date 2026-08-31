"""
@Time       : 2026/08/31
@Author     : zhanglp8181
@File       : test_builtin_runtime_resources.py
@CallChain  : pytest → seed_demo_data → 内置专家/Skill → 专家广场与 Skill 目录
@Description: 验证发布包随附的内置专家和 Skill 启动后直接可用且重复初始化不复制数据。
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.agents.branching import is_open_gallery_resource
from app.agents.schema import AgentResourceImportRequest
from app.api.agents import import_agent_resources, list_agents, page_managed_agents
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillRevision,
    Tenant,
    User,
)
from app.db.seed import seed_demo_data
from app.experts.builtin import (
    BUILTIN_EXPERT_EXPECTED_COUNT,
    ensure_builtin_experts_for_tenant,
    load_builtin_expert_package,
    seed_builtin_experts,
    seed_builtin_experts_for_existing_tenants,
)
from app.general_skills.builtin_catalog import BUILTIN_SKILL_EXPECTED_COUNT


def _seed_database() -> Session:
    """创建完整模型的隔离 SQLite 会话，模拟桌面版首次启动数据库。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_built_in_experts_and_skills_are_approved_and_available() -> None:
    """验证内置专家与 Skill 首次启动即为发布状态，且两次启动保持稳定。"""

    with _seed_database() as db:
        seed_demo_data(db)
        experts = [
            row
            for row in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == "tenant_demo"))
            if (row.metadata_json or {}).get("builtin_expert") is True
        ]
        assert len(experts) == BUILTIN_EXPERT_EXPECTED_COUNT
        assert len({row.id for row in experts}) == BUILTIN_EXPERT_EXPECTED_COUNT
        assert len({(row.metadata_json or {}).get("upstream_path") for row in experts}) == 273
        assert all(row.status == "active" for row in experts)
        assert all(row.published_to_gallery is True for row in experts)
        assert all(row.agent_category_code == "professional" for row in experts)
        assert all(row.visibility_scope == "tenant" for row in experts)
        assert all(
            (row.metadata_json or {}).get(key) == expected
            for row in experts
            for key, expected in (
                ("review_status", "approved"),
                ("approval_status", "approved"),
                ("audit_status", "approved"),
                ("availability_status", "available"),
            )
        )
        admin = db.get(User, "admin")
        assert admin is not None
        management_page = page_managed_agents(
            tenant_id="tenant_demo",
            view="expert",
            q=None,
            expert_source=None,
            expert_department=None,
            expert_direction=None,
            page=1,
            page_size=12,
            db=db,
            current_user=admin,
        )
        assert management_page.total >= BUILTIN_EXPERT_EXPECTED_COUNT
        assert management_page.view_counts["expert"] >= BUILTIN_EXPERT_EXPECTED_COUNT

        skills = [
            row
            for row in db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.catalog_scope == "platform",
                    GeneralSkill.tenant_id.is_(None),
                )
            )
            if (row.metadata_json or {}).get("source_kind") == "platform_builtin"
        ]
        assert len(skills) == BUILTIN_SKILL_EXPECTED_COUNT
        assert all(row.status == "published" for row in skills)
        assert all((row.metadata_json or {}).get("review_status") == "approved" for row in skills)
        revisions = [
            db.get(GeneralSkillRevision, row.current_published_revision_id or "")
            for row in skills
        ]
        assert all(
            revision is not None
            and revision.status == "published"
            and revision.skill_id == skills[index].id
            for index, revision in enumerate(revisions)
        )

        first_expert_state = {row.id: row.profile_revision for row in experts}
        first_skill_state = {row.id: row.current_published_revision_id for row in skills}
        seed_demo_data(db)
        second_experts = [
            row
            for row in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == "tenant_demo"))
            if (row.metadata_json or {}).get("builtin_expert") is True
        ]
        second_skills = [
            row
            for row in db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.catalog_scope == "platform",
                    GeneralSkill.tenant_id.is_(None),
                )
            )
            if (row.metadata_json or {}).get("source_kind") == "platform_builtin"
        ]
        assert {row.id: row.profile_revision for row in second_experts} == first_expert_state
        assert {row.id: row.current_published_revision_id for row in second_skills} == first_skill_state


def test_login_reconciliation_repairs_builtin_expert_drift() -> None:
    """验证数量未变化时，登录边界仍会修复被下架或改写的内置专家。"""

    with _seed_database() as db:
        seed_demo_data(db)
        target = next(
            row
            for row in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == "tenant_demo"))
            if (row.metadata_json or {}).get("builtin_expert") is True
        )
        builtin_key = target.metadata_json["builtin_expert_key"]
        package = load_builtin_expert_package()
        expected_record = next(
            record
            for record in package.records
            if f"agency-agents:{record.expert.parsed.upstream_path}" == builtin_key
        )
        target.status = "archived"
        target.persona_prompt = "被错误覆盖的专家提示词"
        db.add(target)
        db.commit()

        result = ensure_builtin_experts_for_tenant(db, tenant_id="tenant_demo")

        repaired = db.get(AgentProfile, target.id)
        assert repaired is not None
        assert result is not None
        assert result.updated_count == 1
        assert repaired.status == "active"
        assert repaired.persona_prompt == expected_record.expert.translation.markdown_zh
        assert repaired.metadata_json["review_status"] == "approved"


def test_legacy_imported_expert_is_upgraded_in_place() -> None:
    """验证既有离线导入专家沿用原主键并升级为平台内置可用模板。"""

    with _seed_database() as db:
        tenant = Tenant(id="tenant_upgrade", name="Upgrade tenant")
        admin = User(
            id="admin_upgrade",
            tenant_id=tenant.id,
            username="admin",
            role="admin",
            password_hash="unused",
        )
        first = load_builtin_expert_package().records[0]
        db.add(tenant)
        db.add(admin)
        db.add(
            AgentProfile(
                id="legacy-expert-id",
                tenant_id=tenant.id,
                name=first.expert.translation.name_zh,
                description="旧版中文专家",
                persona_prompt=first.expert.translation.markdown_zh,
                status="active",
                owner_user_id=admin.id,
                agent_category_code="professional",
                metadata_json={
                    "employee_type": "expert",
                    "expert_source_code": "agency-agents",
                    "upstream_path": first.expert.parsed.upstream_path,
                    "governance_template": True,
                    "import_batch_id": "legacy-import",
                },
            )
        )
        db.commit()

        result = seed_builtin_experts(db, tenant_id=tenant.id, admin=admin)

        upgraded = db.get(AgentProfile, "legacy-expert-id")
        assert result.created_count == BUILTIN_EXPERT_EXPECTED_COUNT - 1
        assert result.updated_count == 1
        assert upgraded is not None
        assert upgraded.metadata_json["builtin_expert"] is True
        assert upgraded.metadata_json["review_status"] == "approved"
        assert upgraded.published_to_gallery is True
        assert upgraded.status == "active"


def test_builtin_experts_are_provisioned_for_each_tenant_and_visible_to_members() -> None:
    """验证启动补齐已有租户、登录懒补新租户且普通成员能看到全部专家。"""

    with _seed_database() as db:
        tenant = Tenant(id="tenant_other", name="Other tenant")
        admin = User(
            id="admin_other",
            tenant_id=tenant.id,
            username="tenant-admin",
            role="admin",
            password_hash="unused",
        )
        member = User(
            id="member_other",
            tenant_id=tenant.id,
            username="member",
            role="member",
            password_hash="unused",
        )
        db.add(tenant)
        db.add(admin)
        db.add(member)
        db.commit()

        results = seed_builtin_experts_for_existing_tenants(db)
        assert results[tenant.id].created_count == BUILTIN_EXPERT_EXPECTED_COUNT
        stored = db.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == tenant.id)
        ).all()
        builtin_stored = [
            row for row in stored if (row.metadata_json or {}).get("builtin_expert") is True
        ]
        assert all((row.metadata_json or {}).get("approval_status") == "approved" for row in builtin_stored)
        visible = list_agents(tenant.id, db, member, scope="expert")
        assert len(visible) == BUILTIN_EXPERT_EXPECTED_COUNT
        assert all(row.status == "active" for row in visible)
        assert all(row.published_to_gallery is True for row in visible)
        assert all(row.metadata.get("expert_source_code") == "agency-agents" for row in visible)

        new_tenant = Tenant(id="tenant_lazy", name="Lazy tenant")
        lazy_admin = User(
            id="admin_lazy",
            tenant_id=new_tenant.id,
            username="admin",
            role="admin",
            password_hash="unused",
        )
        db.add(new_tenant)
        db.add(lazy_admin)
        db.commit()
        lazy_result = ensure_builtin_experts_for_tenant(db, tenant_id=new_tenant.id)
        assert lazy_result is not None
        assert lazy_result.created_count == BUILTIN_EXPERT_EXPECTED_COUNT
        second_lazy_result = ensure_builtin_experts_for_tenant(db, tenant_id=new_tenant.id)
        assert second_lazy_result is not None
        assert second_lazy_result.unchanged_count == BUILTIN_EXPERT_EXPECTED_COUNT


def test_platform_builtin_skill_can_use_legacy_import_without_overall_binding() -> None:
    """验证平台级已发布 Skill 可从开放广场兼容入口安装到成员能力分身。"""

    with _seed_database() as db:
        tenant = Tenant(id="tenant_demo", name="Demo")
        admin = User(
            id="user_admin",
            tenant_id=tenant.id,
            username="admin",
            role="admin",
            password_hash="unused",
        )
        overall = AgentProfile(
            id="agent_overall",
            tenant_id=tenant.id,
            name="开放广场",
            is_overall=True,
        )
        target = AgentProfile(
            id="agent_member",
            tenant_id=tenant.id,
            name="成员能力分身",
            owner_user_id=admin.id,
        )
        skill = GeneralSkill(
            id="platform_skill",
            tenant_id=None,
            catalog_scope="platform",
            catalog_key="platform:approved-skill",
            slug="approved-skill",
            name="Approved Skill",
            skill_markdown="# Approved Skill",
            status="published",
            metadata_json={"managed_catalog": True, "catalog_scope": "platform"},
            visibility_scope="platform_gallery",
        )
        db.add_all([tenant, admin, overall, target, skill])
        db.commit()

        assert is_open_gallery_resource(db, tenant.id, "general_skill", skill) is True
        result = import_agent_resources(
            target.id,
            AgentResourceImportRequest(
                tenant_id=tenant.id,
                source_agent_id=overall.id,
                resource_type="general_skill",
                resource_ids=[skill.id],
            ),
            db,
            current_user=admin,
        )
        assert result["missing"] == []
        assert result["imported"] == [
            {
                "resource_type": "general_skill",
                "resource_id": skill.id,
                "display_id": skill.slug,
                "name": skill.name,
            }
        ]
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant.id,
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()
        assert binding.status == "active"

        skill.status = "archived"
        db.add(skill)
        db.commit()
        assert is_open_gallery_resource(db, tenant.id, "general_skill", skill) is False


def test_builtin_fixture_accepts_windows_line_endings() -> None:
    """验证 Git 在 Windows 工作树转换换行时不会误判固定包被篡改。"""

    fixture_path = Path(__file__).resolve().parents[1] / "app/experts/data/agency_agents_builtin_v2.json"
    payload = fixture_path.read_bytes()
    package = load_builtin_expert_package(payload=payload.replace(b"\n", b"\r\n"))

    assert len(package.records) == BUILTIN_EXPERT_EXPECTED_COUNT
