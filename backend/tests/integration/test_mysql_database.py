"""
@Time       : 2026/07/27 16:10
@Author     : zhanglp8181
@File       : test_mysql_database.py
@CallChain  : pytest mysql 标记 → Alembic/SQLModel → 临时 MySQL 8.4 数据库
@Description: 验证 MySQL 迁移、双方言关键约束、数据往返与连接池行为。
"""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from app.api.model_configs import set_default_model_config
from app.db.models import (
    ExecutionMutationRejection,
    Message,
    ModelConfig,
    Skill,
    SkillVersion,
    SopInstance,
    SopOperation,
    SopWorkItem,
    Tenant,
)
from app.db.seed import (
    EXCHANGE_SKILL,
    PRICE_COMPARE_SKILL,
    PURCHASE_SKILL,
    REFUND_SKILL,
    _skill_content_graph,
    seed_demo_data,
)
from app.sop_runtime.bulk_migration import (
    M55_SOURCE_VERSIONS,
    apply_m55_published_head_upgrade,
)
from app.sop_runtime.migration_inventory import build_sop_migration_inventory
from app.sop_runtime.execution_store import (
    SopExecutionConflictError,
    SopExecutionFencedError,
    SopExecutionStore,
)


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def upgrade(url: str, revision: str = "head") -> None:
    """把隔离 MySQL 测试库迁移到指定修订，默认迁至当前 head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, revision)


def downgrade(url: str, revision: str) -> None:
    """把隔离 MySQL 测试库降级到指定修订，用于验证迁移可逆边界。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = url
    command.downgrade(config, revision)


def _seed_legacy_published_skills(session: Session) -> None:
    """按 0026 schema 只写入 0027 迁移所需的四条已证明历史 Skill。"""

    for card in (
        EXCHANGE_SKILL,
        REFUND_SKILL,
        PRICE_COMPARE_SKILL,
        PURCHASE_SKILL,
    ):
        content = _skill_content_graph(card)
        if card["skill_id"] != "skill_price_compare_001":
            content["slot_filling_policy"]["target_info"] = sorted(
                content["slot_filling_policy"]["target_info"]
            )
        session.add(
            Skill(
                id=f"skill_{card['skill_id']}",
                tenant_id="tenant_demo",
                skill_id=card["skill_id"],
                version="1.0.0",
                name=card["name"],
                business_domain=card["business_domain"],
                description=card["description"],
                content_json=content,
                status="published",
            )
        )
    session.commit()


