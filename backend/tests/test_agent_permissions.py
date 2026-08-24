from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    model_for_agent,
)
from app.agents.schema import (
    AgentGalleryPublicationRequest,
    AgentModelBindingInput,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileUpdateRequest,
    AgentResponsibilityUpdateRequest,
    AgentResourceBindingInput,
    AgentResourcesUpdateRequest,
)
from app.api.agents import (
    create_agent,
    delete_agent,
    list_agents,
    list_chat_agents,
    remove_chat_agent_usage,
    set_agent_gallery_publication,
    set_agent_responsibility,
    update_agent,
    update_agent_models,
    update_agent_resources,
    use_chat_agent,
)
from app.api.general_skills import import_general_skill
from app.api.tools import create_tool, update_tool
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    CodeItem,
    GeneralSkill,
    ManagementAuditLog,
    Message,
    ModelConfig,
    OrganizationUnit,
    Tenant,
    Tool,
    User,
)
from app.general_skills.schema import GeneralSkillImportRequest
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_tenant_admin,
    require_agent_scope_viewer,
)
from app.organization.reference_data import ensure_agent_category_catalog
from app.tools.tool_schema import ToolCreateRequest, ToolUpdateRequest


def test_database_default_model_is_authoritative_over_demo_seed_environment(monkeypatch) -> None:
    """验证正常运行时以管理端数据库默认模型为准，不回退到`.env`初始密钥。"""

    monkeypatch.setenv("DEMO_MODEL_API_KEY", "stale-seed-key-must-not-win")
    with _test_session() as db:
        db.add(Tenant(id="tenant_model_priority", name="模型优先级租户"))
        configured = ModelConfig(
            id="model_database_authoritative",
            tenant_id="tenant_model_priority",
            name="管理端默认模型",
            api_key_encrypted="database-managed-ciphertext",
            model="database-model",
            is_default=True,
            enabled=True,
        )
        db.add(configured)
        db.commit()

        resolved = model_for_agent(db, "tenant_model_priority", None)

        assert resolved is not None
        assert resolved.id == configured.id
        assert resolved.model == "database-model"


def test_only_owner_can_update_and_delete_agent_while_admin_is_governance_only() -> None:
    """治理管理员可以审核发布，但不能代替真实所有者编辑或删除私人配置。"""

    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as update_error:
            update_agent(
                agent.id,
                AgentProfileUpdateRequest(tenant_id="tenant_demo", name="非法修改"),
                db=db,
                current_user=other,
            )
        assert update_error.value.status_code == 403

        updated = update_agent(
            agent.id,
            AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Owner 修改"),
            db=db,
            current_user=owner,
        )
        assert updated.name == "Owner 修改"

        with pytest.raises(HTTPException) as admin_update_error:
            update_agent(
                agent.id,
                AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Admin 修改"),
                db=db,
                current_user=admin,
            )
        assert admin_update_error.value.status_code == 403

        with pytest.raises(HTTPException) as delete_error:
            delete_agent(agent.id, tenant_id="tenant_demo", db=db, current_user=other)
        assert delete_error.value.status_code == 403


def test_gallery_publication_requires_independent_admin_command() -> None:
    """普通 owner 不能借通用 metadata 发布，管理员只能通过治理命令变更广场状态。"""
    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_publication_guard",
            tenant_id="tenant_demo",
            name="待治理员工",
            is_overall=False,
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "published_to_gallery": False,
            },
        )
        db.add(agent)
        db.commit()

        owner_update = update_agent(
            agent.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                metadata={
                    **agent.metadata_json,
                    "published_to_gallery": True,
                    "gallery_published_by": owner.username,
                },
            ),
            db=db,
            current_user=owner,
        )
        assert owner_update.metadata["published_to_gallery"] is False

        with pytest.raises(HTTPException) as permission_error:
            set_agent_gallery_publication(
                agent.id,
                AgentGalleryPublicationRequest(tenant_id="tenant_demo", published=True),
                db=db,
                current_user=owner,
            )
        assert permission_error.value.status_code == 403

        published = set_agent_gallery_publication(
            agent.id,
            AgentGalleryPublicationRequest(tenant_id="tenant_demo", published=True),
            db=db,
            current_user=admin,
        )
        assert published.metadata["published_to_gallery"] is True
        assert published.metadata["gallery_published_by"] == admin.username
        assert published.metadata["gallery_published_at"]

        unpublished = set_agent_gallery_publication(
            agent.id,
            AgentGalleryPublicationRequest(tenant_id="tenant_demo", published=False),
            db=db,
            current_user=admin,
        )
        assert unpublished.metadata["published_to_gallery"] is False
        assert "gallery_published_by" not in unpublished.metadata
        assert "gallery_published_at" not in unpublished.metadata


