"""
@Time       : 2026/07/28 18:00
@Author     : zhanglp8181
@File       : test_organization_scale.py
@CallChain  : pytest → 匿名规模夹具 → 子级/摘要/分页查询与 SQLite 查询计划
@Description: 验证 500 组织、5000 成员、8000 归属下响应行数有界且组合索引可用。
"""

from time import perf_counter
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.api.auth import page_users
from app.api.organization_units import (
    get_organization_unit_summary,
    list_organization_unit_children,
)
from app.db.models import User
from tests.organization_large_fixture import seed_large_organization_fixture


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_large_organization_queries_keep_response_rows_bounded(
    tmp_path,
    record_property,
) -> None:
    """记录规模环境耗时和计划，并断言树、摘要、分页不会返回全租户行。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'organization-scale.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        seed_started = perf_counter()
        fixture = seed_large_organization_fixture(db)
        seed_elapsed = perf_counter() - seed_started
        administrator = User(
            id="admin_scale",
            tenant_id=fixture.tenant_id,
            username="admin_scale",
            display_name="规模管理员",
            role="admin",
            password_hash="scale-fixture-only",
        )
        db.add(administrator)
        db.commit()

        query_started = perf_counter()
        children = list_organization_unit_children(
            fixture.tenant_id,
            fixture.root_id,
            administrator,
            db,
        )
        summary = get_organization_unit_summary(
            fixture.tenant_id,
            fixture.root_id,
            administrator,
            db,
        )
        page = page_users(
            tenant_id=fixture.tenant_id,
            page=2,
            page_size=50,
            keyword=None,
            membership_status="active",
            member_category_code=None,
            org_unit_id=fixture.root_id,
            include_descendants=True,
            assignment_type=None,
            current_user=administrator,
            db=db,
        )
        query_elapsed = perf_counter() - query_started
        plan = db.exec(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM organization_units "
                "WHERE tenant_id = :tenant_id AND parent_id = :parent_id "
                "AND status = 'active' ORDER BY sort_order"
            ),
            params={"tenant_id": fixture.tenant_id, "parent_id": fixture.root_id},
        ).all()

        record_property("organizations", fixture.organization_count)
        record_property("members", fixture.member_count)
        record_property("org_assignments", fixture.org_assignment_count)
        record_property("positions", fixture.position_count)
        record_property("leaders", fixture.leader_count)
        record_property("seed_elapsed_seconds", round(seed_elapsed, 3))
        record_property("query_elapsed_seconds", round(query_elapsed, 3))
        record_property("direct_children_response_rows", len(children))
        record_property("member_page_response_rows", len(page.items))
        record_property("sqlite_direct_children_plan", str(plan))

        assert fixture.organization_count == 500
        assert fixture.member_count == 5000
        assert fixture.org_assignment_count == 8000
        assert len(children) == 10
        assert summary.subtree_member_count == 5000
        assert page.total == 5000
        assert len(page.items) == 50
        assert "ix_org_unit_tenant_parent_status_sort" in str(plan)


@pytest.mark.mysql
def test_large_organization_queries_use_bounded_pages_on_mysql(
    mysql_database_url: str,
    record_property,
) -> None:
    """在隔离 MySQL 8.4 重放同一规模夹具并记录真实查询计划与环境耗时。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = mysql_database_url
    command.upgrade(config, "head")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as db:
        seed_started = perf_counter()
        fixture = seed_large_organization_fixture(db, tenant_id="tenant_scale_mysql")
        seed_elapsed = perf_counter() - seed_started
        administrator = User(
            id="admin_scale_mysql",
            tenant_id=fixture.tenant_id,
            username="admin_scale",
            display_name="规模管理员",
            role="admin",
            password_hash="scale-fixture-only",
        )
        db.add(administrator)
        db.commit()

        query_started = perf_counter()
        children = list_organization_unit_children(
            fixture.tenant_id,
            fixture.root_id,
            administrator,
            db,
        )
        page = page_users(
            tenant_id=fixture.tenant_id,
            page=1,
            page_size=50,
            keyword="成员 00",
            membership_status="active",
            member_category_code="employee",
            org_unit_id=fixture.root_id,
            include_descendants=True,
            assignment_type=None,
            current_user=administrator,
            db=db,
        )
        query_elapsed = perf_counter() - query_started
        plan = db.exec(
            text(
                "EXPLAIN SELECT id FROM organization_units "
                "WHERE tenant_id = :tenant_id AND parent_id = :parent_id "
                "AND status = 'active' ORDER BY sort_order"
            ),
            params={"tenant_id": fixture.tenant_id, "parent_id": fixture.root_id},
        ).all()

        record_property("mysql_seed_elapsed_seconds", round(seed_elapsed, 3))
        record_property("mysql_query_elapsed_seconds", round(query_elapsed, 3))
        record_property("mysql_direct_children_plan", str(plan))
        record_property("mysql_member_page_response_rows", len(page.items))

        assert len(children) == 10
        assert page.total == 1000
        assert len(page.items) == 50
        assert "ix_org_unit_tenant_parent_status_sort" in str(plan)
    engine.dispose()
