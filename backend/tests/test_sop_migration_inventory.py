"""
@Time       : 2026/07/29 09:35
@Author     : zhanglp8181
@File       : test_sop_migration_inventory.py
@CallChain  : pytest → SOP 迁移预检 → 兼容编译/版本与实例快照
@Description: 验证 M5.5 迁移分类稳定、单项隔离且只读，不改写发布版本和运行实例。
"""

from __future__ import annotations

from fastapi import HTTPException
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.sop_migrations import get_sop_dependency_coverage, get_sop_migration_preview
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    Skill,
    SkillVersion,
    SopInstance,
    SopWorkItem,
    Tenant,
    Tool,
    User,
)
from app.sop_runtime.migration_inventory import (
    MigrationDisposition,
    build_sop_dependency_coverage,
    build_sop_migration_inventory,
)


def test_inventory_is_deterministic_and_does_not_mutate_runtime_rows() -> None:
    """重复预检必须产生相同报告，并保持发布快照、实例和候选快照原样。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _add_published_skill(db, "ready_skill", "1.0.0", _ready_content("ready_skill"))
        version = db.exec(select(SkillVersion)).one()
        instance = SopInstance(
            id="instance_1",
            tenant_id="tenant_a",
            session_id="session_1",
            skill_id="ready_skill",
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum=version.compiled_definition_checksum or "a" * 64,
            status="waiting",
            active_slot_key="foreground:session_1",
        )
        db.add(instance)
        db.flush()
        db.add(
            SopWorkItem(
                id="work_1",
                tenant_id="tenant_a",
                instance_id=instance.id,
                node_execution_id="execution_1",
                skill_version_id=version.id,
                node_id="approve",
                status="offered",
                candidate_snapshot_json=[{"user_id": "reviewer"}],
                participant_scope_snapshot_json={"resolver": "tenant"},
            )
        )
        db.commit()

        before_version = _version_snapshot(db)
        before_instance = _instance_snapshot(db)
        before_work_item = _work_item_snapshot(db)

        first = build_sop_migration_inventory(db, "tenant_a")
        second = build_sop_migration_inventory(db, "tenant_a")

        assert first == second
        assert first.entries[0].disposition is MigrationDisposition.NO_MIGRATION
        assert first.entries[0].active_instance_count == 1
        assert first.entries[0].active_work_item_count == 1
        assert _version_snapshot(db) == before_version
        assert _instance_snapshot(db) == before_instance
        assert _work_item_snapshot(db) == before_work_item


def test_inventory_isolates_warning_and_blocked_skills() -> None:
    """一个旧定义告警或阻断不得中止其他 SOP，并应返回可解释的稳定分类。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _add_published_skill(db, "z_ready", "1.0.0", _ready_content("z_ready"))
        _add_published_skill(db, "a_warning", "1.0.0", _mixed_node_content("a_warning"))
        _add_published_skill(
            db,
            "m_blocked",
            "1.0.0",
            {
                "skill_id": "m_blocked",
                "version": "1.0.0",
                "name": "阻断流程",
                "description": "缺少可执行节点",
                "execution_mode": "deterministic",
                "nodes": [],
            },
        )
        db.commit()

        report = build_sop_migration_inventory(db, "tenant_a")

        assert [entry.skill_id for entry in report.entries] == [
            "a_warning",
            "m_blocked",
            "z_ready",
        ]
        by_id = {entry.skill_id: entry for entry in report.entries}
        assert by_id["a_warning"].disposition is MigrationDisposition.BUSINESS_CONFIRMATION
        assert "LEGACY_MIXED_NODE_REQUIRES_SPLIT" in by_id["a_warning"].diagnostic_codes
        assert by_id["m_blocked"].disposition is MigrationDisposition.TEMPORARILY_UNSUPPORTED
        assert by_id["m_blocked"].diagnostic_codes
        assert by_id["z_ready"].disposition is MigrationDisposition.NO_MIGRATION
        assert report.disposition_counts == {
            "auto_new_version": 0,
            "business_confirmation": 1,
            "no_migration": 1,
            "temporarily_unsupported": 1,
        }


