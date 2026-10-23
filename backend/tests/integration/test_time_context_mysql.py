"""
@Time       : 2026/09/27
@Author     : zhanglp8181
@File       : test_time_context_mysql.py
@CallChain  : pytest mysql → Alembic head → Message/SopInstance JSON → 时间事实回读
@Description: 在隔离 MySQL 验证聊天输入时间与 SOP 执行快照的双库持久语义。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, create_engine, select

from app.core.time_context import freeze_time_context, normalize_persisted_time_context
from app.db.models import AgentProfile, ChatSession, Message, Skill, SkillVersion, SopInstance, SopNodeExecution
from app.session.session_schema import StepAgentResult
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card


pytestmark = pytest.mark.mysql
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _upgrade(database_url: str) -> None:
    """把随机 MySQL 测试库升级到当前 Alembic head。"""

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def test_mysql_time_context_round_trips_message_and_sop_attempt(
    mysql_database_url: str,
) -> None:
    """验证 MySQL 8.4 原样持久并回读含 DST 偏移的输入时间与节点快照。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    context = freeze_time_context(
        source_input_id="message_mysql_sd3_time",
        client_timezone="America/New_York",
        anchor_at_utc=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
    )
    with Session(engine) as db:
        db.add(
            Message(
                id="message_mysql_sd3_time",
                tenant_id="tenant_mysql_sd3_time",
                session_id="session_mysql_sd3_time",
                role="user",
                content="请按今天处理",
                metadata_json={"time_context": context},
            )
        )
        db.add(
            SopInstance(
                id="sopinst_mysql_sd3_time",
                tenant_id="tenant_mysql_sd3_time",
                session_id="session_mysql_sd3_time",
                skill_id="skill_mysql_sd3_time",
                skill_version_id="skillver_mysql_sd3_time",
                skill_version="1.0.0",
                definition_checksum="d" * 64,
                active_slot_key="foreground:session_mysql_sd3_time",
                kind="sop",
                status="waiting",
                current_node_id="collect",
                context_json={"time_context": context},
            )
        )
        db.add(
            SopNodeExecution(
                id="sopnode_mysql_sd3_time",
                tenant_id="tenant_mysql_sd3_time",
                instance_id="sopinst_mysql_sd3_time",
                node_id="collect",
                step_key="collect",
                status="waiting",
                input_json={"time_context": context},
            )
        )
        db.commit()
        db.expire_all()
        message = db.get(Message, "message_mysql_sd3_time")
        instance = db.get(SopInstance, "sopinst_mysql_sd3_time")
        execution = db.get(SopNodeExecution, "sopnode_mysql_sd3_time")

    assert message is not None
    assert instance is not None
    assert execution is not None
    assert normalize_persisted_time_context(message.metadata_json["time_context"]) == context
    assert normalize_persisted_time_context(instance.context_json["time_context"]) == context
    assert normalize_persisted_time_context(execution.input_json["time_context"]) == context


