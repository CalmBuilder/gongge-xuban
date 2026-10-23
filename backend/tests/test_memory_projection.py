"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_memory_projection.py
@CallChain  : pytest → 内部记忆候选/投影 → 真实序列化预算
@Description: 验证完整条目、确定性来源、预算包装与公共全文读取隔离。
"""

import json
import os
import random
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AgentProfile, MemoryRecord, User
from app.llm.client import _request_tokens
from app.memory.projection import MemoryCandidate, MemoryProjectionPolicy, select_memory_projection
from app.memory.service import MemoryService


def candidate(identity: str, content: str, **kwargs) -> MemoryCandidate:
    """构造只供本地确定性测试使用的可信候选，不模拟公共管理 payload。"""
    return MemoryCandidate(
        id=identity, kind="fact", key=identity, content=content,
        updated_at="2026-10-01T00:00:00+00:00", **kwargs,
    )


def cost(items: tuple[MemoryCandidate, ...]) -> int:
    """使用实际 JSON 双重包装和项目总请求估算，计入中文及转义费用。"""
    values = [item.provider_payload() for item in items]
    return _request_tokens([{"role": "user", "content": json.dumps(
        {"memory_context": values, "execution_context": {"memory_context": values}},
        ensure_ascii=False,
    )}])


def test_whole_facts_budget_skips_large_and_counts_real_wrapper() -> None:
    """超大身份整条省略后继续选小事实，双份 JSON 费用可独立复算。"""
    huge = MemoryCandidate(id="huge", key="name", kind="profile", content="中😀" * 6000)
    small = candidate("small", '名称：青禾\n\t保留  两空格 "\\" 😀')
    result = select_memory_projection((huge, small), current_input="名称", input_limit=4096,
                                      render_cost=cost)
    assert result.selected == (small,)
    assert result.budget == 512
    assert result.estimated_cost == cost((small,)) - cost(())
    assert result.omissions["item_over_budget"] == 1
    assert result.omissions["required_fact_omitted"] == 1
    assert result.selected[0].content == small.content
    assert result.policy_version == "memory_projection_v1"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 2**64, "2048"])
def test_policy_rejects_invalid_limits(value: object) -> None:
    """不允许布尔、负数、非有限值、溢出或客户端字符串提高预算。"""
    with pytest.raises(ValueError):
        MemoryProjectionPolicy(configured_memory_limit=value)


def test_zero_and_nonmemory_pressure_do_not_disclose_facts() -> None:
    """显式零预算及非记忆费用占满输入时返回空选择而保留省略信息。"""
    items = (candidate("one", "完整事实"),)
    for policy, renderer in ((MemoryProjectionPolicy(configured_memory_limit=0), cost),
                             (MemoryProjectionPolicy(), lambda _: 1000)):
        result = select_memory_projection(items, current_input="", input_limit=1000,
                                          render_cost=renderer, policy=policy)
        assert result.selected == ()
        assert result.budget == 0


def test_shuffle_latest_source_correction_and_stable_utc(monkeypatch) -> None:
    """业务来源序胜过晚更新时间，洗牌不变，可信键纠正排除旧值。"""
    old = candidate("old", "旧事实", source_sequence=1)
    old = MemoryCandidate(**{**old.snapshot(), "key": "same", "updated_at": "2099-01-01"})
    new = MemoryCandidate(id="new", kind="fact", key="same", content="新事实",
                          source_sequence=2, updated_at="2020-01-01")
    items = [old, new, candidate("other", "无关事实")]
    baseline = None
    original_tz = os.environ.get("TZ")
    try:
        for zone in ("UTC", "Asia/Shanghai", "America/New_York"):
            monkeypatch.setenv("TZ", zone)
            if hasattr(time, "tzset"):
                time.tzset()
            for seed in range(12):
                random.Random(seed).shuffle(items)
                result = select_memory_projection(tuple(items), current_input="新事实", input_limit=8192,
                                                  render_cost=cost)
                assert "old" not in [item.id for item in result.selected]
                baseline = baseline or result.audit()
                assert result.audit() == baseline
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ")
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()
    corrected = select_memory_projection(tuple(items), current_input="新事实", input_limit=8192,
                                         render_cost=cost, corrected_keys=("same",))
    assert [item.id for item in corrected.selected] == ["other"]


def test_dta_twenty_four_thousand_and_remaining_echo_order() -> None:
    """DTA最多二十条，四千字符完整保留，超长整条省略，回声按剩余额输出。"""
    items = tuple(candidate(f"fact-{i:02}", "短事实") for i in range(18))
    boundary = candidate("boundary", "a" * 4000)
    oversized = candidate("oversized", "b" * 4001)
    echoes = tuple(MemoryCandidate(id=f"echo-{i}", key=f"echo-{i}",
                                  kind="conversation_turn", content=f"user: {i}",
                                  source_sequence=i, origin="conversation") for i in range(4))
    policy = MemoryProjectionPolicy(configured_memory_limit=100000, max_items=20,
                                    max_fact_characters=4000)
    result = select_memory_projection((*items, boundary, oversized, *echoes), current_input="",
                                      input_limit=200000, render_cost=cost, policy=policy)
    assert len(result.selected) == 20
    assert boundary in result.selected
    assert oversized not in result.selected
    assert result.selected[-1].id == "echo-3"
    remaining = select_memory_projection((*items, *echoes), current_input="", input_limit=200000,
                                         render_cost=cost, policy=policy)
    assert [item.id for item in remaining.selected[-2:]] == ["echo-2", "echo-3"]


def test_internal_candidates_do_not_change_complete_public_reads() -> None:
    """候选独立按可信来源去重；管理和领域读取仍保留全部全文与原合同。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="u", tenant_id="t", username="user", password_hash="synthetic"))
        db.add(AgentProfile(id="a", tenant_id="t", name="员工", owner_user_id="u"))
        for identity, sequence, year in (("older", 1, 2026), ("newer", 2, 2025)):
            db.add(MemoryRecord(id=identity, tenant_id="t", user_id="u", agent_id="a",
                                kind="profile", content=identity + "全" * 5000,
                                source_sequence=sequence, metadata_json={"key": "name"},
                                updated_at=datetime(year, 1, 1, tzinfo=UTC)))
        db.commit()
        service = MemoryService(db)
        before = service.context_memory_payloads("t", "u", agent_id="a")
        candidates = service.projection_candidates("t", "u", agent_id="a")
        assert len(candidates) == 1 and candidates[0].id == "newer"
        assert len(candidates[0].content) > 5000
        assert candidates[0].scope == ("t", "u", "a")
        assert service.context_memory_payloads("t", "u", agent_id="a") == before
        assert service.projection_candidates("other-tenant", "u", agent_id="a") == ()
    engine.dispose()
