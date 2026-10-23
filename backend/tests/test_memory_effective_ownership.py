"""
@Time       : 2026/09/25
@Author     : zhanglp8181
@File       : test_memory_effective_ownership.py
@CallChain  : pytest → MemoryService/API → 有效记忆归属 → MemoryRecord/ChatSession
@Description: 验证规范列、兼容 metadata 与同租户会话三来源的统一记忆归属契约。
"""

from datetime import datetime, timedelta
import multiprocessing

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.memories import clear_my_memories, list_memories, page_memories, router
from app.db import get_session
from app.db.models import AgentProfile, ChatSession, MemoryRecord, ModelConfig, Tenant, User
from app.llm.client import LLMClient
from app.memory.service import MemoryService
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.security.auth import get_current_user


def test_effective_memory_ownership_respects_precedence_and_user_tenant_boundaries() -> None:
    """验证规范列优先于 metadata，再回退到同用户同租户会话，拒绝越界历史会话。"""

    with _test_session() as db:
        _seed_scope(db)
        db.add_all(
            [
                MemoryRecord(
                    id="memory_canonical_wins",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id="user_demo",
                    session_id="session_agent_b",
                    kind="profile",
                    content="规范列归属 agent_a",
                    metadata_json={"agent_id": "agent_b"},
                ),
                MemoryRecord(
                    id="memory_metadata_wins",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_agent_b",
                    kind="preference",
                    content="metadata 归属 agent_a",
                    metadata_json={"agent_id": "agent_a"},
                ),
                MemoryRecord(
                    id="memory_blank_metadata_session",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_agent_a",
                    kind="fact",
                    content="空白 metadata 回退 agent_a",
                    metadata_json={"agent_id": "   "},
                ),
                MemoryRecord(
                    id="memory_invalid_metadata_session",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_agent_a",
                    kind="fact",
                    content="非法 metadata 回退 agent_a",
                    metadata_json={"agent_id": 42},
                ),
                MemoryRecord(
                    id="memory_cross_tenant_session",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_cross_tenant",
                    kind="fact",
                    content="跨租户会话不能归属 agent_a",
                    metadata_json={},
                ),
                MemoryRecord(
                    id="memory_cross_user_session",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_other_user",
                    kind="fact",
                    content="跨用户会话不能归属 agent_a",
                    metadata_json={},
                ),
            ]
        )
        db.commit()

        rows = MemoryService(db).context_memories(
            "tenant_demo",
            "user_demo",
            agent_id="agent_a",
        )

    assert {row.content for row in rows} == {
        "规范列归属 agent_a",
        "metadata 归属 agent_a",
        "空白 metadata 回退 agent_a",
        "非法 metadata 回退 agent_a",
    }
    assert all(row.content != "跨租户会话不能归属 agent_a" for row in rows)
    assert all(row.content != "跨用户会话不能归属 agent_a" for row in rows)


def test_keyed_memory_write_and_delete_use_the_same_effective_agent_scope() -> None:
    """验证历史会话记忆的按键更新和删除不会误用越界会话或其他员工记忆。"""

    with _test_session() as db:
        _seed_scope(db)
        old = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                MemoryRecord(
                    id="memory_key_same_tenant",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_agent_a",
                    kind="profile",
                    content="旧值",
                    metadata_json={"key": "preferred_name"},
                    updated_at=old,
                ),
                MemoryRecord(
                    id="memory_key_cross_tenant",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_cross_tenant",
                    kind="profile",
                    content="跨租户会话旧值",
                    metadata_json={"key": "preferred_name"},
                    updated_at=old + timedelta(minutes=1),
                ),
                MemoryRecord(
                    id="memory_key_other_agent",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    session_id="session_agent_b",
                    kind="profile",
                    content="agent_b 旧值",
                    metadata_json={"key": "preferred_name", "agent_id": "agent_b"},
                    updated_at=old + timedelta(minutes=2),
                ),
            ]
        )
        db.commit()

        service = MemoryService(db)
        updated = service._upsert_keyed_memory(
            tenant_id="tenant_demo",
            user_id="user_demo",
            username="demo",
            session_id="session_agent_a",
            kind="profile",
            key="preferred_name",
            content="新值",
            importance=0.9,
            metadata={"key": "preferred_name"},
            agent_id="agent_a",
        )
        db.commit()
        service._delete_keyed_memory(
            "tenant_demo",
            "user_demo",
            "profile",
            "preferred_name",
            agent_id="agent_a",
        )
        db.commit()
        remaining = list(db.exec(select(MemoryRecord)).all())

    assert updated.id == "memory_key_same_tenant"
    assert {row.id for row in remaining} == {
        "memory_key_cross_tenant",
        "memory_key_other_agent",
    }


