"""
@Time       : 2026/07/29 01:45
@Author     : zhanglp8181
@File       : service.py
@CallChain  : 管理 API/领域服务 → 审计脱敏与追加 → ManagementAuditLog
@Description: 提供递归脱敏、追加式管理审计写入以及租户和组织范围查询。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db.models import ManagementAuditLog, User, new_id


_REDACTED = "[REDACTED]"
_MAX_DEPTH = 6
_MAX_ITEMS = 20
_MAX_FIELDS = 50
_MAX_TEXT_LENGTH = 500
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "password",
        "password_hash",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "prompt",
        "system_prompt",
        "persona_prompt",
        "content",
        "body",
        "html",
        "raw",
    }
)


def sanitize_audit_payload(value: object, *, _depth: int = 0) -> Any:
    """递归脱敏并限制深度、字段数、集合长度和文本长度。"""

    if _depth >= _MAX_DEPTH:
        return "[MAX DEPTH]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:_MAX_FIELDS]:
            normalized_key = str(key)
            if _is_sensitive_key(normalized_key):
                result[normalized_key] = _REDACTED
            else:
                result[normalized_key] = sanitize_audit_payload(
                    item,
                    _depth=_depth + 1,
                )
        if len(items) > _MAX_FIELDS:
            result["_truncated_fields"] = len(items) - _MAX_FIELDS
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        limit = _MAX_ITEMS - 1 if len(items) > _MAX_ITEMS else _MAX_ITEMS
        result = [
            sanitize_audit_payload(item, _depth=_depth + 1)
            for item in items[:limit]
        ]
        if len(items) > _MAX_ITEMS:
            result.append(f"[TRUNCATED {len(items) - limit} ITEMS]")
        return result
    if isinstance(value, str):
        escaped = value.replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= _MAX_TEXT_LENGTH:
            return escaped
        return f"{escaped[: _MAX_TEXT_LENGTH - 3]}..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_audit_payload(str(value), _depth=_depth + 1)


def append_management_audit(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str | None,
    actor_display_name: str | None,
    action: str,
    action_kind: str,
    outcome: str,
    resource_type: str,
    resource_id: str | None = None,
    target_org_unit_id: str | None = None,
    permission_code: str | None = None,
    permission_source: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    before: Mapping[str, object] | None = None,
    after: Mapping[str, object] | None = None,
    detail: Mapping[str, object] | None = None,
    actor_type: str = "user",
    audit_id: str | None = None,
) -> ManagementAuditLog:
    """向当前事务追加一条已脱敏审计；调用方负责与业务事务共同提交。"""

    resolved_request_id = request_id or new_id("req")
    row = ManagementAuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        actor_display_name=actor_display_name,
        action=action,
        action_kind=action_kind,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        target_org_unit_id=target_org_unit_id,
        permission_code=permission_code,
        permission_source=permission_source,
        request_id=resolved_request_id,
        correlation_id=correlation_id or resolved_request_id,
        before_json=sanitize_audit_payload(before or {}),
        after_json=sanitize_audit_payload(after or {}),
        detail_json=sanitize_audit_payload(detail or {}),
    )
    if audit_id:
        row.id = audit_id
    db.add(row)
    db.flush()
    return row


def append_user_management_audit(
    db: Session,
    *,
    current_user: User,
    tenant_id: str,
    permission_code: str | None,
    action: str,
    action_kind: str,
    outcome: str,
    resource_type: str,
    resource_id: str | None = None,
    target_org_unit_id: str | None = None,
    before: Mapping[str, object] | None = None,
    after: Mapping[str, object] | None = None,
    detail: Mapping[str, object] | None = None,
) -> ManagementAuditLog | None:
    """在嵌套事务中解析权限来源并追加审计，审计写入失败时保留外层业务事务。"""

    try:
        with db.begin_nested():
            permission_source = None
            if permission_code:
                from app.organization.governance import resolve_permission_grants

                grants = [
                    grant
                    for grant in resolve_permission_grants(
                        db,
                        tenant_id=tenant_id,
                        user_id=current_user.id,
                    )
                    if grant.permission_code == permission_code
                    and (
                        target_org_unit_id is None
                        or grant.scope.organization_unit_ids is None
                        or target_org_unit_id in grant.scope.organization_unit_ids
                    )
                ]
                if grants:
                    grant = grants[0]
                    permission_source = f"{grant.source_kind}:{grant.role_code}"
            return append_management_audit(
                db,
                tenant_id=tenant_id,
                actor_user_id=current_user.id,
                actor_display_name=current_user.display_name or current_user.username,
                action=action,
                action_kind=action_kind,
                outcome=outcome,
                resource_type=resource_type,
                resource_id=resource_id,
                target_org_unit_id=target_org_unit_id,
                permission_code=permission_code,
                permission_source=permission_source,
                before=before,
                after=after,
                detail=detail,
            )
    except SQLAlchemyError:
        return None


def query_management_audits(
    db: Session,
    *,
    tenant_id: str,
    allowed_organization_ids: frozenset[str] | None,
    page: int,
    page_size: int,
    actor_user_id: str | None = None,
    action: str | None = None,
    action_kind: str | None = None,
    outcome: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> tuple[list[ManagementAuditLog], int]:
    """按租户、审计授权组织范围和可选条件返回稳定倒序分页。"""

    predicates = [ManagementAuditLog.tenant_id == tenant_id]
    if allowed_organization_ids is not None:
        if not allowed_organization_ids:
            return [], 0
        predicates.append(
            ManagementAuditLog.target_org_unit_id.in_(allowed_organization_ids)
        )
    if actor_user_id:
        predicates.append(ManagementAuditLog.actor_user_id == actor_user_id)
    if action:
        predicates.append(ManagementAuditLog.action == action)
    if action_kind:
        predicates.append(ManagementAuditLog.action_kind == action_kind)
    if outcome:
        predicates.append(ManagementAuditLog.outcome == outcome)
    if resource_type:
        predicates.append(ManagementAuditLog.resource_type == resource_type)
    if resource_id:
        predicates.append(ManagementAuditLog.resource_id == resource_id)
    if created_after:
        predicates.append(ManagementAuditLog.created_at >= created_after)
    if created_before:
        predicates.append(ManagementAuditLog.created_at <= created_before)

    total = db.exec(
        select(func.count()).select_from(ManagementAuditLog).where(*predicates)
    ).one()
    rows = db.exec(
        select(ManagementAuditLog)
        .where(*predicates)
        .order_by(
            ManagementAuditLog.created_at.desc(),
            ManagementAuditLog.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), int(total)


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否属于凭据、请求正文、私有提示或原始内容。"""

    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return any(
        normalized.endswith(f"_{suffix}")
        for suffix in (
            "password",
            "secret",
            "token",
            "api_key",
            "credential",
            "content",
            "body",
            "prompt",
        )
    )
