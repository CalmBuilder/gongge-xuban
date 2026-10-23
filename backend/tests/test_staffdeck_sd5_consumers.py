"""
@Time       : 2026/09/29
@Author     : zhanglp8181
@File       : test_staffdeck_sd5_consumers.py
@CallChain  : pytest → AgentLoop/DynamicTaskAgent → KnowledgeService → 已发布知识版本
@Description: 验证 SD-5 版本重建后的真实 SOP 与 DTA 消费者只读取绑定的 published head。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_private_knowledge_branch
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    ChatSession,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    ModelConfig,
    Skill,
    SkillVersion,
    SopOperation,
    Tenant,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
)
from app.knowledge.service import KnowledgeService
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card


TENANT_ID = "tenant_sd5_consumers"
USER_ID = "user_sd5_consumers"
AGENT_ID = "agent_sd5_consumers"
KB_ID = "kb_sd5_consumers"
PUBLISHED_VERSION_ID = "kbver_sd5_consumers_2"


def _session() -> Session:
    """创建包含完整生产表的隔离 SQLite 会话。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _skill_content() -> dict[str, Any]:
    """构造一个真实确定性知识节点，要求检索成功后进入终态。"""

    return {
        "skill_id": "skill_sd5_consumer",
        "name": "SD5 知识消费者验证",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "condition_schemas": {
            "node_output": {
                "type": "object",
                "properties": {
                    "policy_evidence": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {"outcome": {"type": "string"}},
                            },
                        },
                    }
                },
            }
        },
        "nodes": [
            {
                "node_id": "search_policy",
                "type": "knowledge_query",
                "name": "读取当前正文",
                "instruction": "检索 SD5 当前发布正文中的新事实。",
                "allowed_actions": ["knowledge_query"],
                "metadata": {
                    "operation_result_key": "policy_evidence",
                    "knowledge_query": {
                        "query_type": "policy_check",
                        "desired_evidence": "当前发布正文的新事实",
                    },
                },
            },
            {"node_id": "done", "type": "terminal", "name": "完成"},
            {"node_id": "manual", "type": "terminal", "name": "人工复核"},
        ],
        "edges": [
            {
                "source_node_id": "search_policy",
                "next_node_id": "done",
                "condition": {
                    "op": "eq",
                    "left": {"path": "node_output.policy_evidence.data.outcome"},
                    "right": {"value": "evidence_found"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "search_policy",
                "next_node_id": "manual",
                "condition": {"op": "always"},
                "priority": 0,
            },
        ],
        "start_node_id": "search_policy",
        "terminal_node_ids": ["done", "manual"],
    }


def _seed_consumer_fixture(db: Session) -> tuple[User, AgentProfile, KnowledgeBase, Skill, ChatSession]:
    """写入旧版与新版 published 物理快照，并把 Agent 分支冻结到新版。"""

    user = User(
        id=USER_ID,
        tenant_id=TENANT_ID,
        username="sd5-consumer",
        password_hash="test-only",
        role="member",
    )
    agent = AgentProfile(
        id=AGENT_ID,
        tenant_id=TENANT_ID,
        name="SD5 消费者 Agent",
        owner_user_id=USER_ID,
        status="active",
    )
    knowledge_base = KnowledgeBase(
        id=KB_ID,
        tenant_id=TENANT_ID,
        name="SD5 当前知识",
        owner_user_id=USER_ID,
        access_scope="owner",
        metadata_json={"current_version": "2.0.0"},
    )
    old_version = KnowledgeBaseVersion(
        id="kbver_sd5_consumers_1",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        version="1.0.0",
        name=knowledge_base.name,
        publication_state="published",
    )
    new_version = KnowledgeBaseVersion(
        id=PUBLISHED_VERSION_ID,
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        version="2.0.0",
        name=knowledge_base.name,
        publication_state="published",
    )
    old_document = KnowledgeDocument(
        id="kdoc_sd5_consumers_1",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=old_version.id,
        lineage_id="lineage_sd5_consumers",
        filename="policy.md",
        file_type="md",
        title="SD5 当前知识",
        status="ready",
        normalized_source="# 旧正文\n旧事实 sd5-old-fact",
        source_kind="normalized",
        metadata_json={
            "document_card": {"title": "SD5 当前知识", "summary": "旧事实 sd5-old-fact"},
            "section_tree": [
                {"section_id": "policy", "path": "旧制度", "title": "旧制度", "content": "旧事实 sd5-old-fact"}
            ],
        },
    )
    new_document = KnowledgeDocument(
        id="kdoc_sd5_consumers_2",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=new_version.id,
        lineage_id="lineage_sd5_consumers",
        parent_document_id=old_document.id,
        filename="policy.md",
        file_type="md",
        title="SD5 当前知识",
        status="ready",
        normalized_source="# 新正文\n新事实 sd5-new-fact",
        source_kind="normalized",
        metadata_json={
            "document_card": {"title": "SD5 当前知识", "summary": "新事实 sd5-new-fact"},
            "section_tree": [
                {"section_id": "policy", "path": "当前制度", "title": "当前制度", "content": "新事实 sd5-new-fact"}
            ],
        },
    )
    old_bucket = KnowledgeBucket(
        id="kbucket_sd5_consumers_1",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=old_version.id,
        document_id=old_document.id,
        bucket_key="policy",
        title="旧制度",
        summary="旧事实 sd5-old-fact",
        metadata_json={
            "content": "旧事实 sd5-old-fact",
            "section_ids": ["policy"],
            "section_paths": ["旧制度"],
        },
    )
    new_bucket = KnowledgeBucket(
        id="kbucket_sd5_consumers_2",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=new_version.id,
        document_id=new_document.id,
        bucket_key="policy",
        title="当前制度",
        summary="新事实 sd5-new-fact",
        metadata_json={
            "content": "新事实 sd5-new-fact",
            "section_ids": ["policy"],
            "section_paths": ["当前制度"],
        },
    )
    old_chunk = KnowledgeChunk(
        id="kchunk_sd5_consumers_1",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=old_version.id,
        document_id=old_document.id,
        bucket_id=old_bucket.id,
        chunk_index=0,
        content="旧事实 sd5-old-fact",
        summary="旧事实",
        source_ref="policy.md / 旧制度",
    )
    new_chunk = KnowledgeChunk(
        id="kchunk_sd5_consumers_2",
        tenant_id=TENANT_ID,
        knowledge_base_id=KB_ID,
        knowledge_base_version_id=new_version.id,
        document_id=new_document.id,
        bucket_id=new_bucket.id,
        chunk_index=0,
        content="新事实 sd5-new-fact",
        summary="新事实",
        source_ref="policy.md / 当前制度",
    )
    content = _skill_content()
    definition = compile_legacy_skill_card(content)
    skill = Skill(
        tenant_id=TENANT_ID,
        skill_id="skill_sd5_consumer",
        version="1.0.0",
        name="SD5 知识消费者验证",
        content_json=content,
        status="published",
    )
    skill_version = SkillVersion(
        id="skillver_sd5_consumer",
        tenant_id=TENANT_ID,
        skill_id=skill.skill_id,
        version=skill.version,
        name=skill.name,
        content_json=content,
        status="published",
        compiled_definition_checksum=definition.checksum,
    )
    chat_session = ChatSession(
        id="session_sd5_sop_consumer",
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id=AGENT_ID,
        active_skill_id=skill.skill_id,
        active_step_id="search_policy",
        slots_json={},
    )
    db.add_all(
        [
            Tenant(id=TENANT_ID, name="SD5 consumers"),
            user,
            agent,
            knowledge_base,
            old_version,
            new_version,
            old_document,
            new_document,
            old_bucket,
            new_bucket,
            old_chunk,
            new_chunk,
            skill,
            skill_version,
            chat_session,
        ]
    )
    db.flush()
    ensure_agent_private_knowledge_branch(
        db,
        TENANT_ID,
        AGENT_ID,
        knowledge_base,
        metadata_json={"owner_user_id": USER_ID},
    )
    db.commit()
    return user, agent, knowledge_base, skill, chat_session


class _KnowledgePlanner:
    """构造必须经过真实知识检索再回答的动态任务计划。"""

    def create_plan(self, *, goal, success_criteria, capabilities, input_resources=()):
        """确认规划器只看版本卡片，并返回 knowledge 到 answer 的有界图。"""

        knowledge = [item for item in capabilities if item.capability_type == "knowledge"]
        assert len(knowledge) == 1
        assert PUBLISHED_VERSION_ID in knowledge[0].model_dump_json()
        assert "sd5-new-fact" not in knowledge[0].model_dump_json()
        return NormalizedPlan(
            goal=goal,
            success_criteria=tuple(success_criteria),
            steps=(
                PlanStep(
                    step_key="search_policy",
                    title="检索当前知识",
                    kind="knowledge",
                    capability_refs=("knowledge.search",),
                ),
                PlanStep(
                    step_key="answer",
                    title="回答",
                    kind="answer",
                    depends_on=("search_policy",),
                ),
            ),
            budget={"max_steps": 4},
        )


class _KnowledgeProposer:
    """为动态知识步骤返回严格动作，并在回答中引用检索步骤。"""

    def propose(self, *, view, step):
        """根据冻结步骤返回 query_knowledge 或 answer 提案。"""

        if step.kind == "knowledge":
            return CompletedProviderProposal(
                response_id="sd5-provider-search",
                finish_reason="stop",
                proposal=RuntimeActionProposal(
                    action_kind=ActionKind.QUERY_KNOWLEDGE,
                    capability_ref="knowledge.search",
                    arguments={
                        "query": "sd5-new-fact",
                        "desired_evidence": "新事实 sd5-new-fact",
                    },
                    rationale="必须读取当前发布版本",
                ),
            )
        return CompletedProviderProposal(
            response_id="sd5-provider-answer",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "已依据当前发布知识作答。",
                    "criterion_evidence": {"criterion_01": ["search_policy"]},
                    "pending_questions": [],
                },
                rationale="知识证据已完成核验",
            ),
        )


