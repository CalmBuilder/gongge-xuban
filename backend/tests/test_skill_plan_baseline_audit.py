"""
@Time       : 2026/08/29 16:00
@Author     : zhanglp8181
@File       : test_skill_plan_baseline_audit.py
@CallChain  : pytest → 多角色现状审计 → AgentProfile/AgentLoop/Skill Resolver/来源快照
@Description: 验证 Skill 广场四阶段方案赖以成立的当前身份、执行、权限、来源和交付基线。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import zipfile

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.identity import agent_category
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    GeneralSkill,
    GeneralSkillRevision,
    PublicationRelease,
    Skill,
    SopInstance,
    Tenant,
    User,
)
from app.general_skills.eligibility import EffectiveGeneralSkillResolver
from app.general_skills.runner import GENERAL_SKILL_PLAN_OUTPUT, GeneralSkillRunner
from app.sop_runtime.coordinator import DeterministicSopCoordinator


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "otherpro" / "skills"
BUILTIN_FIXTURE = (
    REPO_ROOT / "backend" / "app" / "db" / "seed_fixtures" / "otherpro_skills_catalog_6654f6b6.zip"
)
EXPECTED_SOURCE_REVISION = "6654f6b60cd9d5be8b54c6fafe44346dabeb3b76"


def _canonical_checksum(value: object) -> str:
    """按运行时同一规范生成资源清单 checksum。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_revision() -> str | None:
    """在存在 Git 元数据时读取上游快照 revision，缺少元数据则交由测试显式跳过。"""

    result = subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _check_constraints(model: type[SQLModel]) -> dict[str, str]:
    """返回模型声明的命名检查约束，供领域角色核对状态契约。"""

    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }


@contextmanager
def _sqlite_session() -> Iterator[Session]:
    """创建隔离 SQLite 会话，验证模型事实而不接触项目业务数据库。"""

    engine: Engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def _seed_skill_forms(db: Session) -> tuple[User, AgentProfile, AgentProfile, GeneralSkillRevision]:
    """创建个人能力分身和带组织事实的同一底座样本，供 Resolver 对称性审计。"""

    tenant = Tenant(id="tenant_baseline_audit", name="Baseline Audit")
    user = User(
        id="user_baseline_owner",
        tenant_id=tenant.id,
        username="baseline-owner",
        role="member",
        password_hash="test-only",
    )
    capability_avatar = AgentProfile(
        id="agent_baseline_avatar",
        tenant_id=tenant.id,
        name="基线能力分身",
        owner_user_id=user.id,
        status="active",
        agent_category_code="assistant",
        visibility_scope="private",
    )
    organization_employee = AgentProfile(
        id="agent_baseline_employee",
        tenant_id=tenant.id,
        name="基线组织数字员工",
        owner_user_id=user.id,
        responsible_org_unit_id="org_finance",
        status="active",
        agent_category_code="assistant",
        visibility_scope="tenant",
    )
    role_binding = AgentRoleBinding(
        id="agentrole_baseline_employee",
        tenant_id=tenant.id,
        agent_id=organization_employee.id,
        business_role_id="role_finance_review",
        assignment_mode="execute",
        supervisor_employee_profile_id="employee_supervisor",
        scope_type="org_unit",
        scope_id="org_finance",
        granted_by_user_id=user.id,
        status="active",
    )
    skill = GeneralSkill(
        id="genskill_baseline_guidance",
        tenant_id=tenant.id,
        slug="baseline-guidance",
        name="基线指导 Skill",
        description="用于验证两种治理形态共用资格链。",
        skill_markdown="# guidance",
        status="published",
        usage_mode="planning_guidance",
        owner_user_id=user.id,
        visibility_scope="user_private",
    )
    resource_checksum = hashlib.sha256(b"# guidance").hexdigest()
    manifest = [{"path": "SKILL.md", "checksum": resource_checksum}]
    revision = GeneralSkillRevision(
        id="gsrev_baseline_guidance_1",
        tenant_id=tenant.id,
        skill_id=skill.id,
        revision_number=1,
        content_checksum=_canonical_checksum(manifest),
        manifest_checksum=hashlib.sha256(b"baseline-manifest").hexdigest(),
        normalized_skill_markdown="# guidance",
        resource_manifest_json=manifest,
        status="published",
        created_by=user.id,
    )
    skill.current_published_revision_id = revision.id

    db.add(tenant)
    db.add(user)
    db.add(capability_avatar)
    db.add(organization_employee)
    db.add(role_binding)
    db.add(skill)
    db.add(revision)
    for agent in (capability_avatar, organization_employee):
        db.add(
            AgentResourceBinding(
                id=f"agentres_baseline_{agent.id.rsplit('_', 1)[-1]}",
                tenant_id=tenant.id,
                agent_id=agent.id,
                resource_type="general_skill",
                resource_id=skill.id,
                status="active",
                metadata_json={
                    "schema_version": 1,
                    "revision_policy": "pinned",
                    "pinned_revision_id": revision.id,
                    "invocation_policy": "model_allowed",
                    "atomic_execution_allowed": False,
                    "created_by_user_id": user.id,
                },
            )
        )
    db.commit()
    return user, capability_avatar, organization_employee, revision


