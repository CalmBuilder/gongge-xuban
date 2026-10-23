"""
@Time       : 2026/09/28 16:00
@Author     : zhanglp8181
@File       : test_external_async_tools.py
@CallChain  : pytest → provider_task 合同/Operation → 外部回执与恢复账本
@Description: 先以失败行为测试锁定 SD-4 目标1 的受理、等待和不重复派发契约。
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    ExecutionSignal,
    ExternalTaskEventInbox,
    ModelConfig,
    SkillVersion,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.api.external_tasks import router as external_tasks_router
from app.db import get_session
from app.dynamic_tasks.agent import DynamicRunOutcome, DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, publish_tool_contract
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def _session() -> Session:
    """创建隔离 SQLite 会话，确保行为测试不触碰应用数据库文件。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _external_callback_http_app(engine) -> FastAPI:
    """构造仅替换数据库依赖的真实callback HTTP应用，保留原路由和认证边界。"""
    app = FastAPI()
    app.include_router(external_tasks_router)

    def owned_session() -> Iterator[Session]:
        """让每次HTTP回调使用独立事务，避免测试直接共享调用方Session。"""
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = owned_session
    return app


def _provider_contract() -> ToolReliabilityContract:
    """构造明确声明 provider_task 跟踪字段的最小外部写合同。"""

    return ToolReliabilityContract.model_validate(
        {
            "risk_class": "external_write",
            "side_effect": "external",
            "confirmation_policy": "once",
            "timeout_policy": "unknown",
            "idempotency": {
                "mode": "request_key",
                "remote_scope": "sd4-fixture",
            },
            "execution_mode": "detached",
            "async_strategy": "provider_task",
            "status_url": "https://provider.example/tasks/{taskId}",
            "poll_interval_seconds": 1,
            "task_id_field": "taskId",
            "status_field": "status",
            "result_field": "result",
            "max_tracking_seconds": 300,
        }
    )


def _started_operation(db: Session) -> tuple[SopExecutionStore, SopInstance, SopNodeExecution, SopOperation]:
    """创建持有 execution lease 且已进入 running 的外部写 Operation。"""

    store = SopExecutionStore(db)
    instance, created = store.start_instance(
        tenant_id="tenant_sd4",
        session_id="session_sd4",
        skill_id="skill_sd4",
        skill_version_id="skillver_sd4",
        skill_version="1.0.0",
        definition_checksum="a" * 64,
        start_node_id="submit",
    )
    assert created is True
    lease = store.owned(instance, worker_id="sd4-test")
    lease.__enter__()
    execution = store.enter_node(instance, "submit", input_snapshot={})
    operation, created = store.prepare_operation(
        instance,
        execution,
        operation_name="orders.submit",
        request={"order_id": "A-1"},
        effect_kind="external_write",
    )
    assert created is True
    store.start_operation(operation)
    db.commit()
    lease.__exit__(None, None, None)
    return store, instance, execution, operation


def _configure_sop_recovery_definition(db: Session, instance: SopInstance) -> None:
    """为真实 SOP 回执恢复补齐可编译版本、会话和工具结果字段。"""

    content = {
        "skill_id": instance.skill_id,
        "name": "外部任务恢复验证",
        "version": instance.skill_version,
        "execution_mode": "deterministic",
        "nodes": [
            {
                "node_id": "submit",
                "type": "tool_call",
                "name": "提交外部任务",
                "allowed_actions": ["call_tool:orders.submit"],
                "metadata": {"operation_result_key": "external_result"},
            },
            {"node_id": "done", "type": "terminal", "name": "完成"},
        ],
        "edges": [{"source_node_id": "submit", "next_node_id": "done"}],
        "start_node_id": "submit",
        "terminal_node_ids": ["done"],
    }
    definition = compile_legacy_skill_card(content)
    instance.definition_checksum = definition.checksum
    db.add(
        SkillVersion(
            id=instance.skill_version_id,
            tenant_id=instance.tenant_id,
            skill_id=instance.skill_id,
            version=instance.skill_version,
            name="外部任务恢复验证",
            content_json=content,
            status="published",
            compiled_definition_checksum=definition.checksum,
        )
    )
    db.add(
        ChatSession(
            id=instance.session_id,
            tenant_id=instance.tenant_id,
            active_skill_id=instance.skill_id,
            active_step_id=instance.current_node_id,
        )
    )
    db.add(instance)
    db.flush()


