"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_final_f1_external_resume.py
@CallChain  : 正式pytest → 原Inbox/Signal → SOP恢复 → 同一ToolExecutor后继Operation
@Description: 重现F1页面已消费回执却遗失后继派发，核同动作恢复与重复消费不重派。
"""
from __future__ import annotations

import hashlib
import pytest
from datetime import timedelta
from sqlalchemy import update
from sqlmodel import Session, SQLModel, create_engine
from app.db.models import AgentProfile, ChatSession, SopInstance, Tenant, User, utc_now
from app.dynamic_tasks.capability_catalog import publish_tool_contract
from tests.test_external_async_tools import _provider_contract

from sqlmodel import select

from app.db.models import ExecutionSignal, SkillVersion, SopOperation, Tool
from app.sop_runtime.external_tasks import (
    apply_pending_external_task_events,
    ingest_external_task_event,
    process_sop_external_task_signal,
)
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolResult
from tests.test_external_async_tools import (
    _configure_sop_recovery_definition,
    _session,
    _started_operation,
)


def test_external_signal_dispatches_next_tool_once_without_reposting_original(monkeypatch):
    """原异步回执恢复须真正执行后继工具并闭合实例，重复信号不得再派发。"""
    calls = []

    def execute(self, tenant_id, tool_call, *args, **kwargs):
        """只记录真实恢复调用边界；旧submit绝不能被当后继再次提交。"""
        calls.append((tenant_id, tool_call.name, kwargs.get("remote_idempotency_key")))
        return ToolResult(tool_name=tool_call.name, success=True, data={"registered": True})

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        _configure_sop_recovery_definition(db, instance)
        version = db.get(SkillVersion, instance.skill_version_id)
        content = dict(version.content_json)
        content["nodes"] = [content["nodes"][0], {
            "node_id": "register", "type": "tool_call", "name": "登记已完成配送",
            "allowed_actions": ["call_tool:orders.register"],
            "metadata": {"operation_result_key": "registered"},
        }, content["nodes"][-1]]
        content["edges"] = [
            {"source_node_id": "submit", "next_node_id": "register"},
            {"source_node_id": "register", "next_node_id": "done"},
        ]
        compiled = compile_legacy_skill_card(content)
        version.content_json = content
        version.compiled_definition_checksum = compiled.checksum
        instance.definition_checksum = compiled.checksum
        db.add(version)
        db.add(instance)
        db.add(Tool(tenant_id=instance.tenant_id, name="orders.register", method="POST", url="/register"))
        operation.external_callback_token_hash = hashlib.sha256(b"f1-unit-callback").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation, provider_task_id="f1-unit-provider", provider_status="accepted",
                receipt={"status_code": 202}, status_url="https://provider.example/status/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        ingest_external_task_event(
            db, tenant_id=instance.tenant_id, operation_id=operation.id,
            provider_task_id="f1-unit-provider", provider_event_id="f1-unit-complete",
            provider_status="completed", observation_revision=2,
            payload={"result": {"receipt_id": "f1-unit-receipt"}},
            callback_token="f1-unit-callback",
        )
        assert apply_pending_external_task_events(db) == 1
        signal = db.exec(select(ExecutionSignal).where(
            ExecutionSignal.execution_id == instance.id,
            ExecutionSignal.signal_type == "external_event",
        )).one()
        process_sop_external_task_signal(db, signal)
        db.refresh(instance)
        db.refresh(signal)
        assert [name for _tenant, name, _key in calls] == ["orders.register"]
        assert instance.status == "succeeded"
        assert signal.status == "consumed"
        successor = db.exec(select(SopOperation).where(
            SopOperation.instance_id == instance.id,
            SopOperation.operation_name == "orders.register",
        )).one()
        assert successor.status == "succeeded"
        assert calls[0][2] == successor.remote_idempotency_key
        process_sop_external_task_signal(db, signal)
        assert len(calls) == 1


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def resume_engine(request, tmp_path):
    """真正独立SQLite或原随机MySQL账户；MySQL必须Alembic且不触真实租户。"""
    if request.param == "mysql":
        from alembic.config import Config
        from alembic import command
        from pathlib import Path
        url = request.getfixturevalue("mysql_database_url")
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine("sqlite:///" + str(tmp_path / "resume.sqlite3"),
                               connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _chained_signal(db):
    """准备未提交业务效果的原两工具定义，再经真实Inbox创建唯一完成Signal。"""
    db.add(Tenant(id="tenant_sd4", name="F1正式隔离租户"))
    db.add(User(id="f1-unit-owner", tenant_id="tenant_sd4", username="f1-unit", password_hash="synthetic"))
    db.add(AgentProfile(id="f1-unit-agent", tenant_id="tenant_sd4", name="F1正式员工", owner_user_id="f1-unit-owner"))
    db.commit()
    store, instance, execution, operation = _started_operation(db)
    _configure_sop_recovery_definition(db, instance)
    instance.agent_id = "f1-unit-agent"
    instance.initiator_user_id = "f1-unit-owner"
    session = db.get(ChatSession, instance.session_id)
    session.agent_id = instance.agent_id
    session.user_id = instance.initiator_user_id
    db.add(session)
    version = db.get(SkillVersion, instance.skill_version_id)
    content = dict(version.content_json)
    content["nodes"] = [content["nodes"][0], {
        "node_id": "register", "type": "tool_call", "name": "登记原回执",
        "allowed_actions": ["call_tool:orders.register"],
        "metadata": {"operation_result_key": "registered"},
    }, content["nodes"][-1]]
    content["edges"] = [{"source_node_id": "submit", "next_node_id": "register"},
                        {"source_node_id": "register", "next_node_id": "done"}]
    compiled = compile_legacy_skill_card(content)
    version.content_json = content
    version.compiled_definition_checksum = compiled.checksum
    instance.definition_checksum = compiled.checksum
    db.add(version)
    db.add(instance)
    tool = Tool(id="f1-unit-register", tenant_id=instance.tenant_id, name="orders.register",
                method="POST", url="/register")
    publish_tool_contract(tool, _provider_contract())
    db.add(tool)
    operation.external_callback_token_hash = hashlib.sha256(b"f1-unit-callback").hexdigest()
    db.add(operation)
    db.flush()
    with store.owned(instance, worker_id="sd4-test"):
        store.mark_operation_waiting_external(operation, provider_task_id="f1-unit-provider",
            provider_status="accepted", receipt={"status_code": 202},
            status_url="https://provider.example/status/{taskId}", tracking_seconds=300)
    db.commit()
    ingest_external_task_event(db, tenant_id=instance.tenant_id, operation_id=operation.id,
        provider_task_id="f1-unit-provider", provider_event_id="f1-unit-complete",
        provider_status="completed", observation_revision=2,
        payload={"result": {"receipt_id": "f1-unit-receipt"}}, callback_token="f1-unit-callback")
    assert apply_pending_external_task_events(db) == 1
    signal = db.exec(select(ExecutionSignal).where(
        ExecutionSignal.execution_id == instance.id, ExecutionSignal.signal_type == "external_event")).one()
    return instance, operation, signal


@pytest.mark.parametrize("barrier", ["archived", "fenced"])
def test_committed_dispatch_rechecks_lifecycle_and_fence(resume_engine, monkeypatch, barrier):
    """另一真实Session在发送身份提交后归档或换fence，下一外呼必须为零。"""
    calls = []
    with Session(resume_engine) as db:
        instance, _operation, signal = _chained_signal(db)
        original_commit = db.commit
        switched = False

        def commit():
            """精确在原后继running与已消费Signal持久化之后提交竞争屏障。"""
            nonlocal switched
            original_commit()
            successor = db.exec(select(SopOperation).where(
                SopOperation.instance_id == instance.id, SopOperation.operation_name == "orders.register")).first()
            if switched or successor is None or successor.status != "running":
                return
            switched = True
            db.rollback()
            with Session(resume_engine) as other:
                if barrier == "archived":
                    other.exec(update(AgentProfile).where(AgentProfile.id == instance.agent_id).values(status="archived"))
                else:
                    other.exec(update(SopInstance).where(SopInstance.id == instance.id).values(
                        fencing_token=SopInstance.fencing_token + 1, lease_owner="f1-other-owner"))
                other.commit()

        def execute(self, tenant_id, tool_call, *args, **kwargs):
            """记录Runtime是否已在原ToolExecutor前拒绝失效派发。"""
            calls.append(tool_call.name)
            return ToolResult(tool_name=tool_call.name, success=True, data={"registered": True})

        monkeypatch.setattr(db, "commit", commit)
        monkeypatch.setattr(ToolExecutor, "execute", execute)
        process_sop_external_task_signal(db, signal)
        assert switched is True
        assert calls == []


@pytest.mark.parametrize("disposition", ["accepted", "unknown", "crash"])
def test_followup_wait_or_fault_preserves_original_dispatch_identity(resume_engine, monkeypatch, disposition):
    """后继再次等待、不确定或发送后异常，都保留已提交动作且禁止重复原提交。"""
    calls = []
    with Session(resume_engine) as db:
        instance, operation, signal = _chained_signal(db)
        original_id = operation.id
        signal_id = signal.id

        def execute(self, tenant_id, tool_call, *args, **kwargs):
            """在真正发送边界用另一Session核 durable 身份，再模拟远端三种结果。"""
            with Session(resume_engine) as observer:
                durable = observer.exec(select(SopOperation).where(
                    SopOperation.instance_id == instance.id, SopOperation.operation_name == "orders.register")).one()
                assert durable.status == "running"
                assert observer.get(ExecutionSignal, signal_id).status == "consumed"
                assert observer.get(SopOperation, original_id).status == "succeeded"
                assert durable.remote_idempotency_key == kwargs["remote_idempotency_key"]
                calls.append((durable.id, durable.logical_action_id, durable.remote_idempotency_key))
            if disposition == "crash":
                raise RuntimeError("F1_AFTER_SEND_CRASH")
            from app.tools.tool_schema import ToolError
            return ToolResult(tool_name=tool_call.name, success=False, disposition=disposition,
                data={"provider_task_id": "f1-next-provider", "provider_status": "working"},
                error=ToolError(code="EXTERNAL_TASK_ACCEPTED" if disposition == "accepted" else "TIMEOUT", message="合成发送结果"))

        monkeypatch.setattr(ToolExecutor, "execute", execute)
        process_sop_external_task_signal(db, signal)
        db.rollback()
        assert len(calls) == 1
        successor = db.get(SopOperation, calls[0][0])
        assert successor.logical_action_id == calls[0][1]
        assert successor.remote_idempotency_key == calls[0][2]
        assert successor.status == {"accepted": "waiting_external", "unknown": "unknown", "crash": "running"}[disposition]
        assert db.get(SopOperation, original_id).status == "succeeded"
        assert db.get(ExecutionSignal, signal_id).status == "consumed"
        process_sop_external_task_signal(db, db.get(ExecutionSignal, signal_id))
        assert len(calls) == 1
        if disposition == "crash":
            from app.sop_runtime.coordinator import DeterministicSopCoordinator
            db.exec(update(SopOperation).where(SopOperation.id == successor.id).values(
                started_at=utc_now() - timedelta(hours=1)))
            db.exec(update(SopInstance).where(SopInstance.id == instance.id).values(
                lease_expires_at=utc_now() - timedelta(hours=1)))
            db.commit()
            db.expire_all()
            coordinator = DeterministicSopCoordinator(db)
            parent = db.get(SopInstance, instance.id)
            chat = db.get(ChatSession, parent.session_id)
            current = coordinator._current_execution(parent, parent.current_node_id)
            _, definition = coordinator._definition_for_instance(parent)
            with coordinator._owned(parent):
                plan = coordinator._advance_until_external_action(chat, parent, current, definition,
                                                                  current_already_completed=False)
            db.commit()
            db.refresh(successor)
            assert successor.status == "unknown"
            assert plan.error_code == "RUNTIME_OPERATION_RECONCILIATION_REQUIRED"
            assert len(calls) == 1


def test_two_real_worker_sessions_only_dispatch_one_successor(resume_engine, monkeypatch):
    """两个独立Session同时消费同Signal，只能有一个后继派发并保留原终态。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    barrier = Barrier(2)
    lock = Lock()
    calls = []
    with Session(resume_engine) as db:
        instance, _operation, signal = _chained_signal(db)
        execution_id, signal_id = instance.id, signal.id

    def execute(self, tenant_id, tool_call, *args, **kwargs):
        """仅记录获原CAS授权的真正恢复执行边界。"""
        with lock:
            calls.append(tool_call.name)
        return ToolResult(tool_name=tool_call.name, success=True, data={"registered": True})

    def consume():
        """每个worker持独立连接并同时进入原消费者，不共享ORM对象。"""
        with Session(resume_engine) as db:
            current = db.get(ExecutionSignal, signal_id)
            barrier.wait(timeout=10)
            process_sop_external_task_signal(db, current)

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(consume), pool.submit(consume)
        first.result(timeout=30)
        second.result(timeout=30)
    with Session(resume_engine) as db:
        assert calls == ["orders.register"]
        assert db.get(SopInstance, execution_id).status == "succeeded"
        assert db.get(ExecutionSignal, signal_id).status == "consumed"
        assert len(db.exec(select(SopOperation).where(
            SopOperation.instance_id == execution_id,
            SopOperation.operation_name == "orders.register")).all()) == 1


