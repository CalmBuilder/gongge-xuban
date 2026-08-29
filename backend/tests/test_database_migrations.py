"""
@Time       : 2026/08/10 18:30
@Author     : zhanglp8181
@File       : test_database_migrations.py
@CallChain  : pytest → Alembic/数据库适配器 → SQLite/MySQL schema
@Description: 验证迁移版本守卫、连接错误脱敏以及关键升降级数据兼容性。
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, MetaData, Table, create_engine, inspect, make_url, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, select

from app.config import Settings
from app.db.factory import SQLAlchemyDatabaseAdapterFactory
from app.db.migrations import (
    DatabaseConnectionError,
    SchemaNotCurrentError,
    assert_schema_current,
    current_revision,
)
from app.db.models import ExecutionCommand, Skill, SkillVersion
from app.db.seed import (
    EXCHANGE_SKILL,
    PRICE_COMPARE_SKILL,
    PURCHASE_SKILL,
    REFUND_SKILL,
    _skill_content_graph,
)
from app.db.sqlite_legacy import initialize_sqlite_database, migrate_sqlite_skill_schema
from app.db.sqlite_legacy import (
    _migrate_dynamic_capability_fields,
    _migrate_execution_control_fields,
    _migrate_execution_plan_fields,
    _migrate_execution_reliability_fields,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_current_revision_returns_none_without_version_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    assert current_revision(engine) is None


def test_schema_guard_rejects_missing_revision() -> None:
    engine = create_engine("sqlite:///:memory:")

    with pytest.raises(SchemaNotCurrentError) as caught:
        assert_schema_current(engine, expected_head="20260718_0001")

    message = str(caught.value)
    assert "current=<none>" in message
    assert "head=20260718_0001" in message
    assert "alembic -c alembic.ini upgrade head" in message


def test_schema_guard_accepts_matching_revision() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260718_0001')"))

    assert_schema_current(engine, expected_head="20260718_0001")


def test_revision_connection_error_hides_password() -> None:
    engine = Mock()
    engine.url = make_url("mysql+pymysql://gongge_xuban:real-password@db:3306/gongge_xuban")
    engine.connect.side_effect = OperationalError("SELECT 1", {}, RuntimeError("access denied"))

    with pytest.raises(DatabaseConnectionError) as caught:
        current_revision(engine)

    assert "***@db:3306/gongge_xuban" in str(caught.value)
    assert "real-password" not in str(caught.value)


def test_sqlite_adapter_keeps_create_and_legacy_path(tmp_path) -> None:
    runtime = SQLAlchemyDatabaseAdapterFactory.create(
        f"sqlite:///{tmp_path / 'desktop.db'}",
        Settings(_env_file=None, public_mock_api_key="test-key"),
    )

    runtime.initialize()

    assert "messages" in inspect(runtime.engine).get_table_names()
    with runtime.engine.connect() as connection:
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 30000


def test_member_lifecycle_migration_upgrades_and_downgrades_sqlite(tmp_path) -> None:
    """验证历史成员升级时保留 actor 并回填生命周期，降级后完整移除新增结构。"""

    database_url = f"sqlite:///{tmp_path / 'member-lifecycle.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260727_0015')"))
        connection.execute(
            text(
                "CREATE TABLE tenants ("
                "id VARCHAR(128) PRIMARY KEY, name VARCHAR(191) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "username VARCHAR(191) NOT NULL, display_name VARCHAR(191), "
                "role VARCHAR(64) NOT NULL, password_hash VARCHAR(512) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE employee_profiles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128) NOT NULL, employee_id VARCHAR(128) NOT NULL, "
                "employee_name VARCHAR(191), department_id VARCHAR(128), "
                "status VARCHAR(64) NOT NULL, metadata_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_demo', 'Demo', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, username, display_name, role, password_hash, created_at, updated_at) "
                "VALUES "
                "('user_existing', 'tenant_demo', 'existing', 'Existing', 'member', 'hash', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260728_0016")
    inspector = inspect(engine)
    assert {"code_sets", "code_items"}.issubset(inspector.get_table_names())
    assert {"membership_status", "member_category_code", "joined_at", "left_at"}.issubset(
        {column["name"] for column in inspector.get_columns("users")}
    )
    with engine.connect() as connection:
        member = (
            connection.execute(
                text(
                    "SELECT id, membership_status, member_category_code, joined_at "
                    "FROM users WHERE id = 'user_existing'"
                )
            )
            .mappings()
            .one()
        )
        assert member["id"] == "user_existing"
        assert member["membership_status"] == "active"
        assert member["member_category_code"] == "employee"
        assert member["joined_at"] is not None
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci "
                    "JOIN code_sets cs ON cs.id = ci.code_set_id "
                    "WHERE cs.tenant_id = 'tenant_demo' AND cs.set_code = 'member_category'"
                )
            ).scalar_one()
            == 5
        )

    command.downgrade(config, "20260727_0015")
    inspector = inspect(engine)
    assert "code_sets" not in inspector.get_table_names()
    assert "membership_status" not in {column["name"] for column in inspector.get_columns("users")}


def test_organization_unit_migration_seeds_one_root_and_downgrades_sqlite(
    tmp_path,
) -> None:
    """验证历史租户升级只生成一个根组织和六个类型码项，降级不删除租户。"""

    database_url = f"sqlite:///{tmp_path / 'organization-units.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0016')"))
        connection.execute(
            text(
                "CREATE TABLE tenants ("
                "id VARCHAR(128) PRIMARY KEY, name VARCHAR(191) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_sets ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "set_code VARCHAR(128) NOT NULL, name VARCHAR(191) NOT NULL, "
                "description VARCHAR(1024), allow_custom_items BOOLEAN NOT NULL, "
                "is_system BOOLEAN NOT NULL, status VARCHAR(64) NOT NULL, "
                "revision INTEGER NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_items ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "code_set_id VARCHAR(128) NOT NULL, item_code VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, description VARCHAR(1024), "
                "parent_item_id VARCHAR(128), sort_order INTEGER NOT NULL, "
                "is_builtin BOOLEAN NOT NULL, status VARCHAR(64) NOT NULL, "
                "metadata_json JSON NOT NULL, revision INTEGER NOT NULL, "
                "created_by_user_id VARCHAR(128), updated_by_user_id VARCHAR(128), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_org', '组织测试企业', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260728_0017")
    inspector = inspect(engine)
    assert "organization_units" in inspector.get_table_names()
    with engine.connect() as connection:
        root = (
            connection.execute(
                text(
                    "SELECT id, code, name, tree_path, depth, root_tenant_id "
                    "FROM organization_units WHERE tenant_id = 'tenant_org'"
                )
            )
            .mappings()
            .one()
        )
        assert root["code"] == "ROOT"
        assert root["name"] == "组织测试企业"
        assert root["tree_path"] == root["id"]
        assert root["depth"] == 0
        assert root["root_tenant_id"] == "tenant_org"
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci "
                    "JOIN code_sets cs ON cs.id = ci.code_set_id "
                    "WHERE cs.tenant_id = 'tenant_org' "
                    "AND cs.set_code = 'organization_unit_type'"
                )
            ).scalar_one()
            == 6
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organization_units "
                    "(id, tenant_id, parent_id, code, name, unit_type_code, tree_path, "
                    "depth, sort_order, is_root, root_tenant_id, status, created_at, updated_at) "
                    "VALUES ('second_root', 'tenant_org', NULL, 'SECOND_ROOT', '错误根', "
                    "'company', 'second_root', 0, 0, 1, 'tenant_org', 'active', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )

    command.downgrade(config, "20260728_0016")
    inspector = inspect(engine)
    assert "organization_units" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM tenants WHERE id = 'tenant_org'")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM code_sets WHERE set_code = 'organization_unit_type'")
            ).scalar_one()
            == 0
        )


def test_organization_assignment_migration_maps_legacy_departments_sqlite(
    tmp_path,
) -> None:
    """验证可识别旧部门生成组织归属，未知值归根并进入迁移治理报告。"""

    database_url = f"sqlite:///{tmp_path / 'organization-assignments.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0017')"))
        connection.execute(
            text(
                "CREATE TABLE tenants (id VARCHAR(128) PRIMARY KEY, name VARCHAR(191) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE employee_profiles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128) NOT NULL, employee_id VARCHAR(128) NOT NULL, "
                "employee_name VARCHAR(191), department_id VARCHAR(128), "
                "status VARCHAR(64) NOT NULL, join_date DATETIME NOT NULL, "
                "leave_date DATETIME, metadata_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_sets ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "set_code VARCHAR(128) NOT NULL, name VARCHAR(191) NOT NULL, "
                "description VARCHAR(1024), allow_custom_items BOOLEAN NOT NULL, "
                "is_system BOOLEAN NOT NULL, status VARCHAR(64) NOT NULL, revision INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_items ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "code_set_id VARCHAR(128) NOT NULL, item_code VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, description VARCHAR(1024), parent_item_id VARCHAR(128), "
                "sort_order INTEGER NOT NULL, is_builtin BOOLEAN NOT NULL, status VARCHAR(64) NOT NULL, "
                "metadata_json JSON NOT NULL, revision INTEGER NOT NULL, "
                "created_by_user_id VARCHAR(128), updated_by_user_id VARCHAR(128), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE organization_units ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "parent_id VARCHAR(128), code VARCHAR(128) NOT NULL, name VARCHAR(191) NOT NULL, "
                "unit_type_code VARCHAR(128) NOT NULL, tree_path TEXT NOT NULL, depth INTEGER NOT NULL, "
                "sort_order INTEGER NOT NULL, is_root BOOLEAN NOT NULL, root_tenant_id VARCHAR(128), "
                "status VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tenants VALUES "
                "('tenant_assign', '任职企业', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO organization_units VALUES "
                "('root_assign', 'tenant_assign', NULL, 'ROOT', '任职企业', 'company', "
                "'root_assign', 0, 0, 1, 'tenant_assign', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee_profiles VALUES "
                "('profile_known', 'tenant_assign', 'user_known', 'E001', '已知', 'FINANCE', "
                "'active', CURRENT_TIMESTAMP, NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('profile_unknown', 'tenant_assign', 'user_unknown', 'E002', '未知', '中文部门', "
                "'active', CURRENT_TIMESTAMP, NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('profile_empty', 'tenant_assign', 'user_empty', 'E003', '空部门', NULL, "
                "'active', CURRENT_TIMESTAMP, NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260728_0018")
    inspector = inspect(engine)
    assert {
        "member_org_assignments",
        "positions",
        "position_assignments",
        "organization_migration_issues",
    }.issubset(inspector.get_table_names())
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT moa.employee_profile_id, ou.code "
                "FROM member_org_assignments moa "
                "JOIN organization_units ou ON ou.id = moa.org_unit_id "
                "ORDER BY moa.employee_profile_id"
            )
        ).all()
        assert rows == [
            ("profile_empty", "ROOT"),
            ("profile_known", "FINANCE"),
            ("profile_unknown", "ROOT"),
        ]
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM organization_migration_issues")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci JOIN code_sets cs "
                    "ON cs.id = ci.code_set_id WHERE cs.set_code = 'position_type'"
                )
            ).scalar_one()
            == 5
        )

    command.downgrade(config, "20260728_0017")
    inspector = inspect(engine)
    assert "member_org_assignments" not in inspector.get_table_names()
    assert "organization_units" in inspector.get_table_names()


def test_position_role_binding_migration_round_trips_sqlite(tmp_path) -> None:
    """验证岗位默认角色绑定的唯一约束以及 0019 到 0018 的可逆边界。"""

    database_url = f"sqlite:///{tmp_path / 'position-role-bindings.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0018')"))

    command.upgrade(config, "20260728_0019")
    assert "position_role_bindings" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        statement = text(
            "INSERT INTO position_role_bindings "
            "(id, tenant_id, position_id, business_role_id, scope_mode, status, "
            "created_at, updated_at) VALUES "
            "(:id, 'tenant_a', 'position_a', 'role_a', 'position_org', 'active', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(statement, {"id": "binding_a"})
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(statement, {"id": "binding_duplicate"})

    command.downgrade(config, "20260728_0018")
    assert "position_role_bindings" not in inspect(engine).get_table_names()


def test_organization_leader_migration_round_trips_sqlite(tmp_path) -> None:
    """验证 0020 初始化负责人类型、不推断负责人并可回滚至 0019。"""

    database_url = f"sqlite:///{tmp_path / 'organization-leaders.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0019')"))
        connection.execute(
            text(
                "CREATE TABLE tenants (id VARCHAR(128) PRIMARY KEY, name VARCHAR(255), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_sets (id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
                "set_code VARCHAR(128), name VARCHAR(255), description TEXT, "
                "allow_custom_items BOOLEAN, is_system BOOLEAN, status VARCHAR(64), "
                "revision INTEGER, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE code_items (id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
                "code_set_id VARCHAR(512), item_code VARCHAR(128), name VARCHAR(255), "
                "description TEXT, parent_item_id VARCHAR(512), sort_order INTEGER, "
                "is_builtin BOOLEAN, status VARCHAR(64), metadata_json TEXT, revision INTEGER, "
                "created_by_user_id VARCHAR(128), updated_by_user_id VARCHAR(128), "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) "
                "VALUES ('tenant_leader', '负责人测试', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260728_0020")
    assert "organization_leader_assignments" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM code_items ci JOIN code_sets cs "
                    "ON cs.id = ci.code_set_id "
                    "WHERE cs.tenant_id = 'tenant_leader' "
                    "AND cs.set_code = 'organization_leader_type'"
                )
            ).scalar_one()
            == 4
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM organization_leader_assignments")
            ).scalar_one()
            == 0
        )

    command.downgrade(config, "20260728_0019")
    assert "organization_leader_assignments" not in inspect(engine).get_table_names()


def test_organization_query_index_migration_round_trips_sqlite(tmp_path) -> None:
    """验证 0021 只增加大组织组合索引，并可无损回滚至 0020。"""

    database_url = f"sqlite:///{tmp_path / 'organization-query-indexes.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    table_columns = {
        "organization_units": (
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
            "parent_id VARCHAR(512), status VARCHAR(64), sort_order INTEGER"
        ),
        "member_org_assignments": (
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
            "org_unit_id VARCHAR(128), employee_profile_id VARCHAR(128), "
            "status VARCHAR(64), effective_until DATETIME"
        ),
        "positions": (
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
            "org_unit_id VARCHAR(128), status VARCHAR(64), code VARCHAR(128)"
        ),
        "position_assignments": (
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
            "position_id VARCHAR(128), employee_profile_id VARCHAR(128), "
            "status VARCHAR(64), effective_until DATETIME"
        ),
        "organization_leader_assignments": (
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
            "org_unit_id VARCHAR(128), employee_profile_id VARCHAR(128), "
            "status VARCHAR(64), effective_until DATETIME"
        ),
    }
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0020')"))
        for table_name, columns in table_columns.items():
            connection.execute(text(f"CREATE TABLE {table_name} ({columns})"))

    command.upgrade(config, "20260728_0021")
    inspector = inspect(engine)
    assert "ix_org_unit_tenant_parent_status_sort" in {
        row["name"] for row in inspector.get_indexes("organization_units")
    }
    assert "ix_member_org_tenant_org_current" in {
        row["name"] for row in inspector.get_indexes("member_org_assignments")
    }
    assert "ix_org_leader_tenant_org_current" in {
        row["name"] for row in inspector.get_indexes("organization_leader_assignments")
    }

    command.downgrade(config, "20260728_0020")
    inspector = inspect(engine)
    assert "ix_org_unit_tenant_parent_status_sort" not in {
        row["name"] for row in inspector.get_indexes("organization_units")
    }
    assert "organization_leader_assignments" in inspector.get_table_names()


def test_governance_role_scope_migration_round_trips_sqlite(tmp_path) -> None:
    """验证 0022 回填业务角色类型并无损增加结构化治理授权字段。"""

    database_url = f"sqlite:///{tmp_path / 'governance-role-scopes.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0021')"))
        connection.execute(
            text(
                "CREATE TABLE business_roles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), role_code VARCHAR(128))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE employee_role_assignments ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128), "
                "employee_profile_id VARCHAR(128), business_role_id VARCHAR(128), "
                "status VARCHAR(64), effective_until DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO business_roles (id, tenant_id, role_code) "
                "VALUES ('role_old', 'tenant_a', 'finance_reviewer')"
            )
        )

    command.upgrade(config, "20260728_0022")
    inspector = inspect(engine)
    assert {"role_kind"}.issubset(
        {column["name"] for column in inspector.get_columns("business_roles")}
    )
    assert {"include_descendants", "granted_by_user_id"}.issubset(
        {column["name"] for column in inspector.get_columns("employee_role_assignments")}
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT role_kind FROM business_roles WHERE id = 'role_old'")
            ).scalar_one()
            == "business"
        )

    command.downgrade(config, "20260728_0021")
    inspector = inspect(engine)
    assert "role_kind" not in {column["name"] for column in inspector.get_columns("business_roles")}
    assert "include_descendants" not in {
        column["name"] for column in inspector.get_columns("employee_role_assignments")
    }


def test_sop_participant_scope_migration_round_trips_sqlite(tmp_path) -> None:
    """验证 0023 为既有工作项安全回填空范围快照，并可独立回退。"""

    database_url = f"sqlite:///{tmp_path / 'sop-participant-scope.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0022')"))
        connection.execute(
            text(
                "CREATE TABLE sop_work_items ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO sop_work_items (id, tenant_id) VALUES ('work_item_old', 'tenant_a')")
        )

    command.upgrade(config, "20260728_0023")
    inspector = inspect(engine)
    assert "participant_scope_snapshot_json" in {
        column["name"] for column in inspector.get_columns("sop_work_items")
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT participant_scope_snapshot_json FROM sop_work_items "
                    "WHERE id = 'work_item_old'"
                )
            ).scalar_one()
            == "{}"
        )

    command.downgrade(config, "20260728_0022")
    inspector = inspect(engine)
    assert "participant_scope_snapshot_json" not in {
        column["name"] for column in inspector.get_columns("sop_work_items")
    }


def test_agent_identity_migration_backfills_legacy_relationships_sqlite(tmp_path) -> None:
    """验证 0024 回填正式 Agent 关系与会话 Usage，并能只撤销迁移生成的事实。"""

    database_url = f"sqlite:///{tmp_path / 'agent-identity.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0023')"))
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "username VARCHAR(191) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_profiles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, is_overall BOOLEAN NOT NULL, "
                "metadata_json JSON, status VARCHAR(64) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_usages ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128) NOT NULL, agent_id VARCHAR(128) NOT NULL, "
                "metadata_json JSON, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_agent_usage_user_agent UNIQUE (tenant_id, user_id, agent_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128), agent_id VARCHAR(128), created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, tenant_id, username) VALUES "
                "('owner_a', 'tenant_a', 'owner'), ('admin_a', 'tenant_a', 'admin')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles "
                "(id, tenant_id, name, is_overall, metadata_json, status) VALUES "
                "('agent_expert', 'tenant_a', '专家', 0, :expert_metadata, 'active'),"
                "('agent_overall', 'tenant_a', '整体', 1, :overall_metadata, 'active')"
            ),
            {
                "expert_metadata": (
                    '{"owner_user_id":"owner_a","published_to_gallery":true,'
                    '"gallery_published_by":"admin","employee_type":"expert"}'
                ),
                "overall_metadata": "{}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, tenant_id, user_id, agent_id, created_at, updated_at) VALUES "
                "('session_old', 'tenant_a', 'owner_a', 'agent_expert', "
                "'2026-07-01 00:00:00', '2026-07-01 00:00:00')"
            )
        )

    command.upgrade(config, "20260728_0024")
    inspector = inspect(engine)
    agent_columns = {column["name"] for column in inspector.get_columns("agent_profiles")}
    assert {
        "owner_user_id",
        "source_agent_id",
        "source_agent_version",
        "profile_revision",
        "published_to_gallery",
        "gallery_published_at",
        "gallery_published_by",
        "agent_category_code",
        "visibility_scope",
    } <= agent_columns
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    assert {
        "agent_profile_revision",
        "capability_snapshot_json",
        "origin",
    } <= session_columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT owner_user_id, profile_revision, published_to_gallery, "
                "gallery_published_by, agent_category_code, visibility_scope "
                "FROM agent_profiles WHERE id = 'agent_expert'"
            )
        ).one()
        assert tuple(row) == ("owner_a", 1, 1, "admin_a", "professional", "tenant")
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM agent_usages "
                "WHERE tenant_id = 'tenant_a' AND user_id = 'owner_a' "
                "AND agent_id = 'agent_expert'"
            )
        ).scalar_one() == 1

    command.downgrade(config, "20260728_0023")
    inspector = inspect(engine)
    assert "owner_user_id" not in {
        column["name"] for column in inspector.get_columns("agent_profiles")
    }
    assert "agent_profile_revision" not in {
        column["name"] for column in inspector.get_columns("sessions")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM agent_usages")).scalar_one() == 0


def test_legacy_sqlite_path_backfills_agent_identity_and_usage(tmp_path) -> None:
    """验证无 Alembic 版本的桌面旧库也能补齐 M4-A 字段和使用关系。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-agent-identity.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "username VARCHAR(191) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_profiles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, description TEXT, persona_prompt TEXT, "
                "is_overall BOOLEAN NOT NULL, metadata_json JSON, status VARCHAR(64) NOT NULL, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_usages ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128) NOT NULL, agent_id VARCHAR(128) NOT NULL, "
                "metadata_json JSON, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_agent_usage_user_agent UNIQUE (tenant_id, user_id, agent_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "user_id VARCHAR(128), agent_id VARCHAR(128), created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, tenant_id, username) VALUES "
                "('owner_legacy', 'tenant_legacy', 'owner'), "
                "('admin_legacy', 'tenant_legacy', 'admin')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles "
                "(id, tenant_id, name, is_overall, metadata_json, status, created_at, updated_at) "
                "VALUES ('agent_legacy', 'tenant_legacy', '旧专家', 0, :metadata, 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "metadata": (
                    '{"owner_user_id":"owner_legacy","published_to_gallery":true,'
                    '"gallery_published_by":"admin","employee_type":"expert"}'
                )
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, tenant_id, user_id, agent_id, created_at, updated_at) VALUES "
                "('session_legacy', 'tenant_legacy', 'owner_legacy', 'agent_legacy', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    migrate_sqlite_skill_schema(engine)
    migrate_sqlite_skill_schema(engine)

    inspector = inspect(engine)
    assert {
        "owner_user_id",
        "profile_revision",
        "published_to_gallery",
        "agent_category_code",
        "visibility_scope",
    } <= {column["name"] for column in inspector.get_columns("agent_profiles")}
    assert {
        "agent_profile_revision",
        "capability_snapshot_json",
        "origin",
    } <= {column["name"] for column in inspector.get_columns("sessions")}
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT owner_user_id, published_to_gallery, gallery_published_by, "
                "agent_category_code, visibility_scope FROM agent_profiles "
                "WHERE id = 'agent_legacy'"
            )
        ).one()
        assert tuple(row) == (
            "owner_legacy",
            1,
            "admin_legacy",
            "professional",
            "tenant",
        )
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM agent_usages "
                "WHERE tenant_id = 'tenant_legacy' AND user_id = 'owner_legacy' "
                "AND agent_id = 'agent_legacy'"
            )
        ).scalar_one() == 1


