"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : test_final_f3_sop_reconciliation.py
@CallChain  : pytest → 原provider失败Inbox/Signal → Attention resolve API → SOP原对账消费者
@Description: 证明SOP人工对账必须结算原动作，取消不推进，保留本人来源、权限与幂等屏障。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.agents.schema import AgentProfileUpdateRequest
from app.api.agents import update_agent
from app.api.attention_items import AttentionResolveRequest, resolve_attention_item, router
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    ExecutionResult,
    ExecutionSignal,
    Message,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopOperationAttempt,
    SopWorkItem,
    SopWorkItemCommandReceipt,
    Tenant,
    Tool,
    User,
)
from app.dynamic_tasks.capability_catalog import publish_tool_contract
from app.security.auth import create_access_token
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.execution_store import SopExecutionStore
from app.sop_runtime.external_tasks import process_sop_external_task_signal
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult
from tests.test_external_async_tools import _configure_sop_recovery_definition, _provider_contract
from tests.test_final_f3_sop_external_delivery import (
    _deliveries,
    _ready_signal,
    _started_visible_operation,
    delivery_engine as original_delivery_engine,
)


# 直接复用已登记双方言fixture；别名不与各测试参数重名，也不另建迁移合同。
delivery_engine = original_delivery_engine


def _attention_http_app(engine: Engine) -> FastAPI:
    """仅替换自有数据库依赖，真实token认证、成员状态和原Attention路由保持不变。"""

    app = FastAPI()
    app.include_router(router)

    def owned_session() -> Iterator[Session]:
        """为每个真实HTTP请求开启独立连接，不共享setup中的User或事务缓存。"""

        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = owned_session
    return app


def _assert_pending_without_partial_resolution(
    db: Session, instance: SopInstance, operation: SopOperation, attention: SopWorkItem,
) -> None:
    """拒绝后从原持久表交叉核unknown、候选异常、原命令和最终消息均无部分写入。"""

    db.refresh(operation)
    db.refresh(attention)
    db.refresh(instance)
    assert operation.status == "unknown"
    assert attention.status == "offered"
    assert instance.status == "waiting"
    assert db.exec(select(SopWorkItemCommandReceipt)).all() == []
    assert db.exec(select(ExecutionSignal).where(
        ExecutionSignal.signal_type == "attention_decided",
    )).all() == []
    assert _deliveries(db, instance) == []


def _pending_reconciliation(
    db: Session, *, cancelled: bool = False, request: dict[str, object] | None = None,
):
    """由原失败回执恢复产生unknown与异常工作项，不直接预置任何完成状态。"""

    instance, operation, external_signal, time_context = _ready_signal(
        db, provider_status="failed", closed_turn="complete", request=request,
    )
    approver = db.get(User, "f3-owner")
    assert approver is not None
    approver.role = "admin"
    db.add(approver)
    db.commit()
    process_sop_external_task_signal(db, external_signal)
    db.refresh(instance)
    db.refresh(operation)
    db.refresh(external_signal)
    assert operation.status == "unknown"
    assert external_signal.status == "consumed"
    attention = db.exec(select(SopWorkItem).where(
        SopWorkItem.instance_id == instance.id,
        SopWorkItem.attention_kind == "exception",
    )).one()
    assert attention.status == "offered"
    if cancelled:
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="f3-reconcile-cancel"):
            assert store.request_cancellation(
                instance, actor_user_id=instance.initiator_user_id,
                reason="合成取消：外部效果必须先对账",
            ) is False
        db.commit()
    node = db.get(SopNodeExecution, operation.node_execution_id)
    assert node is not None and node.status == "waiting"
    return instance, operation, attention, approver, time_context