def _started_dynamic_operation(
    db: Session,
) -> tuple[SopExecutionStore, SopInstance, SopNodeExecution, SopOperation]:
    """创建绑定真实动态计划步骤的外部任务 Operation。"""

    db.add(User(id="dta-user", tenant_id="tenant_sd4", username="dta", password_hash="x"))
    db.add(
        AgentProfile(
            id="dta-agent",
            tenant_id="tenant_sd4",
            name="DTA 外部任务验证 Agent",
            owner_user_id="dta-user",
        )
    )
    db.flush()
    store = SopExecutionStore(db)
    plan = NormalizedPlan(
        goal="恢复外部任务",
        success_criteria=(
            SuccessCriterion(id="external_done", type="assertion", spec={"required": True}),
        ),
        steps=(PlanStep(step_key="submit", title="提交外部任务", kind="tool.write"),),
        budget={"max_steps": 3},
    )
    instance, _revision = store.start_dynamic_instance(
        tenant_id="tenant_sd4",
        session_id="session_dta",
        agent_id="dta-agent",
        initiator_user_id="dta-user",
        plan=plan,
        capability_snapshot={"tools": []},
    )
    with store.owned(instance, worker_id="sd4-dta"):
        execution = store.enter_node(
            instance,
            "submit",
            step_key="submit",
            plan_revision_id=instance.current_plan_revision_id,
            step_kind="dynamic_action",
        )
        operation, created = store.prepare_operation(
            instance,
            execution,
            operation_name="orders.submit",
            request={"order_id": "DTA-1"},
            effect_kind="external_write",
        )
        assert created is True
        store.start_operation(operation)
    db.commit()
    return store, instance, execution, operation


def test_provider_task_contract_accepts_explicit_tracking_policy() -> None:
    """provider_task 必须显式声明 detached、查询地址、字段映射和跟踪期限。"""

    contract = _provider_contract()

    assert contract.execution_mode == "detached"
    assert contract.async_strategy == "provider_task"
    assert contract.status_url.endswith("{taskId}")
    assert contract.max_tracking_seconds == 300


def test_default_async_strategy_uses_internal_worker() -> None:
    """未声明异步策略的本地工具必须使用中性的内部 worker 合同。"""

    contract = ToolReliabilityContract(
        risk_class="read",
        side_effect="none",
        confirmation_policy="none",
        timeout_policy="failed",
    )

    assert contract.async_strategy == "internal_worker"
    assert contract.execution_mode == "sync"


