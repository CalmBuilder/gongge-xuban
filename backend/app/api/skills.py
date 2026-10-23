"""
@Time       : 2026/07/22 05:20
@Author     : zhanglp8181
@File       : skills.py
@CallChain  : 企业 SOP API → 权限/发布编译 → Skill/SkillVersion/Agent 分支
@Description: 管理 SOP 的创建、编辑、发布、版本、分支、统计和文件提取接口。
"""

from __future__ import annotations

import base64
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from typing import Annotated, TypeVar
from io import BytesIO
from time import sleep
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.agents.branching import (
    branch_versions,
    ensure_agent_skill_branch,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_open_gallery_resource,
    mark_resource_open_gallery,
    project_skill_with_branch,
    publish_branch_version,
    require_overall_agent,
    rollback_branch,
    update_branch_skill,
    user_creator_metadata,
    visible_tool_rows,
    visible_skill_rows,
)
from app.async_jobs import enqueue_async_job
from app.skills.publication import (
    compile_skill_for_publication as _compile_skill_for_publication,
    sync_skill_tool_bindings as _sync_skill_tool_bindings,
    validate_participant_references_for_publication as _validate_participant_references_for_publication,
    publish_skill_head,
)
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentResourceBinding,
    AgentSkillBranchVersion,
    ModelConfig,
    Skill,
    SkillFeedback,
    SkillVersion,
    User,
    utc_now,
)
from app.llm import LLMError
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant
from app.skills import SkillDistiller, SkillEditor
from app.skills import candidates as feedback_candidates
from app.skills.candidate_rewrite import (
    MODEL_FAILURE,
    PreparedCandidateRewrite,
    candidate_guard,
    cancel_candidate_rewrite,
    prepare_candidate_rewrite,
    rewrite_candidate,
    settle_candidate_rewrite,
)
from app.skills.candidate_schema import (
    CandidateActionRequest, CandidateCreateRequest, CandidatePublishReceipt,
    CandidatePublishRequest, CandidateResetRead, CandidateResetRequest,
    CandidateSaveRequest, SkillCandidateRead, SkillEditorContextRead,
)
from app.skills.edit_context import start_skill_write
from app.skills.skill_schema import (
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillDistillResponse,
    SkillFileExtractRequest,
    SkillFileExtractResponse,
    SkillRead,
    SkillRewriteRequest,
    SkillRewriteResponse,
    SkillVersionRead,
    SkillUpdateRequest,
)
from app.skills.stream_jobs import SkillStreamEvent, SkillStreamJob, stream_jobs
from app.skills.read import skill_read, stats_for as _stats_for, stats_key as _stats_key
from app.skills.generation_safety import tool_generation_payload as _tool_generation_payload
from app.skills.step_ids import skill_card_with_unique_step_ids
from app.sop_runtime import (
    CompiledSopDefinition,
)
from app.sop_runtime.versioning import (
    PublishedVersionConflictError,
    next_derived_skill_version,
    write_skill_version,
)

router = APIRouter(
    prefix="/api/enterprise/skills",
    tags=["enterprise:skills"],
    dependencies=[Depends(get_current_user)],
)


def skill_version_read(
    row: SkillVersion, stats: dict[str, dict[str, float | int]] | None = None
) -> SkillVersionRead:
    """将不可变版本快照及其独立统计转换为 API 响应。"""

    skill_stats = _stats_for(stats or {}, row.skill_id, row.version)
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillVersionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        content_checksum=row.content_checksum,
        compiled_definition_checksum=row.compiled_definition_checksum,
        meta_model_version=row.meta_model_version,
        source_schema_version=row.source_schema_version,
        published_at=row.published_at.isoformat() if row.published_at else None,
        derived_from_version_id=row.derived_from_version_id,
        runtime_version_id=row.id if row.status == "published" else None,
        scope_agent_id=row.scope_agent_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _branch_version_read(row: AgentSkillBranchVersion, db: Session) -> SkillVersionRead:
    """管理历史ID保持原含义，真实发布身份必须由同范围且相同正文的快照证明。"""

    publication = db.exec(select(SkillVersion).where(
        SkillVersion.tenant_id == row.tenant_id,
        SkillVersion.skill_id == row.skill_id,
        SkillVersion.scope_agent_id == row.agent_id,
        SkillVersion.version == row.version,
        SkillVersion.status == "published",
    )).first()
    if publication and publication.content_json != row.content_json:
        publication = None
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillVersionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=content.name,
        business_domain=content.business_domain,
        description=content.description,
        content=content,
        status=row.status,
        call_count=0,
        positive_feedback_count=0,
        negative_feedback_count=0,
        positive_rate=0.0,
        negative_rate=0.0,
        agent_id=row.agent_id,
        branch_sync_state=row.sync_state,
        branch_base_version=row.base_version,
        runtime_version_id=publication.id if publication else None,
        scope_agent_id=row.agent_id,
        content_checksum=publication.content_checksum if publication else None,
        compiled_definition_checksum=publication.compiled_definition_checksum if publication else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


CandidateResult = TypeVar("CandidateResult")


def _candidate_transaction(db: Session, operation: Callable[[], CandidateResult]) -> CandidateResult:
    """候选编辑/读取释放短屏障；任何失败回滚全部未提交对象和关联。"""
    try:
        result = operation()
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(409, "FEEDBACK_CANDIDATE_WRITE_CONFLICT") from error