def _resolution(attention: SopWorkItem, command: str = "confirm_applied"):
    """固定一次性办理命令、原revision及私密证据，禁止从comment拼业务结果。"""

    return AttentionResolveRequest(
        tenant_id=attention.tenant_id, command_id="f3-manual-evidence-command",
        command=command, expected_revision=attention.revision,
        comment="已独立核对合成对象；PRIVATE_RECONCILIATION_COMMENT不得进入业务结果",
    )


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("applied", [False, True])
def test_original_attention_api_settles_sop_once_without_resending(
    delivery_engine, cancelled, applied,
):
    """原API完成证据后须终结或恢复同一SOP；重复命令不新增动作、attempt或信号。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, time_context = _pending_reconciliation(
            db, cancelled=cancelled,
        )
        operation_identity = (
            operation.id, operation.idempotency_key, operation.external_task_id,
            operation.request_fingerprint, operation.node_execution_id,
        )
        command = "confirm_applied" if applied else "confirm_not_applied"
        request = _resolution(attention, command)
        reply = resolve_attention_item(attention.id, request, approver, db)
        assert reply.status == "completed"
        db.refresh(operation)
        db.refresh(instance)
        assert operation.status == ("succeeded" if applied else "failed"), (
            "SOP异常办理已返回completed，但原Operation仍未完成人工对账"
        )
        assert operation.effect_state == ("complete" if applied else "none")
        assert operation.cancellation_disposition == "reconciled"
        assert operation.reconciled_at is not None
        assert instance.status == (
            "cancelled" if cancelled else "succeeded" if applied else "failed"
        )
        assert instance.active_slot_key is None
        assert operation_identity == (
            operation.id, operation.idempotency_key, operation.external_task_id,
            operation.request_fingerprint, operation.node_execution_id,
        )
        attempts = db.exec(select(SopOperationAttempt).where(
            SopOperationAttempt.operation_id == operation.id,
        )).all()
        assert len(attempts) == 1
        assert attempts[0].status == ("succeeded" if applied else "failed")
        signals = db.exec(select(ExecutionSignal).where(
            ExecutionSignal.execution_id == instance.id,
            ExecutionSignal.signal_type == "attention_decided",
        )).all()
        assert len(signals) == 1
        assert signals[0].status == ("discarded" if cancelled else "consumed")
        assert signals[0].attempt_count == 1
        assert len(db.exec(select(SopOperation).where(
            SopOperation.instance_id == instance.id,
        )).all()) == 1
        deliveries = _deliveries(db, instance)
        assert len(deliveries) == (1 if applied and not cancelled else 0)
        if deliveries:
            metadata = deliveries[0].metadata_json
            assert metadata["user_message_id"] == "f3-confirm-source"
            assert metadata["time_context"] == time_context
            assert metadata["feedback_source"]["execution_id"] == instance.id
            assert metadata["feedback_source"]["definition_checksum"] == instance.definition_checksum
        assert "PRIVATE_RECONCILIATION_COMMENT" not in str(operation.result_json)
        assert all("PRIVATE_RECONCILIATION_COMMENT" not in row.content for row in db.exec(
            select(Message).where(Message.role == "assistant"),
        ).all())
        assert all("PRIVATE_RECONCILIATION_COMMENT" not in str(row.metadata_json)
                   for row in deliveries)
        assert all("PRIVATE_RECONCILIATION_COMMENT" not in str(row.result_json) for row in db.exec(
            select(ExecutionResult).where(ExecutionResult.execution_id == instance.id),
        ).all())
        assert all("PRIVATE_RECONCILIATION_COMMENT" not in str(row.payload_json) for row in db.exec(
            select(AgentEvent).where(AgentEvent.aggregate_id == instance.id),
        ).all())
        if cancelled:
            assert len(db.exec(select(SopNodeExecution).where(
                SopNodeExecution.instance_id == instance.id,
            )).all()) == 1, "已取消实例不能沿正常成功分支创建后继节点"
        replay = resolve_attention_item(attention.id, request, approver, db)
        assert replay.resolution == reply.resolution
        for changed in (
            {"command": "confirm_not_applied" if applied else "confirm_applied"},
            {"command_id": "f3-losing-original-revision-command"},
        ):
            with pytest.raises(HTTPException) as conflict:
                resolve_attention_item(
                    attention.id, request.model_copy(update=changed), approver, db,
                )
            assert conflict.value.status_code == 409
        assert len(db.exec(select(SopWorkItemCommandReceipt)).all()) == 1
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        assert len(db.exec(select(ExecutionSignal)).all()) == 2
        assert len(_deliveries(db, instance)) == len(deliveries)


@pytest.mark.parametrize("invalid", [
    "tenant", "not_candidate", "missing_evidence", "revision",
])
def test_sop_reconciliation_rejects_invalid_actor_or_command_without_partial_effect(
    delivery_engine, invalid,
):
    """跨租户、非候选、缺证据及旧CAS不得部分完成异常、原操作或恢复信号。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        request = _resolution(attention)
        actor = approver
        expected_status = 403
        if invalid == "tenant":
            db.add(Tenant(id="f3-other-tenant", name="隔离异租户"))
            actor = User(
                id="f3-other-actor", tenant_id="f3-other-tenant", username="other", password_hash="x",
            )
            db.add(actor)
        elif invalid == "not_candidate":
            actor = db.get(User, instance.initiator_user_id)
        elif invalid == "missing_evidence":
            request = request.model_copy(update={"comment": " "})
            expected_status = 400
        elif invalid == "revision":
            request = request.model_copy(update={"expected_revision": attention.revision + 1})
            expected_status = 409
        db.commit()
        with pytest.raises(HTTPException) as rejected:
            resolve_attention_item(attention.id, request, actor, db)
        assert rejected.value.status_code == expected_status
        _assert_pending_without_partial_resolution(db, instance, operation, attention)