def test_inventory_blocks_published_head_when_snapshot_content_differs() -> None:
    """同版本发布头被原地改写后必须阻断，不能因快照存在而误报为无需迁移。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _add_published_skill(db, "mutated_head", "1.0.0", _ready_content("mutated_head"))
        head = db.exec(select(Skill).where(Skill.skill_id == "mutated_head")).one()
        head.content_json = {
            **head.content_json,
            "description": "发布后被错误原地修改",
        }
        db.add(head)
        db.commit()

        report = build_sop_migration_inventory(db, "tenant_a")

        assert report.entries[0].disposition is MigrationDisposition.TEMPORARILY_UNSUPPORTED
        assert report.entries[0].reason_code == "CURRENT_PUBLISHED_SNAPSHOT_MISMATCH"
        assert report.entries[0].diagnostic_codes == (
            "CURRENT_PUBLISHED_SNAPSHOT_MISMATCH",
        )


def test_active_historical_instances_are_counted_but_never_retargeted() -> None:
    """活动实例继续绑定创建时版本，预检只报告历史版本占用而不迁移实例。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _add_published_skill(db, "versioned", "1.0.0", _ready_content("versioned", "1.0.0"))
        old_version = db.exec(select(SkillVersion)).one()
        _add_published_skill(db, "versioned", "1.1.0", _ready_content("versioned", "1.1.0"))
        versions = db.exec(
            select(SkillVersion).where(SkillVersion.skill_id == "versioned")
        ).all()
        current_version = next(item for item in versions if item.version == "1.1.0")
        instance = SopInstance(
            id="instance_old",
            tenant_id="tenant_a",
            session_id="session_old",
            skill_id="versioned",
            skill_version_id=old_version.id,
            skill_version=old_version.version,
            definition_checksum=old_version.compiled_definition_checksum or "a" * 64,
            status="running",
            active_slot_key="foreground:session_old",
        )
        db.add(instance)
        db.commit()

        report = build_sop_migration_inventory(db, "tenant_a")
        refreshed = db.get(SopInstance, instance.id)

        assert report.entries[0].published_version_count == 2
        assert report.entries[0].active_instance_count == 1
        assert report.entries[0].active_historical_instance_count == 1
        assert refreshed is not None
        assert refreshed.skill_version_id == old_version.id
        assert refreshed.skill_version_id != current_version.id


def test_published_head_without_immutable_snapshot_is_temporarily_unsupported() -> None:
    """发布头缺少同版本快照时必须阻断迁移，不能把可编译误报为无需迁移。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Skill(
                id="skill_orphan",
                tenant_id="tenant_a",
                skill_id="orphan",
                version="1.0.0",
                name="缺少快照",
                content_json=_ready_content("orphan"),
                status="published",
            )
        )
        db.commit()

        report = build_sop_migration_inventory(db, "tenant_a")

        assert report.entries[0].published_version_count == 0
        assert (
            report.entries[0].disposition
            is MigrationDisposition.TEMPORARILY_UNSUPPORTED
        )
        assert report.entries[0].reason_code == "CURRENT_PUBLISHED_SNAPSHOT_MISSING"


def test_active_work_item_is_reported_even_if_its_instance_is_terminal() -> None:
    """异常的活动工作项不能因实例已终态而从迁移风险报告中消失。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _add_published_skill(db, "inconsistent", "1.0.0", _ready_content("inconsistent"))
        version = db.exec(select(SkillVersion)).one()
        db.add(
            SopInstance(
                id="instance_terminal",
                tenant_id="tenant_a",
                session_id="session_terminal",
                skill_id="inconsistent",
                skill_version_id=version.id,
                skill_version=version.version,
                definition_checksum=version.compiled_definition_checksum or "a" * 64,
                status="succeeded",
            )
        )
        db.add(
            SopWorkItem(
                id="work_orphan_active",
                tenant_id="tenant_a",
                instance_id="instance_terminal",
                node_execution_id="execution_terminal",
                skill_version_id=version.id,
                node_id="review",
                status="offered",
            )
        )
        db.commit()

        report = build_sop_migration_inventory(db, "tenant_a")

        assert report.active_instance_count == 0
        assert report.active_work_item_count == 1
        assert report.entries[0].active_work_item_count == 1


