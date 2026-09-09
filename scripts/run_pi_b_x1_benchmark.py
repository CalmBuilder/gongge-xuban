"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : run_pi_b_x1_benchmark.py
@CallChain  : PI-2B验收命令 → 临时SQLite → replay/partial与旧宽查询 → JSON证据
@Description: 对 B-X1 的历史规模、ORM物化量、SQL候选计划、耗时和峰值内存做可重复对照。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from math import ceil
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
from typing import Any

# 让仓库根目录运行此脚本时始终使用当前工作区的后端源码。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import Engine, event, func  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.api.chat import _existing_turn_replay  # noqa: E402
from app.core.agent_loop import AgentLoop  # noqa: E402
from app.db.models import AgentEvent, Message  # noqa: E402
from app.db.sqlite_legacy import configure_sqlite_runtime  # noqa: E402
from app.observability.turn_event_query import (  # noqa: E402
    TURN_EVENT_SPAN_TYPES,
    partial_turn_events,
    turn_identity_filter,
)
from app.session.session_schema import ChatTurnRequest  # noqa: E402


TENANT_ID = "tenant_pi_b_x1"
TARGET_K = 5
HISTORY_COUNTS = (0, 100, 1_000, 10_000, 100_000)


def _percentile(values: list[float], percentile: float) -> float:
    """按最近邻上取整计算小样本 percentile，避免插值掩盖单次慢查询。"""

    if not values:
        raise ValueError("percentile requires at least one sample")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _seed_history(engine: Engine, history_count: int) -> tuple[str, str, str]:
    """在独立临时库写入固定目标 Turn 与指定数量无关事件。"""

    session_id = f"session_b_x1_{history_count}"
    message_id = f"message_b_x1_{history_count}"
    client_turn_id = f"client_b_x1_{history_count}"
    started_at = datetime(2026, 9, 6, 9, 30, 0)
    target_events = [
        AgentEvent(
            id=f"{session_id}_received",
            tenant_id=TENANT_ID,
            session_id=session_id,
            event_type="user_message_received",
            payload_json={"message_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at,
        )
    ]
    target_events.extend(
        AgentEvent(
            id=f"{session_id}_delta_{index:03d}",
            tenant_id=TENANT_ID,
            session_id=session_id,
            event_type="stream_delta",
            payload_json={
                "turn_id": message_id,
                "client_turn_id": client_turn_id,
                "content": f"目标-{index}",
            },
            created_at=started_at + timedelta(microseconds=index + 1),
        )
        for index in range(TARGET_K)
    )
    target_events.append(
        AgentEvent(
            id=f"{session_id}_complete",
            tenant_id=TENANT_ID,
            session_id=session_id,
            event_type="complete",
            payload_json={"turn_id": message_id, "client_turn_id": client_turn_id},
            created_at=started_at + timedelta(microseconds=TARGET_K + 1),
        )
    )
    unrelated_events = [
        AgentEvent(
            id=f"{session_id}_unrelated_{index:06d}",
            tenant_id=TENANT_ID,
            session_id=session_id,
            event_type="stream_delta",
            payload_json={"turn_id": f"unrelated_{index}", "content": "历史"},
            created_at=started_at + timedelta(microseconds=TARGET_K + index + 2),
        )
        for index in range(history_count)
    ]
    with Session(engine) as db:
        db.add(
            Message(
                id=message_id,
                tenant_id=TENANT_ID,
                session_id=session_id,
                role="user",
                content="B-X1 benchmark",
                metadata_json={"client_turn_id": client_turn_id},
            )
        )
        db.add_all([*target_events, *unrelated_events])
        db.commit()
    return session_id, message_id, client_turn_id


def _request(session_id: str, message_id: str, client_turn_id: str) -> ChatTurnRequest:
    """构造 benchmark 使用的固定 replay 请求。"""

    return ChatTurnRequest(
        tenant_id=TENANT_ID,
        session_id=session_id,
        user_id="user_pi_b_x1",
        message="B-X1 benchmark",
        client_turn_id=client_turn_id,
    )


@contextmanager
def _load_counter() -> Iterable[list[str]]:
    """在一次测量期间统计 AgentEvent ORM 实例化，不以 SQL 文本推测行数。"""

    loaded: list[str] = []

    def on_load(row: AgentEvent, _context: object) -> None:
        """记录 ORM 实际新建的 AgentEvent 实例。"""

        loaded.append(row.id)

    event.listen(AgentEvent, "load", on_load)
    try:
        yield loaded
    finally:
        event.remove(AgentEvent, "load", on_load)


def _measure(engine: Engine, operation: str, ids: tuple[str, str, str]) -> dict[str, Any]:
    """执行一次新查询或旧宽查询，返回时间、物化数、SQL次数和峰值内存。"""

    session_id, message_id, client_turn_id = ids
    sql_count = 0

    def before_execute(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """统计本次测量发出的 AgentEvent SQL 语句数。"""

        nonlocal sql_count
        if "agent_events" in statement.lower():
            sql_count += 1

    event.listen(engine, "before_cursor_execute", before_execute)
    tracemalloc.start()
    try:
        with _load_counter() as loaded:
            started = time.perf_counter()
            with Session(engine) as db:
                if operation == "replay":
                    result = _existing_turn_replay(
                        db,
                        _request(session_id, message_id, client_turn_id),
                    )
                    returned = len(result[1]) if result is not None else 0
                elif operation == "partial":
                    loop = object.__new__(AgentLoop)
                    loop.db = db
                    returned = len(partial_turn_events(db, TENANT_ID, session_id, message_id))
                elif operation == "legacy":
                    returned = len(
                        db.exec(
                            select(AgentEvent)
                            .where(
                                AgentEvent.tenant_id == TENANT_ID,
                                AgentEvent.session_id == session_id,
                            )
                            .order_by(AgentEvent.created_at, AgentEvent.id)
                        ).all()
                    )
                else:
                    raise ValueError(f"unsupported operation: {operation}")
            elapsed_ms = (time.perf_counter() - started) * 1000
            _current, peak_bytes = tracemalloc.get_traced_memory()
            return {
                "operation": operation,
                "elapsed_ms": round(elapsed_ms, 3),
                "loaded_agent_events": len(loaded),
                "returned_rows": returned,
                "agent_event_sql_count": sql_count,
                "peak_tracemalloc_kib": round(peak_bytes / 1024, 2),
            }
    finally:
        tracemalloc.stop()
        event.remove(engine, "before_cursor_execute", before_execute)


def _explain_target_query(engine: Engine, ids: tuple[str, str, str]) -> list[str]:
    """输出 SQLite 目标候选查询的实际 EXPLAIN QUERY PLAN 文本。"""

    session_id, message_id, client_turn_id = ids
    with Session(engine) as db:
        statement = (
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == TENANT_ID,
                AgentEvent.session_id == session_id,
                AgentEvent.event_type.notin_(TURN_EVENT_SPAN_TYPES),
                turn_identity_filter(db, (message_id, client_turn_id)),
            )
            .order_by(AgentEvent.created_at, AgentEvent.id)
            .limit(200)
        )
        compiled = statement.compile(engine, compile_kwargs={"literal_binds": True})
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {compiled}").all()
    return [" | ".join(str(value) for value in row) for row in rows]


def _candidate_rows_before_identity(engine: Engine, ids: tuple[str, str, str]) -> int:
    """统计进入 JSON 身份过滤前的租户/会话候选行，作为物理扫描上界记录。"""

    session_id, _message_id, _client_turn_id = ids
    with Session(engine) as db:
        return int(
            db.exec(
                select(func.count())
                .select_from(AgentEvent)
                .where(
                    AgentEvent.tenant_id == TENANT_ID,
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type.notin_(TURN_EVENT_SPAN_TYPES),
                )
            ).one()
        )


def _benchmark_one(
    root: Path,
    history_count: int,
    warmups: int,
    samples: int,
) -> dict[str, Any]:
    """为一个历史规模建立独立库并完成预热、采样和查询计划记录。"""

    database_path = root / f"pi-b-x1-{history_count}.db"
    engine = create_engine(f"sqlite:///{database_path}")
    configure_sqlite_runtime(engine)
    SQLModel.metadata.create_all(engine)
    try:
        ids = _seed_history(engine, history_count)
        operations = ("replay", "partial", "legacy")
        for operation in operations:
            for _ in range(warmups):
                _measure(engine, operation, ids)
        measurements: dict[str, list[dict[str, Any]]] = {
            operation: [_measure(engine, operation, ids) for _ in range(samples)]
            for operation in operations
        }
        summary: dict[str, Any] = {}
        for operation, rows in measurements.items():
            summary[operation] = {
                "median_ms": round(statistics.median(row["elapsed_ms"] for row in rows), 3),
                "p95_ms": round(_percentile([row["elapsed_ms"] for row in rows], 0.95), 3),
                "loaded_agent_events": sorted({row["loaded_agent_events"] for row in rows}),
                "returned_rows": sorted({row["returned_rows"] for row in rows}),
                "agent_event_sql_count": sorted({row["agent_event_sql_count"] for row in rows}),
                "peak_tracemalloc_kib_max": round(
                    max(row["peak_tracemalloc_kib"] for row in rows),
                    2,
                ),
            }
        assert summary["replay"]["returned_rows"] == [TARGET_K + 2]
        assert summary["partial"]["returned_rows"] == [TARGET_K]
        assert summary["replay"]["loaded_agent_events"] == [TARGET_K + 2]
        assert summary["partial"]["loaded_agent_events"] == [TARGET_K]
        assert summary["legacy"]["returned_rows"] == [history_count + TARGET_K + 2]
        assert summary["legacy"]["loaded_agent_events"] == [history_count + TARGET_K + 2]
        return {
            "history_count": history_count,
            "target_chunk_count": TARGET_K,
            "warmups": warmups,
            "samples": samples,
            "candidate_rows_before_identity": _candidate_rows_before_identity(engine, ids),
            "explain_query_plan": _explain_target_query(engine, ids),
            "summary": summary,
        }
    finally:
        engine.dispose()


def _parse_args() -> argparse.Namespace:
    """解析 benchmark 的规模、预热和采样参数。"""

    parser = argparse.ArgumentParser(description="运行 PI-2B B-X1 SQLite 查询成本基准")
    parser.add_argument(
        "--history-count",
        type=int,
        action="append",
        dest="history_counts",
        help="只测指定历史量；可重复传入，默认使用冻结五档梯度",
    )
    parser.add_argument("--warmups", type=int, default=3, help="每个操作预热次数")
    parser.add_argument("--samples", type=int, default=10, help="每个操作采样次数")
    return parser.parse_args()


def main() -> int:
    """运行隔离 benchmark 并输出不含凭据和业务数据的 JSON 结果。"""

    args = _parse_args()
    if args.warmups < 0 or args.samples <= 0:
        raise SystemExit("--warmups 不能为负数，--samples 必须大于 0")
    history_counts = tuple(args.history_counts or HISTORY_COUNTS)
    if any(value < 0 for value in history_counts):
        raise SystemExit("--history-count 不能为负数")
    with tempfile.TemporaryDirectory(prefix="gongge-pi-b-x1-") as temporary_root:
        results = [
            _benchmark_one(Path(temporary_root), value, args.warmups, args.samples)
            for value in history_counts
        ]
    print(json.dumps({"benchmark": "PI-2B-B-X1", "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