def test_agent_responsibility_is_independent_governance_fact() -> None:
    """责任组织只能由治理者设置，且不改变资料修订、服务范围或资源授权。"""

    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        organization = OrganizationUnit(
            id="org_finance",
            tenant_id="tenant_demo",
            parent_id=None,
            code="FINANCE",
            name="财务部",
            unit_type_code="department",
            tree_path="/org_finance/",
            depth=0,
            is_root=True,
            root_tenant_id="tenant_demo",
        )
        inactive_organization = OrganizationUnit(
            id="org_inactive",
            tenant_id="tenant_demo",
            parent_id="org_finance",
            code="INACTIVE",
            name="停用部门",
            unit_type_code="department",
            tree_path="/org_finance/org_inactive/",
            depth=1,
            status="inactive",
        )
        agent = AgentProfile(
            id="agent_finance",
            tenant_id="tenant_demo",
            name="财务数字员工",
            owner_user_id=owner.id,
            published_to_gallery=True,
            visibility_scope="tenant",
            profile_revision=3,
        )
        db.add(organization)
        db.add(inactive_organization)
        db.add(agent)
        db.commit()

        request = AgentResponsibilityUpdateRequest(
            tenant_id="tenant_demo",
            responsible_org_unit_id=organization.id,
        )
        with pytest.raises(HTTPException) as owner_error:
            set_agent_responsibility(
                agent.id,
                request,
                db=db,
                current_user=owner,
            )
        assert owner_error.value.status_code == 403

        result = set_agent_responsibility(
            agent.id,
            request,
            db=db,
            current_user=admin,
        )
        assert result.responsible_org_unit_id == organization.id
        assert result.responsible_org_unit_name == organization.name
        assert result.profile_revision == 3
        assert result.visibility_scope == "tenant"

        set_agent_responsibility(
            agent.id,
            request,
            db=db,
            current_user=admin,
        )
        audit_rows = db.exec(
            select(ManagementAuditLog).where(
                ManagementAuditLog.action == "agent.responsibility.update"
            )
        ).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].detail_json["authorization_effect"] == "none"

        with pytest.raises(HTTPException) as inactive_error:
            set_agent_responsibility(
                agent.id,
                AgentResponsibilityUpdateRequest(
                    tenant_id="tenant_demo",
                    responsible_org_unit_id=inactive_organization.id,
                ),
                db=db,
                current_user=admin,
            )
        assert inactive_error.value.status_code == 422
        assert inactive_error.value.detail["code"] == "INVALID_AGENT_RESPONSIBLE_ORGANIZATION"

        foreign_tenant = Tenant(id="tenant_foreign", name="其他企业")
        foreign_organization = OrganizationUnit(
            id="org_foreign",
            tenant_id=foreign_tenant.id,
            parent_id=None,
            code="ROOT",
            name="其他企业组织",
            unit_type_code="company",
            tree_path="org_foreign",
            depth=0,
            is_root=True,
            root_tenant_id=foreign_tenant.id,
        )
        db.add(foreign_tenant)
        db.add(foreign_organization)
        agent.responsible_org_unit_id = foreign_organization.id
        db.add(agent)
        db.commit()
        listed = list_agents(
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
            scope="manageable",
        )
        listed_agent = next(item for item in listed if item.id == agent.id)
        assert listed_agent.responsible_org_unit_id == foreign_organization.id
        assert listed_agent.responsible_org_unit_name is None

        cleared = set_agent_responsibility(
            agent.id,
            AgentResponsibilityUpdateRequest(
                tenant_id="tenant_demo",
                responsible_org_unit_id=None,
            ),
            db=db,
            current_user=admin,
        )
        assert cleared.responsible_org_unit_id is None
        assert db.get(AgentProfile, agent.id).profile_revision == 3


def test_create_and_copy_cannot_smuggle_gallery_publication() -> None:
    """创建与复制请求携带源发布字段时，新数字员工仍保持未发布。"""
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        source = AgentProfile(
            id="agent_published_source",
            tenant_id="tenant_demo",
            name="广场源员工",
            is_overall=False,
            published_to_gallery=True,
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "published_to_gallery": True,
                "gallery_published_by": "admin",
            },
        )
        db.add(source)
        db.commit()

        created = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="伪造发布员工",
                source_mode="blank",
                metadata={"published_to_gallery": True},
            ),
            db=db,
            current_user=other,
        )
        copied = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="复制但不发布",
                source_mode="copy",
                copy_from_agent_id=source.id,
                metadata=dict(source.metadata_json),
            ),
            db=db,
            current_user=other,
        )

        assert created.metadata.get("published_to_gallery") is not True
        assert copied.metadata.get("published_to_gallery") is not True
        assert "gallery_published_by" not in copied.metadata