@pytest.mark.parametrize("closure", ["cancelled", "archived"])
def test_closed_execution_does_not_dispatch_a_queued_successor(resume_engine, monkeypatch, closure):
    """回执已持久但原Signal尚未消费时关闭来源，原消费者不能提交后继写。"""
    from app.sop_runtime.execution_store import SopExecutionStore

    calls = []

    def execute(self, tenant_id, tool_call, *args, **kwargs):
        """记录关闭屏障后的外呼，任何调用都应使本正式反例失败。"""
        calls.append(tool_call.name)
        return ToolResult(tool_name=tool_call.name, success=True, data={"registered": True})

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(resume_engine) as db:
        instance, operation, signal = _chained_signal(db)
        if closure == "cancelled":
            store = SopExecutionStore(db)
            with store.owned(instance, worker_id="f1-closure"):
                store.request_cancellation(instance, actor_user_id=instance.initiator_user_id,
                                           reason="F1回执消费前本人取消")
        else:
            agent = db.get(AgentProfile, instance.agent_id)
            agent.status = "archived"
            db.add(agent)
        db.commit()
        process_sop_external_task_signal(db, signal)
        db.refresh(instance)
        db.refresh(operation)
        assert calls == []
        assert instance.status != "succeeded"
        assert operation.status == "succeeded"
        assert not db.exec(select(SopOperation).where(
            SopOperation.instance_id == instance.id,
            SopOperation.operation_name == "orders.register")).all()