def test_sqlite_model_defaults_support_legacy_raw_overall_agent_seed(tmp_path) -> None:
    """验证新 SQLite 表的数据库默认值兼容旧补种逻辑使用的原生 SQL。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'raw-overall-seed.db'}")
    initialize_sqlite_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at, updated_at) VALUES "
                "('tenant_raw_seed', 'Raw Seed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    migrate_sqlite_skill_schema(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT profile_revision, published_to_gallery, agent_category_code, "
                "visibility_scope FROM agent_profiles "
                "WHERE id = 'agent_tenant_raw_seed_overall'"
            )
        ).one()
        assert tuple(row) == (1, 0, "assistant", "private")


def test_knowledge_governance_migration_is_fail_closed_and_reversible(tmp_path) -> None:
    """验证 0025 仅按同租户创建者 ID 回填 owner，并默认 owner/restricted。"""

    database_url = f"sqlite:///{tmp_path / 'knowledge-governance.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0024')"))
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_bases ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "name VARCHAR(191) NOT NULL, description TEXT, status VARCHAR(64) NOT NULL, "
                "metadata_json JSON, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, tenant_id) VALUES "
                "('user_owner', 'tenant_kb')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases "
                "(id, tenant_id, name, status, metadata_json, created_at, updated_at) VALUES "
                "('kb_owned', 'tenant_kb', '有主知识', 'active', "
                "'{\"created_by_user_id\":\"user_owner\"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('kb_unknown', 'tenant_kb', '无主知识', 'active', "
                "'{\"created_by_user_id\":\"missing\"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260728_0025")
    inspector = inspect(engine)
    assert {
        "owner_user_id",
        "responsible_org_unit_id",
        "access_scope",
        "download_policy",
        "revision",
    } <= {column["name"] for column in inspector.get_columns("knowledge_bases")}
    assert "knowledge_base_org_access" in inspector.get_table_names()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, owner_user_id, access_scope, download_policy, revision "
                "FROM knowledge_bases WHERE tenant_id = 'tenant_kb' ORDER BY id"
            )
        ).all()
        assert [tuple(row) for row in rows] == [
            ("kb_owned", "user_owner", "owner", "restricted", 1),
            ("kb_unknown", None, "owner", "restricted", 1),
        ]

    command.downgrade(config, "20260728_0024")
    inspector = inspect(engine)
    assert "knowledge_base_org_access" not in inspector.get_table_names()
    assert "owner_user_id" not in {
        column["name"] for column in inspector.get_columns("knowledge_bases")
    }
    command.upgrade(config, "20260728_0025")


def test_management_audit_migration_is_reversible_and_indexed(tmp_path) -> None:
    """验证 0026 可创建只追加审计结构、常用组合索引并安全降级重建。"""

    database_url = f"sqlite:///{tmp_path / 'management-audit.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260728_0025')"))
    command.upgrade(config, "20260729_0026")
    inspector = inspect(engine)

    assert "management_audit_logs" in inspector.get_table_names()
    columns = {
        column["name"] for column in inspector.get_columns("management_audit_logs")
    }
    assert {
        "tenant_id",
        "actor_user_id",
        "action",
        "action_kind",
        "outcome",
        "resource_type",
        "resource_id",
        "target_org_unit_id",
        "before_json",
        "after_json",
        "detail_json",
        "created_at",
    }.issubset(columns)
    indexes = {
        index["name"] for index in inspector.get_indexes("management_audit_logs")
    }
    assert {
        "ix_management_audit_tenant_created",
        "ix_management_audit_tenant_actor_created",
        "ix_management_audit_tenant_action_created",
        "ix_management_audit_tenant_resource_created",
        "ix_management_audit_tenant_org_created",
    }.issubset(indexes)

    command.downgrade(config, "20260728_0025")
    assert "management_audit_logs" not in inspect(engine).get_table_names()
    command.upgrade(config, "20260729_0026")
    assert "management_audit_logs" in inspect(engine).get_table_names()


def test_legacy_published_snapshot_repair_is_evidence_gated_and_idempotent(
    tmp_path,
) -> None:
    """验证 0027 只补已知种子发布事实，重复升级不复制不可变快照。"""

    database_url = f"sqlite:///{tmp_path / 'legacy-published-snapshots.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0026')"))

    seed_cards = (
        EXCHANGE_SKILL,
        REFUND_SKILL,
        PRICE_COMPARE_SKILL,
        PURCHASE_SKILL,
    )
    with Session(engine) as session:
        for card in seed_cards:
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

    command.upgrade(config, "20260729_0027")
    with Session(engine) as session:
        snapshots = session.exec(
            select(SkillVersion).order_by(SkillVersion.skill_id)
        ).all()
        assert [snapshot.skill_id for snapshot in snapshots] == sorted(
            card["skill_id"] for card in seed_cards
        )
        assert all(snapshot.status == "published" for snapshot in snapshots)
        assert all(snapshot.content_checksum for snapshot in snapshots)
        assert all(snapshot.compiled_definition_checksum for snapshot in snapshots)
        assert all(snapshot.meta_model_version == 1 for snapshot in snapshots)
        assert all(snapshot.source_schema_version == 2 for snapshot in snapshots)
        first_ids = [snapshot.id for snapshot in snapshots]

    command.downgrade(config, "20260729_0026")
    command.upgrade(config, "20260729_0027")
    with Session(engine) as session:
        snapshots = session.exec(
            select(SkillVersion).order_by(SkillVersion.skill_id)
        ).all()
        assert [snapshot.id for snapshot in snapshots] == first_ids


def test_legacy_published_snapshot_repair_rejects_unproven_content(tmp_path) -> None:
    """验证同名发布头一旦内容不在证据指纹内，0027 保持原样并等待人工处置。"""

    database_url = f"sqlite:///{tmp_path / 'unproven-published-snapshot.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0026')"))
    content = _skill_content_graph(REFUND_SKILL)
    content["description"] = "未经来源证明的人工修改"
    with Session(engine) as session:
        session.add(
            Skill(
                id="skill_unproven_refund",
                tenant_id="tenant_demo",
                skill_id=REFUND_SKILL["skill_id"],
                version="1.0.0",
                name=REFUND_SKILL["name"],
                business_domain=REFUND_SKILL["business_domain"],
                description=REFUND_SKILL["description"],
                content_json=content,
                status="published",
            )
        )
        session.commit()

    command.upgrade(config, "20260729_0027")
    with Session(engine) as session:
        assert session.exec(select(SkillVersion)).all() == []


def test_agent_responsibility_migration_is_reversible_and_indexed(tmp_path) -> None:
    """验证 0028 只增加可空责任组织事实，并可完整回退到上一迁移。"""

    database_url = f"sqlite:///{tmp_path / 'agent-responsibility.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0027')"))
        connection.execute(
            text(
                "CREATE TABLE agent_profiles ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )

    command.upgrade(config, "20260729_0028")
    inspector = inspect(engine)
    assert "responsible_org_unit_id" in {
        column["name"] for column in inspector.get_columns("agent_profiles")
    }
    assert {
        "ix_agent_profiles_responsible_org_unit_id",
        "ix_agent_profiles_tenant_responsible_org",
    }.issubset(
        {index["name"] for index in inspector.get_indexes("agent_profiles")}
    )

    command.downgrade(config, "20260729_0027")
    inspector = inspect(engine)
    assert "responsible_org_unit_id" not in {
        column["name"] for column in inspector.get_columns("agent_profiles")
    }
    command.upgrade(config, "20260729_0028")


def test_effective_interval_precision_migration_is_sqlite_noop(tmp_path) -> None:
    """验证 0029 在 SQLite 只推进 revision，不执行不受支持的列类型改写。"""

    database_url = f"sqlite:///{tmp_path / 'effective-interval-precision.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0028')"))

    command.upgrade(config, "20260729_0029")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260729_0029"
        )

    command.downgrade(config, "20260729_0028")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260729_0028"
        )


def test_acceptance_asset_retirement_preserves_history_and_closes_execution(tmp_path) -> None:
    """验证 0030 仅退役临时入口，不删除技能版本或 SOP 历史实例。"""

    database_url = f"sqlite:///{tmp_path / 'acceptance-asset-retirement.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0029')"))
        connection.execute(
            text(
                "CREATE TABLE agent_resource_bindings "
                "(id VARCHAR PRIMARY KEY, agent_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_skill_branches "
                "(id VARCHAR PRIMARY KEY, agent_id VARCHAR, skill_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_role_bindings "
                "(id VARCHAR PRIMARY KEY, agent_id VARCHAR, business_role_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE employee_role_assignments "
                "(id VARCHAR PRIMARY KEY, business_role_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text("CREATE TABLE business_roles (id VARCHAR PRIMARY KEY, status VARCHAR)")
        )
        connection.execute(
            text(
                "CREATE TABLE permission_definitions "
                "(id VARCHAR PRIMARY KEY, permission_code VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text("CREATE TABLE tools (id VARCHAR PRIMARY KEY, name VARCHAR, enabled BOOLEAN)")
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_bases "
                "(id VARCHAR PRIMARY KEY, name VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE skills "
                "(id VARCHAR PRIMARY KEY, skill_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_profiles "
                "(id VARCHAR PRIMARY KEY, status VARCHAR, "
                "published_to_gallery BOOLEAN, visibility_scope VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE skill_versions "
                "(id VARCHAR PRIMARY KEY, skill_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sop_instances "
                "(id VARCHAR PRIMARY KEY, skill_id VARCHAR, status VARCHAR)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skills VALUES "
                "('skill-row', 'skill_telecom_fault_regression_20260728', 'published')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_versions VALUES "
                "('version-row', 'skill_telecom_fault_regression_20260728', 'published')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_instances VALUES "
                "('instance-row', 'skill_telecom_fault_regression_20260728', 'completed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles VALUES "
                "('agent_m55d_telecom_fault', 'active', true, 'tenant')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_resource_bindings VALUES "
                "('binding-row', 'agent_m55d_telecom_fault', 'active')"
            )
        )

    command.upgrade(config, "20260729_0030")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM skills")).scalar_one() == "archived"
        assert connection.execute(
            text("SELECT status FROM agent_resource_bindings")
        ).scalar_one() == "inactive"
        assert connection.execute(
            text(
                "SELECT status || ':' || published_to_gallery || ':' || visibility_scope "
                "FROM agent_profiles"
            )
        ).scalar_one() == "archived:0:private"
        assert connection.execute(text("SELECT COUNT(*) FROM skill_versions")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM sop_instances")).scalar_one() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260729_0030"
        )


def test_memory_agent_pagination_migration_backfills_metadata_and_session_ownership(
    tmp_path,
) -> None:
    """验证 0031 跨 metadata 与会话回填员工归属，并能完整降级。"""

    database_url = f"sqlite:///{tmp_path / 'memory-agent-pagination.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0030')"))
        connection.execute(
            text("CREATE TABLE sessions (id VARCHAR PRIMARY KEY, agent_id VARCHAR)")
        )
        connection.execute(
            text(
                "CREATE TABLE memories ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, user_id VARCHAR NOT NULL, "
                "session_id VARCHAR, metadata_json JSON, updated_at DATETIME)"
            )
        )
        connection.execute(
            text("INSERT INTO sessions VALUES ('session_a', 'agent_from_session')")
        )
        connection.execute(
            text(
                "INSERT INTO memories VALUES "
                "('mem_metadata', 'tenant_demo', 'user_a', NULL, "
                "'{\"agent_id\": \"agent_from_metadata\"}', '2026-08-01 10:00:00'), "
                "('mem_session', 'tenant_demo', 'user_b', 'session_a', '{}', "
                "'2026-08-01 11:00:00')"
            )
        )

    command.upgrade(config, "20260801_0031")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT agent_id FROM memories WHERE id = 'mem_metadata'")
        ).scalar_one() == "agent_from_metadata"
        assert connection.execute(
            text("SELECT agent_id FROM memories WHERE id = 'mem_session'")
        ).scalar_one() == "agent_from_session"
        assert "ix_memories_tenant_agent_user_updated" in {
            index["name"] for index in inspect(connection).get_indexes("memories")
        }

    command.downgrade(config, "20260729_0030")
    with engine.connect() as connection:
        assert "agent_id" not in {
            column["name"] for column in inspect(connection).get_columns("memories")
        }


def test_memory_agent_pagination_migration_resumes_after_mysql_non_transactional_ddl(
    tmp_path,
) -> None:
    """验证 0031 在员工列已创建但 revision 未推进时可以继续回填和建索引。"""

    database_url = f"sqlite:///{tmp_path / 'memory-agent-partial-upgrade.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260729_0030')"))
        connection.execute(text("CREATE TABLE sessions (id VARCHAR PRIMARY KEY, agent_id VARCHAR)"))
        connection.execute(
            text(
                "CREATE TABLE memories ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, user_id VARCHAR NOT NULL, "
                "session_id VARCHAR, agent_id VARCHAR, metadata_json JSON, updated_at DATETIME)"
            )
        )
        connection.execute(text("INSERT INTO sessions VALUES ('session_a', 'agent_resumed')"))
        connection.execute(
            text(
                "INSERT INTO memories VALUES "
                "('memory_a', 'tenant_demo', 'user_a', 'session_a', NULL, '{}', "
                "'2026-08-01 11:00:00')"
            )
        )

    command.upgrade(config, "20260801_0031")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT agent_id FROM memories")).scalar_one() == (
            "agent_resumed"
        )
        assert "ix_memories_tenant_agent_user_updated" in {
            index["name"] for index in inspect(connection).get_indexes("memories")
        }


def test_scheduled_run_pagination_indexes_migration_is_reversible(tmp_path) -> None:
    """验证 0032 创建两类运行记录分页索引且降级不删除历史数据。"""

    database_url = f"sqlite:///{tmp_path / 'scheduled-run-pagination.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260801_0031')"))
        connection.execute(
            text(
                "CREATE TABLE scheduled_task_runs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, agent_id VARCHAR NOT NULL, "
                "user_id VARCHAR NOT NULL, scheduled_for DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO scheduled_task_runs VALUES "
                "('run_1', 'tenant_demo', 'agent_demo', 'user_demo', '2026-08-01 10:00:00')"
            )
        )

    command.upgrade(config, "20260801_0032")
    with engine.connect() as connection:
        index_names = {index["name"] for index in inspect(connection).get_indexes("scheduled_task_runs")}
        assert "ix_sched_runs_tenant_agent_scheduled" in index_names
        assert "ix_sched_runs_tenant_agent_user_scheduled" in index_names

    command.downgrade(config, "20260801_0031")
    with engine.connect() as connection:
        assert inspect(connection).get_indexes("scheduled_task_runs") == []
        assert connection.execute(text("SELECT COUNT(*) FROM scheduled_task_runs")).scalar_one() == 1


def test_scheduled_task_pagination_indexes_migration_is_reversible(tmp_path) -> None:
    """验证 0033 创建两类任务定义分页索引且降级保留任务数据。"""

    database_url = f"sqlite:///{tmp_path / 'scheduled-task-pagination.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260801_0032')"))
        connection.execute(
            text(
                "CREATE TABLE scheduled_tasks ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, agent_id VARCHAR NOT NULL, "
                "created_by_user_id VARCHAR NOT NULL, status VARCHAR NOT NULL, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO scheduled_tasks VALUES "
                "('task_1', 'tenant_demo', 'agent_demo', 'user_demo', 'active', "
                "'2026-08-01 10:00:00')"
            )
        )

    command.upgrade(config, "20260801_0033")
    with engine.connect() as connection:
        index_names = {index["name"] for index in inspect(connection).get_indexes("scheduled_tasks")}
        assert "ix_sched_tasks_tenant_agent_status_updated" in index_names
        assert "ix_sched_tasks_tenant_agent_creator_status_updated" in index_names

    command.downgrade(config, "20260801_0032")
    with engine.connect() as connection:
        assert inspect(connection).get_indexes("scheduled_tasks") == []
        assert connection.execute(text("SELECT COUNT(*) FROM scheduled_tasks")).scalar_one() == 1


def test_scheduled_dynamic_run_migration_backfills_source_and_is_reversible(tmp_path) -> None:
    """验证 0046 回填历史 run、扩展 Signal 类型，并在无新事实时可安全降级。"""

    database_url = f"sqlite:///{tmp_path / 'scheduled-dynamic-run.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260810_0045')"))
        connection.execute(
            text(
                "CREATE TABLE scheduled_task_runs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "scheduled_task_id VARCHAR NOT NULL, agent_id VARCHAR NOT NULL, "
                "user_id VARCHAR NOT NULL, scheduled_for DATETIME NOT NULL, "
                "status VARCHAR NOT NULL, trace_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_scheduled_task_run_due_time UNIQUE "
                "(scheduled_task_id, scheduled_for))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE execution_signals ("
                "id VARCHAR PRIMARY KEY, signal_type VARCHAR NOT NULL, "
                "CONSTRAINT ck_execution_signal_type CHECK ("
                "signal_type IN ('command', 'attention_decided', 'timer', "
                "'operation_settled', 'external_event', 'publication_retry')))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO scheduled_task_runs ("
                "id, tenant_id, scheduled_task_id, agent_id, user_id, scheduled_for, status, "
                "trace_json, created_at, updated_at) VALUES ("
                "'legacy_run', 'tenant_demo', 'task_demo', 'agent_demo', 'user_demo', "
                "'2026-08-11 09:00:00', 'succeeded', '{}', "
                "'2026-08-11 09:00:00', '2026-08-11 09:00:01')"
            )
        )

    command.upgrade(config, "20260811_0046")
    with engine.connect() as connection:
        columns = {
            str(column["name"]): column
            for column in inspect(connection).get_columns("scheduled_task_runs")
        }
        row = connection.execute(
            text(
                "SELECT source_kind, source_ref, source_snapshot_json, source_checksum "
                "FROM scheduled_task_runs WHERE id='legacy_run'"
            )
        ).one()
        assert row.source_kind == "legacy"
        assert row.source_ref == "legacy:legacy_run"
        assert "legacy_run" in str(row.source_snapshot_json)
        assert len(row.source_checksum) == 64
        assert columns["source_ref"]["nullable"] is False
        assert columns["source_snapshot_json"]["nullable"] is False
        assert columns["source_checksum"]["nullable"] is False
        unique_names = {
            item["name"]
            for item in inspect(connection).get_unique_constraints("scheduled_task_runs")
        }
        assert "uq_scheduled_task_run_source_ref" in unique_names
        signal_checks = {
            item["name"]: item["sqltext"]
            for item in inspect(connection).get_check_constraints("execution_signals")
        }
        assert "scheduled_start" in signal_checks["ck_execution_signal_type"]

    command.downgrade(config, "20260810_0045")
    with engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("scheduled_task_runs")
        }
        assert "source_ref" not in columns
        assert connection.execute(
            text("SELECT COUNT(*) FROM scheduled_task_runs")
        ).scalar_one() == 1


def test_scheduled_dynamic_run_migration_resumes_after_partial_mysql_ddl(tmp_path) -> None:
    """验证 0046 在来源列已增加、Signal 旧检查已丢失时仍可从非事务 DDL 中断点恢复。"""

    database_url = f"sqlite:///{tmp_path / 'scheduled-dynamic-run-partial.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260810_0045')"))
        connection.execute(
            text(
                "CREATE TABLE scheduled_task_runs ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, "
                "scheduled_task_id VARCHAR NOT NULL, agent_id VARCHAR NOT NULL, "
                "user_id VARCHAR NOT NULL, scheduled_for DATETIME NOT NULL, "
                "status VARCHAR NOT NULL, trace_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "execution_id VARCHAR, source_kind VARCHAR, source_ref VARCHAR, "
                "source_snapshot_json JSON, source_checksum VARCHAR, "
                "CONSTRAINT uq_scheduled_task_run_due_time UNIQUE "
                "(scheduled_task_id, scheduled_for))"
            )
        )
        connection.execute(
            text("CREATE TABLE execution_signals (id VARCHAR PRIMARY KEY, signal_type VARCHAR NOT NULL)")
        )
        connection.execute(
            text(
                "INSERT INTO scheduled_task_runs ("
                "id, tenant_id, scheduled_task_id, agent_id, user_id, scheduled_for, status, "
                "trace_json, created_at, updated_at) VALUES ("
                "'partial_run', 'tenant_demo', 'task_demo', 'agent_demo', 'user_demo', "
                "'2026-08-11 09:00:00', 'running', '{}', "
                "'2026-08-11 09:00:00', '2026-08-11 09:00:01')"
            )
        )

    command.upgrade(config, "20260811_0046")
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {
            str(column["name"]): column
            for column in inspector.get_columns("scheduled_task_runs")
        }
        assert columns["source_kind"]["nullable"] is False
        assert columns["source_ref"]["nullable"] is False
        assert connection.execute(
            text("SELECT source_ref FROM scheduled_task_runs WHERE id='partial_run'")
        ).scalar_one() == "legacy:partial_run"
        signal_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("execution_signals")
        }
        assert "scheduled_start" in signal_checks["ck_execution_signal_type"]


def test_standing_approval_migration_backfills_authorization_and_is_reversible(
    tmp_path,
) -> None:
    """验证 0047 建表/索引、历史授权来源回填及无新事实时安全降级。"""

    database_url = f"sqlite:///{tmp_path / 'standing-approval.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260811_0046')"))
        connection.execute(text("CREATE TABLE scheduled_tasks (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE connection_profiles (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE agent_connection_bindings (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR PRIMARY KEY, approval_work_item_id VARCHAR NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_operations (id, approval_work_item_id) VALUES "
                "('operation_attention', 'attention_1'), ('operation_legacy', NULL)"
            )
        )

    command.upgrade(config, "20260811_0047")
    command.upgrade(config, "20260811_0047")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "standing_approval_rules",
            "standing_approval_command_receipts",
        }.issubset(inspector.get_table_names())
        operation_columns = {
            str(column["name"]): column
            for column in inspector.get_columns("sop_operations")
        }
        assert operation_columns["authorization_source_type"]["nullable"] is False
        sources = dict(
            connection.execute(
                text("SELECT id, authorization_source_type FROM sop_operations")
            ).all()
        )
        assert sources == {
            "operation_attention": "attention",
            "operation_legacy": "legacy",
        }
        rule_indexes = {
            str(index["name"])
            for index in inspector.get_indexes("standing_approval_rules")
        }
        assert {
            "ix_standing_rules_active_lookup",
            "ix_standing_rules_target_lookup",
        }.issubset(rule_indexes)
        assert "uq_standing_rule_active_scope" in {
            str(item["name"])
            for item in inspector.get_unique_constraints("standing_approval_rules")
        }

    command.downgrade(config, "20260811_0046")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "standing_approval_rules" not in inspector.get_table_names()
        assert "standing_approval_command_receipts" not in inspector.get_table_names()
        assert "authorization_source_type" not in {
            column["name"] for column in inspector.get_columns("sop_operations")
        }
        assert connection.execute(text("SELECT COUNT(*) FROM sop_operations")).scalar_one() == 2


def test_standing_approval_migration_refuses_to_drop_dispatch_evidence(tmp_path) -> None:
    """存在长期规则派发事实时 0047 降级必须失败，禁止丢失授权审计链。"""

    database_url = f"sqlite:///{tmp_path / 'standing-approval-safety.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260811_0046')"))
        connection.execute(text("CREATE TABLE scheduled_tasks (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE connection_profiles (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE agent_connection_bindings (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR PRIMARY KEY, approval_work_item_id VARCHAR NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO sop_operations (id) VALUES ('standing_operation')")
        )
    command.upgrade(config, "20260811_0047")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sop_operations SET authorization_source_type='standing_rule', "
                "authorization_source_ref='rule_1:1' WHERE id='standing_operation'"
            )
        )

    with pytest.raises(RuntimeError, match="dispatch facts"):
        command.downgrade(config, "20260811_0046")


def test_dynamic_task_quota_lease_migration_is_reentrant_and_safe(tmp_path) -> None:
    """验证 0048 唯一槽表可重入创建，且活动租约存在时拒绝降级绕过配额。"""

    database_url = f"sqlite:///{tmp_path / 'dynamic-quota.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260811_0047')"))
        connection.execute(text("CREATE TABLE sop_instances (id VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE execution_signals ("
                "id VARCHAR PRIMARY KEY, signal_type VARCHAR NOT NULL, "
                "CONSTRAINT ck_execution_signal_type CHECK ("
                "signal_type IN ('command', 'attention_decided', 'timer', "
                "'operation_settled', 'external_event', 'publication_retry', "
                "'scheduled_start')))"
            )
        )

    command.upgrade(config, "20260811_0048")
    command.upgrade(config, "20260811_0048")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "dynamic_task_quota_leases" in inspector.get_table_names()
        assert {
            "uq_dynamic_quota_scope_slot",
            "uq_dynamic_quota_holder_scope",
        }.issubset(
            {
                str(item["name"])
                for item in inspector.get_unique_constraints("dynamic_task_quota_leases")
            }
        )
        signal_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("execution_signals")
        }
        assert "capacity_retry" in signal_checks["ck_execution_signal_type"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dynamic_task_quota_leases "
                "(id, tenant_id, scope_type, scope_ref, slot_number, holder_type, holder_id, acquired_at) "
                "VALUES ('quota_1', 'tenant_a', 'tenant', 'tenant_a', 0, "
                "'execution', 'execution_a', '2026-08-11 00:00:00')"
            )
        )

    with pytest.raises(RuntimeError, match="active dynamic task quota leases"):
        command.downgrade(config, "20260811_0047")

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM dynamic_task_quota_leases"))
    command.downgrade(config, "20260811_0047")
    with engine.connect() as connection:
        assert "dynamic_task_quota_leases" not in inspect(connection).get_table_names()
        signal_checks = {
            item["name"]: item["sqltext"]
            for item in inspect(connection).get_check_constraints("execution_signals")
        }
        assert "capacity_retry" not in signal_checks["ck_execution_signal_type"]


def test_agent_gallery_pagination_indexes_migration_is_reversible(tmp_path) -> None:
    """验证 0034 创建员工广场分页索引且降级保留员工数据。"""

    database_url = f"sqlite:///{tmp_path / 'agent-gallery-pagination.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260801_0033')"))
        connection.execute(
            text(
                "CREATE TABLE agent_profiles ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, owner_user_id VARCHAR, "
                "published_to_gallery BOOLEAN NOT NULL, status VARCHAR NOT NULL, "
                "agent_category_code VARCHAR, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles VALUES "
                "('agent_1', 'tenant_demo', 'user_demo', true, 'active', 'professional', "
                "'2026-08-01 10:00:00')"
            )
        )

    command.upgrade(config, "20260801_0034")
    with engine.connect() as connection:
        index_names = {index["name"] for index in inspect(connection).get_indexes("agent_profiles")}
        assert index_names == {
            "ix_agent_profiles_tenant_owner_status_updated",
            "ix_agent_profiles_tenant_gallery_status_category_updated",
            "ix_agent_profiles_tenant_category_status_updated",
        }

    command.downgrade(config, "20260801_0033")
    with engine.connect() as connection:
        assert inspect(connection).get_indexes("agent_profiles") == []
        assert connection.execute(text("SELECT COUNT(*) FROM agent_profiles")).scalar_one() == 1


def test_role_binding_effective_intervals_migration_is_reversible(tmp_path) -> None:
    """验证 0035 为岗位和数字员工角色绑定补齐有效期、授予人和组织子树契约。"""

    database_url = f"sqlite:///{tmp_path / 'role-binding-intervals.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260801_0034')"))
        connection.execute(
            text(
                "CREATE TABLE position_role_bindings ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, position_id VARCHAR NOT NULL, "
                "business_role_id VARCHAR NOT NULL, scope_mode VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_role_bindings ("
                "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, agent_id VARCHAR NOT NULL, "
                "business_role_id VARCHAR NOT NULL, assignment_mode VARCHAR NOT NULL, "
                "scope_type VARCHAR NOT NULL, scope_id VARCHAR NOT NULL, status VARCHAR NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO position_role_bindings VALUES "
                "('posrole_1', 'tenant_demo', 'position_1', 'role_1', 'position_org', "
                "'active', '2026-08-01 10:00:00', '2026-08-01 10:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_role_bindings VALUES "
                "('agentrole_1', 'tenant_demo', 'agent_1', 'role_1', 'execute', 'tenant', '*', "
                "'active', '2026-08-01 11:00:00', '2026-08-01 11:00:00')"
            )
        )

    command.upgrade(config, "20260802_0035")
    with engine.connect() as connection:
        inspector = inspect(connection)
        position_columns = {
            column["name"] for column in inspector.get_columns("position_role_bindings")
        }
        agent_columns = {
            column["name"] for column in inspector.get_columns("agent_role_bindings")
        }
        assert {"granted_by_user_id", "effective_from", "effective_until"}.issubset(
            position_columns
        )
        assert {
            "include_descendants",
            "granted_by_user_id",
            "effective_from",
            "effective_until",
        }.issubset(agent_columns)
        assert connection.execute(
            text("SELECT effective_from FROM position_role_bindings WHERE id='posrole_1'")
        ).scalar_one() is not None
        assert connection.execute(
            text("SELECT effective_from FROM agent_role_bindings WHERE id='agentrole_1'")
        ).scalar_one() is not None

    command.downgrade(config, "20260801_0034")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "effective_from" not in {
            column["name"] for column in inspector.get_columns("position_role_bindings")
        }
        assert "include_descendants" not in {
            column["name"] for column in inspector.get_columns("agent_role_bindings")
        }
        assert connection.execute(
            text("SELECT COUNT(*) FROM position_role_bindings")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM agent_role_bindings")
        ).scalar_one() == 1


def _create_legacy_sop_instances_table(connection) -> None:  # noqa: ANN001
    """创建 0035 时点的最小 SOP 实例表，供 B0.1 独立迁移测试使用。"""

    connection.execute(
        text(
            "CREATE TABLE sop_instances ("
            "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
            "session_id VARCHAR(128) NOT NULL, skill_id VARCHAR(128) NOT NULL, "
            "skill_version_id VARCHAR(128) NOT NULL, skill_version VARCHAR(64) NOT NULL, "
            "definition_checksum VARCHAR(64) NOT NULL, run_number INTEGER NOT NULL, "
            "status VARCHAR(64) NOT NULL, current_node_id VARCHAR(128), "
            "slots_json JSON NOT NULL, context_json JSON NOT NULL, revision INTEGER NOT NULL, "
            "started_at DATETIME, completed_at DATETIME, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_sop_instance_session_version_run UNIQUE "
            "(tenant_id, session_id, skill_version_id, run_number))"
        )
    )


def test_execution_ownership_migration_backfills_and_is_reversible(tmp_path) -> None:
    """验证 0036 回填 SOP kind/活动槽/来源/租约字段并能保留数据降级。"""

    database_url = f"sqlite:///{tmp_path / 'execution-ownership.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
        connection.execute(
            text(
                "INSERT INTO sop_instances VALUES "
                "('inst_active', 'tenant_demo', 'session_active', 'skill_a', 'version_a', "
                "'1.0.0', :checksum, 1, 'running', 'node_a', '{}', '{}', 1, "
                "'2026-08-01 10:00:00', NULL, '2026-08-01 10:00:00', "
                "'2026-08-01 10:00:00'), "
                "('inst_done', 'tenant_demo', 'session_done', 'skill_b', 'version_b', "
                "'1.0.0', :checksum, 1, 'succeeded', 'node_b', '{}', '{}', 4, "
                "'2026-08-01 11:00:00', '2026-08-01 11:10:00', "
                "'2026-08-01 11:00:00', '2026-08-01 11:10:00')"
            ),
            {"checksum": "a" * 64},
        )

    command.upgrade(config, "20260803_0036")
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("sop_instances")}
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
        }.issubset(columns)
        rows = connection.execute(
            text(
                "SELECT id, kind, active_slot_key, source_kind, source_ref, "
                "cancellation_disposition, fencing_token FROM sop_instances ORDER BY id"
            )
        ).mappings().all()
        assert rows[0]["active_slot_key"] == "foreground:session_active"
        assert rows[1]["active_slot_key"] is None
        assert all(row["kind"] == "sop" for row in rows)
        assert all(row["source_kind"] == "legacy" for row in rows)
        assert all(row["cancellation_disposition"] == "none" for row in rows)
        assert all(row["fencing_token"] == 0 for row in rows)
        assert "uq_execution_tenant_active_slot" in {
            item["name"] for item in inspector.get_unique_constraints("sop_instances")
        }
        assert "ix_sop_instances_tenant_lease_expiry" in {
            item["name"] for item in inspector.get_indexes("sop_instances")
        }
        assert inspector.has_table("execution_mutation_rejections")

    command.downgrade(config, "20260802_0035")
    with engine.connect() as connection:
        columns = {
            column["name"] for column in inspect(connection).get_columns("sop_instances")
        }
        assert "kind" not in columns
        assert "fencing_token" not in columns
        assert not inspect(connection).has_table("execution_mutation_rejections")
        assert connection.execute(text("SELECT COUNT(*) FROM sop_instances")).scalar_one() == 2


def test_execution_ownership_migration_rejects_duplicate_active_rows(tmp_path) -> None:
    """验证同 tenant/session 的历史双活动实例会中止迁移且不会被静默裁决。"""

    database_url = f"sqlite:///{tmp_path / 'execution-ownership-dirty.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
        for instance_id, version_id in (("inst_a", "version_a"), ("inst_b", "version_b")):
            connection.execute(
                text(
                    "INSERT INTO sop_instances VALUES "
                    "(:id, 'tenant_demo', 'session_shared', 'skill_a', :version_id, '1.0.0', "
                    ":checksum, 1, 'running', 'node_a', '{}', '{}', 1, "
                    "'2026-08-01 10:00:00', NULL, '2026-08-01 10:00:00', "
                    "'2026-08-01 10:00:00')"
                ),
                {"id": instance_id, "version_id": version_id, "checksum": "a" * 64},
            )

    with pytest.raises(RuntimeError, match="duplicate active executions") as caught:
        command.upgrade(config, "20260803_0036")

    assert "inst_a" in str(caught.value)
    assert "inst_b" in str(caught.value)


def test_execution_ownership_migration_resumes_after_partial_expand(tmp_path) -> None:
    """验证 SQLite 模拟非事务 DDL 中断后可从部分列状态继续完成 0036。"""

    database_url = f"sqlite:///{tmp_path / 'execution-ownership-partial.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
        connection.execute(text("ALTER TABLE sop_instances ADD COLUMN kind VARCHAR(64)"))
        connection.execute(
            text("ALTER TABLE sop_instances ADD COLUMN active_slot_key VARCHAR(512)")
        )
        connection.execute(
            text(
                "INSERT INTO sop_instances "
                "(id, tenant_id, session_id, skill_id, skill_version_id, skill_version, "
                "definition_checksum, run_number, status, current_node_id, slots_json, "
                "context_json, revision, started_at, completed_at, created_at, updated_at, "
                "kind, active_slot_key) VALUES "
                "('inst_partial', 'tenant_demo', 'session_partial', 'skill_a', 'version_a', "
                "'1.0.0', :checksum, 1, 'running', 'node_a', '{}', '{}', 1, NULL, NULL, "
                "'2026-08-01 10:00:00', '2026-08-01 10:00:00', NULL, NULL)"
            ),
            {"checksum": "a" * 64},
        )

    command.upgrade(config, "20260803_0036")
    command.upgrade(config, "20260803_0036")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT kind, active_slot_key, source_kind, fencing_token "
                "FROM sop_instances WHERE id = 'inst_partial'"
            )
        ).one()
        assert tuple(row) == ("sop", "foreground:session_partial", "legacy", 0)


def test_execution_ownership_downgrade_rejects_dynamic_rows(tmp_path) -> None:
    """验证 0035 无法表达的动态执行会阻止降级，避免身份数据被静默丢弃。"""

    database_url = f"sqlite:///{tmp_path / 'execution-dynamic-downgrade.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
    command.upgrade(config, "20260803_0036")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sop_instances "
                "(id, tenant_id, session_id, run_number, kind, active_slot_key, source_kind, "
                "status, slots_json, context_json, revision, cancellation_disposition, "
                "fencing_token, created_at, updated_at) VALUES "
                "('dynamic_1', 'tenant_demo', 'session_dynamic', 1, 'dynamic_task', "
                "'foreground:session_dynamic', 'api', 'running', '{}', '{}', 0, 'none', 0, "
                "'2026-08-01 10:00:00', '2026-08-01 10:00:00')"
            )
        )

    with pytest.raises(RuntimeError, match="downgrade requires SOP-only rows"):
        command.downgrade(config, "20260802_0035")


@pytest.mark.parametrize(
    ("status", "skill_id", "expected_message"),
    (
        ("mystery", "skill_a", "unknown execution statuses"),
        ("running", "", "invalid SOP execution identities"),
    ),
)
def test_execution_ownership_migration_rejects_unmappable_history(
    tmp_path,
    status: str,
    skill_id: str,
    expected_message: str,
) -> None:
    """验证未知状态和空 SOP 身份均中止迁移，避免用猜测值污染历史。"""

    database_url = f"sqlite:///{tmp_path / f'execution-unmappable-{status}.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
        connection.execute(
            text(
                "INSERT INTO sop_instances VALUES "
                "('inst_invalid', 'tenant_demo', 'session_invalid', :skill_id, 'version_a', "
                "'1.0.0', :checksum, 1, :status, 'node_a', '{}', '{}', 1, NULL, NULL, "
                "'2026-08-01 10:00:00', '2026-08-01 10:00:00')"
            ),
            {"skill_id": skill_id, "status": status, "checksum": "a" * 64},
        )

    with pytest.raises(RuntimeError, match=expected_message):
        command.upgrade(config, "20260803_0036")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260802_0035"
        )


def _seed_0036_operation_history(connection, *, invalid_request: bool = False) -> None:  # noqa: ANN001
    """在 0036 schema 写入读成功、写成功和写运行中的代表性历史操作。"""

    connection.execute(
        text(
            "INSERT INTO sop_instances "
            "(id, tenant_id, session_id, skill_id, skill_version_id, skill_version, "
            "definition_checksum, run_number, kind, active_slot_key, source_kind, status, "
            "current_node_id, slots_json, context_json, revision, cancellation_disposition, "
            "fencing_token, created_at, updated_at) VALUES "
            "('inst_reliable', 'tenant_demo', 'session_reliable', 'skill_a', 'version_a', "
            "'1.0.0', :checksum, 1, 'sop', 'foreground:session_reliable', 'legacy', "
            "'running', 'write_running', '{}', '{}', 1, 'none', 0, "
            "'2026-08-03 10:00:00', '2026-08-03 10:00:00')"
        ),
        {"checksum": "a" * 64},
    )
    for execution_id, node_id, status in (
        ("node_read", "read", "succeeded"),
        ("node_write", "write", "succeeded"),
        ("node_running", "write_running", "running"),
    ):
        connection.execute(
            text(
                "INSERT INTO sop_node_executions "
                "(id, tenant_id, instance_id, node_id, attempt, status, input_json, "
                "output_json, error_json, revision, created_at, updated_at) VALUES "
                "(:id, 'tenant_demo', 'inst_reliable', :node_id, 1, :status, '{}', '{}', "
                "'{}', 0, '2026-08-03 10:00:00', '2026-08-03 10:00:00')"
            ),
            {"id": execution_id, "node_id": node_id, "status": status},
        )
    requests = (
        ("op_read", "node_read", "knowledge.search", "succeeded", '{"query":"制度"}'),
        ("op_write", "node_write", "expense.submit", "succeeded", '{"request_id":"R1"}'),
        (
            "op_running",
            "node_running",
            "expense.submit",
            "running",
            '{"value":NaN}' if invalid_request else '{"request_id":"R2"}',
        ),
    )
    for operation_id, execution_id, operation_name, status, request_json in requests:
        connection.execute(
            text(
                "INSERT INTO sop_operations "
                "(id, tenant_id, instance_id, node_execution_id, operation_name, "
                "idempotency_key, status, request_json, result_json, error_json, revision, "
                "created_at, updated_at) VALUES "
                "(:id, 'tenant_demo', 'inst_reliable', :execution_id, :operation_name, "
                ":idempotency_key, :status, :request_json, '{}', '{}', 0, "
                "'2026-08-03 10:00:00', '2026-08-03 10:00:00')"
            ),
            {
                "id": operation_id,
                "execution_id": execution_id,
                "operation_name": operation_name,
                "idempotency_key": f"key-{operation_id}",
                "status": status,
                "request_json": request_json,
            },
        )


def _prepare_0036_operation_database(engine, config) -> None:  # noqa: ANN001
    """从最小 0035 表结构执行 0036，避开与本批无关的历史 SQLite DDL 限制。"""

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260802_0035')"))
        _create_legacy_sop_instances_table(connection)
        connection.execute(
            text(
                "CREATE TABLE sop_node_executions ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_id VARCHAR(128) NOT NULL, "
                "attempt INTEGER NOT NULL, status VARCHAR(64) NOT NULL, input_json JSON NOT NULL, "
                "output_json JSON NOT NULL, error_json JSON NOT NULL, revision INTEGER NOT NULL, "
                "started_at DATETIME, completed_at DATETIME, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, CONSTRAINT uq_sop_node_execution_attempt UNIQUE "
                "(tenant_id, instance_id, node_id, attempt))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_execution_id VARCHAR(128) NOT NULL, "
                "operation_name VARCHAR(191) NOT NULL, idempotency_key VARCHAR(64) NOT NULL, "
                "status VARCHAR(64) NOT NULL, request_json JSON NOT NULL, "
                "result_json JSON NOT NULL, error_json JSON NOT NULL, "
                "external_reference VARCHAR(128), revision INTEGER NOT NULL, started_at DATETIME, "
                "completed_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_sop_operation_tenant_idempotency UNIQUE "
                "(tenant_id, idempotency_key))"
            )
        )
    command.upgrade(config, "20260803_0036")


def test_operation_reliability_migration_backfills_ledgers_and_is_reversible(tmp_path) -> None:
    """验证 0037 保守分类旧效果、生成单 attempt/事实账本，并能无损回退纯历史数据。"""

    database_url = f"sqlite:///{tmp_path / 'operation-reliability.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0036_operation_database(engine, config)
    with engine.begin() as connection:
        _seed_0036_operation_history(connection)

    command.upgrade(config, "20260803_0037")
    with engine.connect() as connection:
        inspector = inspect(connection)
        operation_columns = {
            column["name"] for column in inspector.get_columns("sop_operations")
        }
        assert {
            "logical_action_id",
            "request_fingerprint",
            "remote_idempotency_key",
            "idempotency_required",
            "effect_kind",
            "effect_state",
            "reconciled_at",
        }.issubset(operation_columns)
        rows = connection.execute(
            text(
                "SELECT id, logical_action_id, request_fingerprint, remote_idempotency_key, "
                "effect_kind, effect_state FROM sop_operations ORDER BY id"
            )
        ).mappings().all()
        by_id = {row["id"]: row for row in rows}
        assert by_id["op_read"]["effect_kind"] == "read"
        assert by_id["op_read"]["effect_state"] == "none"
        assert by_id["op_write"]["effect_state"] == "complete"
        assert by_id["op_running"]["effect_state"] == "unknown"
        assert all(str(row["logical_action_id"]).startswith("legacy:") for row in rows)
        assert all(len(str(row["request_fingerprint"])) == 64 for row in rows)
        assert all(row["remote_idempotency_key"] is None for row in rows)
        assert connection.execute(
            text("SELECT COUNT(*) FROM sop_operation_attempts")
        ).scalar_one() == 3
        assert connection.execute(
            text("SELECT COUNT(*) FROM sop_operation_effects")
        ).scalar_one() == 2
        assert connection.execute(
            text("SELECT effect_state FROM sop_instances WHERE id='inst_reliable'")
        ).scalar_one() == "unknown"

    command.downgrade(config, "20260803_0036")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert not inspector.has_table("sop_operation_attempts")
        assert not inspector.has_table("sop_operation_effects")
        assert "logical_action_id" not in {
            column["name"] for column in inspector.get_columns("sop_operations")
        }
        assert connection.execute(text("SELECT COUNT(*) FROM sop_operations")).scalar_one() == 3


def test_operation_reliability_migration_rejects_invalid_json_and_managed_downgrade(
    tmp_path,
) -> None:
    """验证严格 JSON 预检中止脏迁移，且真实新逻辑动作会阻止破坏性降级。"""

    invalid_url = f"sqlite:///{tmp_path / 'operation-invalid-json.db'}"
    invalid_config = Config(str(BACKEND_DIR / "alembic.ini"))
    invalid_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    invalid_config.attributes["database_url"] = invalid_url
    invalid_engine = create_engine(invalid_url)
    _prepare_0036_operation_database(invalid_engine, invalid_config)
    with invalid_engine.begin() as connection:
        _seed_0036_operation_history(connection, invalid_request=True)
    with pytest.raises(RuntimeError, match="unmappable legacy operations"):
        command.upgrade(invalid_config, "20260803_0037")

    managed_url = f"sqlite:///{tmp_path / 'operation-managed-downgrade.db'}"
    managed_config = Config(str(BACKEND_DIR / "alembic.ini"))
    managed_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    managed_config.attributes["database_url"] = managed_url
    managed_engine = create_engine(managed_url)
    _prepare_0036_operation_database(managed_engine, managed_config)
    with managed_engine.begin() as connection:
        _seed_0036_operation_history(connection)
    command.upgrade(managed_config, "20260803_0037")
    with managed_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sop_operations SET logical_action_id='managed-action' "
                "WHERE id='op_write'"
            )
        )
    with pytest.raises(RuntimeError, match="would discard managed history"):
        command.downgrade(managed_config, "20260803_0036")


def test_operation_reliability_migration_resumes_after_partial_expand(tmp_path) -> None:
    """验证模拟 MySQL 非事务 DDL 中断后的部分 0037 列可继续回填并建立完整账本。"""

    database_url = f"sqlite:///{tmp_path / 'operation-partial-expand.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0036_operation_database(engine, config)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE sop_instances ADD COLUMN effect_state VARCHAR(64)")
        )
        connection.execute(
            text("ALTER TABLE sop_operations ADD COLUMN logical_action_id VARCHAR(128)")
        )
        connection.execute(
            text("ALTER TABLE sop_operations ADD COLUMN request_fingerprint VARCHAR(64)")
        )
        _seed_0036_operation_history(connection)

    command.upgrade(config, "20260803_0037")
    command.upgrade(config, "20260803_0037")
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("sop_operations")}
        assert {"idempotency_required", "effect_kind", "effect_state"}.issubset(columns)
        assert connection.execute(
            text("SELECT COUNT(*) FROM sop_operation_attempts")
        ).scalar_one() == 3
        assert connection.execute(
            text("SELECT effect_state FROM sop_instances WHERE id='inst_reliable'")
        ).scalar_one() == "unknown"


def _prepare_0037_capability_database(engine) -> None:  # noqa: ANN001
    """创建 B0.3 所需的最小 0037 表，避免把无关历史 DDL 引入迁移单测。"""

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260803_0037')"))
        connection.execute(text("CREATE TABLE tools (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE general_skills (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE model_configs (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE sop_operations (id VARCHAR(512) PRIMARY KEY)"))
        for table_name in ("tools", "general_skills", "model_configs", "sop_operations"):
            connection.execute(text(f"INSERT INTO {table_name} (id) VALUES ('legacy')"))


def test_dynamic_capability_migration_is_fail_closed_resumable_and_reversible(tmp_path) -> None:
    """验证 0038 默认关闭动态能力、保留旧原子技能语义，并可从部分 DDL 续跑。"""

    database_url = f"sqlite:///{tmp_path / 'capability-catalog.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0037_capability_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE tools ADD COLUMN reliability_contract_json JSON")
        )

    command.upgrade(config, "20260803_0038")
    command.upgrade(config, "20260803_0038")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "reliability_contract_json",
            "reliability_checksum",
            "reliability_published_at",
        }.issubset({item["name"] for item in inspector.get_columns("tools")})
        assert connection.execute(
            text("SELECT reliability_contract_json FROM tools WHERE id='legacy'")
        ).scalar_one() == "{}"
        assert connection.execute(
            text("SELECT usage_mode FROM general_skills WHERE id='legacy'")
        ).scalar_one() == "atomic_execution"
        assert connection.execute(
            text("SELECT preflight_status FROM model_configs WHERE id='legacy'")
        ).scalar_one() == "unverified"

    command.downgrade(config, "20260803_0037")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "reliability_contract_json" not in {
            item["name"] for item in inspector.get_columns("tools")
        }
        assert "capability_snapshot_json" not in {
            item["name"] for item in inspector.get_columns("sop_operations")
        }


def test_dynamic_capability_migration_refuses_to_discard_published_history(tmp_path) -> None:
    """验证工具契约一旦发布，0038 拒绝回退为无法表达该事实的 0037。"""

    database_url = f"sqlite:///{tmp_path / 'capability-managed.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0037_capability_database(engine)
    command.upgrade(config, "20260803_0038")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tools SET reliability_contract_json=:contract, "
                "reliability_checksum='checksum' WHERE id='legacy'"
            ),
            {"contract": '{"dynamic_task_enabled":true}'},
        )

    with pytest.raises(RuntimeError, match="managed history"):
        command.downgrade(config, "20260803_0037")


def test_desktop_sqlite_legacy_path_backfills_execution_and_capability_contracts(
    tmp_path,
) -> None:
    """验证不走 Alembic 的桌面旧库也获得 B0.1～B0.3 列、账本与 fail-closed 默认。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'desktop-capabilities.db'}")
    with engine.begin() as connection:
        _create_legacy_sop_instances_table(connection)
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_execution_id VARCHAR(128) NOT NULL, "
                "operation_name VARCHAR(191) NOT NULL, idempotency_key VARCHAR(64) NOT NULL, "
                "status VARCHAR(64) NOT NULL, request_json JSON NOT NULL, "
                "result_json JSON NOT NULL, error_json JSON NOT NULL, "
                "external_reference VARCHAR(128), revision INTEGER NOT NULL, started_at DATETIME, "
                "completed_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sop_node_executions ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_id VARCHAR(128) NOT NULL, "
                "attempt INTEGER NOT NULL, status VARCHAR(64) NOT NULL, input_json JSON NOT NULL, "
                "output_json JSON NOT NULL, error_json JSON NOT NULL, revision INTEGER NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("CREATE TABLE tools (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE general_skills (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE model_configs (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(
            text(
                "INSERT INTO sop_instances ("
                "id, tenant_id, session_id, skill_id, skill_version_id, skill_version, "
                "definition_checksum, run_number, status, current_node_id, slots_json, "
                "context_json, revision, created_at, updated_at"
                ") VALUES ("
                "'inst_legacy', 'tenant_a', 'session_a', 'skill_a', 'version_a', '1.0.0', "
                ":checksum, 1, 'running', 'submit', '{}', '{}', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO sop_operations ("
                "id, tenant_id, instance_id, node_execution_id, operation_name, "
                "idempotency_key, status, request_json, result_json, error_json, revision, "
                "created_at, updated_at"
                ") VALUES ("
                "'op_legacy', 'tenant_a', 'inst_legacy', 'node_legacy', 'message.send', "
                "'key_legacy', 'running', '{\"recipient\":\"ops\"}', '{}', '{}', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_node_executions ("
                "id, tenant_id, instance_id, node_id, attempt, status, input_json, output_json, "
                "error_json, revision, created_at, updated_at"
                ") VALUES ("
                "'node_legacy', 'tenant_a', 'inst_legacy', 'submit', 1, 'running', '{}', '{}', "
                "'{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        for table_name in ("tools", "general_skills", "model_configs"):
            connection.execute(text(f"INSERT INTO {table_name} (id) VALUES ('legacy')"))
    SQLModel.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        _migrate_execution_reliability_fields(connection, tables)
        _migrate_dynamic_capability_fields(connection, tables)
        _migrate_execution_plan_fields(connection, tables)
        _migrate_execution_reliability_fields(connection, tables)
        _migrate_dynamic_capability_fields(connection, tables)
        _migrate_execution_plan_fields(connection, tables)

    with engine.connect() as connection:
        instance = connection.execute(
            text(
                "SELECT kind, active_slot_key, fencing_token, effect_state "
                "FROM sop_instances WHERE id='inst_legacy'"
            )
        ).one()
        operation = connection.execute(
            text(
                "SELECT logical_action_id, request_fingerprint, effect_kind, effect_state, "
                "capability_snapshot_json FROM sop_operations WHERE id='op_legacy'"
            )
        ).one()
        assert tuple(instance) == ("sop", "foreground:session_a", 0, "unknown")
        assert str(operation[0]).startswith("legacy:")
        assert len(str(operation[1])) == 64
        assert tuple(operation[2:4]) == ("external_write", "unknown")
        assert operation[4] == "{}"
        assert connection.execute(
            text("SELECT COUNT(*) FROM sop_operation_attempts")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM sop_operation_effects")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT preflight_status FROM model_configs WHERE id='legacy'")
        ).scalar_one() == "unverified"
        assert connection.execute(
            text(
                "SELECT step_key, step_kind, required FROM sop_node_executions "
                "WHERE id='node_legacy'"
            )
        ).one() == ("submit", "sop_node", 1)
        assert "execution_plan_revisions" in inspect(connection).get_table_names()


def test_desktop_sqlite_legacy_path_rejects_invalid_operation_request(tmp_path) -> None:
    """验证桌面兼容迁移不会把损坏请求静默降级为空对象后误判副作用。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'desktop-invalid-operation.db'}")
    with engine.begin() as connection:
        _create_legacy_sop_instances_table(connection)
        connection.execute(
            text(
                "CREATE TABLE sop_operations ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_execution_id VARCHAR(128) NOT NULL, "
                "operation_name VARCHAR(191) NOT NULL, idempotency_key VARCHAR(64) NOT NULL, "
                "status VARCHAR(64) NOT NULL, request_json JSON NOT NULL, "
                "result_json JSON NOT NULL, error_json JSON NOT NULL, "
                "revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_operations ("
                "id, tenant_id, instance_id, node_execution_id, operation_name, idempotency_key, "
                "status, request_json, result_json, error_json, revision, created_at, updated_at"
                ") VALUES ("
                "'op_invalid', 'tenant_a', 'inst_missing', 'node_invalid', 'message.send', "
                "'key_invalid', 'running', :request_json, '{}', '{}', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"request_json": '{"value":NaN}'},
        )
    tables = set(inspect(engine).get_table_names())

    with engine.begin() as connection, pytest.raises(
        RuntimeError,
        match=r"sop_operations\[op_invalid\]\.request_json",
    ):
        _migrate_execution_reliability_fields(connection, tables)


def _prepare_0038_execution_plan_database(engine, config) -> None:  # noqa: ANN001
    """从 0036 最小 Runtime 表构造 0038 时点数据库，隔离无关历史 SQLite DDL。"""

    _prepare_0036_operation_database(engine, config)
    command.upgrade(config, "20260803_0037")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tools (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE general_skills (id VARCHAR(512) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE model_configs (id VARCHAR(512) PRIMARY KEY)"))
    command.upgrade(config, "20260803_0038")


def test_execution_plan_resource_migration_backfills_steps_and_round_trips_sqlite(
    tmp_path,
) -> None:
    """验证 0039 为正式 SOP 回填稳定 step key、创建新账本并可在无新事实时回退。"""

    database_url = f"sqlite:///{tmp_path / 'execution-plan-resources.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0038_execution_plan_database(engine, config)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE sop_instances ADD COLUMN agent_id VARCHAR(128)"))
        connection.execute(
            text("ALTER TABLE sop_node_executions ADD COLUMN step_key VARCHAR(128)")
        )
        connection.execute(
            text(
                "INSERT INTO sop_instances ("
                "id, tenant_id, session_id, skill_id, skill_version_id, skill_version, "
                "definition_checksum, run_number, kind, active_slot_key, source_kind, status, "
                "current_node_id, slots_json, context_json, revision, cancellation_disposition, "
                "fencing_token, effect_state, created_at, updated_at"
                ") VALUES ("
                "'instance_0039', 'tenant_a', 'session_a', 'skill_a', 'version_a', '1.0.0', "
                ":checksum, 1, 'sop', 'foreground:session_a', 'chat', 'running', 'collect', "
                "'{}', '{}', 0, 'none', 0, 'none', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO sop_node_executions ("
                "id, tenant_id, instance_id, node_id, attempt, status, input_json, output_json, "
                "error_json, revision, created_at, updated_at"
                ") VALUES ("
                "'node_0039', 'tenant_a', 'instance_0039', 'collect', 1, 'running', '{}', '{}', "
                "'{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260803_0039")
    command.upgrade(config, "20260803_0039")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert set(
            (
                "execution_plan_revisions",
                "action_proposal_records",
                "managed_input_resources",
                "input_resource_snapshots",
            )
        ).issubset(inspector.get_table_names())
        assert connection.execute(
            text(
                "SELECT step_key, step_kind, required FROM sop_node_executions "
                "WHERE id='node_0039'"
            )
        ).one() == ("collect", "sop_node", 1)

    command.downgrade(config, "20260803_0038")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "execution_plan_revisions" not in inspector.get_table_names()
        assert "step_key" not in {
            column["name"] for column in inspector.get_columns("sop_node_executions")
        }


def test_execution_plan_resource_migration_rejects_legacy_dynamic_and_managed_downgrade(
    tmp_path,
) -> None:
    """验证 0039 无法推断旧动态身份时中止，已有动态历史时也禁止降级丢失。"""

    legacy_url = f"sqlite:///{tmp_path / 'legacy-dynamic-0039.db'}"
    legacy_config = Config(str(BACKEND_DIR / "alembic.ini"))
    legacy_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    legacy_config.attributes["database_url"] = legacy_url
    legacy_engine = create_engine(legacy_url)
    _prepare_0038_execution_plan_database(legacy_engine, legacy_config)
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sop_instances ("
                "id, tenant_id, session_id, run_number, kind, active_slot_key, source_kind, status, "
                "slots_json, context_json, revision, cancellation_disposition, fencing_token, "
                "effect_state, created_at, updated_at"
                ") VALUES ("
                "'legacy_dynamic', 'tenant_a', 'session_a', 1, 'dynamic_task', "
                "'foreground:session_a', 'api', 'running', '{}', '{}', 0, 'none', 0, 'none', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(RuntimeError, match="cannot infer identity"):
        command.upgrade(legacy_config, "20260803_0039")

    managed_url = f"sqlite:///{tmp_path / 'managed-dynamic-0039.db'}"
    managed_config = Config(str(BACKEND_DIR / "alembic.ini"))
    managed_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    managed_config.attributes["database_url"] = managed_url
    managed_engine = create_engine(managed_url)
    _prepare_0038_execution_plan_database(managed_engine, managed_config)
    command.upgrade(managed_config, "20260803_0039")
    with managed_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sop_instances ("
                "id, tenant_id, session_id, run_number, kind, active_slot_key, initiator_user_id, "
                "source_kind, agent_id, goal_snapshot_json, current_plan_revision_id, "
                "current_plan_checksum, capability_snapshot_json, budget_snapshot_json, "
                "terminal_reason_json, status, slots_json, context_json, revision, "
                "cancellation_disposition, fencing_token, effect_state, created_at, updated_at"
                ") VALUES ("
                "'managed_dynamic', 'tenant_a', 'session_a', 1, 'dynamic_task', "
                "'foreground:session_a', 'user_a', 'chat', 'agent_a', '{\"goal\":\"demo\"}', "
                "'plan_a', :checksum, '{}', '{}', '{}', 'running', '{}', '{}', 0, 'none', 0, "
                "'none', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="managed history"):
        command.downgrade(managed_config, "20260803_0038")


def test_execution_control_migration_backfills_attention_and_round_trips_sqlite(
    tmp_path,
) -> None:
    """验证 0040 回填正式 SOP Attention、扩展事件并在无新事实时安全回退。"""

    database_url = f"sqlite:///{tmp_path / 'execution-control.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    with engine.begin() as connection:
        metadata = MetaData()
        work_items = Table("sop_work_items", metadata, autoload_with=connection)
        connection.execute(
            work_items.insert().values(
                id="work_0040",
                tenant_id="tenant_a",
                instance_id="instance_a",
                node_execution_id="node_a",
                skill_version_id="version_a",
                node_id="approve",
                status="offered",
                completion_mode="single",
                claim_required=False,
                required_count=None,
                exclude_initiator=True,
                allowed_outcomes_json=["approved", "rejected"],
                outcome_options_json=[],
                action_permissions_json={},
                candidate_snapshot_json=[],
                participant_scope_snapshot_json={},
                revision=0,
                timeout_action="fail",
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
        events = Table("agent_events", metadata, autoload_with=connection)
        connection.execute(
            events.insert().values(
                id="event_0040",
                tenant_id="tenant_a",
                session_id="session_a",
                event_type="legacy_event",
                payload_json={},
                created_at=datetime.now(),
            )
        )

    command.upgrade(config, "20260803_0040")
    command.upgrade(config, "20260803_0040")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert set(
            (
                "execution_commands",
                "execution_signals",
                "execution_results",
                "execution_publications",
                "event_outbox",
            )
        ).issubset(inspector.get_table_names())
        attention = connection.execute(
            text(
                "SELECT attention_kind, attention_key, LENGTH(attention_identity), required "
                "FROM sop_work_items WHERE id='work_0040'"
            )
        ).one()
        assert attention == ("sop_human_task", "sop-node:node_a", 64, 1)
        assert connection.execute(
            text("SELECT schema_version FROM agent_events WHERE id='event_0040'")
        ).scalar_one() == 1
        nullable = {
            column["name"]: column["nullable"]
            for column in inspector.get_columns("sop_work_items")
        }
        assert nullable["node_execution_id"] is True

    command.downgrade(config, "20260803_0039")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "execution_commands" not in inspector.get_table_names()
        assert "attention_kind" not in {
            column["name"] for column in inspector.get_columns("sop_work_items")
        }


def test_execution_control_migration_guards_new_facts_on_downgrade(tmp_path) -> None:
    """验证已有持久命令时 0040 拒绝降级丢失控制事实。"""

    database_url = f"sqlite:///{tmp_path / 'execution-control-managed.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    command.upgrade(config, "20260803_0040")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_commands ("
                "id, tenant_id, execution_id, command_id, command_type, source_type, "
                "expected_execution_revision, payload_json, payload_checksum, status, "
                "result_json, issued_at, created_at, updated_at"
                ") VALUES ("
                "'cmd_row', 'tenant_a', 'execution_a', 'cmd_a', 'steer', 'api', 0, '{}', "
                ":checksum, 'pending', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="execution_commands facts"):
        command.downgrade(config, "20260803_0039")


def test_execution_control_migration_rejects_full_columns_without_constraints(tmp_path) -> None:
    """验证 0040 不把仅有同名列、却缺少幂等和状态约束的半成品控制表当成成功。"""

    database_url = f"sqlite:///{tmp_path / 'execution-control-partial.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    metadata = MetaData()
    Table(
        "execution_commands",
        metadata,
        *(
            Column(
                column.name,
                column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
            )
            for column in ExecutionCommand.__table__.columns
        ),
    )
    metadata.create_all(engine)

    with pytest.raises(RuntimeError, match="missing constraints"):
        command.upgrade(config, "20260803_0040")


def test_execution_artifact_migration_is_repeatable_and_guards_facts(tmp_path) -> None:
    """验证 0041 可重复升级并在 Artifact/lineage 已存在时 fail closed。"""

    database_url = f"sqlite:///{tmp_path / 'execution-artifacts.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    command.upgrade(config, "20260804_0041")
    command.upgrade(config, "20260804_0041")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {"execution_artifacts", "artifact_input_links"} <= set(
            inspector.get_table_names()
        )
        artifact_indexes = {item["name"] for item in inspector.get_indexes("execution_artifacts")}
        assert "ix_execution_artifacts_tenant_checksum" in artifact_indexes
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO execution_artifacts ("
                "id, tenant_id, execution_id, source_node_execution_id, source_step_key, "
                "artifact_key, filename, mime_type, size_bytes, content_checksum, "
                "storage_locator, acl_json, lineage_json, status, created_at, updated_at"
                ") VALUES ("
                "'artifact_1', 'tenant_1', 'execution_1', 'node_1', 'answer', 'brief', "
                "'brief.md', 'text/markdown', 5, :checksum, 'managed/locator', '{}', '{}', "
                "'ready', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="cannot downgrade execution artifacts"):
        command.downgrade(config, "20260803_0040")


def test_connection_profile_migration_is_repeatable_and_guards_secrets(tmp_path) -> None:
    """验证 0042 可重复升级、包含解析索引，并拒绝删除已有密钥事实。"""

    database_url = f"sqlite:///{tmp_path / 'connection-profiles.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    with engine.begin() as connection:
        for table_name in ("tenants", "users", "agent_profiles"):
            connection.execute(text(f"CREATE TABLE {table_name} (id VARCHAR(512) PRIMARY KEY)"))
    command.upgrade(config, "20260810_0042")
    command.upgrade(config, "20260810_0042")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert {
            "connection_secrets",
            "connection_profiles",
            "agent_connection_bindings",
            "connection_command_receipts",
            "connection_oauth_states",
        } <= set(inspector.get_table_names())
        binding_indexes = {
            item["name"] for item in inspector.get_indexes("agent_connection_bindings")
        }
        assert "ix_agent_connection_bindings_resolve" in binding_indexes
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO connection_secrets ("
                "id, tenant_id, provider, reference_id, encrypted_payload, revision, status, "
                "created_at, updated_at) VALUES ("
                "'secret_1', 'tenant_1', 'slack', 'ref_1', 'ciphertext', 1, 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(RuntimeError, match="cannot downgrade connection profiles"):
        command.downgrade(config, "20260804_0041")


def test_connector_inbound_migration_is_repeatable_and_guards_received_facts(tmp_path) -> None:
    """验证 0043 可重复升级并拒绝通过回退删除已经确认接收的外部事件。"""

    database_url = f"sqlite:///{tmp_path / 'connector-inbound.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    with engine.begin() as connection:
        for table_name in ("tenants", "users", "agent_profiles"):
            connection.execute(text(f"CREATE TABLE {table_name} (id VARCHAR(512) PRIMARY KEY)"))
    command.upgrade(config, "20260810_0043")
    command.upgrade(config, "20260810_0043")
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert "connector_inbound_events" in inspector.get_table_names()
        indexes = {
            item["name"] for item in inspector.get_indexes("connector_inbound_events")
        }
        assert "ix_connector_inbound_dispatch" in indexes
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO connector_inbound_events ("
                "id, tenant_id, provider, profile_id, external_event_id, payload_checksum, "
                "encrypted_payload, event_type, status, attempt_count, available_at, "
                "created_at, updated_at) VALUES ("
                "'event_1', 'tenant_1', 'wecom', 'profile_1', 'message_1', :checksum, "
                "'ciphertext', 'text', 'pending', 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"checksum": "a" * 64},
        )
    with pytest.raises(RuntimeError, match="cannot downgrade connector inbox"):
        command.downgrade(config, "20260810_0042")


def test_controlled_write_migration_backfills_and_guards_authorization_facts(tmp_path) -> None:
    """验证 0045 双方言安全字段、空白名单回填及已有写授权事实的降级拒绝。"""

    database_url = f"sqlite:///{tmp_path / 'controlled-writes.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    _prepare_0039_execution_control_database(engine, config)
    with engine.begin() as connection:
        for table_name in ("tenants", "users", "agent_profiles"):
            connection.execute(text(f"CREATE TABLE {table_name} (id VARCHAR(512) PRIMARY KEY)"))
    command.upgrade(config, "20260810_0044")
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE agent_connection_bindings "
                "ADD COLUMN allowed_actions_json JSON NULL"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE sop_operations "
                "ADD COLUMN authorization_evidence_json JSON NULL"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_connection_bindings ("
                "id, tenant_id, agent_id, profile_id, allowed_scopes_json, enabled, revision, "
                "created_by_user_id, updated_by_user_id, created_at, updated_at) VALUES ("
                "'binding_1', 'tenant_1', 'agent_1', 'profile_1', '[]', 1, 1, "
                "'user_1', 'user_1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(config, "20260810_0045")
    command.upgrade(config, "20260810_0045")
    with engine.connect() as connection:
        inspector = inspect(connection)
        binding_columns = {
            item["name"]: item for item in inspector.get_columns("agent_connection_bindings")
        }
        operation_columns = {
            item["name"]: item for item in inspector.get_columns("sop_operations")
        }
        assert binding_columns["allowed_actions_json"]["nullable"] is False
        assert operation_columns["authorization_evidence_json"]["nullable"] is False
        assert {
            "approval_work_item_id",
            "approval_fingerprint",
            "approved_by_user_id",
            "approved_at",
            "authorization_evidence_json",
            "dispatched_at",
        } <= set(operation_columns)
        assert connection.execute(
            text(
                "SELECT allowed_actions_json FROM agent_connection_bindings "
                "WHERE id = 'binding_1'"
            )
        ).scalar_one() == "[]"
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_connection_bindings "
                "SET allowed_actions_json = :actions WHERE id = 'binding_1'"
            ),
            {"actions": '["wecom.message_send"]'},
        )
    with pytest.raises(RuntimeError, match="cannot downgrade controlled writes"):
        command.downgrade(config, "20260810_0044")


def test_desktop_sqlite_rebuilds_legacy_work_item_as_typed_attention(tmp_path) -> None:
    """验证桌面旧库重建工作项后可插入无正式 SOP 节点身份的通用 Attention。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'desktop-attention.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sop_instances (id VARCHAR(512) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sop_work_items ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_execution_id VARCHAR(128) NOT NULL, "
                "skill_version_id VARCHAR(128) NOT NULL, node_id VARCHAR(128) NOT NULL, "
                "status VARCHAR(64) NOT NULL, completion_mode VARCHAR(64) NOT NULL, "
                "claim_required BOOLEAN NOT NULL, exclude_initiator BOOLEAN NOT NULL, "
                "allowed_outcomes_json JSON NOT NULL, candidate_snapshot_json JSON NOT NULL, "
                "revision INTEGER NOT NULL, timeout_action VARCHAR(64) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_events (id VARCHAR(512) PRIMARY KEY, "
                "tenant_id VARCHAR(128) NOT NULL, session_id VARCHAR(128) NOT NULL, "
                "event_type VARCHAR(64) NOT NULL, payload_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sop_work_items ("
                "id, tenant_id, instance_id, node_execution_id, skill_version_id, node_id, "
                "status, completion_mode, claim_required, exclude_initiator, "
                "allowed_outcomes_json, candidate_snapshot_json, revision, timeout_action, "
                "created_at, updated_at) VALUES ("
                "'work_old', 'tenant_a', 'instance_a', 'node_a', 'version_a', 'approve', "
                "'offered', 'single', 0, 1, '[]', '[]', 0, 'fail', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            )
        )
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        _migrate_execution_control_fields(connection, tables)
        _migrate_execution_control_fields(connection, tables)
        connection.execute(
            text(
                "INSERT INTO sop_work_items ("
                "id, tenant_id, instance_id, attention_kind, attention_key, "
                "attention_identity, source_type, payload_json, allowed_commands_json, "
                "resolution_json, required, status, completion_mode, claim_required, "
                "exclude_initiator, allowed_outcomes_json, outcome_options_json, "
                "action_permissions_json, candidate_snapshot_json, "
                "participant_scope_snapshot_json, revision, timeout_action, created_at, "
                "updated_at) VALUES ("
                "'work_generic', 'tenant_a', 'instance_a', 'clarification', 'step:question', "
                ":identity, 'runtime', '{}', '[\"answer\"]', '{}', 1, 'offered', 'single', "
                "0, 0, '[\"answer\"]', '[]', '{}', '[]', '{}', 0, 'fail', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"identity": "b" * 64},
        )
    with engine.connect() as connection:
        inspector = inspect(connection)
        nullable = {
            column["name"]: column["nullable"]
            for column in inspector.get_columns("sop_work_items")
        }
        assert nullable["node_execution_id"] is True
        assert connection.execute(
            text("SELECT attention_kind FROM sop_work_items WHERE id='work_old'")
        ).scalar_one() == "sop_human_task"
        assert connection.execute(
            text("SELECT node_execution_id FROM sop_work_items WHERE id='work_generic'")
        ).scalar_one_or_none() is None


def test_context_compaction_migration_resumes_after_partial_ddl(tmp_path) -> None:
    """验证 0072 在 MySQL 风格逐列 DDL 中断后可跳过已完成列并继续升级。"""

    database_url = f"sqlite:///{tmp_path / 'context-compaction-partial.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ui_configs ("
                "id VARCHAR(128) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL)"
            )
        )
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260820_0071')"))
        connection.execute(
            text(
                "ALTER TABLE ui_configs ADD COLUMN context_token_budget "
                "INTEGER NOT NULL DEFAULT 32000"
            )
        )

    command.upgrade(config, "20260828_0072")
    command.upgrade(config, "20260828_0072")
    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("ui_configs")}
        assert {
            "context_token_budget",
            "context_compaction_trigger_ratio",
            "context_recent_round_limit",
        } <= columns

    command.downgrade(config, "20260820_0071")
    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("ui_configs")}
        assert not {
            "context_token_budget",
            "context_compaction_trigger_ratio",
            "context_recent_round_limit",
        } & columns


