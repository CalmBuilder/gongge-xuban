"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : 20261002_0091_skill_version_scope.py
@CallChain  : Alembic → 原SkillVersion范围唯一性 → 精确发布和实例绑定
@Description: 旧发布保留整体身份，新增非空员工scope，不猜测历史员工发布。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0091"
down_revision: str | None = "20261002_0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """原表扩充精确范围，保留旧ID/内容及实例引用，不猜测历史员工发布。"""

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("skill_versions"):
        return
    columns = {column["name"] for column in inspector.get_columns("skill_versions")}
    if "scope_agent_id" in columns:
        return
    with op.batch_alter_table("skill_versions") as batch:
        batch.add_column(sa.Column("scope_agent_id", sa.String(128), nullable=False,
                                   server_default=""))
        batch.drop_constraint("uq_skill_version", type_="unique")
        batch.create_unique_constraint("uq_skill_version",
                                       ["tenant_id", "skill_id", "scope_agent_id", "version"])
        batch.create_index("ix_skill_versions_scope_agent_id", ["scope_agent_id"])


def downgrade() -> None:
    """存在员工快照时拒绝降级，防止同版本合并丢失执行身份。"""

    if not sa.inspect(op.get_bind()).has_table("skill_versions"):
        return
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM skill_versions WHERE scope_agent_id <> ''"
    )).scalar_one()
    if count:
        raise RuntimeError("SCOPED_SKILL_VERSIONS_PRESENT")
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_index("ix_skill_versions_scope_agent_id")
        batch.drop_constraint("uq_skill_version", type_="unique")
        batch.drop_column("scope_agent_id")
        batch.create_unique_constraint("uq_skill_version", ["tenant_id", "skill_id", "version"])