@pytest.mark.parametrize("revoked", ["membership", "governance_role"])
def test_original_attention_http_rechecks_current_actor_before_resolution(
    delivery_engine, revoked,
):
    """真实认证拒绝离职者，活动候选降为member后不能凭旧候选继续治理对账。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        request = _resolution(attention)
        token = create_access_token(approver)
        if revoked == "membership":
            approver.membership_status = "left"
        else:
            approver.role = "member"
        db.add(approver)
        db.commit()
        with TestClient(_attention_http_app(delivery_engine)) as client:
            response = client.post(
                f"/api/attention-items/{attention.id}/resolve",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + token},
            )
        assert response.status_code == 403
        _assert_pending_without_partial_resolution(db, instance, operation, attention)


@pytest.mark.parametrize("revoked", ["membership", "governance_role"])
def test_authenticated_attention_actor_revoked_before_original_owner_commit(
    delivery_engine, revoked, monkeypatch,
):
    """真实认证成功后在原owner入口撤销资格，最终办理短事务仍须拒绝部分写入。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        request = _resolution(attention)
        token = create_access_token(approver)
        actor_id = approver.id
        original_owned = SopExecutionStore.owned_for_reconciliation
        revoked_before_owner = False

        @contextmanager
        def revoke_then_own(store, current_instance, *, worker_id, ttl_seconds=30):
            """在认证完成与lease事务之间真实提交撤权；不伪造认证身份或办理结果。"""

            nonlocal revoked_before_owner
            store.db.rollback()
            with Session(delivery_engine) as authority_db:
                current_actor = authority_db.get(User, actor_id)
                assert current_actor is not None and current_actor.membership_status == "active"
                assert current_actor.role == "admin"
                if revoked == "membership":
                    current_actor.membership_status = "left"
                else:
                    current_actor.role = "member"
                authority_db.add(current_actor)
                authority_db.commit()
            revoked_before_owner = True
            with original_owned(
                store, current_instance, worker_id=worker_id, ttl_seconds=ttl_seconds,
            ) as lease:
                yield lease

        monkeypatch.setattr(SopExecutionStore, "owned_for_reconciliation", revoke_then_own)
        with TestClient(_attention_http_app(delivery_engine)) as client:
            response = client.post(
                f"/api/attention-items/{attention.id}/resolve",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + token},
            )
        assert revoked_before_owner is True, "反例必须在真实认证后原owner入口撤权"
        assert response.status_code == 403
        _assert_pending_without_partial_resolution(db, instance, operation, attention)