def test_provider_200_task_id_is_accepted_not_business_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """200/202 携带任务身份只能形成受理回执，不能被工具执行器报告为完成。"""

    class FakeClient:
        """返回固定 provider 受理回执且记录提交次数。"""

        calls = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受生产客户端参数但不连接网络。"""

        def __enter__(self) -> "FakeClient":
            """返回当前假客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """结束假客户端上下文。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """记录一次提交并返回仍在处理的远端任务。"""

            FakeClient.calls += 1
            return httpx.Response(
                202,
                json={"taskId": "provider-42", "status": "accepted"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.tools.tool_executor.httpx.Client", FakeClient)
    with _session() as db:
        db.add(Tenant(id="tenant_sd4", name="SD4"))
        tool = Tool(
            tenant_id="tenant_sd4",
            name="orders.submit",
            method="POST",
            url="https://93.184.216.34/orders",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            enabled=True,
        )
        publish_tool_contract(tool, _provider_contract())
        db.add(tool)
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_sd4",
            ToolCall(name=tool.name, arguments={"order_id": "A-1"}),
            remote_idempotency_key="sd4-operation-key",
        )

    assert FakeClient.calls == 1
    assert result.success is False
    assert result.disposition == "accepted"
    assert result.data["provider_task_id"] == "provider-42"


def test_waiting_external_preserves_operation_and_attempt_without_new_dispatch() -> None:
    """受理后 Operation/Attempt 同步等待，重新检查不得创建第二次 dispatch。"""

    with _session() as db:
        store, instance, execution, operation = _started_operation(db)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        db.refresh(operation)
        db.refresh(instance)
        db.refresh(execution)
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == operation.id)
        ).all()

        assert operation.status == "waiting_external"
        assert operation.external_task_id == "provider-42"
        assert execution.status == "waiting"
        assert instance.status == "waiting"
        assert len(attempts) == 1
        assert attempts[0].status == "waiting_external"

        with store.owned(instance, worker_id="sd4-test"):
            repeated, created = store.prepare_operation(
                instance,
                execution,
                operation_name="orders.submit",
                request={"order_id": "A-1"},
                effect_kind="external_write",
            )
        db.commit()

        operation_id = operation.id
        repeated_id = repeated.id
        repeated_status = repeated.status
        repeated_task_id = repeated.external_task_id

    assert repeated_id == operation_id
    assert created is False
    assert repeated_status == "waiting_external"
    assert repeated_task_id == "provider-42"


def test_provider_contract_rejects_tracking_without_status_url() -> None:
    """provider_task 缺少固定查询地址时必须在发布前失败。"""

    with pytest.raises(ValueError, match="status_url"):
        ToolReliabilityContract.model_validate(
            {
                "risk_class": "external_write",
                "side_effect": "external",
                "confirmation_policy": "once",
                "timeout_policy": "unknown",
                "idempotency": {"mode": "request_key", "remote_scope": "sd4-fixture"},
                "execution_mode": "detached",
                "async_strategy": "provider_task",
            }
        )


def test_invalid_provider_task_contract_cannot_fall_back_to_sync_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无效 provider_task 合同必须在派发前拒绝，不能误走同步 HTTP 分支。"""

    class FakeClient:
        """记录不应发生的同步派发。"""

        calls = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受生产客户端参数但不连接网络。"""

        def __enter__(self) -> "FakeClient":
            """返回假客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """结束假客户端上下文。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """记录错误分支尝试的请求。"""

            FakeClient.calls += 1
            return httpx.Response(
                200,
                json={"unexpected": True},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.tools.tool_executor.httpx.Client", FakeClient)
    with _session() as db:
        db.add(Tenant(id="tenant_sd4", name="SD4"))
        tool = Tool(
            tenant_id="tenant_sd4",
            name="orders.submit",
            method="POST",
            url="https://93.184.216.34/orders",
            enabled=True,
            reliability_contract_json={
                "risk_class": "external_write",
                "side_effect": "external",
                "confirmation_policy": "once",
                "timeout_policy": "unknown",
                "idempotency": {"mode": "request_key", "remote_scope": "sd4-fixture"},
                "execution_mode": "detached",
                "async_strategy": "provider_task",
            },
        )
        db.add(tool)
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_sd4",
            ToolCall(name=tool.name, arguments={"order_id": "A-1"}),
            remote_idempotency_key="sd4-operation-key",
        )

    assert FakeClient.calls == 0
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "PROVIDER_CONTRACT_INVALID"


def test_provider_failure_is_not_proof_that_external_effect_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider failed/cancelled 只形成对账要求，不能直接清除外部效果不确定性。"""

    class FakeClient:
        """返回 provider 非成功终态。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受生产客户端参数但不连接网络。"""

        def __enter__(self) -> "FakeClient":
            """返回假客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """结束假客户端上下文。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """返回带任务身份的失败观察。"""

            return httpx.Response(
                200,
                json={"taskId": "provider-42", "status": "failed"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.tools.tool_executor.httpx.Client", FakeClient)
    with _session() as db:
        db.add(Tenant(id="tenant_sd4", name="SD4"))
        tool = Tool(
            tenant_id="tenant_sd4",
            name="orders.submit",
            method="POST",
            url="https://93.184.216.34/orders",
            enabled=True,
        )
        publish_tool_contract(tool, _provider_contract())
        db.add(tool)
        db.commit()
        result = ToolExecutor(db).execute(
            "tenant_sd4",
            ToolCall(name=tool.name, arguments={"order_id": "A-1"}),
            remote_idempotency_key="sd4-operation-key",
        )

    assert result.success is False
    assert result.disposition == "unknown"
    assert result.error is not None
    assert result.error.code == "PROVIDER_OUTCOME_REQUIRES_RECONCILIATION"


def test_accepted_provider_operation_is_polled_without_post_or_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启扫描只查询同一远端任务，不能为轮询创建新的本地派发。"""

    get_calls: list[str] = []
    poll_transaction_states: list[bool] = []
    session_holder: list[Session] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        """记录查询地址并返回仍在处理的观察。"""

        get_calls.append(url)
        poll_transaction_states.append(session_holder[0].in_transaction())
        return httpx.Response(
            200,
            json={"status": "working"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.sop_runtime.external_tasks.httpx.get", fake_get)
    with _session() as db:
        session_holder.append(db)
        store, instance, execution, operation = _started_operation(db)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
                poll_interval_seconds=1,
            )
        operation.external_next_poll_at = utc_now() - timedelta(seconds=1)
        db.add(operation)
        db.commit()

        from app.sop_runtime.external_tasks import poll_due_external_operations

        polled = poll_due_external_operations(db)
        db.refresh(operation)
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == operation.id)
        ).all()

    assert polled == 1
    assert get_calls == ["https://93.184.216.34/tasks/provider-42"]
    assert poll_transaction_states == [False]
    assert operation.status == "waiting_external"
    assert operation.external_task_id == "provider-42"
    assert operation.external_status == "working"
    assert len(attempts) == 1
    assert attempts[0].status == "waiting_external"


def test_completed_poll_uses_inbox_and_enqueues_one_recovery_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮询到终态也必须进入同一 inbox，并只排队一个恢复 Signal。"""

    get_calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        """返回一次完成观察并记录只读查询。"""

        get_calls.append(url)
        return httpx.Response(
            200,
            json={"status": "completed", "result": {"remote": "done"}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("app.sop_runtime.external_tasks.httpx.get", fake_get)
    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-terminal",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
                poll_interval_seconds=1,
            )
        operation.external_next_poll_at = utc_now() - timedelta(seconds=1)
        db.add(operation)
        db.commit()

        from app.sop_runtime.external_tasks import poll_due_external_operations

        assert poll_due_external_operations(db) == 1
        signals = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).all()
        inboxes = db.exec(
            select(ExternalTaskEventInbox).where(
                ExternalTaskEventInbox.operation_id == operation.id,
            )
        ).all()
        db.refresh(operation)

    assert get_calls == ["https://93.184.216.34/tasks/provider-terminal"]
    assert operation.status == "succeeded"
    assert operation.result_json["data"] == {"remote": "done"}
    assert len(inboxes) == 1
    assert inboxes[0].status == "applied"
    assert len(signals) == 1


def test_waiting_external_is_not_converted_to_unknown_by_sync_timeout() -> None:
    """正常外部等待即使超过同步调用时间，也不能被 running 超时扫描误判。"""

    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://provider.example/tasks/{taskId}",
                tracking_seconds=300,
            )
        operation.started_at = utc_now() - timedelta(hours=1)
        db.add(operation)
        db.commit()
        changed = store.mark_stale_running_operation_unknown(
            operation,
            timeout_seconds=1,
        )

    assert changed is False
    assert operation.status == "waiting_external"


def test_external_callback_is_trusted_deduplicated_and_cas_ordered() -> None:
    """回执须精确绑定 Operation，重复不重复唤醒，乱序不能覆盖较新观察。"""

    from app.sop_runtime.external_tasks import (
        ExternalTaskEventConflict,
        apply_pending_external_task_events,
        ingest_external_task_event,
    )

    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        operation.external_submission_id = "submission-42"
        operation.external_callback_token_hash = hashlib.sha256(b"callback-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()

        newer = ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-42",
            provider_event_id="provider-event-2",
            provider_status="completed",
            observation_revision=2,
            payload={"taskId": "provider-42", "status": "completed", "result": {"ok": True}},
            callback_token="callback-secret",
        )
        duplicate = ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-42",
            provider_event_id="provider-event-2",
            provider_status="completed",
            observation_revision=2,
            payload={"taskId": "provider-42", "status": "completed", "result": {"ok": True}},
            callback_token="callback-secret",
        )
        assert newer.id == duplicate.id
        assert duplicate.status == "pending"

        with pytest.raises(ExternalTaskEventConflict):
            ingest_external_task_event(
                db,
                tenant_id=instance.tenant_id,
                operation_id=operation.id,
                provider_task_id="provider-42",
                provider_event_id="provider-event-2",
                provider_status="failed",
                observation_revision=2,
                payload={"taskId": "provider-42", "status": "failed"},
                callback_token="callback-secret",
            )

        applied = apply_pending_external_task_events(db)
        db.refresh(operation)
        inbox = db.get(ExternalTaskEventInbox, newer.id)
        signals = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).all()

        assert applied == 1
        assert inbox is not None and inbox.status == "applied"
        assert operation.status == "succeeded"
        assert operation.result_json["data"]["ok"] is True
        assert len(signals) == 1

        older = ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-42",
            provider_event_id="provider-event-1",
            provider_status="working",
            observation_revision=2,
            payload={"taskId": "provider-42", "status": "working"},
            callback_token="callback-secret",
        )
        assert older.status == "pending"
        assert apply_pending_external_task_events(db) == 1
        db.refresh(operation)
        assert operation.status == "succeeded"
        assert len(
            db.exec(
                select(ExternalTaskEventInbox).where(
                    ExternalTaskEventInbox.operation_id == operation.id,
                    ExternalTaskEventInbox.status == "ignored",
                )
            ).all()
        ) == 1


