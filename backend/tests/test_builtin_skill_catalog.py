"""
@Time       : 2026/08/29 15:10
@Author     : zhanglp8181
@File       : test_builtin_skill_catalog.py
@CallChain  : pytest → BuiltinSkillCatalog → 固定 Skill fixture → 候选审核投影
@Description: 验证项目内置 Skill 快照的数量、来源、checksum、风险和 guidance-only 门禁。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    BusinessRole,
    EmployeeProfile,
    GeneralSkill,
    GeneralSkillCatalogCommand,
    GeneralSkillRevision,
    ManagementAuditLog,
    OrganizationUnit,
    PublicationRelease,
    Tenant,
    User,
)
from app.general_skills.builtin_catalog import (
    BUILTIN_SKILL_EXPECTED_COUNT,
    BUILTIN_SKILL_INITIAL_IMPORT_COMMAND_ID,
    BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM,
    BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM,
    BUILTIN_SKILL_SOURCE_LICENSE,
    BUILTIN_SKILL_SOURCE_REPOSITORY,
    BUILTIN_SKILL_SOURCE_REVISION,
    BuiltinSkillCatalogError,
    BuiltinSkillCatalogImportError,
    BuiltinSkillCatalogService,
    load_builtin_skill_catalog,
    reconcile_builtin_skill_catalogs,
)
from app.general_skills.catalog_governance import (
    CatalogGovernanceError,
    GeneralSkillCatalogGovernanceService,
)
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetchResult


def test_builtin_fixture_is_a_deterministic_pending_catalog() -> None:
    """验证固定 fixture 生成 37 条唯一、待审核且不可直接执行的候选。"""

    first = load_builtin_skill_catalog()
    second = load_builtin_skill_catalog()

    assert len(first.items) == BUILTIN_SKILL_EXPECTED_COUNT
    assert first.report_json() == second.report_json()
    assert first.source_repository == BUILTIN_SKILL_SOURCE_REPOSITORY
    assert first.source_revision == BUILTIN_SKILL_SOURCE_REVISION
    assert first.source_license == BUILTIN_SKILL_SOURCE_LICENSE
    assert first.source_package_checksum == BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM
    assert first.source_normalized_checksum == BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM
    assert len({item.catalog_key for item in first.items}) == BUILTIN_SKILL_EXPECTED_COUNT

    for item in first.items:
        assert item.catalog_key == (
            f"platform_builtin:{BUILTIN_SKILL_SOURCE_REVISION}:{item.source_path}"
        )
        assert item.source_path.startswith("skills/")
        assert item.source_license == "MIT"
        assert item.review_status == "pending"
        assert item.runtime_mode == "guidance_only"
        assert item.files
        assert any(file.relative_path == "SKILL.md" for file in item.files)
        assert item.skill_markdown.startswith("---\n")
        assert item.metadata_json(import_batch_id="batch_a")["managed_catalog"] is True
        manifest = [file.as_resource_manifest() for file in item.files]
        assert {file["relative_path"] for file in manifest} == {
            file.relative_path for file in item.files
        }
        assert item.report_json()["resource_count"] == len(item.files)

    assert any(item.risk_level == "high" for item in first.items)
    assert any(item.risk_level == "medium" for item in first.items)


def test_builtin_catalog_does_not_depend_on_external_source_directory() -> None:
    """验证默认解析路径固定在应用资源目录，而不是读取工作区外部来源目录。"""

    catalog_module = Path(__file__).resolve().parents[1] / "app" / "general_skills" / "builtin_catalog.py"
    source = catalog_module.read_text(encoding="utf-8")

    assert "otherpro/skills" not in source
    assert "github.com/mattpocock/skills" in source
    assert "resource_dir()" in source


def test_builtin_catalog_rejects_changed_fixture_bytes() -> None:
    """验证固定快照发生任何字节变化时不会静默降级为新版本。"""

    fixture = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "db"
        / "seed_fixtures"
        / "otherpro_skills_catalog_6654f6b6.zip"
    ).read_bytes()

    with pytest.raises(BuiltinSkillCatalogError, match="checksum"):
        load_builtin_skill_catalog(payload=fixture + b"changed")


def _catalog_db() -> Session:
    """创建隔离 SQLite 会话，覆盖命令回执、Skill 修订和审计表。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_builtin", name="Built-in catalog tenant"))
    db.add(
        User(
            id="admin_builtin",
            tenant_id="tenant_builtin",
            username="catalog-admin",
            role="admin",
            password_hash="unused",
        )
    )
    db.commit()
    return db


