"""
@Time       : 2026/07/22 22:31
@Author     : zhanglp8181
@File       : test_agent_execution_authorization.py
@CallChain  : pytest → AgentExecutionAuthorizer/ToolExecutor → 组织角色与权限目录
@Description: 验证数字员工不放大调用人权限并在工具边界留存审计快照。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.branching import ensure_private_resource_binding
from app.db.models import (
    AgentProfile,
    AgentRoleBinding,
    BusinessRole,
    BusinessRolePermission,
    EmployeeProfile,
    EmployeeRoleAssignment,
    OrganizationUnit,
    PermissionDefinition,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.organization.agent_execution import AgentExecutionAuthorizer, AgentExecutionDenied
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


PERMISSION_CODE = "it.ticket.resolve"


def test_assist_binding_cannot_execute_protected_tool() -> None:
    """验证 assist 映射即使关联了有权角色也不能执行受保护工具。"""

    with _test_session() as db:
        context = _seed_authorization_context(db, assignment_mode="assist")

        denied = _authorize_and_capture(db, context)

        assert denied.code == "AGENT_EXECUTION_MODE_REQUIRED"


def test_execute_binding_cannot_expand_actor_permission() -> None:
    """验证数字员工 execute 角色不能替无权调用人放大业务权限。"""

    with _test_session() as db:
        context = _seed_authorization_context(
            db, assignment_mode="execute", assign_actor_role=False
        )

        denied = _authorize_and_capture(db, context)

        assert denied.code == "ACTOR_PERMISSION_REQUIRED"


def test_execute_binding_requires_active_supervisor() -> None:
    """验证 execute 映射缺少有效人类监督者时以稳定错误拒绝。"""

    with _test_session() as db:
        context = _seed_authorization_context(
            db, assignment_mode="execute", include_supervisor=False
        )

        denied = _authorize_and_capture(db, context)

        assert denied.code == "AGENT_SUPERVISOR_REQUIRED"


def test_protected_tool_requires_explicit_current_sop() -> None:
    """验证受保护工具不能在未显式绑定的 SOP 或普通对话中执行。"""

    with _test_session() as db:
        context = _seed_authorization_context(db, assignment_mode="execute")
        context["active_skill_id"] = "unlisted_sop"

        denied = _authorize_and_capture(db, context)

        assert denied.code == "AGENT_SOP_PERMISSION_REQUIRED"


def test_tool_executor_persists_successful_authorization_snapshot(monkeypatch) -> None:
    """验证工具成功执行时冻结权限、数字员工角色、监督者和调用人。"""

    class FakeClient:
        """返回固定 HTTP 响应，使测试只聚焦授权边界。"""

        def __init__(self, *args: object, **kwargs: object):
            """接受与 httpx.Client 相同的构造参数但不发起网络请求。"""

        def __enter__(self) -> FakeClient:
            """返回可作为上下文管理器的当前实例。"""

            return self

        def __exit__(self, *args: object) -> None:
            """退出模拟客户端时无需释放外部资源。"""

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            """生成包含已修复标志的成功响应。"""

            return httpx.Response(
                200,
                json={"resolved": True},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    with _test_session() as db:
        context = _seed_authorization_context(db, assignment_mode="execute")
        tool = Tool(
            id="tool_resolve_ticket",
            tenant_id="tenant_demo",
            name="it.ticket.resolve",
            method="POST",
            url="https://example.test/tickets/resolve",
            allowed_skills_json=["it_fault_report"],
            required_permission_code=PERMISSION_CODE,
            enabled=True,
        )
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(
            db, "tenant_demo", context["agent_id"], "tool", tool.id, "active"
        )
        db.commit()

        result = ToolExecutor(db).execute(
            tenant_id="tenant_demo",
            tool_call=ToolCall(name=tool.name, arguments={"ticket_id": "T-1"}),
            active_skill_id="it_fault_report",
            agent_id=context["agent_id"],
            actor_user_id=context["actor_user_id"],
        )

        assert result.success is True
        assert result.authorization_context == {
            "permission_code": PERMISSION_CODE,
            "role_code": "it_support_engineer",
            "agent_role_binding_id": "agent_role_it_support",
            "supervisor_employee_profile_id": "employee_supervisor",
            "actor_user_id": "user_engineer",
            "authorization_mode": "caller_and_agent",
            "scope_type": "tenant",
            "scope_id": "*",
        }


def test_expired_agent_role_binding_cannot_authorize_protected_tool() -> None:
    """数字员工执行角色超过有效期后必须立即失权，不能只看 active 状态。"""

    with _test_session() as db:
        context = _seed_authorization_context(db, assignment_mode="execute")
        binding = db.get(AgentRoleBinding, "agent_role_it_support")
        assert binding is not None
        binding.effective_until = utc_now() - timedelta(seconds=1)
        db.add(binding)
        db.commit()

        denied = _authorize_and_capture(db, context)

        assert denied.code == "AGENT_EXECUTION_MODE_REQUIRED"


def test_org_scoped_agent_binding_requires_matching_business_context() -> None:
    """组织级数字员工绑定必须显式获得业务组织，且只能覆盖本组织或声明的下级。"""

    with _test_session() as db:
        context = _seed_authorization_context(db, assignment_mode="execute")
        root = OrganizationUnit(
            id="org_finance",
            tenant_id="tenant_demo",
            code="FINANCE",
            name="财务部",
            unit_type_code="department",
            tree_path="/org_finance",
            depth=0,
            is_root=True,
            root_tenant_id="tenant_demo",
        )
        child = OrganizationUnit(
            id="org_finance_shared",
            tenant_id="tenant_demo",
            parent_id=root.id,
            code="FINANCE_SHARED",
            name="财务共享组",
            unit_type_code="project_group",
            tree_path="/org_finance/org_finance_shared",
            depth=1,
        )
        sibling = OrganizationUnit(
            id="org_hr",
            tenant_id="tenant_demo",
            code="HR",
            name="人力资源部",
            unit_type_code="department",
            tree_path="/org_hr",
            depth=0,
        )
        db.add(root)
        db.add(child)
        db.add(sibling)
        binding = db.get(AgentRoleBinding, "agent_role_it_support")
        assert binding is not None
        binding.scope_type = "org_unit"
        binding.scope_id = root.id
        binding.include_descendants = True
        db.add(binding)
        db.commit()

        missing_context = _authorize_and_capture(db, context)
        assert missing_context.code == "AGENT_EXECUTION_SCOPE_REQUIRED"

        denied_sibling = _authorize_and_capture(
            db,
            context,
            organization_unit_id=sibling.id,
        )
        assert denied_sibling.code == "AGENT_EXECUTION_SCOPE_REQUIRED"

        decision = AgentExecutionAuthorizer(db).authorize(
            tenant_id="tenant_demo",
            agent_id=str(context["agent_id"]),
            actor_user_id=str(context["actor_user_id"]),
            active_skill_id=str(context["active_skill_id"]),
            allowed_skill_ids=list(context["allowed_skill_ids"]),
            permission_code=PERMISSION_CODE,
            authorization_mode=str(context["authorization_mode"]),
            organization_unit_id=child.id,
        )

        assert decision.agent_role_binding_id == binding.id
        assert decision.scope_type == "org_unit"
        assert decision.scope_id == root.id


def _authorize_and_capture(
    db: Session,
    context: dict[str, str | list[str]],
    *,
    organization_unit_id: str | None = None,
) -> AgentExecutionDenied:
    """执行授权并返回预期的拒绝异常，避免测试分支重复捕获逻辑。"""

    try:
        AgentExecutionAuthorizer(db).authorize(
            tenant_id="tenant_demo",
            agent_id=str(context["agent_id"]),
            actor_user_id=str(context["actor_user_id"]),
            active_skill_id=str(context["active_skill_id"]),
            allowed_skill_ids=list(context["allowed_skill_ids"]),
            permission_code=PERMISSION_CODE,
            authorization_mode=str(context.get("authorization_mode") or "caller_and_agent"),
            organization_unit_id=organization_unit_id,
        )
    except AgentExecutionDenied as exc:
        return exc
    raise AssertionError("预期数字员工执行被拒绝")


def _seed_authorization_context(
    db: Session,
    *,
    assignment_mode: str,
    assign_actor_role: bool = True,
    include_supervisor: bool = True,
) -> dict[str, str | list[str]]:
    """构造同一租户下的调用人、角色权限、数字员工和监督者事实。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    actor = User(
        id="user_engineer",
        tenant_id="tenant_demo",
        username="engineer",
        role="member",
        password_hash="hashed",
    )
    actor_profile = EmployeeProfile(
        id="employee_engineer",
        tenant_id="tenant_demo",
        user_id=actor.id,
        employee_id="E100",
        status="active",
    )
    supervisor_user = User(
        id="user_supervisor",
        tenant_id="tenant_demo",
        username="supervisor",
        role="member",
        password_hash="hashed",
    )
    supervisor = EmployeeProfile(
        id="employee_supervisor",
        tenant_id="tenant_demo",
        user_id=supervisor_user.id,
        employee_id="E200",
        status="active",
    )
    role = BusinessRole(
        id="role_it_support",
        tenant_id="tenant_demo",
        role_code="it_support_engineer",
        name="IT 支持工程师",
        category="information_technology",
        status="active",
    )
    permission = PermissionDefinition(
        id="permission_ticket_resolve",
        tenant_id="tenant_demo",
        permission_code=PERMISSION_CODE,
        name="提交 IT 工单解决结果",
        category="information_technology",
        resource="it.ticket",
        action="resolve",
        status="active",
    )
    agent = AgentProfile(
        id="agent_it_support",
        tenant_id="tenant_demo",
        name="IT 支持专员",
        status="active",
    )
    db.add(actor)
    db.add(actor_profile)
    db.add(supervisor_user)
    db.add(supervisor)
    db.add(role)
    db.add(permission)
    db.add(agent)
    db.flush()
    db.add(
        BusinessRolePermission(
            tenant_id="tenant_demo",
            business_role_id=role.id,
            permission_definition_id=permission.id,
        )
    )
    if assign_actor_role:
        db.add(
            EmployeeRoleAssignment(
                tenant_id="tenant_demo",
                employee_profile_id=actor_profile.id,
                business_role_id=role.id,
                status="active",
            )
        )
    db.add(
        AgentRoleBinding(
            id="agent_role_it_support",
            tenant_id="tenant_demo",
            agent_id=agent.id,
            business_role_id=role.id,
            assignment_mode=assignment_mode,
            supervisor_employee_profile_id=supervisor.id if include_supervisor else None,
            status="active",
        )
    )
    db.commit()
    return {
        "agent_id": agent.id,
        "actor_user_id": actor.id,
        "active_skill_id": "it_fault_report",
        "allowed_skill_ids": ["it_fault_report"],
        "authorization_mode": "caller_and_agent",
    }


def test_workflow_delegation_requires_actor_identity_but_not_actor_grant_permission() -> None:
    """验证已发布 SOP 流程委托不要求申请人事先拥有被委托的执行权限。"""

    with _test_session() as db:
        context = _seed_authorization_context(
            db, assignment_mode="execute", assign_actor_role=False
        )
        decision = AgentExecutionAuthorizer(db).authorize(
            tenant_id="tenant_demo",
            agent_id=str(context["agent_id"]),
            actor_user_id=str(context["actor_user_id"]),
            active_skill_id=str(context["active_skill_id"]),
            allowed_skill_ids=list(context["allowed_skill_ids"]),
            permission_code=PERMISSION_CODE,
            authorization_mode="workflow_delegated",
        )

        assert decision.authorization_mode == "workflow_delegated"
        assert decision.role_code == "it_support_engineer"


def _test_session() -> Session:
    """创建每个测试独立的 SQLite 内存会话并初始化完整元数据。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