def test_memory_api_list_page_and_self_clear_share_effective_ownership() -> None:
    """验证列表、用户组分页、外部归属展示和本人清空使用同一有效归属。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                MemoryRecord(
                    id="api_metadata_owner",
                    tenant_id="tenant_demo",
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_agent_b",
                    kind="profile",
                    content="API metadata 归属",
                    metadata_json={"agent_id": "agent_a"},
                    updated_at=base_time + timedelta(minutes=4),
                ),
                MemoryRecord(
                    id="api_session_owner",
                    tenant_id="tenant_demo",
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_agent_a",
                    kind="fact",
                    content="API session 归属",
                    metadata_json={"agent_id": ""},
                    updated_at=base_time + timedelta(minutes=3),
                ),
                MemoryRecord(
                    id="api_other_user",
                    tenant_id="tenant_demo",
                    user_id="user_other",
                    username="other",
                    session_id="session_other_user",
                    kind="fact",
                    content="API 另一个用户",
                    metadata_json={},
                    updated_at=base_time + timedelta(minutes=2),
                ),
                MemoryRecord(
                    id="api_cross_tenant",
                    tenant_id="tenant_demo",
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_cross_tenant",
                    kind="fact",
                    content="API 跨租户会话",
                    metadata_json={},
                    updated_at=base_time + timedelta(minutes=5),
                ),
                MemoryRecord(
                    id="api_other_agent",
                    tenant_id="tenant_demo",
                    user_id=owner.id,
                    username=owner.username,
                    session_id="session_agent_b",
                    kind="fact",
                    content="API 其他员工",
                    metadata_json={"agent_id": "agent_b"},
                    updated_at=base_time + timedelta(minutes=6),
                ),
            ]
        )
        db.commit()

        listed = list_memories(
            tenant_id="tenant_demo",
            agent_id="agent_a",
            user_id=None,
            username=None,
            q=None,
            limit=100,
            current_user=owner,
            db=db,
        )
        page = page_memories(
            tenant_id="tenant_demo",
            agent_id="agent_a",
            user_id=None,
            username=None,
            q=None,
            page=1,
            page_size=10,
            current_user=owner,
            db=db,
        )
        cleared = clear_my_memories("tenant_demo", "agent_a", owner, db)
        remaining = list(db.exec(select(MemoryRecord)).all())

    expected = {"API metadata 归属", "API session 归属", "API 另一个用户"}
    assert {row["content"] for row in listed} == expected
    assert {row["content"] for row in page.items} == expected
    assert all(row["metadata"].get("agent_id") == "agent_a" for row in listed)
    assert all(row["content"] != "API 跨租户会话" for row in listed)
    assert page.total == 2
    assert cleared == {"deleted": 2}
    assert {row.content for row in remaining} == {
        "API 另一个用户",
        "API 跨租户会话",
        "API 其他员工",
    }


def test_clear_route_rejects_explicit_blank_agent_scope_without_expanding_delete() -> None:
    """验证本人清空路由把显式空白员工参数视为无效范围，而不是全量范围。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        db.add_all(
            [
                MemoryRecord(
                    id="blank_clear_agent_a",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id=owner.id,
                    kind="fact",
                    content="空白参数不应删除 A",
                ),
                MemoryRecord(
                    id="blank_clear_agent_b",
                    tenant_id="tenant_demo",
                    agent_id="agent_b",
                    user_id=owner.id,
                    kind="fact",
                    content="空白参数不应删除 B",
                ),
            ]
        )
        db.commit()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: owner
        app.dependency_overrides[get_session] = lambda: db
        with TestClient(app) as client:
            response = client.delete(
                "/api/enterprise/memories/me",
                params={"tenant_id": "tenant_demo", "agent_id": "   "},
            )

        remaining = {
            row.id
            for row in db.exec(
                select(MemoryRecord).where(MemoryRecord.user_id == owner.id)
            ).all()
        }

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert remaining == {"blank_clear_agent_a", "blank_clear_agent_b"}