def test_alembic_upgrades_empty_mysql_database(mysql_database_url: str) -> None:
    """验证空 MySQL 数据库可迁至最新头部，并包含组织与 Agent 正式字段。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert "alembic_version" in tables
    assert "messages" in tables
    assert "agent_resource_bindings" in tables
    assert "code_sets" in tables
    assert "code_items" in tables
    assert "organization_units" in tables
    assert "member_org_assignments" in tables
    assert "positions" in tables
    assert "position_assignments" in tables
    assert "organization_migration_issues" in tables
    assert "position_role_bindings" in tables
    assert "organization_leader_assignments" in tables
    assert "knowledge_base_org_access" in tables
    assert "management_audit_logs" in tables
    assert "execution_mutation_rejections" in tables
    assert "ix_org_unit_tenant_parent_status_sort" in {
        item["name"] for item in inspector.get_indexes("organization_units")
    }
    assert "ix_member_org_tenant_org_current" in {
        item["name"] for item in inspector.get_indexes("member_org_assignments")
    }
    assert "ix_org_leader_tenant_org_current" in {
        item["name"] for item in inspector.get_indexes("organization_leader_assignments")
    }
    branch_constraints = {
        item["name"] for item in inspector.get_unique_constraints("agent_skill_branch_versions")
    }
    resource_constraints = {
        item["name"] for item in inspector.get_unique_constraints("agent_resource_bindings")
    }
    model_indexes = {item["name"] for item in inspector.get_indexes("model_configs")}
    model_columns = {item["name"]: item for item in inspector.get_columns("model_configs")}
    user_columns = {item["name"]: item for item in inspector.get_columns("users")}
    employee_columns = {item["name"]: item for item in inspector.get_columns("employee_profiles")}
    work_item_columns = {item["name"]: item for item in inspector.get_columns("sop_work_items")}
    execution_columns = {item["name"]: item for item in inspector.get_columns("sop_instances")}
    agent_columns = {item["name"]: item for item in inspector.get_columns("agent_profiles")}
    session_columns = {item["name"]: item for item in inspector.get_columns("sessions")}
    knowledge_columns = {
        item["name"]: item for item in inspector.get_columns("knowledge_bases")
    }
    for table_name in (
        "member_org_assignments",
        "position_assignments",
        "organization_leader_assignments",
        "employee_role_assignments",
    ):
        interval_columns = {
            item["name"]: item for item in inspector.get_columns(table_name)
        }
        assert interval_columns["effective_from"]["type"].fsp == 6
        assert interval_columns["effective_until"]["type"].fsp == 6
    assert "uq_agent_skill_branch_version" in branch_constraints
    assert "uq_agent_resource" in resource_constraints
    assert "uq_model_configs_tenant_default" in model_indexes
    assert model_columns["default_tenant_id"].get("computed") is not None
    assert {
        "membership_status",
        "member_category_code",
        "joined_at",
        "left_at",
    }.issubset(user_columns)
    assert {"join_date", "leave_date"}.issubset(employee_columns)
    assert "participant_scope_snapshot_json" in work_item_columns
    assert {
        "kind",
        "active_slot_key",
        "initiator_user_id",
        "source_kind",
        "source_ref",
        "cancellation_requested_at",
        "cancellation_requested_by",
        "cancellation_reason",
        "cancellation_disposition",
        "lease_owner",
        "lease_expires_at",
        "lease_acquired_at",
        "lease_heartbeat_at",
        "fencing_token",
    }.issubset(execution_columns)
    assert {
        "owner_user_id",
        "responsible_org_unit_id",
        "source_agent_id",
        "source_agent_version",
        "profile_revision",
        "published_to_gallery",
        "gallery_published_at",
        "gallery_published_by",
        "agent_category_code",
        "visibility_scope",
    }.issubset(agent_columns)
    assert {
        "agent_profile_revision",
        "capability_snapshot_json",
        "origin",
    }.issubset(session_columns)
    assert {
        "owner_user_id",
        "responsible_org_unit_id",
        "access_scope",
        "download_policy",
        "revision",
    }.issubset(knowledge_columns)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "20260803_0036"
        )


def test_execution_ownership_migration_backfills_and_reverses_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 0036 对真实 0035 表完成回填、约束和可重复升降级。"""

    upgrade(mysql_database_url, "20260802_0035")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sop_instances "
                "(id, tenant_id, session_id, skill_id, skill_version_id, skill_version, "
                "definition_checksum, run_number, status, current_node_id, slots_json, "
                "context_json, revision, started_at, completed_at, created_at, updated_at) VALUES "
                "('mysql_active', 'tenant_b01', 'session_active', 'skill_a', 'version_a', "
                "'1.0.0', :checksum, 1, 'running', 'node_a', JSON_OBJECT(), JSON_OBJECT(), "
                "1, UTC_TIMESTAMP(), NULL, UTC_TIMESTAMP(), UTC_TIMESTAMP()), "
                "('mysql_done', 'tenant_b01', 'session_done', 'skill_b', 'version_b', "
                "'1.0.0', :checksum, 1, 'succeeded', 'node_b', JSON_OBJECT(), JSON_OBJECT(), "
                "3, UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {"checksum": "a" * 64},
        )

    upgrade(mysql_database_url, "20260803_0036")
    inspector = inspect(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, kind, active_slot_key, source_kind, source_ref, fencing_token "
                "FROM sop_instances ORDER BY id"
            )
        ).mappings().all()
    assert rows[0]["active_slot_key"] == "foreground:session_active"
    assert rows[1]["active_slot_key"] is None
    assert all(row["kind"] == "sop" for row in rows)
    assert all(row["source_kind"] == "legacy" for row in rows)
    assert all(row["fencing_token"] == 0 for row in rows)
    assert "uq_execution_tenant_active_slot" in {
        item["name"] for item in inspector.get_unique_constraints("sop_instances")
    }
    assert inspector.has_table("execution_mutation_rejections")

    downgrade(mysql_database_url, "20260802_0035")
    inspector = inspect(engine)
    assert "kind" not in {
        item["name"] for item in inspector.get_columns("sop_instances")
    }
    assert not inspector.has_table("execution_mutation_rejections")
    upgrade(mysql_database_url, "20260803_0036")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM sop_instances")).scalar_one() == 2
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sop_instances "
                "(id, tenant_id, session_id, run_number, kind, active_slot_key, source_kind, "
                "status, slots_json, context_json, revision, cancellation_disposition, "
                "fencing_token, created_at, updated_at) VALUES "
                "('mysql_dynamic', 'tenant_b01', 'session_dynamic', 1, 'dynamic_task', "
                "'foreground:session_dynamic', 'api', 'running', JSON_OBJECT(), JSON_OBJECT(), "
                "0, 'none', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
    with engine.begin() as connection:
        with pytest.raises((IntegrityError, OperationalError)):
            connection.execute(
                text(
                    "INSERT INTO sop_instances "
                    "(id, tenant_id, session_id, run_number, kind, active_slot_key, "
                    "source_kind, status, slots_json, context_json, revision, "
                    "cancellation_disposition, fencing_token, created_at, updated_at) VALUES "
                    "('mysql_invalid_sop', 'tenant_b01', 'session_invalid', 1, 'sop', "
                    "'foreground:session_invalid', 'api', 'running', JSON_OBJECT(), "
                    "JSON_OBJECT(), 0, 'none', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
                )
            )


def test_mysql_execution_active_slot_and_fencing_are_cross_worker_safe(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 并发活动槽仲裁及租约过期后的旧 Operation 写拒绝与审计。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    barrier = threading.Barrier(2)

    def start_execution(version_id: str) -> tuple[str, bool] | str:
        """从独立 MySQL 事务同时申请同一 tenant/session 的活动槽。"""

        with Session(engine) as session:
            barrier.wait()
            try:
                instance, created = SopExecutionStore(session).start_instance(
                    tenant_id="tenant_b01",
                    session_id="session_race",
                    skill_id="skill_b01",
                    skill_version_id=version_id,
                    skill_version="1.0.0",
                    definition_checksum="b" * 64,
                    start_node_id="start",
                )
                session.commit()
                return instance.id, created
            except (IntegrityError, SopExecutionConflictError) as error:
                session.rollback()
                return type(error).__name__

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(start_execution, ("version-a", "version-b")))

    with Session(engine) as verify_session:
        active = verify_session.exec(
            select(SopInstance).where(
                SopInstance.tenant_id == "tenant_b01",
                SopInstance.active_slot_key == "foreground:session_race",
            )
        ).all()
        assert len(active) == 1
        instance_id = active[0].id
    assert sum(isinstance(outcome, tuple) for outcome in outcomes) == 1

    with Session(engine) as worker_a_session:
        instance_a = worker_a_session.get(SopInstance, instance_id)
        assert instance_a is not None
        store_a = SopExecutionStore(worker_a_session)
        with store_a.owned(instance_a, worker_id="mysql-worker-a") as lease_a:
            execution_a = store_a.enter_node(instance_a, "tool", input_snapshot={})
            operation_a, _ = store_a.prepare_operation(
                instance_a,
                execution_a,
                operation_name="demo.read",
                request={"record_id": "R-1"},
            )
            operation_id = operation_a.id
            worker_a_session.commit()

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE sop_instances SET lease_expires_at = "
                        "UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE id = :instance_id"
                    ),
                    {"instance_id": instance_id},
                )

            with Session(engine) as worker_b_session:
                instance_b = worker_b_session.get(SopInstance, instance_id)
                assert instance_b is not None
                with SopExecutionStore(worker_b_session).owned(
                    instance_b,
                    worker_id="mysql-worker-b",
                ) as lease_b:
                    assert lease_b.fencing_token > lease_a.fencing_token
                worker_b_session.commit()

            with pytest.raises(SopExecutionFencedError):
                store_a.start_operation(operation_a)
            worker_a_session.commit()

    with Session(engine) as verify_session:
        operation = verify_session.get(SopOperation, operation_id)
        rejection = verify_session.exec(
            select(ExecutionMutationRejection).where(
                ExecutionMutationRejection.instance_id == instance_id
            )
        ).one()
    assert operation is not None and operation.status == "prepared"
    assert rejection.action == "operation.start"
    assert rejection.rejected_fencing_token < rejection.current_fencing_token


