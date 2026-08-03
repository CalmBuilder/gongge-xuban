"""
@Time       : 2026/07/22 14:10
@Author     : zhanglp8181
@File       : permissions.py
@CallChain  : Seed/组织角色 API/SOP 授权 → 权限目录与角色映射 → SQLModel
@Description: 管理受控业务域、稳定权限目录、角色权限关系和员工有效权限解析。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from app.db.models import (
    BusinessRole,
    BusinessRoleCategory,
    BusinessRolePermission,
    PermissionDefinition,
    utc_now,
)
from app.organization.roles import active_business_roles


@dataclass(frozen=True, slots=True)
class RoleCategoryDefinition:
    """描述角色和权限目录共用的受控业务域。"""

    code: str
    name: str
    description: str
    role_code_prefix: str


ROLE_CATEGORIES = (
    RoleCategoryDefinition(
        "governance",
        "平台治理",
        "成员、组织、授权、数字员工、知识和审计等平台治理职责",
        "governance",
    ),
    RoleCategoryDefinition("human_resources", "人事", "员工服务、假勤和人事证明", "hr"),
    RoleCategoryDefinition("finance", "财务", "报销、预算和财务复核", "finance"),
    RoleCategoryDefinition("administration", "行政", "会议室、用品和印章事务", "admin"),
    RoleCategoryDefinition("information_technology", "IT", "故障、权限和技术支持", "it"),
    RoleCategoryDefinition("legal_compliance", "法务合规", "合同、条款和尽调", "legal"),
    RoleCategoryDefinition("cross_functional", "跨部门", "跨业务域流程治理和演示", "cross"),
)
ROLE_CATEGORY_CODES = frozenset(item.code for item in ROLE_CATEGORIES)


@dataclass(frozen=True, slots=True)
class BuiltinPermissionDefinition:
    """保存由可执行代码或已发布流程声明的内置业务权限。"""

    code: str
    name: str
    category: str
    resource: str
    action: str
    scope: str | None
    description: str


BUILTIN_PERMISSIONS = (
    BuiltinPermissionDefinition(
        "tenant.settings.manage",
        "管理租户设置",
        "governance",
        "tenant.settings",
        "manage",
        None,
        "允许维护当前租户的显示信息和治理设置，不授予任何业务执行能力。",
    ),
    BuiltinPermissionDefinition(
        "member.read",
        "查看成员",
        "governance",
        "member",
        "read",
        None,
        "允许在授权组织范围内查看成员及其任职摘要。",
    ),
    BuiltinPermissionDefinition(
        "member.manage",
        "管理成员",
        "governance",
        "member",
        "manage",
        None,
        "允许在授权组织范围内维护成员生命周期和任职。",
    ),
    BuiltinPermissionDefinition(
        "organization.read",
        "查看组织",
        "governance",
        "organization",
        "read",
        None,
        "允许查看授权组织范围内的组织、负责人和统计。",
    ),
    BuiltinPermissionDefinition(
        "organization.manage",
        "管理组织",
        "governance",
        "organization",
        "manage",
        None,
        "允许维护授权范围内的组织结构和负责人。",
    ),
    BuiltinPermissionDefinition(
        "position.read",
        "查看岗位",
        "governance",
        "position",
        "read",
        None,
        "允许查看授权组织范围内的岗位和任职。",
    ),
    BuiltinPermissionDefinition(
        "position.manage",
        "管理岗位",
        "governance",
        "position",
        "manage",
        None,
        "允许维护授权组织范围内的岗位和岗位任职。",
    ),
    BuiltinPermissionDefinition(
        "reference_data.read",
        "查看数据码表",
        "governance",
        "reference_data",
        "read",
        None,
        "允许查看当前租户的治理码表。",
    ),
    BuiltinPermissionDefinition(
        "reference_data.manage",
        "管理数据码表",
        "governance",
        "reference_data",
        "manage",
        None,
        "允许维护当前租户的治理码表。",
    ),
    BuiltinPermissionDefinition(
        "authorization.read",
        "查看授权",
        "governance",
        "authorization",
        "read",
        None,
        "允许查看角色、权限目录、授权范围和有效权限解释。",
    ),
    BuiltinPermissionDefinition(
        "authorization.manage",
        "管理授权",
        "governance",
        "authorization",
        "manage",
        None,
        "允许创建角色并向其他成员授予可解释的治理或业务职责。",
    ),
    BuiltinPermissionDefinition(
        "agent.read",
        "查看数字员工治理信息",
        "governance",
        "agent",
        "read",
        None,
        "允许查看数字员工的治理字段，不包含私人记忆或凭据。",
    ),
    BuiltinPermissionDefinition(
        "agent.manage",
        "管理数字员工",
        "governance",
        "agent",
        "manage",
        None,
        "允许治理数字员工发布和可见性，不继承所有者的私人授权。",
    ),
    BuiltinPermissionDefinition(
        "knowledge.read",
        "查看知识治理信息",
        "governance",
        "knowledge",
        "read",
        None,
        "允许查看知识库治理信息，实际内容仍受知识访问范围约束。",
    ),
    BuiltinPermissionDefinition(
        "knowledge.manage",
        "管理知识",
        "governance",
        "knowledge",
        "manage",
        None,
        "允许维护知识库治理信息和访问策略。",
    ),
    BuiltinPermissionDefinition(
        "audit.read",
        "查看治理审计",
        "governance",
        "audit",
        "read",
        None,
        "允许查询授权范围内的治理审计记录。",
    ),
    BuiltinPermissionDefinition(
        "expense.quota.read:any",
        "查询任意员工报销额度",
        "finance",
        "expense.quota",
        "read",
        "any",
        "允许在明确代查目标后查询其他员工的报销额度。",
    ),
    BuiltinPermissionDefinition(
        "expense.travel_policy.assess",
        "执行差旅住宿标准评估",
        "finance",
        "expense.travel_policy",
        "assess",
        None,
        "允许财务数字员工按已发布 SOP 的冻结标准评估境内住宿费用。",
    ),
    BuiltinPermissionDefinition(
        "expense.invoice.verify",
        "执行报销发票查验",
        "finance",
        "expense.invoice",
        "verify",
        None,
        "允许财务数字员工在报销流程中查验结构化发票要素。",
    ),
    BuiltinPermissionDefinition(
        "expense.submit",
        "提交员工报销申请",
        "finance",
        "expense",
        "submit",
        None,
        "允许财务数字员工在政策、发票和明确确认门禁通过后提交报销单。",
    ),
    BuiltinPermissionDefinition(
        "expense.travel_review.claim",
        "认领差旅报销人工核对",
        "finance",
        "expense.travel_review",
        "claim",
        None,
        "允许财务报销专员认领超标或超出自动范围的差旅核对任务。",
    ),
    BuiltinPermissionDefinition(
        "expense.travel_review.complete",
        "完成差旅报销人工核对",
        "finance",
        "expense.travel_review",
        "complete",
        None,
        "允许已认领的财务报销专员提交差旅报销核对意见。",
    ),
    BuiltinPermissionDefinition(
        "expense.travel_review.request_information",
        "要求补充差旅报销材料",
        "finance",
        "expense.travel_review",
        "request_information",
        None,
        "允许已认领的财务报销专员要求申请人补充行程、审批或票据材料。",
    ),
    BuiltinPermissionDefinition(
        "expense.special_approval.create",
        "创建超标报销特批",
        "finance",
        "expense.special_approval",
        "create",
        None,
        "允许财务数字员工在申请人确认后创建结构化超标报销特批单。",
    ),
    BuiltinPermissionDefinition(
        "expense.special_approval.finalize",
        "回写超标报销特批步骤",
        "finance",
        "expense.special_approval",
        "finalize",
        None,
        "允许财务数字员工依据已完成工作项推进或结束顺序审批。",
    ),
    BuiltinPermissionDefinition(
        "expense.special_approval.claim",
        "认领超标报销特批任务",
        "finance",
        "expense.special_approval",
        "claim",
        None,
        "允许匹配当前审批步骤的候选人认领超标报销特批任务。",
    ),
    BuiltinPermissionDefinition(
        "expense.special_approval.approve",
        "批准超标报销特批步骤",
        "finance",
        "expense.special_approval",
        "approve",
        None,
        "允许当前步骤处理人提交批准决定。",
    ),
    BuiltinPermissionDefinition(
        "expense.special_approval.reject",
        "驳回超标报销特批步骤",
        "finance",
        "expense.special_approval",
        "reject",
        None,
        "允许当前步骤处理人提交驳回决定并结束申请。",
    ),
    BuiltinPermissionDefinition(
        "hr.leave_balance.read:any",
        "查询任意员工假期余额",
        "human_resources",
        "hr.leave_balance",
        "read",
        "any",
        "允许在明确代查目标后查询其他员工的假期余额。",
    ),
    BuiltinPermissionDefinition(
        "hr.leave.apply",
        "提交员工请假申请",
        "human_resources",
        "hr.leave",
        "apply",
        None,
        "允许人事数字员工在已发布 SOP、可信员工身份和明确确认约束下提交请假申请。",
    ),
    BuiltinPermissionDefinition(
        "hr.leave_review.claim",
        "认领请假人工核对",
        "human_resources",
        "hr.leave_review",
        "claim",
        None,
        "允许 HR 假勤专员认领余额不足、材料或假种超出自动范围的请假核对任务。",
    ),
    BuiltinPermissionDefinition(
        "hr.leave_review.complete",
        "完成请假人工核对",
        "human_resources",
        "hr.leave_review",
        "complete",
        None,
        "允许已认领的 HR 假勤专员提交请假人工核对意见。",
    ),
    BuiltinPermissionDefinition(
        "hr.leave_review.request_information",
        "要求补充请假材料",
        "human_resources",
        "hr.leave_review",
        "request_information",
        None,
        "允许已认领的 HR 假勤专员要求申请人补充请假材料。",
    ),
    BuiltinPermissionDefinition(
        "hr.overtime_review.claim",
        "认领加班调休人工核对",
        "human_resources",
        "hr.overtime_review",
        "claim",
        None,
        "允许 HR 假勤专员认领政策证据不足或资格异常的加班调休核对任务。",
    ),
    BuiltinPermissionDefinition(
        "hr.overtime_review.complete",
        "完成加班调休人工核对",
        "human_resources",
        "hr.overtime_review",
        "complete",
        None,
        "允许已认领的 HR 假勤专员提交加班调休人工核对意见。",
    ),
    BuiltinPermissionDefinition(
        "hr.overtime_review.request_information",
        "要求补充加班调休材料",
        "human_resources",
        "hr.overtime_review",
        "request_information",
        None,
        "允许已认领的 HR 假勤专员要求员工补充审批或考勤材料。",
    ),
    BuiltinPermissionDefinition(
        "hr.certificate.issue",
        "执行人事证明开具",
        "human_resources",
        "hr.certificate",
        "issue",
        None,
        "允许人事数字员工在已发布 SOP 和监督人约束下执行证明开具。",
    ),
    BuiltinPermissionDefinition(
        "hr.certificate_request.approve",
        "批准特殊证明申请",
        "human_resources",
        "hr.certificate_request",
        "approve",
        None,
        "允许特殊用途或收入证明候选复核人提交批准结果。",
    ),
    BuiltinPermissionDefinition(
        "hr.certificate_request.reject",
        "拒绝特殊证明申请",
        "human_resources",
        "hr.certificate_request",
        "reject",
        None,
        "允许特殊用途或收入证明候选复核人提交拒绝结果。",
    ),
    BuiltinPermissionDefinition(
        "legal.contract_reference.query",
        "检索合同参考资料",
        "legal_compliance",
        "legal.contract_reference",
        "query",
        None,
        "允许法务数字员工在已发布 SOP 和监督人约束下检索合同示例与判例参考。",
    ),
    BuiltinPermissionDefinition(
        "legal.contract_risk.assess",
        "执行合同风险初筛",
        "legal_compliance",
        "legal.contract_risk",
        "assess",
        None,
        "允许法务数字员工按已发布 SOP 对演示合同文本执行结构化风险初筛。",
    ),
    BuiltinPermissionDefinition(
        "legal.contract_review.claim",
        "认领高风险合同复核",
        "legal_compliance",
        "legal.contract_review",
        "claim",
        None,
        "允许候选法务复核人认领高风险合同人工复核任务。",
    ),
    BuiltinPermissionDefinition(
        "legal.contract_review.complete",
        "提交合同复核意见",
        "legal_compliance",
        "legal.contract_review",
        "complete",
        None,
        "允许已认领复核人提交完整的人工复核结论。",
    ),
    BuiltinPermissionDefinition(
        "legal.contract_review.request_information",
        "要求补充合同材料",
        "legal_compliance",
        "legal.contract_review",
        "request_information",
        None,
        "允许已认领复核人说明缺失材料并结束当前复核轮次。",
    ),
    BuiltinPermissionDefinition(
        "legal.partner_due_diligence.query",
        "执行合作方入库尽调",
        "legal_compliance",
        "legal.partner_due_diligence",
        "query",
        None,
        "允许法务数字员工按已发布 SOP 查询受控合作方演示尽调事实。",
    ),
    BuiltinPermissionDefinition(
        "legal.partner_due_diligence.claim",
        "认领合作方尽调复核",
        "legal_compliance",
        "legal.partner_due_diligence",
        "claim",
        None,
        "允许候选法务复核人认领高风险合作方尽调任务。",
    ),
    BuiltinPermissionDefinition(
        "legal.partner_due_diligence.complete",
        "提交合作方尽调复核意见",
        "legal_compliance",
        "legal.partner_due_diligence",
        "complete",
        None,
        "允许已认领复核人提交合作方风险复核结论。",
    ),
    BuiltinPermissionDefinition(
        "legal.partner_due_diligence.request_information",
        "要求补充合作方材料",
        "legal_compliance",
        "legal.partner_due_diligence",
        "request_information",
        None,
        "允许已认领复核人说明合作方缺失材料并结束当前复核轮次。",
    ),
    BuiltinPermissionDefinition(
        "it.ticket.claim",
        "认领 IT 工单",
        "information_technology",
        "it.ticket",
        "claim",
        None,
        "允许候选 IT 支持工程师认领待处理工单。",
    ),
    BuiltinPermissionDefinition(
        "it.ticket.resolve",
        "提交 IT 工单解决结果",
        "information_technology",
        "it.ticket",
        "resolve",
        None,
        "允许实际处理人提交解决说明并恢复报修流程。",
    ),
    BuiltinPermissionDefinition(
        "it.ticket.escalate",
        "升级 IT 工单",
        "information_technology",
        "it.ticket",
        "escalate",
        None,
        "允许实际处理人将无法解决的工单升级处理。",
    ),
    BuiltinPermissionDefinition(
        "it.access.grant",
        "执行系统权限开通",
        "information_technology",
        "it.access",
        "grant",
        None,
        "允许数字员工在已发布 SOP 规则和人类监督下执行权限开通。",
    ),
    BuiltinPermissionDefinition(
        "it.access_request.approve",
        "批准高权限申请",
        "information_technology",
        "it.access_request",
        "approve",
        None,
        "允许高权限申请候选处理人提交批准结果。",
    ),
    BuiltinPermissionDefinition(
        "it.access_request.reject",
        "拒绝高权限申请",
        "information_technology",
        "it.access_request",
        "reject",
        None,
        "允许高权限申请候选处理人提交拒绝结果。",
    ),
    BuiltinPermissionDefinition(
        "admin.seal_application.create",
        "创建用章审批申请",
        "administration",
        "admin.seal_application",
        "create",
        None,
        "允许行政数字员工在申请人明确确认后创建用章审批申请。",
    ),
    BuiltinPermissionDefinition(
        "admin.seal_application.finalize",
        "回写用章审批结果",
        "administration",
        "admin.seal_application",
        "finalize",
        None,
        "允许行政数字员工依据已完成工作项回写批准或驳回状态。",
    ),
    BuiltinPermissionDefinition(
        "admin.seal_application.claim",
        "认领用章审批任务",
        "administration",
        "admin.seal_application",
        "claim",
        None,
        "允许用章审批候选人认领与本人职责匹配的审批任务。",
    ),
    BuiltinPermissionDefinition(
        "admin.seal_application.approve",
        "同意用章申请",
        "administration",
        "admin.seal_application",
        "approve",
        None,
        "允许用章审批候选人提交同意结果。",
    ),
    BuiltinPermissionDefinition(
        "admin.seal_application.reject",
        "拒绝用章申请",
        "administration",
        "admin.seal_application",
        "reject",
        None,
        "允许用章审批候选人提交拒绝结果。",
    ),
    BuiltinPermissionDefinition(
        "sop.demo.approve",
        "同意流程验收任务",
        "cross_functional",
        "sop.demo",
        "approve",
        None,
        "允许演示审批候选人提交同意结果。",
    ),
    BuiltinPermissionDefinition(
        "sop.demo.reject",
        "拒绝流程验收任务",
        "cross_functional",
        "sop.demo",
        "reject",
        None,
        "允许演示审批候选人提交拒绝结果。",
    ),
)


class PermissionCatalogError(ValueError):
    """角色引用不存在、停用或跨租户权限时返回稳定错误。"""

    def __init__(self, code: str, message: str, *, permission_codes: list[str]) -> None:
        """保存机器错误码、用户说明和涉及的稳定权限编码。"""

        self.code = code
        self.permission_codes = permission_codes
        super().__init__(message)


def ensure_builtin_role_categories(db: Session, tenant_id: str) -> None:
    """并发安全地创建内置业务域，后续只允许管理员修改显示属性或停用。"""

    existing = db.exec(
        select(BusinessRoleCategory).where(BusinessRoleCategory.tenant_id == tenant_id)
    ).all()
    by_code = {item.category_code: item for item in existing}
    dialect_name = db.get_bind().dialect.name
    for definition in ROLE_CATEGORIES:
        if definition.code in by_code:
            continue
        row = BusinessRoleCategory(
            tenant_id=tenant_id,
            category_code=definition.code,
            name=definition.name,
            description=definition.description,
            role_code_prefix=definition.role_code_prefix,
            metadata_json={"source": "builtin_role_category_catalog"},
        )
        values = {
            column.name: getattr(row, column.name)
            for column in BusinessRoleCategory.__table__.columns
        }
        if dialect_name == "sqlite":
            statement = (
                sqlite_insert(BusinessRoleCategory)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "category_code"])
            )
            db.exec(statement)
        elif dialect_name == "mysql":
            statement = mysql_insert(BusinessRoleCategory).values(**values).prefix_with("IGNORE")
            db.exec(statement)
        else:
            db.add(row)
    db.flush()


def active_role_category_codes(db: Session, tenant_id: str) -> frozenset[str]:
    """读取租户当前有效分类编码，避免业务写入口依赖进程内常量。"""

    ensure_builtin_role_categories(db, tenant_id)
    rows = db.exec(
        select(BusinessRoleCategory).where(
            BusinessRoleCategory.tenant_id == tenant_id,
            BusinessRoleCategory.status == "active",
        )
    ).all()
    return frozenset(row.category_code for row in rows)


def ensure_builtin_permission_catalog(db: Session, tenant_id: str) -> None:
    """幂等创建或修复代码内置权限目录，不覆盖管理员可见名称以外的历史引用。"""

    existing = db.exec(
        select(PermissionDefinition).where(PermissionDefinition.tenant_id == tenant_id)
    ).all()
    by_code = {item.permission_code: item for item in existing}
    for definition in BUILTIN_PERMISSIONS:
        row = by_code.get(definition.code)
        if row is None:
            row = PermissionDefinition(
                tenant_id=tenant_id,
                permission_code=definition.code,
                name=definition.name,
                category=definition.category,
                resource=definition.resource,
                action=definition.action,
                scope=definition.scope,
                description=definition.description,
                metadata_json={"source": "builtin_permission_catalog"},
            )
        else:
            row.category = definition.category
            row.resource = definition.resource
            row.action = definition.action
            row.scope = definition.scope
            row.description = definition.description
            row.status = "active"
            row.updated_at = utc_now()
        db.add(row)
    db.flush()


def sync_role_permissions(
    db: Session,
    *,
    role: BusinessRole,
    permission_codes: Iterable[str],
) -> list[str]:
    """校验权限目录并以关系表替换角色权限，同时维护旧 JSON 读取缓存。"""

    normalized_codes = sorted({code.strip() for code in permission_codes if code.strip()})
    definitions = db.exec(
        select(PermissionDefinition).where(
            PermissionDefinition.tenant_id == role.tenant_id,
            PermissionDefinition.status == "active",
        )
    ).all()
    definitions_by_code = {
        item.permission_code: item
        for item in definitions
        if (item.category == "governance") == (role.role_kind == "governance")
    }
    unknown_codes = [code for code in normalized_codes if code not in definitions_by_code]
    if unknown_codes:
        raise PermissionCatalogError(
            "UNKNOWN_PERMISSION_DEFINITIONS",
            "角色只能选择权限目录中当前有效的权限点。",
            permission_codes=unknown_codes,
        )
    existing = db.exec(
        select(BusinessRolePermission).where(
            BusinessRolePermission.tenant_id == role.tenant_id,
            BusinessRolePermission.business_role_id == role.id,
        )
    ).all()
    desired_ids = {definitions_by_code[code].id for code in normalized_codes}
    for mapping in existing:
        if mapping.permission_definition_id not in desired_ids:
            db.delete(mapping)
    existing_ids = {mapping.permission_definition_id for mapping in existing}
    for permission_id in sorted(desired_ids - existing_ids):
        db.add(
            BusinessRolePermission(
                tenant_id=role.tenant_id,
                business_role_id=role.id,
                permission_definition_id=permission_id,
            )
        )
    role.permissions_json = normalized_codes
    role.updated_at = utc_now()
    db.add(role)
    db.flush()
    return normalized_codes


def role_permission_codes(db: Session, role: BusinessRole) -> list[str]:
    """从规范关系表读取角色权限；迁移过渡期无映射时兼容已有 JSON 缓存。"""

    mappings = db.exec(
        select(BusinessRolePermission).where(
            BusinessRolePermission.tenant_id == role.tenant_id,
            BusinessRolePermission.business_role_id == role.id,
        )
    ).all()
    if not mappings:
        return sorted(set(role.permissions_json or []))
    permission_ids = {mapping.permission_definition_id for mapping in mappings}
    definitions = db.exec(
        select(PermissionDefinition).where(
            PermissionDefinition.tenant_id == role.tenant_id,
            PermissionDefinition.status == "active",
        )
    ).all()
    return sorted(item.permission_code for item in definitions if item.id in permission_ids)


def employee_permission_codes(
    db: Session,
    *,
    tenant_id: str,
    employee_profile_id: str,
    organization_unit_ids: set[str] | None = None,
) -> list[str]:
    """按统一组织上下文合并员工有效业务权限，不读取平台 admin/member 身份。"""

    permissions: set[str] = set()
    for role in active_business_roles(
        db,
        tenant_id=tenant_id,
        employee_profile_id=employee_profile_id,
        organization_unit_ids=organization_unit_ids,
    ):
        permissions.update(role_permission_codes(db, role))
    return sorted(permissions)


def user_permission_codes(
    db: Session,
    *,
    tenant_id: str,
    user_id: str,
    organization_unit_ids: set[str] | None = None,
) -> list[str]:
    """由登录用户映射到员工档案，再按组织上下文合并有效业务权限。"""

    from app.db.models import EmployeeProfile

    profile = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == user_id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if profile is None:
        return []
    return employee_permission_codes(
        db,
        tenant_id=tenant_id,
        employee_profile_id=profile.id,
        organization_unit_ids=organization_unit_ids,
    )