@router.post("/feedback-candidates", response_model=SkillCandidateRead)
def create_feedback_candidate(request: CandidateCreateRequest,
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> SkillCandidateRead:
    """有效本人分享进入隔离候选；基础由服务端沿原范围解析。"""
    return _candidate_transaction(db, lambda: feedback_candidates.create_candidate(db, request, current_user))


@router.get("/feedback-candidates/editor-context", response_model=SkillEditorContextRead)
def get_feedback_editor_context(tenant_id: str = Query(...),
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> SkillEditorContextRead:
    """刷新读取当前操作者的唯一编辑关联，不信任缓存候选正文。"""
    return _candidate_transaction(db, lambda: feedback_candidates.editor_context(db, tenant_id, current_user))


@router.get("/feedback-candidates/{candidate_id}", response_model=SkillCandidateRead)
def get_feedback_candidate(candidate_id: str, tenant_id: str = Query(...),
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> SkillCandidateRead:
    """读取时重新核验来源/权限/base，失效正文不返回。"""
    return _candidate_transaction(db, lambda: feedback_candidates.get_candidate(db, candidate_id, tenant_id, current_user))


@router.put("/feedback-candidates/{candidate_id}", response_model=SkillCandidateRead)
def save_feedback_candidate(candidate_id: str, request: CandidateSaveRequest,
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> SkillCandidateRead:
    """人工保存只更新候选，不改原Skill活动头和不可变发布。"""
    return _candidate_transaction(db, lambda: feedback_candidates.save_candidate(db, candidate_id, request, current_user))


@router.post("/feedback-candidates/{candidate_id}/reject", response_model=SkillCandidateRead)
def reject_feedback_candidate(candidate_id: str, request: CandidateActionRequest,
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> SkillCandidateRead:
    """拒绝终止候选生成与确认，保留关联拒绝旧迟到普通保存。"""
    return _candidate_transaction(db, lambda: feedback_candidates.reject_candidate(db, candidate_id, request, current_user))


@router.post("/feedback-candidates/{candidate_id}/reset", response_model=CandidateResetRead)
def reset_feedback_candidate(candidate_id: str, request: CandidateResetRequest,
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> CandidateResetRead:
    """明确重载所选范围原内容并签发新普通身份，撤回不阻安全退出。"""
    return _candidate_transaction(db, lambda: feedback_candidates.reset_candidate(db, candidate_id, request, current_user))


@router.post("/feedback-candidates/{candidate_id}/publish", response_model=CandidatePublishReceipt)
def publish_feedback_candidate(candidate_id: str, request: CandidatePublishRequest,
    db: Session = Depends(get_session), current_user: User = Depends(get_current_user),
) -> CandidatePublishReceipt:
    """人工确认复用原发布职责和同事务回执，重复同参不生成第二发布。"""
    return feedback_candidates.publish_candidate(db, candidate_id, request, current_user)


@router.get("", response_model=list[SkillRead], dependencies=[Depends(require_agent_scope_viewer)])
def list_skills(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
) -> list[SkillRead]:
    ensure_tenant(db, tenant_id)
    rows = visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    stats = _skill_stats(db, tenant_id)
    recent_stats = _recent_skill_stats(db, tenant_id, stats)
    return [skill_read(row, stats, recent_stats) for row in rows]


@router.post("", response_model=SkillRead)
def create_skill(
    request: SkillCreateRequest,
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    """创建指定租户的 SOP，并在直接发布时执行统一定义编译。"""
    current_user = start_skill_write(db, request.tenant_id, current_user, agent_id, editor_context)

    if request.feedback_candidate_id is not None:
        raise HTTPException(409, "FEEDBACK_CANDIDATE_USE_ISOLATED_ENDPOINT")
    ensure_tenant(db, request.tenant_id)
    existing = db.exec(
        select(Skill).where(
            Skill.tenant_id == request.tenant_id, Skill.skill_id == request.content.skill_id
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Skill ID already exists for this tenant")
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    compiled_definition = None
    if request.status == "published":
        compiled_definition = _compile_skill_for_publication(normalized_content)
        _validate_participant_references_for_publication(db, request.tenant_id, compiled_definition)
    content = normalized_content.model_dump()
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    row = Skill(
        tenant_id=request.tenant_id,
        skill_id=normalized_content.skill_id,
        version=normalized_content.version,
        name=normalized_content.name,
        business_domain=normalized_content.business_domain,
        description=normalized_content.description,
        content_json=content,
        status=request.status,
    )
    db.add(row)
    db.flush()
    _sync_skill_tool_bindings(db, request.tenant_id, row.skill_id, row.content_json)
    branch = None
    binding_status = "active" if request.status == "published" else "inactive"
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "skill",
            row.id,
            binding_status,
            metadata_json=creator_metadata,
        )
        branch = ensure_agent_skill_branch(
            db,
            request.tenant_id,
            agent.id,
            row,
            metadata_json=creator_metadata,
        )
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        mark_resource_open_gallery(row, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "skill",
            row.id,
            binding_status,
            metadata_json=creator_metadata,
        )
    _upsert_skill_version(
        db, project_skill_with_branch(row, branch) if branch else row,
        compiled_definition=compiled_definition,
        scope_agent_id=branch.agent_id if branch else "",
    )
    if branch and request.status == "published":
        publish_branch_version(db, row, branch, compiled_definition=compiled_definition)
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, request.tenant_id)
    if branch:
        row = project_skill_with_branch(row, branch, binding_status)
    return skill_read(row, stats, _recent_skill_stats(db, request.tenant_id, stats))


@router.get(
    "/{skill_id}", response_model=SkillRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
) -> SkillRead:
    row = _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.put("/{skill_id}", response_model=SkillRead)
def update_skill(
    skill_id: str,
    request: SkillUpdateRequest,
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    """更新整体或 Agent 分支 SOP，并阻止无效定义保持发布状态。"""

    if request.feedback_candidate_id is not None:
        raise HTTPException(409, "FEEDBACK_CANDIDATE_USE_ISOLATED_ENDPOINT")
    if request.content.skill_id != skill_id:
        raise HTTPException(status_code=400, detail="SOP skill_id cannot be modified")
    current_user = start_skill_write(db, request.tenant_id, current_user, agent_id, editor_context)
    row = _get_skill(db, request.tenant_id, skill_id, for_update=True)
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == request.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == row.id,
                AgentResourceBinding.status != "deleted",
            )
        ).first()
        if not binding:
            raise HTTPException(status_code=404, detail="Skill not visible to this agent")
        current_branch = ensure_agent_skill_branch(db, request.tenant_id, agent.id, row)
        if current_branch.status == "active":
            compiled_definition = _compile_skill_for_publication(normalized_content)
            _validate_participant_references_for_publication(
                db, request.tenant_id, compiled_definition
            )
        branch = update_branch_skill(
            db,
            request.tenant_id,
            agent.id,
            row,
            normalized_content.model_dump(),
            "技能分支改写",
        )
        _sync_skill_tool_bindings(
            db,
            request.tenant_id,
            row.skill_id,
            normalized_content.model_dump(),
        )
        db.commit()
        projected = project_skill_with_branch(row, branch, binding.status)
        stats = _skill_stats(db, request.tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, request.tenant_id, stats))
    ensure_open_gallery_admin(request.tenant_id, current_user)
    next_status = request.status or row.status
    compiled_definition = None
    if next_status == "published":
        compiled_definition = _compile_skill_for_publication(normalized_content)
        _validate_participant_references_for_publication(db, request.tenant_id, compiled_definition)
    row.version = normalized_content.version
    row.name = normalized_content.name
    row.business_domain = normalized_content.business_domain
    row.description = normalized_content.description
    row.content_json = normalized_content.model_dump()
    _sync_skill_tool_bindings(db, request.tenant_id, row.skill_id, row.content_json)
    if request.status:
        row.status = request.status
    row.updated_at = utc_now()
    db.add(row)
    _upsert_skill_version(db, row, compiled_definition=compiled_definition)
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, request.tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, request.tenant_id, stats))


@router.post("/{skill_id}/publish", response_model=SkillRead)
def publish_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    """发布整体或 Agent 分支 SOP，并在变更状态前执行统一定义编译。"""
    current_user = start_skill_write(db, tenant_id, current_user, agent_id, editor_context)

    row = _get_skill(db, tenant_id, skill_id, for_update=True)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent is None or agent.is_overall:
        ensure_open_gallery_admin(tenant_id, current_user)
    published = publish_skill_head(db, row, agent)
    db.commit()
    stats = _skill_stats(db, tenant_id)
    return skill_read(published, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/{skill_id}/archive", response_model=SkillRead)
def archive_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    current_user = start_skill_write(db, tenant_id, current_user, agent_id, editor_context)
    row = _get_skill(db, tenant_id, skill_id, for_update=True)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, row)
        branch.status = "inactive"
        branch.updated_at = utc_now()
        db.add(branch)
        ensure_private_resource_binding(db, tenant_id, agent.id, "skill", row.id, "inactive")
        db.commit()
        projected = project_skill_with_branch(row, branch, "inactive")
        stats = _skill_stats(db, tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, tenant_id, stats))
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/{skill_id}/draft", response_model=SkillRead)
def draft_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    feedback_candidate_id: str | None = None,
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    """保留普通下架合同；反馈候选不得借该入口停用原活动版。"""
    current_user = start_skill_write(db, tenant_id, current_user, agent_id, editor_context)

    if feedback_candidate_id is not None:
        raise HTTPException(409, "FEEDBACK_CANDIDATE_USE_ISOLATED_ENDPOINT")
    row = _get_skill(db, tenant_id, skill_id, for_update=True)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        raise HTTPException(status_code=403, detail="Only overall SOPs can be moved to draft")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "draft"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> dict[str, str]:
    """删除或隐藏 SOP 编辑头，但永久保留已发布版本快照供历史实例审计。"""
    current_user = start_skill_write(db, tenant_id, current_user, agent_id, editor_context)

    row = _get_skill(db, tenant_id, skill_id, for_update=True)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == row.id,
            )
        ).first()
        if not binding:
            binding = AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=agent.id,
                resource_type="skill",
                resource_id=row.id,
                status="deleted",
            )
        else:
            binding.status = "deleted"
            binding.updated_at = utc_now()
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, row)
        branch.status = "deleted"
        branch.updated_at = utc_now()
        db.add(binding)
        db.add(branch)
        db.commit()
        return {"status": "hidden"}
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "skill", row):
            raise HTTPException(status_code=404, detail="Skill not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "skill", row.id)
        db.commit()
        return {"status": "hidden"}

    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    feedback_rows = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.skill_id == skill_id,
        )
    ).all()
    for feedback in feedback_rows:
        db.delete(feedback)
    version_rows = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id
        )
    ).all()
    for version_row in version_rows:
        if version_row.status != "published":
            db.delete(version_row)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get(
    "/{skill_id}/versions",
    response_model=list[SkillVersionRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_skill_versions(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
) -> list[SkillVersionRead]:
    row = _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        rows = branch_versions(db, tenant_id, agent.id, skill_id)
        return [_branch_version_read(row, db) for row in rows]
    current_snapshot = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id,
            SkillVersion.skill_id == skill_id,
            SkillVersion.version == row.version,
            SkillVersion.scope_agent_id == "",
        )
    ).first()
    if not current_snapshot:
        compiled_definition = (
            _compile_skill_for_publication(row.content_json) if row.status == "published" else None
        )
        _upsert_skill_version(db, row, compiled_definition=compiled_definition)
        db.commit()
    rows = db.exec(
        select(SkillVersion)
        .where(SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id,
               SkillVersion.scope_agent_id == "")
        .order_by(SkillVersion.created_at.desc())
    ).all()
    stats = _skill_stats(db, tenant_id)
    return [skill_version_read(version_row, stats) for version_row in rows]


