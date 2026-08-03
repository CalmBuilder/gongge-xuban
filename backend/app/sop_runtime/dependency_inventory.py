"""
@Time       : 2026/07/29 15:18
@Author     : zhanglp8181
@File       : dependency_inventory.py
@CallChain  : SOP 迁移预检 → 当前发布定义 → M3/M4/M5 正式事实与资源绑定
@Description: 只读复核发布 SOP 的参与者、数字员工、工具和知识依赖，不代替运行时逐用户授权。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field
from sqlmodel import Session, select

from app.agents.branching import (
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    visible_tool_rows,
)
from app.agents.identity import (
    agent_is_published,
    agent_owner_user_id,
    agent_visibility_scope,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentRoleBinding,
    BusinessRole,
    BusinessRolePermission,
    EmployeeProfile,
    KnowledgeBase,
    MemberOrgAssignment,
    OrganizationUnit,
    PermissionDefinition,
    Skill,
    Tool,
    User,
    utc_now,
)
from app.organization.query import (
    current_assignment_predicates,
    resolve_organization_subtree_ids,
)
from app.sop_runtime.contracts import RuntimeContract
from app.sop_runtime.definition import (
    CompiledSopDefinition,
    HumanTaskKind,
    HumanTaskNode,
    ParticipantScopeResolver,
    ServiceTaskKind,
    ServiceTaskNode,
)
from app.sop_runtime.work_items import SopWorkItemService, WorkItemError


class DependencyReadiness(StrEnum):
    """描述发布定义在当前组织和资源事实下的新实例可用性。"""

    READY = "ready"
    ATTENTION_REQUIRED = "attention_required"
    BLOCKED = "blocked"


class HumanParticipantCoverage(RuntimeContract):
    """解释单个人工节点在当前组织事实下的候选人数和授权来源。"""

    node_id: str
    role_codes: tuple[str, ...] = ()
    action_permission_codes: tuple[str, ...] = ()
    exclude_initiator: bool = True
    declared_direct_user_count: int = Field(default=0, ge=0)
    direct_user_count: int = Field(default=0, ge=0)
    eligible_candidate_count: int = Field(default=0, ge=0)
    source_counts: dict[str, int] = Field(default_factory=dict)
    participant_scope_resolver: str
    participant_scope_org_unit_id: str | None = None
    contextual_scope: bool = False
    context_count: int = Field(default=0, ge=0)
    covered_context_count: int = Field(default=0, ge=0)
    uncovered_org_unit_ids: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()


class AgentDependencyPath(RuntimeContract):
    """解释一个数字员工从 SOP 资源绑定到可执行权限的完整只读路径。"""

    agent_id: str
    agent_name: str
    resource_binding_ids: tuple[str, ...]
    execution_role_codes: tuple[str, ...] = ()
    executable: bool
    issue_codes: tuple[str, ...] = ()


class SopDependencyAssessment(RuntimeContract):
    """保存单个发布头的只读业务依赖复核结果。"""

    readiness: DependencyReadiness
    issue_codes: tuple[str, ...] = ()
    human_task_count: int = Field(ge=0)
    tool_operation_count: int = Field(ge=0)
    knowledge_task_count: int = Field(ge=0)
    bound_agent_count: int = Field(ge=0)
    executable_agent_count: int = Field(ge=0)
    human_participants: tuple[HumanParticipantCoverage, ...] = ()
    agent_paths: tuple[AgentDependencyPath, ...] = ()


def build_sop_dependency_assessment(
    db: Session,
    *,
    skill: Skill,
    compiled_definition: CompiledSopDefinition,
) -> SopDependencyAssessment:
    """复用正式目录和资源可见性规则，判断是否至少存在一条可执行数字员工路径。"""

    human_nodes = tuple(
        node
        for node in compiled_definition.nodes
        if isinstance(node, HumanTaskNode)
        and node.config.kind is HumanTaskKind.STRUCTURED_WORK_ITEM
    )
    service_nodes = tuple(
        node for node in compiled_definition.nodes if isinstance(node, ServiceTaskNode)
    )
    tool_operations = tuple(
        dict.fromkeys(
            operation
            for node in service_nodes
            if node.config.kind is ServiceTaskKind.TOOL
            for operation in node.config.operations
        )
    )
    knowledge_task_count = sum(
        node.config.kind is ServiceTaskKind.KNOWLEDGE for node in service_nodes
    )
    issue_codes = _participant_issue_codes(
        db,
        tenant_id=skill.tenant_id,
        human_nodes=human_nodes,
    )
    human_participants = _human_participant_coverages(
        db,
        tenant_id=skill.tenant_id,
        human_nodes=human_nodes,
    )
    for participant in human_participants:
        issue_codes.update(participant.issue_codes)
    tools, tool_issue_codes = _tool_catalog(
        db,
        tenant_id=skill.tenant_id,
        skill_id=skill.skill_id,
        operations=tool_operations,
    )
    issue_codes.update(tool_issue_codes)

    agent_paths = _agent_dependency_paths(
        db,
        skill=skill,
        tools=tools,
        operations=tool_operations,
        requires_knowledge=knowledge_task_count > 0,
    )
    executable_agent_count = sum(path.executable for path in agent_paths)
    incomplete_agent_path = any(not path.executable for path in agent_paths)

    if not agent_paths:
        issue_codes.add("ACTIVE_AGENT_BINDING_REQUIRED")
    if agent_paths and executable_agent_count == 0:
        issue_codes.add("EXECUTABLE_AGENT_PATH_REQUIRED")
    if incomplete_agent_path:
        issue_codes.add("BOUND_AGENT_PATH_INCOMPLETE")

    blocking_codes = {
        code
        for code in issue_codes
        if code
        not in {
            "BOUND_AGENT_PATH_INCOMPLETE",
            "PARTICIPANT_SCOPE_CONTEXT_REQUIRED",
        }
    }
    if blocking_codes or executable_agent_count == 0:
        readiness = DependencyReadiness.BLOCKED
    elif issue_codes:
        readiness = DependencyReadiness.ATTENTION_REQUIRED
    else:
        readiness = DependencyReadiness.READY
    return SopDependencyAssessment(
        readiness=readiness,
        issue_codes=tuple(sorted(issue_codes)),
        human_task_count=len(human_nodes),
        tool_operation_count=len(tool_operations),
        knowledge_task_count=knowledge_task_count,
        bound_agent_count=len(agent_paths),
        executable_agent_count=executable_agent_count,
        human_participants=human_participants,
        agent_paths=agent_paths,
    )


def _human_participant_coverages(
    db: Session,
    *,
    tenant_id: str,
    human_nodes: tuple[HumanTaskNode, ...],
) -> tuple[HumanParticipantCoverage, ...]:
    """按运行时候选解析器生成节点级覆盖，并逐个验证当前发起组织上下文。"""

    service = SopWorkItemService(db)
    initiator_contexts = _active_initiator_contexts(db, tenant_id=tenant_id)
    coverages: list[HumanParticipantCoverage] = []
    for node in human_nodes:
        resolver = node.config.participant_scope_resolver
        contextual_scope = resolver in {
            ParticipantScopeResolver.INITIATOR_PRIMARY_ORG,
            ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE,
        }
        organization_unit_ids: set[str] | None = None
        issues: set[str] = set()
        context_count = 0
        covered_context_count = 0
        uncovered_org_unit_ids: list[str] = []
        sources: dict[str, dict[str, object]] = {}
        if resolver is ParticipantScopeResolver.EXPLICIT_ORG:
            root_org_unit_id = node.config.participant_scope_org_unit_id
            if root_org_unit_id:
                try:
                    organization_unit_ids = set(
                        resolve_organization_subtree_ids(
                            db,
                            tenant_id=tenant_id,
                            root_org_unit_id=root_org_unit_id,
                            include_descendants=True,
                        )
                    )
                except ValueError:
                    issues.add("PARTICIPANT_ORG_NOT_ACTIVE")
        elif contextual_scope:
            context_count = len(initiator_contexts)
            if not initiator_contexts:
                issues.add("PARTICIPANT_SCOPE_CONTEXT_REQUIRED")
            for org_unit_id, initiator_user_ids in initiator_contexts.items():
                try:
                    scoped_org_unit_ids = set(
                        resolve_organization_subtree_ids(
                            db,
                            tenant_id=tenant_id,
                            root_org_unit_id=org_unit_id,
                            include_descendants=(
                                resolver
                                is ParticipantScopeResolver.INITIATOR_PRIMARY_ORG_SUBTREE
                            ),
                        )
                    )
                    scoped_sources = service.preview_candidate_sources(
                        tenant_id=tenant_id,
                        role_codes=node.config.candidate_role_codes,
                        user_ids=node.config.candidate_user_ids,
                        organization_unit_ids=scoped_org_unit_ids,
                    )
                except (ValueError, WorkItemError):
                    scoped_sources = {}
                _merge_candidate_sources(sources, scoped_sources)
                if all(
                    bool(
                        set(scoped_sources)
                        - ({initiator_user_id} if node.config.exclude_initiator else set())
                    )
                    for initiator_user_id in initiator_user_ids
                ):
                    covered_context_count += 1
                else:
                    uncovered_org_unit_ids.append(org_unit_id)
            if uncovered_org_unit_ids:
                issues.add("PARTICIPANT_CONTEXT_UNCOVERED")
        if not contextual_scope or not initiator_contexts:
            try:
                preview_sources = service.preview_candidate_sources(
                    tenant_id=tenant_id,
                    role_codes=node.config.candidate_role_codes,
                    user_ids=node.config.candidate_user_ids,
                    organization_unit_ids=organization_unit_ids,
                )
                _merge_candidate_sources(sources, preview_sources)
            except WorkItemError as error:
                if error.code == "WORK_ITEM_ROLE_NOT_FOUND":
                    issues.add("PARTICIPANT_ROLE_NOT_ACTIVE")
                else:
                    issues.add(error.code)
        source_counts = {
            "direct_user": 0,
            "business_role": 0,
            "position_role": 0,
        }
        for source in sources.values():
            source_types = source.get("source_types")
            if not isinstance(source_types, set):
                continue
            for source_type in source_types:
                if source_type in source_counts:
                    source_counts[source_type] += 1
        if not sources and "PARTICIPANT_ROLE_NOT_ACTIVE" not in issues:
            issues.add("PARTICIPANT_NO_ELIGIBLE_CANDIDATE")
        coverages.append(
            HumanParticipantCoverage(
                node_id=node.node_id,
                role_codes=tuple(node.config.candidate_role_codes),
                action_permission_codes=tuple(
                    sorted(set(node.config.action_permissions.values()))
                ),
                exclude_initiator=node.config.exclude_initiator,
                declared_direct_user_count=len(node.config.candidate_user_ids),
                direct_user_count=source_counts["direct_user"],
                eligible_candidate_count=len(sources),
                source_counts=source_counts,
                participant_scope_resolver=resolver.value,
                participant_scope_org_unit_id=node.config.participant_scope_org_unit_id,
                contextual_scope=contextual_scope,
                context_count=context_count,
                covered_context_count=covered_context_count,
                uncovered_org_unit_ids=tuple(sorted(uncovered_org_unit_ids)),
                issue_codes=tuple(sorted(issues)),
            )
        )
    return tuple(coverages)


def _active_initiator_contexts(db: Session, *, tenant_id: str) -> dict[str, set[str]]:
    """按活动员工的当前主归属列出可用于动态参与者范围评估的发起上下文。"""

    rows = db.exec(
        select(MemberOrgAssignment.org_unit_id, EmployeeProfile.user_id)
        .join(
            EmployeeProfile,
            EmployeeProfile.id == MemberOrgAssignment.employee_profile_id,
        )
        .join(User, User.id == EmployeeProfile.user_id)
        .where(
            MemberOrgAssignment.tenant_id == tenant_id,
            MemberOrgAssignment.is_primary.is_(True),
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.status == "active",
            User.tenant_id == tenant_id,
            User.membership_status == "active",
            *current_assignment_predicates(),
        )
    ).all()
    contexts: dict[str, set[str]] = {}
    for org_unit_id, user_id in rows:
        contexts.setdefault(org_unit_id, set()).add(user_id)
    return contexts


def _merge_candidate_sources(
    target: dict[str, dict[str, object]],
    additions: dict[str, dict[str, object]],
) -> None:
    """合并不同组织上下文的候选来源，同时保留去重后的角色和来源类型。"""

    for user_id, addition in additions.items():
        current = target.setdefault(
            user_id,
            {
                "employee_profile_id": addition.get("employee_profile_id"),
                "role_codes": set(),
                "source_types": set(),
            },
        )
        for key in ("role_codes", "source_types"):
            current_values = current.get(key)
            addition_values = addition.get(key)
            if isinstance(current_values, set) and isinstance(addition_values, set):
                current_values.update(addition_values)


def _participant_issue_codes(
    db: Session,
    *,
    tenant_id: str,
    human_nodes: tuple[HumanTaskNode, ...],
) -> set[str]:
    """重新校验发布后可能漂移的业务角色、显式组织和动作权限目录。"""

    issue_codes: set[str] = set()
    role_codes = {
        role_code for node in human_nodes for role_code in node.config.candidate_role_codes
    }
    if role_codes:
        active_role_codes = set(
            db.exec(
                select(BusinessRole.role_code).where(
                    BusinessRole.tenant_id == tenant_id,
                    BusinessRole.role_code.in_(tuple(sorted(role_codes))),
                    BusinessRole.role_kind == "business",
                    BusinessRole.status == "active",
                )
            ).all()
        )
        if role_codes - active_role_codes:
            issue_codes.add("PARTICIPANT_ROLE_NOT_ACTIVE")

    explicit_org_ids = {
        node.config.participant_scope_org_unit_id
        for node in human_nodes
        if node.config.participant_scope_resolver is ParticipantScopeResolver.EXPLICIT_ORG
        and node.config.participant_scope_org_unit_id
    }
    if explicit_org_ids:
        active_org_ids = set(
            db.exec(
                select(OrganizationUnit.id).where(
                    OrganizationUnit.tenant_id == tenant_id,
                    OrganizationUnit.id.in_(tuple(sorted(explicit_org_ids))),
                    OrganizationUnit.status == "active",
                )
            ).all()
        )
        if explicit_org_ids - active_org_ids:
            issue_codes.add("PARTICIPANT_ORG_NOT_ACTIVE")

    permission_codes = {
        permission_code
        for node in human_nodes
        for permission_code in node.config.action_permissions.values()
    }
    if permission_codes:
        active_permission_codes = set(
            db.exec(
                select(PermissionDefinition.permission_code).where(
                    PermissionDefinition.tenant_id == tenant_id,
                    PermissionDefinition.permission_code.in_(
                        tuple(sorted(permission_codes))
                    ),
                    PermissionDefinition.status == "active",
                )
            ).all()
        )
        if permission_codes - active_permission_codes:
            issue_codes.add("ACTION_PERMISSION_NOT_ACTIVE")
    return issue_codes


def _tool_catalog(
    db: Session,
    *,
    tenant_id: str,
    skill_id: str,
    operations: tuple[str, ...],
) -> tuple[dict[str, Tool], set[str]]:
    """读取定义引用的工具并校验启用、SOP 白名单和权限目录。"""

    if not operations:
        return {}, set()
    rows = db.exec(
        select(Tool).where(
            Tool.tenant_id == tenant_id,
            Tool.name.in_(operations),
        )
    ).all()
    tools = {row.name: row for row in rows}
    issue_codes: set[str] = set()
    if set(operations) - set(tools):
        issue_codes.add("TOOL_NOT_FOUND")
    if any(not tool.enabled for tool in tools.values()):
        issue_codes.add("TOOL_NOT_ENABLED")
    if any(
        tool.allowed_skills_json and skill_id not in tool.allowed_skills_json
        for tool in tools.values()
    ):
        issue_codes.add("TOOL_SKILL_NOT_ALLOWED")
    permission_codes = {
        tool.required_permission_code
        for tool in tools.values()
        if tool.required_permission_code
    }
    if permission_codes:
        active_permission_codes = set(
            db.exec(
                select(PermissionDefinition.permission_code).where(
                    PermissionDefinition.tenant_id == tenant_id,
                    PermissionDefinition.permission_code.in_(
                        tuple(sorted(permission_codes))
                    ),
                    PermissionDefinition.status == "active",
                )
            ).all()
        )
        if permission_codes - active_permission_codes:
            issue_codes.add("TOOL_PERMISSION_NOT_ACTIVE")
    return tools, issue_codes


def _agent_dependency_paths(
    db: Session,
    *,
    skill: Skill,
    tools: dict[str, Tool],
    operations: tuple[str, ...],
    requires_knowledge: bool,
) -> tuple[AgentDependencyPath, ...]:
    """返回每个已装载数字员工的资源、角色和可执行性明细。"""

    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == skill.tenant_id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == skill.id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    bindings_by_agent: dict[str, list[AgentResourceBinding]] = {}
    for binding in bindings:
        bindings_by_agent.setdefault(binding.agent_id, []).append(binding)
    if not bindings_by_agent:
        return ()
    agents = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == skill.tenant_id,
            AgentProfile.id.in_(tuple(bindings_by_agent)),
            AgentProfile.is_overall == False,  # noqa: E712
        )
        .order_by(AgentProfile.id)
    ).all()
    paths: list[AgentDependencyPath] = []
    for agent in agents:
        issue_codes = _agent_path_issue_codes(
            db,
            skill=skill,
            agent=agent,
            tools=tools,
            operations=operations,
            requires_knowledge=requires_knowledge,
        )
        paths.append(
            AgentDependencyPath(
                agent_id=agent.id,
                agent_name=agent.name,
                resource_binding_ids=tuple(
                    sorted(binding.id for binding in bindings_by_agent[agent.id])
                ),
                execution_role_codes=_agent_execution_role_codes(db, agent),
                executable=not issue_codes,
                issue_codes=tuple(sorted(issue_codes)),
            )
        )
    return tuple(paths)


def _agent_path_issue_codes(
    db: Session,
    *,
    skill: Skill,
    agent: AgentProfile,
    tools: dict[str, Tool],
    operations: tuple[str, ...],
    requires_knowledge: bool,
) -> set[str]:
    """按正式身份和资源边界解释一条 Agent 路径为何可执行或被阻断。"""

    issue_codes: set[str] = set()

    if agent.status != "active" or not agent_owner_user_id(agent):
        issue_codes.add("AGENT_IDENTITY_NOT_EXECUTABLE")
    published = agent_is_published(agent)
    visibility = agent_visibility_scope(agent)
    if (published and visibility != "tenant") or (not published and visibility != "private"):
        issue_codes.add("AGENT_VISIBILITY_NOT_EXECUTABLE")
    visible_tool_names = {
        row.name
        for row in visible_tool_rows(
            db,
            skill.tenant_id,
            agent.id,
            include_inactive=False,
        )
    }
    if set(operations) - visible_tool_names:
        issue_codes.add("AGENT_TOOL_BINDING_REQUIRED")
    for operation in operations:
        tool = tools.get(operation)
        if tool is None or not tool.enabled:
            issue_codes.add("AGENT_TOOL_NOT_EXECUTABLE")
            continue
        if tool.required_permission_code and not _agent_has_execution_permission(
            db,
            tenant_id=skill.tenant_id,
            agent=agent,
            permission_code=tool.required_permission_code,
        ):
            issue_codes.add("AGENT_EXECUTION_PERMISSION_REQUIRED")
    if requires_knowledge and not _agent_has_visible_knowledge(db, agent):
        issue_codes.add("AGENT_KNOWLEDGE_BINDING_REQUIRED")
    return issue_codes


def _agent_execution_role_codes(db: Session, agent: AgentProfile) -> tuple[str, ...]:
    """返回数字员工当前租户级 execute 绑定引用的活动业务角色编码。"""

    bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == agent.tenant_id,
            AgentRoleBinding.agent_id == agent.id,
            AgentRoleBinding.status == "active",
            AgentRoleBinding.assignment_mode == "execute",
            AgentRoleBinding.scope_type == "tenant",
            AgentRoleBinding.scope_id == "*",
        )
    ).all()
    effective_at = utc_now()
    role_ids = tuple(
        dict.fromkeys(
            binding.business_role_id
            for binding in bindings
            if _agent_binding_is_effective(binding, effective_at)
        )
    )
    if not role_ids:
        return ()
    roles = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == agent.tenant_id,
            BusinessRole.id.in_(role_ids),
            BusinessRole.status == "active",
        )
    ).all()
    return tuple(sorted(role.role_code for role in roles))


def _agent_has_execution_permission(
    db: Session,
    *,
    tenant_id: str,
    agent: AgentProfile,
    permission_code: str,
) -> bool:
    """验证受保护工具的 Agent 侧 execute、监督者和角色权限事实。"""

    if agent.is_overall:
        return False
    permission = db.exec(
        select(PermissionDefinition).where(
            PermissionDefinition.tenant_id == tenant_id,
            PermissionDefinition.permission_code == permission_code,
            PermissionDefinition.status == "active",
        )
    ).first()
    if permission is None:
        return False
    bindings = db.exec(
        select(AgentRoleBinding).where(
            AgentRoleBinding.tenant_id == tenant_id,
            AgentRoleBinding.agent_id == agent.id,
            AgentRoleBinding.status == "active",
            AgentRoleBinding.assignment_mode == "execute",
            AgentRoleBinding.scope_type == "tenant",
            AgentRoleBinding.scope_id == "*",
        )
    ).all()
    effective_at = utc_now()
    for binding in bindings:
        if not _agent_binding_is_effective(binding, effective_at):
            continue
        role = db.get(BusinessRole, binding.business_role_id)
        supervisor = (
            db.get(EmployeeProfile, binding.supervisor_employee_profile_id)
            if binding.supervisor_employee_profile_id
            else None
        )
        if (
            role is None
            or role.tenant_id != tenant_id
            or role.status != "active"
            or supervisor is None
            or supervisor.tenant_id != tenant_id
            or supervisor.status != "active"
        ):
            continue
        role_permission = db.exec(
            select(BusinessRolePermission).where(
                BusinessRolePermission.tenant_id == tenant_id,
                BusinessRolePermission.business_role_id == role.id,
                BusinessRolePermission.permission_definition_id == permission.id,
            )
        ).first()
        if role_permission is not None:
            return True
    return False


def _agent_binding_is_effective(binding: AgentRoleBinding, effective_at: datetime) -> bool:
    """判断数字员工角色绑定是否覆盖当前依赖评估时点。"""

    if binding.effective_from is not None and binding.effective_from > effective_at:
        return False
    return binding.effective_until is None or binding.effective_until > effective_at


def _agent_has_visible_knowledge(db: Session, agent: AgentProfile) -> bool:
    """只读判断数字员工是否至少绑定一个当前活动且可见的知识库。"""

    if agent.is_overall:
        rows = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == agent.tenant_id,
                KnowledgeBase.status == "active",
            )
        ).all()
        return any(
            is_open_gallery_resource(
                db,
                agent.tenant_id,
                "knowledge_base",
                row,
            )
            for row in rows
        )
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == agent.tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "knowledge_base",
            AgentResourceBinding.status == "active",
        )
    ).all()
    for binding in bindings:
        row = db.get(KnowledgeBase, binding.resource_id)
        if (
            row is not None
            and row.tenant_id == agent.tenant_id
            and row.status == "active"
            and is_bound_resource_visible_for_agent(
                db,
                agent.tenant_id,
                "knowledge_base",
                row,
                binding,
            )
        ):
            return True
    return False