# 产品/领域角色：统一底座、治理形态和执行模式是两个维度。
def test_product_role_confirms_unified_agent_profile_without_identity_type() -> None:
    """验证能力分身、组织数字员工和专家没有各自的 AgentProfile 身份列。"""

    fields = set(AgentProfile.model_fields)
    assert {
        "owner_user_id",
        "source_agent_id",
        "source_agent_version",
        "responsible_org_unit_id",
        "published_to_gallery",
        "agent_category_code",
        "visibility_scope",
    } <= fields
    assert not fields.intersection(
        {"governance_form", "identity_type", "capability_avatar", "digital_employee"}
    )

    expert_template = AgentProfile(
        id="agent_baseline_expert",
        tenant_id="tenant_baseline_audit",
        name="专家模板分类样本",
        agent_category_code="assistant",
        metadata_json={"employee_type": "expert"},
    )
    assert agent_category(expert_template) == "professional"
    assert "DynamicTaskAgent" not in fields


def test_product_role_confirms_governance_facts_are_separate_from_agent_identity() -> None:
    """验证组织角色、发布事实和 AgentProfile 身份分别由不同模型表达。"""

    role_fields = set(AgentRoleBinding.model_fields)
    assert {
        "agent_id",
        "business_role_id",
        "assignment_mode",
        "supervisor_employee_profile_id",
        "scope_type",
        "scope_id",
        "granted_by_user_id",
        "status",
        "effective_from",
        "effective_until",
    } <= role_fields
    release_fields = set(PublicationRelease.model_fields)
    assert {"resource_type", "resource_id", "snapshot_id", "status", "row_version"} <= release_fields
    assert "publication_release_id" not in set(AgentProfile.model_fields)


# SOP/动态任务角色：执行模式是执行账本的 kind，不是 Agent 身份。
def test_runtime_role_confirms_sop_and_dynamic_task_are_disjoint_execution_contracts() -> None:
    """验证 SOP 和动态任务的字段约束、运行模式和路由入口确实分离。"""

    constraints = _check_constraints(SopInstance)
    assert "kind IN ('sop', 'dynamic_task')" in constraints["ck_execution_kind"]
    assert "skill_id IS NOT NULL" in constraints["ck_execution_sop_identity"]
    assert "current_plan_revision_id IS NULL" in constraints["ck_execution_sop_without_dynamic_plan"]
    assert "agent_id IS NOT NULL" in constraints["ck_execution_dynamic_identity"]
    assert "initiator_user_id IS NOT NULL" in constraints["ck_execution_dynamic_identity"]
    assert "current_plan_checksum IS NOT NULL" in constraints["ck_execution_dynamic_identity"]

    deterministic_sop = Skill(
        id="skill_baseline_deterministic",
        tenant_id="tenant_baseline_audit",
        skill_id="sop_baseline",
        name="确定性基线 SOP",
        content_json={"execution_mode": "deterministic"},
        status="published",
    )
    ordinary_skill = Skill(
        id="skill_baseline_ordinary",
        tenant_id="tenant_baseline_audit",
        skill_id="ordinary_baseline",
        name="普通基线能力",
        content_json={},
        status="published",
    )
    assert DeterministicSopCoordinator.is_enabled(deterministic_sop)
    assert not DeterministicSopCoordinator.is_enabled(ordinary_skill)

    dynamic_route_source = inspect.getsource(AgentLoop._try_handle_dynamic_task)
    assert "DynamicTaskAgent(self.db)" in dynamic_route_source
    assert "if decision.mode != \"dynamic_task\"" in dynamic_route_source
    assert callable(AgentLoop._answer_with_loaded_general_skill)


