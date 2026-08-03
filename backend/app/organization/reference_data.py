"""
@Time       : 2026/07/28 11:48
@Author     : zhanglp8181
@File       : reference_data.py
@CallChain  : 租户初始化/成员管理 API → 码表服务 → CodeSet/CodeItem
@Description: 初始化并校验租户级业务码表，确保稳定编码与活动引用约束。
"""

from __future__ import annotations

import re

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import CodeItem, CodeSet, utc_now


MEMBER_CATEGORY_SET_CODE = "member_category"
MEMBER_CATEGORY_ITEMS = (
    ("employee", "正式员工", 10),
    ("contractor", "合同员工", 20),
    ("consultant", "顾问", 30),
    ("intern", "实习生", 40),
    ("external_collaborator", "外部协作者", 50),
)
ORGANIZATION_UNIT_TYPE_SET_CODE = "organization_unit_type"
ORGANIZATION_UNIT_TYPE_ITEMS = (
    ("company", "企业", 10),
    ("division", "事业部", 20),
    ("department", "部门", 30),
    ("center", "中心", 40),
    ("team", "团队", 50),
    ("project", "项目组", 60),
)
POSITION_TYPE_SET_CODE = "position_type"
POSITION_TYPE_ITEMS = (
    ("management", "管理岗位", 10),
    ("professional", "专业岗位", 20),
    ("operations", "运营岗位", 30),
    ("support", "支持岗位", 40),
    ("project", "项目岗位", 50),
)
ORGANIZATION_LEADER_TYPE_SET_CODE = "organization_leader_type"
ORGANIZATION_LEADER_TYPE_ITEMS = (
    ("primary", "主要负责人", 10),
    ("deputy", "副负责人", 20),
    ("acting", "代理负责人", 30),
    ("project", "项目负责人", 40),
)
AGENT_CATEGORY_SET_CODE = "agent_category"
AGENT_CATEGORY_ITEMS = (
    ("assistant", "通用助理", 10),
    ("professional", "专业专家", 20),
    ("service", "业务服务", 30),
    ("operations", "运营协同", 40),
)
CONFIGURABLE_BUSINESS_CODE_SETS = (
    MEMBER_CATEGORY_SET_CODE,
    ORGANIZATION_UNIT_TYPE_SET_CODE,
    POSITION_TYPE_SET_CODE,
    ORGANIZATION_LEADER_TYPE_SET_CODE,
    AGENT_CATEGORY_SET_CODE,
)
CODE_ITEM_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ReferenceDataError(ValueError):
    """表示租户业务码表治理请求违反白名单、编码或 revision 契约。"""


def ensure_member_category_catalog(db: Session, tenant_id: str) -> CodeSet:
    """幂等创建成员类别码表及内置码项，不覆盖租户已调整的显示信息。"""

    return _ensure_catalog(
        db,
        tenant_id=tenant_id,
        set_code=MEMBER_CATEGORY_SET_CODE,
        name="成员类别",
        description="成员的业务用工或协作类别，不产生权限。",
        items=MEMBER_CATEGORY_ITEMS,
    )


def require_active_member_category(
    db: Session,
    tenant_id: str,
    item_code: str,
) -> CodeItem:
    """验证成员类别属于当前租户的活动码表与活动码项。"""

    normalized_code = item_code.strip()
    code_set = ensure_member_category_catalog(db, tenant_id)
    if code_set.status != "active":
        raise ValueError("MEMBER_CATEGORY_SET_INACTIVE")
    item = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == normalized_code,
        )
    ).first()
    if item is None:
        raise ValueError(f"UNKNOWN_MEMBER_CATEGORY:{normalized_code}")
    if item.status != "active":
        raise ValueError(f"INACTIVE_MEMBER_CATEGORY:{normalized_code}")
    return item


def ensure_organization_unit_type_catalog(db: Session, tenant_id: str) -> CodeSet:
    """幂等创建组织类型码表，不预置任何业务组织节点。"""

    return _ensure_catalog(
        db,
        tenant_id=tenant_id,
        set_code=ORGANIZATION_UNIT_TYPE_SET_CODE,
        name="组织类型",
        description="组织节点的业务分类，不产生层级或权限。",
        items=ORGANIZATION_UNIT_TYPE_ITEMS,
    )


def require_active_organization_unit_type(
    db: Session,
    tenant_id: str,
    item_code: str,
) -> CodeItem:
    """验证组织类型属于当前租户的活动码表和活动码项。"""

    normalized_code = item_code.strip()
    code_set = ensure_organization_unit_type_catalog(db, tenant_id)
    if code_set.status != "active":
        raise ValueError("ORGANIZATION_UNIT_TYPE_SET_INACTIVE")
    item = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == normalized_code,
        )
    ).first()
    if item is None:
        raise ValueError(f"UNKNOWN_ORGANIZATION_UNIT_TYPE:{normalized_code}")
    if item.status != "active":
        raise ValueError(f"INACTIVE_ORGANIZATION_UNIT_TYPE:{normalized_code}")
    return item


