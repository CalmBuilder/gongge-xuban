"""
@Time       : 2026/07/28 17:35
@Author     : zhanglp8181
@File       : test_organization_queries.py
@CallChain  : pytest → M2.5-B 查询 API/服务 → 组织树、成员归属和分页摘要
@Description: 验证唯一子树、按层组织、直属/子树统计和成员服务端分页契约。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.auth import page_users
from app.api.organization_units import (
    get_organization_unit_summary,
    list_organization_unit_children,
    search_organization_units,
)
from app.db.models import EmployeeProfile, Tenant, User
from app.organization.assignments import assign_member_to_organization
from app.organization.query import resolve_organization_subtree_ids
from app.organization.units import (
    OrganizationUnitError,
    create_organization_unit,
    ensure_organization_foundation,
)


def test_subtree_children_search_and_counts_share_stable_organization_facts() -> None:
    """验证按层读取、搜索路径、子树解析和去重人数使用同一组织事实。"""

    with _test_session() as db:
        fixture = _query_fixture(db)
        root = fixture["root"]
        region = fixture["region"]
        project = fixture["project"]
        admin = fixture["admin"]

        root_nodes = list_organization_unit_children("tenant_a", None, admin, db)
        region_nodes = list_organization_unit_children(
            "tenant_a", region.id, admin, db
        )
        search_rows = search_organization_units(
            "tenant_a", "项目", 20, admin, db
        )
        summary = get_organization_unit_summary(
            "tenant_a", region.id, admin, db
        )
        subtree_ids = resolve_organization_subtree_ids(
            db,
            tenant_id="tenant_a",
            root_org_unit_id=region.id,
            include_descendants=True,
        )

        assert [row.id for row in root_nodes] == [root.id]
        assert root_nodes[0].has_children is True
        assert [row.id for row in region_nodes] == [
            fixture["department"].id,
            project.id,
        ]
        assert region_nodes[0].has_children is False
        assert search_rows[0].id == project.id
        assert [item.id for item in search_rows[0].path] == [
            root.id,
            region.id,
            project.id,
        ]
        assert subtree_ids == [
            region.id,
            fixture["department"].id,
            project.id,
        ]
        assert summary.direct_member_count == 1
        assert summary.subtree_member_count == 3
        assert summary.direct_child_count == 2


def test_hierarchical_queries_hide_inactive_organization_history() -> None:
    """验证正常组织树和搜索不展示已停用节点，且停用后代不制造展开入口。"""

    with _test_session() as db:
        fixture = _query_fixture(db)
        admin = fixture["admin"]
        department = fixture["department"]

        department_nodes = list_organization_unit_children(
            "tenant_a", department.id, admin, db
        )
        search_rows = search_organization_units(
            "tenant_a", "历史回归节点", 20, admin, db
        )

        assert department_nodes == []
        assert search_rows == []


def test_member_page_filters_descendants_and_returns_stable_page_summaries() -> None:
    """验证组织子树筛选、服务端分页和当前主组织摘要不依赖浏览器全量数据。"""

    with _test_session() as db:
        fixture = _query_fixture(db)
        admin = fixture["admin"]
        region = fixture["region"]

        first_page = page_users(
            tenant_id="tenant_a",
            page=1,
            page_size=2,
            keyword=None,
            membership_status="active",
            member_category_code=None,
            org_unit_id=region.id,
            include_descendants=True,
            assignment_type=None,
            current_user=admin,
            db=db,
        )
        second_page = page_users(
            tenant_id="tenant_a",
            page=2,
            page_size=2,
            keyword=None,
            membership_status="active",
            member_category_code=None,
            org_unit_id=region.id,
            include_descendants=True,
            assignment_type=None,
            current_user=admin,
            db=db,
        )
        direct_page = page_users(
            tenant_id="tenant_a",
            page=1,
            page_size=20,
            keyword="区域",
            membership_status=None,
            member_category_code=None,
            org_unit_id=region.id,
            include_descendants=False,
            assignment_type="primary",
            current_user=admin,
            db=db,
        )

        assert first_page.total == 3
        assert len(first_page.items) == 2
        assert len(second_page.items) == 1
        assert {
            item.id for item in first_page.items + second_page.items
        } == {"member_region", "member_department", "member_project"}
        assert direct_page.total == 1
        assert direct_page.items[0].primary_org_unit_id == region.id
        assert direct_page.items[0].primary_org_name == region.name
        assert direct_page.items[0].assignment_history_count == 1


def test_organization_queries_reject_foreign_roots_and_member_enumeration() -> None:
    """验证非法租户根和普通成员分页枚举均在服务端拒绝。"""

    with _test_session() as db:
        fixture = _query_fixture(db)
        member = fixture["member_user"]

        with pytest.raises(OrganizationUnitError, match="ORGANIZATION_NOT_FOUND"):
            resolve_organization_subtree_ids(
                db,
                tenant_id="tenant_b",
                root_org_unit_id=fixture["region"].id,
                include_descendants=True,
            )
        with pytest.raises(HTTPException) as forbidden:
            page_users(
                tenant_id="tenant_a",
                page=1,
                page_size=20,
                keyword=None,
                membership_status=None,
                member_category_code=None,
                org_unit_id=None,
                include_descendants=False,
                assignment_type=None,
                current_user=member,
                db=db,
            )
        assert forbidden.value.status_code == 403


def _query_fixture(db: Session) -> dict[str, object]:
    """创建完全匿名的小型多层组织和主职/项目兼任查询夹具。"""

    db.add(Tenant(id="tenant_a", name="匿名企业"))
    db.add(Tenant(id="tenant_b", name="其他企业"))
    admin = User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        display_name="管理员",
        role="admin",
        password_hash="test-only",
    )
    db.add(admin)
    root = ensure_organization_foundation(db, "tenant_a")
    ensure_organization_foundation(db, "tenant_b")
    region = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=root.id,
        code="REGION_N",
        name="北区研究中心",
        unit_type_code="division",
        sort_order=10,
    )
    department = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=region.id,
        code="SHARED_SERVICE",
        name="共享服务部",
        unit_type_code="department",
        sort_order=10,
    )
    project = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=region.id,
        code="PORTFOLIO_A",
        name="项目组合甲",
        unit_type_code="project",
        sort_order=20,
    )
    inactive_history = create_organization_unit(
        db,
        tenant_id="tenant_a",
        parent_id=department.id,
        code="INACTIVE_HISTORY",
        name="历史回归节点",
        unit_type_code="project",
        sort_order=30,
    )
    inactive_history.status = "inactive"
    db.add(inactive_history)
    member_user = None
    for suffix, name, org_id in (
        ("region", "区域成员", region.id),
        ("department", "职能成员", department.id),
        ("project", "项目成员", project.id),
        ("outside", "外部成员", root.id),
    ):
        user = User(
            id=f"member_{suffix}",
            tenant_id="tenant_a",
            username=f"member_{suffix}",
            display_name=name,
            role="member",
            password_hash="test-only",
        )
        profile = EmployeeProfile(
            id=f"profile_{suffix}",
            tenant_id="tenant_a",
            user_id=user.id,
            employee_id=f"E_{suffix}",
            employee_name=name,
        )
        db.add(user)
        db.add(profile)
        db.flush()
        assign_member_to_organization(
            db,
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            org_unit_id=org_id,
        )
        if suffix == "region":
            member_user = user
    db.commit()
    return {
        "admin": admin,
        "member_user": member_user,
        "root": root,
        "region": region,
        "department": department,
        "project": project,
        "inactive_history": inactive_history,
    }


def _test_session() -> Session:
    """创建包含全部模型表和组合索引的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