def test_real_worker_continues_after_losing_signal_claim(resume_engine, monkeypatch):
    """原扫描器失去认领CAS时正常结束本轮，绝不能重试其他owner或退出扫描线程。"""
    from app.scheduled_tasks import worker
    from app.sop_runtime.execution_control import ExecutionControlService

    with Session(resume_engine) as db:
        _instance, _operation, signal = _chained_signal(db)
        signal_id = signal.id
    original_claim = ExecutionControlService.claim_signal

    def lose_claim(self, current, **kwargs):
        """另一个真实Session先赢同Signal，当前调用再走原CAS并明确失去竞争。"""
        with Session(resume_engine) as winner_db:
            winner = winner_db.get(ExecutionSignal, signal_id)
            original_claim(ExecutionControlService(winner_db), winner,
                           worker_id="f1-real-winner", ttl_seconds=300)
            winner_db.commit()
        return original_claim(self, current, **kwargs)

    monkeypatch.setattr(worker, "engine", resume_engine)
    monkeypatch.setattr(worker, "init_db", lambda: None)
    monkeypatch.setattr(worker, "seed_demo_data", lambda _db: None)
    monkeypatch.setattr(worker, "_stopped", False)
    monkeypatch.setattr(ExecutionControlService, "claim_signal", lose_claim)
    worker.run_worker(once=True)
    with Session(resume_engine) as db:
        current = db.get(ExecutionSignal, signal_id)
        assert current.status == "claimed"
        assert current.lease_owner == "f1-real-winner"
        assert current.attempt_count == 1


