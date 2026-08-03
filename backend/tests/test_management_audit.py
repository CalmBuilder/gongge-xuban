"""
@Time       : 2026/07/29 01:40
@Author     : zhanglp8181
@File       : test_management_audit.py
@CallChain  : pytest → management audit service/API → governance grants/organization scope
@Description: 验证独立管理审计的递归脱敏、追加写入、组织范围、过滤分页和直接 URL 防越权。
"""

from __future__ import annotations

from fastapi import HTTPException
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.management_audit import (
    get_management_audit_log,
    list_management_audit_logs,
)
from app.audit.service import (
    append_management_audit,
    append_user_management_audit,
    query_management_audits,
    sanitize_audit_payload,
)
from app.db.models import (
    BusinessRole,
    EmployeeProfile,
    EmployeeRoleAssignment,
    OrganizationUnit,
    Tenant,
    User,
)
from app.organization.governance import ensure_builtin_governance_catalog


def test_sanitizer_recursively_redacts_secrets_bodies_and_large_values() -> None:
    """验证敏感键、正文、深层集合和超长字符串在持久化前统一收紧。"""

    payload = {
        "name": "政企资料",
        "authorization": "Bearer secret-value",
        "nested": {
            "api_key": "sk-live",
            "prompt": "private prompt",
            "items": list(range(30)),
            "summary": "甲" * 800,
        },
    }

    sanitized = sanitize_audit_payload(payload)

    assert sanitized["name"] == "政企资料"
    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["nested"]["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["prompt"] == "[REDACTED]"
    assert len(sanitized["nested"]["items"]) == 20
    assert sanitized["nested"]["items"][-1] == "[TRUNCATED 11 ITEMS]"
    assert len(sanitized["nested"]["summary"]) <= 500


def test_query_filters_tenant_and_scoped_auditor_organization_ids() -> None:
    """验证租户审计与组织子树审计使用结构化范围，不泄漏无组织目标记录。"""

    with _session() as db:
        fixture = _fixture(db)
        _append_fixture_logs(db)

        tenant_rows, tenant_total = query_management_audits(
            db,
            tenant_id="tenant_a",
            allowed_organization_ids=None,
            page=1,
            page_size=20,
        )
        scoped_rows, scoped_total = query_management_audits(
            db,
            tenant_id="tenant_a",
            allowed_organization_ids=frozenset(
                {fixture["division"].id, fixture["department"].id}
            ),
            page=1,
            page_size=20,
        )

        assert tenant_total == 4
        assert {row.id for row in tenant_rows} == {
            "audit_division",
            "audit_department",
            "audit_sibling",
            "audit_tenant_only",
        }
        assert scoped_total == 2
        assert {row.id for row in scoped_rows} == {
            "audit_division",
            "audit_department",
        }


def test_query_filters_actor_action_resource_outcome_and_paginates() -> None:
    """验证审计台账使用服务端过滤和稳定分页，不在前端加载全量后筛选。"""

    with _session() as db:
        _fixture(db)
        _append_fixture_logs(db)

        rows, total = query_management_audits(
            db,
            tenant_id="tenant_a",
            allowed_organization_ids=None,
            actor_user_id="owner",
            action="organization.update",
            resource_type="organization_unit",
            outcome="success",
            page=1,
            page_size=1,
        )

        assert total == 2
        assert len(rows) == 1
        assert rows[0].id == "audit_department"


def test_detail_direct_url_obeys_audit_read_organization_scope() -> None:
    """验证范围审计员猜测范围外日志 ID 时返回 404，租户管理员仍可读取。"""

    with _session() as db:
        fixture = _fixture(db)
        _append_fixture_logs(db)

        inside = get_management_audit_log(
            "audit_department",
            tenant_id="tenant_a",
            current_user=fixture["auditor"],
            db=db,
        )
        assert inside.id == "audit_department"
        assert inside.detail["token"] == "[REDACTED]"

        with pytest.raises(HTTPException) as outside:
            get_management_audit_log(
                "audit_sibling",
                tenant_id="tenant_a",
                current_user=fixture["auditor"],
                db=db,
            )
        assert outside.value.status_code == 404

        tenant_visible = get_management_audit_log(
            "audit_sibling",
            tenant_id="tenant_a",
            current_user=fixture["owner"],
            db=db,
        )
        assert tenant_visible.id == "audit_sibling"


def test_list_api_rejects_cross_tenant_audit_query() -> None:
    """验证浏览器不能用当前租户账号查询另一租户审计台账。"""

    with _session() as db:
        fixture = _fixture(db)
        db.add(Tenant(id="tenant_b", name="企业乙"))
        db.commit()

        with pytest.raises(HTTPException) as forbidden:
            list_management_audit_logs(
                tenant_id="tenant_b",
                page=1,
                page_size=20,
                actor_user_id=None,
                action=None,
                action_kind=None,
                outcome=None,
                resource_type=None,
                resource_id=None,
                created_after=None,
                created_before=None,
                current_user=fixture["owner"],
                db=db,
            )

        assert forbidden.value.status_code == 403


def test_user_audit_write_failure_does_not_abort_outer_business_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证审计追加异常被保存点隔离，业务侧随后仍可提交自己的状态变更。"""

    with _session() as db:
        fixture = _fixture(db)

        def fail_append(*args: object, **kwargs: object) -> None:
            """模拟审计存储不可写，不修改外层业务会话。"""

            del args, kwargs
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("audit unavailable")

        monkeypatch.setattr("app.audit.service.append_management_audit", fail_append)
        fixture["division"].name = "科技创新与研发部"
        db.add(fixture["division"])

        result = append_user_management_audit(
            db,
            current_user=fixture["owner"],
            tenant_id="tenant_a",
            permission_code="organization.manage",
            action="organization.update",
            action_kind="update",
            outcome="success",
            resource_type="organization_unit",
            resource_id=fixture["division"].id,
            target_org_unit_id=fixture["division"].id,
        )
        db.commit()

        assert result is None
        assert db.get(OrganizationUnit, fixture["division"].id).name == "科技创新与研发部"


def _session() -> Session:
    """创建加载完整模型的独占内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _fixture(db: Session) -> dict[str, object]:
    """创建单根组织、租户管理员和只拥有部门子树 audit.read 的审计员。"""

    db.add(Tenant(id="tenant_a", name="软件研究院"))
    owner = User(
        id="owner",
        tenant_id="tenant_a",
        username="owner",
        password_hash="test",
        role="admin",
    )
    auditor = User(
        id="auditor",
        tenant_id="tenant_a",
        username="auditor",
        password_hash="test",
    )
    db.add(owner)
    db.add(auditor)
    root = OrganizationUnit(
        id="org_root",
        tenant_id="tenant_a",
        code="ROOT",
        name="软件研究院",
        unit_type_code="company",
        tree_path="org_root",
        depth=0,
        is_root=True,
        root_tenant_id="tenant_a",
    )
    division = OrganizationUnit(
        id="org_division",
        tenant_id="tenant_a",
        parent_id=root.id,
        code="DIVISION",
        name="科技创新部",
        unit_type_code="department",
        tree_path="org_root/org_division",
        depth=1,
    )
    department = OrganizationUnit(
        id="org_department",
        tenant_id="tenant_a",
        parent_id=division.id,
        code="DEPARTMENT",
        name="政企项目集",
        unit_type_code="project",
        tree_path="org_root/org_division/org_department",
        depth=2,
    )
    sibling = OrganizationUnit(
        id="org_sibling",
        tenant_id="tenant_a",
        parent_id=root.id,
        code="SIBLING",
        name="公众研发事业部",
        unit_type_code="department",
        tree_path="org_root/org_sibling",
        depth=1,
    )
    for organization in (root, division, department, sibling):
        db.add(organization)
    profile = EmployeeProfile(
        id="employee_auditor",
        tenant_id="tenant_a",
        user_id=auditor.id,
        employee_id="E-AUDITOR",
    )
    db.add(profile)
    db.commit()
    ensure_builtin_governance_catalog(db, "tenant_a")
    role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_a",
            BusinessRole.role_code == "governance_auditor",
        )
    ).one()
    db.add(
        EmployeeRoleAssignment(
            id="auditor_scope",
            tenant_id="tenant_a",
            employee_profile_id=profile.id,
            business_role_id=role.id,
            scope_type="org_unit",
            scope_id=division.id,
            include_descendants=True,
            granted_by_user_id=owner.id,
        )
    )
    db.commit()
    return {
        "owner": owner,
        "auditor": auditor,
        "root": root,
        "division": division,
        "department": department,
        "sibling": sibling,
    }


def _append_fixture_logs(db: Session) -> None:
    """追加部门、下级、兄弟和租户级四类审计样本。"""

    fixtures = (
        ("audit_division", "org_division", "organization.update"),
        ("audit_department", "org_department", "organization.update"),
        ("audit_sibling", "org_sibling", "organization.delete"),
        ("audit_tenant_only", None, "tenant.update"),
    )
    for audit_id, organization_id, action in fixtures:
        append_management_audit(
            db,
            audit_id=audit_id,
            tenant_id="tenant_a",
            actor_user_id="owner",
            actor_display_name="平台管理员",
            action=action,
            action_kind="update",
            outcome="success",
            resource_type="organization_unit",
            resource_id=organization_id or "tenant_a",
            target_org_unit_id=organization_id,
            permission_code="organization.manage",
            permission_source="platform_admin_compat",
            detail={"token": "secret", "summary": organization_id or "tenant"},
        )
    db.commit()
