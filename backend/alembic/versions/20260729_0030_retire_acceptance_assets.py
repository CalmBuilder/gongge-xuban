"""
@Time       : 2026/07/29 23:35
@Author     : zhanglp8181
@File       : 20260729_0030_retire_acceptance_assets.py
@CallChain  : Alembic upgrade → 历史浏览器验收资源 → 产品目录与执行授权
@Description: 退役 M5.5-D 临时电信验收资产，同时保留不可变版本和运行实例作为审计证据。

Revision ID: 20260729_0030
Revises: 20260729_0029
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0030"
down_revision: str | None = "20260729_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT_ID = "agent_m55d_telecom_fault"
_ROLE_ID = "bizrole_m55d_telecom_fault_operator"
_SKILL_IDS = (
    "skill_telecom_fault_browser_regression_20260728",
    "skill_telecom_fault_regression_20260728",
)
_TOOL_NAMES = (
    "telecom.circuit.verify.browser.20260728",
    "telecom.enterprise_fault.create.browser.20260728",
    "telecom.circuit.verify.regression.20260728",
    "telecom.enterprise_fault.create.regression.20260728",
)
_KNOWLEDGE_NAMES = (
    "政企专线故障分级与申告规范-浏览器验收临时",
    "政企专线故障分级与申告规范-回归临时",
)
_PERMISSION_CODES = (
    "telecom.circuit.read:any",
    "telecom.fault.create:own",
)


def upgrade() -> None:
    """关闭临时资产的发布、绑定和授权入口，但不破坏历史版本与实例外键。"""

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_resource_bindings SET status = 'inactive' "
            "WHERE agent_id = :agent_id"
        ),
        {"agent_id": _AGENT_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE agent_skill_branches SET status = 'archived' "
            "WHERE agent_id = :agent_id OR skill_id IN :skill_ids"
        ).bindparams(sa.bindparam("skill_ids", expanding=True)),
        {"agent_id": _AGENT_ID, "skill_ids": _SKILL_IDS},
    )
    bind.execute(
        sa.text(
            "UPDATE agent_role_bindings SET status = 'inactive' "
            "WHERE agent_id = :agent_id OR business_role_id = :role_id"
        ),
        {"agent_id": _AGENT_ID, "role_id": _ROLE_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE employee_role_assignments SET status = 'inactive' "
            "WHERE business_role_id = :role_id"
        ),
        {"role_id": _ROLE_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE business_roles SET status = 'inactive' WHERE id = :role_id"
        ),
        {"role_id": _ROLE_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE permission_definitions SET status = 'inactive' "
            "WHERE permission_code IN :permission_codes"
        ).bindparams(sa.bindparam("permission_codes", expanding=True)),
        {"permission_codes": _PERMISSION_CODES},
    )
    bind.execute(
        sa.text("UPDATE tools SET enabled = false WHERE name IN :tool_names").bindparams(
            sa.bindparam("tool_names", expanding=True)
        ),
        {"tool_names": _TOOL_NAMES},
    )
    bind.execute(
        sa.text(
            "UPDATE knowledge_bases SET status = 'archived' "
            "WHERE name IN :knowledge_names"
        ).bindparams(sa.bindparam("knowledge_names", expanding=True)),
        {"knowledge_names": _KNOWLEDGE_NAMES},
    )
    bind.execute(
        sa.text(
            "UPDATE skills SET status = 'archived' WHERE skill_id IN :skill_ids"
        ).bindparams(sa.bindparam("skill_ids", expanding=True)),
        {"skill_ids": _SKILL_IDS},
    )
    bind.execute(
        sa.text(
            "UPDATE agent_profiles SET status = 'archived', "
            "published_to_gallery = false, visibility_scope = 'private' "
            "WHERE id = :agent_id"
        ),
        {"agent_id": _AGENT_ID},
    )


def downgrade() -> None:
    """不自动复活临时验收资产；如需取证恢复，应从升级前数据库备份恢复。"""

