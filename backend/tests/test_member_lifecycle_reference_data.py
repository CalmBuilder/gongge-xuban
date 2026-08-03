"""
@Time       : 2026/07/28 12:05
@Author     : zhanglp8181
@File       : test_member_lifecycle_reference_data.py
@CallChain  : pytest → 成员类别码表服务 → CodeSet/CodeItem
@Description: 验证成员类别初始化、租户隔离和停用码项不可被新成员引用。
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import CodeItem, CodeSet, Tenant
from app.organization.reference_data import (
    ensure_member_category_catalog,
    require_active_member_category,
)


def test_member_category_catalog_is_idempotent_and_tenant_scoped() -> None:
    """验证每个租户只生成一套内置成员类别，重复初始化不会覆盖或复制。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(Tenant(id="tenant_b", name="B"))
        db.commit()

        first = ensure_member_category_catalog(db, "tenant_a")
        first.name = "人员类别"
        db.add(first)
        ensure_member_category_catalog(db, "tenant_a")
        second = ensure_member_category_catalog(db, "tenant_b")
        db.commit()

        sets = db.exec(select(CodeSet).order_by(CodeSet.tenant_id)).all()
        items = db.exec(select(CodeItem)).all()
        assert [(row.tenant_id, row.name) for row in sets] == [
            ("tenant_a", "人员类别"),
            ("tenant_b", "成员类别"),
        ]
        assert len([row for row in items if row.code_set_id == first.id]) == 5
        assert len([row for row in items if row.code_set_id == second.id]) == 5


def test_inactive_or_foreign_member_category_cannot_be_newly_referenced() -> None:
    """验证停用码项和其他租户码项不能被当前租户的新写入引用。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="A"))
        db.add(Tenant(id="tenant_b", name="B"))
        db.commit()
        code_set = ensure_member_category_catalog(db, "tenant_a")
        ensure_member_category_catalog(db, "tenant_b")
        contractor = db.exec(
            select(CodeItem).where(
                CodeItem.code_set_id == code_set.id,
                CodeItem.item_code == "contractor",
            )
        ).one()
        contractor.status = "inactive"
        db.add(contractor)
        db.commit()

        with pytest.raises(ValueError, match="INACTIVE_MEMBER_CATEGORY:contractor"):
            require_active_member_category(db, "tenant_a", "contractor")
        with pytest.raises(ValueError, match="UNKNOWN_MEMBER_CATEGORY:tenant_b_only"):
            require_active_member_category(db, "tenant_a", "tenant_b_only")

        historical = db.get(CodeItem, contractor.id)
        assert historical is not None
        assert historical.name == "合同员工"


def _test_session() -> Session:
    """创建包含全部模型表的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
