"""
@Time       : 2026/09/05 19:00
@Author     : zhanglp8181
@File       : 20260905_0082_local_browser_pairing.py
@CallChain  : Alembic upgrade/downgrade → 已有设备配对表 → 本机/远程统一授权
@Description: 放开配对表的本机传输值，保留既有会话和缺省远程模式，禁止有本机会话时回退。

Revision ID: 20260905_0082
Revises: 20260904_0081
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260905_0082"
down_revision: str | None = "20260904_0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "vertical_ability_browser_relay_sessions"
CONSTRAINT = "ck_vertical_ability_browser_relay_session_transport"


def _replace_transport_check(*, allow_local: bool) -> None:
    """用双方言 batch 操作替换命名 CHECK，重复执行不重建已有正确约束。"""

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return
    existing = next(
        (item for item in inspector.get_check_constraints(TABLE) if item["name"] == CONSTRAINT),
        None,
    )
    if existing is not None and ("local_loopback" in existing["sqltext"]) == allow_local:
        return
    expression = (
        "transport IN ('local_loopback', 'browser_relay')"
        if allow_local else "transport = 'browser_relay'"
    )
    with op.batch_alter_table(TABLE) as batch:
        if existing is not None:
            batch.drop_constraint(CONSTRAINT, type_="check")
        batch.create_check_constraint(CONSTRAINT, expression)


def upgrade() -> None:
    """允许本机设备复用现有配对授权协议，不重写任何旧会话或凭据。"""

    _replace_transport_check(allow_local=True)


def downgrade() -> None:
    """存在本机会话时拒绝回退，不删除记录也不把本机伪装成远程模式。"""

    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    count = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM {TABLE} WHERE transport = 'local_loopback'"
    )).scalar_one()
    if count:
        raise RuntimeError("cannot downgrade while local_loopback pairing sessions exist")
    _replace_transport_check(allow_local=False)