def test_external_callback_http_route_binds_token_and_deduplicates() -> None:
    """真实FastAPI callback入口验证token、Operation/task绑定和重复事件不重复入箱。"""
    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        operation_id = operation.id
        operation.external_submission_id = "submission-http-42"
        with store.owned(instance, worker_id="sd4-http"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-http-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
                callback_token="callback-http-secret",
            )
        db.commit()
        engine = db.get_bind()
        assert engine is not None
        payload = {
            "provider_task_id": "provider-http-42",
            "provider_event_id": "provider-http-event-1",
            "provider_status": "completed",
            "observation_revision": 2,
            "payload": {"result": {"ok": True}},
            "external_submission_id": "submission-http-42",
        }
        with TestClient(_external_callback_http_app(engine)) as client:
            rejected = client.post(
                f"/api/external-tasks/{operation_id}/callback",
                json=payload,
                headers={"X-Gongge-Xuban-External-Task-Token": "wrong-http-secret"},
            )
            assert rejected.status_code == 403
            accepted = client.post(
                f"/api/external-tasks/{operation_id}/callback",
                json=payload,
                headers={"X-Gongge-Xuban-External-Task-Token": "callback-http-secret"},
            )
            assert accepted.status_code == 202
            duplicate = client.post(
                f"/api/external-tasks/{operation_id}/callback",
                json=payload,
                headers={"X-Gongge-Xuban-External-Task-Token": "callback-http-secret"},
            )
            assert duplicate.status_code == 202
        db.refresh(operation)
        assert operation.status == "succeeded"
        assert len(db.exec(select(ExternalTaskEventInbox)).all()) == 1
        assert len(db.exec(select(ExecutionSignal)).all()) == 1