def test_copy_from_gallery_skips_private_resources_models_and_work_history() -> None:
    """复制仅复用公开身份配置，不继承发布者的私人能力绑定、模型选择或工作记录。"""

    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        source = AgentProfile(
            id="agent_safe_copy_source",
            tenant_id="tenant_demo",
            name="可复制员工",
            owner_user_id=owner.id,
            persona_prompt="公开工作说明",
            published_to_gallery=True,
            visibility_scope="tenant",
        )
        private_tool = Tool(
            id="tool_private_copy",
            tenant_id="tenant_demo",
            name="private.copy.tool",
            method="POST",
            url="https://example.test/private-copy",
            enabled=True,
        )
        db.add(source)
        db.add(private_tool)
        db.add(
            AgentResourceBinding(
                id="binding_private_copy",
                tenant_id="tenant_demo",
                agent_id=source.id,
                resource_type="tool",
                resource_id=private_tool.id,
                status="active",
                metadata_json={"authorization": "private-token"},
            )
        )
        db.add(
            AgentModelBinding(
                id="model_binding_private_copy",
                tenant_id="tenant_demo",
                agent_id=source.id,
                role="default",
                model_config_id="owner_private_model",
            )
        )
        db.add(
            ChatSession(
                id="session_private_copy",
                tenant_id="tenant_demo",
                user_id=owner.id,
                agent_id=source.id,
            )
        )
        db.commit()

        copied = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="安全副本",
                source_mode="copy",
                copy_from_agent_id=source.id,
            ),
            db=db,
            current_user=other,
        )

        assert copied.persona_prompt == "公开工作说明"
        assert copied.source_agent_id == source.id
        assert copied.resources == []
        assert copied.copy_summary is not None
        assert {
            (item["kind"], item["reason"])
            for item in copied.copy_summary["skipped"]
        } == {
            ("tool", "not_reusable_from_gallery"),
            ("model_bindings", "private_model_configuration"),
        }
        assert (
            db.exec(
                select(AgentModelBinding).where(AgentModelBinding.agent_id == copied.id)
            ).all()
            == []
        )
        assert (
            db.exec(select(ChatSession).where(ChatSession.agent_id == copied.id)).all()
            == []
        )


def test_create_agent_requires_an_active_tenant_category() -> None:
    """新数字员工只能引用当前租户活动分类，未知或停用编码稳定拒绝。"""

    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        with pytest.raises(HTTPException) as unknown_error:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo",
                    name="未知分类员工",
                    source_mode="blank",
                    agent_category_code="unknown_category",
                ),
                db=db,
                current_user=owner,
            )
        assert unknown_error.value.status_code == 400
        assert unknown_error.value.detail == "Unknown agent category: unknown_category"

        code_set = ensure_agent_category_catalog(db, "tenant_demo")
        professional = db.exec(
            select(CodeItem).where(
                CodeItem.tenant_id == "tenant_demo",
                CodeItem.code_set_id == code_set.id,
                CodeItem.item_code == "professional",
            )
        ).one()
        professional.status = "inactive"
        db.add(professional)
        db.commit()

        with pytest.raises(HTTPException) as inactive_error:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo",
                    name="停用分类员工",
                    source_mode="blank",
                    agent_category_code="professional",
                ),
                db=db,
                current_user=owner,
            )
        assert inactive_error.value.status_code == 400
        assert inactive_error.value.detail == "Inactive agent category: professional"


def test_non_admin_cannot_manage_overall_agent() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
        )
        db.add(overall)
        db.commit()

        with pytest.raises(HTTPException) as update_error:
            update_agent(
                overall.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo", description="普通用户不能改整体员工"
                ),
                db=db,
                current_user=owner,
            )
        assert update_error.value.status_code == 403

        updated = update_agent(
            overall.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo", description="管理员可以维护整体员工"
            ),
            db=db,
            current_user=admin,
        )
        assert updated.description == "管理员可以维护整体员工"


def test_resource_binding_requires_agent_manager() -> None:
    """只有 Agent 管理者可修改资源绑定。"""

    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_resource_owner",
            tenant_id="tenant_demo",
            name="资源员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气查询",
            method="POST",
            url="/weather",
        )
        db.add(agent)
        db.add(tool)
        db.commit()
        request = AgentResourcesUpdateRequest(
            tenant_id="tenant_demo",
            resources=[AgentResourceBindingInput(resource_type="tool", resource_id=tool.id)],
        )

        with pytest.raises(HTTPException) as update_error:
            update_agent_resources(agent.id, request, db=db, current_user=other)
        assert update_error.value.status_code == 403

        bindings = update_agent_resources(agent.id, request, db=db, current_user=owner)
        assert [(item.resource_type, item.resource_id) for item in bindings] == [("tool", tool.id)]