@pytest.mark.parametrize("failure", [
    "unknown", "ambiguous_timeout", "invalid_accepted", "already_persisted_unknown",
])
def test_original_direct_tool_result_unknown_offers_unique_reconciliation(
    delivery_engine, failure,
):
    """原prepare/start后的工具unknown、模糊超时及缺task受理均须有唯一可办理异常。"""

    with Session(delivery_engine) as db:
        db.add(Tenant(id="tenant_sd4", name="F3直接工具对账隔离租户"))
        db.add(User(
            id="f3-owner", tenant_id="tenant_sd4", username="owner", password_hash="x",
            role="admin",
        ))
        db.add(User(
            id="f3-initiator", tenant_id="tenant_sd4", username="user", password_hash="x",
        ))
        db.add(AgentProfile(
            id="f3-agent", tenant_id="tenant_sd4", name="F3直接对账员工",
            owner_user_id="f3-owner", published_to_gallery=True,
        ))
        db.add(AgentUsage(
            tenant_id="tenant_sd4", agent_id="f3-agent", user_id="f3-initiator",
        ))
        db.commit()
        store, instance, execution, operation = _started_visible_operation(
            db, user_output_paths=("receipt_id",), missing_user_schema=False,
        )
        _configure_sop_recovery_definition(db, instance)
        instance.agent_id = "f3-agent"
        instance.initiator_user_id = "f3-initiator"
        chat_session = db.get(ChatSession, instance.session_id)
        assert chat_session is not None
        chat_session.agent_id = instance.agent_id
        chat_session.user_id = instance.initiator_user_id
        db.add(chat_session)
        db.add(instance)
        db.commit()
        call = ToolCall(name=operation.operation_name, arguments=dict(operation.request_json))
        if failure == "already_persisted_unknown":
            with store.owned(instance, worker_id="f3-original-dispatch-unknown"):
                store.mark_operation_unknown(
                    operation,
                    error={"code": "TOOL_CALL_OUTCOME_UNKNOWN", "message": "合成持久结果未知"},
                )
            db.commit()
        if failure == "invalid_accepted":
            result = ToolResult(
                tool_name=call.name, success=True, disposition="accepted", data={},
            )
        else:
            result = ToolResult(
                tool_name=call.name, success=False,
                disposition="completed" if failure == "ambiguous_timeout" else "unknown",
                error={"code": "TIMEOUT" if failure == "ambiguous_timeout"
                       else "TOOL_CALL_OUTCOME_UNKNOWN", "message": "合成已发送结果未知或请求超时"},
            )
        coordinator = DeterministicSopCoordinator(db)
        original_identity = (
            operation.id, operation.idempotency_key, operation.request_fingerprint,
        )
        blocked_replay = ToolResult(
            tool_name=call.name, success=False, disposition="unknown",
            error={"code": "RUNTIME_OPERATION_RECONCILIATION_REQUIRED", "message": "原动作等待对账"},
        )
        for current_result in (result, blocked_replay):
            plan = coordinator.record_tool_result(chat_session, call, current_result)
            assert plan is not None and plan.action is RuntimeAction.WAIT_OPERATION
            db.commit()
        db.refresh(instance)
        db.refresh(operation)
        db.refresh(execution)
        assert operation.status == "unknown"
        assert operation.effect_state == "unknown"
        attention_rows = db.exec(select(SopWorkItem).where(
            SopWorkItem.instance_id == instance.id,
            SopWorkItem.attention_kind == "exception",
        )).all()
        assert len(attention_rows) == 1, "直接工具结果unknown缺少原唯一人工对账入口"
        attention = attention_rows[0]
        assert attention.status == "offered"
        assert attention.source_ref == operation.id
        assert attention.payload_json["operation_id"] == operation.id
        assert attention.allowed_commands_json == ["confirm_applied", "confirm_not_applied"]
        assert instance.status == "waiting" and execution.status == "waiting"
        assert original_identity == (
            operation.id, operation.idempotency_key, operation.request_fingerprint,
        )
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        assert len(db.exec(select(SopOperation)).all()) == 1
        assert db.exec(select(ExecutionSignal)).all() == []
        assert db.exec(select(SopWorkItemCommandReceipt)).all() == []
        assert _deliveries(db, instance) == []


