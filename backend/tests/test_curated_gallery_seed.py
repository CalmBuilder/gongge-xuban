"""
@Time       : 2026/07/29 15:30
@Author     : zhanglp8181
@File       : test_curated_gallery_seed.py
@CallChain  : 精选资源初始化 → 正式所有者同步 → Agent 知识可见性
@Description: 验证精选数字员工及知识库初始化后的归属、绑定和正文访问契约。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.api.agents import list_agents
from app.api.knowledge_bases import list_knowledge_bases
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    BusinessRole,
    ChatSession,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Skill,
    SkillVersion,
    SopInstance,
    SopOperation,
    Tenant,
    Tool,
    User,
)
from app.db.seed import seed_demo_data
from app.db import curated_gallery_seed
from app.config import get_settings
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.versioning import skill_content_checksum
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


EXPECTED_KNOWLEDGE_COUNTS = {
    "IT": 2,
    "人事": 3,
    "法务": 4,
    "行政": 2,
    "财务": 3,
}

LEGACY_MOCK_TOOL_NAMES = {
    "admin.room_book",
    "admin.supply_request",
    "contract.archive_query",
    "expense.submit",
    "expense.quota_query",
    "hr.balance_query",
    "hr.cert_issue",
    "hr.leave_apply",
    "invoice.verify",
    "it.grant_permission",
    "it.ticket_create",
}


class _FlushOnlySession:
    def flush(self) -> None:
        pass


def _seeded_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    seed_demo_data(session)
    session.commit()
    return session


def test_curated_gallery_seed_reads_fixture_as_utf8(monkeypatch) -> None:
    class FakeFixturePath:
        def exists(self) -> bool:
            return True

        def read_text(self, *, encoding=None) -> str:
            assert encoding == "utf-8"
            return "{}"

    monkeypatch.setattr(curated_gallery_seed, "FIXTURE_PATH", FakeFixturePath())
    monkeypatch.setattr(curated_gallery_seed, "_seed_agents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(curated_gallery_seed, "_seed_skills", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        curated_gallery_seed, "_seed_general_skills", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(curated_gallery_seed, "_seed_tools", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(curated_gallery_seed, "_seed_knowledge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        curated_gallery_seed, "_seed_agent_resource_bindings", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        curated_gallery_seed, "_seed_skill_branches", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        curated_gallery_seed, "_seed_knowledge_branches", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        curated_gallery_seed, "_publish_gallery_resources", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        curated_gallery_seed, "_sync_seed_agents_to_current_admin", lambda *_args, **_kwargs: None
    )

    curated_gallery_seed.seed_curated_gallery(_FlushOnlySession())


def test_curated_gallery_seed_exposes_selected_agents_with_knowledge_bases() -> None:
    """精选知识库归当前管理员所有并按数字员工绑定范围展示。"""

    with _seeded_session() as db:
        admin = db.exec(
            select(User).where(User.tenant_id == "tenant_demo", User.username == "admin")
        ).one()
        agents = {
            agent.name: agent
            for agent in list_agents("tenant_demo", db=db, current_user=admin)
            if agent.name in EXPECTED_KNOWLEDGE_COUNTS
        }

        assert set(agents) == set(EXPECTED_KNOWLEDGE_COUNTS)
        assert not db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "默认智能体",
                AgentProfile.status == "active",
            )
        ).first()
        for name, expected_count in EXPECTED_KNOWLEDGE_COUNTS.items():
            agent = agents[name]
            bound_count = sum(
                1
                for resource in agent.resources
                if resource.resource_type == "knowledge_base" and resource.status == "active"
            )
            scoped_knowledge = list_knowledge_bases(
                "tenant_demo",
                agent.id,
                governance_view=False,
                db=db,
                current_user=admin,
            )

            assert bound_count == expected_count
            assert len(scoped_knowledge) == expected_count
            assert all(item.document_count > 0 for item in scoped_knowledge)
            assert all(item.chunk_count > 0 for item in scoped_knowledge)


def test_curated_gallery_seed_uses_existing_admin_id_for_seeded_agents() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            User(
                id="user_existing_admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="Existing Admin",
                role="admin",
                password_hash="test",
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        rows = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name.in_(EXPECTED_KNOWLEDGE_COUNTS.keys()),
            )
        ).all()

        assert len(rows) == len(EXPECTED_KNOWLEDGE_COUNTS)
        assert {row.metadata_json.get("owner_user_id") for row in rows} == {"user_existing_admin"}
        assert {row.owner_user_id for row in rows} == {"user_existing_admin"}
        assert all(row.published_to_gallery is True for row in rows)
        assert all(row.visibility_scope == "tenant" for row in rows)
        assert all(row.agent_category_code == "assistant" for row in rows)

        overall = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.is_overall.is_(True),
            )
        ).one()
        assert overall.owner_user_id is None
        assert overall.published_to_gallery is False
        assert overall.gallery_published_at is None
        assert overall.gallery_published_by is None
        assert overall.visibility_scope == "private"
        assert overall.metadata_json.get("published_to_gallery") is not True
        assert "gallery_published_by" not in overall.metadata_json


def test_curated_gallery_seed_does_not_overwrite_non_seed_employee_name_conflict() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            User(
                id="admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="Administrator",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            AgentProfile(
                id="agent_custom_it",
                tenant_id="tenant_demo",
                name="IT",
                description="用户原有的 IT 员工",
                status="active",
                metadata_json={
                    "owner_user_id": "user_custom",
                    "owner_username": "custom",
                    "created_by": "custom",
                },
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        row = db.get(AgentProfile, "agent_custom_it")

        assert row is not None
        assert row.description == "用户原有的 IT 员工"
        assert row.metadata_json.get("owner_user_id") == "user_custom"
        assert row.metadata_json.get("seed_source") is None


def test_curated_gallery_seed_archives_legacy_default_agent() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            AgentProfile(
                id="agent_tenant_demo_default",
                tenant_id="tenant_demo",
                name="默认智能体",
                description="默认对话可见域",
                status="active",
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        row = db.get(AgentProfile, "agent_tenant_demo_default")
        admin = db.exec(
            select(User).where(User.tenant_id == "tenant_demo", User.username == "admin")
        ).one()
        listed_ids = {agent.id for agent in list_agents("tenant_demo", db=db, current_user=admin)}

        assert row is not None
        assert row.status == "archived"
        assert row.metadata_json.get("hidden_from_product") is True
        assert row.metadata_json.get("is_default_employee") is True
        assert "agent_tenant_demo_default" not in listed_ids


def test_curated_gallery_seed_migrates_only_known_legacy_mock_tools() -> None:
    with _seeded_session() as db:
        rows = db.exec(select(Tool).where(Tool.name.in_(LEGACY_MOCK_TOOL_NAMES))).all()

        assert {row.name for row in rows} == LEGACY_MOCK_TOOL_NAMES
        expected_prefix = f"{get_settings().normalized_tool_base_url}/api/mock/"
        assert all(row.url.startswith(expected_prefix) for row in rows)
        assert all(
            row.headers_json.get("X-API-Key") == "${secret.PUBLIC_MOCK_API_KEY}" for row in rows
        )
        assert all("58.57.119.30:52008" not in row.url for row in rows)


def test_curated_gallery_seed_does_not_downgrade_future_skill_head() -> None:
    """验证启动 seed 不覆盖高于内置身份版本的未来 Runtime 技能头。"""

    with _seeded_session() as db:
        skill = db.exec(select(Skill).where(Skill.skill_id == "skill_expense_quota_query")).one()
        upgraded_content = dict(skill.content_json)
        upgraded_content.update({"version": "3.0.0", "execution_mode": "deterministic"})
        skill.version = "3.0.0"
        skill.content_json = upgraded_content
        db.add(skill)
        db.commit()

        seed_demo_data(db)
        db.commit()
        db.refresh(skill)

        assert skill.version == "3.0.0"
        assert skill.content_json["execution_mode"] == "deterministic"


def test_curated_gallery_seed_does_not_downgrade_newer_synced_agent_branch() -> None:
    """验证连续启动不会用精选 fixture 的 1.0.0 覆盖已同步到新发布头的 Agent 分支。"""

    with _seeded_session() as db:
        finance_agent = db.exec(select(AgentProfile).where(AgentProfile.name == "财务")).one()
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.agent_id == finance_agent.id,
                AgentSkillBranch.skill_id == "skill_expense_quota_query",
            )
        ).one()
        upgraded_content = dict(branch.content_json)
        upgraded_content["version"] = "2.3.1"
        branch.base_version = "2.3.1"
        branch.head_version = "2.3.1"
        branch.content_json = upgraded_content
        branch.sync_state = "synced"
        db.add(branch)
        db.commit()

        seed_demo_data(db)
        db.commit()
        db.refresh(branch)

        assert branch.base_version == "2.3.1"
        assert branch.head_version == "2.3.1"
        assert branch.content_json == upgraded_content
        assert branch.sync_state == "synced"


def test_repeated_demo_seed_keeps_all_skill_branches_exactly_unchanged() -> None:
    """验证第二次完整启动 seed 不刷新任何 Agent SOP 分支或不可变分支版本。"""

    with _seeded_session() as db:
        before_branches = [
            (
                row.id,
                row.base_version,
                row.head_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(select(AgentSkillBranch).order_by(AgentSkillBranch.id)).all()
        ]
        before_versions = [
            (
                row.id,
                row.version,
                row.base_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentSkillBranchVersion).order_by(AgentSkillBranchVersion.id)
            ).all()
        ]

        seed_demo_data(db)
        db.commit()

        assert [
            (
                row.id,
                row.base_version,
                row.head_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(select(AgentSkillBranch).order_by(AgentSkillBranch.id)).all()
        ] == before_branches
        assert [
            (
                row.id,
                row.version,
                row.base_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentSkillBranchVersion).order_by(AgentSkillBranchVersion.id)
            ).all()
        ] == before_versions


def test_curated_gallery_seed_publishes_expense_identity_version() -> None:
    """验证首个闭环 SOP 发布新版本而不修改历史发布快照。"""

    with _seeded_session() as db:
        skill = db.exec(select(Skill).where(Skill.skill_id == "skill_expense_quota_query")).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "skill_expense_quota_query",
                SkillVersion.version == "2.3.0",
            )
        ).one()
        collect_node = next(
            node for node in version.content_json["nodes"] if node["node_id"] == "node_collect_info"
        )

        assert skill.version == "2.3.0"
        assert version.status == "published"
        assert version.meta_model_version == 2
        assert collect_node["metadata"]["input_bindings"]["employee_id"] == {
            "source": "authenticated_employee",
                "attribute": "employee_id",
                "allow_override_roles": ["finance_expense_specialist"],
                "required_override_permission": "expense.quota.read:any",
        }

        role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == "finance_expense_specialist")
        ).one()
        assert set(role.permissions_json) == {
            "expense.quota.read:any",
            "expense.travel_policy.assess",
            "expense.invoice.verify",
            "expense.submit",
            "expense.travel_review.claim",
            "expense.travel_review.complete",
            "expense.travel_review.request_information",
        }
        assert (
            db.exec(
                select(EmployeeRoleAssignment).where(
                    EmployeeRoleAssignment.business_role_id == role.id
                )
            )
            .one()
            .business_role_id
            == role.id
        )
        assert (
            db.exec(select(AgentRoleBinding).where(AgentRoleBinding.business_role_id == role.id))
            .one()
            .business_role_id
            == role.id
        )
        finance_agent = db.exec(select(AgentProfile).where(AgentProfile.name == "财务")).one()
        finance_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.agent_id == finance_agent.id,
                AgentSkillBranch.skill_id == "skill_expense_quota_query",
            )
        ).one()
        assert finance_branch.head_version == "2.3.0"
        assert finance_branch.content_json["execution_mode"] == "deterministic"


def test_curated_gallery_seed_publishes_leave_balance_deterministic_version() -> None:
    """验证假期余额发布无告警版本，并冻结 HR 角色、身份和假期类型映射。"""

    with _seeded_session() as db:
        skill = db.exec(select(Skill).where(Skill.skill_id == "skill_leave_balance_query")).one()
        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == "skill_leave_balance_query",
                SkillVersion.version == "2.2.0",
            )
        ).one()
        collect_node = next(
            node
            for node in version.content_json["nodes"]
            if node["node_id"] == "node_collect_leave_query"
        )

        assert skill.version == "2.2.0"
        assert version.status == "published"
        assert version.meta_model_version == 3
        assert collect_node["metadata"]["input_bindings"]["employee_id"] == {
            "source": "authenticated_employee",
                "attribute": "employee_id",
                "allow_override_roles": ["hr_leave_specialist"],
                "required_override_permission": "hr.leave_balance.read:any",
        }
        assert collect_node["metadata"]["value_aliases"]["leave_type"]["年假"] == "annual"
        response_instruction = next(
            node["instruction"]
            for node in version.content_json["nodes"]
            if node["node_id"] == "node_response_leave_balance"
        )
        assert "工具没有有效期字段" in response_instruction
        assert "不得承诺或编造有效期" in response_instruction

        role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == "hr_leave_specialist")
        ).one()
        assert role.permissions_json == [
            "hr.leave.apply",
            "hr.leave_balance.read:any",
            "hr.leave_review.claim",
            "hr.leave_review.complete",
            "hr.leave_review.request_information",
            "hr.overtime_review.claim",
            "hr.overtime_review.complete",
            "hr.overtime_review.request_information",
        ]
        human_resources_agent = db.exec(select(AgentProfile).where(AgentProfile.name == "人事")).one()
        human_resources_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.agent_id == human_resources_agent.id,
                AgentSkillBranch.skill_id == "skill_leave_balance_query",
            )
        ).one()
        assert human_resources_branch.head_version == "2.2.0"
        assert human_resources_branch.content_json["execution_mode"] == "deterministic"


def test_leave_balance_runtime_normalizes_type_and_closes_success_and_failure_paths() -> None:
    """验证真实发布定义按可信身份查询，并让中文类型和失败回执确定性收口。"""

    with _seeded_session() as db:
        skill = db.exec(select(Skill).where(Skill.skill_id == "skill_leave_balance_query")).one()
        coordinator = DeterministicSopCoordinator(db)
        successful_session = ChatSession(
            id="session_leave_success",
            tenant_id="tenant_demo",
            user_id="user_demo",
            active_skill_id=skill.skill_id,
            active_step_id="node_collect_leave_query",
            slots_json={"leave_type": "年假"},
        )
        db.add(successful_session)
        db.commit()

        calling = coordinator.prepare_step(
            successful_session,
            skill,
            StepAgentResult(slot_updates={"leave_type": "年假"}),
            user_message="查一下我还剩多少年假",
        )
        assert calling.tool_call == ToolCall(
            name="hr.balance_query",
            arguments={"employee_id": "E002"},
        )
        assert successful_session.slots_json["leave_type"] == "annual"

        successful_plan = coordinator.record_tool_result(
            successful_session,
            calling.tool_call,
            ToolResult(
                tool_name="hr.balance_query",
                success=True,
                data={"leave_balance": {"annual": 5.0}},
            ),
        )
        db.commit()
        successful_instance = db.exec(
            select(SopInstance).where(SopInstance.session_id == successful_session.id)
        ).one()

        assert successful_plan is not None and successful_plan.action == "complete"
        assert successful_session.active_step_id == "node_response_leave_balance"
        assert successful_instance.status == "succeeded"

        failed_session = ChatSession(
            id="session_leave_failure",
            tenant_id="tenant_demo",
            user_id="user_demo",
            active_skill_id=skill.skill_id,
            active_step_id="node_collect_leave_query",
            slots_json={"leave_type": "调休"},
        )
        db.add(failed_session)
        db.commit()
        failed_calling = coordinator.prepare_step(
            failed_session,
            skill,
            StepAgentResult(slot_updates={"leave_type": "调休"}),
            user_message="查一下我的调休余额",
        )
        assert failed_calling.tool_call is not None
        failed_plan = coordinator.record_tool_result(
            failed_session,
            failed_calling.tool_call,
            ToolResult(
                tool_name="hr.balance_query",
                success=False,
                error=ToolError(code="UPSTREAM_TIMEOUT", message="上游超时"),
            ),
        )
        db.commit()
        failed_operation = db.exec(
            select(SopOperation).where(SopOperation.instance_id != successful_instance.id)
        ).one()

        assert failed_plan is not None and failed_plan.action == "complete"
        assert failed_session.active_step_id == "node_response_leave_failure"
        assert failed_operation.status == "failed"
        assert failed_operation.error_json["code"] == "UPSTREAM_TIMEOUT"


def test_demo_seed_publishes_participant_acceptance_slice_idempotently() -> None:
    """验证验收账号、多角色任职和确定性人工任务版本可重复初始化。"""

    with _seeded_session() as db:
        seed_demo_data(db)
        approver = db.exec(select(User).where(User.username == "approver_demo")).one()
        profile = db.exec(
            select(EmployeeProfile).where(EmployeeProfile.user_id == approver.id)
        ).one()
        role = db.exec(
            select(BusinessRole).where(BusinessRole.role_code == "process_demo_approver")
        ).one()
        assignment = db.exec(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.employee_profile_id == profile.id,
                EmployeeRoleAssignment.business_role_id == role.id,
            )
        ).one()
        skill = db.exec(select(Skill).where(Skill.skill_id == "participant_approval_demo")).one()
        versions = db.exec(
            select(SkillVersion).where(SkillVersion.skill_id == "participant_approval_demo")
        ).all()

        assert assignment.status == "active"
        assert skill.content_json["execution_mode"] == "deterministic"
        assert len(versions) == 1
        assert versions[0].compiled_definition_checksum


def test_demo_seed_freezes_initial_legacy_published_heads_idempotently() -> None:
    """验证新库初始化时四个兼容 SOP 同步写入可追溯且不重复的发布快照。"""

    expected_skill_ids = {
        "after_sales_exchange",
        "after_sales_refund",
        "skill_price_compare_001",
        "skill_purchase_001",
    }
    with _seeded_session() as db:
        seed_demo_data(db)
        seed_demo_data(db)
        versions = db.exec(
            select(SkillVersion).where(SkillVersion.skill_id.in_(expected_skill_ids))
        ).all()
        heads = db.exec(
            select(Skill).where(Skill.skill_id.in_(expected_skill_ids))
        ).all()

        assert {version.skill_id for version in versions} == expected_skill_ids
        assert len(versions) == len(expected_skill_ids)
        assert all(version.status == "published" for version in versions)
        assert all(version.content_checksum for version in versions)
        assert all(version.compiled_definition_checksum for version in versions)
        versions_by_skill_id = {version.skill_id: version for version in versions}
        assert all(
            skill_content_checksum(head.content_json)
            == skill_content_checksum(versions_by_skill_id[head.skill_id].content_json)
            for head in heads
        )


def test_demo_seed_second_start_does_not_rewrite_published_heads_or_bindings() -> None:
    """同一数据库第二次启动不得只因种子重放刷新发布头或资源绑定时间。"""

    with _seeded_session() as db:
        before_skills = [
            (
                row.id,
                row.version,
                row.status,
                row.content_json,
                row.updated_at,
            )
            for row in db.exec(select(Skill).order_by(Skill.id)).all()
        ]
        before_bindings = [
            (
                row.id,
                row.agent_id,
                row.resource_type,
                row.resource_id,
                row.status,
                row.metadata_json,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentResourceBinding).order_by(AgentResourceBinding.id)
            ).all()
        ]

        seed_demo_data(db)
        db.commit()

        after_skills = [
            (
                row.id,
                row.version,
                row.status,
                row.content_json,
                row.updated_at,
            )
            for row in db.exec(select(Skill).order_by(Skill.id)).all()
        ]
        after_bindings = [
            (
                row.id,
                row.agent_id,
                row.resource_type,
                row.resource_id,
                row.status,
                row.metadata_json,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentResourceBinding).order_by(AgentResourceBinding.id)
            ).all()
        ]

        assert after_skills == before_skills
        assert after_bindings == before_bindings


def test_public_mock_tool_migration_preserves_third_party_and_unknown_urls() -> None:
    from app.db.curated_gallery_seed import _migrate_public_mock_tool

    rows = [
        {
            "name": "bigmodel.web_search",
            "url": "https://open.bigmodel.cn/api/paas/v4/web_search",
            "headers_json": {"Authorization": "Bearer ${secret.BIGMODEL_API_KEY}"},
        },
        {
            "name": "other.quota",
            "url": "https://example.com/api/mock/expense/quota_query",
            "headers_json": {},
        },
        {
            "name": "legacy.unknown",
            "url": "http://58.57.119.30:52008/api/mock/unknown",
            "headers_json": {},
        },
    ]

    migrated = [_migrate_public_mock_tool(row, "https://staff.example.com") for row in rows]

    assert migrated == rows


def test_public_mock_tool_migration_is_idempotent() -> None:
    from app.db.curated_gallery_seed import _migrate_public_mock_tool

    source = {
        "name": "expense.quota_query",
        "url": "http://58.57.119.30:52008/api/mock/expense/quota_query",
        "headers_json": {},
    }
    once = _migrate_public_mock_tool(source, "https://staff.example.com")

    assert _migrate_public_mock_tool(once, "https://staff.example.com") == once