@pytest.mark.parametrize(
    ("query", "expected_deleted", "expected_remaining"),
    [
        ({}, 2, set()),
        ({"agent_id": "agent_a"}, 1, {"scope_clear_agent_b"}),
        ({"agent_id": ""}, 0, {"scope_clear_agent_a", "scope_clear_agent_b"}),
        ({"agent_id": "missing_agent"}, 0, {"scope_clear_agent_a", "scope_clear_agent_b"}),
    ],
)
def test_clear_route_distinguishes_missing_valid_and_invalid_agent_filters(
    query: dict[str, str],
    expected_deleted: int,
    expected_remaining: set[str],
) -> None:
    """验证本人清空的缺省、有效和显式无效员工筛选不会混淆。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        db.add_all(
            [
                MemoryRecord(
                    id="scope_clear_agent_a",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id=owner.id,
                    kind="fact",
                    content="范围测试 A",
                ),
                MemoryRecord(
                    id="scope_clear_agent_b",
                    tenant_id="tenant_demo",
                    agent_id="agent_b",
                    user_id=owner.id,
                    kind="fact",
                    content="范围测试 B",
                ),
            ]
        )
        db.commit()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: owner
        app.dependency_overrides[get_session] = lambda: db
        with TestClient(app) as client:
            response = client.delete(
                "/api/enterprise/memories/me",
                params={"tenant_id": "tenant_demo", **query},
            )

        remaining = {
            row.id
            for row in db.exec(
                select(MemoryRecord).where(MemoryRecord.user_id == owner.id)
            ).all()
        }

    assert response.status_code == 200
    assert response.json() == {"deleted": expected_deleted}
    assert remaining == expected_remaining


def test_capture_turn_filters_large_other_agent_history_before_limit(monkeypatch) -> None:
    """验证大量其他员工的新记忆不会挤掉当前员工的历史模型输入。"""

    captured: dict[str, object] = {}

    def fake_init(self, model_config) -> None:  # noqa: ANN001
        """替换模型客户端初始化，避免该测试产生真实模型请求。"""

        del model_config

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        """捕获记忆抽取请求，返回空结果以检查输入筛选边界。"""

        del self, system_prompt
        captured.update(payload)
        return {"memories": [], "updated_summary": ""}

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    with _test_session() as db:
        _seed_scope(db)
        db.add(
            ChatSession(
                id="session_capture",
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_a",
            )
        )
        target_time = datetime(2026, 8, 1, 10, 0)
        db.add(
            MemoryRecord(
                id="capture_target",
                tenant_id="tenant_demo",
                agent_id="agent_a",
                user_id="user_demo",
                kind="preference",
                content="当前员工目标记忆",
                metadata_json={"key": "target"},
                updated_at=target_time,
            )
        )
        db.add_all(
            [
                MemoryRecord(
                    id=f"capture_other_{index:03d}",
                    tenant_id="tenant_demo",
                    agent_id="agent_b",
                    user_id="user_demo",
                    kind="fact",
                    content=f"其他员工新记忆 {index}",
                    updated_at=target_time + timedelta(minutes=index + 1),
                )
                for index in range(151)
            ]
        )
        db.commit()

        MemoryService(db).capture_turn(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_a",
                message="继续测试记忆",
            ),
            db.get(ChatSession, "session_capture"),
            StepAgentResult(),
            None,
            ModelConfig(
                tenant_id="tenant_demo",
                name="demo",
                api_key_encrypted="",
                model="demo",
            ),
            [{"role": "user", "content": "继续测试记忆"}],
        )

    assert captured["existing_memories"] == "- preference/target: 当前员工目标记忆"


def test_memory_search_dedupes_latest_value_before_legacy_and_page_filters() -> None:
    """验证同键新值先覆盖旧值，再按两个接口各自的搜索契约过滤。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        base_time = datetime(2026, 8, 1, 10, 0)
        db.add_all(
            [
                MemoryRecord(
                    id="search_old_value",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="obsoletevalue",
                    metadata_json={"key": "preferred_name"},
                    updated_at=base_time,
                ),
                MemoryRecord(
                    id="search_new_value",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="currentvalue",
                    metadata_json={"key": "preferred_name"},
                    updated_at=base_time + timedelta(minutes=1),
                ),
                MemoryRecord(
                    id="search_plain_value",
                    tenant_id="tenant_demo",
                    agent_id="agent_a",
                    user_id=owner.id,
                    username=owner.username,
                    kind="fact",
                    content="plainalpha",
                    metadata_json={"key": "plain"},
                    updated_at=base_time - timedelta(minutes=1),
                ),
            ]
        )
        db.commit()

        legacy_old = list_memories(
            "tenant_demo", "agent_a", None, None, "obsoletevalue", 100, owner, db
        )
        legacy_new = list_memories(
            "tenant_demo", "agent_a", None, None, "currentvalue", 100, owner, db
        )
        legacy_user_id = list_memories(
            "tenant_demo", "agent_a", None, None, owner.id, 100, owner, db
        )
        page_old = page_memories(
            "tenant_demo", "agent_a", None, None, "obsoletevalue", 1, 10, owner, db
        )
        page_new = page_memories(
            "tenant_demo", "agent_a", None, None, "currentvalue", 1, 10, owner, db
        )
        page_percent = page_memories(
            "tenant_demo", "agent_a", None, None, "%", 1, 10, owner, db
        )
        page_underscore = page_memories(
            "tenant_demo", "agent_a", None, None, "_", 1, 10, owner, db
        )

    assert legacy_old == []
    assert [row["content"] for row in legacy_new] == ["currentvalue"]
    assert legacy_user_id == []
    assert page_old.total == 0
    assert page_old.items == []
    assert page_new.total == 1
    assert [row["content"] for row in page_new.items] == ["currentvalue"]
    assert page_percent.total == 1
    assert page_underscore.total == 1


