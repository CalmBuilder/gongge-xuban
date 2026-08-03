"""管理端专家分类词表与原子分类更新接口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentProfile, User, utc_now
from app.experts.taxonomy_schema import AGENCY_AGENTS_TAXONOMY
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(
    prefix="/api/enterprise/expert-taxonomy",
    tags=["enterprise:expert-taxonomy"],
)


class ExpertTaxonomyCategoryRead(BaseModel):
    """一个一级分类及其允许的二级分类。"""

    name: str
    subcategories: list[str]


class ExpertTaxonomyRead(BaseModel):
    """管理端可使用的版本化分类词表。"""

    version: int = 1
    categories: list[ExpertTaxonomyCategoryRead]


class ExpertTaxonomyAssignmentRequest(BaseModel):
    """一次单个或批量专家分类调整。"""

    tenant_id: str
    agent_ids: Annotated[list[str], Field(min_length=1)]
    category: str
    subcategory: str

    @field_validator("agent_ids")
    @classmethod
    def normalize_agent_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not normalized:
            raise ValueError("At least one agent id is required")
        if len(normalized) > 500:
            raise ValueError("At most 500 agents can be updated at once")
        return normalized


class ExpertTaxonomyAssignmentResult(BaseModel):
    """专家分类调整结果。"""

    updated_count: int
    agent_ids: list[str]


def _validate_pair(category: str, subcategory: str) -> None:
    if subcategory not in AGENCY_AGENTS_TAXONOMY.get(category, frozenset()):
        raise HTTPException(status_code=400, detail="Invalid expert category pair")


@router.get("", response_model=ExpertTaxonomyRead)
def get_expert_taxonomy(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExpertTaxonomyRead:
    """返回前后端共用的受控专家分类词表。"""

    ensure_tenant(db, tenant_id)
    ensure_current_user_tenant(tenant_id, current_user)
    return ExpertTaxonomyRead(
        categories=[
            ExpertTaxonomyCategoryRead(
                name=name,
                subcategories=sorted(subcategories),
            )
            for name, subcategories in sorted(AGENCY_AGENTS_TAXONOMY.items())
        ]
    )


@router.patch("/assignments", response_model=ExpertTaxonomyAssignmentResult)
def assign_expert_taxonomy(
    request: ExpertTaxonomyAssignmentRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExpertTaxonomyAssignmentResult:
    """校验全部目标后，在一个事务内更新专家分类。"""

    ensure_tenant(db, request.tenant_id)
    ensure_current_user_tenant(request.tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Administrator access required")
    _validate_pair(request.category, request.subcategory)

    rows = list(
        db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == request.tenant_id,
                AgentProfile.id.in_(request.agent_ids),
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    missing = [agent_id for agent_id in request.agent_ids if agent_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"Expert not found: {missing[0]}")
    for agent_id in request.agent_ids:
        row = by_id[agent_id]
        if row.is_overall or (row.metadata_json or {}).get("employee_type") != "expert":
            raise HTTPException(status_code=400, detail=f"Agent is not an expert: {agent_id}")

    updated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for agent_id in request.agent_ids:
        row = by_id[agent_id]
        row.metadata_json = {
            **(row.metadata_json or {}),
            "expert_category": request.category,
            "expert_subcategory": request.subcategory,
            "role_name": request.category,
            "expert_taxonomy_manually_edited": True,
            "expert_taxonomy_updated_at": updated_at,
            "expert_taxonomy_updated_by": current_user.username,
        }
        row.updated_at = utc_now()
        db.add(row)
    db.commit()
    return ExpertTaxonomyAssignmentResult(
        updated_count=len(request.agent_ids),
        agent_ids=request.agent_ids,
    )
