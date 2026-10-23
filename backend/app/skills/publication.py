"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : publication.py
@CallChain  : 原Skill API/人工候选确认 → 原编译/参与者/工具/分支版本 → 原发布事务
@Description: 从原API提取相同发布职责供共享，调用方负责授权和事务提交，不创建第二发布器。
"""
from fastapi import HTTPException
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_agent_skill_branch,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    mark_resource_open_gallery,
    project_skill_with_branch,
    publish_branch_version,
    update_branch_skill,
)
from app.db.models import (
    AgentProfile, BusinessRole, OrganizationUnit, PermissionDefinition, Skill, Tool, utc_now,
)
from app.skills.skill_schema import SkillCard
from app.sop_runtime import CompiledSopDefinition, SopCompilationError, compile_legacy_skill_card
from app.sop_runtime.capabilities import DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.definition import CollectInputNode, HumanTaskNode, HumanTaskKind, ParticipantScopeResolver
from app.sop_runtime.versioning import PublishedVersionConflictError, write_skill_version


def compile_skill_for_publication(
    content: SkillCard | dict[str, object],
) -> CompiledSopDefinition:
    """在写入 published 状态前编译定义，并转换为可修复的 HTTP 诊断。"""

    try:
        compiled_definition = compile_legacy_skill_card(content)
    except SopCompilationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "diagnostics": [
                    diagnostic.model_dump(mode="json") for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    execution_mode = (
        content.execution_mode if isinstance(content, SkillCard) else content.get("execution_mode")
    )
    unsupported = DEFAULT_CAPABILITY_REGISTRY.non_executable_nodes(compiled_definition)
    if execution_mode == "deterministic" and unsupported:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CAPABILITY_NOT_EXECUTABLE",
                "nodes": [
                    {"node_id": node_id, "capability": capability}
                    for node_id, capability in unsupported
                ],
            },
        )
    return compiled_definition


def validate_participant_references_for_publication(
    db: Session,
    tenant_id: str,
    compiled_definition: CompiledSopDefinition,
) -> None:
    """发布前校验结构化参与角色和动作权限均来自当前有效目录。"""

    referenced_role_codes = {
        role_code
        for node in compiled_definition.nodes
        if isinstance(node, HumanTaskNode)
        and node.config.kind is HumanTaskKind.STRUCTURED_WORK_ITEM
        for role_code in node.config.candidate_role_codes
    }
    if referenced_role_codes:
        active_roles = db.exec(
            select(BusinessRole).where(
                BusinessRole.tenant_id == tenant_id,
                BusinessRole.role_code.in_(tuple(sorted(referenced_role_codes))),
                BusinessRole.role_kind == "business",
                BusinessRole.status == "active",
            ).order_by(BusinessRole.id).with_for_update().execution_options(populate_existing=True)
        ).all()
        missing_role_codes = sorted(
            referenced_role_codes - {role.role_code for role in active_roles}
        )
        if missing_role_codes:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PARTICIPANT_ROLE_NOT_FOUND",
                    "message": "结构化人工任务引用了不存在或已停用的公司业务角色。",
                    "role_codes": missing_role_codes,
                },
            )

    explicit_org_unit_ids = {
        node.config.participant_scope_org_unit_id
        for node in compiled_definition.nodes
        if isinstance(node, HumanTaskNode)
        and node.config.kind is HumanTaskKind.STRUCTURED_WORK_ITEM
        and node.config.participant_scope_resolver is ParticipantScopeResolver.EXPLICIT_ORG
        and node.config.participant_scope_org_unit_id
    }
    if explicit_org_unit_ids:
        active_org_units = db.exec(
            select(OrganizationUnit).where(
                OrganizationUnit.tenant_id == tenant_id,
                OrganizationUnit.id.in_(tuple(sorted(explicit_org_unit_ids))),
                OrganizationUnit.status == "active",
            ).order_by(OrganizationUnit.id).with_for_update().execution_options(populate_existing=True)
        ).all()
        missing_org_unit_ids = sorted(
            explicit_org_unit_ids - {organization.id for organization in active_org_units}
        )
        if missing_org_unit_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PARTICIPANT_ORG_NOT_FOUND",
                    "message": "结构化人工任务引用了不存在、停用或跨租户的组织。",
                    "org_unit_ids": missing_org_unit_ids,
                },
            )

    referenced_permission_codes: set[str] = set()
    for node in compiled_definition.nodes:
        if isinstance(node, HumanTaskNode):
            referenced_permission_codes.update(node.config.action_permissions.values())
        elif isinstance(node, CollectInputNode):
            referenced_permission_codes.update(
                binding.required_override_permission
                for binding in node.config.input_bindings.values()
                if binding.required_override_permission
            )
    if not referenced_permission_codes:
        return
    active_permissions = db.exec(
        select(PermissionDefinition).where(
            PermissionDefinition.tenant_id == tenant_id,
            PermissionDefinition.permission_code.in_(tuple(sorted(referenced_permission_codes))),
            PermissionDefinition.status == "active",
        ).order_by(PermissionDefinition.id).with_for_update().execution_options(populate_existing=True)
    ).all()
    missing_permission_codes = sorted(
        referenced_permission_codes
        - {permission.permission_code for permission in active_permissions}
    )
    if missing_permission_codes:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ACTION_PERMISSION_NOT_FOUND",
                "message": "SOP 引用了不存在或已停用的业务权限点。",
                "permission_codes": missing_permission_codes,
            },
        )


def sync_skill_tool_bindings(
    db: Session,
    tenant_id: str,
    skill_id: str,
    content: dict[str, object],
) -> None:
    """沿原工具allowed_skills绑定规则写入，事务由原发布调用方统一提交。"""
    tool_names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if not value.startswith("call_tool:"):
                    continue
                tool_name = value.split(":", 1)[1].strip()
                if tool_name:
                    tool_names.add(tool_name)
    if not tool_names:
        return

    rows = db.exec(
        select(Tool).where(
            Tool.tenant_id == tenant_id,
            Tool.name.in_(sorted(tool_names)),
        )
    ).all()
    for row in rows:
        allowed_skills = [
            str(item) for item in (row.allowed_skills_json or []) if str(item).strip()
        ]
        if skill_id in allowed_skills:
            continue
        row.allowed_skills_json = [*allowed_skills, skill_id]
        row.updated_at = utc_now()
        db.add(row)


def publish_skill_head(
    db: Session,
    skill: Skill,
    agent: AgentProfile | None,
    *,
    content: SkillCard | None = None,
    derived_from_version_id: str | None = None,
) -> Skill:
    """同事务复用原发布编译/绑定/分支/不可变快照，人工候选只提供新正文。"""

    branch = ensure_agent_skill_branch(db, skill.tenant_id, agent.id, skill) if agent and not agent.is_overall else None
    raw = content.model_dump() if content else (branch.content_json if branch else skill.content_json)
    compiled = compile_skill_for_publication(raw)
    validate_participant_references_for_publication(db, skill.tenant_id, compiled)
    if branch:
        if content:
            branch = update_branch_skill(db, skill.tenant_id, agent.id, skill, raw,
                                         "人工确认反馈改进",
                                         derived_from_version_id=derived_from_version_id)
            compiled = compile_skill_for_publication(branch.content_json)
        branch.status = "active"
        branch.updated_at = utc_now()
        db.add(branch)
        sync_skill_tool_bindings(db, skill.tenant_id, skill.skill_id, branch.content_json)
        ensure_private_resource_binding(db, skill.tenant_id, agent.id, "skill", skill.id, "active")
        publish_branch_version(db, skill, branch, compiled_definition=compiled,
                               derived_from_version_id=derived_from_version_id)
        return project_skill_with_branch(skill, branch, "active")
    if content:
        skill.version = content.version
        skill.name = content.name
        skill.business_domain = content.business_domain
        skill.description = content.description
        skill.content_json = raw
    skill.status = "published"
    sync_skill_tool_bindings(db, skill.tenant_id, skill.skill_id, skill.content_json)
    mark_resource_open_gallery(skill)
    skill.updated_at = utc_now()
    db.add(skill)
    db.flush()
    ensure_open_gallery_binding(db, skill.tenant_id, "skill", skill.id, "active")
    try:
        result = write_skill_version(db, skill, compiled_definition=compiled,
                                    derived_from_version_id=derived_from_version_id)
    except PublishedVersionConflictError as error:
        raise HTTPException(409, {"code": "PUBLISHED_SKILL_VERSION_IMMUTABLE",
                                 "skill_id": error.skill_id, "version": error.version}) from error
    object.__setattr__(skill, "publication_identity", {
        "id": result.version.id, "scope_agent_id": "", "checksum": result.version.compiled_definition_checksum,
    })
    return skill