def test_external_callback_rejects_cross_tenant_or_wrong_task_binding() -> None:
    """回执不能借用其他租户或其他 provider task 的 Operation 身份。"""

    from app.sop_runtime.external_tasks import ExternalTaskEventRejected, ingest_external_task_event

    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        operation.external_submission_id = "submission-42"
        operation.external_callback_token_hash = hashlib.sha256(b"callback-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-42",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()

        with pytest.raises(ExternalTaskEventRejected):
            ingest_external_task_event(
                db,
                tenant_id="other-tenant",
                operation_id=operation.id,
                provider_task_id="provider-42",
                provider_event_id="cross-tenant",
                provider_status="completed",
                observation_revision=2,
                payload={"status": "completed"},
                callback_token="callback-secret",
            )
        with pytest.raises(ExternalTaskEventRejected):
            ingest_external_task_event(
                db,
                tenant_id=instance.tenant_id,
                operation_id=operation.id,
                provider_task_id="provider-other",
                provider_event_id="wrong-task",
                provider_status="completed",
                observation_revision=2,
                payload={"status": "completed"},
                callback_token="callback-secret",
            )


def test_early_external_callback_remains_available_after_acceptance_commit() -> None:
    """provider 早到回执先留在 inbox，Operation 受理提交后仍可被同一身份应用。"""

    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
    )

    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        operation.external_submission_id = "submission-early"
        operation.external_callback_token_hash = hashlib.sha256(b"callback-secret").hexdigest()
        db.add(operation)
        db.commit()

        early = ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-early",
            provider_event_id="provider-early-event",
            provider_status="completed",
            observation_revision=2,
            payload={"taskId": "provider-early", "status": "completed", "result": {"early": True}},
            callback_token="callback-secret",
        )
        db.commit()
        assert early.status == "pending"

        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-early",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        early_inbox = db.get(ExternalTaskEventInbox, early.id)
        assert early_inbox is not None
        early_inbox.available_at = utc_now() - timedelta(seconds=1)
        db.add(early_inbox)
        db.commit()

        assert apply_pending_external_task_events(db) == 1
        db.refresh(operation)
        assert operation.status == "succeeded"
        assert operation.external_task_id == "provider-early"


def test_sop_external_signal_resumes_waiting_node_without_redispatch() -> None:
    """SOP 消费者必须按原节点恢复，并把结果写回会话而非重新调用工具。"""

    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
        process_sop_external_task_signal,
    )

    with _session() as db:
        store, instance, execution, operation = _started_operation(db)
        _configure_sop_recovery_definition(db, instance)
        operation.external_submission_id = "sop-submission"
        operation.external_callback_token_hash = hashlib.sha256(b"sop-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-sop",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-sop",
            provider_event_id="sop-complete-1",
            provider_status="completed",
            observation_revision=2,
            payload={"taskId": "provider-sop", "status": "completed", "result": {"ok": True}},
            callback_token="sop-secret",
        )
        assert apply_pending_external_task_events(db) == 1
        signals = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).all()
        assert signals, db.exec(
            select(ExternalTaskEventInbox).where(
                ExternalTaskEventInbox.operation_id == operation.id,
            )
        ).all()
        signal = signals[0]

        plan = process_sop_external_task_signal(db, signal)
        db.refresh(instance)
        db.refresh(execution)
        db.refresh(operation)
        db.refresh(signal)
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == operation.id)
        ).all()

    assert plan is not None, signal.last_error_json
    assert instance.status == "succeeded"
    assert execution.status == "succeeded"
    assert operation.status == "succeeded"
    assert instance.context_json["tool_results"]["external_result"]["data"]["data"] == {"ok": True}
    assert signal.status == "consumed"
    assert len(attempts) == 1


