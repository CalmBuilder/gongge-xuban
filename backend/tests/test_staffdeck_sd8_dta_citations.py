"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_staffdeck_sd8_dta_citations.py
@CallChain  : pytest SQLite/MySQL → KnowledgeService → DynamicTaskAgent → 原消息发布/读取
@Description: 验证最终成果引用绑定实际充分检索、物理版本与已用步骤，不借其他执行。
"""

import pytest
from sqlmodel import select

from app.api.chat import message_read
from app.db.models import (
    AgentEvent,
    ExecutionResult,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    Message,
    ModelConfig,
    SopOperation,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    PlanStep,
    RuntimeActionProposal,
)
from app.knowledge.service import KnowledgeService
from tests.test_staffdeck_sd5_consumers import (
    AGENT_ID,
    KB_ID,
    PUBLISHED_VERSION_ID,
    TENANT_ID,
    USER_ID,
    _Catalog,
    _KnowledgePlanner,
    _seed_consumer_fixture,
)
from tests import test_staffdeck_sd8_retrieval as retrieval_tests

retrieval_db = retrieval_tests.retrieval_db


class _LegacySnapshotAgent(DynamicTaskAgent):
    """创建升级前确实缺输出schema的知识快照，之后只读恢复而不补写旧契约。"""

    @staticmethod
    def _knowledge_snapshot(**kwargs):
        """在首次创建时还原旧卡片形态与合法checksum，不改已持久快照。"""
        snapshot = DynamicTaskAgent._knowledge_snapshot(**kwargs)
        model_view = dict(snapshot.model_view)
        model_view.pop("output_schema", None)
        payload = snapshot.model_dump(mode="json", exclude={"checksum", "agent_id"})
        payload["model_view"] = model_view
        return snapshot.model_copy(update={"model_view": model_view,
                                           "checksum": capability_checksum(payload)})


class _Planner(_KnowledgePlanner):
    """复用原知识计划，按用例可增加一个实际执行但最终不引用的知识步骤。"""

    def __init__(self, unused: bool):
        """保存是否实际执行额外条款检索，不改变最终业务查询。"""
        self.unused = unused

    def create_plan(self, **kwargs):
        """由原规范计划增加独立前置知识步骤，保留同一确定性执行账本。"""
        plan = super().create_plan(**kwargs)
        if not self.unused:
            return plan
        first = PlanStep(step_key="unused_policy", title="查询其他条款", kind="knowledge",
                         capability_refs=("knowledge.search",))
        search = plan.steps[0].model_copy(update={"depends_on": ("unused_policy",)})
        return plan.model_copy(update={"steps": (first, search, plan.steps[1])})


class _Proposer:
    """只替换模型动作协议，检索、充分性、结果校验及发布仍走生产实现。"""

    def __init__(self, case: str):
        """按固定标签选择业务查询，不向检索响应或成果注入知识事实。"""
        self.case = case

    def propose(self, *, view, step):
        """核验模型实际收到原检索证据；最终成果只声明使用search_policy。"""
        if step.kind == "knowledge":
            query = "sd8-unrelated-fact" if step.step_key == "unused_policy" else (
                "ZIRCON999UNKNOWN" if self.case == "no_match" else "sd5-new-fact"
            )
            arguments = {"query": query}
            if self.case == "insufficient" and step.step_key != "unused_policy":
                arguments["desired_evidence"] = "QUASARABSENT999"
            proposal = RuntimeActionProposal(action_kind=ActionKind.QUERY_KNOWLEDGE,
                capability_ref="knowledge.search", arguments=arguments,
                rationale="实际知识步骤")
        else:
            completed = next(row for row in view.execution_context["completed_steps"]
                             if row["step_key"] == "search_policy")
            projection = completed["model_output"]
            assert isinstance(projection, dict), "KNOWLEDGE_RESULT_NOT_VISIBLE_TO_ANSWER"
            expected = self.case if self.case in {"no_match", "insufficient"} else "evidence_found"
            assert projection["outcome"] == expected
            if self.case == "insufficient":
                assert projection["evidence_sufficiency"]["satisfied"] is False
                for field in ("chunks", "evidence_pack", "okf_citations", "selected_documents",
                              "selected_buckets", "selected_concepts"):
                    assert projection[field] == [], "UNALIGNED_KNOWLEDGE_CANDIDATE_REACHED_DTA_ANSWER"
            elif self.case != "no_match":
                assert "sd5-new-fact" in str(projection["evidence_pack"])
                assert "sd5-old-fact" not in str(projection["evidence_pack"])
            assert projection["evidence_sufficiency"]["satisfied"] == (
                self.case not in {"no_match", "insufficient"}
            )
            proposal = RuntimeActionProposal(action_kind=ActionKind.ANSWER, arguments={
                "markdown": "已完成实际检索；充分性以原回执为准。",
                "criterion_evidence": {"criterion_01": ["search_policy"]},
                "pending_questions": [],
            }, rationale="按检索回执完成")
        return CompletedProviderProposal(response_id="sd8-" + step.step_key,
                                         finish_reason="stop", proposal=proposal)


def _seed(db):
    """同租户旧新概念共用逻辑键但物理ID、正文和文档引用不同。"""
    user, agent, kb, _, _ = _seed_consumer_fixture(db)
    for suffix in ("1", "2"):
        db.add(KnowledgeConcept(id="sd8-physical-concept-" + suffix, tenant_id=TENANT_ID,
            knowledge_base_id=KB_ID, knowledge_base_version_id="kbver_sd5_consumers_" + suffix,
            document_id="kdoc_sd5_consumers_" + suffix, concept_id="sd8-same-logical-policy",
            concept_type="Business Rule", title="Policy", description="制度事实",
            content_md="旧事实 sd5-old-fact" if suffix == "1" else "新事实 sd5-new-fact",
            source_refs=[{"document_id": "kdoc_sd5_consumers_" + suffix}]))
    db.add(KnowledgeDocument(id="sd8-unused-doc", tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID, knowledge_base_version_id=PUBLISHED_VERSION_ID,
        filename="other.md", file_type="md", status="ready",
        normalized_source="其他条款 sd8-unrelated-fact", summary="其他条款 sd8-unrelated-fact"))
    db.add(KnowledgeBucket(id="sd8-unused-bucket", tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID, knowledge_base_version_id=PUBLISHED_VERSION_ID,
        document_id="sd8-unused-doc", bucket_key="other", title="其他条款 sd8-unrelated-fact",
        summary="其他条款 sd8-unrelated-fact", metadata_json={"content": "sd8-unrelated-fact"}))
    db.add(KnowledgeChunk(id="sd8-unused-chunk", tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID, knowledge_base_version_id=PUBLISHED_VERSION_ID,
        document_id="sd8-unused-doc", bucket_id="sd8-unused-bucket", chunk_index=0,
        content="其他条款 sd8-unrelated-fact", summary="其他条款 sd8-unrelated-fact"))
    model = ModelConfig(id="sd8-citation-model", tenant_id=TENANT_ID, name="Synthetic model",
        api_key_encrypted="test-only", model="test-model", preflight_status="ready",
        capability_snapshot_json={"protocol_version": "dynamic-v1", "sdk_available": True,
            "credentials_verified": True, "structured_output": True, "tool_calling": True})
    db.add(model)
    db.commit()
    return user, agent, kb, model


def _run(db, model, kb, case, session, legacy):
    """执行实际Service、统一Runtime、验证结果及原事务发布，返回持久成果。"""
    agent_class = _LegacySnapshotAgent if legacy else DynamicTaskAgent
    dynamic = agent_class(db, catalog=_Catalog(model), planner=_Planner(case == "unused"),
        action_proposer=_Proposer(case), knowledge_service=KnowledgeService(db))
    instance, _ = dynamic.start_task(tenant_id=TENANT_ID, session_id=session, agent_id=AGENT_ID,
        initiator_user_id=USER_ID, goal="依据知识检索回执回答", success_criteria=("完成检索",),
        model_config=model, knowledge_capability={"available": True, "required": True,
            "knowledge_bases": [{"id": kb.id, "version_id": PUBLISHED_VERSION_ID,
                                 "version": "2.0.0", "name": kb.name}]})
    frozen_before = capability_checksum(instance.capability_snapshot_json)
    plan_checksum = instance.current_plan_checksum
    knowledge = instance.capability_snapshot_json["knowledge"][0]
    assert ("output_schema" not in knowledge["model_view"]) is legacy
    outcome = dynamic.run_until_blocked_or_complete(execution_id=instance.id,
        model_config=model, worker_id="sd8-citation-worker", actor_user_id=USER_ID)
    assert outcome.status == "succeeded"
    db.refresh(instance)
    assert capability_checksum(instance.capability_snapshot_json) == frozen_before
    assert instance.current_plan_checksum == plan_checksum
    result = db.get(ExecutionResult, instance.current_result_id)
    assert result.status == "verified" and result.verification_json["passed"] is True
    messages = db.exec(select(Message).where(Message.session_id == session,
                                            Message.role == "assistant")).all()
    message = next(row for row in messages if row.metadata_json.get("execution_id") == instance.id)
    assert message.content == result.result_json["markdown"]
    assert message.metadata_json["result_checksum"] == result.checksum
    events = db.exec(select(AgentEvent).where(AgentEvent.session_id == session,
        AgentEvent.event_type == "assistant_message_created")).all()
    event = next(row for row in events if row.payload_json.get("message_id") == message.id)
    assert event.payload_json.get("knowledge_citations", []) == message.metadata_json.get(
        "knowledge_citations", [])
    return instance, message, dynamic


@pytest.mark.parametrize("case", ["matched", "no_match", "insufficient", "unused"])
@pytest.mark.parametrize("legacy", [False, True], ids=["current-schema", "legacy-no-schema"])
def test_verified_dta_publication_uses_only_exact_sufficient_used_knowledge(
    retrieval_db, case, legacy,
):
    """双方言核消息API物理来源、事件镜像及重放；不足/无匹配不借同会话旧成功引用。"""
    db = retrieval_db
    _, _, kb, model = _seed(db)
    session = "sd8-citation-session"
    if case in {"no_match", "insufficient"}:
        _run(db, model, kb, "matched", session, legacy)
    instance, message, dynamic = _run(db, model, kb, case, session, legacy)
    operations = db.exec(select(SopOperation).where(SopOperation.instance_id == instance.id,
        SopOperation.operation_name == "knowledge.search")).all()
    results = [row.result_json["data"] for row in operations]
    expected_outcome = case if case in {"no_match", "insufficient"} else "evidence_found"
    assert any(row["outcome"] == expected_outcome for row in results)
    if case == "insufficient":
        assert results[-1]["evidence_sufficiency"]["satisfied"] is False
        assert results[-1]["chunks"]
    stored = message.metadata_json.get("knowledge_citations", [])
    read = message_read(message, db=db)
    citations = read.metadata.get("knowledge_citations", [])
    if case in {"no_match", "insufficient"}:
        assert stored == [] and citations == []
    else:
        assert citations and stored
        concept_ids = [row["concept_id"] for row in citations if row.get("concept_id")]
        chunk_ids = [row["chunk_id"] for row in citations if row.get("chunk_id")]
        assert concept_ids == ["sd8-physical-concept-2"]
        assert chunk_ids == ["kchunk_sd5_consumers_2"]
        assert all("sd5-new-fact" in row["excerpt"] for row in citations)
        assert all("sd5-old-fact" not in row["excerpt"] for row in citations)
        assert all("sd8-unrelated-fact" not in row["excerpt"] for row in citations)
        # 原Operation的逻辑键及完整原回执不能为修引用而改写。
        selected = [row for payload in results for row in payload["selected_concepts"]]
        assert any(row["concept_id"] == "sd8-same-logical-policy" for row in selected)
    old_message_id = message.id
    old_metadata = dict(message.metadata_json)
    repeated = dynamic.run_until_blocked_or_complete(execution_id=instance.id,
        model_config=model, worker_id="sd8-replay-worker", actor_user_id=USER_ID)
    assert repeated.status == "succeeded"
    assert db.get(Message, old_message_id).metadata_json == old_metadata
    same_execution = [row for row in db.exec(select(Message).where(Message.session_id == session,
        Message.role == "assistant")).all() if row.metadata_json.get("execution_id") == instance.id]
    assert [row.id for row in same_execution] == [old_message_id]


@pytest.mark.parametrize("case", ["legacy", "current", "wrong_type", "wrong_id", "wrong_agent",
    "wrong_tenant", "wrong_instance", "explicit_empty", "explicit_narrow", "no_schema_tool"])
def test_knowledge_projection_compatibility_preserves_schema_and_scope_boundaries(case):
    """旧知识兼容仅缺字段及精确冻结身份；显式schema和普通工具不获得回退放行。"""
    from app.db.models import SopInstance
    from app.dynamic_tasks.execution_context import _model_visible_operation_output

    instance = SopInstance(id="projection-instance", tenant_id=TENANT_ID, agent_id=AGENT_ID,
                           session_id="projection-session", kind="dynamic_task")
    snapshot = DynamicTaskAgent._knowledge_snapshot(tenant_id=TENANT_ID, agent_id=AGENT_ID,
        capability={"available": True, "required": True,
                    "knowledge_bases": [{"id": KB_ID, "version_id": PUBLISHED_VERSION_ID}]})
    raw = snapshot.model_dump(mode="json")
    if case != "current":
        raw["model_view"].pop("output_schema", None)
    if case == "wrong_type":
        raw["capability_type"] = "tool"
    if case == "wrong_id":
        raw["capability_id"] = "other"
    if case == "wrong_agent":
        raw["agent_id"] = "other"
    if case == "wrong_tenant":
        raw["tenant_id"] = "other"
    if case == "explicit_empty":
        raw["model_view"]["output_schema"] = {"type": "object", "additionalProperties": False}
    if case == "explicit_narrow":
        raw["model_view"]["output_schema"] = {"type": "object", "additionalProperties": False,
                                               "properties": {"outcome": {"type": "string"}}}
    if case == "no_schema_tool":
        raw.update(capability_type="tool", capability_id="other", name="ordinary.tool")
    data = {"outcome": "evidence_found", "degraded": False,
        "evidence_sufficiency": {"satisfied": True},
        "knowledge_base_version_ids": [PUBLISHED_VERSION_ID],
        "evidence_pack": [{"chunk_id": "chunk", "document_id": "doc", "content": "原业务证据",
                           "api_key": "synthetic-private-sideband"}],
        "api_key": "synthetic-private-sideband", "private_receipt": "audit-only",
        "trace": [{"authorization": "synthetic-private-sideband"}]}
    operation = SopOperation(id="projection-op", tenant_id=TENANT_ID,
        instance_id="other" if case == "wrong_instance" else instance.id,
        node_execution_id="projection-node", operation_name=raw["name"],
        idempotency_key="projection", logical_action_id="projection-action",
        request_fingerprint="projection", status="succeeded", result_json={"data": data})
    before = capability_checksum(raw)
    value = _model_visible_operation_output(operation, capability_by_name={raw["name"]: raw},
                                            instance=instance)
    assert capability_checksum(raw) == before and operation.result_json["data"] == data
    if case in {"legacy", "current"}:
        assert value["outcome"] == "evidence_found"
        assert value["knowledge_base_version_ids"] == [PUBLISHED_VERSION_ID]
        assert value["evidence_pack"] == [{"chunk_id": "chunk", "document_id": "doc",
                                           "content": "原业务证据"}]
        assert "synthetic-private-sideband" not in str(value) and "private_receipt" not in value
    elif case == "explicit_empty":
        assert value == {}
    elif case == "explicit_narrow":
        assert value == {"outcome": "evidence_found"}
    else:
        assert value is None


@pytest.mark.parametrize("legacy", [False, True], ids=["current-schema", "legacy-no-schema"])
def test_dta_answer_projection_withholds_partially_insufficient_knowledge(legacy):
    """即使部分方面命中，只要必需证据整体不足，答案模型也不能收到候选正文。"""
    from app.db.models import SopInstance
    from app.dynamic_tasks.execution_context import _model_visible_operation_output

    instance = SopInstance(id="partial-projection-instance", tenant_id=TENANT_ID, agent_id=AGENT_ID,
                           session_id="partial-projection-session", kind="dynamic_task")
    snapshot = DynamicTaskAgent._knowledge_snapshot(tenant_id=TENANT_ID, agent_id=AGENT_ID,
        capability={"available": True, "required": True,
                    "knowledge_bases": [{"id": KB_ID, "version_id": PUBLISHED_VERSION_ID}]})
    raw = snapshot.model_dump(mode="json")
    if legacy:
        raw["model_view"].pop("output_schema", None)
    data = {
        "outcome": "insufficient",
        "degraded": False,
        "knowledge_base_ids": [KB_ID],
        "knowledge_base_version_ids": [PUBLISHED_VERSION_ID],
        "evidence_sufficiency": {
            "required": True,
            "satisfied": False,
            "evidence_count": 3,
            "aligned_evidence_count": 3,
            "required_aspects": ["ZIRCON999UNKNOWN 政策额度", "ZIRCON999UNKNOWN 截止时间"],
            "covered_aspects": ["ZIRCON999UNKNOWN 政策额度"],
            "max_alignment_score": 1.2,
            "alignment_threshold": 2.0,
            "reason": "desired_evidence_not_aligned",
        },
        "selected_documents": [{"id": "partial-doc", "tenant_id": TENANT_ID,
            "knowledge_base_id": KB_ID, "knowledge_base_version_id": PUBLISHED_VERSION_ID,
            "document_id": "partial-doc", "title": "其他政策", "summary": "已知政策1500元"}],
        "selected_concepts": [{"id": "partial-concept", "tenant_id": TENANT_ID,
            "knowledge_base_id": KB_ID, "knowledge_base_version_id": PUBLISHED_VERSION_ID,
            "document_id": "partial-doc", "concept_id": "CYPRESS42", "type": "Business Rule",
            "title": "其他政策", "content": "已知政策1500元"}],
        "okf_citations": [{"concept_id": "CYPRESS42", "title": "其他政策",
                           "label": "已知政策1500元"}],
        "selected_buckets": [],
        "evidence_pack": [],
        "chunks": [],
    }
    operation = SopOperation(tenant_id=TENANT_ID, instance_id=instance.id,
        node_execution_id="partial-projection-node", operation_name="knowledge.search",
        idempotency_key="partial-projection", logical_action_id="partial-projection-action",
        request_fingerprint="partial-projection", status="succeeded", result_json={"data": data})
    original_checksum = capability_checksum(data)

    visible = _model_visible_operation_output(operation,
        capability_by_name={"knowledge.search": raw}, instance=instance)

    assert isinstance(visible, dict)
    assert visible["outcome"] == "insufficient"
    assert visible["evidence_sufficiency"]["satisfied"] is False
    assert all(visible[field] == [] for field in (
        "chunks", "evidence_pack", "okf_citations", "selected_buckets",
        "selected_concepts", "selected_documents",
    ))
    assert "1500元" not in str(visible)
    assert capability_checksum(data) == original_checksum


def test_actual_sop_knowledge_message_read_keeps_physical_revision(retrieval_db):
    """真实SOP知识节点经原消息引用读取不得把新版同逻辑概念补成旧版正文。"""
    from app.core.agent_loop import AgentLoop
    from app.session.session_schema import ChatTurnRequest, StepAgentResult
    from app.sop_runtime.coordinator import DeterministicSopCoordinator
    from app.db.models import ChatSession, Skill

    db = retrieval_db
    _seed(db)
    chat = db.get(ChatSession, "session_sd5_sop_consumer")
    skill = db.exec(select(Skill).where(Skill.tenant_id == TENANT_ID)).one()
    loop = AgentLoop(db)
    prepared = DeterministicSopCoordinator(db).prepare_step(chat, skill, StepAgentResult())
    result = loop._execute_knowledge_query_cycle(ChatTurnRequest(tenant_id=TENANT_ID,
        session_id=chat.id, user_id=USER_ID, agent_id=AGENT_ID, message="查询当前发布知识"),
        chat, skill, [], None, prepared)
    db.commit()
    operation = db.exec(select(SopOperation).where(SopOperation.operation_name == "knowledge.search"))
    assert operation.one().status == "succeeded"
    assert result.knowledge_results
    message = loop._append_message(TENANT_ID, chat.id, "assistant", "当前知识回执。",
                                    metadata=loop._assistant_message_metadata(result, chat))
    db.commit()
    stored = message.metadata_json["knowledge_citations"]
    read = message_read(message, db=db)
    concepts = [row for row in read.metadata["knowledge_citations"] if row.get("concept_id")]
    assert [row["concept_id"] for row in concepts] == ["sd8-physical-concept-2"]
    assert all("sd5-new-fact" in row["excerpt"] and "sd5-old-fact" not in row["excerpt"]
               for row in concepts)
    assert [row["concept_id"] for row in stored if row.get("concept_id")] == [
        "sd8-physical-concept-2"]
    assert result.knowledge_results[0]["selected_concepts"][0]["concept_id"] == (
        "sd8-same-logical-policy")


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
def test_okf_only_evidence_remains_visible_with_its_original_source(retrieval_db, legacy):
    """真实无文档概念命中保持完整OKF来源，不能只传充分性而丢弃证明该状态的引用。"""
    from app.db.models import KnowledgeBase, KnowledgeBaseVersion, SopInstance, Tenant
    from app.dynamic_tasks.execution_context import _model_visible_operation_output
    from app.knowledge.schema import KnowledgeSearchRequest

    db = retrieval_db
    db.add(Tenant(id="okf-owner", name="Synthetic OKF owner"))
    db.add(KnowledgeBase(id="okf-kb", tenant_id="okf-owner", name="Standalone OKF"))
    db.add(KnowledgeBaseVersion(id="okf-version", tenant_id="okf-owner", knowledge_base_id="okf-kb",
                               name="Standalone OKF", version="1.0.0", publication_state="published"))
    db.add(KnowledgeConcept(id="okf-physical", tenant_id="okf-owner", knowledge_base_id="okf-kb",
        knowledge_base_version_id="okf-version", concept_id="ORCHID_OKF", concept_type="Business Rule",
        title="ORCHID_OKF", content_md="ORCHID_OKF 独立业务事实。",
        citations_json=[{"target": "synthetic-policy.md", "label": "合成原始制度",
                         "authorization": "synthetic-private-sideband"}]))
    db.commit()
    response = KnowledgeService(db).search(KnowledgeSearchRequest(tenant_id="okf-owner",
        knowledge_base_ids=["okf-kb"], knowledge_base_version_ids=["okf-version"],
        query="ORCHID_OKF", desired_evidence="来源"))
    assert response.outcome == "evidence_found" and response.evidence_sufficiency.satisfied
    assert response.evidence_pack == [] and response.selected_documents == []
    data = response.model_dump(mode="json")
    snapshot = DynamicTaskAgent._knowledge_snapshot(tenant_id="okf-owner", agent_id="okf-agent",
        capability={"available": True, "knowledge_bases": [{"id": "okf-kb", "version_id": "okf-version"}]})
    raw = snapshot.model_dump(mode="json")
    if legacy:
        raw["model_view"].pop("output_schema", None)
    instance = SopInstance(id="okf-instance", tenant_id="okf-owner", agent_id="okf-agent",
                           session_id="okf-session", kind="dynamic_task")
    operation = SopOperation(tenant_id=instance.tenant_id, instance_id=instance.id,
        node_execution_id="okf-node", operation_name="knowledge.search", status="succeeded",
        idempotency_key="okf", logical_action_id="okf-action", request_fingerprint="okf",
        result_json={"data": data})
    before = capability_checksum(data)
    visible = _model_visible_operation_output(operation, capability_by_name={"knowledge.search": raw},
                                              instance=instance)
    assert visible["okf_citations"] == response.okf_citations
    assert visible["selected_concepts"][0]["citations"] == [{"target": "synthetic-policy.md",
                                                              "label": "合成原始制度"}]
    assert "synthetic-private-sideband" not in str(visible)
    assert capability_checksum(data) == before


def test_shared_concept_citation_preserves_legacy_result_without_physical_id():
    """新引用优先物理身份时，升级前只有逻辑ID的原结果仍能生成同形引用。"""
    from app.knowledge.citations import knowledge_citations_from_results

    raw = {"selected_concepts": [{"concept_id": "legacy-policy", "title": "合成旧概念",
                                  "content": "合成旧概念的原始正文"}]}
    before = capability_checksum(raw)
    citations = knowledge_citations_from_results([raw])
    assert citations[0]["concept_id"] == "legacy-policy"
    assert citations[0]["excerpt"] == "合成旧概念的原始正文"
    assert capability_checksum(raw) == before