def test_mysql_effective_interval_precision_migration_is_reversible(
    mysql_database_url: str,
) -> None:
    """验证 0029 在 MySQL 上把四类任期升为微秒精度并可回退到秒级。"""

    tables = (
        "member_org_assignments",
        "position_assignments",
        "organization_leader_assignments",
        "employee_role_assignments",
    )
    upgrade(mysql_database_url, "20260729_0028")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        inspector = inspect(engine)
        for table_name in tables:
            columns = {item["name"]: item for item in inspector.get_columns(table_name)}
            assert columns["effective_from"]["type"].fsp in {None, 0}
            assert columns["effective_until"]["type"].fsp in {None, 0}

        upgrade(mysql_database_url, "20260729_0029")
        inspector.clear_cache()
        for table_name in tables:
            columns = {item["name"]: item for item in inspector.get_columns(table_name)}
            assert columns["effective_from"]["type"].fsp == 6
            assert columns["effective_until"]["type"].fsp == 6

        downgrade(mysql_database_url, "20260729_0028")
        inspector.clear_cache()
        for table_name in tables:
            columns = {item["name"]: item for item in inspector.get_columns(table_name)}
            assert columns["effective_from"]["type"].fsp in {None, 0}
            assert columns["effective_until"]["type"].fsp in {None, 0}
    finally:
        engine.dispose()


def test_agent_identity_migration_backfills_legacy_relationships_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 0024 正式关系回填、历史 Usage 生成和迁移回退边界。"""

    upgrade(mysql_database_url, "20260728_0023")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_m4_mysql', 'M4 MySQL', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, username, display_name, role, password_hash, "
                "membership_status, member_category_code, joined_at, created_at, updated_at) VALUES "
                "('owner_m4', 'tenant_m4_mysql', 'owner', 'Owner', 'member', 'test', "
                "'active', 'employee', UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP()), "
                "('admin_m4', 'tenant_m4_mysql', 'admin', 'Admin', 'admin', 'test', "
                "'active', 'employee', UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles "
                "(id, tenant_id, name, is_overall, status, metadata_json, "
                "created_at, updated_at) VALUES "
                "('agent_m4', 'tenant_m4_mysql', 'M4 专家', 0, 'active', "
                "CAST(:metadata AS JSON), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "metadata": (
                    '{"owner_user_id":"owner_m4","published_to_gallery":true,'
                    '"gallery_published_by":"admin","employee_type":"expert"}'
                )
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, tenant_id, user_id, agent_id, slots_json, skill_stack_json, "
                "pending_tasks_json, knowledge_context_json, context_state_json, status, "
                "created_at, updated_at) VALUES "
                "('session_m4', 'tenant_m4_mysql', 'owner_m4', 'agent_m4', "
                "JSON_OBJECT(), JSON_ARRAY(), JSON_ARRAY(), JSON_ARRAY(), JSON_OBJECT(), "
                "'active', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )

    upgrade(mysql_database_url, "20260728_0024")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT owner_user_id, profile_revision, published_to_gallery, "
                "gallery_published_by, agent_category_code, visibility_scope "
                "FROM agent_profiles WHERE id = 'agent_m4'"
            )
        ).one()
        assert tuple(row) == ("owner_m4", 1, 1, "admin_m4", "professional", "tenant")
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM agent_usages "
                "WHERE tenant_id = 'tenant_m4_mysql' AND user_id = 'owner_m4' "
                "AND agent_id = 'agent_m4'"
            )
        ).scalar_one() == 1

    downgrade(mysql_database_url, "20260728_0023")
    inspector = inspect(engine)
    assert "owner_user_id" not in {
        column["name"] for column in inspector.get_columns("agent_profiles")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM agent_usages")).scalar_one() == 0


def test_knowledge_governance_migration_is_fail_closed_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 0025 仅回填可信同租户 owner，并可安全降级和再次升级。"""

    upgrade(mysql_database_url, "20260728_0024")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_m5_mysql', 'M5 MySQL', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, username, display_name, role, password_hash, "
                "membership_status, member_category_code, joined_at, created_at, updated_at) VALUES "
                "('owner_m5', 'tenant_m5_mysql', 'owner', 'Owner', 'member', 'test', "
                "'active', 'employee', UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases "
                "(id, tenant_id, name, status, metadata_json, created_at, updated_at) VALUES "
                "('kb_m5_owned', 'tenant_m5_mysql', '有主知识', 'active', "
                "CAST(:owned AS JSON), UTC_TIMESTAMP(), UTC_TIMESTAMP()), "
                "('kb_m5_unknown', 'tenant_m5_mysql', '无主知识', 'active', "
                "CAST(:unknown AS JSON), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "owned": '{"created_by_user_id":"owner_m5"}',
                "unknown": '{"created_by_user_id":"missing"}',
            },
        )

    upgrade(mysql_database_url, "20260728_0025")
    inspector = inspect(engine)
    assert "knowledge_base_org_access" in inspector.get_table_names()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, owner_user_id, access_scope, download_policy, revision "
                "FROM knowledge_bases WHERE tenant_id = 'tenant_m5_mysql' ORDER BY id"
            )
        ).all()
        assert [tuple(row) for row in rows] == [
            ("kb_m5_owned", "owner_m5", "owner", "restricted", 1),
            ("kb_m5_unknown", None, "owner", "restricted", 1),
        ]

    downgrade(mysql_database_url, "20260728_0024")
    inspector = inspect(engine)
    assert "knowledge_base_org_access" not in inspector.get_table_names()
    assert "owner_user_id" not in {
        column["name"] for column in inspector.get_columns("knowledge_bases")
    }
    upgrade(mysql_database_url, "20260728_0025")


