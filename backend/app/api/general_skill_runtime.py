"""
@Time       : 2026/08/13 02:10
@Author     : zhanglp8181
@File       : general_skill_runtime.py
@CallChain  : Chat Skill UI/internal tools → FastAPI → GeneralSkillRuntimeService
@Description: 暴露会话 Skill 目录、mute、受控加载和固定资源分页读取 API。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.config import Settings, get_settings
from app.db.models import User
from app.api.general_skill_imports import get_general_skill_remote_fetcher
from app.general_skills.install_intents import (
    GeneralSkillInstallIntentError,
    GeneralSkillInstallIntentService,
)
from app.general_skills.install_intent_schema import (
    GeneralSkillInstallIntentCreate,
    GeneralSkillInstallIntentRead,
    GeneralSkillInstallIntentResolve,
)
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.remote_source import RemoteFetcher
from app.general_skills.runtime import GeneralSkillRuntimeError, GeneralSkillRuntimeService
from app.general_skills.runtime_schema import (
    GeneralSkillLoadRead,
    GeneralSkillLoadRequest,
    GeneralSkillResourceRead,
    SessionGeneralSkillCatalogRead,
    SessionGeneralSkillItemRead,
    SessionGeneralSkillOverrideRead,
    SessionGeneralSkillOverrideWrite,
)
from app.security.auth import get_current_user


router = APIRouter(
    prefix="/api/chat/sessions",
    tags=["chat:general-skills"],
    dependencies=[Depends(get_current_user)],
)


def _install_service(db: Session, settings: Settings) -> GeneralSkillInstallIntentService:
    """创建复用正式对象存储的对话安装服务。"""

    if not settings.general_skill_import_v2_enabled:
        raise HTTPException(status_code=404, detail={"error_code": "FEATURE_NOT_AVAILABLE"})
    return GeneralSkillInstallIntentService(
        db,
        FileSystemSkillObjectStore(settings.general_skill_object_store_path),
    )


@router.get(
    "/{session_id}/general-skill-install-intents",
    response_model=list[GeneralSkillInstallIntentRead],
)
def list_general_skill_install_intents(
    session_id: str,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> list[GeneralSkillInstallIntentRead]:
    """恢复当前用户会话中的全部持久安装卡。"""

    try:
        return _install_service(db, settings).list_session(session_id, current_user=current_user)
    except GeneralSkillInstallIntentError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/{session_id}/general-skill-install-intents",
    response_model=GeneralSkillInstallIntentRead,
    status_code=202,
)
def create_general_skill_install_intent(
    session_id: str,
    payload: GeneralSkillInstallIntentCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    fetcher: RemoteFetcher = Depends(get_general_skill_remote_fetcher),
) -> GeneralSkillInstallIntentRead:
    """由明确 UI 动作创建固定 GitHub 来源的待本人确认卡。"""

    try:
        return _install_service(db, settings).create(
            session_id,
            payload,
            idempotency_key=idempotency_key,
            current_user=current_user,
            fetcher=fetcher,
        )
    except GeneralSkillInstallIntentError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc


@router.post(
    "/{session_id}/general-skill-install-intents/{intent_id}/resolve",
    response_model=GeneralSkillInstallIntentRead,
)
def resolve_general_skill_install_intent(
    session_id: str,
    intent_id: str,
    payload: GeneralSkillInstallIntentResolve,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> GeneralSkillInstallIntentRead:
    """以本人点击、命令 ID 和乐观锁确认或取消安装卡。"""

    try:
        return _install_service(db, settings).resolve(
            session_id,
            intent_id,
            command=payload.command,
            expected_row_version=payload.expected_row_version,
            current_user=current_user,
        )
    except GeneralSkillInstallIntentError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc


def _http_error(exc: GeneralSkillRuntimeError) -> HTTPException:
    """把领域错误映射为稳定 HTTP detail，不返回内部对象路径。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"error_code": exc.code, "message": str(exc)},
    )


