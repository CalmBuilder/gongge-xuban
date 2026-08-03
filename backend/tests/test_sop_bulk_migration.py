"""
@Time       : 2026/07/29 11:38
@Author     : zhanglp8181
@File       : test_sop_bulk_migration.py
@CallChain  : M5.5-D 迁移服务 → 正式发布头与数字员工分支 → 版本与运行实例不变式
@Description: 验证全发布头受控升级、混合节点拆分、幂等和旧实例版本锚点。
"""

from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    BusinessRole,
    BusinessRolePermission,
    EmployeeRoleAssignment,
    KnowledgeBase,
    PermissionDefinition,
    Skill,
    SkillVersion,
    SopInstance,
    Tool,
)
from app.db.seed import seed_demo_data
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.bulk_migration import (
    M55_SOURCE_VERSIONS,
    M55_TARGET_VERSIONS,
    apply_m55_published_head_upgrade,
)
from app.sop_runtime.versioning import write_skill_version

TELECOM_AGENT_ID = "agent_m55d_telecom_fault"
TELECOM_KNOWLEDGE_NAMES = (
    "政企专线故障分级与申告规范-浏览器验收临时",
    "政企专线故障分级与申告规范-回归临时",
)
TELECOM_PERMISSION_CODES = (
    "telecom.circuit.read:any",
    "telecom.fault.create:own",
)
TELECOM_SKILL_IDS = (
    "skill_telecom_fault_browser_regression_20260728",
    "skill_telecom_fault_regression_20260728",
)
TELECOM_TOOL_NAMES = (
    "telecom.circuit.verify.browser.20260728",
    "telecom.enterprise_fault.create.browser.20260728",
    "telecom.circuit.verify.regression.20260728",
    "telecom.enterprise_fault.create.regression.20260728",
)


def _seeded_session() -> Session:
    """创建包含一期 21 个正式种子发布头的隔离 SQLite 会话。"""

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    seed_demo_data(db)
    db.commit()
    return db


def test_m55_upgrade_creates_derived_versions_without_reanchoring_old_instance() -> None:
    """全部现有种子头生成新快照，旧实例仍固定在来源版本。"""

    with _seeded_session() as db:
        source_skill = db.exec(
            select(Skill).where(Skill.skill_id == "expense_over_limit_approval")
        ).one()
        source_snapshot = db.exec(
            select(SkillVersion).where(
                SkillVersion.skill_id == source_skill.skill_id,
                SkillVersion.version == source_skill.version,
            )
        ).one()
        instance = SopInstance(
            tenant_id=source_skill.tenant_id,
            session_id="session_m55d_old",
            skill_id=source_skill.skill_id,
            skill_version_id=source_snapshot.id,
            skill_version=source_snapshot.version,
            definition_checksum=source_snapshot.compiled_definition_checksum or "checksum",
            status="waiting",
        )
        db.add(instance)
        db.commit()

        source_snapshots = {
            (row.skill_id, row.version): (row.id, row.content_json)
            for row in db.exec(select(SkillVersion)).all()
        }
        report = apply_m55_published_head_upgrade(
            db,
            tenant_id="tenant_demo",
            require_all=False,
        )
        db.commit()

        assert len(report.migrated_skill_ids) == len(M55_SOURCE_VERSIONS)
        assert report.missing_skill_ids == ()
        for skill in db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id.in_(tuple(report.migrated_skill_ids)),
            )
        ).all():
            assert skill.version == M55_TARGET_VERSIONS[skill.skill_id]
            compiled = compile_legacy_skill_card(skill.content_json)
            assert compiled.diagnostics == ()
            target = db.exec(
                select(SkillVersion).where(
                    SkillVersion.tenant_id == skill.tenant_id,
                    SkillVersion.skill_id == skill.skill_id,
                    SkillVersion.version == skill.version,
                )
            ).one()
            source_id, source_content = source_snapshots[
                (skill.skill_id, M55_SOURCE_VERSIONS[skill.skill_id])
            ]
            assert target.derived_from_version_id == source_id
            assert db.get(SkillVersion, source_id).content_json == source_content
            if skill.skill_id not in {"after_sales_exchange", "skill_purchase_001"}:
                source_without_version = {
                    key: value for key, value in source_content.items() if key != "version"
                }
                target_without_version = {
                    key: value
                    for key, value in target.content_json.items()
                    if key != "version"
                }
                assert target_without_version == source_without_version

        active_branches = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == "tenant_demo",
                AgentSkillBranch.status == "active",
                AgentSkillBranch.skill_id.in_(tuple(report.migrated_skill_ids)),
            )
        ).all()
        assert active_branches
        for branch in active_branches:
            assert branch.head_version == M55_TARGET_VERSIONS[branch.skill_id]
            assert branch.base_version == branch.head_version
            assert branch.content_json["version"] == branch.head_version

        recovered = db.get(SopInstance, instance.id)
        assert recovered.skill_version_id == source_snapshot.id
        assert recovered.skill_version == source_snapshot.version


