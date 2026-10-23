"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_source_final_audit.py
@CallChain  : 独立来源审核 → 真实DTA结果发布/Runtime拒绝 → 原回复来源恢复
@Description: 验证最终成功、身份拒绝及同轮多个实例不能缺失或猜测执行来源。
"""

import pytest
from sqlmodel import Session, select

from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession, Message, ModelConfig, SopInstance, Tenant, User
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.capability_catalog import capability_checksum
from app.feedback.source import feedback_source
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from tests.test_dynamic_task_agent import (
    _Executor,
    _Planner,
    _RunProposer,
    _StartCatalog,
    _ensure_dynamic_agent,
    _model_capabilities,
    _snapshot,
)
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_source import _reply, _turn
from tests.test_sop_coordinator import _seed
from tests.test_sop_identity_context import _seed as _seed_identity

source_engine = _feedback_engine


@pytest.mark.parametrize("resumed", [False, True], ids=["initial", "resumed"])
def test_dynamic_success_message_freezes_actual_turn_and_receipt(source_engine, resumed):
    """真实read→answer成功落盘固化本轮来源，恢复轮不强求创建source_ref等于新轮。"""

    with Session(source_engine, expire_on_commit=False) as db:
        db.add(Tenant(id="tenant_demo", name="隔离动态来源"))
        _ensure_dynamic_agent(db)
        owner = User(id="user_demo", tenant_id="tenant_demo", username="source-owner",
                     password_hash="synthetic")
        chat = ChatSession(id="source-dynamic", tenant_id="tenant_demo", user_id=owner.id,
                           agent_id="agent_demo")
        db.add_all([owner, chat])
        db.commit()
        first_turn = _turn(db, chat, 1)
        capabilities = _model_capabilities()
        model = ModelConfig(id="source-dynamic-model", tenant_id=chat.tenant_id,
                            name="隔离协议模型", api_key_encrypted="synthetic",
                            model="model-demo", capability_snapshot_json=capabilities,
                            capability_checksum=capability_checksum(capabilities),
                            preflight_status="ready")
        db.add(model)
        db.commit()
        executor = _Executor()
        proposer = _RunProposer()
        agent = DynamicTaskAgent(db, catalog=_StartCatalog(model, _snapshot()),
                                 tool_executor=executor, planner=_Planner(),
                                 action_proposer=proposer)
        instance, _ = agent.start_task(tenant_id=chat.tenant_id, session_id=chat.id,
                                      agent_id=chat.agent_id, initiator_user_id=owner.id,
                                      goal="生成续约风险简报", success_criteria=("覆盖合同证据",),
                                      model_config=model, source_ref=first_turn.id)
        reply_turn = _turn(db, chat, 2) if resumed else first_turn
        outcome = agent.run_until_blocked_or_complete(
            execution_id=instance.id, model_config=model, worker_id="source-audit-worker",
            actor_user_id=owner.id, user_message_id=reply_turn.id)
        assert outcome.status == "succeeded"
        assert outcome.message is not None
        assert executor.calls == 1
        assert proposer.calls == 2
        assert instance.source_ref == first_turn.id
        message = outcome.message
        source = feedback_source(db, message)
        assert source.status == "verified"
        assert source.branch == "dynamic_task"
        assert source.turn_id == reply_turn.id
        assert source.execution_id == instance.id
        assert source.skill_id is None
        assert source.skill_version_id is None
        assert source.definition_checksum is None
        snapshot = message.metadata_json["feedback_source"]
        assert snapshot["turn_id"] == reply_turn.id
        assert snapshot["execution_id"] == instance.id
        assert instance.status == "succeeded"
        assert snapshot["execution_status"] == "succeeded"
        assert len(snapshot["operations"]) == 1
        assert snapshot["operations"][0]["status"] == "succeeded"
        reply_id = message.id
        db.commit()
    with Session(source_engine) as restored:
        persisted = restored.get(Message, reply_id)
        assert feedback_source(restored, persisted).model_dump() == source.model_dump()
        assert persisted.metadata_json["feedback_source"] == snapshot


def test_same_turn_multiple_executions_without_runtime_identity_are_missing(source_engine):
    """同一原轮的两个真实实例不能用最新排序猜回复来源，受信身份仍能准确解析。"""

    with Session(source_engine) as db:
        skill, chat = _seed(db)
        turn = _turn(db, chat, 1)
        coordinator = DeterministicSopCoordinator(db)
        first_result = coordinator.prepare_step(chat, skill, StepAgentResult())
        first_instance = db.get(SopInstance, first_result.runtime_execution_id())
        with coordinator._owned(first_instance):
            coordinator.store.fail_instance(first_instance)
        db.commit()
        second_result = coordinator.prepare_step(chat, skill, StepAgentResult())
        second_instance = db.get(SopInstance, second_result.runtime_execution_id())
        assert first_instance.id != second_instance.id
        assert first_instance.source_ref == second_instance.source_ref == turn.id
        ambiguous = _reply(db, chat, turn, None)
        assert feedback_source(db, ambiguous).status == "missing"
        assert ambiguous.metadata_json["feedback_source"]["operations"] is None
        metadata = AgentLoop(db)._assistant_message_metadata(second_result, chat,
                                                            user_message_id=turn.id)
        metadata.update(turn_id=turn.id, user_message_id=turn.id)
        trusted = Message(id="source-trusted-answer", tenant_id=chat.tenant_id,
                          session_id=chat.id, role="assistant", content="合成受信回复",
                          metadata_json=metadata)
        db.add(trusted)
        db.commit()
        assert feedback_source(db, trusted).execution_id == second_instance.id


def test_identity_rejection_reply_keeps_actual_failed_execution(source_engine):
    """本人代查拒绝保留真实失败实例及零外呼事实，不因失败支路漏传来源身份。"""

    with Session(source_engine) as db:
        skill, chat = _seed_identity(db, actor_role="member", requested_employee_id="E002")
        turn = _turn(db, chat, 1)
        result = DeterministicSopCoordinator(db).prepare_step(chat, skill, StepAgentResult())
        assert result.runtime_reply_metadata()["runtime_error_code"] == "SUBJECT_OVERRIDE_FORBIDDEN"
        instance = db.exec(select(SopInstance).where(SopInstance.session_id == chat.id)).one()
        assert instance.status == "failed"
        reply = _reply(db, chat, turn, result)
        source = feedback_source(db, reply)
        assert source.status == "verified"
        assert source.branch == "sop"
        assert source.execution_id == instance.id
        assert source.skill_version_id == "skillver_quota_220"
        assert reply.metadata_json["feedback_source"]["execution_status"] == "failed"
        assert reply.metadata_json["feedback_source"]["operations"] == []
