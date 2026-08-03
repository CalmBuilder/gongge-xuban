"""
@Time       : 2026/07/27 19:45
@Author     : zhanglp8181
@File       : builtin_tools.py
@CallChain  : ToolExecutor → 内置工具白名单 → ApprovalRequestService
@Description: 在同一数据库事务内分发少量受控领域工具，禁止按配置执行任意本地代码。
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlmodel import Session

from app.approvals import ApprovalRequestService


def execute_builtin_tool(
    db: Session,
    *,
    tenant_id: str,
    tool_name: str,
    arguments: Mapping[str, object],
    actor_user_id: str | None,
) -> dict[str, object]:
    """按固定名称分发内置领域命令，未知名称一律拒绝。"""

    service = ApprovalRequestService(db)
    if tool_name == "admin.seal_application_create":
        return service.create_seal_application(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payload=arguments,
        )
    if tool_name == "admin.seal_application_approve":
        return service.decide_seal_application(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_outcome="approved",
        )
    if tool_name == "admin.seal_application_reject":
        return service.decide_seal_application(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_outcome="rejected",
        )
    if tool_name == "admin.seal_application_query":
        return service.query_seal_application(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payload=arguments,
        )
    if tool_name == "expense.special_approval_create":
        return service.create_expense_special_approval(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payload=arguments,
        )
    if tool_name == "expense.special_approval_step1_approve":
        return service.decide_expense_special_approval(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_step=1,
            expected_outcome="approved",
        )
    if tool_name == "expense.special_approval_step1_reject":
        return service.decide_expense_special_approval(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_step=1,
            expected_outcome="rejected",
        )
    if tool_name == "expense.special_approval_step2_approve":
        return service.decide_expense_special_approval(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_step=2,
            expected_outcome="approved",
        )
    if tool_name == "expense.special_approval_step2_reject":
        return service.decide_expense_special_approval(
            tenant_id=tenant_id,
            approval_request_id=str(arguments.get("approval_request_id") or ""),
            expected_step=2,
            expected_outcome="rejected",
        )
    if tool_name == "expense.special_approval_query":
        return service.query_expense_special_approval(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payload=arguments,
        )
    raise ValueError(f"Unsupported builtin tool: {tool_name}")
