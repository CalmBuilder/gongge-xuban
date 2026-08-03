"""
@Time       : 2026/07/28 11:15
@Author     : zhanglp8181
@File       : test_organization_units.py
@CallChain  : pytest → 组织树服务 → OrganizationUnit/CodeSet/CodeItem
@Description: 验证 M2 单根组织树、类型码表、移动环保护和停用引用约束。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.organization_units import (
    OrganizationUnitCreate,
    OrganizationUnitUpdate,
    create_organization_unit_endpoint,
    list_organization_units,
    update_organization_unit_endpoint,
)
from app.db.models import CodeItem, CodeSet, OrganizationUnit, Tenant, User
from app.organization.units import (
    OrganizationUnitError,
    create_organization_unit,
    deactivate_organization_unit,
    ensure_organization_foundation,
    move_organization_unit,
)


def test_organization_foundation_is_idempotent_and_tenant_scoped() -> None:
    """验证每个租户只初始化一个稳定根节点和一套组织类型码项。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业 A"))
        db.add(Tenant(id="tenant_b", name="企业 B"))
        db.commit()

        first_root = ensure_organization_foundation(db, "tenant_a")
        repeated_root = ensure_organization_foundation(db, "tenant_a")
        second_root = ensure_organization_foundation(db, "tenant_b")
        db.commit()

        roots = db.exec(
            select(OrganizationUnit).where(OrganizationUnit.is_root.is_(True))
        ).all()
        code_sets = db.exec(
            select(CodeSet).where(CodeSet.set_code == "organization_unit_type")
        ).all()
        items = db.exec(select(CodeItem)).all()

        assert first_root.id == repeated_root.id
        assert {(row.tenant_id, row.name, row.code) for row in roots} == {
            ("tenant_a", "企业 A", "ROOT"),
            ("tenant_b", "企业 B", "ROOT"),
        }
        assert second_root.tree_path == second_root.id
        assert len(code_sets) == 2
        assert all(
            len([item for item in items if item.code_set_id == code_set.id]) == 6
            for code_set in code_sets
        )


def test_organization_move_updates_subtree_and_rejects_cycles_or_foreign_parents() -> None:
    """验证组织移动原子更新子树路径，并拒绝根移动、成环和跨租户父节点。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业 A"))
        db.add(Tenant(id="tenant_b", name="企业 B"))
        db.commit()
        root_a = ensure_organization_foundation(db, "tenant_a")
        root_b = ensure_organization_foundation(db, "tenant_b")
        division = create_organization_unit(
            db,
            tenant_id="tenant_a",
            parent_id=root_a.id,
            code="engineering",
            name="研发体系",
            unit_type_code="division",
        )
        team = create_organization_unit(
            db,
            tenant_id="tenant_a",
            parent_id=division.id,
            code="platform",
            name="平台组",
            unit_type_code="team",
        )
        center = create_organization_unit(
            db,
            tenant_id="tenant_a",
            parent_id=root_a.id,
            code="delivery",
            name="交付中心",
            unit_type_code="center",
        )
        db.commit()

        with pytest.raises(OrganizationUnitError, match="ROOT_UNIT_IMMUTABLE"):
            move_organization_unit(db, root_a, center.id)
        with pytest.raises(OrganizationUnitError, match="ORGANIZATION_CYCLE"):
            move_organization_unit(db, division, team.id)
        with pytest.raises(OrganizationUnitError, match="PARENT_NOT_FOUND"):
            move_organization_unit(db, division, root_b.id)

        move_organization_unit(db, division, center.id)
        db.commit()
        db.refresh(division)
        db.refresh(team)

        assert division.parent_id == center.id
        assert division.depth == 2
        assert division.tree_path == f"{root_a.id}/{center.id}/{division.id}"
        assert team.depth == 3
        assert team.tree_path == f"{division.tree_path}/{team.id}"


def test_root_and_organization_with_active_children_cannot_be_deactivated() -> None:
    """验证根节点和仍有活动下级的组织不能被停用。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业 A"))
        db.commit()
        root = ensure_organization_foundation(db, "tenant_a")
        division = create_organization_unit(
            db,
            tenant_id="tenant_a",
            parent_id=root.id,
            code="operations",
            name="运营体系",
            unit_type_code="division",
        )
        create_organization_unit(
            db,
            tenant_id="tenant_a",
            parent_id=division.id,
            code="support",
            name="支持组",
            unit_type_code="team",
        )
        db.commit()

        with pytest.raises(OrganizationUnitError, match="ROOT_UNIT_IMMUTABLE"):
            deactivate_organization_unit(db, root)
        with pytest.raises(OrganizationUnitError, match="ACTIVE_CHILDREN_EXIST"):
            deactivate_organization_unit(db, division)


def test_organization_api_enforces_admin_and_tenant_boundaries() -> None:
    """验证普通成员不能进入组织治理，兼容管理员仍可读写且不能跨租户。"""

    with _test_session() as db:
        db.add(Tenant(id="tenant_a", name="企业 A"))
        db.add(Tenant(id="tenant_b", name="企业 B"))
        administrator = User(
            id="admin_a",
            tenant_id="tenant_a",
            username="admin",
            role="admin",
            password_hash="test-only",
        )
        member = User(
            id="member_a",
            tenant_id="tenant_a",
            username="member",
            role="member",
            password_hash="test-only",
        )
        db.add(administrator)
        db.add(member)
        db.commit()

        with pytest.raises(HTTPException) as member_read:
            list_organization_units("tenant_a", member, db)
        assert member_read.value.status_code == 403

        rows = list_organization_units("tenant_a", administrator, db)
        root = rows[0]
        assert root.is_root is True

        with pytest.raises(HTTPException) as cross_tenant:
            list_organization_units("tenant_b", member, db)
        assert cross_tenant.value.status_code == 403

        request = OrganizationUnitCreate(
            tenant_id="tenant_a",
            parent_id=root.id,
            code="finance",
            name="财务部",
            unit_type_code="department",
        )
        with pytest.raises(HTTPException) as forbidden:
            create_organization_unit_endpoint(request, member, db)
        assert forbidden.value.status_code == 403

        created = create_organization_unit_endpoint(request, administrator, db)
        assert created.parent_id == root.id
        assert created.tree_path == f"{root.id}/{created.id}"

        with pytest.raises(HTTPException) as immutable:
            update_organization_unit_endpoint(
                root.id,
                OrganizationUnitUpdate(tenant_id="tenant_a", name="另一个根"),
                administrator,
                db,
            )
        assert immutable.value.status_code == 400
        assert immutable.value.detail == "ROOT_UNIT_IMMUTABLE"


def _test_session() -> Session:
    """创建包含全部模型表的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