def test_management_audit_migration_round_trips_json_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 0026 的审计 JSON、组合索引和降级重建行为。"""

    upgrade(mysql_database_url, "20260728_0025")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    upgrade(mysql_database_url, "20260729_0026")
    inspector = inspect(engine)
    assert "management_audit_logs" in inspector.get_table_names()
    assert "ix_management_audit_tenant_org_created" in {
        item["name"] for item in inspector.get_indexes("management_audit_logs")
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO management_audit_logs "
                "(id, tenant_id, actor_user_id, actor_type, actor_display_name, action, "
                "action_kind, outcome, resource_type, resource_id, target_org_unit_id, "
                "permission_code, permission_source, request_id, correlation_id, "
                "before_json, after_json, detail_json, created_at) VALUES "
                "('audit_mysql', 'tenant_mysql', 'actor_mysql', 'user', '审计员', "
                "'organization.update', 'update', 'success', 'organization_unit', "
                "'org_mysql', 'org_mysql', 'organization.manage', 'direct_role', "
                "'req_mysql', 'corr_mysql', CAST('{}' AS JSON), CAST('{}' AS JSON), "
                "CAST(:detail AS JSON), UTC_TIMESTAMP())"
            ),
            {"detail": '{"summary":"安全摘要"}'},
        )
    with engine.connect() as connection:
        detail = connection.execute(
            text(
                "SELECT JSON_UNQUOTE(JSON_EXTRACT(detail_json, '$.summary')) "
                "FROM management_audit_logs WHERE id = 'audit_mysql'"
            )
        ).scalar_one()
        assert detail == "安全摘要"

    downgrade(mysql_database_url, "20260728_0025")
    assert "management_audit_logs" not in inspect(engine).get_table_names()
    upgrade(mysql_database_url, "20260729_0026")


def test_mysql_knowledge_content_access_intersection_covers_all_read_boundaries(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 下列表、召回、引用、原文与导出共享成员和 Agent 访问交集。"""

    from fastapi import HTTPException
    from datetime import timedelta

    from app.agents.branching import (
        ensure_agent_private_knowledge_branch,
        ensure_knowledge_base_version,
    )
    from app.api.knowledge import (
        _accessible_knowledge_versions,
        get_bucket_chunks,
        get_document,
        get_document_buckets,
        list_documents,
        search_knowledge,
    )
    from app.api.knowledge_bases import export_okf, list_knowledge_bases
    from app.db.models import (
        AgentProfile,
        EmployeeProfile,
        KnowledgeBase,
        KnowledgeBaseOrgAccess,
        KnowledgeBucket,
        KnowledgeChunk,
        KnowledgeDocument,
        MemberOrgAssignment,
        OrganizationUnit,
        User,
        utc_now,
    )
    from app.knowledge.schema import KnowledgeSearchRequest
    from app.knowledge.access import resolve_knowledge_access

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as session:
        tenant = Tenant(id="tenant_m5b_mysql", name="软件研究院")
        root = OrganizationUnit(
            id="org_m5b_root",
            tenant_id=tenant.id,
            code="ROOT",
            name="软件研究院",
            unit_type_code="company",
            tree_path="org_m5b_root",
            depth=0,
            is_root=True,
            root_tenant_id=tenant.id,
        )
        project = OrganizationUnit(
            id="org_m5b_project",
            tenant_id=tenant.id,
            parent_id=root.id,
            code="PROJECT",
            name="政企项目集",
            unit_type_code="department",
            tree_path=f"{root.id}/org_m5b_project",
            depth=1,
        )
        outside_org = OrganizationUnit(
            id="org_m5b_outside",
            tenant_id=tenant.id,
            parent_id=root.id,
            code="OUTSIDE",
            name="公众研发事业部",
            unit_type_code="department",
            tree_path=f"{root.id}/org_m5b_outside",
            depth=1,
        )
        session.add(tenant)
        session.add(root)
        session.add(project)
        session.add(outside_org)
        users = {
            name: User(
                id=f"user_m5b_{name}",
                tenant_id=tenant.id,
                username=name,
                password_hash="test",
                role="admin" if name == "governor" else "member",
            )
            for name in ("inside", "outside", "governor")
        }
        for name in ("inside", "outside"):
            user = users[name]
            profile = EmployeeProfile(
                id=f"employee_m5b_{name}",
                tenant_id=tenant.id,
                user_id=user.id,
                employee_id=f"E-{name}",
            )
            session.add(user)
            session.add(profile)
            session.add(
                MemberOrgAssignment(
                    tenant_id=tenant.id,
                    employee_profile_id=profile.id,
                    org_unit_id=project.id if name == "inside" else outside_org.id,
                    effective_from=utc_now() - timedelta(days=1),
                )
            )
        session.add(users["governor"])
        agent = AgentProfile(
            id="agent_m5b_policy",
            tenant_id=tenant.id,
            name="制度数字员工",
            status="active",
            owner_user_id=users["governor"].id,
            published_to_gallery=True,
            visibility_scope="tenant",
        )
        knowledge_base = KnowledgeBase(
            id="kb_m5b_project",
            tenant_id=tenant.id,
            name="政企研发资料库",
            owner_user_id=users["governor"].id,
            access_scope="organization",
            download_policy="restricted",
        )
        session.add(agent)
        session.add(knowledge_base)
        session.add(
            KnowledgeBaseOrgAccess(
                id="access_m5b_project",
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                org_unit_id=project.id,
                include_descendants=True,
            )
        )
        session.flush()
        branch = ensure_agent_private_knowledge_branch(
            session,
            tenant.id,
            agent.id,
            knowledge_base,
        )
        version = ensure_knowledge_base_version(session, knowledge_base, branch.head_version)
        document = KnowledgeDocument(
            id="document_m5b_project",
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="project-policy.md",
            file_type="md",
            title="政企研发规范",
            status="ready",
        )
        bucket = KnowledgeBucket(
            id="bucket_m5b_project",
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=document.id,
            bucket_key="交付",
            title="交付规范",
            summary="政企项目交付前必须完成安全检查。",
        )
        chunk = KnowledgeChunk(
            id="chunk_m5b_project",
            tenant_id=tenant.id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="政企项目交付前必须完成安全检查。",
        )
        session.add(document)
        session.add(bucket)
        session.add(chunk)
        session.commit()

        inside_rows = list_knowledge_bases(
            tenant.id,
            agent.id,
            False,
            session,
            users["inside"],
        )
        outside_rows = list_knowledge_bases(
            tenant.id,
            agent.id,
            False,
            session,
            users["outside"],
        )
        documents = list_documents(
            tenant.id,
            knowledge_base.id,
            agent.id,
            False,
            session,
            users["inside"],
        )
        access_resolution = resolve_knowledge_access(
            session,
            tenant_id=tenant.id,
            current_user=users["inside"],
            agent_id=agent.id,
        )
        assert access_resolution.allowed_knowledge_base_ids == (knowledge_base.id,), (
            access_resolution.member_org_unit_ids,
            access_resolution.decisions,
        )
        visible_versions = _accessible_knowledge_versions(
            session,
            tenant_id=tenant.id,
            current_user=users["inside"],
            agent_id=agent.id,
            requested_knowledge_base_ids=[knowledge_base.id],
        )
        assert visible_versions[knowledge_base.id].id == version.id, (
            visible_versions[knowledge_base.id].id,
            version.id,
            branch.head_version,
        )
        detail = get_document(
            document.id,
            tenant.id,
            agent.id,
            session,
            users["inside"],
        )
        buckets = get_document_buckets(
            document.id,
            tenant.id,
            agent.id,
            session,
            users["inside"],
        )
        chunks = get_bucket_chunks(
            bucket.id,
            tenant.id,
            agent.id,
            session,
            users["inside"],
        )
        search = search_knowledge(
            KnowledgeSearchRequest(
                tenant_id=tenant.id,
                agent_id=agent.id,
                query="政企研发规范 安全检查",
            ),
            session,
            users["inside"],
        )
        governance_rows = list_knowledge_bases(
            tenant.id,
            None,
            True,
            session,
            users["governor"],
        )

        assert [row.id for row in inside_rows] == [knowledge_base.id]
        assert outside_rows == []
        assert [row.id for row in documents] == [document.id]
        assert detail.id == document.id
        assert [row.id for row in buckets] == [bucket.id]
        assert [row.id for row in chunks] == [chunk.id]
        assert any("安全检查" in item.content for item in search.chunks) or any(
            "安全检查" in str(item.get("content") or "")
            for item in search.evidence_pack
        )
        assert governance_rows[0].content_access_allowed is False

        for denied_call in (
            lambda: get_document(
                document.id,
                tenant.id,
                agent.id,
                session,
                users["outside"],
            ),
            lambda: export_okf(
                knowledge_base.id,
                tenant.id,
                agent.id,
                session,
                users["inside"],
            ),
        ):
            with pytest.raises(HTTPException) as caught:
                denied_call()
            assert caught.value.status_code == 404

        knowledge_base.download_policy = "allowed"
        session.add(knowledge_base)
        session.commit()
        exported = export_okf(
            knowledge_base.id,
            tenant.id,
            agent.id,
            session,
            users["inside"],
        )
        assert exported.media_type == "application/zip"


