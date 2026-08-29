"""
@Time       : 2026/08/01 15:20
@Author     : zhanglp8181
@File       : service.py
@CallChain  : Agent Loop/记忆 API → MemoryService → MemoryRecord
@Description: 提取、归并和读取长期记忆，并维护可查询的员工归属字段。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlmodel import Session, select

from app import paths
from app.db.models import AgentProfile, ChatSession, MemoryRecord, ModelConfig, Tool, User, utc_now
from app.llm import LLMClient
from app.observability.spans import llm_operation
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "memory_extractor_prompt.md"
MEMORY_SOURCE = "model_memory_extractor"
PROFILE_NAME_KEY = "preferred_name"
ALLOWED_MEMORY_KINDS = {"profile", "preference", "fact"}


class MemoryAgentUnavailable(RuntimeError):
    """表示记忆所属 Agent 已归档或租户不匹配，迟到任务必须丢弃。"""


class MemoryService:
    """提供按租户、用户和 Agent 隔离的记忆读取与写入能力。"""

    def __init__(self, db: Session):
        """保存当前请求使用的数据库会话。"""

        self.db = db

    def recall(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int | None = None,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        """读取当前用户可用于上下文的长期记忆，并校验 Agent 生命周期。"""

        del query, limit
        return self.context_memories(tenant_id, user_id, agent_id=agent_id)

    def context_memories(
        self,
        tenant_id: str,
        user_id: str,
        *,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        """按租户、用户和可选 Agent 返回去重后的结构化记忆。"""

        self._assert_agent_available(tenant_id, agent_id)
        return [
            row
            for row in self._list_user_memories(
                tenant_id,
                user_id,
                limit=None,
                agent_id=agent_id,
            )
            if row.kind in ALLOWED_MEMORY_KINDS
        ]

    def capture_turn(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        conversation_messages: list[dict[str, str]],
    ) -> list[MemoryRecord]:
        """提取一轮对话记忆，并在 Agent 归档竞态中拒绝迟到模型写入。"""

        from app.core.context_projection import compact_step_result

        if not request.user_id:
            return []

        agent_id = session.agent_id or request.agent_id
        self._assert_agent_available(request.tenant_id, agent_id)
        user = self.db.get(User, request.user_id)
        username = user.username if user else request.user_id
        existing_rows = self._list_user_memories(
            request.tenant_id,
            request.user_id,
            limit=30,
            normalize=False,
            agent_id=agent_id,
        )
        existing_memory_text = _memories_for_model(existing_rows)
        # 读取记忆后即将进入不可控时长的外部模型调用；先结束只读事务，
        # 避免 SQLite 的共享读锁在推理期间阻塞前台流式事件写入。Memory
        # 任务使用独立 Session，后续 upsert 会在模型返回后重新建立事务。
        self.db.rollback()
        with llm_operation("memory.capture", existing_count=len(existing_rows)):
            raw_delta = LLMClient(model_config).generate_json(
                PROMPT_PATH.read_text(encoding="utf-8"),
                {
                    "conversation_context": {
                        "messages": conversation_messages
                    },
                    "existing_memories": existing_memory_text,
                    "step_result": compact_step_result(step_result.model_dump(mode="json")),
                    "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
                },
            )
        records: list[MemoryRecord] = []
        for update in _normalize_memory_updates(raw_delta):
            if update["operation"] == "delete":
                self._delete_keyed_memory(
                    request.tenant_id,
                    request.user_id,
                    update["kind"],
                    update["key"],
                    agent_id=agent_id,
                )
                continue
            records.append(
                self._upsert_keyed_memory(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    username=username,
                    session_id=session.id,
                    kind=update["kind"],
                    key=update["key"],
                    content=update["content"],
                    importance=update["importance"],
                    metadata={
                        "source": MEMORY_SOURCE,
                        "key": update["key"],
                        "reason": update.get("reason"),
                        "agent_id": agent_id,
                    },
                    agent_id=agent_id,
                )
            )

        return records

    def _list_user_memories(
        self,
        tenant_id: str,
        user_id: str,
        limit: int | None = 80,
        normalize: bool = True,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        statement = (
            select(MemoryRecord)
            .where(
                MemoryRecord.tenant_id == tenant_id,
                MemoryRecord.user_id == user_id,
                MemoryRecord.kind != "conversation",
            )
            .order_by(MemoryRecord.updated_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit * 5 if agent_id else limit)
        rows = list(self.db.exec(statement).all())
        if agent_id:
            rows = [row for row in rows if self._memory_matches_agent(row, agent_id)]
        if limit is not None:
            rows = rows[:limit]
        return memory_rows_for_read(rows) if normalize else rows

    def _upsert_keyed_memory(
        self,
        tenant_id: str,
        user_id: str,
        username: str | None,
        session_id: str,
        kind: str,
        key: str,
        content: str,
        importance: float,
        metadata: dict[str, Any],
        agent_id: str | None = None,
    ) -> MemoryRecord:
        """按用户、员工和结构化键更新记忆，并同步可索引的员工归属。"""

        self._assert_agent_available(tenant_id, agent_id)
        existing, duplicates = self._find_keyed_memory_candidates(tenant_id, user_id, kind, key, agent_id=agent_id)
        now = utc_now()
        if existing:
            existing.content = content[:1200]
            existing.username = username
            existing.session_id = session_id
            existing.importance = importance
            existing.updated_at = now
            existing.metadata_json = {**(existing.metadata_json or {}), **metadata}
            existing.agent_id = agent_id
            record = existing
        else:
            record = MemoryRecord(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                username=username,
                session_id=session_id,
                kind=kind,
                content=content[:1200],
                importance=importance,
                metadata_json=metadata,
            )
            self.db.add(record)

        for duplicate in duplicates:
            if duplicate.id != record.id:
                self.db.delete(duplicate)
        self.db.add(record)
        return record

    def _delete_keyed_memory(
        self,
        tenant_id: str,
        user_id: str,
        kind: str,
        key: str,
        agent_id: str | None = None,
    ) -> None:
        """删除指定员工和结构化键的记忆，归档竞态下不执行迟到清理。"""

        self._assert_agent_available(tenant_id, agent_id)
        existing, duplicates = self._find_keyed_memory_candidates(tenant_id, user_id, kind, key, agent_id=agent_id)
        for row in [existing, *duplicates]:
            if row:
                self.db.delete(row)

    def _find_keyed_memory_candidates(
        self,
        tenant_id: str,
        user_id: str,
        kind: str,
        key: str,
        agent_id: str | None = None,
    ) -> tuple[MemoryRecord | None, list[MemoryRecord]]:
        rows = list(
            self.db.exec(
                select(MemoryRecord)
                .where(
                    MemoryRecord.tenant_id == tenant_id,
                    MemoryRecord.user_id == user_id,
                    MemoryRecord.kind == kind,
                )
                .order_by(MemoryRecord.updated_at.desc())
            ).all()
        )
        if agent_id:
            rows = [row for row in rows if self._memory_matches_agent(row, agent_id)]
        candidates = [row for row in rows if _memory_matches_key(row, key)]
        if not candidates:
            return None, []
        return candidates[0], candidates[1:]

    def _upsert_summary(
        self,
        tenant_id: str,
        user_id: str,
        username: str | None,
        session_id: str,
        summary: str,
        metadata: dict[str, Any],
        agent_id: str | None = None,
    ) -> MemoryRecord:
        """更新指定员工的用户摘要，并保持列字段与兼容 metadata 一致。"""

        self._assert_agent_available(tenant_id, agent_id)
        summary_rows = list(
            self.db.exec(
                select(MemoryRecord)
                .where(
                    MemoryRecord.tenant_id == tenant_id,
                    MemoryRecord.user_id == user_id,
                    MemoryRecord.kind == "summary",
                )
                .order_by(MemoryRecord.updated_at.desc())
            ).all()
        )
        if agent_id:
            existing = next((row for row in summary_rows if self._memory_matches_agent(row, agent_id)), None)
        else:
            existing = summary_rows[0] if summary_rows else None
        now = utc_now()
        if existing:
            existing.content = summary[:1800]
            existing.username = username
            existing.session_id = session_id
            existing.importance = 0.8
            existing.updated_at = now
            existing.agent_id = agent_id
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                **metadata,
                "agent_id": agent_id,
                "turn_count": int((existing.metadata_json or {}).get("turn_count", 0)) + 1,
            }
            self.db.add(existing)
            return existing
        record = MemoryRecord(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            username=username,
            session_id=session_id,
            kind="summary",
            content=summary[:1800],
            importance=0.8,
            metadata_json={**metadata, "agent_id": agent_id, "turn_count": 1},
        )
        self.db.add(record)
        return record

    def _assert_agent_available(self, tenant_id: str, agent_id: str | None) -> None:
        """以 Agent 行锁作为记忆写屏障，归档事务优先时拒绝迟到写入。"""

        if not agent_id:
            return
        agent = self.db.exec(
            select(AgentProfile)
            .where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id == agent_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if agent is None or agent.status != "active":
            raise MemoryAgentUnavailable("MEMORY_AGENT_UNAVAILABLE")

    def _memory_matches_agent(self, record: MemoryRecord, agent_id: str | None) -> bool:
        if memory_matches_agent(record, agent_id):
            return True
        if not agent_id or memory_agent_id(record) or not record.session_id:
            return False
        session = self.db.get(ChatSession, record.session_id)
        return bool(session and session.agent_id == agent_id)


def memory_read(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "username": record.username,
        "session_id": record.session_id,
        "kind": record.kind,
        "content": record.content,
        "importance": record.importance,
        "metadata": record.metadata_json or {},
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _memories_for_model(records: list[MemoryRecord]) -> str:
    lines: list[str] = []
    for record in records:
        key = str((record.metadata_json or {}).get("key") or "").strip()
        label = "/".join(part for part in (record.kind, key) if part)
        lines.append(f"- {label}: {record.content}" if label else f"- {record.content}")
    return "\n".join(lines)


def memory_rows_for_read(rows: list[MemoryRecord]) -> list[MemoryRecord]:
    visible: list[MemoryRecord] = []
    seen_keys: set[tuple[str, str, str | None, str]] = set()
    for row in rows:
        dedupe_key = (row.user_id, row.kind, memory_agent_id(row), _read_dedupe_key(row))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        visible.append(row)
    return visible


def memory_agent_id(record: MemoryRecord) -> str | None:
    """优先读取索引列，并兼容迁移前仅写入 metadata 的历史记录。"""

    if record.agent_id and record.agent_id.strip():
        return record.agent_id.strip()
    metadata = record.metadata_json or {}
    value = metadata.get("agent_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def memory_matches_agent(record: MemoryRecord, agent_id: str | None) -> bool:
    if not agent_id:
        return True
    return memory_agent_id(record) == agent_id


def tool_read_for_activity(tool: Tool | None, result: ToolResult | None = None) -> dict[str, Any]:
    return {
        "name": result.tool_name if result else tool.name if tool else "",
        "display_name": tool.display_name if tool else None,
        "description": tool.description if tool else None,
        "success": result.success if result else None,
    }


def _normalize_memory_updates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw.get("memories")
    if not isinstance(items, list):
        return []

    updates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in ALLOWED_MEMORY_KINDS:
            continue
        content = str(item.get("content") or "").strip()
        operation = str(item.get("operation") or "upsert").strip().lower()
        if operation not in {"upsert", "delete"}:
            operation = "upsert"
        if operation == "upsert" and not content:
            continue
        key = _normalize_memory_key(item.get("key"), kind, content)
        updates.append(
            {
                "operation": operation,
                "kind": kind,
                "key": key,
                "content": content,
                "importance": _normalize_importance(item.get("importance")),
                "reason": str(item.get("reason") or "").strip()[:300],
            }
        )
    return updates


def _normalize_summary(raw: dict[str, Any]) -> str:
    value = raw.get("updated_summary") or raw.get("summary")
    if not isinstance(value, str):
        return ""
    return value.strip()[:1800]


def _normalize_memory_key(value: Any, kind: str, content: str) -> str:
    if isinstance(value, str):
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
        if normalized:
            return normalized[:80]
    digest = hashlib.md5(f"{kind}:{content}".encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"{kind}_{digest}"


def _normalize_importance(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.7
    return min(max(number, 0.0), 1.0)


def _memory_matches_key(record: MemoryRecord, key: str) -> bool:
    metadata = record.metadata_json or {}
    return metadata.get("key") == key


def _read_dedupe_key(record: MemoryRecord) -> str:
    metadata = record.metadata_json or {}
    key = metadata.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    if record.kind == "summary":
        return "summary"
    return record.id
