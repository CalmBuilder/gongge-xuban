"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_source.py
@CallChain  : Coordinator真实实例 → AgentLoop回复快照 → Message落盘 → FeedbackSource
@Description: 用双方言真实存储验证原轮、等待恢复、冻结回执与缺失来源边界。
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession, Message, SopInstance, SopOperation
from app.feedback.source import feedback_source
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.tools.tool_schema import ToolResult
from tests.test_executions_api import _seed_execution
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_sop_coordinator import _seed

source_engine = _feedback_engine


def _turn(db: Session, chat: ChatSession, number: int) -> Message:
    """持久化顺序明确的合成用户轮，避免 MySQL 时间精度影响最新输入选择。"""

    row = Message(
        id=f"source-turn-{number}",
        tenant_id=chat.tenant_id,
        session_id=chat.id,
        role="user",
        content="合成反馈来源输入",
        created_at=datetime(2026, 10, 2, tzinfo=UTC) + timedelta(seconds=number),
    )
    db.add(row)
    db.commit()
    return row


def _reply(
    db: Session, chat: ChatSession, turn: Message, result: StepAgentResult | None
) -> Message:
    """调用真实 AgentLoop 元数据入口并提交回复，以原消息而非测试自制快照解源。"""

    loop = AgentLoop(db)
    metadata = loop._assistant_message_metadata(result, chat, user_message_id=turn.id)
    metadata.update(turn_id=turn.id, user_message_id=turn.id)
    row = loop._append_message(
        chat.tenant_id,
        chat.id,
        "assistant",
        "合成回复",
        metadata=metadata,
        message_id=f"source-answer-{turn.id}",
    )
    db.commit()
    db.refresh(row)
    return row


def test_source_prepare_and_wait_resume_bind_same_frozen_execution(source_engine):
    """首次及等待恢复轮绑定同一真实实例，之后当前技能切换不污染原发布身份。"""

    with Session(source_engine) as db:
        skill, chat = _seed(db)
        coordinator = DeterministicSopCoordinator(db)
        first_turn = _turn(db, chat, 1)
        first_result = coordinator.prepare_step(chat, skill, StepAgentResult(reply="请提供员工号"))
        first = _reply(db, chat, first_turn, first_result)
        first_source = feedback_source(db, first)
        assert first_source.status == "verified"
        assert first_source.branch == "sop"
        assert first_source.turn_id == first_turn.id
        assert first_source.skill_version_id == "skillver_quota_200"
        assert first_source.skill_version == "2.0.0"

        second_turn = _turn(db, chat, 2)
        resumed_result = coordinator.prepare_step(chat, skill, StepAgentResult(reply="仍需员工号"))
        second = _reply(db, chat, second_turn, resumed_result)
        resumed_source = feedback_source(db, second)
        assert resumed_source.status == "verified"
        assert resumed_source.execution_id == first_source.execution_id
        assert resumed_source.turn_id == second_turn.id
        instance = db.get(SopInstance, resumed_source.execution_id)
        assert instance.source_ref == first_turn.id
        skill.version = "9.0.0"
        chat.active_skill_id = "unrelated-later-skill"
        db.add(skill)
        db.add(chat)
        db.commit()
        first_id, second_id = first.id, second.id

    with Session(source_engine) as reader:
        old = feedback_source(reader, reader.get(Message, first_id))
        restored = feedback_source(reader, reader.get(Message, second_id))
        assert old.model_dump() == first_source.model_dump()
        assert restored.model_dump() == resumed_source.model_dump()