def test_expired_claim_is_reclaimed_by_original_scanner_and_completes(resume_engine, monkeypatch):
    """模拟旧owner死亡后租约到期，原消费者CAS接管并恢复原回执，不重新提交原动作。"""
    from app.sop_runtime.execution_control import ExecutionControlService
    from app.sop_runtime.external_tasks import due_sop_external_task_signals

    calls = []

    def execute(self, tenant_id, tool_call, *args, **kwargs):
        """只记录本轮真正后继，旧submit不允许重提。"""
        calls.append(tool_call.name)
        return ToolResult(tool_name=tool_call.name, success=True, data={"registered": True})

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(resume_engine) as db:
        instance, operation, signal = _chained_signal(db)
        ExecutionControlService(db).claim_signal(signal, worker_id="f1-dead-owner", ttl_seconds=1)
        signal.lease_expires_at = utc_now() - timedelta(hours=1)
        db.add(signal)
        db.commit()
        candidates = due_sop_external_task_signals(db)
        assert [row.id for row in candidates] == [signal.id]
        process_sop_external_task_signal(db, candidates[0])
        db.refresh(instance)
        db.refresh(signal)
        assert calls == ["orders.register"]
        assert instance.status == "succeeded"
        assert signal.status == "consumed"
        assert signal.attempt_count == 2
        assert db.get(SopOperation, operation.id).external_task_id == "f1-unit-provider"
