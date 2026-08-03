"""
@Time       : 2026/07/22 22:10
@Author     : zhanglp8181
@File       : agent_execution.py
@CallChain  : Agent Loop/Tool API → ToolExecutor → AgentExecutionAuthorizer → 组织角色与权限目录
@Description: 在工具副作用边界校验数字员工受控执行的双重权限上限。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    BusinessRolePermission,
    EmployeeProfile,
    PermissionDefinition,
    utc_now,
)
from app.organization.permissions import user_permission_codes
from app.organization.query import resolve_organization_subtree_ids


@dataclass(frozen=True, slots=True)
class AgentExecutionDecision:
    """保存已冻结的数字员工授权上下文，供工具事件审计使用。"""

    permission_code: str
    role_code: str
    agent_role_binding_id: str
    supervisor_employee_profile_id: str
    actor_user_id: str
    authorization_mode: str
    scope_type: str
    scope_id: str

    def as_dict(self) -> dict[str, str]:
        """返回不含动态对象的可持久化审计快照。"""

        return {
            "permission_code": self.permission_code,
            "role_code": self.role_code,
            "agent_role_binding_id": self.agent_role_binding_id,
            "supervisor_employee_profile_id": self.supervisor_employee_profile_id,
            "actor_user_id": self.actor_user_id,
            "authorization_mode": self.authorization_mode,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
        }


class AgentExecutionDenied(Exception):
    """表示数字员工执行未满足统一授权契约。"""

    def __init__(self, code: str, message: str):
        """初始化稳定错误码和面向用户的拒绝原因。"""

        super().__init__(message)
        self.code = code
        self.message = message


class AgentExecutionAuthorizer:
    """复用组织角色目录，校验数字员工与当前调用人的权限交集。"""

    def __init__(self, db: Session):
        """绑定当前事务会话，保证授权判断与工具读取一致。"""

        self.db = db

    def authorize(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        actor_user_id: str | None,
        active_skill_id: str | None,
        allowed_skill_ids: list[str],
        permission_code: str,
        authorization_mode: str,
        organization_unit_id: str | None = None,
    ) -> AgentExecutionDecision:
        """
        校验受保护工具的 SOP 白名单、调用人权限和数字员工 execute 绑定。
        """

        self._assert_explicit_skill(active_skill_id, allowed_skill_ids)
        if not agent_id:
            raise AgentExecutionDenied(
                "AGENT_ID_REQUIRED", "受保护工具必须由已映射公司角色的数字员工执行。"
            )
        if not actor_user_id:
            raise AgentExecutionDenied(
                "ACTOR_USER_REQUIRED", "受保护工具缺少可审计的当前调用人。"
            )
        if authorization_mode == "caller_and_agent":
            self._assert_actor_permission(
                tenant_id,
                actor_user_id,
                permission_code,
                organization_unit_id=organization_unit_id,
            )
        elif authorization_mode == "workflow_delegated":
            self._assert_active_actor(tenant_id, actor_user_id)
        else:
            raise AgentExecutionDenied(
                "AGENT_AUTHORIZATION_MODE_INVALID", "工具配置了不支持的数字员工授权来源。"
            )
        return self._authorized_binding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            permission_code=permission_code,
            authorization_mode=authorization_mode,
            organization_unit_id=organization_unit_id,
        )

    def _assert_active_actor(self, tenant_id: str, actor_user_id: str) -> None:
        """校验流程委托的发起人是同租户活动员工，保留可审计责任主体。"""

        profile = self.db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == tenant_id,
                EmployeeProfile.user_id == actor_user_id,
                EmployeeProfile.status == "active",
            )
        ).first()
        if profile is None:
            raise AgentExecutionDenied(
                "ACTOR_EMPLOYEE_PROFILE_REQUIRED", "流程委托执行需要可审计的活动员工档案。"
            )

    def _assert_explicit_skill(
        self, active_skill_id: str | None, allowed_skill_ids: list[str]
    ) -> None:
        """要求受保护工具显式归属当前 SOP，禁止对话中越过流程调用。"""

        if not active_skill_id or active_skill_id not in allowed_skill_ids:
            raise AgentExecutionDenied(
                "AGENT_SOP_PERMISSION_REQUIRED",
                "受保护工具未明确允许由当前 SOP 调用。",
            )

    def _assert_actor_permission(
        self,
        tenant_id: str,
        actor_user_id: str,
        permission_code: str,
        *,
        organization_unit_id: str | None,
    ) -> None:
        """校验登录调用人的业务权限，防止数字员工放大调用人权限。"""

        if permission_code not in user_permission_codes(
            self.db,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            organization_unit_ids={organization_unit_id} if organization_unit_id else None,
        ):
            raise AgentExecutionDenied(
                "ACTOR_PERMISSION_REQUIRED",
                f"当前调用人缺少业务权限：{permission_code}。",
            )

    def _authorized_binding(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        actor_user_id: str,
        permission_code: str,
        authorization_mode: str,
        organization_unit_id: str | None,
    ) -> AgentExecutionDecision:
        """查找同时满足时点、组织范围、execute、监督者和角色权限的绑定。"""

        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != tenant_id
            or agent.status != "active"
            or agent.is_overall
        ):
            raise AgentExecutionDenied("AGENT_NOT_ACTIVE", "数字员工不存在或未启用。")

        permission = self.db.exec(
            select(PermissionDefinition).where(
                PermissionDefinition.tenant_id == tenant_id,
                PermissionDefinition.permission_code == permission_code,
                PermissionDefinition.status == "active",
            )
        ).first()
        if permission is None:
            raise AgentExecutionDenied(
                "PERMISSION_DEFINITION_NOT_ACTIVE", f"权限点不存在或未启用：{permission_code}。"
            )

        bindings = self.db.exec(
            select(AgentRoleBinding).where(
                AgentRoleBinding.tenant_id == tenant_id,
                AgentRoleBinding.agent_id == agent_id,
                AgentRoleBinding.status == "active",
            )
        ).all()
        effective_at = utc_now()
        execute_bindings = [
            item
            for item in bindings
            if item.assignment_mode == "execute"
            and (item.effective_from is None or item.effective_from <= effective_at)
            and (item.effective_until is None or item.effective_until > effective_at)
        ]
        if not execute_bindings:
            raise AgentExecutionDenied(
                "AGENT_EXECUTION_MODE_REQUIRED", "当前数字员工仅可辅助，未获授权受控执行。"
            )
        scoped_bindings = [
            binding
            for binding in execute_bindings
            if self._binding_matches_organization(
                binding,
                organization_unit_id=organization_unit_id,
            )
        ]
        if not scoped_bindings:
            raise AgentExecutionDenied(
                "AGENT_EXECUTION_SCOPE_REQUIRED",
                "数字员工执行角色不覆盖当前业务组织，或调用缺少组织上下文。",
            )

        missing_supervisor = False
        for binding in scoped_bindings:
            role = self.db.get(BusinessRole, binding.business_role_id)
            if role is None or role.tenant_id != tenant_id or role.status != "active":
                continue
            if not self._role_has_permission(tenant_id, role.id, permission.id):
                continue
            if not binding.supervisor_employee_profile_id:
                missing_supervisor = True
                continue
            supervisor = self.db.get(EmployeeProfile, binding.supervisor_employee_profile_id)
            if (
                supervisor is None
                or supervisor.tenant_id != tenant_id
                or supervisor.status != "active"
            ):
                missing_supervisor = True
                continue
            return AgentExecutionDecision(
                permission_code=permission_code,
                role_code=role.role_code,
                agent_role_binding_id=binding.id,
                supervisor_employee_profile_id=supervisor.id,
                actor_user_id=actor_user_id,
                authorization_mode=authorization_mode,
                scope_type=binding.scope_type,
                scope_id=binding.scope_id,
            )

        if missing_supervisor:
            raise AgentExecutionDenied(
                "AGENT_SUPERVISOR_REQUIRED", "数字员工的受控执行角色缺少有效人类监督者。"
            )
        raise AgentExecutionDenied(
            "AGENT_PERMISSION_REQUIRED", f"数字员工角色缺少业务权限：{permission_code}。"
        )

    def _binding_matches_organization(
        self,
        binding: AgentRoleBinding,
        *,
        organization_unit_id: str | None,
    ) -> bool:
        """验证数字员工角色绑定是否覆盖当前业务组织，非法历史范围默认不生效。"""

        if binding.scope_type == "tenant":
            return binding.scope_id == "*"
        if binding.scope_type != "org_unit" or not organization_unit_id:
            return False
        try:
            organization_ids = resolve_organization_subtree_ids(
                self.db,
                tenant_id=binding.tenant_id,
                root_org_unit_id=binding.scope_id,
                include_descendants=binding.include_descendants,
            )
        except ValueError:
            return False
        return organization_unit_id in organization_ids

    def _role_has_permission(
        self, tenant_id: str, business_role_id: str, permission_definition_id: str
    ) -> bool:
        """以关系表事实判定角色权限，不依赖旧 JSON 缓存。"""

        return (
            self.db.exec(
                select(BusinessRolePermission).where(
                    BusinessRolePermission.tenant_id == tenant_id,
                    BusinessRolePermission.business_role_id == business_role_id,
                    BusinessRolePermission.permission_definition_id == permission_definition_id,
                )
            ).first()
            is not None
        )