@router.get(
    "/{skill_id}/versions/{version}",
    response_model=SkillVersionRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
) -> SkillVersionRead:
    _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        row = next(
            (
                item
                for item in branch_versions(db, tenant_id, agent.id, skill_id)
                if item.version == version
            ),
            None,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Skill version not found")
        return _branch_version_read(row, db)
    row = _get_skill_version(db, tenant_id, skill_id, version)
    return skill_version_read(row, _skill_stats(db, tenant_id))


@router.delete("/{skill_id}/versions/{version}")
def delete_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> dict[str, str]:
    current_user = start_skill_write(db, tenant_id, current_user, None, editor_context)
    skill = _get_skill(db, tenant_id, skill_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    row = _get_skill_version(db, tenant_id, skill_id, version)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="Published skill versions are immutable")
    if skill.version == version:
        raise HTTPException(status_code=409, detail="Cannot delete the active skill version")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{skill_id}/versions/{version}/rollback", response_model=SkillRead)
def rollback_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
    editor_context: Annotated[str | None, Header(alias="X-Skill-Editor-Context")] = None,
) -> SkillRead:
    """从历史内容派生新的发布版本，保留旧快照并记录来源关系。"""
    current_user = start_skill_write(db, tenant_id, current_user, agent_id, editor_context)

    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = rollback_branch(db, tenant_id, agent.id, skill_id, version)
        db.commit()
        skill = _get_skill(db, tenant_id, skill_id)
        projected = project_skill_with_branch(skill, branch)
        stats = _skill_stats(db, tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, tenant_id, stats))
    ensure_open_gallery_admin(tenant_id, current_user)
    row = _get_skill(db, tenant_id, skill_id, for_update=True)
    version_row = _get_skill_version(db, tenant_id, skill_id, version)
    normalized_content, _warnings = skill_card_with_unique_step_ids(
        SkillCard.model_validate(version_row.content_json)
    )
    derived_version = next_derived_skill_version(db, row)
    normalized_content = normalized_content.model_copy(
        update={
            "version": derived_version,
            "name": version_row.name,
            "business_domain": version_row.business_domain,
            "description": version_row.description or normalized_content.description,
        }
    )
    compiled_definition = _compile_skill_for_publication(normalized_content)
    _validate_participant_references_for_publication(db, tenant_id, compiled_definition)
    row.version = derived_version
    row.name = version_row.name
    row.business_domain = version_row.business_domain
    row.description = version_row.description
    row.content_json = normalized_content.model_dump()
    row.status = "published"
    row.updated_at = utc_now()
    db.add(row)
    _upsert_skill_version(
        db,
        row,
        compiled_definition=compiled_definition,
        derived_from_version_id=version_row.id,
    )
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "active")
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/files/extract", response_model=SkillFileExtractResponse)
def extract_skill_file(request: SkillFileExtractRequest) -> SkillFileExtractResponse:
    try:
        data = base64.b64decode(request.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid file content") from exc
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File is too large")
    text = _extract_uploaded_skill_file(request.filename, data)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in file")
    return SkillFileExtractResponse(filename=request.filename, text=text)


@router.post("/distill", response_model=SkillDistillResponse)
def distill_skill(
    request: SkillDistillRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillDistillResponse:
    request = _prepare_skill_generation_request(db, request, current_user)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    request = _with_available_tools(db, request)
    try:
        return SkillDistiller().distill(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/distill/stream")
def distill_skill_stream(
    request: SkillDistillRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    request = _prepare_skill_generation_request(db, request, current_user)
    job_id = _start_distill_stream_job(request, current_user)
    return StreamingResponse(_stream_skill_job(job_id), media_type="text/event-stream")


@router.post("/{skill_id}/rewrite/stream")
def rewrite_skill_stream(
    skill_id: str,
    request: SkillRewriteRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """原流式改写入口先冻结受管候选；普通入口也不能删除反馈关联旁路。"""
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    if request.feedback_candidate_id:
        prepared = prepare_candidate_rewrite(db, request, current_user, skill_id)
        job_id = _start_rewrite_stream_job(skill_id, prepared.request, current_user, prepared)
    else:
        request = _prepare_skill_generation_request(db, request, current_user, skill_id=skill_id)
        db.commit()
        job_id = _start_rewrite_stream_job(skill_id, request, current_user)
    return StreamingResponse(_stream_skill_job(job_id), media_type="text/event-stream")


@router.post("/distill/jobs")
def create_distill_job(
    request: SkillDistillRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    request = _prepare_skill_generation_request(db, request, current_user)
    return {"job_id": _start_distill_stream_job(request, current_user)}


@router.post("/{skill_id}/rewrite/jobs")
def create_rewrite_job(
    skill_id: str,
    request: SkillRewriteRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """原后台任务创建复用同一短准备事务，不在worker重新信任客户端来源。"""
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    if request.feedback_candidate_id:
        prepared = prepare_candidate_rewrite(db, request, current_user, skill_id)
        return {'job_id': _start_rewrite_stream_job(
            skill_id, prepared.request, current_user, prepared,
        )}
    request = _prepare_skill_generation_request(db, request, current_user, skill_id=skill_id)
    db.commit()
    return {"job_id": _start_rewrite_stream_job(skill_id, request, current_user)}


@router.get("/jobs/{job_id}")
def get_skill_stream_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """仅本人原任务元数据；反馈即使曾成功也重新验证当前来源和候选。"""
    job = _owned_stream_job(job_id, current_user)
    return {
        "job_id": job.id,
        "name": job.name,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "last_seq": job.events[-1].seq if job.events else 0,
    }


@router.get("/jobs/{job_id}/stream")
def stream_existing_skill_job(
    job_id: str,
    after_seq: int = Query(0),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """本人刷新续接先fresh验证，迭代中每个缓存事件再次复核。"""
    _owned_stream_job(job_id, current_user)
    return StreamingResponse(_stream_skill_job(job_id, after_seq), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
def cancel_skill_stream_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """本人请求停止原生成；受管任务取消后不能结算晚到响应。"""
    job = _owned_stream_job(job_id, current_user)
    if job.feedback_fence is not None:
        cancel_candidate_rewrite(get_session_engine(), job.feedback_fence)
    stream_jobs.cancel(job_id)
    if job.feedback_fence is not None:
        stream_jobs.invalidate_feedback(job_id, 'FEEDBACK_CANDIDATE_GENERATION_CANCELLED')
    stream_jobs.append(job_id, "status", {"text": "已请求停止生成"})
    return {"status": "cancel_requested", "job_id": job_id}


@router.post("/{skill_id}/rewrite", response_model=SkillRewriteResponse)
def rewrite_skill(
    skill_id: str,
    request: SkillRewriteRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRewriteResponse:
    """原同步改写；反馈分支先提交冻结事务，再由每次SDK守卫和候选结算承接。"""
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    if request.feedback_candidate_id:
        prepared = prepare_candidate_rewrite(db, request, current_user, skill_id)
        return rewrite_candidate(db.get_bind(), prepared)
    request = _prepare_skill_generation_request(db, request, current_user, skill_id=skill_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    request = _with_available_tools_for_rewrite(db, request)
    db.expunge(model_config)
    db.commit()
    try:
        return SkillEditor().rewrite(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _owned_stream_job(job_id: str, current_user: User) -> SkillStreamJob:
    """保留本人/tenant隔离；受管缓存读取不得以历史成功免除当前授权。"""
    job = stream_jobs.get(job_id)
    if not job or job.tenant_id != current_user.tenant_id or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    _check_feedback_job(job)
    return job


def _check_feedback_job(job: SkillStreamJob) -> None:
    """失效立即清除原缓存，只返回稳定错误，不转发旧模型内容或供应商异常。"""
    job = stream_jobs.get(job.id) or job
    if job.feedback_fence is None:
        return
    try:
        candidate_guard(get_session_engine(), job.feedback_fence,
                        lambda: stream_jobs.is_cancelled(job.id), allow_own_settlement=True)()
    except HTTPException as error:
        stream_jobs.invalidate_feedback(job.id, 'FEEDBACK_CANDIDATE_UNAVAILABLE')
        raise HTTPException(error.status_code, 'FEEDBACK_CANDIDATE_UNAVAILABLE') from error


def _start_distill_stream_job(request: SkillDistillRequest, current_user: User) -> str:
    job = stream_jobs.create("skill.distill", request.tenant_id, current_user.id)
    stream_jobs.append(job.id, "job_started", {"job_id": job.id, "name": job.name})
    enqueue_async_job(
        "skill.distill_stream",
        _run_distill_stream_job,
        job.id,
        request.model_dump(mode="json"),
        metadata={"tenant_id": request.tenant_id, "job_id": job.id},
    )
    return job.id


def _start_rewrite_stream_job(
    skill_id: str, request: SkillRewriteRequest, current_user: User,
    prepared: PreparedCandidateRewrite | None = None,
) -> str:
    """复用原队列；已准备候选只传脱离会话的输入及实际操作者fence。"""
    job = stream_jobs.create("skill.rewrite", request.tenant_id, current_user.id)
    if prepared is not None:
        stream_jobs.bind_feedback(job.id, prepared.fence)
    stream_jobs.append(
        job.id, "job_started", {"job_id": job.id, "name": job.name, "skill_id": skill_id}
    )
    arguments = (job.id, skill_id, request.model_dump(mode='json'))
    if prepared is not None:
        arguments = (*arguments, prepared)
    enqueue_async_job(
        "skill.rewrite_stream",
        _run_rewrite_stream_job,
        *arguments,
        metadata={"tenant_id": request.tenant_id, "job_id": job.id, "skill_id": skill_id},
    )
    return job.id


def _run_distill_stream_job(job_id: str, request_data: dict[str, object]) -> None:
    stream_jobs.start(job_id)
    try:
        request = SkillDistillRequest.model_validate(request_data)
        with Session(get_session_engine()) as db:
            request = _prepare_skill_generation_request(db, request, None)
            model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
            enriched_request = _with_available_tools(db, request)
            stream_jobs.append(job_id, "status", {"text": "正在调用模型生成新技能"})
            for item in SkillDistiller().stream_text(enriched_request, model_config):
                if stream_jobs.is_cancelled(job_id):
                    stream_jobs.append(job_id, "status", {"text": "已停止生成"})
                    stream_jobs.complete(job_id)
                    return
                stream_jobs.append(job_id, str(item["event"]), dict(item["data"]))
        stream_jobs.complete(job_id)
    except Exception as exc:  # noqa: BLE001 - expose stable job failure to UI.
        stream_jobs.fail(job_id, str(exc))


def _run_rewrite_stream_job(
    job_id: str, skill_id: str, request_data: dict[str, object],
    prepared: PreparedCandidateRewrite | None = None,
) -> None:
    """原worker贯穿实际actor；受管每SDK/事件/结算与缓存均重新校验且无外呼事务。"""
    stream_jobs.start(job_id)
    if prepared is not None:
        _run_feedback_rewrite_stream_job(job_id, prepared)
        return
    try:
        request = SkillRewriteRequest.model_validate(request_data)
        if request.current_skill.skill_id != skill_id:
            raise ValueError("Path skill_id must match current_skill.skill_id")
        with Session(get_session_engine()) as db:
            job = stream_jobs.get(job_id)
            actor = db.get(User, job.user_id) if job else None
            if actor is None:
                raise HTTPException(403, 'SKILL_ACTOR_UNAVAILABLE')
            request = _prepare_skill_generation_request(db, request, actor, skill_id=skill_id)
            model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
            enriched_request = _with_available_tools_for_rewrite(db, request)
            db.expunge(model_config)
            db.commit()
        stream_jobs.append(job_id, "status", {"text": "正在调用模型分析改写要求"})
        for item in SkillEditor().stream_text(enriched_request, model_config):
            if stream_jobs.is_cancelled(job_id):
                stream_jobs.append(job_id, "status", {"text": "已停止改写"})
                stream_jobs.complete(job_id)
                return
            stream_jobs.append(job_id, str(item["event"]), dict(item["data"]))
        stream_jobs.complete(job_id)
    except Exception as exc:  # noqa: BLE001 - expose stable job failure to UI.
        stream_jobs.fail(job_id, str(exc))


def _run_feedback_rewrite_stream_job(job_id: str, prepared: PreparedCandidateRewrite) -> None:
    """在现有任务内运行原编辑器；成功只结算候选，失效/取消/失败清除所有正文。"""
    engine = get_session_engine()
    fence = prepared.fence
    guard = candidate_guard(engine, fence, lambda: stream_jobs.is_cancelled(job_id))
    try:
        guard()
        for item in SkillEditor().stream_text(prepared.request, prepared.model, request_guard=guard):
            guard()
            if item['event'] == 'complete':
                response = SkillRewriteResponse.model_validate(item['data'])
                response, fence = settle_candidate_rewrite(engine, fence, response)
                stream_jobs.bind_feedback(job_id, fence)
                guard = candidate_guard(engine, fence, lambda: stream_jobs.is_cancelled(job_id))
                guard()
                item = {'event': 'complete', 'data': response.model_dump(mode='json')}
            stream_jobs.append(job_id, str(item['event']), dict(item['data']))
        guard()
        stream_jobs.complete(job_id)
    except Exception:  # noqa: BLE001 - no raw provider/candidate exception enters cache.
        stream_jobs.invalidate_feedback(job_id, MODEL_FAILURE)


def _stream_skill_job(job_id: str, after_seq: int = 0) -> Iterator[str]:
    """原SSE帧与序号保持；当前校验先于附着、每个旧缓存事件和完成帧。"""
    last_seq = max(0, after_seq)
    attached = stream_jobs.get(job_id)
    if attached:
        try:
            _check_feedback_job(attached)
        except HTTPException:
            yield _sse('error', {'message': 'FEEDBACK_CANDIDATE_UNAVAILABLE'})
            return
    yield _sse("job_attached", {"job_id": job_id, "after_seq": after_seq})
    while True:
        job, events = stream_jobs.snapshot(job_id, last_seq)
        if not job:
            yield _sse("error", {"message": "Job not found"})
            return
        for event in events:
            try:
                _check_feedback_job(job)
            except HTTPException:
                yield _sse('error', {'message': 'FEEDBACK_CANDIDATE_UNAVAILABLE'})
                return
            last_seq = event.seq
            yield _sse_event(event, job_id)
        if job.status in {"succeeded", "failed"} and not events:
            try:
                _check_feedback_job(job)
            except HTTPException:
                yield _sse('error', {'message': 'FEEDBACK_CANDIDATE_UNAVAILABLE'})
                return
            yield _sse("job_complete", {"job_id": job_id, "status": job.status, "error": job.error})
            return
        sleep(0.15)


def _sse_event(event: SkillStreamEvent, job_id: str) -> str:
    data = {"job_id": job_id, "seq": event.seq, **event.data}
    return _sse(event.event, data)


def get_session_engine():
    from app.db import engine

    return engine


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig:
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if not model_config:
        raise HTTPException(status_code=400, detail="No enabled default model config")
    return model_config


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig:
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return model_config


def _with_available_tools(db: Session, request: SkillDistillRequest) -> SkillDistillRequest:
    """只把服务端确认可见的工具注入 Skill 生成上下文，客户端目录不能扩权。"""

    available_tools = [_tool_generation_payload(tool) for tool in visible_tool_rows(
        db, request.tenant_id, request.agent_id, include_inactive=False
    )]
    return request.model_copy(update={"available_tools": available_tools})


def _with_available_tools_for_rewrite(
    db: Session, request: SkillRewriteRequest
) -> SkillRewriteRequest:
    """只把服务端确认可见的工具注入改写上下文，避免租户级工具泄露。"""

    available_tools = [_tool_generation_payload(tool) for tool in visible_tool_rows(
        db, request.tenant_id, request.agent_id, include_inactive=False
    )]
    return request.model_copy(update={"available_tools": available_tools})


def _prepare_skill_generation_request(
    db: Session,
    request: SkillDistillRequest | SkillRewriteRequest,
    current_user: User | None,
    *,
    skill_id: str | None = None,
) -> SkillDistillRequest | SkillRewriteRequest:
    """校验生成范围并用数据库中的可见 Skill 替换客户端伪造的当前内容。"""

    if current_user is not None:
        ensure_current_user_tenant(request.tenant_id, current_user)
        current_user = start_skill_write(db, request.tenant_id, current_user,
                                         request.agent_id, request.editor_context)
    ensure_tenant(db, request.tenant_id)
    agent = get_agent(db, request.tenant_id, request.agent_id)
    if request.agent_id and not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent is not None and agent.status != "active":
        raise HTTPException(status_code=409, detail="Agent is not active")
    if current_user is not None:
        managed_agent = ensure_agent_scope_manager(
            db, request.tenant_id, request.agent_id, current_user
        )
        if managed_agent is None or managed_agent.is_overall:
            ensure_open_gallery_admin(request.tenant_id, current_user)
    if skill_id is None:
        return request
    if not isinstance(request, SkillRewriteRequest):
        raise HTTPException(status_code=400, detail="Rewrite request is required")
    visible_skill = _get_visible_skill_for_scope(
        db, request.tenant_id, skill_id, request.agent_id
    )
    server_skill = SkillCard.model_validate(visible_skill.content_json)
    return request.model_copy(update={"current_skill": server_skill, 'feedback_materials': []})


def _sse(event: object, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _extract_uploaded_skill_file(filename: str, data: bytes) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"md", "txt"}:
        return _decode_text_bytes(data)
    if suffix == "docx":
        return _extract_docx_text(data)
    if suffix == "doc":
        return _decode_legacy_doc_text(data)
    raise HTTPException(status_code=400, detail="Unsupported file type")


def _decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _decode_legacy_doc_text(data: bytes) -> str:
    text = _decode_text_bytes(data)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _extract_docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail="Invalid docx file") from exc

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail="Invalid docx xml") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts: list[str] = []
        for element in paragraph.iter():
            if element.tag == f"{namespace}t" and element.text:
                parts.append(element.text)
            elif element.tag == f"{namespace}tab":
                parts.append("\t")
            elif element.tag in {f"{namespace}br", f"{namespace}cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _skill_stats(db: Session, tenant_id: str) -> dict[str, dict[str, float | int]]:
    stats: dict[str, dict[str, float | int]] = {}
    events = db.exec(
        select(AgentEvent).where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.event_type.in_(["skill_started", "skill_resumed"]),  # type: ignore[attr-defined]
        )
    ).all()
    for event in events:
        payload = event.payload_json or {}
        skill_id = str(payload.get("to_skill_id") or "")
        if not skill_id:
            continue
        skill_version = (
            str(payload.get("to_skill_version") or payload.get("skill_version") or "") or None
        )
        _increment_call(stats, skill_id, skill_version)

    feedback_rows = db.exec(select(SkillFeedback).where(SkillFeedback.tenant_id == tenant_id)).all()
    flow_feedback: dict[tuple[str, str | None, str, str], set[str]] = {}
    for feedback in feedback_rows:
        skill_version = feedback.skill_version
        flow_key = (feedback.skill_id, skill_version, feedback.session_id, feedback.user_id)
        flow_feedback.setdefault(flow_key, set()).add(feedback.rating)

    for (skill_id, skill_version, _session_id, _user_id), ratings in flow_feedback.items():
        entries = [stats.setdefault(skill_id, _empty_stats())]
        if skill_version:
            entries.append(stats.setdefault(_stats_key(skill_id, skill_version), _empty_stats()))
        for entry in entries:
            if "down" in ratings:
                entry["negative_feedback_count"] = int(entry["negative_feedback_count"]) + 1
            elif "up" in ratings:
                entry["positive_feedback_count"] = int(entry["positive_feedback_count"]) + 1

    for entry in stats.values():
        positive = int(entry["positive_feedback_count"])
        negative = int(entry["negative_feedback_count"])
        calls = int(entry["call_count"])
        entry["positive_rate"] = round(positive / calls, 4) if calls else 0.0
        entry["negative_rate"] = round(negative / calls, 4) if calls else 0.0
    return stats


def _increment_call(
    stats: dict[str, dict[str, float | int]], skill_id: str, version: str | None
) -> None:
    entries = [stats.setdefault(skill_id, _empty_stats())]
    if version:
        entries.append(stats.setdefault(_stats_key(skill_id, version), _empty_stats()))
    for entry in entries:
        entry["call_count"] = int(entry["call_count"]) + 1


def _recent_skill_stats(
    db: Session,
    tenant_id: str,
    stats: dict[str, dict[str, float | int]],
) -> dict[str, dict[str, object]]:
    recent_versions: dict[str, list[str]] = {}
    version_rows = db.exec(
        select(SkillVersion)
        .where(SkillVersion.tenant_id == tenant_id, SkillVersion.scope_agent_id == "")
        .order_by(
            SkillVersion.skill_id.asc(), SkillVersion.created_at.desc(), SkillVersion.version.desc()
        )
    ).all()
    for row in version_rows:
        versions = recent_versions.setdefault(row.skill_id, [])
        if len(versions) < 3:
            versions.append(row.version)

    skill_rows = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    for row in skill_rows:
        recent_versions.setdefault(row.skill_id, [row.version])

    recent_stats: dict[str, dict[str, object]] = {}
    for skill_id, versions in recent_versions.items():
        entry: dict[str, object] = {
            **_empty_stats(),
            "recent_versions": versions,
        }
        for version in versions:
            version_stats = stats.get(_stats_key(skill_id, version), {})
            entry["call_count"] = int(entry["call_count"]) + int(version_stats.get("call_count", 0))
            entry["positive_feedback_count"] = int(entry["positive_feedback_count"]) + int(
                version_stats.get("positive_feedback_count", 0)
            )
            entry["negative_feedback_count"] = int(entry["negative_feedback_count"]) + int(
                version_stats.get("negative_feedback_count", 0)
            )
        positive = int(entry["positive_feedback_count"])
        negative = int(entry["negative_feedback_count"])
        calls = int(entry["call_count"])
        entry["positive_rate"] = round(positive / calls, 4) if calls else 0.0
        entry["negative_rate"] = round(negative / calls, 4) if calls else 0.0
        recent_stats[skill_id] = entry
    return recent_stats


def _upsert_skill_version(
    db: Session,
    row: Skill,
    *,
    compiled_definition: CompiledSopDefinition | None = None,
    derived_from_version_id: str | None = None,
    scope_agent_id: str = "",
) -> SkillVersion:
    """在调用方事务内保存版本快照，并把不可变冲突转换为 HTTP 409。"""

    try:
        result = write_skill_version(
            db,
            row,
            compiled_definition=compiled_definition,
            derived_from_version_id=derived_from_version_id,
            scope_agent_id=scope_agent_id,
        )
    except PublishedVersionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PUBLISHED_SKILL_VERSION_IMMUTABLE",
                "skill_id": exc.skill_id,
                "version": exc.version,
            },
        ) from exc
    object.__setattr__(row, "publication_identity", {
        "id": result.version.id if result.version.status == "published" else None,
        "scope_agent_id": scope_agent_id,
        "checksum": result.version.compiled_definition_checksum,
    })
    return result.version


def _empty_stats() -> dict[str, float | int]:
    return {
        "call_count": 0,
        "positive_feedback_count": 0,
        "negative_feedback_count": 0,
        "positive_rate": 0.0,
        "negative_rate": 0.0,
    }


def _get_skill(
    db: Session,
    tenant_id: str,
    skill_id: str,
    *,
    for_update: bool = False,
) -> Skill:
    """按租户读取 SOP 编辑头，并可在发布事务中获取数据库行锁。"""

    ensure_tenant(db, tenant_id)
    statement = select(Skill).where(
        Skill.tenant_id == tenant_id,
        Skill.skill_id == skill_id,
    )
    if for_update:
        statement = statement.with_for_update()
    row = db.exec(statement).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _get_visible_skill_for_scope(
    db: Session,
    tenant_id: str,
    skill_id: str,
    agent_id: str | None,
) -> Skill:
    row = next(
        (
            item
            for item in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
            if item.skill_id == skill_id
        ),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _get_skill_version(db: Session, tenant_id: str, skill_id: str, version: str) -> SkillVersion:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id,
            SkillVersion.skill_id == skill_id,
            SkillVersion.version == version,
            SkillVersion.scope_agent_id == "",
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return row