def test_member_lifecycle_migration_round_trips_existing_mysql_data(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 历史成员升级回填、码项初始化及降级均保留原 actor。"""

    upgrade(mysql_database_url, "20260727_0015")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_m1_mysql', 'M1 MySQL', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, username, display_name, role, password_hash, "
                "created_at, updated_at) VALUES "
                "('user_m1_mysql', 'tenant_m1_mysql', 'm1-user', 'M1 User', "
                "'member', 'test-only', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee_profiles "
                "(id, tenant_id, user_id, employee_id, employee_name, department_id, "
                "status, metadata_json, created_at, updated_at) VALUES "
                "('profile_m1_mysql', 'tenant_m1_mysql', 'user_m1_mysql', 'M100', "
                "'M1 Employee', NULL, 'active', JSON_OBJECT(), UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )

    upgrade(mysql_database_url, "20260728_0016")
    inspector = inspect(engine)
    with engine.connect() as connection:
        member = (
            connection.execute(
                text(
                    "SELECT membership_status, member_category_code, joined_at "
                    "FROM users WHERE id = 'user_m1_mysql'"
                )
            )
            .mappings()
            .one()
        )
        category_count = connection.execute(
            text("SELECT COUNT(*) FROM code_items WHERE tenant_id = 'tenant_m1_mysql'")
        ).scalar_one()
        assert member["membership_status"] == "active"
        assert member["member_category_code"] == "employee"
        assert member["joined_at"] is not None
        assert category_count == 5

    downgrade(mysql_database_url, "20260727_0015")
    inspector = inspect(engine)
    assert "code_sets" not in inspector.get_table_names()
    assert "code_items" not in inspector.get_table_names()
    assert "membership_status" not in {item["name"] for item in inspector.get_columns("users")}
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM users WHERE id = 'user_m1_mysql'")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM employee_profiles WHERE id = 'profile_m1_mysql'")
            ).scalar_one()
            == 1
        )


def test_organization_unit_migration_round_trips_existing_mysql_tenant(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 历史租户升级生成唯一根和类型码表，降级不删除租户。"""

    upgrade(mysql_database_url, "20260728_0016")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_m2_mysql', 'M2 MySQL', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )

    upgrade(mysql_database_url, "20260728_0017")
    with engine.connect() as connection:
        root = (
            connection.execute(
                text(
                    "SELECT id, code, name, tree_path, depth, root_tenant_id "
                    "FROM organization_units WHERE tenant_id = 'tenant_m2_mysql'"
                )
            )
            .mappings()
            .one()
        )
        assert root["code"] == "ROOT"
        assert root["name"] == "M2 MySQL"
        assert root["tree_path"] == root["id"]
        assert root["depth"] == 0
        assert root["root_tenant_id"] == "tenant_m2_mysql"
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci "
                    "JOIN code_sets cs ON cs.id = ci.code_set_id "
                    "WHERE cs.tenant_id = 'tenant_m2_mysql' "
                    "AND cs.set_code = 'organization_unit_type'"
                )
            ).scalar_one()
            == 6
        )

    downgrade(mysql_database_url, "20260728_0016")
    inspector = inspect(engine)
    assert "organization_units" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM tenants WHERE id = 'tenant_m2_mysql'")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM code_sets WHERE set_code = 'organization_unit_type'")
            ).scalar_one()
            == 0
        )