def test_replayed_unknown_restores_waiting_barrier_for_existing_attention(delivery_engine):
    """重入unknown Operation时，即使异常Attention已存在，也必须重新挂起同一原节点。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, _approver, _time_context = _pending_reconciliation(db)
        execution = db.get(SopNodeExecution, operation.node_execution_id)
        chat_session = db.get(ChatSession, instance.session_id)
        assert execution is not None and chat_session is not None
        store = SopExecutionStore(db)
        with store.owned(instance, worker_id="f3-replay-unknown-existing-attention"):
            store.resume_waiting_node(instance, execution, slots=instance.slots_json or {})
        db.commit()
        db.refresh(instance)
        db.refresh(execution)
        assert instance.status == "running" and execution.status == "running"

        call = ToolCall(
            name=operation.operation_name,
            arguments=dict(operation.request_json or {}),
        )
        plan = DeterministicSopCoordinator(db).record_tool_result(
            chat_session,
            call,
            ToolResult(
                tool_name=call.name,
                success=False,
                disposition="unknown",
                error={
                    "code": "RUNTIME_OPERATION_RECONCILIATION_REQUIRED",
                    "message": "原外部效果仍未知，禁止重复提交。",
                },
            ),
        )
        assert plan is not None and plan.action is RuntimeAction.WAIT_OPERATION
        db.commit()
        db.refresh(instance)
        db.refresh(execution)
        db.refresh(operation)
        db.refresh(attention)
        assert operation.status == "unknown"
        assert attention.status == "offered"
        assert instance.status == "waiting" and execution.status == "waiting"
        assert len(db.exec(select(SopOperation)).all()) == 1
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        assert db.exec(select(ExecutionSignal).where(
            ExecutionSignal.signal_type == "attention_decided",
        )).all() == []
        assert _deliveries(db, instance) == []


def test_reentered_sop_planner_reparks_unknown_operation_before_returning(delivery_engine):
    """新输入重入已有unknown SOP节点时，规划器必须先恢复原对账等待屏障。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, _approver, _time_context = _pending_reconciliation(
            db, request={},
        )
        execution = db.get(SopNodeExecution, operation.node_execution_id)
        chat_session = db.get(ChatSession, instance.session_id)
        assert execution is not None and chat_session is not None
        coordinator = DeterministicSopCoordinator(db)
        with coordinator.store.owned(
            instance, worker_id="f3-reentered-unknown-sop-planner",
        ):
            coordinator.store.resume_waiting_node(
                instance, execution, slots=instance.slots_json or {},
            )
            _version, definition = coordinator._definition_for_instance(instance)
            plan = coordinator._advance_until_external_action(
                chat_session, instance, execution, definition,
            )
        assert plan.action is RuntimeAction.WAIT_OPERATION
        db.commit()
        db.refresh(instance)
        db.refresh(execution)
        db.refresh(operation)
        db.refresh(attention)
        assert operation.status == "unknown"
        assert attention.status == "offered"
        assert instance.status == "waiting" and execution.status == "waiting"
        assert len(db.exec(select(SopOperation)).all()) == 1
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        assert db.exec(select(ExecutionSignal).where(
            ExecutionSignal.signal_type == "attention_decided",
        )).all() == []
        assert _deliveries(db, instance) == []


