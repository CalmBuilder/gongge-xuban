"""
@Time       : 2026/08/01 23:20
@Author     : zhanglp8181
@File       : test_agent_gallery_pagination.py
@CallChain  : pytest → agents.page_agent_gallery → AgentProfile/AgentUsage
@Description: 验证数字员工广场关系视图分页、计数、搜索、专家筛选和访问隔离。
"""

from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.schema import AgentGalleryPageRead, AgentGalleryScope, AgentManagementPageRead
from app.api.agents import page_agent_gallery, page_managed_agents
from app.db.models import AgentProfile, AgentUsage, Tenant, User


def test_owned_gallery_page_paginates_before_card_projection_and_returns_scope_counts() -> None:
    """验证我创建的员工按稳定顺序分页，计数排除归档、隐藏和其他关系记录。"""

    with _test_session() as db:
        admin, other = _seed_users(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        owned_rows = [
            AgentProfile(
                id=f"agent_owned_{index:02d}",
                tenant_id="tenant_demo",
                name=f"员工 {index:02d}",
                owner_user_id=admin.id,
                status="active",
                updated_at=base_time + timedelta(minutes=index),
            )
            for index in range(13)
        ]
        gallery = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            owner_user_id=other.id,
            status="active",
            published_to_gallery=True,
        )
        db.add_all(
            [
                *owned_rows,
                gallery,
                AgentProfile(
                    id="agent_archived",
                    tenant_id="tenant_demo",
                    name="归档员工",
                    owner_user_id=admin.id,
                    status="archived",
                ),
                AgentProfile(
                    id="agent_hidden",
                    tenant_id="tenant_demo",
                    name="隐藏员工",
                    owner_user_id=admin.id,
                    status="active",
                    metadata_json={"hidden_from_product": True},
                ),
            ]
        )
        db.add(
            AgentUsage(
                tenant_id="tenant_demo",
                user_id=admin.id,
                agent_id=gallery.id,
            )
        )
        db.commit()

        first = _page(db, admin, "owned", page=1, page_size=12)
        second = _page(db, admin, "owned", page=2, page_size=12)

    assert first.total == 13
    assert first.scope_counts["owned"] == 13
    assert first.scope_counts["used"] == 1
    assert first.scope_counts["gallery"] == 1
    assert [item.id for item in first.items[:2]] == ["agent_owned_12", "agent_owned_11"]
    assert [item.id for item in second.items] == ["agent_owned_00"]


def test_gallery_search_and_expert_facets_keep_visibility_and_filter_semantics() -> None:
    """验证搜索覆盖标签，专家级联筛选返回完整计数且普通成员看不到私人专家。"""

    with _test_session() as db:
        owner, other = _seed_users(db)
        db.add_all(
            [
                AgentProfile(
                    id="expert_data",
                    tenant_id="tenant_demo",
                    name="数据工程师",
                    owner_user_id=owner.id,
                    status="active",
                    published_to_gallery=True,
                    agent_category_code="professional",
                    metadata_json={
                        "employee_type": "expert",
                        "expert_source_code": "agency-agents",
                        "expert_category": "工程研发",
                        "expert_subcategory": "数据与数据库",
                        "expert_tags": ["SQL", "数据治理"],
                    },
                ),
                AgentProfile(
                    id="expert_frontend",
                    tenant_id="tenant_demo",
                    name="前端工程师",
                    owner_user_id=other.id,
                    status="active",
                    published_to_gallery=True,
                    agent_category_code="professional",
                    metadata_json={
                        "employee_type": "expert",
                        "expert_source_code": "agency-agents",
                        "expert_category": "工程研发",
                        "expert_subcategory": "前端与客户端",
                        "expert_tags": ["React"],
                    },
                ),
                AgentProfile(
                    id="expert_private",
                    tenant_id="tenant_demo",
                    name="私人专家",
                    owner_user_id=other.id,
                    status="active",
                    published_to_gallery=False,
                    agent_category_code="professional",
                    metadata_json={
                        "employee_type": "expert",
                        "expert_source_code": "private",
                        "expert_category": "私人部门",
                    },
                ),
            ]
        )
        db.commit()

        searched = _page(db, owner, "expert", q="数据治理")
        filtered = _page(
            db,
            owner,
            "expert",
            expert_source="agency-agents",
            expert_department="工程研发",
            expert_direction="前端与客户端",
        )

    assert [item.id for item in searched.items] == ["expert_data"]
    assert searched.scope_counts["expert"] == 2
    assert [(item.value, item.count) for item in filtered.facets.sources] == [
        ("agency-agents", 2)
    ]
    assert [(item.value, item.count) for item in filtered.facets.departments] == [
        ("工程研发", 2)
    ]
    assert [(item.value, item.count) for item in filtered.facets.directions] == [
        ("前端与客户端", 1),
        ("数据与数据库", 1),
    ]
    assert [item.id for item in filtered.items] == ["expert_frontend"]