def ensure_position_type_catalog(db: Session, tenant_id: str) -> CodeSet:
    """幂等创建岗位类型码表，岗位类型只分类而不产生授权。"""

    return _ensure_catalog(
        db,
        tenant_id=tenant_id,
        set_code=POSITION_TYPE_SET_CODE,
        name="岗位类型",
        description="岗位的业务分类，不直接产生任职或权限。",
        items=POSITION_TYPE_ITEMS,
    )


def require_active_position_type(
    db: Session,
    tenant_id: str,
    item_code: str,
) -> CodeItem:
    """验证岗位类型属于当前租户的活动码表和活动码项。"""

    normalized_code = item_code.strip()
    code_set = ensure_position_type_catalog(db, tenant_id)
    if code_set.status != "active":
        raise ValueError("POSITION_TYPE_SET_INACTIVE")
    item = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == normalized_code,
        )
    ).first()
    if item is None:
        raise ValueError(f"UNKNOWN_POSITION_TYPE:{normalized_code}")
    if item.status != "active":
        raise ValueError(f"INACTIVE_POSITION_TYPE:{normalized_code}")
    return item


def ensure_organization_leader_type_catalog(db: Session, tenant_id: str) -> CodeSet:
    """幂等创建负责人类型码表，不创建任何负责人关系。"""

    return _ensure_catalog(
        db,
        tenant_id=tenant_id,
        set_code=ORGANIZATION_LEADER_TYPE_SET_CODE,
        name="负责人类型",
        description="组织责任关系的业务分类，不直接产生角色或权限。",
        items=ORGANIZATION_LEADER_TYPE_ITEMS,
    )


def require_active_organization_leader_type(
    db: Session,
    tenant_id: str,
    item_code: str,
) -> CodeItem:
    """验证负责人类型属于当前租户的活动码表与活动码项。"""

    return _require_active_item(
        db,
        tenant_id=tenant_id,
        set_code=ORGANIZATION_LEADER_TYPE_SET_CODE,
        item_code=item_code,
    )


def ensure_agent_category_catalog(db: Session, tenant_id: str) -> CodeSet:
    """幂等创建数字员工业务分类码表，分类只用于发现与展示。"""

    return _ensure_catalog(
        db,
        tenant_id=tenant_id,
        set_code=AGENT_CATEGORY_SET_CODE,
        name="数字员工业务分类",
        description="数字员工的业务发现分类，不产生所有权、角色或资源权限。",
        items=AGENT_CATEGORY_ITEMS,
    )


def require_active_agent_category(
    db: Session,
    tenant_id: str,
    item_code: str,
) -> CodeItem:
    """验证数字员工分类属于当前租户的活动码表与活动码项。"""

    return _require_active_item(
        db,
        tenant_id=tenant_id,
        set_code=AGENT_CATEGORY_SET_CODE,
        item_code=item_code,
    )


def ensure_configurable_business_catalog(
    db: Session,
    tenant_id: str,
    set_code: str,
) -> CodeSet:
    """只初始化服务器白名单中的业务码表，拒绝把安全协议变成租户配置。"""

    builders = {
        MEMBER_CATEGORY_SET_CODE: ensure_member_category_catalog,
        ORGANIZATION_UNIT_TYPE_SET_CODE: ensure_organization_unit_type_catalog,
        POSITION_TYPE_SET_CODE: ensure_position_type_catalog,
        ORGANIZATION_LEADER_TYPE_SET_CODE: ensure_organization_leader_type_catalog,
        AGENT_CATEGORY_SET_CODE: ensure_agent_category_catalog,
    }
    builder = builders.get(set_code)
    if builder is None:
        raise ReferenceDataError("CODE_SET_NOT_CONFIGURABLE")
    return builder(db, tenant_id)