def test_runtime_role_confirms_dynamic_requests_do_not_relabel_the_agent() -> None:
    """验证进入 DynamicTaskAgent 是本轮请求路由，不是能力分身身份判定。"""

    assert AgentLoop._explicit_dynamic_task_requested(
        "请通过持久、可恢复的分析任务完成合同风险诊断"
    )
    assert not AgentLoop._explicit_dynamic_task_requested("请解释 DynamicTaskAgent 是什么")


# Skill/权限角色：两种治理形态共用 agent_id + user + binding + revision 资格链。
def test_skill_role_confirms_capability_and_organization_forms_use_same_resolver() -> None:
    """验证个人能力分身和带组织治理事实的 Agent 使用同一个 Skill Resolver。"""

    with _sqlite_session() as db:
        user, capability_avatar, organization_employee, revision = _seed_skill_forms(db)
        resolver = EffectiveGeneralSkillResolver(db)

        avatar_catalog = resolver.resolve(user, capability_avatar.id)
        employee_catalog = resolver.resolve(user, organization_employee.id)

        assert len(avatar_catalog.items) == 1
        assert len(employee_catalog.items) == 1
        assert avatar_catalog.items[0].skill_id == employee_catalog.items[0].skill_id
        assert avatar_catalog.items[0].revision_id == revision.id
        assert employee_catalog.items[0].revision_id == revision.id
        assert avatar_catalog.agent_id != employee_catalog.agent_id


def test_security_role_confirms_tenant_and_metadata_boundaries_fail_closed() -> None:
    """验证跨租户 Agent 和带旧自由字段的 Skill binding 都不能进入有效目录。"""

    with _sqlite_session() as db:
        user, capability_avatar, _organization_employee, revision = _seed_skill_forms(db)
        other_tenant = Tenant(id="tenant_baseline_other", name="Other Tenant")
        other_user = User(
            id="user_baseline_other",
            tenant_id=other_tenant.id,
            username="other-user",
            role="member",
            password_hash="test-only",
        )
        other_agent = AgentProfile(
            id="agent_baseline_other",
            tenant_id=other_tenant.id,
            name="其他租户 Agent",
            owner_user_id=other_user.id,
            status="active",
        )
        invalid_agent = AgentProfile(
            id="agent_baseline_invalid_metadata",
            tenant_id=user.tenant_id,
            name="旧 metadata Agent",
            owner_user_id=user.id,
            status="active",
        )
        db.add(other_tenant)
        db.add(other_user)
        db.add(other_agent)
        db.add(invalid_agent)
        db.add(
            AgentResourceBinding(
                id="agentres_baseline_invalid_metadata",
                tenant_id=user.tenant_id,
                agent_id=invalid_agent.id,
                resource_type="general_skill",
                resource_id="genskill_baseline_guidance",
                status="active",
                metadata_json={
                    "schema_version": 1,
                    "revision_policy": "pinned",
                    "pinned_revision_id": revision.id,
                    "invocation_policy": "model_allowed",
                    "atomic_execution_allowed": False,
                    "created_by_user_id": user.id,
                    "scope": "legacy_private_scope",
                },
            )
        )
        db.commit()

        resolver = EffectiveGeneralSkillResolver(db)
        assert resolver.resolve(other_user, capability_avatar.id).items == ()
        assert resolver.resolve(user, invalid_agent.id).items == ()