def test_mysql_sop_time_context_recovers_same_instance_after_fresh_worker_session(
    mysql_database_url: str,
) -> None:
    """MySQL 新运行时会话恢复实际等待 SOP 时保留同一实例、节点和输入锚点。"""

    _upgrade(mysql_database_url)
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    first_context = freeze_time_context(
        source_input_id="message_mysql_sd3_restart_first",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 9, 28, 9, 59, tzinfo=UTC),
    )
    second_context = freeze_time_context(
        source_input_id="message_mysql_sd3_restart_second",
        client_timezone="Pacific/Honolulu",
        anchor_at_utc=datetime(2026, 9, 28, 10, 5, tzinfo=UTC),
    )
    content = {
        "skill_id": "skill_mysql_sd3_restart",
        "name": "MySQL 时间恢复技能",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "nodes": [
            {
                "node_id": "collect_employee",
                "type": "collect_info",
                "name": "收集员工号",
                "instruction": "收集员工号",
                "expected_user_info": ["employee_id"],
                "allowed_actions": ["ask_user"],
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复",
                "instruction": "回复结果",
                "expected_user_info": [],
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {
                "source_node_id": "collect_employee",
                "next_node_id": "reply",
                "priority": 0,
                "label": "默认推进",
            }
        ],
        "start_node_id": "collect_employee",
        "terminal_node_ids": ["reply"],
        "required_info": ["employee_id"],
    }
    definition = compile_legacy_skill_card(content)
    try:
        with Session(engine, expire_on_commit=False) as db:
            skill = Skill(
                tenant_id="tenant_mysql_sd3_restart",
                skill_id="skill_mysql_sd3_restart",
                version="1.0.0",
                name="MySQL 时间恢复技能",
                content_json=content,
                status="published",
            )
            db.add_all(
                [
                    skill,
                    AgentProfile(
                        id="agent_mysql_sd3_restart",
                        tenant_id="tenant_mysql_sd3_restart",
                        name="MySQL 时间恢复员工",
                        status="active",
                    ),
                    SkillVersion(
                        id="skillver_mysql_sd3_restart",
                        tenant_id="tenant_mysql_sd3_restart",
                        skill_id=skill.skill_id,
                        version=skill.version,
                        name=skill.name,
                        content_json=content,
                        status="published",
                        compiled_definition_checksum=definition.checksum,
                    ),
                    ChatSession(
                        id="session_mysql_sd3_restart",
                        tenant_id="tenant_mysql_sd3_restart",
                        agent_id="agent_mysql_sd3_restart",
                        active_skill_id=skill.skill_id,
                        active_step_id="collect_employee",
                    ),
                    Message(
                        id=first_context["source_input_id"],
                        tenant_id="tenant_mysql_sd3_restart",
                        session_id="session_mysql_sd3_restart",
                        role="user",
                        content="跨午夜前等待员工号",
                        metadata_json={"time_context": first_context},
                    ),
                ]
            )
            db.commit()
            waiting = DeterministicSopCoordinator(db).prepare_step(
                db.get(ChatSession, "session_mysql_sd3_restart"),
                skill,
                StepAgentResult(reply="请提供员工号"),
                time_context=first_context,
            )
            db.commit()
            instance = db.exec(select(SopInstance)).one()
            execution = db.exec(select(SopNodeExecution)).one()
            instance_id = instance.id
            execution_id = execution.id
            node_id = execution.node_id
            assert waiting.action == "ask_user"
            assert instance.status == "waiting"

        with Session(engine, expire_on_commit=False) as restarted_db:
            restarted_skill = restarted_db.get(Skill, skill.id)
            restarted_session = restarted_db.get(ChatSession, "session_mysql_sd3_restart")
            assert restarted_skill is not None
            assert restarted_session is not None
            restarted_db.add(
                Message(
                    id=second_context["source_input_id"],
                    tenant_id="tenant_mysql_sd3_restart",
                    session_id=restarted_session.id,
                    role="user",
                    content="员工号 E001，按午夜后的新日期执行",
                    metadata_json={"time_context": second_context},
                )
            )
            restarted_session.slots_json = {"employee_id": "E001"}
            restarted_db.add(restarted_session)
            restarted_db.commit()
            resumed = DeterministicSopCoordinator(restarted_db).prepare_step(
                restarted_session,
                restarted_skill,
                StepAgentResult(slot_updates={"employee_id": "E001"}),
                user_message="员工号 E001，按午夜后的新日期执行",
                time_context=second_context,
            )
            restarted_db.commit()
            recovered_instance = restarted_db.get(SopInstance, instance_id)
            recovered_execution = restarted_db.get(SopNodeExecution, execution_id)
            instance_count = len(restarted_db.exec(select(SopInstance)).all())

        assert resumed.action == "reply"
        assert resumed.is_step_completed is True
        assert recovered_instance is not None
        assert recovered_execution is not None
        assert instance_count == 1
        assert recovered_execution.node_id == node_id
        assert recovered_execution.input_json["time_context"] == first_context
        assert recovered_execution.input_json["resume_time_context"] == second_context
    finally:
        engine.dispose()
