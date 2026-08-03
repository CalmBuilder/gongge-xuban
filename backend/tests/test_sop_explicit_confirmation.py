"""
@Time       : 2026/07/22 18:55
@Author     : zhanglp8181
@File       : test_sop_explicit_confirmation.py
@CallChain  : pytest → 确认策略编译/Coordinator → Scheduler/Operation
@Description: 验证明示确认只能来自当前轮白名单消息，且确认前不会产生工具副作用。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import ChatSession, Skill, SkillVersion, SopOperation
from app.db.demo_sop_versions import _meeting_room_deterministic_content
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import (
    SopCompilationError,
    compile_legacy_skill_card,
)
from app.sop_runtime.scheduler import plan_next_action


def _confirmation_content() -> dict[str, object]:
    """构造带有当前轮明确确认门禁的最小写操作定义。"""

    return {
        "skill_id": "confirmation_demo",
        "name": "明确确认验收",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "condition_schemas": {
            "slots": {
                "type": "object",
                "properties": {
                    "request": {"type": "string"},
                    "confirmation": {"type": "string"},
                },
            }
        },
        "nodes": [
            {
                "node_id": "collect_request",
                "type": "collect_info",
                "name": "收集请求",
                "expected_user_info": ["request"],
            },
            {
                "node_id": "confirm_request",
                "type": "collect_info",
                "name": "确认请求",
                "expected_user_info": ["confirmation"],
                "metadata": {
                    "confirmation_policy": {
                        "slot_name": "confirmation",
                        "phrase_values": {
                            "确认提交": "confirmed",
                            "取消提交": "cancelled",
                        },
                        "prompt": "请回复“确认提交”或“取消提交”。",
                    }
                },
            },
            {
                "node_id": "call_operation",
                "type": "tool_call",
                "name": "执行写操作",
                "allowed_actions": ["call_tool:demo.write"],
                "metadata": {
                    "operation_input": {"request": "slots.request"},
                    "operation_result_key": "demo_write",
                },
            },
            {
                "node_id": "completed",
                "type": "response",
                "name": "完成",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "cancelled",
                "type": "response",
                "name": "取消",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {"source_node_id": "collect_request", "next_node_id": "confirm_request"},
            {
                "source_node_id": "confirm_request",
                "next_node_id": "call_operation",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.confirmation"},
                    "right": {"value": "confirmed"},
                },
                "priority": 100,
            },
            {
                "source_node_id": "confirm_request",
                "next_node_id": "cancelled",
                "condition": {"op": "always"},
                "priority": 0,
            },
            {"source_node_id": "call_operation", "next_node_id": "completed"},
        ],
        "start_node_id": "collect_request",
        "terminal_node_ids": ["completed", "cancelled"],
    }


def _runtime_session() -> Session:
    """创建包含 Runtime 表的隔离内存数据库会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_runtime(db: Session) -> tuple[Skill, ChatSession]:
    """写入明确确认验收定义及其不可变版本和会话。"""

    content = _confirmation_content()
    definition = compile_legacy_skill_card(content)
    skill = Skill(
        tenant_id="tenant_demo",
        skill_id="confirmation_demo",
        version="1.0.0",
        name="明确确认验收",
        content_json=content,
        status="published",
    )
    version = SkillVersion(
        id="skillver_confirmation_demo",
        tenant_id="tenant_demo",
        skill_id=skill.skill_id,
        version=skill.version,
        name=skill.name,
        content_json=content,
        status="published",
        compiled_definition_checksum=definition.checksum,
    )
    chat_session = ChatSession(
        id="session_confirmation_demo",
        tenant_id="tenant_demo",
        active_skill_id=skill.skill_id,
        active_step_id="collect_request",
        slots_json={"request": "创建记录", "confirmation": "confirmed"},
    )
    db.add(skill)
    db.add(version)
    db.add(chat_session)
    db.commit()
    return skill, chat_session