def test_m55_upgrade_splits_mixed_nodes_and_is_exactly_idempotent() -> None:
    """两条旧混合节点不再有诊断，重复迁移不新增版本或刷新发布头。"""

    with _seeded_session() as db:
        first = apply_m55_published_head_upgrade(
            db,
            tenant_id="tenant_demo",
            require_all=False,
        )
        db.commit()
        exchange = db.exec(
            select(Skill).where(Skill.skill_id == "after_sales_exchange")
        ).one()
        purchase = db.exec(
            select(Skill).where(Skill.skill_id == "skill_purchase_001")
        ).one()
        exchange_compiled = compile_legacy_skill_card(exchange.content_json)
        purchase_compiled = compile_legacy_skill_card(purchase.content_json)
        assert exchange.version == "2.0.0"
        assert purchase.version == "2.0.0"
        assert exchange_compiled.diagnostics == ()
        assert purchase_compiled.diagnostics == ()
        assert {node.node_id for node in exchange_compiled.nodes} >= {
            "collect_exchange_order_info",
            "query_exchange_order",
            "reply_exchange_guidance",
        }
        purchase_service = next(
            node for node in purchase_compiled.nodes if node.node_id == "confirm_product"
        )
        assert purchase_service.config.operations == ("product.purchase",)

        before_heads = [
            (row.id, row.version, row.content_json, row.updated_at)
            for row in db.exec(select(Skill).order_by(Skill.id)).all()
        ]
        before_versions = [
            (
                row.id,
                row.skill_id,
                row.version,
                row.content_json,
                row.derived_from_version_id,
                row.updated_at,
            )
            for row in db.exec(select(SkillVersion).order_by(SkillVersion.id)).all()
        ]
        before_branches = [
            (
                row.id,
                row.base_version,
                row.head_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(select(AgentSkillBranch).order_by(AgentSkillBranch.id)).all()
        ]
        before_branch_versions = [
            (
                row.id,
                row.agent_id,
                row.skill_id,
                row.version,
                row.content_json,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentSkillBranchVersion).order_by(AgentSkillBranchVersion.id)
            ).all()
        ]
        second = apply_m55_published_head_upgrade(
            db,
            tenant_id="tenant_demo",
            require_all=False,
        )
        db.commit()

        assert first.migrated_skill_ids == second.already_migrated_skill_ids
        assert second.migrated_skill_ids == ()
        assert [
            (row.id, row.version, row.content_json, row.updated_at)
            for row in db.exec(select(Skill).order_by(Skill.id)).all()
        ] == before_heads
        assert [
            (
                row.id,
                row.skill_id,
                row.version,
                row.content_json,
                row.derived_from_version_id,
                row.updated_at,
            )
            for row in db.exec(select(SkillVersion).order_by(SkillVersion.id)).all()
        ] == before_versions
        assert [
            (
                row.id,
                row.base_version,
                row.head_version,
                row.content_json,
                row.sync_state,
                row.updated_at,
            )
            for row in db.exec(select(AgentSkillBranch).order_by(AgentSkillBranch.id)).all()
        ] == before_branches
        assert [
            (
                row.id,
                row.agent_id,
                row.skill_id,
                row.version,
                row.content_json,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentSkillBranchVersion).order_by(AgentSkillBranchVersion.id)
            ).all()
        ] == before_branch_versions


