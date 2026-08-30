"""
@Time       : 2026/08/29 16:35
@Author     : zhanglp8181
@File       : test_builtin_skill_catalog_api.py
@CallChain  : 内置 Skill 目录 API → 权限过滤/详情/导入命令 → 数据库候选
@Description: 验证管理员候选可见、普通成员隐藏和固定快照导入 HTTP 契约。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings
from app.api.general_skill_catalog import (
    get_builtin_skill_catalog_detail,
    import_external_skill_catalog,
    import_builtin_skill_catalog,
    list_builtin_skill_catalog,
    review_builtin_skill_catalog,
)
from app.db.models import AgentProfile, GeneralSkill, GeneralSkillRevision, Tenant, User
from app.general_skills.builtin_catalog import BuiltinSkillCatalogService
from app.general_skills.builtin_schema import (
    BuiltinSkillCatalogImportRequest,
    BuiltinSkillCatalogReviewItem,
    BuiltinSkillCatalogReviewRequest,
    ExternalSkillCatalogImportRequest,
)
from app.general_skills.remote_source import RemoteFetchResult


def _api_db() -> Session:
    """创建包含管理员和普通成员的隔离目录 API 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_catalog_api", name="Catalog API tenant"))
    db.add_all(
        [
            User(
                id="catalog_api_admin",
                tenant_id="tenant_catalog_api",
                username="catalog-admin",
                role="admin",
                password_hash="unused",
            ),
            User(
                id="catalog_api_member",
                tenant_id="tenant_catalog_api",
                username="catalog-member",
                role="member",
                password_hash="unused",
            ),
        ]
    )
    db.commit()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_catalog_api",
        command_id="catalog-api-initial",
        actor_user_id="catalog_api_admin",
    )
    return db


def test_catalog_api_shows_pending_candidates_only_to_admin() -> None:
    """验证管理员能查看 37 条待审候选，而普通成员看不到未发布目录。"""

    db = _api_db()
    admin = db.get(User, "catalog_api_admin")
    member = db.get(User, "catalog_api_member")
    assert admin is not None and member is not None

    admin_page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=100,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status="draft",
        db=db,
        current_user=admin,
    )
    member_page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=100,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
        db=db,
        current_user=member,
    )

    assert admin_page.total == 37
    assert len(admin_page.items) == 37
    assert {item.review_status for item in admin_page.items} == {"pending"}
    assert {item.runtime_mode for item in admin_page.items} == {"guidance_only"}
    assert {item.source_kind for item in admin_page.items} == {"platform_builtin"}
    source_filtered_page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=100,
        search=None,
        category=None,
        source_kind="platform_builtin",
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status="draft",
        db=db,
        current_user=admin,
    )
    assert source_filtered_page.total == 37
    assert member_page.total == 0
    assert member_page.items == []


def test_catalog_api_detail_and_import_replay_return_source_evidence() -> None:
    """验证详情页返回文件摘要，重放导入命令不重复创建候选。"""

    db = _api_db()
    admin = db.get(User, "catalog_api_admin")
    assert admin is not None
    first = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=1,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
        db=db,
        current_user=admin,
    )
    item = first.items[0]
    detail = get_builtin_skill_catalog_detail(
        item.slug,
        tenant_id="tenant_catalog_api",
        db=db,
        current_user=admin,
    )
    replay = import_builtin_skill_catalog(
        BuiltinSkillCatalogImportRequest(
            tenant_id="tenant_catalog_api",
            command_id="catalog-api-initial",
        ),
        db=db,
        current_user=admin,
    )

    assert detail.id == item.id
    assert detail.revision_id == item.revision_id
    assert detail.source_package_checksum == item.source_package_checksum
    assert detail.resources
    assert any(resource.relative_path == "SKILL.md" for resource in detail.resources)
    assert replay.replayed is True
    assert replay.created_count == 37
    assert replay.existing_count == 0
    assert replay.source_package_checksum == item.source_package_checksum


def test_catalog_api_rejects_cross_tenant_and_member_detail_access() -> None:
    """验证租户边界和普通成员访问未发布详情均 fail-closed。"""

    db = _api_db()
    admin = db.get(User, "catalog_api_admin")
    member = db.get(User, "catalog_api_member")
    assert admin is not None and member is not None
    page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=1,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
        db=db,
        current_user=admin,
    )
    slug = page.items[0].slug

    with pytest.raises(HTTPException) as cross_tenant:
        list_builtin_skill_catalog(
            tenant_id="another_tenant",
            page=1,
            page_size=20,
            search=None,
            category=None,
            stability=None,
            risk_level=None,
            invocation_policy=None,
            status=None,
            db=db,
            current_user=admin,
        )
    assert cross_tenant.value.status_code == 404

    with pytest.raises(HTTPException) as member_detail:
        get_builtin_skill_catalog_detail(
            slug,
            tenant_id="tenant_catalog_api",
            db=db,
            current_user=member,
        )
    assert member_detail.value.status_code == 404