def test_organization_assignment_migration_maps_legacy_departments_on_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 将稳定旧部门映射成组织，把未知值归根并留下治理报告。"""

    upgrade(mysql_database_url, "20260728_0017")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_m2b_mysql', 'M2-B MySQL', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, username, display_name, role, membership_status, "
                "member_category_code, joined_at, left_at, password_hash, created_at, updated_at) "
                "VALUES ('user_m2b_1', 'tenant_m2b_mysql', 'm2b-1', 'M2B One', 'member', "
                "'active', 'employee', UTC_TIMESTAMP(), NULL, 'test-only', "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP()), "
                "('user_m2b_2', 'tenant_m2b_mysql', 'm2b-2', 'M2B Two', 'member', "
                "'active', 'employee', UTC_TIMESTAMP(), NULL, 'test-only', "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee_profiles "
                "(id, tenant_id, user_id, employee_id, employee_name, department_id, "
                "status, join_date, leave_date, metadata_json, created_at, updated_at) VALUES "
                "('profile_m2b_1', 'tenant_m2b_mysql', 'user_m2b_1', 'M2B01', "
                "'M2B One', 'FINANCE', 'active', UTC_TIMESTAMP(), NULL, JSON_OBJECT(), "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP()), "
                "('profile_m2b_2', 'tenant_m2b_mysql', 'user_m2b_2', 'M2B02', "
                "'M2B Two', '中文部门', 'active', UTC_TIMESTAMP(), NULL, JSON_OBJECT(), "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            )
        )
        root_id = "orgroot_m2b_mysql"
        connection.execute(
            text(
                "INSERT INTO organization_units "
                "(id, tenant_id, parent_id, code, name, unit_type_code, tree_path, depth, "
                "sort_order, is_root, root_tenant_id, status, created_at, updated_at) "
                "VALUES (:id, 'tenant_m2b_mysql', NULL, 'ROOT', 'M2-B MySQL', 'company', "
                ":path, 0, 0, TRUE, 'tenant_m2b_mysql', 'active', "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {"id": root_id, "path": root_id},
        )

    upgrade(mysql_database_url)
    with engine.connect() as connection:
        assignments = connection.execute(
            text(
                "SELECT moa.employee_profile_id, ou.code "
                "FROM member_org_assignments moa "
                "JOIN organization_units ou ON ou.id = moa.org_unit_id "
                "WHERE moa.tenant_id = 'tenant_m2b_mysql' "
                "ORDER BY moa.employee_profile_id"
            )
        ).all()
        assert assignments == [("profile_m2b_1", "FINANCE"), ("profile_m2b_2", "ROOT")]
        assert (
            connection.execute(
                text(
                    "SELECT issue_code FROM organization_migration_issues "
                    "WHERE employee_profile_id = 'profile_m2b_2'"
                )
            ).scalar_one()
            == "UNRECOGNIZED_LEGACY_DEPARTMENT"
        )
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci JOIN code_sets cs "
                    "ON cs.id = ci.code_set_id WHERE cs.tenant_id = 'tenant_m2b_mysql' "
                    "AND cs.set_code = 'position_type'"
                )
            ).scalar_one()
            == 5
        )

    downgrade(mysql_database_url, "20260728_0017")
    inspector = inspect(engine)
    assert "member_org_assignments" not in inspector.get_table_names()
    assert "organization_units" in inspector.get_table_names()


def test_mysql_round_trips_unicode_and_longtext(mysql_database_url: str) -> None:
    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    content = "中文🙂" + ("长文本" * 100_000)

    with Session(engine) as session:
        session.add(
            Message(
                tenant_id="tenant_test",
                session_id="session_test",
                role="user",
                content=content,
            )
        )
        session.commit()
        stored = session.exec(select(Message)).one()

    assert stored.content == content


def test_demo_seed_is_idempotent_on_mysql(mysql_database_url: str) -> None:
    """验证 MySQL 种子幂等，并保持超标特批 v2 到 v2.1 的不可变派生链。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)

    with Session(engine) as session:
        seed_demo_data(session)
        seed_demo_data(session)
        tenant_count = session.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id = 'tenant_demo'")
        ).scalar_one()
        overall_agent_count = session.execute(
            text("SELECT COUNT(*) FROM agent_profiles WHERE id = 'agent_tenant_demo_overall'")
        ).scalar_one()
        expense_versions = session.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id == "expense_over_limit_approval")
            .order_by(SkillVersion.version)
        ).all()

    assert tenant_count == 1
    assert overall_agent_count == 1
    v2 = next(version for version in expense_versions if version.version == "2.0.0")
    v21 = next(version for version in expense_versions if version.version == "2.1.0")
    v2_nodes = {
        node["node_id"]: node for node in v2.content_json["nodes"] if "node_id" in node
    }
    v21_nodes = {
        node["node_id"]: node
        for node in v21.content_json["nodes"]
        if "node_id" in node
    }
    assert v21.derived_from_version_id == v2.id
    assert (
        v2_nodes["department_special_approval"]["metadata"]["participant_policy"].get(
            "participant_scope_resolver"
        )
        is None
    )
    assert v21_nodes["department_special_approval"]["metadata"]["participant_policy"][
        "participant_scope_resolver"
    ] == "initiator_primary_org_subtree"
    assert (
        v21_nodes["finance_special_approval"]["metadata"]["participant_policy"].get(
            "participant_scope_resolver"
        )
        is None
    )