def test_m55_upgrade_does_not_promote_acceptance_assets_to_product_dependencies() -> None:
    """正式发布头升级不得创建或激活历史验收数字员工及其授权关系。"""

    with _seeded_session() as db:
        _seed_telecom_regression_resources(db)
        report = apply_m55_published_head_upgrade(
            db,
            tenant_id="tenant_demo",
            require_all=True,
        )
        db.commit()

        assert len(report.migrated_skill_ids) == len(M55_SOURCE_VERSIONS)
        agent = db.get(AgentProfile, TELECOM_AGENT_ID)
        assert agent is None
        for skill_id in TELECOM_SKILL_IDS:
            skill = db.exec(select(Skill).where(Skill.skill_id == skill_id)).one()
            assert skill.version == "1.0.0"
        assert all(
            permission.status == "inactive"
            for permission in db.exec(
                select(PermissionDefinition).where(
                    PermissionDefinition.permission_code.in_(TELECOM_PERMISSION_CODES)
                )
            ).all()
        )

        before_dependencies = _telecom_dependency_snapshot(db)
        second = apply_m55_published_head_upgrade(
            db,
            tenant_id="tenant_demo",
            require_all=True,
        )
        db.commit()

        assert len(second.already_migrated_skill_ids) == len(M55_SOURCE_VERSIONS)
        assert second.migrated_skill_ids == ()
        assert _telecom_dependency_snapshot(db) == before_dependencies


def _telecom_dependency_snapshot(db: Session) -> dict[str, list[tuple[object, ...]]]:
    """提取电信测试 SOP 的正式依赖和更新时间，证明重复执行不产生隐性写入。"""

    return {
        "knowledge_bases": [
            (row.id, row.metadata_json, row.updated_at)
            for row in db.exec(
                select(KnowledgeBase)
                .where(KnowledgeBase.name.in_(TELECOM_KNOWLEDGE_NAMES))
                .order_by(KnowledgeBase.id)
            ).all()
        ],
        "agent_resource_bindings": [
            (
                row.id,
                row.resource_type,
                row.resource_id,
                row.status,
                row.metadata_json,
                row.updated_at,
            )
            for row in db.exec(
                select(AgentResourceBinding)
                .where(AgentResourceBinding.agent_id == TELECOM_AGENT_ID)
                .order_by(AgentResourceBinding.id)
            ).all()
        ],
        "roles": [
            (row.id, row.role_code, row.status, row.updated_at)
            for row in db.exec(
                select(BusinessRole)
                .where(BusinessRole.role_code == "telecom_fault_operator")
                .order_by(BusinessRole.id)
            ).all()
        ],
        "role_permissions": [
            (row.id, row.business_role_id, row.permission_definition_id, row.created_at)
            for row in db.exec(
                select(BusinessRolePermission)
                .where(
                    BusinessRolePermission.business_role_id
                    == "bizrole_m55d_telecom_fault_operator"
                )
                .order_by(BusinessRolePermission.id)
            ).all()
        ],
        "employee_roles": [
            (row.id, row.status, row.updated_at)
            for row in db.exec(
                select(EmployeeRoleAssignment)
                .where(EmployeeRoleAssignment.business_role_id == "bizrole_m55d_telecom_fault_operator")
                .order_by(EmployeeRoleAssignment.id)
            ).all()
        ],
        "agent_roles": [
            (row.id, row.status, row.updated_at)
            for row in db.exec(
                select(AgentRoleBinding)
                .where(AgentRoleBinding.agent_id == TELECOM_AGENT_ID)
                .order_by(AgentRoleBinding.id)
            ).all()
        ],
        "permissions": [
            (row.id, row.permission_code, row.status, row.updated_at)
            for row in db.exec(
                select(PermissionDefinition)
                .where(PermissionDefinition.permission_code.in_(TELECOM_PERMISSION_CODES))
                .order_by(PermissionDefinition.id)
            ).all()
        ],
    }