def test_model_execution_identity_cannot_override_runtime(source_engine):
    """模型 JSON 声明执行 ID 与私有属性均不能设置真实 Runtime 赋值的身份。"""

    forged = StepAgentResult.model_validate(
        {"reply": "请提供员工号", "execution_id": "forged-id", "_runtime_execution_id": "forged-id"}
    )
    assert getattr(forged, "_runtime_execution_id", None) is None
    assert "execution_id" not in forged.model_dump()
    with Session(source_engine) as db:
        skill, chat = _seed(db)
        turn = _turn(db, chat, 1)
        prepared = DeterministicSopCoordinator(db).prepare_step(chat, skill, forged)
        reply = _reply(db, chat, turn, prepared)
        source = feedback_source(db, reply)
        instance = db.exec(select(SopInstance).where(SopInstance.session_id == chat.id)).one()
        assert source.status == "verified"
        assert source.execution_id == instance.id
        assert source.execution_id != "forged-id"


def test_source_zero_operations_and_later_receipts_remain_frozen(source_engine):
    """真实零调用与领取中的回执固化到各自回复，完成不改旧消息事实。"""

    with Session(source_engine) as db:
        skill, chat = _seed(db)
        coordinator = DeterministicSopCoordinator(db)
        first_turn = _turn(db, chat, 1)
        waiting = coordinator.prepare_step(chat, skill, StepAgentResult(reply="请提供员工号"))
        first = _reply(db, chat, first_turn, waiting)
        first_snapshot = first.metadata_json["feedback_source"]
        assert first_snapshot["operations"] == []

        second_turn = _turn(db, chat, 2)
        chat.slots_json = {"employee_id": "E001"}
        calling = coordinator.prepare_step(
            chat, skill, StepAgentResult(slot_updates={"employee_id": "E001"})
        )
        second = _reply(db, chat, second_turn, calling)
        second_snapshot = second.metadata_json["feedback_source"]
        operation = db.exec(select(SopOperation)).one()
        assert len(second_snapshot["operations"]) == 1
        assert second_snapshot["operations"][0]["id"] == operation.id
        assert operation.status == "running"
        assert second_snapshot["operations"][0]["status"] == operation.status
        assert second_snapshot["operations"][0]["node_execution_id"] == operation.node_execution_id
        coordinator.record_tool_result(
            chat,
            calling.tool_call,
            ToolResult(
                tool_name="expense.quota_query", success=True, data={"remaining": 20000.0}
            ),
        )
        db.commit()
        assert operation.status == "succeeded"
        first_id, second_id = first.id, second.id

    with Session(source_engine) as reader:
        old_waiting = reader.get(Message, first_id)
        old_calling = reader.get(Message, second_id)
        assert old_waiting.metadata_json["feedback_source"] == first_snapshot
        assert old_calling.metadata_json["feedback_source"] == second_snapshot
        assert feedback_source(reader, old_waiting).status == "verified"
        assert feedback_source(reader, old_calling).status == "verified"


def test_missing_source_does_not_guess_active_execution_or_zero_calls(source_engine):
    """旧回复无受信实例关联时保留缺失与未知调用，不借当前活动实例伪造来源。"""

    with Session(source_engine) as db:
        skill, chat = _seed(db)
        initial = _turn(db, chat, 1)
        DeterministicSopCoordinator(db).prepare_step(chat, skill, StepAgentResult())
        db.commit()
        assert db.exec(select(SopInstance)).one().source_ref == initial.id
        later_turn = _turn(db, chat, 2)
        missing = _reply(db, chat, later_turn, None)
        assert feedback_source(db, missing).status == "missing"
        assert feedback_source(db, missing).skill_version_id is None
        assert missing.metadata_json["feedback_source"]["operations"] is None


