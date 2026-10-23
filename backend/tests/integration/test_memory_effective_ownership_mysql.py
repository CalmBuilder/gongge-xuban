"""
@Time       : 2026/09/25
@Author     : zhanglp8181
@File       : test_memory_effective_ownership_mysql.py
@CallChain  : pytest mysql → Alembic head → MemoryService/API → MySQL记忆归属与分页
@Description: 在隔离 MySQL 上验证有效归属过滤、结构化去重和用户组分页一致性。
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, create_engine, select

from app.api.memories import clear_my_memories, list_memories, page_memories
from app.db.models import AgentProfile, ChatSession, MemoryRecord, Tenant, User
from app.memory.service import MemoryService


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把本轮隔离 MySQL 数据库升级到当前 Alembic head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def test_mysql_memory_scope_filters_history_before_limit_and_paginates_stably(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 大量其他员工历史不会挤占目标记忆，且分页与同键规则稳定。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd2_mysql_memory"
        other_tenant_id = "tenant_sd2_mysql_other"
        target_agent_id = "agent_sd2_mysql_target"
        other_agent_id = "agent_sd2_mysql_other"
        owner = User(
            id="user_sd2_mysql_a",
            tenant_id=tenant_id,
            username="sd2_mysql_owner",
            password_hash="hash",
        )
        user_b = User(
            id="user_sd2_mysql_b",
            tenant_id=tenant_id,
            username="sd2_mysql_b",
            password_hash="hash",
        )
        db.add_all(
            [
                Tenant(id=tenant_id, name="SD2 MySQL"),
                Tenant(id=other_tenant_id, name="SD2 MySQL Other"),
                owner,
                user_b,
                AgentProfile(
                    id=target_agent_id,
                    tenant_id=tenant_id,
                    name="SD2 MySQL Target",
                    status="active",
                    owner_user_id=owner.id,
                ),
                AgentProfile(
                    id=other_agent_id,
                    tenant_id=tenant_id,
                    name="SD2 MySQL Other Agent",
                    status="active",
                ),
                ChatSession(
                    id="session_sd2_mysql_target",
                    tenant_id=tenant_id,
                    user_id=owner.id,
                    agent_id=target_agent_id,
                ),
                ChatSession(
                    id="session_sd2_mysql_b",
                    tenant_id=tenant_id,
                    user_id=user_b.id,
                    agent_id=target_agent_id,
                ),
                ChatSession(
                    id="session_sd2_mysql_cross_tenant",
                    tenant_id=other_tenant_id,
                    user_id="user_sd2_mysql_other_tenant",
                    agent_id=target_agent_id,
                ),
            ]
        )
        base_time = datetime(2026, 8, 1, 10, 0)
        owner_id = owner.id
        user_b_id = user_b.id
        db.add_all(
            [
                MemoryRecord(
                    id="memory_sd2_mysql_target",
                    tenant_id=tenant_id,
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_sd2_mysql_target",
                    kind="profile",
                    content="MySQL 目标历史",
                    metadata_json={"key": "preferred_name"},
                    updated_at=base_time,
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_b",
                    tenant_id=tenant_id,
                    user_id=user_b.id,
                    username=user_b.username,
                    session_id="session_sd2_mysql_b",
                    kind="fact",
                    content="MySQL 用户 B 目标历史",
                    metadata_json={},
                    updated_at=base_time,
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_cross_tenant",
                    tenant_id=tenant_id,
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_sd2_mysql_cross_tenant",
                    kind="fact",
                    content="MySQL 跨租户会话",
                    metadata_json={},
                    updated_at=base_time + timedelta(minutes=10),
                ),
            ]
        )
        db.add_all(
            [
                MemoryRecord(
                    id=f"memory_sd2_mysql_other_{index:03d}",
                    tenant_id=tenant_id,
                    agent_id=other_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content=f"MySQL 其他员工 {index}",
                    updated_at=base_time + timedelta(minutes=index + 11),
                )
                for index in range(151)
            ]
        )
        db.commit()

        service_rows = MemoryService(db)._list_user_memories(
            tenant_id,
            owner.id,
            limit=1,
            agent_id=target_agent_id,
        )
        listed = list_memories(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            user_id=None,
            username=None,
            q="MySQL 目标",
            limit=1,
            current_user=owner,
            db=db,
        )
        first_page = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "目标历史",
            1,
            1,
            owner,
            db,
        )
        second_page = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "目标历史",
            2,
            1,
            owner,
            db,
        )

        updated = MemoryService(db)._upsert_keyed_memory(
            tenant_id=tenant_id,
            user_id=owner.id,
            username=owner.username,
            session_id="session_sd2_mysql_target",
            kind="profile",
            key="preferred_name",
            content="MySQL 目标更新",
            importance=0.9,
            metadata={"key": "preferred_name"},
            agent_id=target_agent_id,
        )
        db.commit()
        service_row_ids = [row.id for row in service_rows]
        listed_contents = [row["content"] for row in listed]
        first_page_user_ids = [row["user_id"] for row in first_page.items]
        second_page_user_ids = [row["user_id"] for row in second_page.items]
        first_page_total = first_page.total
        updated_id = updated.id
    finally:
        db.close()
        engine.dispose()

    assert service_row_ids == ["memory_sd2_mysql_target"]
    assert listed_contents == ["MySQL 目标历史"]
    assert first_page_total == 2
    assert first_page_user_ids == [owner_id]
    assert second_page_user_ids == [user_b_id]
    assert updated_id == "memory_sd2_mysql_target"


def test_mysql_memory_search_dedupes_latest_value_and_preserves_field_contract(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 搜索先去重，并保持 legacy/page 字段与 ILIKE 通配契约。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd2_mysql_search"
        target_agent_id = "agent_sd2_mysql_search_target"
        owner = User(
            id="usersd2mysqlsearch",
            tenant_id=tenant_id,
            username="sd2mysqlsearchuser",
            password_hash="hash",
        )
        db.add_all(
            [
                Tenant(id=tenant_id, name="SD2 MySQL Search"),
                owner,
                AgentProfile(
                    id=target_agent_id,
                    tenant_id=tenant_id,
                    name="SD2 MySQL Search Target",
                    status="active",
                    owner_user_id=owner.id,
                ),
                ChatSession(
                    id="session_sd2_mysql_search",
                    tenant_id=tenant_id,
                    user_id=owner.id,
                    agent_id=target_agent_id,
                ),
            ]
        )
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                MemoryRecord(
                    id="memory_sd2_mysql_search_old",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="obsoletevalue",
                    metadata_json={"key": "preferred_name"},
                    updated_at=base_time,
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_new",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="currentvalue",
                    metadata_json={"key": "preferred_name"},
                    updated_at=base_time + timedelta(minutes=1),
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_plain",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content="plainalpha",
                    metadata_json={"key": "plain"},
                    updated_at=base_time - timedelta(minutes=1),
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_legacy",
                    tenant_id=tenant_id,
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_sd2_mysql_search",
                    kind="fact",
                    content="legacy session value",
                    metadata_json={"key": "legacy"},
                    updated_at=base_time - timedelta(minutes=2),
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_cafe",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content="Café",
                    metadata_json={"key": "accent"},
                    updated_at=base_time - timedelta(minutes=3),
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_percent",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content="100% complete",
                    metadata_json={"key": "literal_percent"},
                    updated_at=base_time - timedelta(minutes=4),
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_search_underscore",
                    tenant_id=tenant_id,
                    agent_id=target_agent_id,
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content="underscore_value",
                    metadata_json={"key": "literal_underscore"},
                    updated_at=base_time - timedelta(minutes=5),
                ),
            ]
        )
        db.commit()

        legacy_old = list_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "obsoletevalue",
            100,
            owner,
            db,
        )
        legacy_new = list_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "currentvalue",
            100,
            owner,
            db,
        )
        legacy_user_id = list_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            owner.id,
            100,
            owner,
            db,
        )
        page_old = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "obsoletevalue",
            1,
            10,
            owner,
            db,
        )
        page_new = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "currentvalue",
            1,
            10,
            owner,
            db,
        )
        page_user_id = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            owner.id,
            1,
            10,
            owner,
            db,
        )
        page_percent = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "%",
            1,
            10,
            owner,
            db,
        )
        page_underscore = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "_",
            1,
            10,
            owner,
            db,
        )
        page_cafe = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            "cafe",
            1,
            10,
            owner,
            db,
        )
        page_literal_percent = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            r"\%",
            1,
            10,
            owner,
            db,
        )
        page_literal_underscore = page_memories(
            tenant_id,
            target_agent_id,
            None,
            None,
            r"\_",
            1,
            10,
            owner,
            db,
        )
    finally:
        db.close()
        engine.dispose()

    assert legacy_old == []
    assert [row["content"] for row in legacy_new] == ["currentvalue"]
    assert legacy_user_id == []
    assert page_old.total == 0
    assert page_old.items == []
    assert page_new.total == 1
    assert [row["content"] for row in page_new.items] == ["currentvalue"]
    assert page_user_id.total == 1
    assert page_percent.total == 1
    assert page_underscore.total == 1
    assert page_cafe.total == 1
    assert [row["content"] for row in page_cafe.items] == ["Café"]
    assert page_literal_percent.total == 1
    assert [row["content"] for row in page_literal_percent.items] == ["100% complete"]
    assert page_literal_underscore.total == 1
    assert [row["content"] for row in page_literal_underscore.items] == ["underscore_value"]


def test_mysql_clear_invalid_agent_scope_does_not_expand_personal_delete(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 上显式无效员工筛选为零命中，缺省和有效筛选仍各守原合同。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    db = Session(engine)
    try:
        tenant_id = "tenant_sd2_mysql_clear"
        owner = User(
            id="user_sd2_mysql_clear",
            tenant_id=tenant_id,
            username="sd2_mysql_clear_user",
            password_hash="hash",
        )
        db.add_all(
            [
                Tenant(id=tenant_id, name="SD2 MySQL Clear"),
                owner,
                AgentProfile(
                    id="agent_sd2_mysql_clear_a",
                    tenant_id=tenant_id,
                    name="SD2 MySQL Clear A",
                    status="active",
                    owner_user_id=owner.id,
                ),
                AgentProfile(
                    id="agent_sd2_mysql_clear_b",
                    tenant_id=tenant_id,
                    name="SD2 MySQL Clear B",
                    status="active",
                    owner_user_id=owner.id,
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_clear_a",
                    tenant_id=tenant_id,
                    agent_id="agent_sd2_mysql_clear_a",
                    user_id=owner.id,
                    kind="fact",
                    content="MySQL clear A",
                ),
                MemoryRecord(
                    id="memory_sd2_mysql_clear_b",
                    tenant_id=tenant_id,
                    agent_id="agent_sd2_mysql_clear_b",
                    user_id=owner.id,
                    kind="fact",
                    content="MySQL clear B",
                ),
            ]
        )
        db.commit()

        invalid = clear_my_memories(tenant_id, "   ", owner, db)
        remaining_after_invalid = {
            row.id
            for row in db.exec(
                select(MemoryRecord).where(MemoryRecord.user_id == owner.id)
            ).all()
        }
        valid = clear_my_memories(tenant_id, "agent_sd2_mysql_clear_a", owner, db)
        remaining_after_valid = {
            row.id
            for row in db.exec(
                select(MemoryRecord).where(MemoryRecord.user_id == owner.id)
            ).all()
        }
    finally:
        db.close()
        engine.dispose()

    assert invalid == {"deleted": 0}
    assert remaining_after_invalid == {
        "memory_sd2_mysql_clear_a",
        "memory_sd2_mysql_clear_b",
    }
    assert valid == {"deleted": 1}
    assert remaining_after_valid == {"memory_sd2_mysql_clear_b"}
