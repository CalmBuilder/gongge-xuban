"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : 20261001_0089_memory_claim_budget.py
@CallChain  : Alembic → MemoryCaptureJob → 每 claim 实际发送计数
@Description: 补齐每次领取的持久发送上限，并为历史待办固定原完成时间截止。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261001_0089"
down_revision: str | None = "20261001_0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 SQLite/MySQL 旧任务增加 claim 计数，重跑已添加字段时保持幂等。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "memory_capture_jobs" not in inspector.get_table_names():
        return
    if "claim_dispatch_count" not in {
        item["name"] for item in inspector.get_columns("memory_capture_jobs")
    }:
        op.add_column(
            "memory_capture_jobs",
            sa.Column("claim_dispatch_count", sa.Integer(), nullable=False, server_default="0"),
        )
    expression = (
        "DATE_ADD(created_at, INTERVAL 600 SECOND)"
        if bind.dialect.name == "mysql"
        else "datetime(created_at, '+600 seconds')"
    )
    bind.execute(
        sa.text(
            f"UPDATE memory_capture_jobs SET deadline_at = {expression} WHERE deadline_at IS NULL"
        )
    )


def downgrade() -> None:
    """活动任务尚未结算时拒绝删除发送预算，避免回退放宽在途合同。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "memory_capture_jobs" not in inspector.get_table_names():
        return
    active = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM memory_capture_jobs WHERE status IN ('pending','running','retry')"
        )
    ).scalar_one()
    if active:
        raise RuntimeError("MEMORY_CAPTURE_ACTIVE_JOBS")
    if "claim_dispatch_count" in {
        item["name"] for item in inspector.get_columns("memory_capture_jobs")
    }:
        with op.batch_alter_table("memory_capture_jobs") as batch:
            batch.drop_column("claim_dispatch_count")