def test_scheduled_worker_routes_sop_external_signal_to_sop_consumer() -> None:
    """正式后台 worker 必须把 SOP 外部回执交给 SOP 消费者而非动态分支吞掉。"""

    from app.scheduled_tasks.worker import _process_due_dynamic_signals
    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
    )

    with _session() as db:
        store, instance, execution, operation = _started_operation(db)
        _configure_sop_recovery_definition(db, instance)
        operation.external_submission_id = "sop-worker-submission"
        operation.external_callback_token_hash = hashlib.sha256(b"sop-worker-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-sop-worker",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-sop-worker",
            provider_event_id="sop-worker-complete-1",
            provider_status="completed",
            observation_revision=2,
            payload={"result": {"ok": True}},
            callback_token="sop-worker-secret",
        )
        assert apply_pending_external_task_events(db) == 1
        db.commit()

        assert _process_due_dynamic_signals(db, once=True) == 1
        db.refresh(instance)
        db.refresh(execution)
        db.refresh(operation)

    assert instance.status == "succeeded"
    assert execution.status == "succeeded"
    assert operation.status == "succeeded"


def test_scheduled_worker_polls_external_tasks_before_signal_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """常驻调度循环必须先查询到期 provider task，再消费两分支 Signal。"""

    from app.scheduled_tasks import worker

    calls: list[object] = []
    monkeypatch.setattr(
        worker,
        "poll_due_external_operations",
        lambda _db: calls.append("poll") or 2,
    )
    monkeypatch.setattr(
        worker,
        "_process_due_dynamic_signals",
        lambda _db, once: calls.append(("signals", once)) or 1,
    )

    with _session() as db:
        assert worker._process_external_task_maintenance(db, once=True) == 3

    assert calls == ["poll", ("signals", True)]


