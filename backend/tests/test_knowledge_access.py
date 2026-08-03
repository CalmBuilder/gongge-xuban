"""
@Time       : 2026/07/29 00:20
@Author     : zhanglp8181
@File       : test_knowledge_access.py
@CallChain  : pytest → knowledge access resolver → member/org/agent/knowledge facts
@Description: 验证成员、组织、数字员工绑定、请求限定和下载策略的统一允许拒绝矩阵。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.branching import ensure_agent_private_knowledge_branch
from app.api.knowledge import search_knowledge
from app.db.models import (
    AgentProfile,
    EmployeeProfile,
    KnowledgeBase,
    KnowledgeBaseOrgAccess,
    MemberOrgAssignment,
    OrganizationUnit,
    Tenant,
    User,
)
from app.knowledge.access import resolve_knowledge_access
from app.knowledge.schema import KnowledgeSearchRequest


def test_owner_tenant_and_organization_scopes_use_active_member_facts() -> None:
    """验证 owner、全租户和组织子树分别使用明确身份与当前有效任职。"""

    with _session() as db:
        owner, inside, outside = _seed_members_and_org(db)
        _seed_knowledge_bases(db, owner, agent=None)

        owner_result = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=owner,
        )
        inside_result = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=inside,
        )
        outside_result = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=outside,
        )

        assert set(owner_result.allowed_knowledge_base_ids) == {"kb_owner", "kb_tenant"}
        assert set(inside_result.allowed_knowledge_base_ids) == {"kb_org", "kb_tenant"}
        assert set(outside_result.allowed_knowledge_base_ids) == {"kb_tenant"}


def test_agent_binding_and_explicit_request_are_intersections_not_overrides() -> None:
    """验证绑定 Agent 或显式请求 ID 都只能缩小成员允许集，不能扩大正文范围。"""

    with _session() as db:
        owner, inside, _ = _seed_members_and_org(db)
        agent = AgentProfile(
            id="agent_policy",
            tenant_id="tenant_demo",
            name="制度助手",
            status="active",
            owner_user_id=owner.id,
        )
        db.add(agent)
        _seed_knowledge_bases(db, owner, agent=agent)
        db.commit()

        resolution = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=inside,
            agent_id=agent.id,
            requested_knowledge_base_ids=["kb_org", "kb_tenant"],
        )

        assert resolution.allowed_knowledge_base_ids == ("kb_org",)
        assert resolution.decision_for("kb_tenant").denial_reason == "agent_unbound"
        assert resolution.decision_for("kb_owner").denial_reason == "outside_request_scope"


def test_inactive_member_unknown_agent_and_restricted_download_fail_closed() -> None:
    """验证停用成员、未知 Agent 和限制下载策略均不会降级为租户全量。"""

    with _session() as db:
        owner, inside, _ = _seed_members_and_org(db)
        _seed_knowledge_bases(db, owner, agent=None)

        inside.membership_status = "suspended"
        db.add(inside)
        db.commit()
        inactive = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=inside,
        )
        unknown_agent = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=owner,
            agent_id="agent_missing",
        )
        restricted_download = resolve_knowledge_access(
            db,
            tenant_id="tenant_demo",
            current_user=owner,
            requested_knowledge_base_ids=["kb_owner"],
            require_download=True,
        )

        assert inactive.allowed_knowledge_base_ids == ()
        assert unknown_agent.allowed_knowledge_base_ids == ()
        assert restricted_download.allowed_knowledge_base_ids == ()
        assert (
            restricted_download.decision_for("kb_owner").denial_reason
            == "download_restricted"
        )


def test_explicit_inaccessible_version_does_not_fall_back_to_allowed_versions(
    monkeypatch,
) -> None:
    """验证显式版本求交为空时直接返回无权限，不把空列表当成全版本检索。"""

    with _session() as db:
        owner, inside, _ = _seed_members_and_org(db)
        agent = AgentProfile(
            id="agent_policy",
            tenant_id="tenant_demo",
            name="制度助手",
            status="active",
            owner_user_id=owner.id,
            published_to_gallery=True,
            visibility_scope="tenant",
        )
        db.add(agent)
        _seed_knowledge_bases(db, owner, agent=agent)
        db.commit()

        def fail_if_search_called(*_args, **_kwargs):
            """若下游检索被调用则表明显式版本限制发生了越权退化。"""

            raise AssertionError("KnowledgeService.search must not be called")

        monkeypatch.setattr(
            "app.api.knowledge.KnowledgeService.search",
            fail_if_search_called,
        )
        response = search_knowledge(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                query="政企规范",
                knowledge_base_version_ids=["kbver_forbidden"],
            ),
            db,
            inside,
        )

        assert response.outcome == "no_match"
        assert response.trace[0]["phase"] == "no_accessible_knowledge"
        assert "知识版本" in response.trace[0]["message"]


def _session() -> Session:
    """创建加载完整模型的独占内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_members_and_org(db: Session) -> tuple[User, User, User]:
    """建立真实公司样式组织子树以及范围内外三个活动成员。"""

    db.add(Tenant(id="tenant_demo", name="软件研究院"))
    root = OrganizationUnit(
        id="org_root",
        tenant_id="tenant_demo",
        code="ROOT",
        name="软件研究院",
        unit_type_code="company",
        tree_path="org_root",
        depth=0,
        is_root=True,
        root_tenant_id="tenant_demo",
    )
    project = OrganizationUnit(
        id="org_project",
        tenant_id="tenant_demo",
        parent_id=root.id,
        code="PROJECT",
        name="政企项目集",
        unit_type_code="department",
        tree_path="org_root/org_project",
        depth=1,
    )
    outside_org = OrganizationUnit(
        id="org_outside",
        tenant_id="tenant_demo",
        parent_id=root.id,
        code="OUTSIDE",
        name="公众研发事业部",
        unit_type_code="department",
        tree_path="org_root/org_outside",
        depth=1,
    )
    db.add(root)
    db.add(project)
    db.add(outside_org)
    users = [
        User(
            id=user_id,
            tenant_id="tenant_demo",
            username=user_id,
            password_hash="test",
        )
        for user_id in ("owner", "inside", "outside")
    ]
    for user in users:
        db.add(user)
        profile = EmployeeProfile(
            id=f"employee_{user.id}",
            tenant_id="tenant_demo",
            user_id=user.id,
            employee_id=f"E-{user.id}",
        )
        db.add(profile)
        db.add(
            MemberOrgAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=profile.id,
                org_unit_id=project.id if user.id == "inside" else outside_org.id,
            )
        )
    db.commit()
    return users[0], users[1], users[2]


def _seed_knowledge_bases(
    db: Session,
    owner: User,
    *,
    agent: AgentProfile | None,
) -> None:
    """创建 owner、tenant、组织三类知识库，并可只给 Agent 绑定组织知识。"""

    rows = [
        KnowledgeBase(
            id="kb_owner",
            tenant_id="tenant_demo",
            name="所有者资料",
            owner_user_id=owner.id,
            access_scope="owner",
        ),
        KnowledgeBase(
            id="kb_tenant",
            tenant_id="tenant_demo",
            name="全院制度",
            owner_user_id=owner.id,
            access_scope="tenant",
        ),
        KnowledgeBase(
            id="kb_org",
            tenant_id="tenant_demo",
            name="政企研发资料",
            owner_user_id=owner.id,
            access_scope="organization",
        ),
    ]
    for row in rows:
        db.add(row)
    db.add(
        KnowledgeBaseOrgAccess(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_org",
            org_unit_id="org_project",
            include_descendants=True,
        )
    )
    db.flush()
    if agent is not None:
        ensure_agent_private_knowledge_branch(
            db,
            "tenant_demo",
            agent.id,
            rows[2],
            metadata_json={"owner_user_id": owner.id},
        )
    db.commit()