def test_legacy_published_snapshot_repair_is_evidence_gated_on_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 0027 只按历史指纹补齐快照，重复升级不复制不可变事实。"""

    legacy_skill_ids = (
        "after_sales_exchange",
        "after_sales_refund",
        "skill_price_compare_001",
        "skill_purchase_001",
    )
    upgrade(mysql_database_url, "20260729_0026")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as session:
        _seed_legacy_published_skills(session)
        session.execute(
            text(
                "DELETE FROM skill_versions WHERE tenant_id = 'tenant_demo' "
                "AND skill_id IN "
                "('after_sales_exchange', 'after_sales_refund', "
                "'skill_price_compare_001', 'skill_purchase_001')"
            )
        )
        session.commit()

    upgrade(mysql_database_url, "20260729_0027")
    with Session(engine) as session:
        snapshots = session.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id.in_(legacy_skill_ids))
            .order_by(SkillVersion.skill_id)
        ).all()
        first_ids = [snapshot.id for snapshot in snapshots]

        assert len(snapshots) == 4
        assert all(snapshot.content_checksum for snapshot in snapshots)
        assert all(snapshot.compiled_definition_checksum for snapshot in snapshots)
        assert all(snapshot.meta_model_version == 1 for snapshot in snapshots)
        assert all(snapshot.source_schema_version == 2 for snapshot in snapshots)

    downgrade(mysql_database_url, "20260729_0026")
    upgrade(mysql_database_url, "20260729_0027")
    with Session(engine) as session:
        snapshots = session.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id.in_(legacy_skill_ids))
            .order_by(SkillVersion.skill_id)
        ).all()
        assert [snapshot.id for snapshot in snapshots] == first_ids


def test_m55_all_published_heads_upgrade_and_dependencies_are_idempotent_on_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 上正式发布头、分支同步和重复应用的一致性。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as session:
        seed_demo_data(session)
        session.commit()
        first = apply_m55_published_head_upgrade(
            session,
            tenant_id="tenant_demo",
            require_all=True,
        )
        session.commit()
        first_version_count = session.exec(
            select(SkillVersion).where(SkillVersion.tenant_id == "tenant_demo")
        ).all()
        inventory = build_sop_migration_inventory(session, "tenant_demo")

        assert len(first.migrated_skill_ids) == len(M55_SOURCE_VERSIONS)
        assert inventory.disposition_counts == {
            "no_migration": len(M55_SOURCE_VERSIONS),
            "auto_new_version": 0,
            "business_confirmation": 0,
            "temporarily_unsupported": 0,
        }, [
            (
                entry.skill_id,
                entry.reason_code,
                entry.dependency_assessment.issue_codes,
            )
            for entry in inventory.entries
            if entry.disposition != "no_migration"
        ]
        assert inventory.dependency_counts["blocked"] == 0

        second = apply_m55_published_head_upgrade(
            session,
            tenant_id="tenant_demo",
            require_all=True,
        )
        session.commit()
        second_version_count = session.exec(
            select(SkillVersion).where(SkillVersion.tenant_id == "tenant_demo")
        ).all()

        assert second.migrated_skill_ids == ()
        assert len(second.already_migrated_skill_ids) == len(M55_SOURCE_VERSIONS)
        assert len(second_version_count) == len(first_version_count)


def test_mysql_org_scoped_expense_candidates_and_claim_race(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 上代表 SOP 的部门子树候选和双事务认领互斥。"""

    from app.db.models import SopWorkItem
    from app.sop_runtime.work_items import SopWorkItemService, WorkItemError
    from tests.test_expense_special_approval_org_scope import (
        _seed_representative_runtime,
    )

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    _seed_representative_runtime(engine)
    with Session(engine) as session:
        item = session.exec(
            select(SopWorkItem).where(
                SopWorkItem.node_id == "department_special_approval"
            )
        ).one()
        assert [candidate.user_id for candidate in SopWorkItemService(session).candidates(item)] == [
            "department_child",
            "department_same",
        ]

    barrier = threading.Barrier(2)

    def claim(actor_user_id: str) -> str:
        """从独立 MySQL 事务同时认领相同 offered revision。"""

        with Session(engine) as session:
            item = session.exec(
                select(SopWorkItem).where(
                    SopWorkItem.node_id == "department_special_approval"
                )
            ).one()
            barrier.wait()
            try:
                SopWorkItemService(session).claim(
                    item,
                    actor_user_id=actor_user_id,
                    command_id=f"mysql-race-{actor_user_id}",
                    expected_revision=0,
                )
                session.commit()
                return "claimed"
            except WorkItemError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(claim, ("department_same", "department_child"))
        )

    assert sorted(outcomes) == ["WORK_ITEM_ALREADY_CLAIMED", "claimed"]
    with Session(engine) as session:
        item = session.exec(
            select(SopWorkItem).where(
                SopWorkItem.node_id == "department_special_approval"
            )
        ).one()
        assert item.status == "claimed"
        assert item.revision == 1