def test_builtin_catalog_service_is_idempotent_and_keeps_candidates_unbound() -> None:
    """验证项目级首次入库、跨请求重放和候选无发布/无绑定事实。"""

    db = _catalog_db()
    service = BuiltinSkillCatalogService(db)

    first = service.import_snapshot(
        tenant_id="tenant_builtin",
        command_id="builtin-initial-1",
        actor_user_id="admin_builtin",
    )
    second = service.import_snapshot(
        tenant_id="tenant_builtin",
        command_id="builtin-initial-1",
        actor_user_id="admin_builtin",
    )

    assert first.replayed is False
    assert first.created_count == BUILTIN_SKILL_EXPECTED_COUNT
    assert first.existing_count == 0
    assert second.replayed is True
    assert second.as_dict() | {"replayed": False} == first.as_dict()

    skills = db.exec(select(GeneralSkill).where(GeneralSkill.catalog_scope == "platform")).all()
    revisions = db.exec(
        select(GeneralSkillRevision).where(GeneralSkillRevision.catalog_scope == "platform")
    ).all()
    bindings = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == "tenant_builtin")
    ).all()
    commands = db.exec(
        select(GeneralSkillCatalogCommand).where(
            GeneralSkillCatalogCommand.catalog_scope == "platform"
        )
    ).all()
    audits = db.exec(
        select(ManagementAuditLog).where(ManagementAuditLog.tenant_id == "tenant_builtin")
    ).all()

    assert len(skills) == BUILTIN_SKILL_EXPECTED_COUNT
    assert len(revisions) == BUILTIN_SKILL_EXPECTED_COUNT
    assert not bindings
    assert len(commands) == 1
    assert len(audits) == 1
    assert all(skill.tenant_id is None for skill in skills)
    assert all(skill.catalog_scope == "platform" for skill in skills)
    assert all(skill.visibility_scope == "platform_gallery" for skill in skills)
    assert all(skill.catalog_key for skill in skills)
    assert commands[0].tenant_id is None
    assert commands[0].scope_key == "platform"
    assert all(skill.status == "draft" for skill in skills)
    assert all(skill.owner_user_id is None for skill in skills)
    assert all(skill.current_published_revision_id is None for skill in skills)
    assert all(revision.status == "draft" for revision in revisions)


def test_builtin_catalog_initial_command_id_is_stable_for_seed_replays() -> None:
    """验证演示租户初始化使用固定命令号，重启不会产生第二个导入批次。"""

    assert BUILTIN_SKILL_INITIAL_IMPORT_COMMAND_ID == "builtin-skill-initial-6654f6b6"


def test_builtin_catalog_reconciliation_is_project_scoped_not_tenant_provisioning() -> None:
    """验证启动对账只导入一次项目目录，不为每个租户复制 Skill 主体。"""

    db = _catalog_db()
    db.add(Tenant(id="tenant_second", name="Second catalog tenant"))
    db.add(
        User(
            id="admin_second",
            tenant_id="tenant_second",
            username="second-admin",
            role="admin",
            password_hash="unused",
        )
    )
    db.add(Tenant(id="tenant_without_admin", name="Pending catalog tenant"))
    db.commit()

    outcomes = reconcile_builtin_skill_catalogs(db)

    assert len(outcomes) == 1
    assert outcomes[0]["catalog_scope"] == "platform"
    assert outcomes[0]["status"] == "imported"
    assert outcomes[0]["operator_tenant_id"] == "tenant_builtin"
    assert len(db.exec(select(GeneralSkill)).all()) == BUILTIN_SKILL_EXPECTED_COUNT
    assert len(
        db.exec(select(GeneralSkill).where(GeneralSkill.catalog_scope == "platform")).all()
    ) == BUILTIN_SKILL_EXPECTED_COUNT
    assert not db.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == "tenant_without_admin")
    ).all()