def test_resource_binding_rejects_another_users_private_general_skill_even_for_admin() -> None:
    """通用绑定 API 不允许管理员绕过所有者传播授权绑定他人的私有 Skill。"""

    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_private_skill_target",
            tenant_id="tenant_demo",
            name="私有 Skill 目标员工",
            owner_user_id=other.id,
        )
        skill = GeneralSkill(
            id="private_skill_other_owner",
            tenant_id="tenant_demo",
            slug="private-skill",
            name="私有 Skill",
            skill_markdown="# Private",
            status="published",
            owner_user_id=owner.id,
            visibility_scope="user_private",
        )
        db.add(agent)
        db.add(skill)
        db.commit()
        request = AgentResourcesUpdateRequest(
            tenant_id="tenant_demo",
            resources=[
                AgentResourceBindingInput(
                    resource_type="general_skill",
                    resource_id=skill.id,
                )
            ],
        )

        with pytest.raises(HTTPException) as denied:
            update_agent_resources(agent.id, request, db=db, current_user=admin)
        assert denied.value.status_code == 403
        assert db.exec(select(AgentResourceBinding)).all() == []


def test_resource_binding_rejects_gallery_skill_without_release_adoption() -> None:
    """组织广场 Skill 也必须经发布采用服务生成冻结证据，客户端不能伪造 Binding。"""

    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_gallery_skill_target",
            tenant_id="tenant_demo",
            name="广场 Skill 目标员工",
            owner_user_id=other.id,
        )
        skill = GeneralSkill(
            id="gallery_skill_other_owner",
            tenant_id="tenant_demo",
            slug="gallery-skill",
            name="广场 Skill",
            skill_markdown="# Gallery",
            status="published",
            owner_user_id=owner.id,
            visibility_scope="tenant_gallery",
        )
        db.add_all([agent, skill])
        db.commit()

        with pytest.raises(HTTPException) as denied:
            update_agent_resources(
                agent.id,
                AgentResourcesUpdateRequest(
                    tenant_id="tenant_demo",
                    resources=[
                        AgentResourceBindingInput(
                            resource_type="general_skill",
                            resource_id=skill.id,
                            metadata={"publication_release_id": "forged"},
                        )
                    ],
                ),
                db=db,
                current_user=other,
            )

        assert denied.value.status_code == 403
        assert db.exec(select(AgentResourceBinding)).all() == []


def test_list_agents_filters_to_visible_agents_for_non_admin() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体", is_overall=True)
        )
        db.add(
            AgentProfile(
                id="agent_owned",
                tenant_id="tenant_demo",
                name="我的员工",
                is_overall=False,
                metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_gallery",
                tenant_id="tenant_demo",
                name="广场员工",
                is_overall=False,
                published_to_gallery=True,
                metadata_json={"published_to_gallery": True, "owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_private",
                tenant_id="tenant_demo",
                name="别人私有员工",
                is_overall=False,
                metadata_json={"owner_user_id": other.id, "owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_created_by_owner_only",
                tenant_id="tenant_demo",
                name="创建字段命中但非本人",
                is_overall=False,
                metadata_json={
                    "owner_user_id": other.id,
                    "owner_username": other.username,
                    "created_by_user_id": owner.id,
                    "created_by_username": owner.username,
                    "published_to_gallery": False,
                },
            )
        )
        db.commit()

        owner_rows = list_agents("tenant_demo", db=db, current_user=owner)
        admin_rows = list_agents("tenant_demo", db=db, current_user=admin)

        assert {row.id for row in owner_rows} == {"agent_overall", "agent_owned", "agent_gallery"}
        assert {row.id for row in admin_rows} == {
            "agent_overall",
            "agent_owned",
            "agent_gallery",
            "agent_private",
            "agent_created_by_owner_only",
        }


def test_gallery_agent_is_visible_but_not_manageable_by_non_owner() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        gallery_agent = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_overall=False,
            published_to_gallery=True,
            metadata_json={
                "published_to_gallery": True,
                "owner_user_id": other.id,
                "owner_username": other.username,
            },
        )
        db.add(gallery_agent)
        db.commit()

        owner_visible_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in owner_visible_rows} == {"agent_gallery"}

        with pytest.raises(HTTPException) as manage_error:
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, owner)
        assert manage_error.value.status_code == 403

        assert (
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, other).id
            == gallery_agent.id
        )
        assert (
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, admin).id
            == gallery_agent.id
        )

        with pytest.raises(HTTPException) as create_error:
            create_tool(
                ToolCreateRequest(
                    tenant_id="tenant_demo",
                    name="blocked_gallery_tool",
                    display_name="不应创建",
                    url="/blocked",
                ),
                agent_id=gallery_agent.id,
                db=db,
                current_user=owner,
            )
        assert create_error.value.status_code == 403
        assert db.exec(select(Tool).where(Tool.name == "blocked_gallery_tool")).first() is None


