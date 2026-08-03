"""
@Time       : 2026/07/28 17:20
@Author     : zhanglp8181
@File       : test_reference_data_api.py
@CallChain  : pytest → 统一业务码表 API → CodeSet/CodeItem
@Description: 验证白名单、租户管理员权限、稳定编码、停用历史与 revision 冲突。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.reference_data import (
    CodeItemCreate,
    CodeItemUpdate,
    create_code_item,
    list_code_items,
    list_code_sets,
    update_code_item,
)
from app.db.models import Tenant, User


def test_unified_catalog_is_whitelisted_and_admin_governed() -> None:
    """验证统一目录只返回五类业务码表且普通成员不能治理。"""

    with _test_session() as db:
        admin, member = _identity_fixture(db)
        rows = list_code_sets("tenant_a", admin, db)
        assert [row.code for row in rows] == [
            "member_category",
            "organization_unit_type",
            "position_type",
            "organization_leader_type",
            "agent_category",
        ]
        with pytest.raises(HTTPException) as forbidden:
            list_code_sets("tenant_a", member, db)
        assert forbidden.value.status_code == 403


def test_custom_item_keeps_code_and_uses_revision_for_updates() -> None:
    """验证自定义码项编码稳定，状态可停用且过期 revision 被拒绝。"""

    with _test_session() as db:
        admin, _ = _identity_fixture(db)
        created = create_code_item(
            "organization_unit_type",
            CodeItemCreate(
                tenant_id="tenant_a",
                code="research_institute",
                name="研究院",
                sort_order=80,
            ),
            admin,
            db,
        )
        assert created.code == "research_institute"
        updated = update_code_item(
            "organization_unit_type",
            created.code,
            CodeItemUpdate(
                tenant_id="tenant_a",
                name="专业研究院",
                status="inactive",
                sort_order=85,
                revision=created.revision,
            ),
            admin,
            db,
        )
        assert updated.code == created.code
        assert updated.status == "inactive"
        assert updated.revision == 1
        with pytest.raises(HTTPException) as conflict:
            update_code_item(
                "organization_unit_type",
                created.code,
                CodeItemUpdate(
                    tenant_id="tenant_a",
                    name="错误覆盖",
                    status="active",
                    sort_order=90,
                    revision=0,
                ),
                admin,
                db,
            )
        assert conflict.value.status_code == 409
        rows = list_code_items(
            "organization_unit_type",
            "tenant_a",
            admin,
            db,
        )
        assert any(row.code == created.code and row.status == "inactive" for row in rows)


def _test_session() -> Session:
    """创建不跨线程丢失数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _identity_fixture(db: Session) -> tuple[User, User]:
    """创建同租户管理员与普通成员。"""

    db.add(Tenant(id="tenant_a", name="企业甲"))
    admin = User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="test",
    )
    member = User(
        id="member_a",
        tenant_id="tenant_a",
        username="member",
        role="member",
        password_hash="test",
    )
    db.add(admin)
    db.add(member)
    db.commit()
    return admin, member