def test_builtin_catalog_service_rejects_non_admin_and_snapshot_conflict() -> None:
    """验证租户管理员门禁和来源键内容冲突均 fail-closed。"""

    db = _catalog_db()
    db.add(
        User(
            id="member_builtin",
            tenant_id="tenant_builtin",
            username="catalog-member",
            role="member",
            password_hash="unused",
        )
    )
    db.commit()
    service = BuiltinSkillCatalogService(db)

    with pytest.raises(BuiltinSkillCatalogImportError, match="administrator"):
        service.import_snapshot(
            tenant_id="tenant_builtin",
            command_id="builtin-member-1",
            actor_user_id="member_builtin",
        )

    item = load_builtin_skill_catalog().items[0]
    db.add(
        GeneralSkill(
            id="conflicting_builtin_skill",
            tenant_id=None,
            catalog_scope="platform",
            catalog_key=item.catalog_key,
            slug="conflicting-builtin-skill",
            name=item.name,
            skill_markdown=item.skill_markdown,
            metadata_json={
                "managed_catalog": True,
                "catalog_key": item.catalog_key,
                "content_checksum": "different",
            },
            status="draft",
            usage_mode="planning_guidance",
            visibility_scope="platform_gallery",
        )
    )
    db.add(
        GeneralSkillRevision(
            id="conflicting_builtin_revision",
            tenant_id=None,
            catalog_scope="platform",
            skill_id="conflicting_builtin_skill",
            revision_number=1,
            content_checksum="d" * 64,
            manifest_checksum="e" * 64,
            normalized_skill_markdown=item.skill_markdown,
            status="draft",
            created_by="admin_builtin",
        )
    )
    db.commit()

    with pytest.raises(BuiltinSkillCatalogImportError, match="another content checksum"):
        service.import_snapshot(
            tenant_id="tenant_builtin",
            command_id="builtin-conflict-1",
            actor_user_id="admin_builtin",
        )
    assert not db.exec(
        select(GeneralSkillCatalogCommand).where(
            GeneralSkillCatalogCommand.command_id == "builtin-conflict-1"
        )
    ).first()


def test_external_catalog_import_is_project_owned_pending_and_replayable(tmp_path: Path) -> None:
    """验证固定 GitHub 外部包只生成项目候选，不直接发布、绑定或执行。"""

    from io import BytesIO
    from zipfile import ZipFile

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "repo-sha/skills/engineering/release/SKILL.md",
            "---\nname: release-check\ndescription: 审查发布清单。\n---\n# Release\n",
        )
        archive.writestr(
            "repo-sha/skills/engineering/release/checklist.txt",
            "确认回滚、监控和审批记录。\n",
        )
    package = payload.getvalue()
    revision = "a" * 40

    class Fetcher:
        """返回固定归档并证明服务按 GitHub commit 构造不可漂移 URL。"""

        calls = 0

        def fetch(
            self,
            source_url: str,
            *,
            allowed_hosts: frozenset[str] | None = None,
            authorization: str | None = None,
            authorization_hosts: frozenset[str] | None = None,
        ) -> RemoteFetchResult:
            """返回测试归档，不访问真实外网。"""

            del authorization, authorization_hosts
            self.calls += 1
            assert source_url.endswith(f"/archive/{revision}.zip")
            assert allowed_hosts
            return RemoteFetchResult(source_url, package, 0)

    db = _catalog_db()
    fetcher = Fetcher()
    result = BuiltinSkillCatalogService(db).import_external(
        tenant_id="tenant_builtin",
        command_id="external-catalog-1",
        actor_user_id="admin_builtin",
        source_kind="github",
        source_url="https://github.com/example/release-skills",
        source_license="MIT",
        revision=revision,
        source_subpath="skills",
        fetcher=fetcher,
        https_allowed_hosts=frozenset(),
        object_store=FileSystemSkillObjectStore(tmp_path / "objects"),
    )
    replay = BuiltinSkillCatalogService(db).import_external(
        tenant_id="tenant_builtin",
        command_id="external-catalog-1",
        actor_user_id="admin_builtin",
        source_kind="github",
        source_url="https://github.com/example/release-skills",
        source_license="MIT",
        revision=revision,
        source_subpath="skills",
        fetcher=fetcher,
        https_allowed_hosts=frozenset(),
        object_store=FileSystemSkillObjectStore(tmp_path / "objects"),
    )

    assert result.created_count == 1
    assert result.source_kind == "platform_external"
    assert result.source_repository == "https://github.com/example/release-skills"
    assert result.source_revision == revision
    assert result.replayed is False
    assert replay.replayed is True
    assert fetcher.calls == 1
    skill = db.exec(select(GeneralSkill).where(GeneralSkill.catalog_scope == "platform")).one()
    revision_row = db.exec(
        select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)
    ).one()
    metadata = skill.metadata_json
    assert metadata["managed_catalog"] is True
    assert metadata["source_kind"] == "github"
    assert metadata["source_revision"] == revision
    assert metadata["review_status"] == "pending"
    assert skill.status == "draft"
    assert skill.owner_user_id is None
    assert skill.current_published_revision_id is None
    assert revision_row.status == "draft"
    assert revision_row.resource_manifest_json[0]["object_key"].startswith("sha256:")
    assert revision_row.resource_manifest_json[0]["path"]
    assert not db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.resource_id == skill.id)
    ).all()