def test_catalog_api_external_import_is_admin_only_and_replayable(tmp_path: Path) -> None:
    """验证管理员外部导入只生成候选，重放不重复访问来源或创建记录。"""

    db = _api_db()
    admin = db.get(User, "catalog_api_admin")
    member = db.get(User, "catalog_api_member")
    assert admin is not None and member is not None
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "repo-sha/skills/engineering/release/SKILL.md",
            "---\nname: external-release\ndescription: 外部发布检查。\n---\n# Check\n",
        )
    revision = "b" * 40

    class Fetcher:
        """返回一个固定外部归档，测试 API 不访问真实网络。"""

        calls = 0

        def fetch(
            self,
            source_url: str,
            *,
            allowed_hosts: frozenset[str] | None = None,
            authorization: str | None = None,
            authorization_hosts: frozenset[str] | None = None,
        ) -> RemoteFetchResult:
            """验证 API 使用 GitHub 固定 commit 归档。"""

            del authorization, authorization_hosts
            self.calls += 1
            assert source_url.endswith(f"/archive/{revision}.zip")
            assert allowed_hosts
            return RemoteFetchResult(source_url, payload.getvalue(), 0)

    fetcher = Fetcher()
    request = ExternalSkillCatalogImportRequest(
        tenant_id="tenant_catalog_api",
        command_id="external-api-command-1",
        source_kind="github",
        source_url="https://github.com/example/external-skills",
        source_license="MIT",
        revision=revision,
        source_subpath="skills",
    )
    settings = Settings(
        public_mock_api_key="test-key",
        general_skill_object_store_path=str(tmp_path / "objects"),
    )
    first = import_external_skill_catalog(
        request,
        db=db,
        current_user=admin,
        settings=settings,
        fetcher=fetcher,
    )
    replay = import_external_skill_catalog(
        request,
        db=db,
        current_user=admin,
        settings=settings,
        fetcher=fetcher,
    )

    assert first.created_count == 1
    assert first.source_kind == "platform_external"
    assert first.source_url == "https://github.com/example/external-skills"
    assert first.replayed is False
    assert replay.replayed is True
    assert fetcher.calls == 1

    with pytest.raises(HTTPException) as member_error:
        import_external_skill_catalog(
            request,
            db=db,
            current_user=member,
            settings=settings,
            fetcher=fetcher,
        )
    assert member_error.value.status_code == 403


def test_catalog_api_review_publishes_platform_candidate_to_all_member_galleries() -> None:
    """验证平台目录审核后各租户成员都可发现，但仍须单独绑定到 Agent 才能运行。"""

    db = _api_db()
    admin = db.get(User, "catalog_api_admin")
    member = db.get(User, "catalog_api_member")
    assert admin is not None and member is not None
    db.add(
        AgentProfile(
            id="catalog_api_overall",
            tenant_id="tenant_catalog_api",
            name="API 目录总览",
            is_overall=True,
            status="active",
        )
    )
    db.add(Tenant(id="tenant_catalog_api_second", name="Second catalog API tenant"))
    db.add(
        User(
            id="catalog_api_second_member",
            tenant_id="tenant_catalog_api_second",
            username="second-catalog-member",
            role="member",
            password_hash="unused",
        )
    )
    db.commit()
    candidate = db.exec(
        select(GeneralSkill).where(GeneralSkill.catalog_scope == "platform")
    ).first()
    assert candidate is not None
    revision = db.exec(
        select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == candidate.id)
    ).one()
    request = BuiltinSkillCatalogReviewRequest(
        tenant_id="tenant_catalog_api",
        command_id="catalog-api-review-1",
        items=[
            BuiltinSkillCatalogReviewItem(
                skill_id=candidate.id,
                decision="approve",
                expected_skill_row_version=candidate.row_version,
                expected_revision_row_version=revision.row_version,
            )
        ],
    )
    result = review_builtin_skill_catalog(request, db=db, current_user=admin)
    assert result.approved_count == 1
    assert result.replayed is False
    member_page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api",
        page=1,
        page_size=20,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
        db=db,
        current_user=member,
    )
    assert member_page.total == 1
    assert member_page.items[0].id == candidate.id
    second_member = db.get(User, "catalog_api_second_member")
    assert second_member is not None
    second_member_page = list_builtin_skill_catalog(
        tenant_id="tenant_catalog_api_second",
        page=1,
        page_size=20,
        search=None,
        category=None,
        stability=None,
        risk_level=None,
        invocation_policy=None,
        status=None,
        db=db,
        current_user=second_member,
    )
    assert second_member_page.total == 1
    assert second_member_page.items[0].id == candidate.id