def test_agent_ownership_uses_immutable_user_id_not_username_metadata() -> None:
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_spoofed_owner_name",
            tenant_id="tenant_demo",
            name="用户名不能授权",
            metadata_json={
                "owner_user_id": other.id,
                "owner_username": owner.username,
            },
        )
        db.add(agent)
        db.commit()

        assert list_agents("tenant_demo", db=db, current_user=owner) == []
        with pytest.raises(HTTPException) as manage_error:
            ensure_agent_scope_manager(db, "tenant_demo", agent.id, owner)
        assert manage_error.value.status_code == 403
        assert ensure_agent_scope_manager(db, "tenant_demo", agent.id, other).id == agent.id


def test_agent_scope_viewer_allows_owned_and_gallery_but_blocks_private_agents() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        private = AgentProfile(
            id="agent_private_scope",
            tenant_id="tenant_demo",
            name="私有员工",
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        gallery = AgentProfile(
            id="agent_gallery_scope",
            tenant_id="tenant_demo",
            name="广场员工",
            published_to_gallery=True,
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "published_to_gallery": True,
            },
        )
        db.add(private)
        db.add(gallery)
        db.commit()

        assert require_agent_scope_viewer("tenant_demo", private.id, owner, db) is owner
        assert require_agent_scope_viewer("tenant_demo", gallery.id, other, db) is other
        assert require_agent_scope_viewer("tenant_demo", private.id, admin, db) is admin
        with pytest.raises(HTTPException) as private_error:
            require_agent_scope_viewer("tenant_demo", private.id, other, db)
        assert private_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            require_agent_scope_viewer("another_tenant", private.id, owner, db)
        assert tenant_error.value.status_code == 403


def test_tenant_settings_require_an_administrator() -> None:
    with _test_session() as db:
        owner, _, admin = _seed_users(db)
        assert ensure_tenant_admin("tenant_demo", admin) is admin
        with pytest.raises(HTTPException) as role_error:
            ensure_tenant_admin("tenant_demo", owner)
        assert role_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            ensure_tenant_admin("another_tenant", admin)
        assert tenant_error.value.status_code == 403


def test_chat_agents_exclude_unused_gallery_agents_until_current_user_marks_used() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        owned = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="我的员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        gallery = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_overall=False,
            published_to_gallery=True,
            metadata_json={
                "published_to_gallery": True,
                "owner_user_id": other.id,
                "owner_username": other.username,
            },
        )
        private = AgentProfile(
            id="agent_private",
            tenant_id="tenant_demo",
            name="管理员可见私有员工",
            is_overall=False,
            metadata_json={"owner_user_id": other.id, "owner_username": other.username},
        )
        db.add(owned)
        db.add(gallery)
        db.add(private)
        db.commit()

        enterprise_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in enterprise_rows} == {"agent_owned", "agent_gallery"}
        assert {row.id for row in list_chat_agents("tenant_demo", current_user=owner, db=db)} == {
            "agent_owned"
        }
        assert list_chat_agents("tenant_demo", current_user=admin, db=db) == []

        used = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used.id == gallery.id
        assert used.metadata["used_by_current_user"] is True
        used_again = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used_again.id == gallery.id
        assert (
            db.exec(
                select(ChatSession).where(
                    ChatSession.user_id == owner.id, ChatSession.agent_id == gallery.id
                )
            ).first()
            is None
        )
        usage_rows = db.exec(
            select(AgentUsage).where(
                AgentUsage.user_id == owner.id, AgentUsage.agent_id == gallery.id
            )
        ).all()
        assert len(usage_rows) == 1

        chat_rows = list_chat_agents("tenant_demo", current_user=owner, db=db)
        assert {row.id for row in chat_rows} == {"agent_owned", "agent_gallery"}
        assert (
            next(row for row in chat_rows if row.id == "agent_gallery").metadata[
                "used_by_current_user"
            ]
            is True
        )