def test_external_catalog_import_rejects_unconfigured_https_source() -> None:
    """验证未配置 HTTPS 主机白名单时管理员外部导入 fail-closed。"""

    db = _catalog_db()

    with pytest.raises(BuiltinSkillCatalogError, match="not configured"):
        from app.general_skills.builtin_catalog import _external_source_descriptor

        _external_source_descriptor(
            source_kind="https",
            source_url="https://example.com/skill.zip",
            revision=None,
            source_subpath=None,
            source_license="MIT",
            https_allowed_hosts=frozenset(),
        )
    db.close()


def test_catalog_review_is_atomic_replayable_and_publishes_gallery_binding() -> None:
    """验证批量审核的双 CAS、审计、回放和普通成员广场发布事实。"""

    db = _catalog_db()
    db.add(
        User(
            id="member_catalog_review",
            tenant_id="tenant_builtin",
            username="catalog-member-review",
            role="member",
            password_hash="unused",
        )
    )
    db.add(
        AgentProfile(
            id="overall_catalog_review",
            tenant_id="tenant_builtin",
            name="目录总览",
            is_overall=True,
            status="active",
        )
    )
    db.commit()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_builtin",
        command_id="review-fixture-import",
        actor_user_id="admin_builtin",
    )
    skill = db.exec(
        select(GeneralSkill).where(GeneralSkill.catalog_scope == "platform")
    ).first()
    assert skill is not None
    revision = db.get(GeneralSkillRevision, skill.current_published_revision_id or "")
    assert revision is None
    revision = db.exec(
        select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)
    ).one()

    service = GeneralSkillCatalogGovernanceService(db)
    result = service.review(
        tenant_id="tenant_builtin",
        command_id="catalog-review-approve-1",
        actor_user_id="admin_builtin",
        items=[
            {
                "skill_id": skill.id,
                "decision": "approve",
                "expected_skill_row_version": skill.row_version,
                "expected_revision_row_version": revision.row_version,
                "review_note": "来源、风险和内容 checksum 已复核",
            }
        ],
    )
    replay = service.review(
        tenant_id="tenant_builtin",
        command_id="catalog-review-approve-1",
        actor_user_id="admin_builtin",
        items=[
            {
                "skill_id": skill.id,
                "decision": "approve",
                "expected_skill_row_version": 1,
                "expected_revision_row_version": 1,
                "review_note": "来源、风险和内容 checksum 已复核",
            }
        ],
    )

    assert result.replayed is False
    assert result.approved_count == 1
    assert replay.replayed is True
    db.expire_all()
    skill = db.get(GeneralSkill, skill.id)
    assert skill is not None
    assert skill.status == "published"
    assert skill.current_published_revision_id == revision.id
    assert skill.metadata_json["review_status"] == "approved"
    revision = db.get(GeneralSkillRevision, revision.id)
    assert revision is not None and revision.status == "published"
    assert not db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.resource_id == skill.id)
    ).all()
    assert db.exec(
        select(ManagementAuditLog).where(
            ManagementAuditLog.action == "general_skill.catalog.review"
        )
    ).all()