def test_dta_external_signal_resumes_waiting_step_without_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DTA 消费者必须恢复冻结计划步骤并消费 Signal，不创建第二次派发。"""

    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
    )

    with _session() as db:
        store, instance, execution, operation = _started_dynamic_operation(db)
        operation.external_submission_id = "dta-submission"
        operation.external_callback_token_hash = hashlib.sha256(b"dta-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-dta"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-dta",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-dta",
            provider_event_id="dta-complete-1",
            provider_status="completed",
            observation_revision=2,
            payload={"taskId": "provider-dta", "status": "completed", "result": {"ok": True}},
            callback_token="dta-secret",
        )
        assert apply_pending_external_task_events(db) == 1
        signals = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).all()
        assert signals, db.exec(
            select(ExternalTaskEventInbox).where(
                ExternalTaskEventInbox.operation_id == operation.id,
            )
        ).all()
        signal = signals[0]
        agent = DynamicTaskAgent(db)

        def fake_run_until_blocked_or_complete(**kwargs: object) -> DynamicRunOutcome:
            """模拟恢复后计划继续完成，避免本测试引入模型或规划调用。"""

            with agent.store.owned(instance, worker_id="sd4-dta-finish"):
                from app.sop_runtime.execution_control import ExecutionControlService

                ExecutionControlService(agent.db, agent.store).freeze_result(
                    instance,
                    result={"summary": "external task completed"},
                    verification={"passed": True, "source": "sd4-test"},
                    application_publication_status="settled",
                )
                agent.store.complete_instance(instance, slots=instance.slots_json)
            return DynamicRunOutcome("succeeded", instance.id)

        monkeypatch.setattr(agent, "run_until_blocked_or_complete", fake_run_until_blocked_or_complete)
        outcome = agent.resume_external_task_signal(
            signal_id=signal.id,
            model_config=ModelConfig(
                id="model-sd4",
                tenant_id=instance.tenant_id,
                name="SD4 controlled model",
                api_key_encrypted="x",
                model="controlled",
            ),
            worker_id="sd4-dta-resume",
        )
        db.refresh(instance)
        db.refresh(execution)
        db.refresh(operation)
        db.refresh(signal)
        attempts = db.exec(
            select(SopOperationAttempt).where(SopOperationAttempt.operation_id == operation.id)
        ).all()

    assert outcome.status == "succeeded"
    assert instance.status == "succeeded"
    assert execution.status == "succeeded"
    assert operation.status == "succeeded"
    assert signal.status == "consumed"
    assert len(attempts) == 1


def test_cancelled_external_wait_ignores_late_callback_without_revival() -> None:
    """撤销中的外部等待收到迟到回执时只保留 inbox 事实，不复活业务推进。"""

    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
    )

    with _session() as db:
        store, instance, _execution, operation = _started_operation(db)
        operation.external_callback_token_hash = hashlib.sha256(b"cancel-secret").hexdigest()
        db.add(operation)
        with store.owned(instance, worker_id="sd4-test"):
            store.mark_operation_waiting_external(
                operation,
                provider_task_id="provider-cancelled",
                provider_status="accepted",
                receipt={"status_code": 202},
                status_url="https://93.184.216.34/tasks/{taskId}",
                tracking_seconds=300,
            )
        db.commit()
        with store.owned(instance, worker_id="sd4-cancel"):
            settled = store.request_cancellation(
                instance,
                actor_user_id="user-cancel",
                reason="测试撤销外部等待",
            )
        db.commit()
        assert settled is False
        db.refresh(operation)
        assert operation.status == "unknown"

        late = ingest_external_task_event(
            db,
            tenant_id=instance.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-cancelled",
            provider_event_id="late-cancelled-result",
            provider_status="completed",
            observation_revision=2,
            payload={"status": "completed", "result": {"late": True}},
            callback_token="cancel-secret",
        )
        assert apply_pending_external_task_events(db) == 1
        db.refresh(operation)
        inbox = db.get(ExternalTaskEventInbox, late.id)
        signals = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == instance.id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).all()

    assert inbox is not None and inbox.status == "ignored"
    assert inbox.last_error_json["code"] == "EXTERNAL_OPERATION_ALREADY_TERMINAL"
    assert operation.status == "unknown"
    assert signals == []


def test_tool_test_provider_task_recovers_through_no_model_dynamic_signal_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具测试 provider_task 通过原 Signal worker 恢复，但不要求 ModelConfig。"""

    from app.dynamic_tasks.worker import process_dynamic_task_signal
    from app.sop_runtime.external_identity import external_task_callback_token
    from app.sop_runtime.external_tasks import (
        apply_pending_external_task_events,
        ingest_external_task_event,
    )
    from app.tools.tool_test_adapter import ToolTestExecutionAdapter

    class FakeClient:
        """返回固定受理回执并记录提交次数。"""

        calls = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受 provider 客户端参数但不连接网络。"""

        def __enter__(self) -> "FakeClient":
            """返回受控客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """结束受控客户端上下文。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """返回带 task 身份的异步受理结果。"""

            FakeClient.calls += 1
            return httpx.Response(
                202,
                json={"taskId": "provider-tool-test", "status": "accepted"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.tools.tool_executor.httpx.Client", FakeClient)
    with _session() as db:
        db.add(Tenant(id="tenant_tool_async", name="异步工具测试租户"))
        db.add(
            User(
                id="tool-async-user",
                tenant_id="tenant_tool_async",
                username="tool-async-user",
                password_hash="x",
            )
        )
        db.add(
            AgentProfile(
                id="tool-async-agent",
                tenant_id="tenant_tool_async",
                name="异步工具测试员工",
                owner_user_id="tool-async-user",
                is_overall=True,
            )
        )
        tool = Tool(
            tenant_id="tenant_tool_async",
            name="orders.async_test",
            method="POST",
            url="https://93.184.216.34/orders/async",
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            enabled=True,
        )
        publish_tool_contract(tool, _provider_contract())
        db.add(tool)
        db.flush()
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_tool_async",
                agent_id="tool-async-agent",
                resource_type="tool",
                resource_id=tool.id,
            )
        )
        db.commit()

        adapter = ToolTestExecutionAdapter(db)
        accepted = adapter.execute(
            tenant_id="tenant_tool_async",
            tool=tool,
            arguments={"order_id": "ASYNC-1"},
            actor_user_id="tool-async-user",
            agent_id="tool-async-agent",
            submission_id="tool-async-submit-1",
        )
        operation = db.exec(
            select(SopOperation).where(SopOperation.operation_name == tool.name)
        ).one()
        assert accepted.disposition == "waiting"
        assert operation.external_task_id == "provider-tool-test"
        ingest_external_task_event(
            db,
            tenant_id=operation.tenant_id,
            operation_id=operation.id,
            provider_task_id="provider-tool-test",
            provider_event_id="tool-async-complete-1",
            provider_status="completed",
            observation_revision=2,
            payload={"status": "completed", "result": {"ok": True}},
            callback_token=external_task_callback_token(operation.remote_idempotency_key or ""),
        )
        assert apply_pending_external_task_events(db) == 1
        signal = db.exec(
            select(ExecutionSignal).where(
                ExecutionSignal.execution_id == operation.instance_id,
                ExecutionSignal.signal_type == "external_event",
            )
        ).one()
        outcome = process_dynamic_task_signal(db, signal)
        db.refresh(operation)
        instance = db.get(SopInstance, operation.instance_id)
        db.refresh(signal)

    assert FakeClient.calls == 1
    assert outcome is not None and outcome.status == "succeeded"
    assert instance is not None and instance.status == "succeeded"
    assert operation.status == "succeeded"
    assert signal.status == "consumed"


def test_tool_test_adapter_is_single_action_idempotent_and_publishes_verified_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具测试必须独立占槽、无模型执行一次，并以已核验结果发布。"""

    from app.tools.tool_test_adapter import ToolTestExecutionAdapter

    class FakeClient:
        """返回固定合法结果并记录真实 HTTP 派发次数。"""

        calls = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            """接受生产 HTTP 客户端参数但不建立外网连接。"""

        def __enter__(self) -> "FakeClient":
            """返回当前受控客户端。"""

            return self

        def __exit__(self, *args: object) -> None:
            """结束受控客户端上下文。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """返回符合工具 output schema 的结果。"""

            FakeClient.calls += 1
            return httpx.Response(
                200,
                json={"ok": True, "source": "tool-test"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.tools.tool_executor.httpx.Client", FakeClient)
    with _session() as db:
        db.add(Tenant(id="tenant_tool_test", name="工具测试租户"))
        db.add(
            User(
                id="tool-test-user",
                tenant_id="tenant_tool_test",
                username="tool-test-user",
                password_hash="x",
            )
        )
        db.add(
            AgentProfile(
                id="tool-test-agent",
                tenant_id="tenant_tool_test",
                name="工具测试员工",
                owner_user_id="tool-test-user",
                is_overall=True,
            )
        )
        tool = Tool(
            tenant_id="tenant_tool_test",
            name="orders.test_query",
            method="GET",
            url="https://93.184.216.34/orders/test",
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            enabled=True,
        )
        db.add(tool)
        db.flush()
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_tool_test",
                agent_id="tool-test-agent",
                resource_type="tool",
                resource_id=tool.id,
            )
        )
        db.commit()

        adapter = ToolTestExecutionAdapter(db)
        first = adapter.execute(
            tenant_id="tenant_tool_test",
            tool=tool,
            arguments={"order_id": "T-1"},
            actor_user_id="tool-test-user",
            agent_id="tool-test-agent",
            submission_id="tool-submit-1",
        )
        second = adapter.execute(
            tenant_id="tenant_tool_test",
            tool=tool,
            arguments={"order_id": "T-1"},
            actor_user_id="tool-test-user",
            agent_id="tool-test-agent",
            submission_id="tool-submit-1",
        )
        conflict = adapter.execute(
            tenant_id="tenant_tool_test",
            tool=tool,
            arguments={"order_id": "T-2"},
            actor_user_id="tool-test-user",
            agent_id="tool-test-agent",
            submission_id="tool-submit-1",
        )
        independent = adapter.execute(
            tenant_id="tenant_tool_test",
            tool=tool,
            arguments={"order_id": "T-1"},
            actor_user_id="tool-test-user",
            agent_id="tool-test-agent",
            submission_id="tool-submit-2",
        )

        executions = db.exec(
            select(SopInstance).where(SopInstance.source_kind == "tool_test")
        ).all()
        operations = db.exec(
            select(SopOperation).where(SopOperation.operation_name == tool.name)
        ).all()

    assert FakeClient.calls == 2
    assert first.success is True
    assert second.success is True
    assert first.data == {"ok": True, "source": "tool-test"}
    assert second.data == first.data
    assert conflict.error is not None and conflict.error.code == "TOOL_TEST_IDENTITY_CONFLICT"
    assert independent.success is True
    assert len(executions) == 2
    assert all(item.status == "succeeded" for item in executions)
    assert all(item.session_id.startswith("tool-test-session:") for item in executions)
    assert all(item.source_ref.startswith("tool-test:") for item in executions)
    assert len(operations) == 2