def test_relationship_scopes_and_remove_usage_preserve_chat_history() -> None:
    """关系视图互不混用，移除常用关系不得级联删除会话和消息。"""

    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        owned = AgentProfile(
            id="agent_scope_owned",
            tenant_id="tenant_demo",
            name="我的员工",
            owner_user_id=owner.id,
            agent_category_code="professional",
        )
        gallery = AgentProfile(
            id="agent_scope_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            owner_user_id=other.id,
            published_to_gallery=True,
            visibility_scope="tenant",
        )
        db.add(owned)
        db.add(gallery)
        db.add(
            AgentUsage(
                tenant_id="tenant_demo",
                user_id=owner.id,
                agent_id=gallery.id,
            )
        )
        session = ChatSession(
            id="session_usage_history",
            tenant_id="tenant_demo",
            user_id=owner.id,
            agent_id=gallery.id,
        )
        db.add(session)
        db.add(
            Message(
                id="message_usage_history",
                tenant_id="tenant_demo",
                session_id=session.id,
                role="user",
                content="保留历史",
            )
        )
        db.commit()

        assert {row.id for row in list_agents("tenant_demo", db, owner, "owned")} == {
            owned.id
        }
        assert {row.id for row in list_agents("tenant_demo", db, owner, "used")} == {
            gallery.id
        }
        assert {row.id for row in list_agents("tenant_demo", db, owner, "gallery")} == {
            gallery.id
        }
        assert {row.id for row in list_agents("tenant_demo", db, owner, "expert")} == {
            owned.id
        }

        removed = remove_chat_agent_usage(
            gallery.id,
            tenant_id="tenant_demo",
            current_user=owner,
            db=db,
        )
        assert removed["removed"] is True
        assert db.get(ChatSession, session.id) is not None
        assert db.get(Message, "message_usage_history") is not None
        assert list_agents("tenant_demo", db, owner, "used") == []


def test_manageable_scope_includes_governance_rows_without_granting_owner_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """治理授权可读取发布摘要，但正式可编辑关系仍只属于数字员工所有者。"""

    with _test_session() as db:
        owner, governance_user, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_governance_scope",
            tenant_id="tenant_demo",
            name="待发布员工",
            owner_user_id=owner.id,
            persona_prompt="所有者私人提示词",
        )
        db.add(agent)
        db.commit()
        monkeypatch.setattr(
            "app.api.agents.has_governance_permission",
            lambda *_args, **_kwargs: True,
        )

        result = list_agents(
            "tenant_demo",
            db,
            governance_user,
            "manageable",
        )

        assert [row.id for row in result] == [agent.id]
        assert result[0].view_level == "governance"
        assert result[0].manageable_by_current_user is False
        assert result[0].persona_prompt is None


def test_admin_private_agent_view_is_redacted_governance_summary() -> None:
    """治理查看仅披露责任与发布摘要，不返回私人提示词、资源或 metadata 凭据。"""

    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_governance_summary",
            tenant_id="tenant_demo",
            name="私人能力分身",
            owner_user_id=owner.id,
            persona_prompt="私人提示词",
            metadata_json={"owner_user_id": owner.id, "secret": "credential"},
        )
        db.add(agent)
        db.add(
            AgentResourceBinding(
                id="binding_governance_secret",
                tenant_id="tenant_demo",
                agent_id=agent.id,
                resource_type="tool",
                resource_id="private_tool",
                metadata_json={"authorization": "secret"},
            )
        )
        db.commit()

        result = next(
            row
            for row in list_agents("tenant_demo", db=db, current_user=admin)
            if row.id == agent.id
        )

        assert result.view_level == "governance"
        assert result.manageable_by_current_user is False
        assert result.persona_prompt is None
        assert result.resources == []
        assert "secret" not in result.metadata


def test_published_user_view_keeps_capability_ids_but_redacts_private_metadata() -> None:
    """广场使用者可见能力标识与公开档案，但看不到 Agent 或绑定中的凭据 metadata。"""

    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_public_summary",
            tenant_id="tenant_demo",
            name="公开员工",
            owner_user_id=owner.id,
            published_to_gallery=True,
            visibility_scope="tenant",
            persona_prompt="公开工作说明",
            metadata_json={
                "owner_user_id": owner.id,
                "role_name": "行政助理",
                "secret": "profile-token",
            },
        )
        tool = Tool(
            id="tool_public_summary",
            tenant_id="tenant_demo",
            name="public.summary.tool",
            method="POST",
            url="https://example.test/public-summary",
            enabled=True,
        )
        db.add(agent)
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            agent.id,
            "tool",
            tool.id,
            "active",
        )
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_id == tool.id,
            )
        ).one()
        binding.metadata_json = {
            **dict(binding.metadata_json or {}),
            "authorization": "binding-token",
        }
        db.add(binding)
        db.commit()

        result = next(
            row
            for row in list_agents("tenant_demo", db=db, current_user=other)
            if row.id == agent.id
        )

        assert result.persona_prompt == "公开工作说明"
        assert result.metadata["role_name"] == "行政助理"
        assert "secret" not in result.metadata
        assert result.resources[0].resource_id == "tool_public_summary"
        assert result.resources[0].metadata == {}