def test_catalog_review_preflight_failure_does_not_partially_publish() -> None:
    """验证同一批次第二项 CAS 冲突时第一项也保持待审核。"""

    db = _catalog_db()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_builtin",
        command_id="review-fixture-import-atomic",
        actor_user_id="admin_builtin",
    )
    skills = db.exec(
        select(GeneralSkill)
        .where(GeneralSkill.catalog_scope == "platform")
        .order_by(GeneralSkill.id)
    ).all()[:2]
    revisions = [
        db.exec(select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)).one()
        for skill in skills
    ]

    with pytest.raises(CatalogGovernanceError, match="changed before review"):
        GeneralSkillCatalogGovernanceService(db).review(
            tenant_id="tenant_builtin",
            command_id="catalog-review-atomic-fail",
            actor_user_id="admin_builtin",
            items=[
                {
                    "skill_id": skills[0].id,
                    "decision": "approve",
                    "expected_skill_row_version": skills[0].row_version,
                    "expected_revision_row_version": revisions[0].row_version,
                },
                {
                    "skill_id": skills[1].id,
                    "decision": "reject",
                    "expected_skill_row_version": skills[1].row_version + 1,
                    "expected_revision_row_version": revisions[1].row_version,
                },
            ],
        )
    db.expire_all()
    assert all(skill.status == "draft" for skill in db.exec(select(GeneralSkill)).all())
    assert not db.exec(
        select(GeneralSkillCatalogCommand).where(
            GeneralSkillCatalogCommand.command_id == "catalog-review-atomic-fail"
        )
    ).first()


def test_catalog_skill_install_uses_strict_catalog_metadata_and_shared_resolver() -> None:
    """验证能力分身显式安装项目 Skill 后使用同一 resolver，重复操作不新增绑定。"""

    db = _catalog_db()
    owner = User(
        id="catalog_avatar_owner",
        tenant_id="tenant_builtin",
        username="catalog-avatar-owner",
        role="member",
        password_hash="unused",
    )
    db.add(owner)
    db.add(
        AgentProfile(
            id="catalog_avatar_agent",
            tenant_id="tenant_builtin",
            name="目录能力分身",
            owner_user_id=owner.id,
            status="active",
        )
    )
    db.add(
        AgentProfile(
            id="overall_catalog_install",
            tenant_id="tenant_builtin",
            name="安装总览",
            is_overall=True,
            status="active",
        )
    )
    db.commit()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_builtin",
        command_id="install-fixture-import",
        actor_user_id="admin_builtin",
    )
    skill = db.exec(select(GeneralSkill)).first()
    assert skill is not None
    revision = db.exec(select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)).one()
    GeneralSkillCatalogGovernanceService(db).review(
        tenant_id="tenant_builtin",
        command_id="install-review-1",
        actor_user_id="admin_builtin",
        items=[
            {
                "skill_id": skill.id,
                "decision": "approve",
                "expected_skill_row_version": skill.row_version,
                "expected_revision_row_version": revision.row_version,
            }
        ],
    )
    db.expire_all()
    skill = db.get(GeneralSkill, skill.id)
    revision = db.get(GeneralSkillRevision, revision.id)
    assert skill is not None and revision is not None
    service = GeneralSkillCatalogGovernanceService(db)
    first = service.bind(
        current_user=owner,
        skill_id=skill.id,
        agent_id="catalog_avatar_agent",
        mode="install",
        revision_policy="pinned",
        pinned_revision_id=revision.id,
        invocation_policy="model_allowed",
    )
    second = service.bind(
        current_user=owner,
        skill_id=skill.id,
        agent_id="catalog_avatar_agent",
        mode="install",
        revision_policy="pinned",
        pinned_revision_id=revision.id,
        invocation_policy="model_allowed",
    )

    assert first.action == "created"
    assert second.action == "unchanged"
    assert first.binding.id == second.binding.id
    assert first.binding.metadata_json["managed_catalog"] is True
    assert first.binding.metadata_json["catalog_key"] == skill.metadata_json["catalog_key"]
    catalog = EffectiveGeneralSkillResolver(db).resolve(owner, "catalog_avatar_agent")
    assert [item.skill_id for item in catalog.items] == [skill.id]


