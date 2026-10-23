"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_material_migrations.py
@CallChain  : 历史未分享反馈 → SQLite兼容重启/MySQL真实Alembic → 安全修订
@Description: 证明旧反馈默认未授权，迁移及桌面重复初始化不伪造来源或分享。
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import FeedbackMaterial, MessageFeedback
from app.db.sqlite_legacy import initialize_sqlite_database


@pytest.mark.parametrize('dialect', ['sqlite', pytest.param('mysql',marks=pytest.mark.mysql)])
def test_feedback_alembic_upgrade_preserves_old_unshared_rows(dialect, request, tmp_path):
    """实际从旧head插入未授权反馈后升级，修订列默认1且没有隐式分享记录。"""
    backend=Path(__file__).resolve().parents[1]
    url=request.getfixturevalue('mysql_database_url') if dialect=='mysql' else 'sqlite:///'+str(tmp_path/'old.sqlite')
    config=Config(str(backend/'alembic.ini'))
    config.set_main_option('script_location',str(backend/'alembic'))
    config.attributes['database_url']=url
    engine=create_engine(url)
    if dialect == 'sqlite':
        # 桌面沿原metadata/兼容初始化；旧全Alembic链含MySQL专用ALTER，不假称可运行。
        SQLModel.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE feedback_materials'))
            conn.execute(text('ALTER TABLE message_feedback DROP COLUMN revision'))
            conn.execute(text('ALTER TABLE message_feedback DROP COLUMN analysis_generation'))
        command.stamp(config, '20261001_0089')
    else:
        command.upgrade(config, '20261001_0089')
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO message_feedback (id,tenant_id,session_id,message_id,user_id,rating,analysis_status,analysis_json,created_at,updated_at) VALUES ('migration-fb','migration-t','migration-s','migration-msg','migration-u','down','pending','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        command.upgrade(config,'head')
        command.upgrade(config,'head')
        with Session(engine) as db:
            row=db.get(MessageFeedback,'migration-fb')
            assert row.revision==row.analysis_generation==1
            assert row.rating=='down'
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM feedback_materials')).scalar_one()==0
        columns={c['name'] for c in inspect(engine).get_columns('message_feedback')}
        assert {'revision','analysis_generation'} <= columns
    finally:
        engine.dispose()


def test_sqlite_legacy_feedback_restart_adds_only_safe_defaults(tmp_path):
    """桌面旧库两次初始化均补安全修订列，既有评分及未分享状态不变。"""
    engine=create_engine('sqlite:///'+str(tmp_path/'desktop.sqlite'))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE message_feedback (id VARCHAR PRIMARY KEY,tenant_id VARCHAR,session_id VARCHAR,message_id VARCHAR,user_id VARCHAR,rating VARCHAR,created_at DATETIME,updated_at DATETIME)"))
            conn.execute(text("INSERT INTO message_feedback VALUES ('old-fb','old-t','old-s','old-m','old-u','down',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        initialize_sqlite_database(engine)
        initialize_sqlite_database(engine)
        with Session(engine) as db:
            row=db.get(MessageFeedback,'old-fb')
            assert row.revision==row.analysis_generation==1
            assert row.rating=='down'
            assert db.get(FeedbackMaterial,'old-fb') is None
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM feedback_materials')).scalar_one()==0
    finally:
        engine.dispose()