def test_preview_api_requires_authorization_read_and_tenant_boundary() -> None:
    """迁移预检属于治理数据，兼容管理员可读且跨租户直链必须拒绝。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="企业甲"))
        db.add(Tenant(id="tenant_b", name="企业乙"))
        admin = User(
            id="admin_a",
            tenant_id="tenant_a",
            username="admin",
            display_name="管理员",
            role="admin",
            password_hash="not-used",
        )
        member = User(
            id="member_a",
            tenant_id="tenant_a",
            username="member",
            display_name="普通成员",
            role="member",
            password_hash="not-used",
        )
        db.add(admin)
        db.add(member)
        db.commit()

        allowed = get_sop_migration_preview(
            tenant_id="tenant_a",
            current_user=admin,
            db=db,
        )
        assert allowed.tenant_id == "tenant_a"

        with pytest.raises(HTTPException) as denied:
            get_sop_migration_preview(
                tenant_id="tenant_a",
                current_user=member,
                db=db,
            )
        assert denied.value.status_code == 403

        with pytest.raises(HTTPException) as cross_tenant:
            get_sop_migration_preview(
                tenant_id="tenant_b",
                current_user=admin,
                db=db,
            )
        assert cross_tenant.value.status_code == 403


def test_dependency_coverage_reuses_preview_readiness_and_default_requester_policy() -> None:
    """治理覆盖报告必须复用迁移预检判定，并明确当前平台级发起人默认策略。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="企业甲"))
        admin = User(
            id="admin_a",
            tenant_id="tenant_a",
            username="admin",
            display_name="管理员",
            role="admin",
            password_hash="not-used",
        )
        db.add(admin)
        _add_published_skill(db, "ready_skill", "1.0.0", _ready_content("ready_skill"))
        db.commit()

        preview = build_sop_migration_inventory(db, "tenant_a")
        report = build_sop_dependency_coverage(db, "tenant_a")
        api_report = get_sop_dependency_coverage(
            tenant_id="tenant_a",
            current_user=admin,
            db=db,
        )

        assert report == api_report
        assert report.total == preview.total == 1
        assert report.readiness_counts == preview.dependency_counts
        assert (
            report.entries[0].dependency_assessment
            == preview.entries[0].dependency_assessment
        )
        assert report.entries[0].requester_policy == "active_tenant_member"
        assert report.entries[0].requester_policy_explicit is False


def _add_published_skill(
    db: Session,
    skill_id: str,
    version: str,
    content: dict[str, object],
) -> None:
    """写入测试所需发布头和不可变版本；同技能新版本只替换当前头。"""

    existing = db.exec(
        select(Skill).where(Skill.tenant_id == "tenant_a", Skill.skill_id == skill_id)
    ).first()
    if existing is None:
        existing = Skill(
            id=f"skill_{skill_id}",
            tenant_id="tenant_a",
            skill_id=skill_id,
            version=version,
            name=str(content["name"]),
            content_json=content,
            status="published",
        )
    else:
        existing.version = version
        existing.content_json = content
    db.add(existing)
    db.add(
        SkillVersion(
            id=f"version_{skill_id}_{version}",
            tenant_id="tenant_a",
            skill_id=skill_id,
            version=version,
            name=str(content["name"]),
            content_json=content,
            status="published",
            content_checksum="b" * 64,
            compiled_definition_checksum="a" * 64,
            meta_model_version=1,
            source_schema_version=1,
        )
    )
    overall = db.get(AgentProfile, "agent_overall_test")
    if overall is None:
        overall = AgentProfile(
            id="agent_overall_test",
            tenant_id="tenant_a",
            name="整体智能体",
            is_overall=True,
            owner_user_id="owner_test",
            status="active",
        )
        db.add(overall)
    executor = db.get(AgentProfile, "agent_executor_test")
    if executor is None:
        executor = AgentProfile(
            id="agent_executor_test",
            tenant_id="tenant_a",
            name="测试执行员工",
            owner_user_id="owner_test",
            published_to_gallery=True,
            visibility_scope="tenant",
            status="active",
        )
        db.add(executor)
    skill_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == "tenant_a",
            AgentResourceBinding.agent_id == overall.id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == existing.id,
        )
    ).first()
    if skill_binding is None:
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_a",
                agent_id=overall.id,
                resource_type="skill",
                resource_id=existing.id,
                status="active",
                metadata_json={"scope": "open_gallery", "visibility": "open_gallery"},
            )
        )
    executor_skill_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == "tenant_a",
            AgentResourceBinding.agent_id == executor.id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == existing.id,
        )
    ).first()
    if executor_skill_binding is None:
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_a",
                agent_id=executor.id,
                resource_type="skill",
                resource_id=existing.id,
                status="active",
                metadata_json={
                    "scope": "agent_private",
                    "visibility": "agent_private",
                    "owner_agent_id": executor.id,
                },
            )
        )
    legacy_tool = db.exec(
        select(Tool).where(
            Tool.tenant_id == "tenant_a",
            Tool.name == "legacy_lookup",
        )
    ).first()
    if legacy_tool is None:
        legacy_tool = Tool(
            id="tool_legacy_lookup",
            tenant_id="tenant_a",
            name="legacy_lookup",
            method="POST",
            url="https://example.test/legacy",
            enabled=True,
        )
        db.add(legacy_tool)
    tool_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == "tenant_a",
            AgentResourceBinding.agent_id == overall.id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.resource_id == legacy_tool.id,
        )
    ).first()
    if tool_binding is None:
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_a",
                agent_id=overall.id,
                resource_type="tool",
                resource_id=legacy_tool.id,
                status="active",
                metadata_json={"scope": "open_gallery", "visibility": "open_gallery"},
            )
        )
    executor_tool_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == "tenant_a",
            AgentResourceBinding.agent_id == executor.id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.resource_id == legacy_tool.id,
        )
    ).first()
    if executor_tool_binding is None:
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_a",
                agent_id=executor.id,
                resource_type="tool",
                resource_id=legacy_tool.id,
                status="active",
                metadata_json={
                    "scope": "agent_private",
                    "visibility": "agent_private",
                    "owner_agent_id": executor.id,
                },
            )
        )
    db.flush()