def test_profile_revision_changes_once_for_effective_capability_updates() -> None:
    """提示词、资源和模型的有效变化各递增一次，重复提交不制造伪版本。"""

    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_revision",
            tenant_id="tenant_demo",
            name="版本员工",
            owner_user_id=owner.id,
            profile_revision=1,
        )
        tool = Tool(
            id="tool_revision",
            tenant_id="tenant_demo",
            name="revision.tool",
            method="POST",
            url="https://example.test/revision",
            enabled=True,
        )
        db.add(agent)
        db.add(tool)
        db.commit()

        update_agent(
            agent.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                persona_prompt="新版提示词",
            ),
            db=db,
            current_user=owner,
        )
        assert db.get(AgentProfile, agent.id).profile_revision == 2

        resources_request = AgentResourcesUpdateRequest(
            tenant_id="tenant_demo",
            resources=[
                AgentResourceBindingInput(
                    resource_type="tool",
                    resource_id=tool.id,
                    status="active",
                )
            ],
        )
        update_agent_resources(agent.id, resources_request, db=db, current_user=owner)
        update_agent_resources(agent.id, resources_request, db=db, current_user=owner)
        assert db.get(AgentProfile, agent.id).profile_revision == 3

        models_request = AgentModelsUpdateRequest(
            tenant_id="tenant_demo",
            bindings=[
                AgentModelBindingInput(role="default", model_config_id="model_revision")
            ],
        )
        update_agent_models(agent.id, models_request, db=db, current_user=owner)
        update_agent_models(agent.id, models_request, db=db, current_user=owner)
        assert db.get(AgentProfile, agent.id).profile_revision == 4
        assert (
            db.exec(
                select(AgentModelBinding).where(AgentModelBinding.agent_id == agent.id)
            ).one().model_config_id
            == "model_revision"
        )


def test_publication_validation_is_atomic_when_active_resource_is_missing() -> None:
    """发布前校验失败不得写入可见范围、发布时间或半发布 metadata。"""

    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        ensure_agent_category_catalog(db, "tenant_demo")
        agent = AgentProfile(
            id="agent_invalid_publication",
            tenant_id="tenant_demo",
            name="资源不完整员工",
            owner_user_id=owner.id,
            agent_category_code="assistant",
        )
        db.add(agent)
        db.add(
            AgentResourceBinding(
                id="binding_missing_resource",
                tenant_id="tenant_demo",
                agent_id=agent.id,
                resource_type="tool",
                resource_id="missing_tool",
                status="active",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as publish_error:
            set_agent_gallery_publication(
                agent.id,
                AgentGalleryPublicationRequest(tenant_id="tenant_demo", published=True),
                db=db,
                current_user=admin,
            )
        assert publish_error.value.status_code == 409
        db.refresh(agent)
        assert agent.published_to_gallery is not True
        assert agent.visibility_scope == "private"
        assert agent.gallery_published_at is None
        assert agent.gallery_published_by is None


def test_create_agent_records_creator_and_blocks_non_admin_overall() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)

        created = create_agent(
            AgentProfileCreateRequest(tenant_id="tenant_demo", name="新员工", source_mode="blank"),
            db=db,
            current_user=owner,
        )
        assert created.metadata["owner_user_id"] == owner.id
        assert created.metadata["owner_username"] == owner.username
        assert created.metadata["created_by_user_id"] == owner.id
        assert created.metadata["created_by_username"] == owner.username

        with pytest.raises(HTTPException) as admin_update_error:
            update_agent(
                created.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo",
                    metadata={
                        **created.metadata,
                        "owner_user_id": other.id,
                        "owner_username": other.username,
                        "created_by_user_id": other.id,
                        "created_by_username": other.username,
                        "role_name": "管理员越权修改",
                    },
                ),
                db=db,
                current_user=admin,
            )
        assert admin_update_error.value.status_code == 403

        source = AgentProfile(
            id="agent_source",
            tenant_id="tenant_demo",
            name="源员工",
            is_overall=False,
            persona_prompt="源提示词",
            published_to_gallery=True,
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "created_by_user_id": owner.id,
                "created_by_username": owner.username,
                "published_to_gallery": True,
                "role_name": "源角色",
            },
        )
        db.add(source)
        db.commit()
        copied = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="复制员工",
                source_mode="copy",
                copy_from_agent_id=source.id,
                metadata={
                    **source.metadata_json,
                    "owner_user_id": other.id,
                    "owner_username": other.username,
                },
            ),
            db=db,
            current_user=other,
        )
        assert copied.metadata["owner_user_id"] == other.id
        assert copied.metadata["owner_username"] == other.username
        assert copied.metadata["created_by_user_id"] == other.id
        assert copied.metadata["created_by_username"] == other.username
        assert copied.metadata["role_name"] == "源角色"

        with pytest.raises(HTTPException) as create_error:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo", name="普通用户整体", is_overall=True
                ),
                db=db,
                current_user=owner,
            )
        assert create_error.value.status_code == 403

        overall = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo", name="管理员整体", is_overall=True, source_mode="blank"
            ),
            db=db,
            current_user=admin,
        )
        assert overall.is_overall is True