def test_sop_reconciliation_respects_existing_execution_owner_and_preserves_retry(
    delivery_engine,
):
    """原有效owner阻止另一办理提交；释放同一fence后原命令合法续接且只消费一次。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        request = _resolution(attention)
        holder = SopExecutionStore(db)
        lease = holder.claim(instance, worker_id="f3-existing-reconcile-owner", ttl_seconds=60)
        db.commit()
        with pytest.raises(HTTPException) as rejected:
            resolve_attention_item(attention.id, request, approver, db)
        assert rejected.value.status_code == 409
        db.refresh(operation)
        db.refresh(attention)
        assert operation.status == "unknown"
        assert attention.status == "offered"
        holder.release(lease)
        db.commit()
        resolved = resolve_attention_item(attention.id, request, approver, db)
        assert resolved.status == "completed"
        db.refresh(operation)
        assert operation.status == "succeeded"
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1


def _freeze_successor_before_original_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """在原accepted/失败回执之前登记两工具冻结定义与合法绑定，不预置任何后继成功。"""

    def configure(db: Session, instance: SopInstance) -> None:
        """扩展合成发布版本的事前定义，后续仅由原coordinator创建并派发register节点。"""

        _configure_sop_recovery_definition(db, instance)
        version = db.get(SkillVersion, instance.skill_version_id)
        assert version is not None
        content = dict(version.content_json)
        content["nodes"] = [content["nodes"][0], {
            "node_id": "register", "type": "tool_call", "name": "登记原人工对账结果",
            "allowed_actions": ["call_tool:orders.register"],
            "metadata": {"operation_result_key": "registered"},
        }, content["nodes"][-1]]
        content["edges"] = [
            {"source_node_id": "submit", "next_node_id": "register"},
            {"source_node_id": "register", "next_node_id": "done"},
        ]
        definition = compile_legacy_skill_card(content)
        version.content_json = content
        version.compiled_definition_checksum = definition.checksum
        instance.definition_checksum = definition.checksum
        db.add(version)
        db.add(instance)
        tool = Tool(
            id="f3-reconcile-register-tool", tenant_id=instance.tenant_id,
            name="orders.register", method="POST", url="https://93.184.216.34/register",
            output_schema={"type": "object", "properties": {
                "receipt_id": {"type": "string"}, "registered": {"type": "boolean"},
            }},
        )
        publish_tool_contract(tool, _provider_contract())
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(
            db, instance.tenant_id, "f3-agent", "tool", tool.id,
        )
        db.flush()

    monkeypatch.setattr(
        "tests.test_final_f3_sop_external_delivery._configure_sop_recovery_definition", configure,
    )


def test_reconciliation_successor_original_http_transport_has_no_transaction(
    delivery_engine, monkeypatch,
):
    """人工已生效后经原HTTP适配与受控transport发唯一后继，网络边界不能持数据库事务。"""

    _freeze_successor_before_original_receipt(monkeypatch)
    original_client = httpx.Client
    observed_transactions: list[bool] = []
    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        instance_id, operation_id, attention_id = instance.id, operation.id, attention.id
        request = _resolution(attention)

        def transport(http_request: httpx.Request) -> httpx.Response:
            """仅替换HTTP传输；原工具鉴权、冻结合同、序列化和对账恢复全部真实执行。"""

            observed_transactions.append(db.in_transaction())
            assert http_request.method == "POST" and http_request.url.path == "/register"
            with Session(delivery_engine) as observer:
                original = observer.get(SopOperation, operation_id)
                assert original is not None and original.status == "succeeded"
                assert observer.get(SopWorkItem, attention_id).status == "completed"
                decided = observer.exec(select(ExecutionSignal).where(
                    ExecutionSignal.execution_id == instance_id,
                    ExecutionSignal.signal_type == "attention_decided",
                )).one()
                assert decided.status == "consumed"
                successor = observer.exec(select(SopOperation).where(
                    SopOperation.instance_id == instance_id,
                    SopOperation.operation_name == "orders.register",
                )).one()
                assert successor.status == "running"
                assert http_request.headers["Idempotency-Key"] == successor.remote_idempotency_key
            return httpx.Response(200, json={
                "status": "completed", "registered": True, "receipt_id": "F3_REGISTER_RECEIPT",
            })

        def client_factory(*args, **kwargs):
            """复用真实HTTPX Client，受控MockTransport只避免外部网络和真实业务对象。"""

            return original_client(*args, transport=httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr("app.tools.tool_executor.httpx.Client", client_factory)
        reply = resolve_attention_item(attention.id, request, approver, db)
        assert reply.status == "completed"
        assert observed_transactions == [False], "原HTTP运输层仍持有本次数据库事务"
        db.refresh(instance)
        assert instance.status == "succeeded"
        assert len(db.exec(select(SopOperation)).all()) == 2
        assert len(db.exec(select(SopOperationAttempt)).all()) == 2
        assert len(_deliveries(db, instance)) == 1
        resolve_attention_item(attention.id, request, approver, db)
        assert observed_transactions == [False]


@pytest.mark.parametrize("barrier", ["binding_revoked", "fence_replaced"])
def test_reconciliation_successor_rechecks_owner_and_authority_after_tool_lookup(
    delivery_engine, monkeypatch, barrier,
):
    """原HTTP鉴权读取后另一真实Session撤绑定或换fence，提交守卫必须阻止任何外呼。"""

    _freeze_successor_before_original_receipt(monkeypatch)
    original_client = httpx.Client
    original_authorize = ToolExecutor._authorize_tool
    calls: list[str] = []
    switched = False
    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        instance_id, agent_id = instance.id, instance.agent_id
        operation_id, attention_id = operation.id, attention.id
        request = _resolution(attention)

        def authorize_then_revoke(executor, **kwargs):
            """原鉴权返回后结束该读事务，让独立连接在实际HTTP边界前提交故障竞争。"""

            nonlocal switched
            result = original_authorize(executor, **kwargs)
            if switched or kwargs["tool_call"].name != "orders.register" or result[2] is not None:
                return result
            switched = True
            executor.db.rollback()
            with Session(delivery_engine) as authority_db:
                if barrier == "binding_revoked":
                    authority_db.exec(update(AgentResourceBinding).where(
                        AgentResourceBinding.agent_id == agent_id,
                        AgentResourceBinding.resource_type == "tool",
                        AgentResourceBinding.resource_id == "f3-reconcile-register-tool",
                    ).values(status="inactive"))
                else:
                    authority_db.exec(update(SopInstance).where(
                        SopInstance.id == instance_id,
                    ).values(fencing_token=SopInstance.fencing_token + 1,
                             lease_owner="f3-current-owner-winner"))
                authority_db.commit()
            return result

        def transport(http_request: httpx.Request) -> httpx.Response:
            """记录真正序列化的HTTP请求，失权后有任何请求即证明零效果守卫未生效。"""

            calls.append(http_request.url.path)
            return httpx.Response(200, json={
                "status": "completed", "registered": True, "receipt_id": "F3_REGISTER_RECEIPT",
            })

        def client_factory(*args, **kwargs):
            """只替換自有受控运输，保留原HTTPX Client与全部ToolExecutor鉴权过程。"""

            return original_client(*args, transport=httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr(ToolExecutor, "_authorize_tool", authorize_then_revoke)
        monkeypatch.setattr("app.tools.tool_executor.httpx.Client", client_factory)
        try:
            resolve_attention_item(attention.id, request, approver, db)
        except HTTPException as rejected:
            assert rejected.status_code == 409
        assert switched is True
        assert calls == [], "原Tool lookup与HTTP之间撤权后仍发出了后继请求"
        db.rollback()
        assert db.get(SopOperation, operation_id).status == "succeeded"
        assert db.get(SopWorkItem, attention_id).status == "completed"
        assert db.get(SopInstance, instance_id).status != "succeeded"
        assert _deliveries(db, db.get(SopInstance, instance_id)) == []


@pytest.mark.parametrize("applied", [False, True])
def test_original_agent_update_archive_reconciles_effect_without_successor_or_cancellation(
    delivery_engine, monkeypatch, applied,
):
    """原PUT普通状态归档不制造取消请求；仍须完成真实对账，但不能创建后继或成功消息。"""

    _freeze_successor_before_original_receipt(monkeypatch)
    calls: list[str] = []

    def execute(executor, tenant_id, tool_call, *args, **kwargs):
        """归档来源不允许进入后继派发；这里仅记录原调用边界，不返回业务成功。"""

        calls.append(tool_call.name)
        raise AssertionError("归档对账不得派发正常后继")

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        update_agent(
            instance.agent_id,
            AgentProfileUpdateRequest(tenant_id=instance.tenant_id, status="archived"),
            db=db, current_user=approver,
        )
        db.refresh(instance)
        assert instance.cancellation_requested_at is None
        reply = resolve_attention_item(
            attention.id, _resolution(attention, "confirm_applied" if applied
                                       else "confirm_not_applied"), approver, db,
        )
        assert reply.status == "completed"
        db.refresh(instance)
        db.refresh(operation)
        assert operation.status == ("succeeded" if applied else "failed")
        assert operation.effect_state == ("complete" if applied else "none")
        assert operation.cancellation_disposition == "reconciled"
        assert instance.status == "failed"
        assert instance.active_slot_key is None
        assert instance.cancellation_requested_at is None
        assert instance.cancellation_requested_by is None
        assert calls == []
        assert db.exec(select(SopOperation).where(
            SopOperation.operation_name == "orders.register",
        )).all() == []
        assert len(db.exec(select(SopOperationAttempt)).all()) == 1
        assert _deliveries(db, instance) == []


@pytest.mark.parametrize("revoked", ["agent_usage", "membership"])
@pytest.mark.parametrize("applied", [False, True])
def test_reconciliation_keeps_confirmed_effect_when_original_actor_authority_is_revoked(
    delivery_engine, revoked, applied,
):
    """合法治理人可结算原效果，发起人失权不得令已确认效果整体回滚或继续成功披露。"""

    with Session(delivery_engine) as db:
        instance, operation, attention, approver, _time_context = _pending_reconciliation(db)
        if revoked == "agent_usage":
            db.delete(db.exec(select(AgentUsage).where(
                AgentUsage.agent_id == instance.agent_id,
                AgentUsage.user_id == instance.initiator_user_id,
            )).one())
        else:
            original_actor = db.get(User, instance.initiator_user_id)
            assert original_actor is not None
            original_actor.membership_status = "left"
            db.add(original_actor)
        db.commit()
        reply = resolve_attention_item(
            attention.id, _resolution(attention, "confirm_applied" if applied
                                       else "confirm_not_applied"), approver, db,
        )
        assert reply.status == "completed"
        db.refresh(instance)
        db.refresh(operation)
        assert operation.status == ("succeeded" if applied else "failed")
        assert operation.effect_state == ("complete" if applied else "none")
        assert operation.cancellation_disposition == "reconciled"
        assert operation.reconciled_at is not None
        attempt = db.exec(select(SopOperationAttempt).where(
            SopOperationAttempt.operation_id == operation.id,
        )).one()
        assert attempt.status == ("succeeded" if applied else "failed")
        assert instance.status == "failed"
        assert instance.active_slot_key is None
        assert instance.cancellation_requested_at is None
        assert instance.cancellation_requested_by is None
        assert len(db.exec(select(SopOperation)).all()) == 1
        assert len(db.exec(select(SopNodeExecution)).all()) == 1
        assert _deliveries(db, instance) == []
