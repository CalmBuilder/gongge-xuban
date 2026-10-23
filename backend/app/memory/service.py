"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : service.py
@CallChain  : Agent Loop/持久 capture worker/记忆 API → MemoryService → LLMClient/MemoryRecord
@Description: 快照提取长期记忆，保留 worker 原始 delta 供原子校验，并维护统一读取与归属。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from sqlmodel import Session, select

from app import paths
from app.db.models import (
    AgentProfile, ChatSession, MemoryCaptureScope, MemoryKeyState, MemoryRecord,
    ModelConfig, Tool, User, utc_now,
)
from app.llm import LLMClient
from app.memory.extraction_contract import MemoryExtractionContract, MemoryExtractionVersionInvalid
from app.memory.keys import normalize_memory_key_identity
from app.memory.projection import MemoryCandidate, deduplicate_candidates, safe_memory_metadata
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

    def context_memory_payloads(
        self,
        tenant_id: str,
        user_id: str,
        *,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """生成 Router/DTA 使用的只读记忆 payload，并投影历史会话有效归属。"""

        rows = self.context_memories(tenant_id, user_id, agent_id=agent_id)
        session_agents = self._session_agent_map(tenant_id, rows)
        return [
            memory_read(
                row,
                effective_agent_id=memory_effective_agent_id(row, session_agents),
            )
            for row in rows
        ]

    def projection_candidates(
        self, tenant_id: str, user_id: str, *, agent_id: str | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        """内部读取完整事实与真实来源；不扩张公共API或复用其去重后的缺失来源。"""
        from app.security.permissions import can_use_agent_in_chat

        user = self.db.get(User, user_id, populate_existing=True)
        agent = self.db.get(AgentProfile, agent_id, populate_existing=True) if agent_id else None
        if (user is None or user.tenant_id != tenant_id or user.membership_status != "active"
                or (agent_id and (agent is None or not can_use_agent_in_chat(self.db, agent, user)))):
            return ()
        rows = list(self.db.exec(select(MemoryRecord).where(
            MemoryRecord.tenant_id == tenant_id, MemoryRecord.user_id == user_id,
            MemoryRecord.kind.in_(ALLOWED_MEMORY_KINDS),
        ).execution_options(populate_existing=True)).all())
        sessions = self._session_agent_map(tenant_id, rows)
        scopes = list(self.db.exec(select(MemoryCaptureScope).where(
            MemoryCaptureScope.tenant_id == tenant_id, MemoryCaptureScope.user_id == user_id,
        ).execution_options(populate_existing=True)).all())
        keys = list(self.db.exec(select(MemoryKeyState).where(
            MemoryKeyState.tenant_id == tenant_id, MemoryKeyState.user_id == user_id,
        ).execution_options(populate_existing=True)).all())
        user_scope = next((scope for scope in scopes if scope.scope_kind == "user"), None)
        result = []
        for row in rows:
            effective_agent = memory_effective_agent_id(row, sessions) or ""
            if effective_agent != (agent_id or ""):
                continue
            agent_scope = next((scope for scope in scopes if scope.scope_kind == "agent"
                                and scope.agent_scope_key == (effective_agent or "__user__")), None)
            vector = (user_scope.clear_generation if user_scope else 0,
                      agent_scope.clear_generation if agent_scope else 0,
                      user_scope.permission_generation if user_scope else 0,
                      agent_scope.permission_generation if agent_scope else 0)
            row_vector = (row.user_generation, row.agent_generation,
                          row.user_permission_generation, row.agent_permission_generation)
            if row_vector != vector:
                continue
            key = normalize_memory_key_identity(_read_dedupe_key(row))
            state = next((item for item in keys if item.agent_scope_key == (effective_agent or "__user__")
                          and item.kind == row.kind and item.normalized_key == key), None)
            if state and (state.tombstoned or state.record_id != row.id):
                continue
            result.append(MemoryCandidate(
                id=row.id, kind=row.kind, key=key, content=row.content,
                importance=row.importance, source_sequence=row.source_sequence,
                updated_at=row.updated_at.isoformat(), scope=(tenant_id, user_id, effective_agent),
                generations=vector, agent_capture_generation=agent.memory_capture_generation if agent else 0,
                metadata=safe_memory_metadata(row.metadata_json),
            ))
        return deduplicate_candidates(result)

    def projection_echo_candidates(
        self, tenant_id: str, user_id: str, *, agent_id: str,
        echoes: list[dict[str, object]],
    ) -> tuple[MemoryCandidate, ...]:
        """会话回声保留独立来源类型，冻结同一范围代数以支持后续撤销。"""
        scopes = self.db.exec(select(MemoryCaptureScope).where(
            MemoryCaptureScope.tenant_id == tenant_id, MemoryCaptureScope.user_id == user_id,
        ).execution_options(populate_existing=True)).all()
        user_scope = next((item for item in scopes if item.scope_kind == "user"), None)
        agent_scope = next((item for item in scopes if item.scope_kind == "agent"
                           and item.agent_scope_key == agent_id), None)
        agent = self.db.get(AgentProfile, agent_id, populate_existing=True)
        vector = (user_scope.clear_generation if user_scope else 0,
                  agent_scope.clear_generation if agent_scope else 0,
                  user_scope.permission_generation if user_scope else 0,
                  agent_scope.permission_generation if agent_scope else 0)
        return tuple(MemoryCandidate(
            id=f"echo-{index:02}", key=f"echo-{index:02}", kind=str(item["kind"]),
            content=str(item["content"]), source_sequence=index, origin="conversation",
            metadata=safe_memory_metadata(item.get("metadata")), scope=(tenant_id, user_id, agent_id),
            generations=vector, agent_capture_generation=agent.memory_capture_generation if agent else 0,
        ) for index, item in enumerate(echoes))

    def projection_validator(
        self, tenant_id: str, user_id: str, *, agent_id: str | None = None,
    ) -> Callable[[MemoryCandidate], bool]:
        """创建最终发送校验器；每次使用独立短会话，不相信旧ORM权限或正文存在。"""
        bind = self.db.get_bind()

        def validate(candidate: MemoryCandidate) -> bool:
            """校验当前范围/权限代数和规范键删除身份，拒绝旧来源被新行复活。"""
            from app.security.permissions import can_use_agent_in_chat

            if candidate.scope != (tenant_id, user_id, agent_id or ""):
                return False
            with Session(bind) as current:
                user = current.get(User, user_id)
                agent = current.get(AgentProfile, agent_id) if agent_id else None
                if (user is None or user.tenant_id != tenant_id
                        or user.membership_status != "active"
                        or (agent_id and (agent is None or not can_use_agent_in_chat(current, agent, user)
                                          or agent.memory_capture_generation
                                          != candidate.agent_capture_generation))):
                    return False
                scopes = current.exec(select(MemoryCaptureScope).where(
                    MemoryCaptureScope.tenant_id == tenant_id, MemoryCaptureScope.user_id == user_id,
                )).all()
                user_scope = next((item for item in scopes if item.scope_kind == "user"), None)
                agent_scope = next((item for item in scopes if item.scope_kind == "agent"
                                   and item.agent_scope_key == (agent_id or "__user__")), None)
                vector = (user_scope.clear_generation if user_scope else 0,
                          agent_scope.clear_generation if agent_scope else 0,
                          user_scope.permission_generation if user_scope else 0,
                          agent_scope.permission_generation if agent_scope else 0)
                if candidate.generations != vector:
                    return False
                if candidate.origin == "memory":
                    state = current.exec(select(MemoryKeyState).where(
                        MemoryKeyState.tenant_id == tenant_id,
                        MemoryKeyState.user_id == user_id,
                        MemoryKeyState.agent_scope_key == (agent_id or "__user__"),
                        MemoryKeyState.kind == candidate.kind,
                        MemoryKeyState.normalized_key == normalize_memory_key_identity(
                            candidate.key or candidate.id),
                    )).first()
                    if state and state.tombstoned and (
                        state.user_generation, state.agent_generation,
                        state.user_permission_generation, state.agent_permission_generation,
                    ) == candidate.generations:
                        return False
                    if (state and state.record_id != candidate.id
                            and current.get(MemoryRecord, candidate.id) is None):
                        # 删除后新来源可创建新行，但不能复活已删除的冻结身份。
                        # 原行的合法内容更新仍允许旧计划沿原正文完成。
                        return False
                return True
        return validate

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
        updates = self.extract_turn_delta(
            request,
            session,
            step_result,
            tool_result,
            model_config,
            conversation_messages,
        )
        if not request.user_id:
            return []

        agent_id = session.agent_id or request.agent_id
        user = self.db.get(User, request.user_id)
        username = user.username if user else request.user_id
        records: list[MemoryRecord] = []
        for update in updates:
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

    def extract_turn_delta(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        conversation_messages: list[dict[str, str]],
        *,
        dispatch_observer: Any | None = None,
        timeout_seconds: float | None = None,
        extraction_contract: MemoryExtractionContract | None = None,
    ) -> list[dict[str, Any]]:
        """持久提取沿可信快照发送及解析v1，JSON修复不重读资源且外呼不持事务。"""

        from app.core.context_projection import compact_step_result

        if not request.user_id:
            return []

        if dispatch_observer is not None and extraction_contract is None:
            raise MemoryExtractionVersionInvalid("capture_contract_changed")
        if extraction_contract is not None:
            extraction_contract.validate()
            system_prompt = extraction_contract.prompt_text
        else:
            system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        agent_id = session.agent_id or request.agent_id
        self._assert_agent_available(request.tenant_id, agent_id)
        existing_rows = self._list_user_memories(
            request.tenant_id,
            request.user_id,
            limit=30,
            normalize=False,
            agent_id=agent_id,
        )
        existing_memory_text = _memories_for_model(existing_rows)
        model_config = ModelConfig.model_validate(model_config.model_dump())
        # 读取记忆后即将进入不可控时长的外部模型调用；先结束只读事务，
        # 避免 SQLite 的共享读锁在推理期间阻塞前台流式事件写入。
        self.db.rollback()
        client = (
            LLMClient(model_config, dispatch_observer=dispatch_observer, timeout_seconds=timeout_seconds)
            if dispatch_observer is not None
            else LLMClient(model_config)
        )
        with llm_operation("memory.capture", existing_count=len(existing_rows)):
            raw_delta = client.generate_json(
                system_prompt,
                {
                    "conversation_context": {"messages": conversation_messages},
                    "existing_memories": existing_memory_text,
                    "step_result": compact_step_result(step_result.model_dump(mode="json")),
                    "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
                },
            )
        if dispatch_observer is not None:
            # 持久 worker 必须把整份原输出交给 T3 校验，不过滤坏项或截短键后保存半份。
            items = raw_delta.get("memories")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("MEMORY_CAPTURE_DELTA_INVALID")
            return [dict(item) for item in items]
        return _normalize_memory_updates(raw_delta)

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
            .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
        )
        rows = list(self.db.exec(statement).all())
        session_agents = self._session_agent_map(tenant_id, rows)
        if agent_id:
            rows = [
                row
                for row in rows
                if self._memory_matches_agent(row, agent_id, session_agents)
            ]
        rows = memory_rows_for_read(rows, session_agents)
        if limit is not None:
            rows = rows[:limit]
        return rows

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
            existing.metadata_json = _merge_memory_metadata(existing.metadata_json, metadata)
            normalized_agent_id = _normalize_agent_id(agent_id)
            if normalized_agent_id:
                existing.agent_id = normalized_agent_id
            record = existing
        else:
            record = MemoryRecord(
                tenant_id=tenant_id,
                agent_id=_normalize_agent_id(agent_id),
                user_id=user_id,
                username=username,
                session_id=session_id,
                kind=kind,
                content=content[:1200],
                importance=importance,
                metadata_json=_merge_memory_metadata({}, metadata),
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
                .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
            ).all()
        )
        session_agents = self._session_agent_map(tenant_id, rows)
        if agent_id:
            rows = [
                row
                for row in rows
                if self._memory_matches_agent(row, agent_id, session_agents)
            ]
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
                .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
            ).all()
        )
        session_agents = self._session_agent_map(tenant_id, summary_rows)
        if agent_id:
            existing = next(
                (
                    row
                    for row in summary_rows
                    if self._memory_matches_agent(row, agent_id, session_agents)
                ),
                None,
            )
        else:
            existing = summary_rows[0] if summary_rows else None
        now = utc_now()
        if existing:
            existing.content = summary[:1800]
            existing.username = username
            existing.session_id = session_id
            existing.importance = 0.8
            existing.updated_at = now
            existing_agent_id = _normalize_agent_id(agent_id)
            if existing_agent_id:
                existing.agent_id = existing_agent_id
            existing.metadata_json = {
                **_merge_memory_metadata(existing.metadata_json, metadata),
                "turn_count": int(_record_metadata(existing).get("turn_count", 0)) + 1,
            }
            if existing_agent_id:
                existing.metadata_json["agent_id"] = existing_agent_id
            self.db.add(existing)
            return existing
        record = MemoryRecord(
            tenant_id=tenant_id,
            agent_id=_normalize_agent_id(agent_id),
            user_id=user_id,
            username=username,
            session_id=session_id,
            kind="summary",
            content=summary[:1800],
            importance=0.8,
            metadata_json={
                **_merge_memory_metadata({}, metadata),
                **(
                    {"agent_id": _normalize_agent_id(agent_id)}
                    if _normalize_agent_id(agent_id)
                    else {}
                ),
                "turn_count": 1,
            },
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

    def _memory_matches_agent(
        self,
        record: MemoryRecord,
        agent_id: str | None,
        session_agents: Mapping[str, ChatSession] | None = None,
    ) -> bool:
        """按统一有效归属判断记忆是否属于指定员工。"""

        if session_agents is None:
            session_agents = self._session_agent_map(record.tenant_id, [record])
        return memory_matches_agent(record, agent_id, session_agents)

    def _session_agent_map(
        self,
        tenant_id: str,
        rows: list[MemoryRecord],
    ) -> dict[str, ChatSession]:
        """批量加载同租户历史会话，供所有记忆读写路径复用有效归属。"""

        session_ids = sorted(
            {
                row.session_id
                for row in rows
                if row.session_id and not memory_agent_id(row)
            }
        )
        if not session_ids:
            return {}
        sessions = self.db.exec(
            select(ChatSession).where(
                ChatSession.tenant_id == tenant_id,
                ChatSession.id.in_(session_ids),
            )
        ).all()
        return {session.id: session for session in sessions}


def memory_read(
    record: MemoryRecord,
    *,
    effective_agent_id: str | None = None,
) -> dict[str, Any]:
    """把记忆投影为上下文/API结构，并让有效归属覆盖冲突 metadata。"""

    metadata = dict(_record_metadata(record))
    canonical_agent_id = _normalize_agent_id(record.agent_id)
    if canonical_agent_id:
        metadata["agent_id"] = canonical_agent_id
        metadata["agent_id_source"] = "column"
    elif effective_agent_id:
        metadata["agent_id"] = effective_agent_id
        metadata["agent_id_source"] = (
            "metadata" if memory_agent_id(record) else "session"
        )
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "username": record.username,
        "session_id": record.session_id,
        "kind": record.kind,
        "content": record.content,
        "importance": record.importance,
        "metadata": metadata,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _memories_for_model(records: list[MemoryRecord]) -> str:
    lines: list[str] = []
    for record in records:
        key = str(_record_metadata(record).get("key") or "").strip()
        label = "/".join(part for part in (record.kind, key) if part)
        lines.append(f"- {label}: {record.content}" if label else f"- {record.content}")
    return "\n".join(lines)


def memory_rows_for_read(
    rows: list[MemoryRecord],
    session_agents: Mapping[str, ChatSession] | None = None,
) -> list[MemoryRecord]:
    """按有效员工归属和结构化键去重，调用方须先完成权限及业务条件过滤。"""

    visible: list[MemoryRecord] = []
    seen_keys: set[tuple[str, str, str | None, str]] = set()
    for row in rows:
        dedupe_key = (
            row.user_id,
            row.kind,
            memory_effective_agent_id(row, session_agents),
            _read_dedupe_key(row),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        visible.append(row)
    return visible


def memory_agent_id(record: MemoryRecord) -> str | None:
    """优先读取索引列，并兼容迁移前仅写入 metadata 的历史记录。"""

    canonical_agent_id = _normalize_agent_id(record.agent_id)
    if canonical_agent_id:
        return canonical_agent_id
    metadata = record.metadata_json if isinstance(record.metadata_json, dict) else {}
    value = metadata.get("agent_id")
    return _normalize_agent_id(value)


def memory_effective_agent_id(
    record: MemoryRecord,
    session_agents: Mapping[str, ChatSession] | None = None,
) -> str | None:
    """按规范列、合法 metadata、同用户同租户历史会话计算唯一有效归属。"""

    direct_agent_id = memory_agent_id(record)
    if direct_agent_id:
        return direct_agent_id
    if not record.session_id or not session_agents:
        return None
    session = session_agents.get(record.session_id)
    if session is None or session.tenant_id != record.tenant_id:
        return None
    if session.user_id and session.user_id != record.user_id:
        return None
    return _normalize_agent_id(session.agent_id)


def memory_matches_agent(
    record: MemoryRecord,
    agent_id: str | None,
    session_agents: Mapping[str, ChatSession] | None = None,
) -> bool:
    """判断记忆是否命中员工过滤，未指定员工时保留全租户既有语义。"""

    if agent_id is None:
        return True
    normalized_agent_id = _normalize_agent_id(agent_id)
    if not normalized_agent_id:
        return False
    return memory_effective_agent_id(record, session_agents) == normalized_agent_id


def _normalize_agent_id(value: object) -> str | None:
    """只接受非空字符串作为可参与归属计算的员工标识。"""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _record_metadata(record: MemoryRecord) -> dict[str, Any]:
    """把历史 JSON 元数据安全归一为字典，避免非法形状阻断查询。"""

    return record.metadata_json if isinstance(record.metadata_json, dict) else {}


def _merge_memory_metadata(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """合并记忆元数据，避免空白或非法 agent_id 抹掉既有有效归属。"""

    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in incoming.items():
        if key == "agent_id" and _normalize_agent_id(value) is None:
            continue
        merged[key] = value
    return merged


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
    metadata = _record_metadata(record)
    return metadata.get("key") == key


def _read_dedupe_key(record: MemoryRecord) -> str:
    metadata = _record_metadata(record)
    key = metadata.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    if record.kind == "summary":
        return "summary"
    return record.id
