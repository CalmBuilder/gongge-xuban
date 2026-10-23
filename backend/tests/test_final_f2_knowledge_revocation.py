"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f2_knowledge_revocation.py
@CallChain  : 双库真实检索 → 原DTA后继/SDK → 分支撤权 → 提案与成果读回
@Description: 复现检索后知识失权仍再次外发，核正常冻结、修复重试及在途响应拒绝。
"""
import hashlib
import json

import pytest
from sqlmodel import Session, select

from app.agents.branching import rollback_knowledge_branch
from app.db.models import (
    ActionProposalRecord,
    AgentKnowledgeBranch,
    ExecutionPublication,
    ExecutionResult,
    ExecutionSignal,
    KnowledgeBaseVersion,
    Tool,
)
from app.dynamic_tasks import agent as runtime
from app.dynamic_tasks.capability_catalog import publish_tool_contract, published_tool_snapshot
from app.dynamic_tasks.planning import PlanStep
from app.dynamic_tasks.worker import _is_terminal_signal_error, process_dynamic_task_signal
from app.knowledge.service import KnowledgeService
from app.sop_runtime.external_tasks import apply_pending_external_task_events, ingest_external_task_event
from tests.test_memory_projection_consumers import SDK, llm
from tests.test_external_async_tools import _provider_contract
from tests.test_staffdeck_sd5_consumers import (
    AGENT_ID, KB_ID, PUBLISHED_VERSION_ID, TENANT_ID, USER_ID,
    _Catalog, _KnowledgePlanner, _KnowledgeProposer,
)
from tests.test_staffdeck_sd8_dta_citations import _seed
from tests.test_staffdeck_sd8_retrieval import retrieval_db as _retrieval_db

retrieval_db = _retrieval_db


def _advance_agent_knowledge_branch(db: Session, kb: object) -> None:
    """模拟员工显式同步到已发布后继版本，旧执行不再命中当前精确分支。"""

    branch = db.exec(select(AgentKnowledgeBranch).where(
        AgentKnowledgeBranch.tenant_id == TENANT_ID,
        AgentKnowledgeBranch.agent_id == AGENT_ID,
        AgentKnowledgeBranch.knowledge_base_id == KB_ID,
    )).one()
    published = KnowledgeBaseVersion(
        id="kbver_sd5_consumers_forward_sync",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        version="2.1.0",
        scope=f"agent:{AGENT_ID}",
        name=str(getattr(kb, "name")),
        publication_state="published",
        parent_version_id=PUBLISHED_VERSION_ID,
    )
    db.add(published)
    branch.base_version = published.version
    branch.head_version = published.version
    branch.sync_state = "synced"
    branch.content_revision += 1
    db.add(branch)
    db.commit()


@pytest.mark.parametrize("case", ["legal", "legal_retry", "legal_old_binding",
                                 "legal_forward_publication", "revoked_before",
                                 "revoked_forward_branch", "repair_revoked", "inflight_revoked"])
def test_consumed_knowledge_reauthorized_at_actual_sdk_and_result(retrieval_db, monkeypatch, case):
    """原知识Operation完成后换head；下一SDK零发送，在途返回不能落提案/成果。"""
    db = retrieval_db
    _, _, kb, model = _seed(db)
    dynamic = runtime.DynamicTaskAgent(db, catalog=_Catalog(model), planner=_KnowledgePlanner(),
        action_proposer=_KnowledgeProposer(), knowledge_service=KnowledgeService(db))
    instance, _ = dynamic.start_task(tenant_id=TENANT_ID, session_id="f2-knowledge-source",
        agent_id=AGENT_ID, initiator_user_id=USER_ID, goal="依据原已选知识回答",
        success_criteria=("完成原检索",), model_config=model,
        knowledge_capability={"available": True, "required": True, "knowledge_bases": [{
            "id": kb.id, "version_id": PUBLISHED_VERSION_ID, "version": "2.0.0",
            "name": kb.name}]})
    dynamic.advance_next_knowledge_step(execution_id=instance.id, model_config=model,
        worker_id="f2-source-worker", actor_user_id=USER_ID)
    db.commit()
    frozen = json.dumps(instance.capability_snapshot_json, sort_keys=True)
    before = len(db.exec(select(ActionProposalRecord)).all())

    def revoke():
        """第二连接沿原版本回退行为改变当前授权，原执行和检索结果保持不变。"""
        with Session(db.get_bind()) as other:
            rollback_knowledge_branch(other, TENANT_ID, AGENT_ID, KB_ID, "1.0.0")
            other.commit()

    answer = json.dumps({"action_kind": "answer", "arguments": {
        "markdown": "已完成实际检索。", "criterion_evidence": {"criterion_01": ["search_policy"]},
        "pending_questions": []}, "rationale": "核验当前来源"})

    class SourceSDK(SDK):
        """仅替换传输；在真实首SDK后撤权，保留生产JSON修复与派发守卫。"""

        def create(self, **kwargs):
            """首请求必须有原知识事实；返回前由独立连接失效该精确版本授权。"""
            assert "sd5-new-fact" in json.dumps(kwargs["messages"], ensure_ascii=False)
            response = super().create(**kwargs)
            if len(self.calls) == 1 and case in {"repair_revoked", "inflight_revoked"}:
                revoke()
            return response

    sdk = SourceSDK((["{invalid", answer] if case in {"repair_revoked", "legal_retry"} else [answer]))

    def client(_model, **kwargs):
        """真实LLMClient接同一SDK并保留生产派发observer，不以客户端替身绕守卫。"""
        value = llm(sdk)
        value.dispatch_observer = kwargs.get("dispatch_observer")
        return value

    monkeypatch.setattr(runtime, "LLMClient", client)
    dynamic.action_proposer = None
    if case == "revoked_before":
        revoke()
    if case == "legal_old_binding":
        with Session(db.get_bind()) as other:
            current_kb = other.get(type(kb), kb.id)
            current_kb.metadata_json = {**current_kb.metadata_json, "current_version": "1.0.0"}
            other.add(current_kb)
            other.commit()
    if case == "legal_forward_publication":
        published = KnowledgeBaseVersion(
            id="kbver_sd5_consumers_forward_publish",
            tenant_id=TENANT_ID,
            knowledge_base_id=kb.id,
            version="2.1.0",
            scope="overall",
            name=kb.name,
            publication_state="published",
            parent_version_id=PUBLISHED_VERSION_ID,
        )
        db.add(published)
        kb.metadata_json = {**(kb.metadata_json or {}), "current_version": published.version}
        db.add(kb)
        db.commit()
    if case == "revoked_forward_branch":
        _advance_agent_knowledge_branch(db, kb)
    if case in {"legal", "legal_retry", "legal_old_binding", "legal_forward_publication"}:
        outcome = dynamic.run_until_blocked_or_complete(execution_id=instance.id, model_config=model,
            worker_id="f2-source-worker", actor_user_id=USER_ID)
        assert outcome.status == "succeeded" and len(sdk.calls) == (2 if case == "legal_retry" else 1)
    else:
        with pytest.raises(runtime.DynamicTaskAgentError, match="DYNAMIC_KNOWLEDGE_ACCESS_CHANGED"):
            dynamic.run_until_blocked_or_complete(execution_id=instance.id, model_config=model,
                worker_id="f2-source-worker", actor_user_id=USER_ID)
        assert len(sdk.calls) == (0 if case in {"revoked_before", "revoked_forward_branch"} else 1)
        assert len(db.exec(select(ActionProposalRecord)).all()) == before
        assert not db.exec(select(ExecutionResult)).all()
        with Session(db.get_bind()) as observer:
            persisted = observer.get(type(instance), instance.id)
            assert persisted.lease_owner is None
    assert json.dumps(instance.capability_snapshot_json, sort_keys=True) == frozen


@pytest.mark.parametrize("code", ["DYNAMIC_KNOWLEDGE_ACCESS_CHANGED", "DYNAMIC_KNOWLEDGE_ACTOR_DENIED"])
def test_knowledge_source_denial_is_terminal_for_same_signal(code):
    """稳定来源拒绝不能按网络重试再次披露或长期生成待批准后继。"""
    assert _is_terminal_signal_error(code)


@pytest.mark.parametrize("branch_change", ["rollback", "forward_sync"])
def test_consumed_external_signal_knowledge_denial_closes_original_execution(
    retrieval_db, monkeypatch, branch_change
):
    """真实回执已消费后撤权，零SDK且原Execution闭合失败，重复唤醒不能重发。"""
    db = retrieval_db
    _, _, kb, model = _seed(db)
    tool = Tool(id="f2-signal-tool", tenant_id=TENANT_ID, name="f2.submit", method="POST",
                url="https://provider.example/submit")
    publish_tool_contract(tool, _provider_contract().model_copy(update={"dynamic_task_enabled": True}))
    db.add(tool)
    db.commit()

    class SignalCatalog(_Catalog):
        """只提供本次合法发布的工具快照，不改知识目录与版本授权。"""

        def list_tools(self, tenant_id, agent_id):
            """使用原工具发布编译所得snapshot，包含准确外部写/跟踪合同。"""
            return [published_tool_snapshot(tool, agent_id)]

    class SignalPlanner(_KnowledgePlanner):
        """原知识之后声明外部提交和回答步骤，不伪造未声明node。"""

        def create_plan(self, **kwargs):
            """在启动时冻结真实三个步骤，后续运行计划保持不可变。"""
            plan = super().create_plan(**kwargs)
            submit = PlanStep(step_key="external_source", title="原外部提交", kind="tool.write",
                              capability_refs=(tool.name,), depends_on=("search_policy",))
            answer = plan.steps[1].model_copy(update={"depends_on": ("external_source",)})
            return plan.model_copy(update={"steps": (plan.steps[0], submit, answer),
                                           "budget": {"max_steps": 5}})

    dynamic = runtime.DynamicTaskAgent(db, catalog=SignalCatalog(model), planner=SignalPlanner(),
        action_proposer=_KnowledgeProposer(), knowledge_service=KnowledgeService(db))
    instance, _ = dynamic.start_task(tenant_id=TENANT_ID, session_id="f2-consumed-signal",
        agent_id=AGENT_ID, initiator_user_id=USER_ID, goal="依据原检索继续办理",
        success_criteria=("完成原检索",), model_config=model, source_kind="manual",
        knowledge_capability={"available": True, "required": True, "knowledge_bases": [{
            "id": kb.id, "version_id": PUBLISHED_VERSION_ID, "version": "2.0.0",
            "name": kb.name}]})
    dynamic.advance_next_knowledge_step(execution_id=instance.id, model_config=model,
        worker_id="f2-signal-source", actor_user_id=USER_ID)
    store = dynamic.store
    with store.owned(instance, worker_id="f2-signal-source"):
        node = store.enter_node(instance, "external_source", step_key="external_source",
            plan_revision_id=instance.current_plan_revision_id, step_kind="dynamic_action")
        operation, _ = store.prepare_operation(instance, node, operation_name="f2.submit",
            request={"synthetic_reference": "F2_REVOKED_SOURCE"}, effect_kind="external_write")
        store.start_operation(operation)
        operation.external_callback_token_hash = hashlib.sha256(b"f2-synthetic-callback").hexdigest()
        db.add(operation)
        store.mark_operation_waiting_external(operation, provider_task_id="f2-original-task",
            provider_status="accepted", receipt={"status_code": 202},
            status_url="https://provider.example/status/{taskId}", tracking_seconds=300)
    db.commit()
    if branch_change == "rollback":
        with Session(db.get_bind()) as other:
            rollback_knowledge_branch(other, TENANT_ID, AGENT_ID, KB_ID, "1.0.0")
            other.commit()
    else:
        _advance_agent_knowledge_branch(db, kb)
    ingest_external_task_event(db, tenant_id=TENANT_ID, operation_id=operation.id,
        provider_task_id="f2-original-task", provider_event_id="f2-original-complete",
        provider_status="completed", observation_revision=2, payload={"result": {"ok": True}},
        callback_token="f2-synthetic-callback")
    assert apply_pending_external_task_events(db) == 1
    signal = db.exec(select(ExecutionSignal).where(
        ExecutionSignal.execution_id == instance.id, ExecutionSignal.signal_type == "external_event")).one()
    sdk = SDK([])

    def client(_model, **kwargs):
        """保留原SDK及observer；失权流程绝不可请求此传输。"""
        value = llm(sdk)
        value.dispatch_observer = kwargs.get("dispatch_observer")
        return value

    def factory(_db):
        """只复用已合法启动的原agent及能力目录，Signal处理和闭合仍为生产实现。"""
        return dynamic

    monkeypatch.setattr(runtime, "LLMClient", client)
    dynamic.action_proposer = None
    process_dynamic_task_signal(db, signal, agent_factory=factory)
    db.refresh(instance)
    db.refresh(signal)
    db.refresh(operation)
    assert sdk.calls == []
    assert signal.status == "consumed" and signal.attempt_count == 1
    assert operation.status == "succeeded"
    assert instance.status == "failed"
    assert instance.terminal_reason_json["code"] == "DYNAMIC_KNOWLEDGE_ACCESS_CHANGED"
    assert instance.lease_owner is None
    result = db.get(ExecutionResult, instance.current_result_id)
    assert result is not None
    assert result.execution_id == instance.id
    assert result.status == "verified"
    assert result.result_json == {
        "status": "failed",
        "context": {"failure_code": "DYNAMIC_KNOWLEDGE_ACCESS_CHANGED"},
    }
    assert result.verification_json == {"passed": True, "source": "runtime_failure"}
    publication = db.exec(select(ExecutionPublication).where(
        ExecutionPublication.tenant_id == TENANT_ID,
        ExecutionPublication.execution_id == instance.id,
        ExecutionPublication.result_id == result.id,
        ExecutionPublication.target_type == "application",
    )).one()
    assert publication.required is True
    assert publication.status == "settled"
    assert publication.receipt_json == {
        "projection": "execution_result",
        "execution_id": instance.id,
        "result_id": result.id,
    }
    process_dynamic_task_signal(db, signal, agent_factory=factory)
    assert sdk.calls == []