def test_management_page_uses_owner_scope_and_returns_cross_page_view_counts() -> None:
    """验证管理页只分页当前所有者员工，并以全集统计在线、下线和专家视图。"""

    with _test_session() as db:
        owner, other = _seed_users(db)
        db.add_all(
            [
                AgentProfile(
                    id=f"managed_{index:02d}",
                    tenant_id="tenant_demo",
                    name=f"管理员工 {index:02d}",
                    owner_user_id=owner.id,
                    status="archived" if index == 0 else "active",
                    agent_category_code="professional" if index < 2 else "assistant",
                )
                for index in range(13)
            ]
            + [
                AgentProfile(
                    id="managed_other",
                    tenant_id="tenant_demo",
                    name="其他成员员工",
                    owner_user_id=other.id,
                    status="active",
                )
            ]
        )
        db.commit()

        first = _management_page(db, owner, page=1)
        second = _management_page(db, owner, page=2)

    assert first.total == 13
    assert len(first.items) == 12
    assert [item.id for item in second.items] == ["managed_00"]
    assert first.view_counts == {
        "all": 13,
        "online": 12,
        "offline": 1,
        "pending": 0,
        "expert": 2,
        "governance": 0,
    }


def _management_page(
    db: Session,
    user: User,
    *,
    page: int,
) -> AgentManagementPageRead:
    """使用显式参数调用管理分页函数，固定测试边界为每页十二条。"""

    return page_managed_agents(
        tenant_id="tenant_demo",
        view="all",
        q=None,
        expert_source=None,
        expert_department=None,
        expert_direction=None,
        page=page,
        page_size=12,
        db=db,
        current_user=user,
    )


def _page(
    db: Session,
    user: User,
    scope: AgentGalleryScope,
    *,
    q: str | None = None,
    expert_source: str | None = None,
    expert_department: str | None = None,
    expert_direction: str | None = None,
    page: int = 1,
    page_size: int = 12,
) -> AgentGalleryPageRead:
    """使用显式参数调用广场分页函数，避免 FastAPI Query 默认对象影响单元测试。"""

    return page_agent_gallery(
        tenant_id="tenant_demo",
        scope=scope,
        q=q,
        expert_source=expert_source,
        expert_department=expert_department,
        expert_direction=expert_direction,
        page=page,
        page_size=page_size,
        db=db,
        current_user=user,
    )


def _seed_users(db: Session) -> tuple[User, User]:
    """创建广场分页测试使用的租户和两个普通成员。"""

    owner = User(
        id="owner_user",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="hash",
    )
    other = User(
        id="other_user",
        tenant_id="tenant_demo",
        username="other",
        password_hash="hash",
    )
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add_all([owner, other])
    db.commit()
    return owner, other


def _test_session() -> Session:
    """创建带完整 SQLModel 元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