class _Catalog:
    """给真实 DynamicTaskAgent 提供最小模型目录，不替换知识服务。"""

    def __init__(self, model: ModelConfig) -> None:
        """保存受控模型，保持动态入口的租户校验。"""

        self.model = model

    def require_dynamic_model(self, tenant_id: str, model_config_id: str) -> ModelConfig:
        """返回同租户模型。"""

        assert tenant_id == self.model.tenant_id
        assert model_config_id == self.model.id
        return self.model

    def list_tools(self, tenant_id: str, agent_id: str) -> list[CapabilitySnapshot]:
        """动态消费者不暴露额外工具。"""

        return []

    def list_connector_reads(self, tenant_id: str, agent_id: str, actor_user_id: str) -> list[CapabilitySnapshot]:
        """动态消费者不暴露连接器读取。"""

        return []

    def list_connector_writes(
        self,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
        session_id: str,
        **kwargs: object,
    ) -> list[CapabilitySnapshot]:
        """动态消费者不暴露连接器写入。"""

        return []

    def list_general_skills(self, tenant_id: str, agent_id: str, actor_user_id: str) -> list[CapabilitySnapshot]:
        """动态消费者不加载指导 Skill。"""

        return []


def test_sd5_real_sop_agent_loop_consumes_published_revision() -> None:
    """真实 AgentLoop→KnowledgeService 必须返回新版事实且不得混入旧版。"""

    with _session() as db:
        _, _, _, skill, chat_session = _seed_consumer_fixture(db)
        step = DeterministicSopCoordinator(db).prepare_step(
            chat_session,
            skill,
            StepAgentResult(),
        )
        result = AgentLoop(db)._execute_knowledge_query_cycle(
            ChatTurnRequest(
                tenant_id=TENANT_ID,
                session_id=chat_session.id,
                user_id=USER_ID,
                agent_id=AGENT_ID,
                message="查询当前发布知识",
            ),
            chat_session,
            skill,
            [],
            None,
            step,
        )
        db.commit()
        operation = db.exec(select(SopOperation)).one()

    assert result.knowledge_results
    serialized = str(result.knowledge_results[0])
    assert "sd5-new-fact" in serialized
    assert "sd5-old-fact" not in serialized
    assert PUBLISHED_VERSION_ID in serialized
    assert operation.status == "succeeded"