def create_business_code_item(
    db: Session,
    *,
    tenant_id: str,
    set_code: str,
    item_code: str,
    name: str,
    description: str | None,
    sort_order: int,
    actor_user_id: str,
) -> CodeItem:
    """在白名单业务码表中创建编码不可变的租户自定义码项。"""

    code_set = ensure_configurable_business_catalog(db, tenant_id, set_code)
    code = item_code.strip()
    normalized_name = name.strip()
    if not code_set.allow_custom_items:
        raise ReferenceDataError("CODE_SET_CUSTOM_ITEMS_DISABLED")
    if CODE_ITEM_PATTERN.fullmatch(code) is None:
        raise ReferenceDataError("INVALID_CODE_ITEM_CODE")
    if not normalized_name:
        raise ReferenceDataError("CODE_ITEM_NAME_REQUIRED")
    existing = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == code,
        )
    ).first()
    if existing is not None:
        raise ReferenceDataError("CODE_ITEM_EXISTS")
    item = CodeItem(
        tenant_id=tenant_id,
        code_set_id=code_set.id,
        item_code=code,
        name=normalized_name[:191],
        description=(description or "").strip()[:1024] or None,
        sort_order=sort_order,
        is_builtin=False,
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
    )
    db.add(item)
    db.flush()
    return item


def update_business_code_item(
    db: Session,
    *,
    tenant_id: str,
    set_code: str,
    item_code: str,
    name: str,
    description: str | None,
    status: str,
    sort_order: int,
    revision: int,
    actor_user_id: str,
) -> CodeItem:
    """按 revision 更新码项显示属性和状态，路径编码保持不可变。"""

    code_set = ensure_configurable_business_catalog(db, tenant_id, set_code)
    item = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == item_code,
        )
    ).first()
    if item is None:
        raise ReferenceDataError("CODE_ITEM_NOT_FOUND")
    if item.revision != revision:
        raise ReferenceDataError("CODE_ITEM_REVISION_CONFLICT")
    normalized_name = name.strip()
    if not normalized_name:
        raise ReferenceDataError("CODE_ITEM_NAME_REQUIRED")
    if status not in {"active", "inactive"}:
        raise ReferenceDataError("INVALID_CODE_ITEM_STATUS")
    item.name = normalized_name[:191]
    item.description = (description or "").strip()[:1024] or None
    item.status = status
    item.sort_order = sort_order
    item.revision += 1
    item.updated_by_user_id = actor_user_id
    item.updated_at = utc_now()
    db.add(item)
    db.flush()
    return item


def _ensure_catalog(
    db: Session,
    *,
    tenant_id: str,
    set_code: str,
    name: str,
    description: str,
    items: tuple[tuple[str, str, int], ...],
) -> CodeSet:
    """按统一约束幂等初始化一个租户业务码表及其内置码项。"""

    code_set = db.exec(
        select(CodeSet).where(
            CodeSet.tenant_id == tenant_id,
            CodeSet.set_code == set_code,
        )
    ).first()
    if code_set is None:
        candidate = CodeSet(
            tenant_id=tenant_id,
            set_code=set_code,
            name=name,
            description=description,
            allow_custom_items=True,
            is_system=True,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            code_set = candidate
        except IntegrityError:
            code_set = db.exec(
                select(CodeSet).where(
                    CodeSet.tenant_id == tenant_id,
                    CodeSet.set_code == set_code,
                )
            ).first()
            if code_set is None:
                raise
    existing_codes = {
        item.item_code
        for item in db.exec(
            select(CodeItem).where(
                CodeItem.tenant_id == tenant_id,
                CodeItem.code_set_id == code_set.id,
            )
        ).all()
    }
    for item_code, item_name, sort_order in items:
        if item_code not in existing_codes:
            candidate_item = CodeItem(
                    tenant_id=tenant_id,
                    code_set_id=code_set.id,
                    item_code=item_code,
                    name=item_name,
                    sort_order=sort_order,
                    is_builtin=True,
                )
            try:
                with db.begin_nested():
                    db.add(candidate_item)
                    db.flush()
            except IntegrityError:
                existing_item = db.exec(
                    select(CodeItem.id).where(
                        CodeItem.tenant_id == tenant_id,
                        CodeItem.code_set_id == code_set.id,
                        CodeItem.item_code == item_code,
                    )
                ).first()
                if existing_item is None:
                    raise
    db.flush()
    return code_set


def _require_active_item(
    db: Session,
    *,
    tenant_id: str,
    set_code: str,
    item_code: str,
) -> CodeItem:
    """读取白名单码表活动码项并返回稳定错误编码。"""

    normalized = item_code.strip()
    code_set = ensure_configurable_business_catalog(db, tenant_id, set_code)
    if code_set.status != "active":
        raise ReferenceDataError("CODE_SET_INACTIVE")
    item = db.exec(
        select(CodeItem).where(
            CodeItem.tenant_id == tenant_id,
            CodeItem.code_set_id == code_set.id,
            CodeItem.item_code == normalized,
        )
    ).first()
    if item is None:
        raise ReferenceDataError(f"UNKNOWN_CODE_ITEM:{normalized}")
    if item.status != "active":
        raise ReferenceDataError(f"INACTIVE_CODE_ITEM:{normalized}")
    return item