def test_catalog_skill_bind_requires_organization_release_and_supports_admin_binding() -> None:
    """验证管理员绑定数字员工前必须具备组织、监督和 active Agent Release。"""

    db = _catalog_db()
    db.add(
        OrganizationUnit(
            id="org_catalog",
            tenant_id="tenant_builtin",
            code="catalog-org",
            name="Catalog organization",
            unit_type_code="department",
            tree_path="/catalog-org",
            is_root=True,
            root_tenant_id="tenant_builtin",
        )
    )
    db.add(
        BusinessRole(
            id="role_catalog",
            tenant_id="tenant_builtin",
            role_code="catalog.operator",
            name="Catalog operator",
        )
    )
    db.add(
        EmployeeProfile(
            id="employee_supervisor",
            tenant_id="tenant_builtin",
            user_id="admin_builtin",
            employee_id="CATALOG-SUPERVISOR",
            employee_name="Catalog supervisor",
        )
    )
    employee = AgentProfile(
        id="catalog_org_employee",
        tenant_id="tenant_builtin",
        name="目录组织数字员工",
        owner_user_id="admin_builtin",
        responsible_org_unit_id="org_catalog",
        status="active",
    )
    db.add(employee)
    db.add(
        AgentRoleBinding(
            id="catalog_org_role",
            tenant_id="tenant_builtin",
            agent_id=employee.id,
            business_role_id="role_catalog",
            supervisor_employee_profile_id="employee_supervisor",
            status="active",
        )
    )
    db.add(
        PublicationRelease(
            id="catalog_agent_release",
            tenant_id="tenant_builtin",
            approved_request_id="catalog-approved-request",
            resource_type="agent",
            resource_id=employee.id,
            snapshot_kind="agent",
            snapshot_id="catalog-agent-snapshot",
            snapshot_checksum="a" * 64,
            status="active",
        )
    )
    db.commit()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id="tenant_builtin",
        command_id="bind-fixture-import",
        actor_user_id="admin_builtin",
    )
    skill = db.exec(select(GeneralSkill)).first()
    assert skill is not None
    revision = db.exec(select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)).one()
    GeneralSkillCatalogGovernanceService(db).review(
        tenant_id="tenant_builtin",
        command_id="bind-review-1",
        actor_user_id="admin_builtin",
        items=[
            {
                "skill_id": skill.id,
                "decision": "approve",
                "expected_skill_row_version": skill.row_version,
                "expected_revision_row_version": revision.row_version,
            }
        ],
    )
    db.expire_all()
    skill = db.get(GeneralSkill, skill.id)
    revision = db.get(GeneralSkillRevision, revision.id)
    assert skill is not None and revision is not None
    result = GeneralSkillCatalogGovernanceService(db).bind(
        current_user=db.get(User, "admin_builtin"),
        skill_id=skill.id,
        agent_id=employee.id,
        mode="bind",
        revision_policy="pinned",
        pinned_revision_id=revision.id,
        invocation_policy="user_only",
    )
    assert result.action == "created"
    assert result.mode == "bind"
    assert result.binding.metadata_json["managed_catalog"] is True


def _prepare_published_catalog_skill(db: Session, agent_id: str) -> tuple[User, AgentProfile, GeneralSkill, GeneralSkillRevision]:
    """创建一个已审核、已绑定到能力分身的项目 Skill，供生命周期测试复用。"""

    owner = db.get(User, "admin_builtin")
    assert owner is not None
    db.add(
        AgentProfile(
            id=agent_id,
            tenant_id=owner.tenant_id,
            name=f"生命周期测试分身-{agent_id}",
            owner_user_id=owner.id,
            status="active",
        )
    )
    db.commit()
    BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id=owner.tenant_id,
        command_id=f"lifecycle-import-{agent_id}",
        actor_user_id=owner.id,
    )
    skill = db.exec(select(GeneralSkill)).first()
    assert skill is not None
    revision = db.exec(
        select(GeneralSkillRevision).where(GeneralSkillRevision.skill_id == skill.id)
    ).one()
    GeneralSkillCatalogGovernanceService(db).review(
        tenant_id=owner.tenant_id,
        command_id=f"lifecycle-review-{agent_id}",
        actor_user_id=owner.id,
        items=[
            {
                "skill_id": skill.id,
                "decision": "approve",
                "expected_skill_row_version": skill.row_version,
                "expected_revision_row_version": revision.row_version,
            }
        ],
    )
    db.expire_all()
    skill = db.get(GeneralSkill, skill.id)
    revision = db.get(GeneralSkillRevision, revision.id)
    assert skill is not None and revision is not None
    GeneralSkillCatalogGovernanceService(db).bind(
        current_user=owner,
        skill_id=skill.id,
        agent_id=agent_id,
        mode="install",
        revision_policy="pinned",
        pinned_revision_id=revision.id,
        invocation_policy="model_allowed",
    )
    db.expire_all()
    skill = db.get(GeneralSkill, skill.id)
    revision = db.get(GeneralSkillRevision, revision.id)
    agent = db.get(AgentProfile, agent_id)
    assert skill is not None and revision is not None and agent is not None
    return owner, agent, skill, revision