def test_dynamic_feedback_keeps_execution_without_sop_identity(source_engine):
    """现有动态 Execution 回复可证明 DTA 来源，但不得填充任何 SOP 发布字段。"""

    with Session(source_engine) as db:
        _, _, instance = _seed_execution(db, suffix="feedback_source")
        chat = ChatSession(
            id=instance.session_id,
            tenant_id=instance.tenant_id,
            user_id=instance.initiator_user_id,
            agent_id=instance.agent_id,
        )
        db.add(chat)
        db.commit()
        turn = _turn(db, chat, 1)
        instance.source_ref = turn.id
        db.add(instance)
        loop = AgentLoop(db)
        message = loop._persist_dynamic_waiting_message(
            chat_session=chat,
            execution_id=instance.id,
            blocking_step_key="clarify",
            user_message_id=turn.id,
        )
        db.commit()
        source = feedback_source(db, message)
        assert source.status == "verified"
        assert source.branch == "dynamic_task"
        assert source.execution_id == instance.id
        assert source.skill_id is None
        assert source.skill_version_id is None
        assert source.skill_version is None
        assert source.definition_checksum is None


def test_dynamic_resumed_wait_reply_keeps_original_execution(source_engine):
    """DTA 下一轮进入新等待步骤仍绑定原实例，而不要求 source_ref 改成恢复轮。"""

    with Session(source_engine) as db:
        _, _, instance = _seed_execution(db, suffix="feedback_resume")
        chat = ChatSession(
            id=instance.session_id,
            tenant_id=instance.tenant_id,
            user_id=instance.initiator_user_id,
            agent_id=instance.agent_id,
        )
        db.add(chat)
        db.commit()
        first_turn = _turn(db, chat, 1)
        instance.source_ref = first_turn.id
        db.add(instance)
        loop = AgentLoop(db)
        first_message = loop._persist_dynamic_waiting_message(
            chat_session=chat,
            execution_id=instance.id,
            blocking_step_key="clarify",
            user_message_id=first_turn.id,
        )
        db.commit()
        first_source = feedback_source(db, first_message)
        assert first_source.status == "verified"

        second_turn = _turn(db, chat, 2)
        resumed_message = loop._persist_dynamic_waiting_message(
            chat_session=chat,
            execution_id=instance.id,
            blocking_step_key="confirm",
            user_message_id=second_turn.id,
        )
        db.commit()
        assert resumed_message.id != first_message.id
        assert instance.source_ref == first_turn.id
        restored = feedback_source(db, resumed_message)
        assert restored.status == "verified"
        assert restored.branch == "dynamic_task"
        assert restored.execution_id == first_source.execution_id
        assert restored.turn_id == second_turn.id
        assert restored.skill_version_id is None
        assert resumed_message.metadata_json["feedback_source"]["turn_id"] == second_turn.id
        assert resumed_message.metadata_json["feedback_source"]["operations"] == []
        reply_id, instance_id = resumed_message.id, instance.id

    with Session(source_engine) as reader:
        restarted = feedback_source(reader, reader.get(Message, reply_id))
        assert restarted.status == "verified"
        assert restarted.execution_id == instance_id
        assert restarted.branch == "dynamic_task"
        assert restarted.skill_version_id is None


def test_source_rejects_cross_session_execution_and_wrong_turn_snapshot(source_engine):
    """持久关联如会话或原轮不一致必须 fail closed，禁止从相邻会话补执行。"""

    with Session(source_engine) as db:
        skill, chat = _seed(db)
        turn = _turn(db, chat, 1)
        result = DeterministicSopCoordinator(db).prepare_step(chat, skill, StepAgentResult())
        reply = _reply(db, chat, turn, result)
        assert feedback_source(db, reply).status == "verified"
        snapshot = dict(reply.metadata_json["feedback_source"])
        snapshot["turn_id"] = "different-turn"
        reply.metadata_json = {**reply.metadata_json, "feedback_source": snapshot}
        db.add(reply)
        db.commit()
        assert feedback_source(db, reply).status == "missing"
        snapshot["turn_id"] = turn.id
        reply.metadata_json = {**reply.metadata_json, "feedback_source": snapshot}
        instance = db.get(SopInstance, snapshot["execution_id"])
        instance.session_id = "different-session"
        db.add(instance)
        db.add(reply)
        db.commit()
        assert feedback_source(db, reply).status == "missing"