def test_mysql_crud_and_rollback(mysql_database_url: str) -> None:
    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    message = Message(
        id="msg_crud",
        tenant_id="tenant_test",
        session_id="session_test",
        role="user",
        content="initial",
    )
    with Session(engine) as session:
        session.add(message)
        session.commit()
        stored = session.get(Message, "msg_crud")
        assert stored is not None
        stored.content = "updated"
        session.add(stored)
        session.commit()
        updated = session.get(Message, "msg_crud")
        assert updated is not None
        assert updated.content == "updated"
        session.delete(stored)
        session.commit()
        assert session.get(Message, "msg_crud") is None

        session.add(
            Message(
                id="msg_rollback",
                tenant_id="tenant_test",
                session_id="session_test",
                role="user",
                content="rollback",
            )
        )
        session.flush()
        session.rollback()
        assert session.get(Message, "msg_rollback") is None


def test_mysql_stale_revision_is_rejected(mysql_database_url: str) -> None:
    from app.db.migrations import SchemaNotCurrentError, assert_schema_current

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'stale_revision'"))

    with pytest.raises(SchemaNotCurrentError, match="alembic -c alembic.ini upgrade head"):
        assert_schema_current(engine, expected_head="20260718_0001")


def test_mysql_knowledge_statements_return_text(mysql_database_url: str) -> None:
    from app.api.knowledge import _bucket_chunk_statement, _document_bucket_statement
    from app.db.models import KnowledgeBucket, KnowledgeChunk

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    bucket = KnowledgeBucket(
        tenant_id="tenant_test",
        knowledge_base_id="kb_test",
        document_id="doc_test",
        bucket_key="主题🙂",
        title="中文标题",
        summary="中文摘要🙂",
    )
    chunk = KnowledgeChunk(
        tenant_id="tenant_test",
        knowledge_base_id="kb_test",
        document_id="doc_test",
        bucket_id=bucket.id,
        chunk_index=0,
        content="中文正文🙂",
        summary="片段摘要",
    )
    with Session(engine) as session:
        session.add(bucket)
        session.add(chunk)
        session.commit()
        bucket_row = (
            session.execute(_document_bucket_statement("mysql", "tenant_test", "doc_test"))
            .mappings()
            .one()
        )
        chunk_row = (
            session.execute(_bucket_chunk_statement("mysql", "tenant_test", bucket.id))
            .mappings()
            .one()
        )

    assert bucket_row["summary"] == "中文摘要🙂"
    assert chunk_row["content"] == "中文正文🙂"
    assert isinstance(chunk_row["content"], str)


def test_mysql_engine_pool_survives_dispose(mysql_database_url: str) -> None:
    from app.config import Settings
    from app.db.factory import SQLAlchemyDatabaseAdapterFactory

    runtime = SQLAlchemyDatabaseAdapterFactory.create(
        mysql_database_url,
        Settings(_env_file=None, public_mock_api_key="test-key"),
    )
    engine = runtime.engine
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_sop_migration_preview_is_deterministic_and_read_only_mysql(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 预检覆盖全部发布头且不改写版本、实例或工作项。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as session:
        seed_demo_data(session)
        before = (
            [(row.id, row.version, row.status) for row in session.exec(select(SkillVersion)).all()],
            [(row.id, row.skill_version_id, row.revision) for row in session.exec(select(SopInstance)).all()],
            [
                (row.id, row.skill_version_id, row.revision, row.candidate_snapshot_json)
                for row in session.exec(select(SopWorkItem)).all()
            ],
        )

        first = build_sop_migration_inventory(session, "tenant_demo")
        second = build_sop_migration_inventory(session, "tenant_demo")
        published_heads = session.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.status == "published",
            )
        ).all()
        after = (
            [(row.id, row.version, row.status) for row in session.exec(select(SkillVersion)).all()],
            [(row.id, row.skill_version_id, row.revision) for row in session.exec(select(SopInstance)).all()],
            [
                (row.id, row.skill_version_id, row.revision, row.candidate_snapshot_json)
                for row in session.exec(select(SopWorkItem)).all()
            ],
        )

    assert first == second
    assert first.total == len(published_heads)
    assert first.total == len(first.entries)
    assert sum(first.dependency_counts.values()) == first.total
    assert all(entry.dependency_assessment is not None for entry in first.entries)
    by_skill_id = {entry.skill_id: entry for entry in first.entries}
    assert (
        by_skill_id["expense_over_limit_approval"]
        .dependency_assessment.executable_agent_count
        >= 1
    )
    assert (
        by_skill_id["skill_graph_visual_demo"]
        .dependency_assessment.executable_agent_count
        >= 1
    )
    assert before == after


def test_mysql_serializes_default_model_switches(mysql_database_url: str) -> None:
    """验证 MySQL 同租户双线程切换均完成，且唯一索引拒绝第二个默认模型。"""

    upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    with Session(engine) as session:
        session.add(Tenant(id="tenant_models", name="Model Tenant"))
        session.add_all(
            [
                ModelConfig(
                    id="model_previous",
                    tenant_id="tenant_models",
                    name="Previous",
                    api_key_encrypted="",
                    model="previous",
                    is_default=True,
                ),
                ModelConfig(
                    id="model_a",
                    tenant_id="tenant_models",
                    name="Model A",
                    api_key_encrypted="",
                    model="a",
                ),
                ModelConfig(
                    id="model_b",
                    tenant_id="tenant_models",
                    name="Model B",
                    api_key_encrypted="",
                    model="b",
                ),
            ]
        )
        session.commit()

    barrier = threading.Barrier(2)

    def switch(model_id: str) -> str:
        """在独立 MySQL 会话中同步发起一次默认模型切换。"""

        barrier.wait()
        with Session(engine) as session:
            return set_default_model_config(
                model_id,
                tenant_id="tenant_models",
                db=session,
            ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        switched = set(executor.map(switch, ("model_a", "model_b")))

    with Session(engine) as session:
        defaults = session.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == "tenant_models",
                ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
            )
        ).all()
        session.add(
            ModelConfig(
                id="model_duplicate",
                tenant_id="tenant_models",
                name="Duplicate",
                api_key_encrypted="",
                model="duplicate",
                is_default=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    assert switched == {"model_a", "model_b"}
    assert len(defaults) == 1