@pytest.mark.parametrize(
    "needle",
    ["%%%%%%%%b", "%a%a%a%a%a%a%a%a%b"],
)
def test_page_search_wildcard_input_returns_without_regex_backtracking(needle: str) -> None:
    """验证分页搜索的通配符输入不会因正则回溯而超时。"""

    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(
        target=_run_page_query_in_child,
        args=(result_queue, needle),
    )
    process.start()
    process.join(timeout=5.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)

    assert not process.is_alive(), f"分页搜索在输入 {needle!r} 下超时"
    assert process.exitcode == 0
    assert result_queue.get(timeout=1.0) == 0


def _run_page_query_in_child(result_queue, needle: str) -> None:  # noqa: ANN001
    """在隔离子进程中执行单条分页匹配，避免回溯红测挂住测试进程。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        db.add(
            MemoryRecord(
                id="regex_timeout_probe",
                tenant_id="tenant_demo",
                agent_id="agent_a",
                user_id=owner.id,
                username=owner.username,
                kind="fact",
                content="a" * 80,
            )
        )
        db.commit()
        result = page_memories(
            "tenant_demo",
            "agent_a",
            None,
            None,
            needle,
            1,
            10,
            owner,
            db,
        )
    result_queue.put(result.total)


def test_page_search_keeps_sqlite_native_casefold_semantics() -> None:
    """验证分页搜索保留 SQLite 原生 ILIKE 对非 ASCII 大小写的比较结果。"""

    with _test_session() as db:
        owner = _seed_scope(db)
        db.add(
            MemoryRecord(
                id="sqlite_native_casefold",
                tenant_id="tenant_demo",
                agent_id="agent_a",
                user_id=owner.id,
                username=owner.username,
                kind="fact",
                content="Ä",
                metadata_json={"key": "sqlite_casefold"},
            )
        )
        db.commit()

        page = page_memories(
            "tenant_demo",
            "agent_a",
            None,
            None,
            "ä",
            1,
            10,
            owner,
            db,
        )

    assert page.total == 0
    assert page.items == []


def test_agent_loop_memory_payload_projects_session_ownership_without_read_backfill() -> None:
    """验证 AgentLoop 给 Router/DTA 的历史记忆 payload 显示有效会话归属且不回写。"""

    with _test_session() as db:
        _seed_scope(db)
        legacy = MemoryRecord(
            id="memory_agent_loop_legacy",
            tenant_id="tenant_demo",
            user_id="user_demo",
            session_id="session_agent_a",
            kind="preference",
            content="AgentLoop 读取的历史偏好",
            metadata_json={"agent_id": "   ", "key": "communication_style"},
        )
        db.add(legacy)
        db.commit()

        payloads = MemoryService(db).context_memory_payloads(
            "tenant_demo",
            "user_demo",
            agent_id="agent_a",
        )

        db.refresh(legacy)
        persisted_metadata = dict(legacy.metadata_json)

    assert len(payloads) == 1
    assert payloads[0]["metadata"] == {
        "agent_id": "agent_a",
        "agent_id_source": "session",
        "key": "communication_style",
    }
    assert persisted_metadata == {"agent_id": "   ", "key": "communication_style"}


def _seed_scope(db: Session) -> User:
    """创建目标租户、两个员工、两个用户以及跨边界会话。"""

    owner = User(
        id="user_demo",
        tenant_id="tenant_demo",
        username="demo",
        password_hash="hash",
    )
    db.add_all(
        [
            Tenant(id="tenant_demo", name="Demo"),
            Tenant(id="tenant_other", name="Other"),
            owner,
            User(
                id="user_other",
                tenant_id="tenant_demo",
                username="other",
                password_hash="hash",
            ),
            AgentProfile(
                id="agent_a",
                tenant_id="tenant_demo",
                name="员工 A",
                status="active",
                owner_user_id=owner.id,
            ),
            AgentProfile(
                id="agent_b",
                tenant_id="tenant_demo",
                name="员工 B",
                status="active",
                owner_user_id=owner.id,
            ),
            AgentProfile(id="agent_a_other", tenant_id="tenant_other", name="员工 A2", status="active"),
            ChatSession(
                id="session_agent_a",
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_a",
            ),
            ChatSession(
                id="session_agent_b",
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_b",
            ),
            ChatSession(
                id="session_other_user",
                tenant_id="tenant_demo",
                user_id="user_other",
                agent_id="agent_a",
            ),
            ChatSession(
                id="session_cross_tenant",
                tenant_id="tenant_other",
                user_id="user_other_tenant",
                agent_id="agent_a",
            ),
        ]
    )
    db.commit()
    return owner


def _test_session() -> Session:
    """创建完整 SQLModel 元数据的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
