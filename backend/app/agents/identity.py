"""
@Time       : 2026/07/28 21:18
@Author     : zhanglp8181
@File       : identity.py
@CallChain  : Agent API/权限服务 → 正式字段与 legacy metadata 双读 → 列表和对象授权
@Description: 集中解析数字员工所有权、发布、分类和可见范围，避免各调用方产生迁移期双语义。
"""

from __future__ import annotations

from app.db.models import AgentProfile


def agent_owner_user_id(row: AgentProfile) -> str | None:
    """正式 owner 优先；仅在历史字段为空时读取不可变 metadata 用户 ID。"""

    if row.owner_user_id:
        return row.owner_user_id
    candidate = (row.metadata_json or {}).get("owner_user_id")
    return str(candidate).strip() if candidate else None


def agent_is_published(row: AgentProfile) -> bool:
    """读取正式广场状态，并兼容尚未经过 0024 回填的测试或历史对象。"""

    if row.published_to_gallery is not None:
        return row.published_to_gallery
    return (row.metadata_json or {}).get("published_to_gallery") is True


def agent_category(row: AgentProfile) -> str:
    """返回正式业务分类，兼容明确标记为 expert 的历史数字员工。"""

    metadata = row.metadata_json or {}
    if row.agent_category_code != "assistant":
        return row.agent_category_code
    if metadata.get("employee_type") == "expert":
        return "professional"
    return row.agent_category_code or "assistant"


def agent_visibility_scope(row: AgentProfile) -> str:
    """返回受控可见范围；历史已发布对象按租户可见处理。"""

    if row.visibility_scope == "tenant" or agent_is_published(row):
        return "tenant"
    return "private"
