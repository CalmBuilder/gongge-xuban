"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : test_final_f3_sop_external_delivery.py
@CallChain  : pytest → 原外部任务Inbox/Signal → SOP coordinator → 最终Message/来源事件
@Description: 重现远端效果已完成而聊天仍显示等待，约束准确来源、一次交付及撤权屏障。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.time_context import freeze_time_context
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentUsage,
    ChatSession,
    ExecutionResult,
    ExecutionSignal,
    ExternalTaskEventInbox,
    Message,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    Tenant,
    Tool,
    User,
)
from app.dynamic_tasks.capability_catalog import (
    ToolReliabilityContract,
    capability_checksum,
    publish_tool_contract,
    published_tool_snapshot,
)
from app.feedback.source import feedback_source
from app.observability.event_log import EventLog
from app.sop_runtime.external_tasks import (
    apply_pending_external_task_events,
    due_sop_external_task_signals,
    ingest_external_task_event,
    process_sop_external_task_signal,
)
from app.sop_runtime.execution_store import SopExecutionStore
from tests.test_external_async_tools import (
    _configure_sop_recovery_definition,
    _provider_contract,
)


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def delivery_engine(request, tmp_path):
    """使用独立SQLite或原随机最小MySQL账号，并以真实Alembic初始化MySQL。"""

    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine(
            "sqlite:///" + str(tmp_path / "external-delivery.sqlite3"),
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _started_visible_operation(
    db: Session, *, user_output_paths: tuple[str, ...], missing_user_schema: bool,
    request: dict[str, object] | None = None,
):
    """事前发布原工具三视图并在prepare时冻结，可为状态机回归指定原始请求参数。"""

    tool = Tool(
        id="f3-delivery-tool", tenant_id="tenant_sd4", name="orders.submit", method="POST",
        url="https://provider.example/submit", output_schema={
            "type": "object", "properties": {
                "receipt_id": {"type": "string"},
                "business": {"type": "object", "properties": {
                    "object_id": {"type": "string"}, "units": {"type": "integer"},
                    "warehouse": {"type": "string"}, "registered": {"type": "boolean"},
                }},
                "provider_trace": {"type": "string"},
            },
        },
    )
    contract_body = _provider_contract().model_dump(mode="json")
    contract_body["model_visibility"] = {
        "allowed_paths": ["output.receipt_id", "output.business"],
        "user_display_paths": ["output." + path for path in user_output_paths],
        "audit_only_paths": ["output.provider_trace"],
    }
    publish_tool_contract(tool, ToolReliabilityContract.model_validate(contract_body))
    db.add(tool)
    db.flush()
    published = published_tool_snapshot(tool, "f3-agent")
    assert published is not None
    frozen = published.model_dump(mode="json", exclude={"checksum", "agent_id"})
    if missing_user_schema:
        # 严格旧快照控制：保留审计合同，缺失user输出schema不等于允许原始回执披露。
        frozen["user_view"].pop("output_schema")
    store = SopExecutionStore(db)
    instance, created = store.start_instance(
        tenant_id="tenant_sd4", session_id="session_sd4", skill_id="skill_sd4",
        skill_version_id="skillver_sd4", skill_version="1.0.0", definition_checksum="a" * 64,
        start_node_id="submit",
    )
    assert created is True
    with store.owned(instance, worker_id="sd4-test"):
        execution = store.enter_node(instance, "submit", input_snapshot={})
        operation, created = store.prepare_operation(
            instance, execution, operation_name=tool.name,
            request=request if request is not None else {"order_id": "A-1"},
            effect_kind="external_write", capability_snapshot=frozen,
            capability_snapshot_checksum=capability_checksum(frozen),
        )
        assert created is True
        store.start_operation(operation)
    db.commit()
    return store, instance, execution, operation


def _ready_signal(
    db: Session, *, closed_turn: str | None = None, provider_status: str = "completed",
    user_output_paths: tuple[str, ...] = (
        "receipt_id", "business.object_id", "business.units", "business.warehouse",
        "business.registered",
    ),
    provider_result: dict[str, object] | None = None, missing_user_schema: bool = False,
    request: dict[str, object] | None = None,
):
    """通过原accepted及callback建立回执，保留来源消息并允许测试指定冻结请求参数。"""

    db.add(Tenant(id="tenant_sd4", name="F3正式交付隔离租户"))
    db.add(User(id="f3-owner", tenant_id="tenant_sd4", username="owner", password_hash="x"))
    db.add(User(id="f3-initiator", tenant_id="tenant_sd4", username="user", password_hash="x"))
    db.add(AgentProfile(
        id="f3-agent", tenant_id="tenant_sd4", name="F3交付员工", owner_user_id="f3-owner",
        published_to_gallery=True,
    ))
    db.add(AgentUsage(tenant_id="tenant_sd4", agent_id="f3-agent", user_id="f3-initiator"))
    db.commit()
    store, instance, execution, operation = _started_visible_operation(
        db, user_output_paths=user_output_paths, missing_user_schema=missing_user_schema,
        request=request,
    )
    _configure_sop_recovery_definition(db, instance)
    instance.agent_id = "f3-agent"
    instance.initiator_user_id = "f3-initiator"
    instance.source_ref = "f3-original-goal"
    session = db.get(ChatSession, instance.session_id)
    session.agent_id = instance.agent_id
    session.user_id = instance.initiator_user_id
    anchor = datetime(2026, 10, 3, 15, 59, tzinfo=UTC)
    time_context = freeze_time_context(
        source_input_id="f3-confirm-source", client_timezone="Asia/Shanghai", anchor_at_utc=anchor,
    )
    for identifier, body, instant, metadata in (
        ("f3-original-goal", "请处理编号AA83的业务", anchor - timedelta(minutes=1), {}),
        ("f3-confirm-source", "确认原动作", anchor, {
            "client_turn_id": "f3-confirm-client", "time_context": time_context,
        }),
        ("f3-unrelated-later", "另一个无关问题", anchor + timedelta(days=1), {}),
    ):
        db.add(Message(
            id=identifier, tenant_id=instance.tenant_id, session_id=instance.session_id,
            role="user", content=body, metadata_json=metadata, created_at=instant,
        ))
    execution.input_json = {"slots": {}, "time_context": time_context}
    instance.context_json = {"time_context": time_context}
    db.add(session)
    db.add(execution)
    db.add(instance)
    operation.external_callback_token_hash = hashlib.sha256(b"f3-unit-callback").hexdigest()
    db.add(operation)
    with store.owned(instance, worker_id="sd4-test"):
        store.mark_operation_waiting_external(
            operation, provider_task_id="f3-unit-task", provider_status="accepted",
            receipt={"status_code": 202}, status_url="https://provider.example/tasks/{taskId}",
            tracking_seconds=300,
        )
    db.flush()
    if closed_turn:
        EventLog(db).record(instance.tenant_id, instance.session_id, closed_turn, {
            "turn_id": "f3-confirm-source", "user_message_id": "f3-confirm-source",
            "client_turn_id": "f3-confirm-client", "status": "waiting" if closed_turn == "complete"
            else "cancelled",
        })
    db.commit()
    ingest_external_task_event(
        db, tenant_id=instance.tenant_id, operation_id=operation.id,
        provider_task_id="f3-unit-task", provider_event_id="f3-unit-completed",
        provider_status=provider_status, observation_revision=2,
        payload={"result": provider_result if provider_result is not None else {
            "receipt_id": "RCPT-AA83", "business": {
            "object_id": "AA83", "units": 7, "warehouse": "南区", "registered": True,
        }}}, callback_token="f3-unit-callback",
    )
    assert apply_pending_external_task_events(db) == 1
    signal = db.exec(select(ExecutionSignal).where(
        ExecutionSignal.execution_id == instance.id,
        ExecutionSignal.signal_type == "external_event",
    )).one()
    return instance, operation, signal, time_context


def _deliveries(db: Session, instance: SopInstance) -> list[Message]:
    """按原实例和终态快照定位最终回复，拒绝把此前等待文案误计为交付。"""

    return [message for message in db.exec(select(Message).where(
        Message.tenant_id == instance.tenant_id, Message.session_id == instance.session_id,
        Message.role == "assistant",
    )).all() if message.metadata_json.get("execution_id") == instance.id
        and (message.metadata_json.get("feedback_source") or {}).get("execution_status")
        == "succeeded"]


def _wait_for_original_signal_due(db: Session, signal: ExecutionSignal) -> None:
    """有限轮询原数据库UTC时间，等待真实退避截止，不改写时间、Signal或成功状态。"""

    deadline = time.monotonic() + 10
    while True:
        db.rollback()
        db.refresh(signal)
        if SopExecutionStore(db).database_now() >= signal.available_at:
            return
        if time.monotonic() >= deadline:
            raise AssertionError("原Signal在10秒内尚未达到数据库退避截止")
        time.sleep(0.05)


def test_legacy_session_source_completes_without_inventing_chat_delivery(delivery_engine):
    """旧实例仅以会话ID作来源时保留恢复成功，不拿现存消息补造可反馈交付。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(db)
        instance.source_ref = instance.session_id
        instance.context_json = {}
        execution = db.get(SopNodeExecution, operation.node_execution_id)
        execution.input_json = {}
        db.add(instance)
        db.add(execution)
        db.commit()
        process_sop_external_task_signal(db, signal)
        db.refresh(instance)
        db.refresh(signal)
        assert instance.status == "succeeded"
        assert signal.status == "consumed"
        assert _deliveries(db, instance) == []
        assert db.exec(select(Message).where(Message.role == "assistant")).all() == []


@pytest.mark.parametrize("closed_turn", [None, "complete"])
def test_completed_external_sop_delivers_original_receipt_and_exact_cause(
    delivery_engine, closed_turn,
):
    """原恢复必须交付真实Operation回执并冻结版本/确认致因，封口轮次不重新complete。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, time_context = _ready_signal(db, closed_turn=closed_turn)
        process_sop_external_task_signal(db, signal)
        db.refresh(instance)
        db.refresh(signal)
        assert instance.status == "succeeded"
        assert signal.status == "consumed"
        deliveries = _deliveries(db, instance)
        assert len(deliveries) == 1, "外部效果完成后仍缺少原实例的最终聊天回复"
        message = deliveries[0]
        assert "RCPT-AA83" in message.content
        for fact in ("AA83", "7", "南区"):
            assert fact in message.content
        metadata = message.metadata_json
        assert metadata["source"] == "runtime_control"
        assert metadata["render_policy"] == "verbatim"
        assert metadata["user_message_id"] == "f3-confirm-source"
        assert metadata["turn_id"] == "f3-confirm-source"
        assert metadata["client_turn_id"] == "f3-confirm-client"
        assert metadata["time_context"] == time_context
        result = db.get(ExecutionResult, metadata["result_id"])
        assert result.execution_id == instance.id
        assert result.tenant_id == instance.tenant_id
        source = feedback_source(db, message)
        assert source.status == "verified"
        assert source.branch == "sop"
        assert source.execution_id == instance.id
        assert source.skill_version_id == instance.skill_version_id
        assert source.definition_checksum == instance.definition_checksum
        assert source.turn_id == "f3-confirm-source"
        assert source.agent_id == instance.agent_id
        assert metadata["feedback_source"]["operations"][0]["id"] == operation.id
        assert metadata["feedback_source"]["operations"][0]["status"] == "succeeded"
        events = db.exec(select(AgentEvent).where(
            AgentEvent.tenant_id == instance.tenant_id,
            AgentEvent.session_id == instance.session_id,
        )).all()
        delivery_events = [event for event in events
                           if event.payload_json.get("assistant_message_id") == message.id]
        assert len(delivery_events) >= 1
        completes = [event for event in events if event.event_type == "complete"
                     and event.payload_json.get("turn_id") == "f3-confirm-source"]
        assert len(completes) == 1
        if closed_turn:
            assert completes[0].payload_json["status"] == "waiting"


def test_duplicate_signal_and_callback_deliver_one_final_message(delivery_engine):
    """原Signal与相同callback重复消费保持原Operation和最终回复各一次。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(db)
        process_sop_external_task_signal(db, signal)
        ingest_external_task_event(
            db, tenant_id=instance.tenant_id, operation_id=operation.id,
            provider_task_id="f3-unit-task", provider_event_id="f3-unit-completed",
            provider_status="completed", observation_revision=2,
            payload={"result": {"receipt_id": "RCPT-AA83", "business": {
                "object_id": "AA83", "units": 7, "warehouse": "南区", "registered": True,
            }}}, callback_token="f3-unit-callback",
        )
        for _ in range(2):
            process_sop_external_task_signal(db, db.get(ExecutionSignal, signal.id))
        assert len(_deliveries(db, instance)) == 1
        assert len(db.exec(select(SopOperation).where(
            SopOperation.instance_id == instance.id,
        )).all()) == 1
        assert operation.status == "succeeded"


def test_two_worker_sessions_deliver_one_final_message(delivery_engine):
    """两个真实Session并发认领同一Signal，只有原CAS获胜者可原子追加最终回复。"""

    with Session(delivery_engine) as db:
        instance, _operation, signal, _time_context = _ready_signal(db)
        instance_id, signal_id = instance.id, signal.id
    barrier = threading.Barrier(2)
    errors = []

    def consume():
        """独立数据库连接在同一认领起点并发进入原恢复函数。"""

        try:
            with Session(delivery_engine) as db:
                current = db.get(ExecutionSignal, signal_id)
                barrier.wait(timeout=10)
                process_sop_external_task_signal(db, current)
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with Session(delivery_engine) as db:
        instance = db.get(SopInstance, instance_id)
        assert instance.status == "succeeded"
        assert len(_deliveries(db, instance)) == 1


@pytest.mark.parametrize("barrier", [
    "missing_source", "foreign_source", "source_not_user", "session_actor_changed",
    "agent_archived", "usage_revoked", "user_inactive", "version_changed",
])
def test_invalid_source_or_permission_never_discloses_final_receipt(delivery_engine, barrier):
    """回执完成后的来源、权限或版本失效必须零最终披露，不能采用最新消息补身份。"""

    with Session(delivery_engine) as db:
        instance, _operation, signal, _time_context = _ready_signal(db)
        source = db.get(Message, "f3-confirm-source")
        if barrier == "missing_source":
            db.delete(source)
        elif barrier == "foreign_source":
            source.tenant_id = "f3-foreign-tenant"
            db.add(source)
        elif barrier == "source_not_user":
            source.role = "assistant"
            db.add(source)
        elif barrier == "session_actor_changed":
            session = db.get(ChatSession, instance.session_id)
            session.user_id = "f3-owner"
            db.add(session)
        elif barrier == "agent_archived":
            agent = db.get(AgentProfile, instance.agent_id)
            agent.status = "archived"
            db.add(agent)
        elif barrier == "usage_revoked":
            db.delete(db.exec(select(AgentUsage).where(
                AgentUsage.agent_id == instance.agent_id,
                AgentUsage.user_id == instance.initiator_user_id,
            )).one())
        elif barrier == "user_inactive":
            user = db.get(User, instance.initiator_user_id)
            user.membership_status = "inactive"
            db.add(user)
        else:
            version = db.get(SkillVersion, instance.skill_version_id)
            version.compiled_definition_checksum = "f" * 64
            db.add(version)
        db.commit()
        process_sop_external_task_signal(db, signal)
        assert _deliveries(db, instance) == []
        replies = db.exec(select(Message).where(Message.role == "assistant")).all()
        assert all("RCPT-AA83" not in message.content for message in replies)


@pytest.mark.parametrize("closed_turn", ["stream_cancelled", "stream_interrupted"])
def test_cancelled_turn_never_receives_late_success(delivery_engine, closed_turn):
    """原轮次取消或中断后，即使远端真实完成也不能追加成功回复或重开轮次。"""

    with Session(delivery_engine) as db:
        instance, _operation, signal, _time_context = _ready_signal(db, closed_turn=closed_turn)
        process_sop_external_task_signal(db, signal)
        assert _deliveries(db, instance) == []
        assert db.exec(select(AgentEvent).where(AgentEvent.event_type == "complete")).all() == []


@pytest.mark.parametrize("provider_status", ["failed", "expired"])
def test_non_success_provider_status_never_delivers_success(delivery_engine, provider_status):
    """供应商失败或截止后的原external_write对账等待不得伪装为业务成功。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(
            db, provider_status=provider_status,
        )
        process_sop_external_task_signal(db, signal)
        db.refresh(operation)
        assert operation.status == "unknown"
        assert _deliveries(db, instance) == []


def test_delivery_write_crash_rolls_back_completion_and_retry_delivers_once(
    delivery_engine, monkeypatch,
):
    """最终Message写入异常与实例终态同事务回滚，原Signal重试只交付一次。"""

    with Session(delivery_engine) as db:
        instance, _operation, signal, _time_context = _ready_signal(db)
        original_add = db.add
        crashed = False

        def add(item, *args, **kwargs):
            """在原最终回复进入Session后制造一次可回滚持久化异常。"""

            nonlocal crashed
            original_add(item, *args, **kwargs)
            if (not crashed and isinstance(item, Message) and item.role == "assistant"
                    and item.metadata_json.get("execution_id") == instance.id):
                crashed = True
                raise RuntimeError("F3_DELIVERY_WRITE_CRASH")

        monkeypatch.setattr(db, "add", add)
        process_sop_external_task_signal(db, signal)
        assert crashed is True, "原SOP恢复尚未尝试写入最终聊天回复"
        db.refresh(instance)
        assert instance.status != "succeeded"
        assert _deliveries(db, instance) == []
        db.refresh(signal)
        assert signal.status == "pending"
        assert signal.lease_owner is None
        assert signal.lease_expires_at is None
        assert signal.available_at > SopExecutionStore(db).database_now()
        assert all(item.id != signal.id for item in due_sop_external_task_signals(db))
        monkeypatch.setattr(db, "add", original_add)
        process_sop_external_task_signal(db, db.get(ExecutionSignal, signal.id))
        db.refresh(signal)
        assert signal.status == "pending"
        assert signal.attempt_count == 1
        assert _deliveries(db, instance) == []
        _wait_for_original_signal_due(db, signal)
        eligible = [item for item in due_sop_external_task_signals(db) if item.id == signal.id]
        assert len(eligible) == 1
        process_sop_external_task_signal(db, eligible[0])
        db.refresh(instance)
        assert instance.status == "succeeded"
        assert len(_deliveries(db, instance)) == 1


def _audit_only_result() -> dict[str, object]:
    """构造合成可显示回执与仅审计侧带，不包含任何真实用户资料或凭据。"""

    return {
        "receipt_id": "RCPT-AA83", "provider_trace": "SYNTHETIC-AUDIT-MARKER",
        "business": {"object_id": "AA83", "units": 7, "warehouse": "南区", "registered": True},
    }


def _assert_delivery_has_no_audit_data(
    db: Session, instance: SopInstance, operation: SopOperation, *, receipt_visible: bool,
) -> None:
    """交叉核原完整审计保留及Message、交付事件、会话摘要的合法用户schema边界。"""

    db.refresh(instance)
    db.refresh(operation)
    assert instance.status == "succeeded"
    assert operation.status == "succeeded"
    assert operation.result_json["data"] == _audit_only_result()
    assert operation.external_receipt_json["result"] == _audit_only_result()
    inbox = db.exec(select(ExternalTaskEventInbox).where(
        ExternalTaskEventInbox.operation_id == operation.id,
        ExternalTaskEventInbox.provider_event_id == "f3-unit-completed",
    )).one()
    assert inbox.payload_json["result"] == _audit_only_result()
    delivered = _deliveries(db, instance)
    assert len(delivered) == 1
    message = delivered[0]
    assert ("RCPT-AA83" in message.content) is receipt_visible
    assert "SYNTHETIC-AUDIT-MARKER" not in message.content
    assert "provider_trace" not in message.content
    assert "南区" not in message.content
    assert "SYNTHETIC-AUDIT-MARKER" not in json.dumps(message.metadata_json)
    session = db.get(ChatSession, instance.session_id)
    assert "SYNTHETIC-AUDIT-MARKER" not in str(session.summary or "")
    delivered_events = [event for event in db.exec(select(AgentEvent).where(
        AgentEvent.tenant_id == instance.tenant_id, AgentEvent.session_id == instance.session_id,
    )).all() if event.payload_json.get("assistant_message_id") == message.id]
    assert len(delivered_events) >= 1
    assert all("SYNTHETIC-AUDIT-MARKER" not in json.dumps(event.payload_json)
               for event in delivered_events)


def test_external_delivery_obeys_frozen_user_schema_and_preserves_full_audit(delivery_engine):
    """callback真实回执仅披露冻结receipt_id，audit_only原文仍留在原持久账本。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(
            db, user_output_paths=("receipt_id",), provider_result=_audit_only_result(),
        )
        assert operation.capability_snapshot_json["user_view"]["output_schema"]["properties"] == {
            "receipt_id": {"type": "string"},
        }
        process_sop_external_task_signal(db, signal)
        _assert_delivery_has_no_audit_data(db, instance, operation, receipt_visible=True)


def test_current_tool_widening_cannot_expand_frozen_external_delivery(delivery_engine):
    """当前工具后继放宽用户view也不能把旧Operation的audit_only侧带重新披露。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(
            db, user_output_paths=("receipt_id",), provider_result=_audit_only_result(),
        )
        old_checksum = operation.capability_checksum
        old_snapshot = json.dumps(operation.capability_snapshot_json, sort_keys=True)
        tool = db.get(Tool, "f3-delivery-tool")
        current = dict(tool.reliability_contract_json)
        current["model_visibility"] = {
            "allowed_paths": ["output.receipt_id", "output.business", "output.provider_trace"],
            "user_display_paths": ["output.receipt_id", "output.provider_trace"],
            "audit_only_paths": [],
        }
        publish_tool_contract(tool, ToolReliabilityContract.model_validate(current))
        db.add(tool)
        db.commit()
        successor = published_tool_snapshot(tool, "f3-agent")
        assert "provider_trace" in successor.user_view["output_schema"]["properties"]
        assert successor.checksum != old_checksum
        process_sop_external_task_signal(db, signal)
        assert operation.capability_checksum == old_checksum
        assert json.dumps(operation.capability_snapshot_json, sort_keys=True) == old_snapshot
        _assert_delivery_has_no_audit_data(db, instance, operation, receipt_visible=True)


def test_missing_frozen_user_output_schema_never_falls_back_to_raw_receipt(delivery_engine):
    """旧冻结view缺schema时保留完成和完整审计，用户消息不能fallback原始私密结果。"""

    with Session(delivery_engine) as db:
        instance, operation, signal, _time_context = _ready_signal(
            db, user_output_paths=("receipt_id",), provider_result=_audit_only_result(),
            missing_user_schema=True,
        )
        assert "output_schema" not in operation.capability_snapshot_json["user_view"]
        process_sop_external_task_signal(db, signal)
        _assert_delivery_has_no_audit_data(db, instance, operation, receipt_visible=False)
