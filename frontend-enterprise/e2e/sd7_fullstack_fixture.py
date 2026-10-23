"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : sd7_fullstack_fixture.py
@CallChain  : 显式 FULLSTACK_SD7 → 隔离随机库 → 本人历史反馈 → 真实页面分享与撤回
@Description: F1合成旧反馈；显式F2经原发布/Coordinator建立等待来源，不预置材料/分享/成功结果。
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def prepare_sd7(runtime: Path, *, reuse: bool) -> None:
    """仅在显式临时全栈目录和随机测试数据库启用；重启保留当前分享修订。"""
    if os.environ.get("FULLSTACK_SD7") != "1":
        return
    # runner 可将 TMPDIR 指向同一批次的 temporary/，runtime/ 是其兄弟目录。
    allowed_roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    if not any(runtime.resolve().is_relative_to(root) for root in allowed_roots):
        raise RuntimeError("SD7_REQUIRES_ISOLATED_TEMPORARY_RUNTIME")
    from app.db import engine

    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("SD7_REQUIRES_RANDOM_MYSQL_TEST_DATABASE")
    elif engine.dialect.name == "sqlite":
        if not Path(engine.url.database or "").resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError("SD7_SQLITE_OUTSIDE_ISOLATED_RUNTIME")
    else:
        raise RuntimeError("SD7_UNSUPPORTED_TEST_DATABASE")
    if not reuse:
        _seed_feedback()
        if os.environ.get('FULLSTACK_SD7_F2') == '1':
            _seed_current_editor_source()


def _seed_current_editor_source() -> None:
    """原发布/Coordinator建立等待来源；显式收尾协议输入有合法domain，不伪造模型质量。"""
    from sqlmodel import Session

    from app.api.skills import create_skill
    from app.core.agent_loop import AgentLoop
    from app.db import engine
    from app.db.models import ChatSession, Message, MessageFeedback, User
    from app.session.session_schema import StepAgentResult
    from app.skills.skill_schema import SkillCard, SkillCreateRequest
    from app.sop_runtime.coordinator import DeterministicSopCoordinator

    card = SkillCard.model_validate({
        'skill_id': 'skill_sd7_feedback_wait', 'name': 'SD7 F2 Original SOP', 'version': '1.0.0',
        'business_domain': 'protocol' if os.environ.get('FULLSTACK_SD7_REPAIR') == '1' else None,
        'description': 'Synthetic original waiting explanation', 'execution_mode': 'deterministic',
        'goal': ['Receive employee identifier before continuing'],
        'condition_schemas': {'slots': {'type': 'object', 'properties': {'employee_id': {'type': 'string'}}}},
        'nodes': [
            {'node_id': 'collect', 'name': 'Collect employee', 'type': 'collect_info',
             'instruction': 'Ask for the employee identifier.', 'expected_user_info': ['employee_id'],
             'allowed_actions': ['ask_user', 'continue_flow']},
            {'node_id': 'reply', 'name': 'Reply', 'type': 'response', 'allowed_actions': ['answer_user']},
        ],
        'edges': [{'source_node_id': 'collect', 'next_node_id': 'reply'}],
        'start_node_id': 'collect', 'terminal_node_ids': ['reply'],
    })
    with Session(engine) as db:
        admin = db.get(User, 'admin')
        owner = db.get(User, 'sd7_owner')
        created = create_skill(SkillCreateRequest(tenant_id='tenant_demo', content=card, status='published'),
                               agent_id='agent_sd7_feedback', db=db, current_user=admin)
        from app.agents.branching import visible_skill_rows

        skill = next(item for item in visible_skill_rows(db, 'tenant_demo', 'agent_sd7_feedback')
                     if item.skill_id == created.skill_id)
        chat = ChatSession(id='session_sd7_f2_wait', tenant_id='tenant_demo', user_id=owner.id,
                           agent_id='agent_sd7_feedback', active_skill_id=card.skill_id,
                           title='SD7 F2 Original Wait')
        turn = Message(id='msg_sd7_f2_user', tenant_id='tenant_demo', session_id=chat.id,
                       role='user', content='Please continue the synthetic workflow.')
        db.add_all([chat, turn])
        db.commit()
        result = DeterministicSopCoordinator(db).prepare_step(chat, skill, StepAgentResult())
        db.commit()
        loop = AgentLoop(db)
        metadata = loop._assistant_message_metadata(result, chat, user_message_id=turn.id)
        # 与原 _persist_final_reply 相同，轮次键属于回复持久化边界；来源快照仍由原入口生成。
        metadata.update(user_message_id=turn.id, turn_id=turn.id)
        reply = loop._append_message('tenant_demo', chat.id, 'assistant',
            'Please provide your employee identifier before continuing.', metadata=metadata,
            message_id='msg_sd7_f2_reply')
        from app.feedback.source import feedback_source

        assert feedback_source(db, reply).status == 'verified'
        db.add(MessageFeedback(id='fb_sd7_f2_wait', tenant_id='tenant_demo', session_id=chat.id,
                               message_id=reply.id, user_id=owner.id, rating='down'))
        db.commit()


