"""
@Time       : 2026/07/22 16:35
@Author     : zhanglp8181
@File       : identity_context.py
@CallChain  : DeterministicSopCoordinator → 身份输入绑定 → EmployeeProfile/实例上下文
@Description: 将可信登录身份解析为 SOP 槽位，并执行本人办理与授权代办校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from sqlmodel import Session, select

from app.db.models import EmployeeProfile, User
from app.organization.permissions import employee_permission_codes
from app.organization.roles import active_business_role_codes
from app.sop_runtime.definition import (
    AuthenticatedEmployeeAttribute,
    AuthenticatedInputBinding,
    CollectInputNode,
    CompiledSopDefinition,
)


class SopIdentityContextError(ValueError):
    """可信身份缺失、目标不存在或代办越权时返回稳定错误。"""

    def __init__(
        self,
        code: str,
        user_message: str,
        *,
        audit_context: Mapping[str, object] | None = None,
    ) -> None:
        """保存面向 Runtime、用户和审计记录的三类错误信息。"""

        self.code = code
        self.user_message = user_message
        self.audit_context = dict(audit_context or {})
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """返回身份绑定后的槽位和可持久化审计上下文。"""

    slots: dict[str, object]
    audit_context: dict[str, object]


def resolve_identity_inputs(
    db: Session,
    *,
    definition: CompiledSopDefinition,
    tenant_id: str,
    actor_user_id: str | None,
    slots: Mapping[str, object],
    user_message: str,
    existing_identity_context: Mapping[str, object] | None = None,
) -> IdentityResolution:
    """解析定义声明的身份槽位，并拒绝未授权或未明确提出的代办目标。"""

    bindings = _definition_bindings(definition)
    resolved_slots = dict(slots)
    if not bindings:
        return IdentityResolution(slots=resolved_slots, audit_context={})
    actor, actor_profile = _load_actor_profile(db, tenant_id, actor_user_id)
    actor_business_roles = active_business_role_codes(
        db,
        tenant_id=tenant_id,
        employee_profile_id=actor_profile.id,
    )
    actor_business_permissions = employee_permission_codes(
        db,
        tenant_id=tenant_id,
        employee_profile_id=actor_profile.id,
    )
    subject_profile = actor_profile
    provenance: dict[str, object] = {}
    existing_subject = str(
        (existing_identity_context or {}).get("subject_employee_id") or ""
    ).strip()

    for slot_name, binding in bindings.items():
        profile_value = _profile_attribute(actor_profile, binding.attribute)
        requested_value = resolved_slots.get(slot_name)
        normalized_requested = str(requested_value).strip() if requested_value is not None else ""
        normalized_profile = str(profile_value).strip() if profile_value is not None else ""
        explicit_subject = _explicit_employee_profile_from_message(
            db,
            tenant_id=tenant_id,
            binding=binding,
            user_message=user_message,
        )
        if explicit_subject is not None:
            normalized_requested = str(
                _profile_attribute(explicit_subject, binding.attribute)
            ).strip()
        if not normalized_profile:
            raise SopIdentityContextError(
                "EMPLOYEE_PROFILE_ATTRIBUTE_MISSING",
                "当前账号的员工档案不完整，请联系管理员完善后再试。",
                audit_context={"actor_user_id": actor.id, "slot_name": slot_name},
            )
        if not normalized_requested or normalized_requested == normalized_profile:
            resolved_slots[slot_name] = profile_value
            provenance[slot_name] = {
                "source": "authenticated_employee",
                "attribute": binding.attribute.value,
                "mode": "self",
            }
            continue
        subject_profile = _authorize_override(
            db,
            tenant_id=tenant_id,
            actor=actor,
            actor_profile=actor_profile,
            actor_business_roles=actor_business_roles,
            actor_business_permissions=actor_business_permissions,
            binding=binding,
            requested_value=normalized_requested,
            user_message=user_message,
            existing_subject_employee_id=existing_subject,
        )
        resolved_slots[slot_name] = _profile_attribute(subject_profile, binding.attribute)
        provenance[slot_name] = {
            "source": "explicit_delegated_subject",
            "attribute": binding.attribute.value,
            "mode": "delegated",
        }

    delegated = subject_profile.id != actor_profile.id
    audit_context = {
        "actor_user_id": actor.id,
        "actor_role": actor.role,
        "actor_business_roles": actor_business_roles,
        "actor_business_permissions": actor_business_permissions,
        "actor_employee_profile_id": actor_profile.id,
        "actor_employee_id": actor_profile.employee_id,
        "subject_user_id": subject_profile.user_id,
        "subject_employee_profile_id": subject_profile.id,
        "subject_employee_id": subject_profile.employee_id,
        "delegated": delegated,
        "slot_provenance": provenance,
    }
    return IdentityResolution(slots=resolved_slots, audit_context=audit_context)


def sanitize_identity_slots_after_failure(
    definition: CompiledSopDefinition,
    slots: Mapping[str, object],
    audit_context: Mapping[str, object],
) -> dict[str, object]:
    """身份校验失败后移除不可信输入，并尽量恢复为已认证员工属性。"""

    sanitized_slots = dict(slots)
    for slot_name, binding in _definition_bindings(definition).items():
        trusted_value = audit_context.get(f"actor_{binding.attribute.value}")
        if trusted_value is None or str(trusted_value).strip() == "":
            sanitized_slots.pop(slot_name, None)
            continue
        sanitized_slots[slot_name] = trusted_value
    return sanitized_slots


def _definition_bindings(
    definition: CompiledSopDefinition,
) -> dict[str, AuthenticatedInputBinding]:
    """汇总定义中的身份输入绑定，并拒绝同一槽位出现冲突声明。"""

    bindings: dict[str, AuthenticatedInputBinding] = {}
    for node in definition.nodes:
        if not isinstance(node, CollectInputNode):
            continue
        for slot_name, binding in node.config.input_bindings.items():
            existing = bindings.get(slot_name)
            if existing is not None and existing != binding:
                raise SopIdentityContextError(
                    "IDENTITY_BINDING_CONFLICT",
                    "流程身份输入配置冲突，请联系管理员处理。",
                    audit_context={"slot_name": slot_name},
                )
            bindings[slot_name] = binding
    return bindings


def _load_actor_profile(
    db: Session, tenant_id: str, actor_user_id: str | None
) -> tuple[User, EmployeeProfile]:
    """在同一租户读取登录账号及其有效员工档案。"""

    actor = db.get(User, actor_user_id) if actor_user_id else None
    if actor is None or actor.tenant_id != tenant_id:
        raise SopIdentityContextError(
            "AUTHENTICATED_ACTOR_REQUIRED",
            "当前会话缺少可信登录身份，请重新登录后再试。",
        )
    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == actor.id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if profile is None:
        raise SopIdentityContextError(
            "EMPLOYEE_PROFILE_REQUIRED",
            "当前账号尚未绑定有效员工档案，请联系管理员完善工号后再试。",
            audit_context={"actor_user_id": actor.id, "actor_role": actor.role},
        )
    return actor, profile


def _authorize_override(
    db: Session,
    *,
    tenant_id: str,
    actor: User,
    actor_profile: EmployeeProfile,
    actor_business_roles: list[str],
    actor_business_permissions: list[str],
    binding: AuthenticatedInputBinding,
    requested_value: str,
    user_message: str,
    existing_subject_employee_id: str,
) -> EmployeeProfile:
    """校验角色、原子权限、明确代办意图和目标档案后返回业务主体。"""

    base_context = {
        "actor_user_id": actor.id,
        "actor_role": actor.role,
        "actor_business_roles": actor_business_roles,
        "actor_business_permissions": actor_business_permissions,
        "actor_employee_id": actor_profile.employee_id,
        "requested_value": requested_value,
    }
    if not set(actor_business_roles).intersection(binding.allow_override_roles):
        raise SopIdentityContextError(
            "SUBJECT_OVERRIDE_FORBIDDEN",
            "当前员工未被授予该业务角色，只能办理本人业务。",
            audit_context=base_context,
        )
    if (
        binding.required_override_permission
        and binding.required_override_permission not in actor_business_permissions
    ):
        raise SopIdentityContextError(
            "SUBJECT_OVERRIDE_PERMISSION_REQUIRED",
            "当前员工角色未包含该业务权限，只能办理本人业务。",
            audit_context={
                **base_context,
                "required_permission": binding.required_override_permission,
            },
        )
    if binding.attribute is not AuthenticatedEmployeeAttribute.EMPLOYEE_ID:
        raise SopIdentityContextError(
            "SUBJECT_OVERRIDE_ATTRIBUTE_UNSUPPORTED",
            "当前流程不支持按该身份字段代办，请联系管理员处理。",
            audit_context=base_context,
        )
    explicit_in_turn = requested_value.casefold() in user_message.casefold()
    continuing_existing_subject = requested_value == existing_subject_employee_id
    if not explicit_in_turn and not continuing_existing_subject:
        raise SopIdentityContextError(
            "SUBJECT_OVERRIDE_NOT_EXPLICIT",
            "代办查询必须明确提供目标员工工号，请重新说明后再试。",
            audit_context=base_context,
        )
    target = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.employee_id == requested_value,
            EmployeeProfile.status == "active",
        )
    ).first()
    if target is None:
        raise SopIdentityContextError(
            "SUBJECT_EMPLOYEE_NOT_FOUND",
            "指定工号未绑定有效员工档案，无法办理代办查询。",
            audit_context=base_context,
        )
    return target


def _profile_attribute(
    profile: EmployeeProfile, attribute: AuthenticatedEmployeeAttribute
) -> Any:
    """从白名单枚举读取员工档案属性，禁止任意对象路径访问。"""

    return getattr(profile, attribute.value)


def _explicit_employee_profile_from_message(
    db: Session,
    *,
    tenant_id: str,
    binding: AuthenticatedInputBinding,
    user_message: str,
) -> EmployeeProfile | None:
    """从当前消息确定性识别已存在工号，避免模型漏抽槽位绕过代办校验。"""

    if binding.attribute is not AuthenticatedEmployeeAttribute.EMPLOYEE_ID:
        return None
    candidate_values = list(
        dict.fromkeys(
            re.findall(
                r"(?<![A-Za-z0-9_-])[A-Za-z0-9][A-Za-z0-9_-]{1,127}(?![A-Za-z0-9_-])",
                user_message,
            )
        )
    )[:64]
    if not candidate_values:
        return None
    matched_profiles = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.status == "active",
            EmployeeProfile.employee_id.in_(candidate_values),  # type: ignore[union-attr]
        )
    ).all()
    distinct_profiles = {
        profile.employee_id: profile
        for profile in matched_profiles
    }
    if len(distinct_profiles) > 1:
        raise SopIdentityContextError(
            "SUBJECT_EMPLOYEE_AMBIGUOUS",
            "当前消息包含多个员工工号，请一次只指定一名员工后重试。",
            audit_context={"matched_employee_ids": sorted(distinct_profiles)},
        )
    return next(iter(distinct_profiles.values()), None)
