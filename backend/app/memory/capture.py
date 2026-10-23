"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : capture.py
@CallChain  : AgentLoop/DynamicTaskAgent → 来源锚点与持久任务登记 → MemoryCaptureJob
@Description: 冻结记忆提取可信来源、模型合同和失效代数，并提供提交后的唤醒通知边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from threading import Lock
from typing import Any, Callable

from sqlalchemy import update
from sqlmodel import Session, select

from app import paths
from app.db.models import (
    AgentProfile,
    ChatSession,
    MemoryCaptureJob,
    MemoryCaptureScope,
    Message,
    ModelConfig,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.tools.tool_schema import ToolResult
from app.memory.extraction_contract import (
    MEMORY_EXTRACTION_SCHEMA_VERSION, MemoryExtractionContract, MemoryExtractionVersionInvalid,
)


CAPTURE_SCHEMA_VERSION = MEMORY_EXTRACTION_SCHEMA_VERSION
USER_SCOPE_KIND = "user"
AGENT_SCOPE_KIND = "agent"
USER_SCOPE_KEY = "__user__"
MEMORY_JOB_NAME = "memory.capture_turn"
_wake_lock = Lock()
_wake_notifications: list[str] = []
_wake_dispatcher: Callable[[str], None] | None = None


@dataclass(frozen=True)
class MemorySourceAnchor:
    """描述一次用户输入固定的 scope、序号和失效代数。"""

    user_scope_id: str
    agent_scope_id: str
    agent_scope_key: str
    source_sequence: int
    user_generation: int
    agent_generation: int
    user_permission_generation: int
    agent_permission_generation: int
    agent_capture_generation: int
    model_config_id: str | None = None
    model_capture_generation: int | None = None
    model_contract_hash: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        """返回可写入消息 metadata 的来源快照，不包含正文。"""

        return {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "user_scope_id": self.user_scope_id,
            "agent_scope_id": self.agent_scope_id,
            "agent_scope_key": self.agent_scope_key,
            "source_sequence": self.source_sequence,
            "user_generation": self.user_generation,
            "agent_generation": self.agent_generation,
            "user_permission_generation": self.user_permission_generation,
            "agent_permission_generation": self.agent_permission_generation,
            "agent_capture_generation": self.agent_capture_generation,
            "model_config_id": self.model_config_id,
            "model_capture_generation": self.model_capture_generation,
            "model_contract_hash": self.model_contract_hash,
        }


@dataclass(frozen=True)
class MemoryCaptureRegistration:
    """返回持久任务及本次事务是否首次创建它。"""

    job: MemoryCaptureJob
    created: bool


def _canonical_json(value: object) -> str:
    """用稳定 JSON 编码身份摘要，避免 Python dict 顺序参与合同。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_value(value: object) -> str:
    """计算来源、步骤或配置合同的 SHA-256 摘要。"""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _message_hash(message: Message) -> str:
    """只对消息身份、角色和正文做摘要，不把正文复制进任务 manifest。"""

    return _hash_value({"id": message.id, "role": message.role, "content": message.content})


def memory_model_contract_hash(model_config: ModelConfig) -> str:
    """固定提取任务实际使用的模型合同，凭据只保留不可逆引用摘要。"""

    return _hash_value(
        {
            "provider": model_config.provider,
            "model": model_config.model,
            "base_url": model_config.base_url,
            "temperature": model_config.temperature,
            "max_output_tokens": model_config.max_output_tokens,
            "extra_body": model_config.extra_body_json or {},
            "capability_checksum": model_config.capability_checksum,
            "enabled": bool(model_config.enabled),
            "credential_ref": _hash_value(model_config.api_key_encrypted),
        }
    )


def advance_memory_user_permission_generation(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> MemoryCaptureScope:
    """在成员授权事实原事务中单调推进用户 scope 权限代数。"""

    scope = _get_or_create_scope(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        scope_kind=USER_SCOPE_KIND,
        agent_scope_key=USER_SCOPE_KEY,
    )
    scope.permission_generation += 1
    scope.row_version += 1
    scope.updated_at = utc_now()
    db.add(scope)
    return scope


def advance_memory_agent_permission_generation(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    agent_id: str,
) -> MemoryCaptureScope:
    """在 Agent 使用关系原事务中单调推进该用户 Agent scope 权限代数。"""

    scope = _get_or_create_scope(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        scope_kind=AGENT_SCOPE_KIND,
        agent_scope_key=agent_id,
    )
    scope.permission_generation += 1
    scope.row_version += 1
    scope.updated_at = utc_now()
    db.add(scope)
    return scope


def advance_memory_clear_generation(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    agent_id: str | None,
) -> tuple[MemoryCaptureScope, MemoryCaptureScope | None]:
    """在本人清空事务中先锁用户再锁 Agent，并推进对应清空代数。"""

    user_scope = _get_or_create_scope(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        scope_kind=USER_SCOPE_KIND,
        agent_scope_key=USER_SCOPE_KEY,
    )
    agent_scope = None
    if agent_id:
        agent_scope = _get_or_create_scope(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            scope_kind=AGENT_SCOPE_KIND,
            agent_scope_key=agent_id,
        )
        agent_scope.clear_generation += 1
        agent_scope.row_version += 1
        agent_scope.updated_at = utc_now()
        db.add(agent_scope)
    else:
        user_scope.clear_generation += 1
        user_scope.row_version += 1
        user_scope.updated_at = utc_now()
        db.add(user_scope)
    return user_scope, agent_scope


def memory_extractor_prompt_hash() -> str:
    """冻结可信资源摘要；不可读版本保留空合同，worker明确失效且不发送。"""

    prompt_path = paths.resource_dir() / "app" / "llm" / "prompts" / "memory_extractor_prompt.md"
    try:
        return MemoryExtractionContract.read(prompt_path).prompt_hash
    except MemoryExtractionVersionInvalid:
        return ""


def _scope_statement(
    tenant_id: str,
    user_id: str,
    scope_kind: str,
    agent_scope_key: str,
):
    """构造一个带行锁的 scope 查询。"""

    return (
        select(MemoryCaptureScope)
        .where(
            MemoryCaptureScope.tenant_id == tenant_id,
            MemoryCaptureScope.user_id == user_id,
            MemoryCaptureScope.scope_kind == scope_kind,
            MemoryCaptureScope.agent_scope_key == agent_scope_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _get_or_create_scope(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    scope_kind: str,
    agent_scope_key: str,
) -> MemoryCaptureScope:
    """在当前输入事务中锁定或建立一个来源 scope。"""

    scope = db.exec(_scope_statement(tenant_id, user_id, scope_kind, agent_scope_key)).first()
    if scope is None:
        scope = MemoryCaptureScope(
            id=new_id("memscope"),
            tenant_id=tenant_id,
            user_id=user_id,
            scope_kind=scope_kind,
            agent_scope_key=agent_scope_key,
        )
        db.add(scope)
        db.flush()
    else:
        # SQLite 忽略 FOR UPDATE；实际 UPDATE 取得写权后才读取代数。
        db.exec(
            update(MemoryCaptureScope)
            .where(MemoryCaptureScope.id == scope.id)
            .values(row_version=MemoryCaptureScope.row_version + 1)
            .execution_options(synchronize_session=False)
        )
        db.refresh(scope)
    return scope


def freeze_memory_source_anchor(
    db: Session,
    *,
    tenant_id: str,
    user_id: str | None,
    agent_id: str | None,
    message: Message,
    model_config: ModelConfig | None = None,
) -> MemorySourceAnchor | None:
    """在用户消息进入当前事务时固定唯一来源序号和全部失效代数。"""

    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id or message.tenant_id != tenant_id or message.role != "user":
        return None
    if agent_id:
        agent = db.get(AgentProfile, agent_id)
        if agent is None or agent.tenant_id != tenant_id or agent.status != "active":
            return None

    user_scope = _get_or_create_scope(
        db,
        tenant_id=tenant_id,
        user_id=normalized_user_id,
        scope_kind=USER_SCOPE_KIND,
        agent_scope_key=USER_SCOPE_KEY,
    )
    user_scope.source_sequence += 1
    user_scope.row_version += 1
    user_scope.updated_at = utc_now()
    db.add(user_scope)

    normalized_agent_id = str(agent_id or "").strip()
    if normalized_agent_id:
        agent_scope = _get_or_create_scope(
            db,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
            scope_kind=AGENT_SCOPE_KIND,
            agent_scope_key=normalized_agent_id,
        )
        agent_capture_generation = int(
            getattr(db.get(AgentProfile, normalized_agent_id), "memory_capture_generation", 1)
            or 1
        )
    else:
        agent_scope = user_scope
        agent_capture_generation = 1

    anchor = MemorySourceAnchor(
        user_scope_id=user_scope.id,
        agent_scope_id=agent_scope.id,
        agent_scope_key=normalized_agent_id or USER_SCOPE_KEY,
        source_sequence=int(user_scope.source_sequence),
        user_generation=int(user_scope.clear_generation),
        agent_generation=int(agent_scope.clear_generation),
        user_permission_generation=int(user_scope.permission_generation),
        agent_permission_generation=int(agent_scope.permission_generation),
        agent_capture_generation=agent_capture_generation,
        model_config_id=model_config.id if model_config else None,
        model_capture_generation=model_config.memory_capture_generation if model_config else None,
        model_contract_hash=memory_model_contract_hash(model_config) if model_config else None,
    )
    message_metadata = dict(message.metadata_json or {})
    message_metadata["memory_capture_source"] = anchor.as_metadata()
    message.metadata_json = message_metadata
    db.add(message)
    return anchor


def source_anchor_from_message(message: Message) -> MemorySourceAnchor | None:
    """读取并严格校验用户消息上的来源快照，缺失时不猜测会话最新消息。"""

    payload = (message.metadata_json or {}).get("memory_capture_source")
    if not isinstance(payload, dict):
        return None
    try:
        values = {
            "user_scope_id": str(payload["user_scope_id"]),
            "agent_scope_id": str(payload["agent_scope_id"]),
            "agent_scope_key": str(payload["agent_scope_key"]),
            "source_sequence": int(payload["source_sequence"]),
            "user_generation": int(payload["user_generation"]),
            "agent_generation": int(payload["agent_generation"]),
            "user_permission_generation": int(payload["user_permission_generation"]),
            "agent_permission_generation": int(payload["agent_permission_generation"]),
            "agent_capture_generation": int(payload["agent_capture_generation"]),
            "model_config_id": payload.get("model_config_id"),
            "model_capture_generation": payload.get("model_capture_generation"),
            "model_contract_hash": payload.get("model_contract_hash"),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (
        payload.get("schema_version") != CAPTURE_SCHEMA_VERSION
        or values["source_sequence"] < 1
        or not values["agent_scope_key"]
    ):
        return None
    return MemorySourceAnchor(**values)


def memory_source_anchor_is_current(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    anchor: MemorySourceAnchor,
) -> bool:
    """核对来源锚点的清除、权限和 Agent 捕获代数，阻止失效 SOP 槽位回流。"""

    if not user_id.strip() or not anchor.agent_scope_key:
        return False
    scopes = db.exec(
        select(MemoryCaptureScope).where(
            MemoryCaptureScope.tenant_id == tenant_id,
            MemoryCaptureScope.user_id == user_id,
        )
    ).all()
    user_scope = next(
        (
            scope
            for scope in scopes
            if scope.scope_kind == USER_SCOPE_KIND and scope.agent_scope_key == USER_SCOPE_KEY
        ),
        None,
    )
    agent_scope = next(
        (
            scope
            for scope in scopes
            if scope.scope_kind == AGENT_SCOPE_KIND
            and scope.agent_scope_key == anchor.agent_scope_key
        ),
        None,
    )
    current = (
        int(user_scope.clear_generation if user_scope else 0),
        int(agent_scope.clear_generation if agent_scope else 0),
        int(user_scope.permission_generation if user_scope else 0),
        int(agent_scope.permission_generation if agent_scope else 0),
    )
    captured = (
        int(anchor.user_generation),
        int(anchor.agent_generation),
        int(anchor.user_permission_generation),
        int(anchor.agent_permission_generation),
    )
    if current != captured:
        return False
    if anchor.agent_scope_key == USER_SCOPE_KEY:
        return True
    agent = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == anchor.agent_scope_key,
        )
    ).first()
    return bool(
        agent is not None
        and int(agent.memory_capture_generation or 0) == int(anchor.agent_capture_generation)
    )


def freeze_memory_model_selection(db: Session, message: Message, model: ModelConfig) -> None:
    """首次实际选模时冻结无凭据合同；重放或完成登记不能替换已冻结模型。"""

    anchor = source_anchor_from_message(message)
    if anchor is None or anchor.model_config_id or message.tenant_id != model.tenant_id:
        return
    metadata = dict(message.metadata_json or {})
    metadata["memory_capture_source"] = {
        **anchor.as_metadata(),
        "model_config_id": model.id,
        "model_capture_generation": model.memory_capture_generation,
        "model_contract_hash": memory_model_contract_hash(model),
    }
    message.metadata_json = metadata
    db.add(message)


def _request_step_hash(step_result: StepAgentResult | None) -> str:
    """对步骤结果做摘要，manifest 不携带模型输入正文。"""

    return _hash_value(step_result.model_dump(mode="json") if step_result else {})


def _request_tool_hash(tool_result: ToolResult | None) -> str:
    """对工具结果做摘要，防止后台任务依赖错轮内存对象。"""

    return _hash_value(tool_result.model_dump(mode="json") if tool_result else {})


def _source_message_manifest(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    source_message_id: str,
    cutoff_message_id: str,
) -> list[dict[str, str]] | None:
    """按固定 assistant 截止点记录全部消息身份和摘要，不限制最近条数。"""

    rows = list(
        db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    by_id = {row.id: row for row in rows}
    rows.sort(key=lambda row: _message_history_order(row, by_id))
    cutoff_index = next(
        (index for index, row in enumerate(rows) if row.id == cutoff_message_id),
        None,
    )
    if cutoff_index is None:
        return None
    if not any(row.id == source_message_id for row in rows[: cutoff_index + 1]):
        return None
    manifest: list[dict[str, str]] = []
    for row in rows[: cutoff_index + 1]:
        if row.role not in {"user", "assistant"}:
            continue
        manifest.append({"id": row.id, "role": row.role, "hash": _message_hash(row)})
    return manifest


def _message_history_order(message: Message, by_id: dict[str, Message]) -> tuple:
    """受审轮次按冻结来源序排列，助手紧随显式来源；历史无锚点消息保持时间顺序。"""

    origin = message
    if message.role == "assistant":
        metadata = message.metadata_json or {}
        source_id = metadata.get("user_message_id") or metadata.get("turn_id")
        origin = by_id.get(str(source_id), message)
    anchor = source_anchor_from_message(origin) if origin.role == "user" else None
    # MySQL DATETIME 读回值与落库前微秒可能跨秒；用户来源序才是原轮顺序。
    # 同一助手与用户共享 origin，固定 cutoff 不依赖随机 ID 或时间精度。
    return (
        1 if anchor else 0,
        anchor.source_sequence if anchor else origin.created_at,
        origin.id,
        1 if message.role == "assistant" else 0,
        message.id,
    )


def register_memory_capture_job(
    db: Session,
    request: ChatTurnRequest,
    chat_session: ChatSession,
    model_config: ModelConfig,
    *,
    user_message_id: str,
    assistant_message_id: str | None,
    step_result: StepAgentResult | None = None,
    tool_result: ToolResult | None = None,
    events: Any | None = None,
) -> MemoryCaptureRegistration | None:
    """在原完成事务内登记唯一持久任务，重复完成只复用原任务。"""

    source_message = db.get(Message, user_message_id)
    if (
        source_message is None
        or source_message.tenant_id != request.tenant_id
        or source_message.session_id != chat_session.id
        or source_message.role != "user"
        or source_message.id != user_message_id
    ):
        return None
    anchor = source_anchor_from_message(source_message)
    if anchor is None:
        return None
    if request.user_id and request.user_id != chat_session.user_id:
        return None
    user_id = str(chat_session.user_id or request.user_id or "").strip()
    if not user_id:
        return None
    normalized_agent_id = str(chat_session.agent_id or request.agent_id or "").strip() or None
    if (
        model_config.tenant_id != request.tenant_id
        or not anchor.model_config_id
        or anchor.model_config_id != model_config.id
        or not anchor.model_contract_hash
        or anchor.model_capture_generation is None
        or anchor.agent_scope_key != (normalized_agent_id or USER_SCOPE_KEY)
    ):
        return None

    assistant_message = db.get(Message, assistant_message_id) if assistant_message_id else None
    if (
        assistant_message is None
        or assistant_message.tenant_id != request.tenant_id
        or assistant_message.session_id != chat_session.id
        or assistant_message.role != "assistant"
        or any(
            str((assistant_message.metadata_json or {}).get(key) or source_message.id)
            != source_message.id
            for key in ("turn_id", "user_message_id")
        )
        or not any(
            (assistant_message.metadata_json or {}).get(key) == source_message.id
            for key in ("turn_id", "user_message_id")
        )
    ):
        return None

    existing = db.exec(
        select(MemoryCaptureJob)
        .where(
            MemoryCaptureJob.tenant_id == request.tenant_id,
            MemoryCaptureJob.user_id == user_id,
            MemoryCaptureJob.agent_scope_key == anchor.agent_scope_key,
            MemoryCaptureJob.source_message_id == source_message.id,
        )
        .with_for_update()
    ).first()
    if existing is not None:
        return (MemoryCaptureRegistration(job=existing, created=False)
            if existing.source_assistant_message_id == assistant_message.id else None)

    source_manifest = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "source_message_id": source_message.id,
        "source_message_hash": _message_hash(source_message),
        "source_assistant_message_id": assistant_message.id,
        "source_assistant_message_hash": _message_hash(assistant_message),
        "source_cutoff_assistant_message_id": assistant_message.id,
        "source_sequence": anchor.source_sequence,
        "step_result_hash": _request_step_hash(step_result),
        "tool_result_hash": _request_tool_hash(tool_result),
    }
    history_manifest = _source_message_manifest(
        db,
        tenant_id=request.tenant_id,
        session_id=chat_session.id,
        source_message_id=source_message.id,
        cutoff_message_id=assistant_message.id,
    )
    if history_manifest is None:
        return None
    source_manifest["messages"] = history_manifest
    job = MemoryCaptureJob(
        id=new_id("memjob"),
        tenant_id=request.tenant_id,
        user_id=user_id,
        agent_id=normalized_agent_id,
        agent_scope_key=anchor.agent_scope_key,
        scope_id=anchor.agent_scope_id,
        source_message_id=source_message.id,
        source_assistant_message_id=assistant_message.id,
        source_sequence=anchor.source_sequence,
        user_generation=anchor.user_generation,
        agent_generation=anchor.agent_generation,
        user_permission_generation=anchor.user_permission_generation,
        agent_permission_generation=anchor.agent_permission_generation,
        agent_capture_generation=anchor.agent_capture_generation,
        source_manifest_json=source_manifest,
        source_hash=_hash_value(source_manifest),
        model_config_id=anchor.model_config_id,
        model_capture_generation=anchor.model_capture_generation,
        model_contract_hash=anchor.model_contract_hash,
        extractor_schema_version=CAPTURE_SCHEMA_VERSION,
        prompt_hash=memory_extractor_prompt_hash(),
        status="pending",
        deadline_at=utc_now() + timedelta(seconds=600),
    )
    db.add(job)
    db.flush()
    if events is not None:
        events.record(
            request.tenant_id,
            chat_session.id,
            "async_job_enqueued",
            {
                "job_id": job.id, "job_name": MEMORY_JOB_NAME, "feature": "memory",
                "source_message_id": source_message.id,
                "assistant_message_id": assistant_message.id,
            },
        )
    return MemoryCaptureRegistration(job=job, created=True)


def wake_memory_capture_job(job_id: str) -> None:
    """记录提交后的 best-effort 唤醒通知，任务本身始终由数据库保存。"""

    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return
    with _wake_lock:
        if normalized_job_id not in _wake_notifications:
            _wake_notifications.append(normalized_job_id)
        dispatcher = _wake_dispatcher
    if dispatcher is not None:
        try:
            dispatcher(normalized_job_id)
        except Exception:  # noqa: BLE001 - scanner remains the durable recovery path.
            return


def configure_memory_capture_wakeup(dispatcher: Callable[[str], None] | None) -> None:
    """配置提交后唤醒的进程内投递器，数据库扫描仍是唯一恢复事实。"""

    global _wake_dispatcher
    with _wake_lock:
        _wake_dispatcher = dispatcher


def drain_memory_capture_wakeups() -> list[str]:
    """取出测试或未来持久 worker 使用的唤醒通知，不改变任务状态。"""

    with _wake_lock:
        notifications = list(_wake_notifications)
        _wake_notifications.clear()
    return notifications