def _ready_content(skill_id: str, version: str = "1.0.0") -> dict[str, object]:
    """生成最小可执行的确定性输入收集与结束流程。"""

    return {
        "skill_id": skill_id,
        "version": version,
        "name": f"{skill_id} 流程",
        "description": "测试流程",
        "execution_mode": "deterministic",
        "nodes": [
            {
                "node_id": "collect",
                "name": "收集信息",
                "type": "collect_info",
                "expected_user_info": ["subject"],
            },
            {
                "node_id": "done",
                "name": "完成",
                "type": "response",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
        "start_node_id": "collect",
        "terminal_node_ids": ["done"],
    }


def _mixed_node_content(skill_id: str) -> dict[str, object]:
    """生成旧式同节点收集输入并调用工具的业务确认样本。"""

    content = _ready_content(skill_id)
    content["nodes"] = [
        {
            "node_id": "mixed",
            "name": "收集并执行",
            "type": "service_task",
            "expected_user_info": ["subject"],
            "allowed_actions": ["call_tool:legacy_lookup"],
        },
        {
            "node_id": "done",
            "name": "完成",
            "type": "response",
            "allowed_actions": ["answer_user"],
        },
    ]
    content["edges"] = [{"source_node_id": "mixed", "next_node_id": "done"}]
    content["start_node_id"] = "mixed"
    content["terminal_node_ids"] = ["done"]
    return content


def _version_snapshot(db: Session) -> list[tuple[str, str, str, dict[str, object]]]:
    """读取版本不可变性断言所需快照。"""

    return [
        (row.id, row.version, row.status, row.content_json)
        for row in db.exec(select(SkillVersion).order_by(SkillVersion.id)).all()
    ]


def _instance_snapshot(db: Session) -> list[tuple[str, str, str, int]]:
    """读取实例版本锚点与状态快照。"""

    return [
        (row.id, row.skill_version_id, row.status, row.revision)
        for row in db.exec(select(SopInstance).order_by(SopInstance.id)).all()
    ]


def _work_item_snapshot(db: Session) -> list[tuple[str, str, list[dict[str, object]]]]:
    """读取工作项版本锚点与候选快照。"""

    return [
        (row.id, row.skill_version_id, row.candidate_snapshot_json)
        for row in db.exec(select(SopWorkItem).order_by(SopWorkItem.id)).all()
    ]
