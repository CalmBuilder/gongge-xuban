"""
@Time       : 2026/07/27 00:00
@Author     : zhanglp8181
@File       : test_graph_visual_knowledge_sop.py
@CallChain  : pytest → 图结构演示 v2 定义/发布 → Scheduler/版本存储
@Description: 验证真实浏览器回归所用知识与工具分支采用 v5 契约且保持版本不可变。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.demo_sop_versions import (
    GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION,
    GRAPH_VISUAL_DEMO_SKILL_ID,
    _graph_visual_demo_knowledge_content,
    ensure_graph_visual_demo_knowledge_version,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    Skill,
    SkillVersion,
)
from app.sop_runtime.capabilities import DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action


def _definition():
    """编译浏览器知识回归使用的零告警 v5 定义。"""

    return compile_legacy_skill_card(_graph_visual_demo_knowledge_content({}))


def test_graph_visual_demo_builds_declared_knowledge_query_and_routes_receipt() -> None:
    """验证查询只来自声明输入，且持久回执状态决定成功或保守终态。"""

    definition = _definition()
    query = plan_next_action(
        definition,
        current_node_id="read_policy_knowledge",
        slots={
            "request_type": "knowledge",
            "request_detail": "员工年假资格和天数规则",
        },
    )
    succeeded = plan_next_action(
        definition,
        current_node_id="read_policy_knowledge",
        slots={},
        node_outputs={
            "graph_knowledge_result": {
                "status": "succeeded",
                "data": {"outcome": "evidence_found"},
            }
        },
    )
    failed = plan_next_action(
        definition,
        current_node_id="read_policy_knowledge",
        slots={},
        node_outputs={
            "graph_knowledge_result": {
                "status": "failed",
                "data": {"outcome": "no_match"},
            }
        },
    )

    assert definition.meta_model_version == 5
    assert definition.diagnostics == ()
    assert DEFAULT_CAPABILITY_REGISTRY.non_executable_nodes(definition) == ()
    assert query.action is RuntimeAction.QUERY_KNOWLEDGE
    assert query.operation_arguments["query_type"] == "policy_check"
    assert "policy_question: 员工年假资格和天数规则" in str(
        query.operation_arguments["query"]
    )
    assert succeeded.next_node_id == "reply_knowledge_success"
    assert failed.next_node_id == "reply_knowledge_failure"


def test_graph_visual_demo_publishes_new_snapshot_without_overwriting_v1() -> None:
    """验证迁移生成 v2 快照和派生关系，并保留旧发布内容。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        overall = AgentProfile(
            id="agent_overall_test",
            tenant_id="tenant_demo",
            name="整体智能体",
            is_overall=True,
        )
        hr_agent = AgentProfile(
            id="agent_hr_test",
            tenant_id="tenant_demo",
            name="人事",
        )
        old_content = {
            "skill_id": GRAPH_VISUAL_DEMO_SKILL_ID,
            "name": "图结构可视化验证流程",
            "version": "1.0.0",
            "nodes": [
                {
                    "node_id": "legacy_reply",
                    "type": "response",
                    "name": "旧版反馈",
                    "allowed_actions": ["answer_user"],
                }
            ],
            "edges": [],
            "start_node_id": "legacy_reply",
            "terminal_node_ids": ["legacy_reply"],
        }
        skill = Skill(
            tenant_id="tenant_demo",
            skill_id=GRAPH_VISUAL_DEMO_SKILL_ID,
            version="1.0.0",
            name="图结构可视化验证流程",
            content_json=old_content,
            status="published",
        )
        db.add(overall)
        db.add(hr_agent)
        db.add(skill)
        db.commit()

        ensure_graph_visual_demo_knowledge_version(db)
        db.commit()

        versions = db.exec(
            select(SkillVersion)
            .where(SkillVersion.skill_id == GRAPH_VISUAL_DEMO_SKILL_ID)
            .order_by(SkillVersion.version)
        ).all()
        db.refresh(skill)
        hr_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.agent_id == hr_agent.id,
                AgentSkillBranch.skill_id == GRAPH_VISUAL_DEMO_SKILL_ID,
            )
        ).one()
        hr_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == hr_agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == skill.id,
            )
        ).one()

    assert skill.version == GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION
    assert [version.version for version in versions] == [
        "1.0.0",
        GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION,
    ]
    assert versions[0].content_json == old_content
    assert versions[1].derived_from_version_id == versions[0].id
    assert versions[1].meta_model_version == 5
    assert hr_binding.status == "active"
    assert hr_binding.metadata_json["source"] == "demo_seed"
    assert hr_branch.head_version == GRAPH_VISUAL_DEMO_KNOWLEDGE_VERSION
    assert hr_branch.sync_state == "synced"
