"""
@Time       : 2026/07/28 15:40
@Author     : zhanglp8181
@File       : reference_data.py
@CallChain  : 数据码表页面 → FastAPI → 白名单业务码表服务 → CodeSet/CodeItem
@Description: 提供成员、组织、岗位和负责人类型的统一租户码表治理 API。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import CodeItem, CodeSet, User
from app.organization.reference_data import (
    CONFIGURABLE_BUSINESS_CODE_SETS,
    ReferenceDataError,
    create_business_code_item,
    ensure_configurable_business_catalog,
    update_business_code_item,
)
from app.organization.governance import ensure_governance_permission
from app.security.auth import get_current_user


router = APIRouter(prefix="/api/reference-data", tags=["reference-data"])


class CodeSetRead(BaseModel):
    """返回允许租户治理的业务码表，不暴露安全协议集合。"""

    code: str
    name: str
    description: str | None
    status: Literal["active", "inactive"]
    allow_custom_items: bool


class CodeItemRead(BaseModel):
    """返回编码不可变、可停用并保留历史的业务码项。"""

    code: str
    name: str
    description: str | None
    status: Literal["active", "inactive"]
    is_builtin: bool
    sort_order: int
    revision: int


class CodeItemCreate(BaseModel):
    """创建租户自定义码项的请求。"""

    tenant_id: str
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=191)
    description: str | None = Field(default=None, max_length=1024)
    sort_order: int = 100


class CodeItemUpdate(BaseModel):
    """按 revision 更新码项显示属性和状态。"""

    tenant_id: str
    name: str = Field(min_length=1, max_length=191)
    description: str | None = Field(default=None, max_length=1024)
    status: Literal["active", "inactive"]
    sort_order: int = 100
    revision: int = Field(ge=0)


@router.get("/code-sets", response_model=list[CodeSetRead])
def list_code_sets(
    tenant_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[CodeSetRead]:
    """列出服务器白名单业务码表，普通成员不可进入治理目录。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="reference_data.read",
    )
    rows: list[CodeSet] = []
    for set_code in CONFIGURABLE_BUSINESS_CODE_SETS:
        rows.append(ensure_configurable_business_catalog(db, tenant_id, set_code))
    db.commit()
    return [
        CodeSetRead(
            code=row.set_code,
            name=row.name,
            description=row.description,
            status=row.status,
            allow_custom_items=row.allow_custom_items,
        )
        for row in rows
    ]


@router.get("/code-sets/{set_code}/items", response_model=list[CodeItemRead])
def list_code_items(
    set_code: str,
    tenant_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[CodeItemRead]:
    """按白名单码表列出活动与历史展示所需全部码项。"""

    ensure_governance_permission(
        db,
        tenant_id=tenant_id,
        current_user=current_user,
        permission_code="reference_data.read",
    )
    try:
        code_set = ensure_configurable_business_catalog(db, tenant_id, set_code)
    except ReferenceDataError as error:
        raise _reference_http_error(error) from error
    db.commit()
    rows = db.exec(
        select(CodeItem)
        .where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
        )
        .order_by(CodeItem.sort_order, CodeItem.item_code)
    ).all()
    return [_item_read(row) for row in rows]


@router.post("/code-sets/{set_code}/items", response_model=CodeItemRead)
def create_code_item(
    set_code: str,
    request: CodeItemCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> CodeItemRead:
    """由租户管理员在白名单业务码表创建自定义码项。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="reference_data.manage",
    )
    try:
        item = create_business_code_item(
            db,
            tenant_id=request.tenant_id,
            set_code=set_code,
            item_code=request.code,
            name=request.name,
            description=request.description,
            sort_order=request.sort_order,
            actor_user_id=current_user.id,
        )
    except ReferenceDataError as error:
        raise _reference_http_error(error) from error
    db.commit()
    db.refresh(item)
    return _item_read(item)


@router.put("/code-sets/{set_code}/items/{item_code}", response_model=CodeItemRead)
def update_code_item(
    set_code: str,
    item_code: str,
    request: CodeItemUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> CodeItemRead:
    """更新码项显示信息和状态，不允许改变路径编码。"""

    ensure_governance_permission(
        db,
        tenant_id=request.tenant_id,
        current_user=current_user,
        permission_code="reference_data.manage",
    )
    try:
        item = update_business_code_item(
            db,
            tenant_id=request.tenant_id,
            set_code=set_code,
            item_code=item_code,
            name=request.name,
            description=request.description,
            status=request.status,
            sort_order=request.sort_order,
            revision=request.revision,
            actor_user_id=current_user.id,
        )
    except ReferenceDataError as error:
        raise _reference_http_error(error) from error
    db.commit()
    db.refresh(item)
    return _item_read(item)


def _item_read(item: CodeItem) -> CodeItemRead:
    """把数据库码项转换为统一前端契约。"""

    return CodeItemRead(
        code=item.item_code,
        name=item.name,
        description=item.description,
        status=item.status,
        is_builtin=item.is_builtin,
        sort_order=item.sort_order,
        revision=item.revision,
    )


def _reference_http_error(error: ReferenceDataError) -> HTTPException:
    """把码表领域错误映射为稳定 HTTP 状态。"""

    detail = str(error)
    if detail in {"CODE_SET_NOT_CONFIGURABLE", "CODE_ITEM_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=detail)
    if detail in {"CODE_ITEM_EXISTS", "CODE_ITEM_REVISION_CONFLICT"}:
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)
