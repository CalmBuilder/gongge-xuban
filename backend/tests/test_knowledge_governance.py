"""
@Time       : 2026/07/28 23:35
@Author     : zhanglp8181
@File       : test_knowledge_governance.py
@CallChain  : pytest → Knowledge management API/governance service → SQLite SQLModel
@Description: 验证知识库默认最小权限、组织范围校验、乐观锁和租户隔离。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.knowledge_bases import (
    create_knowledge_base,
    delete_knowledge_base,
    list_knowledge_bases,
    update_knowledge_governance,
)
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseOrgAccess,
    ManagementAuditLog,
    OrganizationUnit,
    Tenant,
    User,
)
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseGovernanceUpdateRequest,
    KnowledgeBaseOrgAccessInput,
)


def test_create_knowledge_base_defaults_to_current_owner_and_restricted_download() -> None:
    """验证新知识库不会因进入开放资源管理分支而默认向全租户开放正文。"""

    with _session() as db:
        admin = _seed_tenant(db, "tenant_demo", "admin")

        result = create_knowledge_base(
            KnowledgeBaseCreateRequest(
                tenant_id="tenant_demo",
                name="研究院制度库",
            ),
            agent_id=None,
            db=db,
            current_user=admin,
        )

        row = db.get(KnowledgeBase, result.id)
        assert row is not None
        assert result.owner_user_id == admin.id
        assert result.access_scope == "owner"
        assert result.download_policy == "restricted"
        assert result.revision == 1
        assert result.organization_access == []


def test_update_knowledge_governance_saves_org_roots_and_rejects_stale_revision() -> None:
    """验证组织访问根规范化保存，并用 revision 防止覆盖并发更新。"""

    with _session() as db:
        admin = _seed_tenant(db, "tenant_demo", "admin")
        root, child = _seed_organization(db, "tenant_demo")
        row = KnowledgeBase(
            id="kb_project",
            tenant_id="tenant_demo",
            name="政企研发资料库",
            owner_user_id=admin.id,
        )
        db.add(row)
        db.commit()

        result = update_knowledge_governance(
            row.id,
            KnowledgeBaseGovernanceUpdateRequest(
                tenant_id="tenant_demo",
                expected_revision=1,
                responsible_org_unit_id=root.id,
                access_scope="organization",
                download_policy="restricted",
                organization_access=[
                    KnowledgeBaseOrgAccessInput(
                        org_unit_id=child.id,
                        include_descendants=True,
                    )
                ],
            ),
            db=db,
            current_user=admin,
        )

        assert result.revision == 2
        assert result.responsible_org_unit_id == root.id
        assert result.access_scope == "organization"
        assert [item.org_unit_id for item in result.organization_access] == [child.id]
        saved = db.exec(
            select(KnowledgeBaseOrgAccess).where(
                KnowledgeBaseOrgAccess.knowledge_base_id == row.id
            )
        ).one()
        assert saved.include_descendants is True
        assert saved.status == "active"

        with pytest.raises(HTTPException) as caught:
            update_knowledge_governance(
                row.id,
                KnowledgeBaseGovernanceUpdateRequest(
                    tenant_id="tenant_demo",
                    expected_revision=1,
                    access_scope="owner",
                    download_policy="restricted",
                ),
                db=db,
                current_user=admin,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "KNOWLEDGE_BASE_REVISION_CONFLICT"
        audit_rows = db.exec(
            select(ManagementAuditLog)
            .where(ManagementAuditLog.resource_id == row.id)
            .order_by(ManagementAuditLog.created_at)
        ).all()
        assert [audit.outcome for audit in audit_rows] == ["success", "failure"]
        assert audit_rows[0].before_json["revision"] == 1
        assert audit_rows[0].after_json["revision"] == 2
        assert (
            audit_rows[1].detail_json["reason"]
            == "KNOWLEDGE_BASE_REVISION_CONFLICT"
        )


def test_knowledge_governance_rejects_cross_tenant_and_inconsistent_org_scope() -> None:
    """验证跨租户组织及非 organization 范围夹带组织根均被拒绝且不改变 revision。"""

    with _session() as db:
        admin = _seed_tenant(db, "tenant_demo", "admin")
        _seed_organization(db, "tenant_demo")
        _seed_tenant(db, "tenant_other", "other_admin")
        other_root, _ = _seed_organization(db, "tenant_other")
        row = KnowledgeBase(
            id="kb_private",
            tenant_id="tenant_demo",
            name="私有资料库",
            owner_user_id=admin.id,
        )
        db.add(row)
        db.commit()

        for request in (
            KnowledgeBaseGovernanceUpdateRequest(
                tenant_id="tenant_demo",
                expected_revision=1,
                access_scope="organization",
                download_policy="restricted",
                organization_access=[
                    KnowledgeBaseOrgAccessInput(org_unit_id=other_root.id)
                ],
            ),
            KnowledgeBaseGovernanceUpdateRequest(
                tenant_id="tenant_demo",
                expected_revision=1,
                access_scope="owner",
                download_policy="restricted",
                organization_access=[
                    KnowledgeBaseOrgAccessInput(org_unit_id=other_root.id)
                ],
            ),
        ):
            with pytest.raises(HTTPException) as caught:
                update_knowledge_governance(
                    row.id,
                    request,
                    db=db,
                    current_user=admin,
                )
            assert caught.value.status_code == 400

        db.refresh(row)
        assert row.revision == 1
        assert row.access_scope == "owner"
        assert db.exec(select(KnowledgeBaseOrgAccess)).all() == []


def test_delete_knowledge_base_removes_governance_roots() -> None:
    """验证删除临时知识库时不会留下失去父资源的组织访问事实。"""

    with _session() as db:
        admin = _seed_tenant(db, "tenant_demo", "admin")
        _, child = _seed_organization(db, "tenant_demo")
        row = KnowledgeBase(
            id="kb_delete",
            tenant_id="tenant_demo",
            name="待删除知识库",
            owner_user_id=admin.id,
        )
        db.add(row)
        db.add(
            KnowledgeBaseOrgAccess(
                id="access_delete",
                tenant_id="tenant_demo",
                knowledge_base_id=row.id,
                org_unit_id=child.id,
            )
        )
        db.commit()

        result = delete_knowledge_base(
            row.id,
            tenant_id="tenant_demo",
            agent_id=None,
            db=db,
            current_user=admin,
        )

        assert result == {"status": "deleted"}
        assert db.get(KnowledgeBase, row.id) is None
        assert db.exec(select(KnowledgeBaseOrgAccess)).all() == []


def test_governance_view_does_not_grant_content_access() -> None:
    """治理管理员可看治理元数据，但未归属访问组织时不能进入正文列表。"""

    with _session() as db:
        admin = _seed_tenant(db, "tenant_demo", "admin")
        _, project = _seed_organization(db, "tenant_demo")
        row = KnowledgeBase(
            id="kb_governance_only",
            tenant_id="tenant_demo",
            name="政企研发资料库",
            owner_user_id=admin.id,
            access_scope="organization",
        )
        db.add(row)
        db.add(
            KnowledgeBaseOrgAccess(
                id="access_governance_only",
                tenant_id="tenant_demo",
                knowledge_base_id=row.id,
                org_unit_id=project.id,
            )
        )
        db.commit()

        governance_rows = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=None,
            governance_view=True,
            db=db,
            current_user=admin,
        )
        content_rows = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=None,
            governance_view=False,
            db=db,
            current_user=admin,
        )

        assert [item.id for item in governance_rows] == [row.id]
        assert governance_rows[0].content_access_allowed is False
        assert governance_rows[0].content_access_reason == "organization_mismatch"
        assert content_rows == []


def _session() -> Session:
    """创建每个测试独占的内存 SQLite 会话并加载完整模型。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_tenant(db: Session, tenant_id: str, username: str) -> User:
    """创建租户和兼容平台管理员，使测试聚焦知识治理契约。"""

    db.add(Tenant(id=tenant_id, name=tenant_id))
    user = User(
        id=f"user_{username}",
        tenant_id=tenant_id,
        username=username,
        display_name=username,
        role="admin",
        password_hash="test",
    )
    db.add(user)
    db.commit()
    return user


def _seed_organization(
    db: Session,
    tenant_id: str,
) -> tuple[OrganizationUnit, OrganizationUnit]:
    """创建单根与一层业务组织，路径结构与真实公司样本一致。"""

    root = OrganizationUnit(
        id=f"org_{tenant_id}_root",
        tenant_id=tenant_id,
        code="ROOT",
        name="软件研究院",
        unit_type_code="company",
        tree_path=f"org_{tenant_id}_root",
        depth=0,
        is_root=True,
        root_tenant_id=tenant_id,
    )
    child = OrganizationUnit(
        id=f"org_{tenant_id}_project",
        tenant_id=tenant_id,
        parent_id=root.id,
        code="PROJECT",
        name="政企项目集",
        unit_type_code="department",
        tree_path=f"{root.id}/org_{tenant_id}_project",
        depth=1,
    )
    db.add(root)
    db.add(child)
    db.commit()
    return root, child
