"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : 20261002_0090_feedback_materials.py
@CallChain  : Alembic → 原反馈事实/分析修订 → 本人预览与明确分享材料
@Description: 历史反馈默认未分享，双库持久化最小授权投影及不可复活修订。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '20261002_0090'
down_revision: str | None = '20261001_0089'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只补缺失修订列及材料表，不将历史反馈自动分享或回填原执行猜测。"""
    bind=op.get_bind()
    inspector=sa.inspect(bind)
    if 'message_feedback' in inspector.get_table_names():
        columns={c['name'] for c in inspector.get_columns('message_feedback')}
        for name in ['revision','analysis_generation']:
            if name not in columns:
                op.add_column('message_feedback',sa.Column(name,sa.Integer(),nullable=False,
                                                          server_default='1'))
    if 'feedback_materials' in inspector.get_table_names():
        return
    op.create_table('feedback_materials',
        sa.Column('id',sa.String(512),primary_key=True),
        *[sa.Column(name,sa.String(128),nullable=False) for name in [
            'tenant_id','feedback_id','owner_user_id','target_agent_id','request_key']],
        sa.Column('feedback_revision',sa.Integer(),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('material_checksum',sa.String(64),nullable=False),
        sa.Column('material_json',sa.JSON(),nullable=False),
        sa.Column('status',sa.String(64),nullable=False,server_default='preview'),
        sa.Column('created_at',sa.DateTime().with_variant(mysql.DATETIME(fsp=6),'mysql'),
                  nullable=False),
        sa.Column('updated_at',sa.DateTime().with_variant(mysql.DATETIME(fsp=6),'mysql'),
                  nullable=False),
        sa.UniqueConstraint('tenant_id','owner_user_id','request_key',
                            name='uq_feedback_material_owner_request'),
        sa.CheckConstraint("status IN ('preview','shared','withdrawn','invalid')",
                           name='ck_feedback_material_status'))
    for name in ['tenant_id','feedback_id','owner_user_id','target_agent_id','status']:
        op.create_index('ix_feedback_materials_'+name,'feedback_materials',[name])


def downgrade() -> None:
    """活跃授权材料存在时拒绝丢失撤回合同，其余只移除本批新增结构。"""
    inspector=sa.inspect(op.get_bind())
    if 'feedback_materials' in inspector.get_table_names():
        active=op.get_bind().execute(sa.text("SELECT COUNT(*) FROM feedback_materials WHERE status IN ('preview','shared')")).scalar_one()
        if active:
            raise RuntimeError('FEEDBACK_ACTIVE_MATERIALS')
        op.drop_table('feedback_materials')
    if 'message_feedback' in inspector.get_table_names():
        columns={c['name'] for c in inspector.get_columns('message_feedback')}
        with op.batch_alter_table('message_feedback') as batch:
            for name in ['revision','analysis_generation']:
                if name in columns:
                    batch.drop_column(name)
