"""
@Time       : 2026/09/06
@Author     : zhanglp8181
@File       : turn_event_query.py
@CallChain  : chat replay/AgentLoop partial → turn event SQL filter → AgentEvent
@Description: 提供按 Turn 收窄事件读取所需的无状态 SQL 条件、游标和身份转换。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, false, func, literal, or_, true
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select

from app.db.models import AgentEvent


TURN_EVENT_ID_KEYS: tuple[str, ...] = (
    "turn_id",
    "user_message_id",
    "message_id",
    "client_turn_id",
)
TURN_EVENT_PAGE_SIZE = 200
TURN_EVENT_SPAN_TYPES = frozenset(
    {
        "llm_call_started",
        "llm_call_finished",
        "llm_call_failed",
        "knowledge_span_started",
        "knowledge_span_finished",
        "knowledge_span_failed",
    }
)
# Python str.strip() 的 Unicode White_Space 集合，并补上 Python 对历史控制分隔符的
# 兼容行为。SQLite trim(X,Y) 将 Y 解释为待剥离字符集合；MySQL 使用同一集合构造 ICU
# 正则，避免默认 ai_ci 排序规则把不同大小写/重音的 Turn 串在一起。U+001C..U+001F
# 是 Python str.strip() 额外剥离的历史控制分隔符。
PYTHON_STRIP_CHARS = (
    " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
MYSQL_STRIP_PATTERN = r"^[\p{White_Space}\x{001c}-\x{001f}]+|[\p{White_Space}\x{001c}-\x{001f}]+$"
EventCursor = tuple[datetime, str]


def normalize_turn_identity(value: Any) -> str:
    """只把 JSON 字符串身份按 Python strip 规则归一，拒绝隐式字符串化脏值。"""

    return value.strip() if isinstance(value, str) else ""


def event_turn_ids(row: AgentEvent) -> set[str]:
    """提取事件四种兼容身份键，并保持空值、空白和字段优先级语义。"""

    payload = row.payload_json or {}
    return {
        normalized
        for normalized in (
            normalize_turn_identity(payload.get(key))
            for key in TURN_EVENT_ID_KEYS
        )
        if normalized
    }


def event_has_turn_identity(row: AgentEvent, turn_ids: Iterable[str]) -> bool:
    """判断已加载事件是否携带目标 Turn 身份，用于 SQL 候选的兼容性复核。"""

    expected = {
        normalized
        for normalized in (normalize_turn_identity(value) for value in turn_ids)
        if normalized
    }
    return bool(event_turn_ids(row) & expected)


def primary_turn_identity(row: AgentEvent) -> str:
    """按 partial 的历史优先级提取 turn_id，否则使用 user_message_id。"""

    payload = row.payload_json or {}
    return normalize_turn_identity(payload.get("turn_id") or payload.get("user_message_id"))


def _turn_identity_expression(db: Session, key: str) -> ColumnElement[Any]:
    """构造当前数据库方言的 JSON 身份提取和空白归一表达式。"""

    expression: ColumnElement[Any] = AgentEvent.payload_json[key].as_string()
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        expression = func.trim(expression, literal(PYTHON_STRIP_CHARS))
        return expression.collate("BINARY")
    if dialect_name == "mysql":
        expression = func.regexp_replace(expression, literal(MYSQL_STRIP_PATTERN), literal(""))
        return expression.collate("utf8mb4_bin")
    return expression


def _json_string_type_filter(db: Session, key: str) -> ColumnElement[bool]:
    """只允许 JSON 字符串进入身份比较，阻断方言对数字/布尔值的隐式转字符串。"""

    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        return func.json_type(
            AgentEvent.payload_json,
            literal(f"$.{key}"),
        ) == "text"
    if dialect_name == "mysql":
        return func.json_type(AgentEvent.payload_json[key]) == "STRING"
    return true()


def turn_identity_filter(
    db: Session,
    turn_ids: Iterable[str],
    *,
    keys: Sequence[str] = TURN_EVENT_ID_KEYS,
) -> ColumnElement[bool]:
    """返回匹配任一兼容身份键的 SQL 条件，不创建 Session 或执行查询。"""

    values = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_turn_identity(value) for value in turn_ids)
            if normalized
        )
    )
    if not values or not keys:
        return false()
    return or_(
        *(
            and_(
                _json_string_type_filter(db, key),
                _turn_identity_expression(db, key).in_(values),
            )
            for key in keys
        )
    )


def _cursor_at_or_before(cursor: EventCursor) -> ColumnElement[bool]:
    """构造包含高水位边界的稳定排序键条件。"""

    created_at, event_id = cursor
    return or_(
        AgentEvent.created_at < created_at,
        (AgentEvent.created_at == created_at) & (AgentEvent.id <= event_id),
    )


def _cursor_after(cursor: EventCursor) -> ColumnElement[bool]:
    """构造严格位于上一页末尾之后的 keyset 条件。"""

    created_at, event_id = cursor
    return or_(
        AgentEvent.created_at > created_at,
        (AgentEvent.created_at == created_at) & (AgentEvent.id > event_id),
    )


def session_high_watermark(
    db: Session,
    tenant_id: str,
    session_id: str,
) -> EventCursor | None:
    """只读取会话尾部排序键，固定本次 replay 可见的事件上界 H。"""

    row = db.exec(
        select(AgentEvent.created_at, AgentEvent.id)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[0], str(row[1])


def latest_received_event(
    db: Session,
    tenant_id: str,
    session_id: str,
    client_turn_id: str,
    high_watermark: EventCursor,
) -> AgentEvent | None:
    """在 H 内按 client_turn_id 查找最后一条接收事件，不扫描无关事件正文。"""

    normalized_client_turn_id = normalize_turn_identity(client_turn_id)
    if not normalized_client_turn_id:
        return None
    row = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type == "user_message_received",
            _cursor_at_or_before(high_watermark),
            turn_identity_filter(
                db,
                (normalized_client_turn_id,),
                keys=("client_turn_id",),
            ),
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    # SQL JSON 表达式是候选过滤；Python 复核保留字符串身份和 Unicode strip 的最终
    # 判定。非字符串 JSON 身份属于无效事件，不通过隐式字符串化进入 Turn。
    payload = row.payload_json or {}
    if normalize_turn_identity(payload.get("client_turn_id")) != normalized_client_turn_id:
        return None
    return row


def turn_event_page(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_ids: Iterable[str],
    high_watermark: EventCursor,
    *,
    after: EventCursor | None = None,
    page_size: int = TURN_EVENT_PAGE_SIZE,
) -> list[AgentEvent]:
    """按固定 H 和 keyset 游标读取一页非 span 目标事件。"""

    normalized_turn_ids = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_turn_identity(value) for value in turn_ids)
            if normalized
        )
    )
    candidate_page = _turn_event_candidate_page(
        db,
        tenant_id,
        session_id,
        normalized_turn_ids,
        high_watermark,
        after=after,
        page_size=page_size,
    )
    return [row for row in candidate_page if event_has_turn_identity(row, normalized_turn_ids)]


def turn_events_after_cursor(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_ids: Iterable[str],
    after: EventCursor | None,
    *,
    page_size: int = TURN_EVENT_PAGE_SIZE,
    include_same_timestamp: bool = False,
    overlap_since: datetime | None = None,
    exclude_event_ids: Iterable[str] = (),
) -> list[AgentEvent]:
    """按目标 Turn 查询游标之后的一页事件，不为实时重连物化无关会话历史。

    实时 relay 可请求包含游标时间戳的未见事件，以覆盖数据库时间精度较低或事件
    在独立事务中交错提交时的同刻低 ID 迟到事件；也可以传入当前 Turn 已知首事件
    时间作为有限重叠下界，覆盖事件先生成、后提交且时间戳早于当前游标的迟到事实。
    调用方必须传入已发出的 ID 做去重。
    固定 H 的 replay 保持默认严格 keyset 语义，不使用该重叠读取模式。
    """

    normalized_turn_ids = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_turn_identity(value) for value in turn_ids)
            if normalized
        )
    )
    candidate_page = _turn_event_candidate_page(
        db,
        tenant_id,
        session_id,
        normalized_turn_ids,
        None,
        after=after,
        page_size=page_size,
        include_same_timestamp=include_same_timestamp,
        overlap_since=overlap_since,
        exclude_event_ids=exclude_event_ids,
    )
    return [row for row in candidate_page if event_has_turn_identity(row, normalized_turn_ids)]


def _turn_event_candidate_page(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_ids: Iterable[str],
    high_watermark: EventCursor | None,
    *,
    after: EventCursor | None,
    page_size: int,
    include_same_timestamp: bool = False,
    overlap_since: datetime | None = None,
    exclude_event_ids: Iterable[str] = (),
) -> list[AgentEvent]:
    """读取一页 SQL 候选；内部保留候选末尾供完整分页推进。"""

    if page_size <= 0:
        raise ValueError("page_size must be positive")
    normalized_turn_ids = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_turn_identity(value) for value in turn_ids)
            if normalized
        )
    )
    statement = select(AgentEvent).where(
        AgentEvent.tenant_id == tenant_id,
        AgentEvent.session_id == session_id,
        AgentEvent.event_type.notin_(TURN_EVENT_SPAN_TYPES),
        turn_identity_filter(db, normalized_turn_ids),
    )
    if high_watermark is not None:
        statement = statement.where(_cursor_at_or_before(high_watermark))
    if after is not None:
        if include_same_timestamp:
            statement = statement.where(
                AgentEvent.created_at >= (overlap_since or after[0])
            )
        else:
            statement = statement.where(_cursor_after(after))
    elif include_same_timestamp and overlap_since is not None:
        statement = statement.where(AgentEvent.created_at >= overlap_since)
    excluded_ids = tuple(dict.fromkeys(str(event_id) for event_id in exclude_event_ids if event_id))
    if excluded_ids:
        statement = statement.where(AgentEvent.id.notin_(excluded_ids))
    return list(
        db.exec(
            statement.order_by(AgentEvent.created_at, AgentEvent.id).limit(page_size)
        ).all()
    )


def all_turn_events(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_ids: Iterable[str],
    high_watermark: EventCursor,
    *,
    page_size: int = TURN_EVENT_PAGE_SIZE,
) -> list[AgentEvent]:
    """循环读取完整目标 Turn，直到 keyset 分页没有更多候选。"""

    rows: list[AgentEvent] = []
    after: EventCursor | None = None
    normalized_turn_ids = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_turn_identity(value) for value in turn_ids)
            if normalized
        )
    )
    while True:
        candidate_page = _turn_event_candidate_page(
            db,
            tenant_id,
            session_id,
            normalized_turn_ids,
            high_watermark,
            after=after,
            page_size=page_size,
        )
        if not candidate_page:
            break
        rows.extend(
            row for row in candidate_page if event_has_turn_identity(row, normalized_turn_ids)
        )
        if len(candidate_page) < page_size:
            break
        last = candidate_page[-1]
        after = (last.created_at, last.id)
    return rows


def partial_turn_events(
    db: Session,
    tenant_id: str,
    session_id: str,
    turn_id: str,
    *,
    page_size: int = TURN_EVENT_PAGE_SIZE,
) -> list[AgentEvent]:
    """按 partial 独有的 turn_id 优先级分页读取 stream_delta，避免整段历史物化。"""

    if page_size <= 0:
        raise ValueError("page_size must be positive")
    normalized_turn_id = normalize_turn_identity(turn_id)
    if not normalized_turn_id:
        return []
    rows: list[AgentEvent] = []
    after: EventCursor | None = None
    while True:
        statement = select(AgentEvent).where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type == "stream_delta",
            turn_identity_filter(
                db,
                (normalized_turn_id,),
                keys=("turn_id", "user_message_id"),
            ),
        )
        if after is not None:
            statement = statement.where(_cursor_after(after))
        candidate_page = list(
            db.exec(
                statement.order_by(AgentEvent.created_at, AgentEvent.id).limit(page_size)
            ).all()
        )
        if not candidate_page:
            break
        rows.extend(
            row
            for row in candidate_page
            if primary_turn_identity(row) == normalized_turn_id
        )
        if len(candidate_page) < page_size:
            break
        last = candidate_page[-1]
        after = (last.created_at, last.id)
    return rows