# 平台资产/供应链角色：源目录和项目内固化产物数量、路径和 revision 必须一致。
def test_supply_chain_role_confirms_37_skill_source_and_fixture_match() -> None:
    """验证当前上游目录与项目内 fixture 都是同一 revision 的 37 个 Skill。"""

    assert SOURCE_ROOT.is_dir()
    assert BUILTIN_FIXTURE.is_file()
    source_revision = _source_revision()
    if source_revision is None:
        pytest.skip("otherpro/skills 缺少 Git 元数据，无法在本环境核对完整 source revision")
    assert source_revision == EXPECTED_SOURCE_REVISION

    source_skill_paths = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("SKILL.md")
        if path.is_file()
    }
    assert len(source_skill_paths) == 37

    with zipfile.ZipFile(BUILTIN_FIXTURE) as archive:
        fixture_names = set(archive.namelist())
        fixture_skill_paths = {
            name for name in fixture_names if name.endswith("/SKILL.md")
        }
        assert len(fixture_skill_paths) == 37
        assert fixture_skill_paths == source_skill_paths
        assert all(
            not name.startswith("/") and "../" not in name and "\\" not in name
            for name in fixture_names
        )
        for path in sorted(source_skill_paths):
            source_checksum = hashlib.sha256((SOURCE_ROOT / path).read_bytes()).hexdigest()
            fixture_checksum = hashlib.sha256(archive.read(path)).hexdigest()
            assert fixture_checksum == source_checksum, path
            assert archive.read(path).strip()
    assert EXPECTED_SOURCE_REVISION[:8] in BUILTIN_FIXTURE.name


def test_supply_chain_role_confirms_production_code_does_not_read_otherpro_paths() -> None:
    """验证主项目后端只应消费固化产物，不把 otherpro 目录作为运行时路径。"""

    backend_app = REPO_ROOT / "backend" / "app"
    forbidden_local_path = str(REPO_ROOT / "otherpro")
    for source_file in backend_app.rglob("*.py"):
        content = source_file.read_text(encoding="utf-8")
        assert forbidden_local_path not in content, source_file
        assert "otherpro/skills" not in content, source_file


# 安全/运行时角色：当前 runner 的真实能力证明 guidance_only 必须在入口形成门禁。
def test_security_role_confirms_existing_runner_is_python_bash_capable() -> None:
    """验证当前通用 Skill runner 会启动 Python/Bash，方案不能假定导入即安全。"""

    execution_source = inspect.getsource(GeneralSkillRunner._execute_plan)
    assert "subprocess.Popen" in execution_source
    assert '"runner.sh"' in execution_source
    assert '"runner.py"' in execution_source
    assert GENERAL_SKILL_PLAN_OUTPUT["runtime"] == "bash | python"


# QA/发布角色：方案文档必须与当前基线、实施状态和关闭门禁一致。
def test_qa_role_confirms_plan_documents_current_baseline_and_gates() -> None:
    """验证方案明确写出当前底座、批次门禁、双数据库和浏览器回归。"""

    plan_path = REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-08-29-Skill广场第二阶段升级实施方案.md"
    manual_path = REPO_ROOT / "docs" / "manuals" / "共格·序伴能力分身与数字员工关系、组织化及执行模式手册.md"
    plan = plan_path.read_text(encoding="utf-8")
    manual = manual_path.read_text(encoding="utf-8")

    assert "方案审核通过，可开工" in plan
    assert "未宣称未运行的检查已通过" in plan
    assert "AgentProfile" in plan and "DynamicTaskAgent" in plan and "SopInstance" in plan
    assert "阶段与小批次通过标准" in plan
    assert all(batch_id in plan for batch_id in ("S1-A", "S2-C", "S3-B", "S4-C", "R-ALL"))
    assert "真实 Chromium" in plan
    assert "SQLite/MySQL" in plan
    assert "唯一主参考" in plan and "源码逻辑与流程" in plan
    assert "guidance_only" in plan
    assert "所有治理形态都可以使用通用 Skill" in manual