def test_catalog_lifecycle_archive_stops_new_adoption_but_keeps_existing_binding() -> None:
    """验证普通下架只停止新采用，既有固定绑定仍能通过统一 resolver 使用。"""

    db = _catalog_db()
    owner, agent, skill, revision = _prepare_published_catalog_skill(db, "catalog_archive_agent")
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_id == skill.id,
        )
    ).one()
    before_skill_version = skill.row_version
    before_revision_version = revision.row_version

    result = GeneralSkillCatalogGovernanceService(db).lifecycle(
        current_user=owner,
        skill_id=skill.id,
        command_id="catalog-lifecycle-archive-1",
        action="archive",
        expected_skill_row_version=before_skill_version,
        expected_revision_row_version=before_revision_version,
        reason="等待新的来源风险复核",
    )

    assert result.action == "archive"
    assert result.deactivated_binding_count == 0
    db.expire_all()
    archived = db.get(GeneralSkill, skill.id)
    archived_revision = db.get(GeneralSkillRevision, revision.id)
    archived_binding = db.get(AgentResourceBinding, binding.id)
    assert archived is not None and archived.status == "archived"
    assert archived_revision is not None and archived_revision.status == "published"
    assert archived_binding is not None and archived_binding.status == "active"
    assert archived.metadata_json["catalog_lifecycle_status"] == "archived"
    assert [item.skill_id for item in EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items] == [skill.id]

    with pytest.raises(CatalogGovernanceError, match="published catalog skill is unavailable"):
        GeneralSkillCatalogGovernanceService(db).bind(
            current_user=owner,
            skill_id=skill.id,
            agent_id=agent.id,
            mode="install",
            revision_policy="pinned",
            pinned_revision_id=revision.id,
            invocation_policy="model_allowed",
        )


def test_catalog_lifecycle_revoke_stops_existing_binding_and_is_replayable() -> None:
    """验证安全撤销会停用既有绑定、撤销修订并可安全重放同一命令。"""

    db = _catalog_db()
    owner, agent, skill, revision = _prepare_published_catalog_skill(db, "catalog_revoke_agent")
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_id == skill.id,
        )
    ).one()
    request = {
        "current_user": owner,
        "skill_id": skill.id,
        "command_id": "catalog-lifecycle-revoke-1",
        "action": "revoke",
        "expected_skill_row_version": skill.row_version,
        "expected_revision_row_version": revision.row_version,
        "reason": "发现不可接受的执行风险，立即停止采用",
    }
    service = GeneralSkillCatalogGovernanceService(db)
    result = service.lifecycle(**request)
    replay = service.lifecycle(**request)

    assert result.action == "revoke"
    assert result.deactivated_binding_count == 1
    assert replay.replayed is True
    db.expire_all()
    revoked = db.get(GeneralSkill, skill.id)
    revoked_revision = db.get(GeneralSkillRevision, revision.id)
    revoked_binding = db.get(AgentResourceBinding, binding.id)
    assert revoked is not None and revoked.status == "archived"
    assert revoked_revision is not None and revoked_revision.status == "revoked"
    assert revoked_binding is not None and revoked_binding.status == "inactive"
    assert EffectiveGeneralSkillResolver(db).resolve(owner, agent.id).items == ()