@router.get("/{session_id}/general-skills", response_model=SessionGeneralSkillCatalogRead)
def session_general_skills(
    session_id: str,
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SessionGeneralSkillCatalogRead:
    """返回当前认证用户在固定会话/Agent 内实际可用的 Skill 菜单。"""

    try:
        items = GeneralSkillRuntimeService(db).session_menu(
            current_user, session_id=session_id, agent_id=agent_id
        )
    except GeneralSkillRuntimeError as exc:
        raise _http_error(exc) from exc
    return SessionGeneralSkillCatalogRead(
        session_id=session_id,
        agent_id=agent_id,
        items=[
            SessionGeneralSkillItemRead(
                skill_id=item.skill_id,
                revision_id=item.revision_id,
                revision_number=item.revision_number,
                name=item.name,
                description=item.description,
                invocation_policy=item.invocation_policy,
                revision_policy=item.revision_policy,
                enabled=enabled,
                override_row_version=row_version,
            )
            for item, enabled, row_version in items
        ],
    )


@router.put(
    "/{session_id}/general-skills/{skill_id}",
    response_model=SessionGeneralSkillOverrideRead,
)
def set_session_general_skill(
    session_id: str,
    skill_id: str,
    payload: SessionGeneralSkillOverrideWrite,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SessionGeneralSkillOverrideRead:
    """设置当前会话 mute/恢复继承，不能绕过上层绑定与撤权。"""

    try:
        row = GeneralSkillRuntimeService(db).set_session_enabled(
            current_user,
            session_id=session_id,
            agent_id=payload.agent_id,
            skill_id=skill_id,
            enabled=payload.enabled,
            expected_row_version=payload.expected_row_version,
        )
    except GeneralSkillRuntimeError as exc:
        raise _http_error(exc) from exc
    return SessionGeneralSkillOverrideRead(
        skill_id=row.skill_id,
        enabled=row.enabled,
        row_version=row.row_version,
    )


@router.post("/{session_id}/general-skill-loads", response_model=GeneralSkillLoadRead)
def load_session_general_skill(
    session_id: str,
    payload: GeneralSkillLoadRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillLoadRead:
    """在模型调用前加载固定 revision 并形成幂等 Use 账本。"""

    try:
        loaded = GeneralSkillRuntimeService(db).load(
            current_user,
            session_id=session_id,
            agent_id=payload.agent_id,
            turn_id=payload.turn_id,
            skill_id=payload.skill_id,
            selection_mode=payload.selection_mode,
            parent_skill_use_id=payload.parent_skill_use_id,
        )
    except GeneralSkillRuntimeError as exc:
        raise _http_error(exc) from exc
    return GeneralSkillLoadRead(
        use_id=loaded.use_id,
        skill_id=loaded.skill_id,
        revision_id=loaded.revision_id,
        revision_number=loaded.revision_number,
        name=loaded.name,
        selection_mode=loaded.selection_mode,
    )


@router.get(
    "/{session_id}/general-skill-loads/{use_id}/resources/{resource_checksum}",
    response_model=GeneralSkillResourceRead,
)
def read_session_general_skill_resource(
    session_id: str,
    use_id: str,
    resource_checksum: str,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillResourceRead:
    """按 Use 固定 manifest 分页读取 UTF-8 文本资源。"""

    try:
        content, has_more = GeneralSkillRuntimeService(db).read_resource(
            current_user,
            session_id=session_id,
            use_id=use_id,
            resource_checksum=resource_checksum,
            offset=offset,
            limit=limit,
        )
        decoded = content.decode("utf-8", errors="strict")
    except (GeneralSkillRuntimeError, UnicodeDecodeError) as exc:
        if isinstance(exc, GeneralSkillRuntimeError):
            raise _http_error(exc) from exc
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "GENERAL_SKILL_RESOURCE_NOT_TEXT",
                "message": "resource is not UTF-8 text",
            },
        ) from exc
    return GeneralSkillResourceRead(
        use_id=use_id,
        resource_checksum=resource_checksum,
        offset=offset,
        content=decoded,
        has_more=has_more,
    )
