from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import PersonaConfig, User, utc_now
from app.db.seed import DEFAULT_PERSONA_PROMPT
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/persona",
    tags=["enterprise:persona"],
    dependencies=[Depends(get_current_user)],
)


class PersonaRead(BaseModel):
    tenant_id: str
    system_prompt: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class PersonaUpdateRequest(BaseModel):
    tenant_id: str
    system_prompt: str


def persona_read(row: PersonaConfig) -> PersonaRead:
    return PersonaRead(
        tenant_id=row.tenant_id,
        system_prompt=row.system_prompt,
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=PersonaRead, dependencies=[Depends(require_current_tenant)])
def get_persona(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> PersonaRead:
    ensure_tenant(db, tenant_id)
    row = db.get(PersonaConfig, tenant_id)
    if not row:
        row = PersonaConfig(tenant_id=tenant_id, system_prompt=DEFAULT_PERSONA_PROMPT)
        db.add(row)
        db.commit()
        db.refresh(row)
    return persona_read(row)


@router.put("", response_model=PersonaRead)
def update_persona(
    request: PersonaUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PersonaRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    row = db.get(PersonaConfig, request.tenant_id)
    if not row:
        row = PersonaConfig(tenant_id=request.tenant_id, system_prompt=request.system_prompt)
    else:
        row.system_prompt = request.system_prompt
        row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return persona_read(row)
