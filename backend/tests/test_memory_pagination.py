"""
@Time       : 2026/08/01 15:35
@Author     : zhanglp8181
@File       : test_memory_pagination.py
@CallChain  : pytest → memories.page_memories → MemoryRecord 查询
@Description: 验证员工记忆按用户分组分页、搜索和访问隔离行为。
"""

from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.memories import page_memories
from app.db.models import AgentProfile, MemoryRecord, Tenant, User


def test_page_memories_paginates_user_groups_after_agent_and_search_filters() -> None:
    """验证总数按用户组计算，当前页仍返回该用户的全部匹配记忆。"""

    with _test_session() as db:
        owner = _seed_owner_and_agent(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                MemoryRecord(
                    id="mem_a_profile",
                    tenant_id="tenant_demo",
                    agent_id="agent_owned",
                    user_id="user_a",
                    username="alice",
                    kind="profile",
                    content="蓝色偏好",
                    updated_at=base_time + timedelta(minutes=3),
                ),
                MemoryRecord(
                    id="mem_a_fact",
                    tenant_id="tenant_demo",
                    agent_id="agent_owned",
                    user_id="user_a",
                    username="alice",
                    kind="fact",
                    content="蓝色工牌",
                    updated_at=base_time + timedelta(minutes=2),
                ),
                MemoryRecord(
                    id="mem_b_profile",
                    tenant_id="tenant_demo",
                    agent_id="agent_owned",
                    user_id="user_b",
                    username="bob",
                    kind="profile",
                    content="蓝色衬衫",
                    updated_at=base_time + timedelta(minutes=1),
                ),
                MemoryRecord(
                    id="mem_other_agent",
                    tenant_id="tenant_demo",
                    agent_id="agent_other",
                    user_id="user_c",
                    username="carol",
                    kind="profile",
                    content="蓝色外套",
                    updated_at=base_time + timedelta(minutes=4),
                ),
            ]
        )
        db.commit()

        first_page = page_memories(
            tenant_id="tenant_demo",
            agent_id="agent_owned",
            user_id=None,
            username=None,
            q="蓝色",
            page=1,
            page_size=1,
            current_user=owner,
            db=db,
        )
        second_page = page_memories(
            tenant_id="tenant_demo",
            agent_id="agent_owned",
            user_id=None,
            username=None,
            q="user_b",
            page=1,
            page_size=10,
            current_user=owner,
            db=db,
        )

    assert first_page.total == 2
    assert first_page.page == 1
    assert [item["id"] for item in first_page.items] == ["mem_a_profile", "mem_a_fact"]
    assert second_page.total == 1
    assert [item["user_id"] for item in second_page.items] == ["user_b"]


def test_page_memories_keeps_non_owner_scoped_to_self() -> None:
    """验证非管理员且非员工创建者不能借分页筛选读取其他用户记忆。"""

    with _test_session() as db:
        owner = _seed_owner_and_agent(db)
        viewer = User(
            id="viewer_user",
            tenant_id="tenant_demo",
            username="viewer",
            password_hash="hash",
        )
        db.add(viewer)
        db.add_all(
            [
                MemoryRecord(
                    tenant_id="tenant_demo",
                    agent_id="agent_owned",
                    user_id=viewer.id,
                    username=viewer.username,
                    kind="profile",
                    content="访问者自己的记忆",
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    agent_id="agent_owned",
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="创建者隐私记忆",
                ),
            ]
        )
        db.commit()

        own_page = page_memories(
            "tenant_demo", "agent_owned", None, None, None, 1, 10, viewer, db
        )
        forbidden_filter = page_memories(
            "tenant_demo", "agent_owned", owner.id, None, None, 1, 10, viewer, db
        )

    assert own_page.total == 1
    assert [item["content"] for item in own_page.items] == ["访问者自己的记忆"]
    assert forbidden_filter.total == 0
    assert forbidden_filter.items == []


def _seed_owner_and_agent(db: Session) -> User:
    """创建分页测试共用的租户、员工创建者和员工档案。"""

    owner = User(
        id="owner_user",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="hash",
    )
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(owner)
    db.add(
        AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="创建者员工",
            status="active",
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
    )
    db.commit()
    return owner


def _test_session() -> Session:
    """创建启用完整 SQLModel 元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