def test_sd5_real_dynamic_agent_consumes_published_revision() -> None:
    """真实 DynamicTaskAgent→KnowledgeService 必须冻结并读取新版事实。"""

    with _session() as db:
        user, agent, knowledge_base, _, _ = _seed_consumer_fixture(db)
        model = ModelConfig(
            id="model_sd5_consumer",
            tenant_id=TENANT_ID,
            name="SD5 consumer model",
            api_key_encrypted="test-only",
            model="test-model",
            capability_snapshot_json={
                "protocol_version": "dynamic-v1",
                "sdk_available": True,
                "credentials_verified": True,
                "structured_output": True,
                "tool_calling": True,
            },
            preflight_status="ready",
        )
        db.add(model)
        db.commit()
        dynamic = DynamicTaskAgent(
            db,
            catalog=_Catalog(model),
            planner=_KnowledgePlanner(),
            action_proposer=_KnowledgeProposer(),
            knowledge_service=KnowledgeService(db),
        )
        instance, _ = dynamic.start_task(
            tenant_id=TENANT_ID,
            session_id="session_sd5_dta_consumer",
            agent_id=agent.id,
            initiator_user_id=user.id,
            goal="依据当前发布知识回答",
            success_criteria=("必须使用当前发布正文",),
            model_config=model,
            knowledge_capability={
                "available": True,
                "required": True,
                "knowledge_bases": [
                    {
                        "id": knowledge_base.id,
                        "version_id": PUBLISHED_VERSION_ID,
                        "version": "2.0.0",
                        "name": knowledge_base.name,
                        "description": knowledge_base.description,
                    }
                ],
            },
        )
        outcome = dynamic.run_until_blocked_or_complete(
            execution_id=instance.id,
            model_config=model,
            worker_id="sd5-consumer-worker",
            actor_user_id=user.id,
        )
        operations = db.exec(
            select(SopOperation).where(
                SopOperation.instance_id == instance.id,
                SopOperation.operation_name == "knowledge.search",
            )
        ).all()

    assert outcome.status == "succeeded"
    assert len(operations) == 1
    result_payload = operations[0].result_json or {}
    serialized = str(result_payload)
    assert "sd5-new-fact" in serialized
    assert "sd5-old-fact" not in serialized
    result_data = result_payload.get("data") or {}
    assert result_data["knowledge_base_version_ids"] == [PUBLISHED_VERSION_ID]
    frozen_cards = operations[0].capability_snapshot_json["model_view"]["knowledge_bases"]
    assert [card["version_id"] for card in frozen_cards] == [PUBLISHED_VERSION_ID]
