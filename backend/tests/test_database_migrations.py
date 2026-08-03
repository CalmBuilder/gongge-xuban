"""
@Time       : 2026/07/28 12:12
@Author     : zhanglp8181
@File       : test_database_migrations.py
@CallChain  : pytest → Alembic/数据库适配器 → SQLite/MySQL schema
@Description: 验证迁移版本守卫、连接错误脱敏以及关键升降级数据兼容性。
"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, make_url, text
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
from app.db.models import Skill, SkillVersion
from app.db.seed import (
    EXCHANGE_SKILL,
    PRICE_COMPARE_SKILL,
    PURCHASE_SKILL,
    REFUND_SKILL,
    _skill_content_graph,
)
from app.db.sqlite_legacy import initialize_sqlite_database, migrate_sqlite_skill_schema

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