def _seed_feedback() -> None:
    """初始化双 member actor 和原反馈；不创建执行、发布版本、Operation 或分享投影。"""
    from sqlmodel import Session

    from app.db import engine
    from app.db.models import AgentProfile, AgentUsage, ChatSession, Message, MessageFeedback, User
    from app.security.auth import hash_password

    owner_password = os.environ.get("FULLSTACK_SD7_OWNER_PASSWORD", "")
    manager_password = os.environ.get("FULLSTACK_SD7_MANAGER_PASSWORD", "")
    if not owner_password or not manager_password:
        raise RuntimeError("SD7_SYNTHETIC_LOGIN_ENVIRONMENT_REQUIRED")
    with Session(engine) as db:
        for user_id, username, password in (
            ("sd7_owner", "sd7-owner", owner_password),
            ("sd7_manager", "sd7-manager", manager_password),
        ):
            db.add(User(
                id=user_id, tenant_id="tenant_demo", username=username,
                display_name=username, role="member", password_hash=hash_password(password),
            ))
        db.add(AgentProfile(
            id="agent_sd7_feedback", tenant_id="tenant_demo", name="SD7 Feedback Employee",
            owner_user_id="sd7_manager", status="active", published_to_gallery=True,
            visibility_scope="tenant", description="Synthetic feedback consent test employee",
            metadata_json={"owner_user_id": "sd7_manager", "owner_username": "sd7-manager"},
        ))
        db.add(AgentProfile(
            id="agent_sd7_other", tenant_id="tenant_demo", name="SD7 Other Employee",
            owner_user_id="sd7_owner", status="active", published_to_gallery=True,
            visibility_scope="tenant", description="Synthetic unrelated management scope",
            metadata_json={"owner_user_id": "sd7_owner", "owner_username": "sd7-owner"},
        ))
        db.flush()
        for user_id in ("sd7_owner", "sd7_manager"):
            for agent_id in ("agent_sd7_feedback", "agent_sd7_other"):
                db.add(AgentUsage(
                    tenant_id="tenant_demo", user_id=user_id, agent_id=agent_id,
                ))
        db.add(ChatSession(
            id="session_sd7_feedback_legacy", tenant_id="tenant_demo", user_id="sd7_owner",
            agent_id="agent_sd7_feedback", title="SD7 Historical Feedback",
            slots_json={"private_slot": "SD7_PRIVATE_SLOT_BYTES"},
        ))
        db.add(Message(
            id="msg_sd7_feedback_user", tenant_id="tenant_demo",
            session_id="session_sd7_feedback_legacy", role="user",
            content="SD7_PRIVATE_USER_BYTES",
        ))
        db.add(Message(
            id="msg_sd7_feedback_reply", tenant_id="tenant_demo",
            session_id="session_sd7_feedback_legacy", role="assistant",
            content="SD7_PRIVATE_CONVERSATION_BYTES",
            metadata_json={"user_message_id": "msg_sd7_feedback_user"},
        ))
        db.add(MessageFeedback(
            id="fb_sd7_feedback_legacy", tenant_id="tenant_demo",
            session_id="session_sd7_feedback_legacy", message_id="msg_sd7_feedback_reply",
            user_id="sd7_owner", rating="down", analysis_status="analyzed",
            analysis_bucket="skill_issue", analysis_summary="SD7_PRIVATE_MODEL_INFERENCE_BYTES",
            analysis_reason="SD7_PRIVATE_MODEL_REASON_BYTES",
            analysis_json={"evidence": [{"slot": "SD7_PRIVATE_MODEL_SLOT_BYTES"}]},
        ))
        db.commit()
