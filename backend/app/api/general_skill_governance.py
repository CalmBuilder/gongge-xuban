"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : general_skill_governance.py
@CallChain  : Skill 管理页面 → FastAPI → GeneralSkillGovernanceService → DB/Audit ledger
@Description: 暴露用户级 Skill 绑定策略、修订列表、回滚与软撤销 API。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentResourceBinding, GeneralSkill, GeneralSkillRevision, User
from app.general_skills.eligibility import EffectiveGeneralSkillResolver, GeneralSkillBindingMetadata
from app.general_skills.governance import (
    GeneralSkillGovernanceError,
    GeneralSkillGovernanceService,
)
from app.general_skills.governance_schema import (
    GeneralSkillBindingRead,
    GeneralSkillBindingCreate,
    GeneralSkillBindingUpdate,
    EffectiveGeneralSkillCatalogRead,
    GeneralSkillRevisionRead,
    GeneralSkillRevokeRequest,
    GeneralSkillRollbackRequest,
)
from app.security.auth import get_current_user


router = APIRouter(
    prefix="/api/enterprise/general-skill-governance",
    tags=["enterprise:general-skill-governance"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/agents/{agent_id}/catalog",
    response_model=EffectiveGeneralSkillCatalogRead,
)
def effective_catalog(
    agent_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> EffectiveGeneralSkillCatalogRead:
    """返回当前用户与指定 Agent 的权威 Skill 资格交集，不返回正文。"""

    catalog = EffectiveGeneralSkillResolver(db).resolve(current_user, agent_id)
    return EffectiveGeneralSkillCatalogRead(
        tenant_id=catalog.tenant_id,
        user_id=catalog.user_id,
        agent_id=catalog.agent_id,
        authorization_revision=catalog.authorization_revision,
        eligibility_hash=catalog.eligibility_hash,
        items=[asdict(item) for item in catalog.items],
    )


def _revision_read(row: GeneralSkillRevision) -> GeneralSkillRevisionRead:
    """把修订转换为不泄漏正文或对象路径的管理摘要。"""

    return GeneralSkillRevisionRead(
        id=row.id,
        skill_id=row.skill_id,
        revision_number=row.revision_number,
        content_checksum=row.content_checksum,
        manifest_checksum=row.manifest_checksum,
        status=row.status,
        row_version=row.row_version,
        created_at=row.created_at.isoformat(),
        published_at=row.published_at.isoformat() if row.published_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
    )


def _binding_read(row: AgentResourceBinding) -> GeneralSkillBindingRead:
    """严格解析绑定 metadata 后生成治理响应。"""

    metadata = GeneralSkillBindingMetadata.model_validate(row.metadata_json)
    return GeneralSkillBindingRead(
        id=row.id,
        agent_id=row.agent_id,
        skill_id=row.resource_id,
        status=row.status,
        revision_policy=metadata.revision_policy,
        pinned_revision_id=metadata.pinned_revision_id,
        invocation_policy=metadata.invocation_policy,
        row_version=row.row_version,
    )


@router.get("/skills/{skill_id}/revisions", response_model=list[GeneralSkillRevisionRead])
def list_revisions(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[GeneralSkillRevisionRead]:
    """仅向 Skill 所有者或同租户管理员列出不可变修订摘要。"""

    skill = db.get(GeneralSkill, skill_id)
    if skill is None or skill.tenant_id != tenant_id or tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="GENERAL_SKILL_NOT_AVAILABLE")
    if (
        skill.owner_user_id != current_user.id
        and current_user.role != "admin"
        and skill.visibility_scope != "tenant_gallery"
    ):
        raise HTTPException(status_code=403, detail="GENERAL_SKILL_FORBIDDEN")
    rows = db.exec(
        select(GeneralSkillRevision)
        .where(
            GeneralSkillRevision.tenant_id == tenant_id,
            GeneralSkillRevision.skill_id == skill_id,
        )
        .order_by(GeneralSkillRevision.revision_number.desc())
    ).all()
    return [_revision_read(row) for row in rows]


@router.patch("/bindings/{binding_id}", response_model=GeneralSkillBindingRead)
def update_binding(
    binding_id: str,
    request: GeneralSkillBindingUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillBindingRead:
    """原子更新绑定版本策略与启停状态，不允许部分成功。"""

    service = GeneralSkillGovernanceService(db)
    try:
        binding = service.update_binding_configuration(
            current_user=current_user,
            agent_id=request.agent_id,
            binding_id=binding_id,
            status=request.status,
            revision_policy=request.revision_policy,
            pinned_revision_id=request.pinned_revision_id,
            invocation_policy=request.invocation_policy,
            expected_row_version=request.expected_row_version,
        )
        return _binding_read(binding)
    except GeneralSkillGovernanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.error_code, "message": str(exc)},
        ) from exc


@router.post("/bindings", response_model=GeneralSkillBindingRead, status_code=201)
def create_binding(
    request: GeneralSkillBindingCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillBindingRead:
    """由所有者把本人 Skill 复用到本人数字员工；组织 Skill 必须走发布采用入口。"""

    try:
        row = GeneralSkillGovernanceService(db).create_binding(
            current_user=current_user,
            agent_id=request.agent_id,
            skill_id=request.skill_id,
            revision_policy=request.revision_policy,
            pinned_revision_id=request.pinned_revision_id,
            invocation_policy=request.invocation_policy,
        )
        return _binding_read(row)
    except GeneralSkillGovernanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.error_code, "message": str(exc)},
        ) from exc


@router.post("/skills/{skill_id}/rollback", response_model=GeneralSkillRevisionRead)
def rollback_skill(
    skill_id: str,
    request: GeneralSkillRollbackRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRevisionRead:
    """回滚到已审核旧修订并即时使 follow_latest 绑定重新解析。"""

    try:
        row = GeneralSkillGovernanceService(db).rollback_skill(
            current_user=current_user,
            skill_id=skill_id,
            target_revision_id=request.target_revision_id,
            expected_skill_row_version=request.expected_skill_row_version,
            expected_target_row_version=request.expected_target_row_version,
        )
        return _revision_read(row)
    except GeneralSkillGovernanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.error_code, "message": str(exc)},
        ) from exc


@router.post(
    "/skills/{skill_id}/revisions/{revision_id}/revoke",
    response_model=GeneralSkillRevisionRead,
)
def revoke_revision(
    skill_id: str,
    revision_id: str,
    request: GeneralSkillRevokeRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRevisionRead:
    """软撤销指定修订并立即从统一资格目录中排除。"""

    try:
        row = GeneralSkillGovernanceService(db).revoke_revision(
            current_user=current_user,
            skill_id=skill_id,
            revision_id=revision_id,
            expected_skill_row_version=request.expected_skill_row_version,
            expected_revision_row_version=request.expected_revision_row_version,
        )
        return _revision_read(row)
    except GeneralSkillGovernanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.error_code, "message": str(exc)},
        ) from exc