def _seed_telecom_regression_resources(db: Session) -> None:
    """补造已清理的浏览器回归资源，复现实际 MySQL 的迁移前依赖形态。"""

    for permission_code in TELECOM_PERMISSION_CODES:
        db.add(
            PermissionDefinition(
                tenant_id="tenant_demo",
                permission_code=permission_code,
                name=permission_code,
                category="telecom",
                resource=permission_code.partition(":")[0],
                action=permission_code.partition(":")[2],
                scope="tenant",
                status="inactive",
            )
        )
    for index, tool_name in enumerate(TELECOM_TOOL_NAMES):
        skill_id = TELECOM_SKILL_IDS[0] if ".browser." in tool_name else TELECOM_SKILL_IDS[1]
        required_permission = (
            TELECOM_PERMISSION_CODES[index]
            if index < len(TELECOM_PERMISSION_CODES)
            else None
        )
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name=tool_name,
                display_name=tool_name,
                description="M5.5-D 测试工具",
                method="POST",
                url=f"/api/mock/{index}",
                allowed_skills_json=[skill_id],
                required_permission_code=required_permission,
                enabled=True,
            )
        )
    for index, name in enumerate(TELECOM_KNOWLEDGE_NAMES):
        db.add(
            KnowledgeBase(
                tenant_id="tenant_demo",
                name=name,
                owner_user_id="admin",
                access_scope="owner",
                status="active",
                metadata_json={
                    "scope": "agent_private",
                    "visibility": "agent_private",
                    "owner_agent_id": f"deleted_agent_{index}",
                },
            )
        )
    db.flush()

    operation_pairs = (
        TELECOM_TOOL_NAMES[:2],
        TELECOM_TOOL_NAMES[2:],
    )
    for skill_id, operations in zip(TELECOM_SKILL_IDS, operation_pairs, strict=True):
        content = {
            "skill_id": skill_id,
            "name": skill_id,
            "version": "1.0.0",
            "business_domain": "telecom",
            "description": "M5.5-D 测试发布头",
            "execution_mode": "deterministic",
            "required_info": [],
            "start_node_id": "verify",
            "terminal_node_ids": ["reply"],
            "nodes": [
                {
                    "node_id": "verify",
                    "type": "tool_call",
                    "name": "核验",
                    "allowed_actions": [f"call_tool:{operations[0]}"],
                },
                {
                    "node_id": "policy",
                    "type": "knowledge_query",
                    "name": "制度",
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": "故障等级和首次响应时限",
                            "max_chunks": 6,
                            "max_depth": 2,
                        },
                        "operation_result_key": "fault_policy",
                    },
                },
                {
                    "node_id": "create",
                    "type": "tool_call",
                    "name": "建单",
                    "allowed_actions": [f"call_tool:{operations[1]}"],
                },
                {
                    "node_id": "reply",
                    "type": "response",
                    "name": "结果",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {"source_node_id": "verify", "next_node_id": "policy"},
                {"source_node_id": "policy", "next_node_id": "create"},
                {"source_node_id": "create", "next_node_id": "reply"},
            ],
        }
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id=skill_id,
            version="1.0.0",
            name=skill_id,
            business_domain="telecom",
            description="M5.5-D 测试发布头",
            content_json=content,
            status="published",
        )
        db.add(skill)
        db.flush()
        write_skill_version(
            db,
            skill,
            compiled_definition=compile_legacy_skill_card(content),
        )
    db.commit()