def test_compiler_freezes_confirmation_policy_as_meta_model_version_four() -> None:
    """验证确认短语归一后进入不可变定义，并提升到统一元模型第四版。"""

    content = _confirmation_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    policy = nodes[1]["metadata"]["confirmation_policy"]
    policy["phrase_values"][" 是的，确认提交。 "] = "confirmed"

    definition = compile_legacy_skill_card(content)
    confirm_node = next(node for node in definition.nodes if node.node_id == "confirm_request")

    assert definition.meta_model_version == 4
    assert confirm_node.config.confirmation_policy is not None
    assert confirm_node.config.confirmation_policy.phrase_values["是的确认提交"] == "confirmed"


def test_compiler_rejects_confirmation_policy_for_undeclared_slot() -> None:
    """验证确认策略不能把节点未声明的任意槽位变成授权信号。"""

    content = _confirmation_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[1]["metadata"]["confirmation_policy"]["slot_name"] = "approval"

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert {item.code for item in caught.value.diagnostics} == {"CONFIRMATION_SLOT_UNDECLARED"}


def test_runtime_requires_exact_current_turn_confirmation_before_tool_call() -> None:
    """验证历史槽位、模型推测值和模糊回复均不能越过写操作确认门禁。"""

    with _runtime_session() as db:
        skill, chat_session = _seed_runtime(db)
        coordinator = DeterministicSopCoordinator(db)

        first_wait = coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="准备执行", slot_updates={"confirmation": "confirmed"}),
            user_message="请帮我创建记录",
        )
        db.commit()
        assert first_wait.action == "ask_user"
        assert first_wait.reply == "请回复“确认提交”或“取消提交”。"
        assert first_wait.is_runtime_control_reply() is True
        assert db.exec(select(SopOperation)).all() == []

        # 模拟对话 Router 把会话步骤误指回流程起点；活动实例游标必须仍是唯一真相源。
        chat_session.active_step_id = "collect_request"
        chat_session.slots_json = {**(chat_session.slots_json or {}), "confirmation": "confirmed"}
        second_wait = coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="继续", slot_updates={"confirmation": "confirmed"}),
            user_message="继续吧",
        )
        db.commit()
        assert second_wait.action == "ask_user"
        assert db.exec(select(SopOperation)).all() == []

        confirmed = coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="已确认"),
            user_message=" 确认提交。 ",
        )
        db.commit()

        operations = db.exec(select(SopOperation)).all()
        assert confirmed.action == "call_tool"
        assert confirmed.tool_call is not None
        assert confirmed.tool_call.name == "demo.write"
        assert confirmed.tool_call.arguments == {"request": "创建记录"}
        assert len(operations) == 1


def test_runtime_cancellation_completes_without_operation() -> None:
    """验证明确取消进入无副作用终态，不创建任何工具操作记录。"""

    with _runtime_session() as db:
        skill, chat_session = _seed_runtime(db)
        coordinator = DeterministicSopCoordinator(db)
        coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="准备执行"),
            user_message="请帮我创建记录",
        )
        cancelled = coordinator.prepare_step(
            chat_session,
            skill,
            StepAgentResult(reply="已取消"),
            user_message="取消提交",
        )
        db.commit()

        assert cancelled.is_step_completed is True
        assert db.exec(select(SopOperation)).all() == []


def test_meeting_room_routes_unavailable_receipt_without_claiming_success() -> None:
    """验证工具正常返回不可用时进入备选终态，而不是误报会议室已预订。"""

    definition = compile_legacy_skill_card(_meeting_room_deterministic_content({}))

    plan = plan_next_action(
        definition,
        current_node_id="node_call_room_booking",
        slots={},
        tool_results={
            "room_booking": {
                "status": "succeeded",
                "data": {"status": "unavailable"},
            }
        },
    )

    assert plan.action == "advance"
    assert plan.next_node_id == "node_booking_unavailable"