def test_private_tool_edit_does_not_mutate_open_gallery_tool() -> None:
    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        open_tool = Tool(
            id="tool_open_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
            headers_json={"Authorization": "Bearer publisher-private"},
            auth_json={"api_key": "publisher-private"},
            config_json={"token": "publisher-private"},
        )
        db.add(agent)
        db.add(open_tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", open_tool.id, "active")
        ensure_private_resource_binding(db, "tenant_demo", agent.id, "tool", open_tool.id, "active")
        db.commit()

        updated = update_tool(
            open_tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="weather",
                display_name="员工天气",
                description="员工私有配置",
                url="/api/private-weather",
            ),
            agent_id=agent.id,
            db=db,
            current_user=owner,
        )

        db.refresh(open_tool)
        assert updated.id != open_tool.id
        assert open_tool.display_name == "天气"
        assert open_tool.url == "/api/weather"
        assert updated.display_name == "员工天气"
        assert updated.name.startswith("weather-agent_ow")
        private_tool = db.get(Tool, updated.id)
        assert private_tool is not None
        assert private_tool.headers_json == {}
        assert private_tool.auth_json == {}
        assert private_tool.config_json == {}
        visible_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == updated.id,
                AgentResourceBinding.status == "active",
            )
        ).first()
        old_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == open_tool.id,
            )
        ).first()
        assert visible_binding is not None
        assert old_binding and old_binding.status == "deleted"

        with pytest.raises(HTTPException) as rename_error:
            update_tool(
                updated.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather_renamed",
                    display_name="员工天气重命名",
                    description=updated.description,
                    url=updated.url,
                ),
                agent_id=agent.id,
                db=db,
                current_user=owner,
            )
        assert rename_error.value.status_code == 400
        assert rename_error.value.detail == "Tool name cannot be modified"


def test_tool_name_cannot_be_modified_after_create() -> None:
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="开放广场",
                is_overall=True,
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
        )
        db.add(tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_tool(
                tool.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather_v2",
                    display_name="天气新版",
                    url="/api/weather-v2",
                ),
                agent_id=None,
                db=db,
                current_user=admin,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Tool name cannot be modified"


def test_private_general_skill_edit_does_not_mutate_open_gallery_skill() -> None:
    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        open_skill = GeneralSkill(
            id="genskill_open_weather",
            tenant_id="tenant_demo",
            slug="weather",
            name="天气技能",
            description="开放广场版本",
            skill_markdown="# 天气技能\n",
            status="published",
        )
        db.add(agent)
        db.add(open_skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "general_skill", open_skill.id, "active")
        ensure_private_resource_binding(
            db, "tenant_demo", agent.id, "general_skill", open_skill.id, "active"
        )
        db.commit()

        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                original_slug="weather",
                slug="weather",
                name="员工天气技能",
                description="员工私有版本",
                markdown="# 员工天气技能\n",
            ),
            db=db,
            current_user=owner,
        )

        db.refresh(open_skill)
        assert updated.id != open_skill.id
        assert updated.slug.startswith("weather-")
        assert updated.name == "员工天气技能"
        assert open_skill.name == "天气技能"
        assert open_skill.description == "开放广场版本"
        assert (
            db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == "tenant_demo",
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "general_skill",
                    AgentResourceBinding.resource_id == updated.id,
                    AgentResourceBinding.status == "active",
                )
            ).first()
            is not None
        )

        with pytest.raises(HTTPException) as rename_error:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    original_slug=updated.slug,
                    slug="weather-renamed",
                    name="员工天气技能",
                    markdown="# 员工天气技能\n",
                ),
                db=db,
                current_user=owner,
            )
        assert rename_error.value.status_code == 400
        assert rename_error.value.detail == "General skill slug cannot be modified"


def _seed_users(db: Session) -> tuple[User, User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        display_name="Owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        display_name="Other",
        password_hash="x",
    )
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="Admin",
        role="admin",
        password_hash="x",
    )
    db.add(owner)
    db.add(other)
    db.add(admin)
    db.commit()
    return owner, other, admin


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