def _prepare_0039_execution_control_database(engine, config: Config) -> None:
    """建立避开早期 SQLite 方言缺陷、但字段与 0039 一致的控制迁移基线。"""

    _prepare_0038_execution_plan_database(engine, config)
    command.upgrade(config, "20260803_0039")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sop_work_items ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "instance_id VARCHAR(128) NOT NULL, node_execution_id VARCHAR(128) NOT NULL, "
                "skill_version_id VARCHAR(128) NOT NULL, node_id VARCHAR(128) NOT NULL, "
                "status VARCHAR(64) NOT NULL, owner_user_id VARCHAR(128), "
                "assignee_user_id VARCHAR(128), initiator_user_id VARCHAR(128), "
                "subject_employee_profile_id VARCHAR(128), completion_mode VARCHAR(64) NOT NULL, "
                "claim_required BOOLEAN NOT NULL, required_count INTEGER, "
                "exclude_initiator BOOLEAN NOT NULL, allowed_outcomes_json JSON NOT NULL, "
                "outcome_options_json JSON NOT NULL, action_permissions_json JSON NOT NULL, "
                "candidate_snapshot_json JSON NOT NULL, "
                "participant_scope_snapshot_json JSON NOT NULL, outcome VARCHAR(64), "
                "comment TEXT, revision INTEGER NOT NULL, expires_at DATETIME, "
                "timeout_action VARCHAR(64) NOT NULL, claimed_at DATETIME, completed_at DATETIME, "
                "expired_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_sop_work_item_node_execution UNIQUE "
                "(tenant_id, node_execution_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_events ("
                "id VARCHAR(512) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL, "
                "session_id VARCHAR(128) NOT NULL, event_type VARCHAR(64) NOT NULL, "
                "payload_json JSON NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
