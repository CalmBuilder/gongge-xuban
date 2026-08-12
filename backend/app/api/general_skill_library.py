"""
@Time       : 2026/08/12 11:25
@Author     : zhanglp8181
@File       : general_skill_library.py
@CallChain  : 我的 Skill 库页面 → FastAPI → GeneralSkillLibraryService
@Description: 暴露本人私有 Skill 库和多 Agent 原子装配 preview/commit API。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.db.models import User
from app.general_skills.library import GeneralSkillLibraryError, GeneralSkillLibraryService
from app.general_skills.library_schema import (
    GeneralSkillBindingBatchCommitRead,
    GeneralSkillBindingBatchCommitRequest,
    GeneralSkillBindingBatchPreviewRead,
    GeneralSkillBindingBatchPreviewRequest,
    MyGeneralSkillRead,
    MyGeneralSkillAgentRead,
)
from app.security.auth import get_current_user


router = APIRouter(tags=["enterprise:general-skill-library"])


@router.get("/api/enterprise/my-general-skills", response_model=list[MyGeneralSkillRead])
def list_my_general_skills(
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[MyGeneralSkillRead]:
    """列出当前用户拥有的私有 Skill 及本人各 Agent 绑定。"""

    return GeneralSkillLibraryService(db).list_owned(current_user)


@router.get(
    "/api/enterprise/my-general-skills/agents",
    response_model=list[MyGeneralSkillAgentRead],
)
def list_my_general_skill_agents(
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[MyGeneralSkillAgentRead]:
    """列出与批量事务采用同一所有权口径的可装配 Agent。"""

    return GeneralSkillLibraryService(db).list_owned_agents(current_user)


@router.post(
    "/api/enterprise/general-skill-bindings:batch-preview",
    response_model=GeneralSkillBindingBatchPreviewRead,
)
def preview_general_skill_bindings(
    request: GeneralSkillBindingBatchPreviewRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillBindingBatchPreviewRead:
    """无写入预检本人 Skill 到多个本人 Agent 的装配动作。"""

    try:
        return GeneralSkillLibraryService(db).preview(request, current_user)
    except GeneralSkillLibraryError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/api/enterprise/general-skill-bindings:batch",
    response_model=GeneralSkillBindingBatchCommitRead,
)
def commit_general_skill_bindings(
    request: GeneralSkillBindingBatchCommitRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillBindingBatchCommitRead:
    """以 preview checksum 和幂等键原子提交全部 Agent 绑定。"""

    try:
        return GeneralSkillLibraryService(db).commit(
            request,
            idempotency_key=idempotency_key,
            current_user=current_user,
        )
    except GeneralSkillLibraryError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: GeneralSkillLibraryError) -> HTTPException:
    """把稳定领域错误投影为不泄漏资源存在性的 HTTP 错误。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )
