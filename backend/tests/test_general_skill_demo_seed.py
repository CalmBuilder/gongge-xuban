"""
@Time       : 2026/08/12 13:05
@Author     : zhanglp8181
@File       : test_general_skill_demo_seed.py
@CallChain  : pytest → initialize_skill_five_closure_demo → AgentProfile
@Description: 验证五闭环演示初始化的幂等、职责分离、密码零管理和既有数据保护。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import AgentProfile, Tenant, User
from app.general_skills.demo_seed import (
    SKILL_DEMO_AGENT_DEFINITIONS,
    SkillDemoSeedError,
    initialize_skill_five_closure_demo,
)


def _context() -> tuple[Session, dict[str, User]]:
    """建立不依赖生产 seed 的三账号内存租户。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    users = {
        "owner": User(
            id="demo_owner", tenant_id="tenant_demo_seed", username="owner", password_hash="owner"
        ),
        "adopter": User(
            id="demo_adopter",
            tenant_id="tenant_demo_seed",
            username="adopter",
            password_hash="adopter",
        ),
        "reviewer": User(
            id="demo_reviewer",
            tenant_id="tenant_demo_seed",
            username="reviewer",
            role="admin",
            password_hash="reviewer",
        ),
    }
    db.add(Tenant(id="tenant_demo_seed", name="Demo Seed"))
    db.add_all(list(users.values()))
    db.commit()
    return db, users


def test_demo_seed_is_idempotent_and_never_changes_credentials() -> None:
    """证明重复初始化只管理演示 Agent，既不新增账号也不修改密码。"""

    db, users = _context()
    first = initialize_skill_five_closure_demo(
        db,
        tenant_id="tenant_demo_seed",
        owner_username="owner",
        adopter_username="adopter",
        reviewer_username="reviewer",
    )
    second = initialize_skill_five_closure_demo(
        db,
        tenant_id="tenant_demo_seed",
        owner_username="owner",
        adopter_username="adopter",
        reviewer_username="reviewer",
    )
    assert len(first["created_agent_ids"]) == len(SKILL_DEMO_AGENT_DEFINITIONS)
    assert second["created_agent_ids"] == []
    assert len(second["unchanged_agent_ids"]) == len(SKILL_DEMO_AGENT_DEFINITIONS)
    assert len(db.exec(select(AgentProfile)).all()) == len(SKILL_DEMO_AGENT_DEFINITIONS)
    for role, user in users.items():
        assert db.get(User, user.id).password_hash == role


def test_demo_seed_requires_separated_admin_and_refuses_identity_takeover() -> None:
    """证明审核人必须独立管理员，且同名非演示记录不会被静默接管。"""

    db, users = _context()
    users["reviewer"].role = "member"
    db.add(users["reviewer"])
    db.commit()
    with pytest.raises(SkillDemoSeedError, match="administrator"):
        initialize_skill_five_closure_demo(
            db,
            tenant_id="tenant_demo_seed",
            owner_username="owner",
            adopter_username="adopter",
            reviewer_username="reviewer",
        )

    users["reviewer"].role = "admin"
    db.add(users["reviewer"])
    definition = SKILL_DEMO_AGENT_DEFINITIONS[0]
    db.add(
        AgentProfile(
            id="unrelated_agent",
            tenant_id="tenant_demo_seed",
            name=definition.name,
            owner_user_id=users["owner"].id,
        )
    )
    db.commit()
    with pytest.raises(SkillDemoSeedError, match="refusing to take over"):
        initialize_skill_five_closure_demo(
            db,
            tenant_id="tenant_demo_seed",
            owner_username="owner",
            adopter_username="adopter",
            reviewer_username="reviewer",
        )
