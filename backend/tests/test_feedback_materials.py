"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_materials.py
@CallChain  : 正式反馈/独立连接修改 → 原轮来源/迟到分析/分享修订
@Description: 锁定F1精确原来源、最小分享与撤回合同，两库沿原随机夹具迁移。
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import ChatSession, Message, MessageFeedback, ModelConfig, Tenant, User
from app.feedback.service import FeedbackAnalysisService
from app.llm.client import LLMClient


@pytest.fixture(params=['sqlite', pytest.param('mysql', marks=pytest.mark.mysql)])
def feedback_engine(request, tmp_path):
    """SQLite真实文件及MySQL原随机账号/Alembic提供两独立提交连接。"""
    if request.param == 'mysql':
        url=request.getfixturevalue('mysql_database_url')
        backend=Path(__file__).resolve().parents[1]
        config=Config(str(backend/'alembic.ini'))
        config.set_main_option('script_location',str(backend/'alembic'))
        config.attributes['database_url']=url
        command.upgrade(config,'head')
        engine=create_engine(url,pool_pre_ping=True)
    else:
        engine=create_engine('sqlite:///'+str(tmp_path/'feedback.sqlite'))
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def seed_feedback(db):
    """生成无可证明历史Execution的合法旧回复，当前会话游标故意指向新技能。"""
    db.add(Tenant(id='fb-t',name='隔离'))
    db.add(User(id='fb-u',tenant_id='fb-t',username='owner',password_hash='synthetic'))
    db.add(ChatSession(id='fb-s',tenant_id='fb-t',user_id='fb-u',active_skill_id='later-sop',
                       slots_json={'password':'SYNTHETIC_PRIVATE_SLOT'}))
    db.add(Message(id='fb-turn',tenant_id='fb-t',session_id='fb-s',role='user',content='原问题'))
    db.add(Message(id='fb-answer',tenant_id='fb-t',session_id='fb-s',role='assistant',
                   content='原回复',metadata_json={'turn_id':'fb-turn'}))
    db.add(MessageFeedback(id='fb-1',tenant_id='fb-t',session_id='fb-s',message_id='fb-answer',
                           user_id='fb-u',rating='down'))
    db.add(ModelConfig(id='fb-model',tenant_id='fb-t',name='isolated',model='mock',
                       api_key_encrypted='mock',is_default=True))
    db.commit()
    return db.get(MessageFeedback,'fb-1')


def test_old_feedback_does_not_inherit_current_session_skill(feedback_engine):
    """原消息无版本证据时不得把当前技能和当前slots作为原轮执行事实。"""
    with Session(feedback_engine) as db:
        feedback=seed_feedback(db)
        payload=FeedbackAnalysisService(db)._analysis_payload(feedback)
        assert payload['session']['active_skill_id'] is None
        assert payload['session']['slots']=={}
        assert payload['source']['status']=='missing'


def test_late_analysis_cannot_overwrite_modified_rating(feedback_engine,monkeypatch):
    """模型外呼期间另一真实连接修改评分，旧分析结果不得污染新修订。"""
    monkeypatch.setattr(LLMClient,'__init__',lambda *_:None)
    def complete_after_change(self,prompt,payload):
        """按正式反馈入口提交另一评分，再返回过时模型归因。"""
        from app.api.chat import upsert_message_feedback
        from app.session.session_schema import MessageFeedbackRequest
        monkeypatch.setattr('app.api.chat.enqueue_feedback_analysis',lambda *_:None)
        with Session(feedback_engine) as writer:
            upsert_message_feedback('fb-answer',MessageFeedbackRequest(tenant_id='fb-t',rating='up'),
                                    current_user=writer.get(User,'fb-u'),db=writer)
        return {'bucket':'skill_issue','summary':'过时归因','reason':'过时归因'}
    monkeypatch.setattr(LLMClient,'generate_json',complete_after_change)
    with Session(feedback_engine) as db:
        seed_feedback(db)
        FeedbackAnalysisService(db).analyze_feedback('fb-1')
    with Session(feedback_engine) as reader:
        row=reader.get(MessageFeedback,'fb-1')
        assert row.rating=='up'
        assert row.analysis_status=='pending'
        assert row.analysis_summary is None
