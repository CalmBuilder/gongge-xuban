"""
@Time       : 2026/07/29 11:25
@Author     : zhanglp8181
@File       : bulk_migration.py
@CallChain  : M5.5-D 预检/迁移命令 → 不可变版本服务 → Skill/SkillVersion/Agent 依赖
@Description: 显式校验并升级一期正式发布头，同时同步数字员工的跟随型技能分支。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from sqlmodel import Session, select

from app.agents.branching import (
    ensure_agent_skill_branch,
    is_open_gallery_resource,
    sync_branch_from_overall,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    Skill,
    SkillVersion,
    utc_now,
)
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.versioning import skill_content_checksum, write_skill_version


def _increment_patch(version: str) -> str:
    """把三段式版本补丁位递增一位，清单定义阶段拒绝非语义版本。"""

    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"M5.5-D 来源版本不是三段式语义版本：{version}")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


M55_SOURCE_VERSIONS: dict[str, str] = {
    "after_sales_exchange": "1.0.0",
    "after_sales_refund": "1.0.0",
    "contract_risk_review": "2.1.0",
    "expense_over_limit_approval": "2.1.0",
    "expense_travel_reimbursement": "2.1.0",
    "fault_report_v1": "3.1.0",
    "leave_apply_v1": "2.1.0",
    "participant_approval_demo": "1.1.0",
    "partner_onboarding_dd": "2.3.0",
    "seal_application_approval": "2.0.2",
    "skill_clause_modification": "2.0.0",
    "skill_expense_quota_query": "2.3.0",
    "skill_graph_visual_demo": "2.1.0",
    "skill_hr_cert_issue_001": "2.0.0",
    "skill_leave_balance_query": "2.2.0",
    "skill_meeting_room_book": "2.0.0",
    "skill_office_supply_request": "2.0.0",
    "skill_overtime_compensatory_leave": "3.1.0",
    "skill_perm_grant_routing_001": "2.2.0",
    "skill_price_compare_001": "1.0.0",
    "skill_purchase_001": "1.0.0",
}
M55_TARGET_VERSIONS: dict[str, str] = {
    skill_id: (
        "2.0.0"
        if skill_id in {"after_sales_exchange", "skill_purchase_001"}
        else _increment_patch(source_version)
    )
    for skill_id, source_version in M55_SOURCE_VERSIONS.items()
}

class M55MigrationError(RuntimeError):
    """表示发布头、快照或依赖事实不满足受控升级前提。"""


@dataclass(frozen=True, slots=True)
class M55MigrationReport:
    """汇总一次显式迁移事务实际升级、幂等命中和缺失的发布头。"""

    migrated_skill_ids: tuple[str, ...]
    already_migrated_skill_ids: tuple[str, ...]
    missing_skill_ids: tuple[str, ...]
    synchronized_branch_ids: tuple[str, ...]
    already_synchronized_branch_ids: tuple[str, ...]


def apply_m55_published_head_upgrade(
    db: Session,
    *,
    tenant_id: str,
    require_all: bool = True,
) -> M55MigrationReport:
    """校验来源快照后为正式清单内发布头创建新版本并同步跟随型分支。"""

    rows = db.exec(
        select(Skill)
        .where(
            Skill.tenant_id == tenant_id,
            Skill.skill_id.in_(tuple(M55_SOURCE_VERSIONS)),
        )
        .order_by(Skill.skill_id)
        .with_for_update()
    ).all()
    by_skill_id = {row.skill_id: row for row in rows}
    missing = tuple(sorted(set(M55_SOURCE_VERSIONS) - set(by_skill_id)))
    if require_all and missing:
        raise M55MigrationError(f"M5.5-D 缺少发布头：{', '.join(missing)}")

    migrated: list[str] = []
    already_migrated: list[str] = []
    for skill_id in sorted(by_skill_id):
        skill = by_skill_id[skill_id]
        source_version = M55_SOURCE_VERSIONS[skill_id]
        target_version = M55_TARGET_VERSIONS[skill_id]
        if skill.status != "published":
            raise M55MigrationError(f"{skill_id} 当前不是 published")
        if skill.version == target_version:
            _assert_target_snapshot(db, skill)
            already_migrated.append(skill_id)
            continue
        if skill.version != source_version:
            raise M55MigrationError(
                f"{skill_id} 当前版本 {skill.version} 不等于受控来源 {source_version}"
            )

        source_snapshot = _source_snapshot(db, skill)
        target_content = build_m55_upgraded_content(
            skill_id=skill_id,
            source_content=skill.content_json,
            target_version=target_version,
        )
        compiled = compile_legacy_skill_card(target_content)
        if compiled.diagnostics:
            diagnostic_codes = ", ".join(item.code for item in compiled.diagnostics)
            raise M55MigrationError(f"{skill_id} 升级后仍有诊断：{diagnostic_codes}")

        skill.version = target_version
        skill.content_json = target_content
        skill.updated_at = utc_now()
        db.add(skill)
        db.flush()
        write_skill_version(
            db,
            skill,
            compiled_definition=compiled,
            derived_from_version_id=source_snapshot.id,
            version_id=_target_version_id(skill_id, target_version),
        )
        migrated.append(skill_id)

    synchronized_branches, already_synchronized_branches = (
        _synchronize_active_synced_agent_branches(db, tenant_id=tenant_id)
    )
    return M55MigrationReport(
        migrated_skill_ids=tuple(migrated),
        already_migrated_skill_ids=tuple(already_migrated),
        missing_skill_ids=missing,
        synchronized_branch_ids=synchronized_branches,
        already_synchronized_branch_ids=already_synchronized_branches,
    )


def build_m55_upgraded_content(
    *,
    skill_id: str,
    source_content: dict[str, object],
    target_version: str,
) -> dict[str, object]:
    """复制旧定义并应用唯一获批的结构修正，不改变其他 SOP 的业务节点和条件。"""

    content = deepcopy(source_content)
    content["version"] = target_version
    if skill_id == "after_sales_exchange":
        _upgrade_exchange_definition(content)
    elif skill_id == "skill_purchase_001":
        _upgrade_purchase_definition(content)
    return content


def _synchronize_active_synced_agent_branches(
    db: Session,
    *,
    tenant_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把活动数字员工的 synced 分支推进到新发布头，同时拒绝覆盖用户已分叉的内容。"""

    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.status == "active",
        )
    ).all()
    synchronized: list[str] = []
    already_synchronized: list[str] = []
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        agent = db.get(AgentProfile, binding.agent_id)
        skill = db.get(Skill, binding.resource_id)
        if (
            agent is None
            or agent.status != "active"
            or agent.is_overall
            or skill is None
            or skill.tenant_id != tenant_id
            or skill.skill_id not in M55_TARGET_VERSIONS
        ):
            continue
        key = (agent.id, skill.skill_id)
        if key in seen:
            continue
        seen.add(key)
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
        branch_key = f"{agent.id}:{skill.skill_id}"
        if branch is None:
            ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
            synchronized.append(branch_key)
            continue
        if branch.status != "active":
            raise M55MigrationError(f"活动资源绑定的 Agent SOP 分支已停用：{branch_key}")
        if branch.sync_state != "synced":
            raise M55MigrationError(f"Agent SOP 分支存在用户定制，禁止自动覆盖：{branch_key}")
        if branch.head_version == skill.version:
            if skill_content_checksum(branch.content_json) != skill_content_checksum(
                skill.content_json
            ):
                raise M55MigrationError(f"Agent SOP 分支版本相同但内容不一致：{branch_key}")
            already_synchronized.append(branch_key)
            continue
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            raise M55MigrationError(f"私有 Agent SOP 分支不能自动跟随整体版本：{branch_key}")
        sync_branch_from_overall(db, tenant_id, agent.id, skill)
        synchronized.append(branch_key)
    return tuple(sorted(synchronized)), tuple(sorted(already_synchronized))


def _source_snapshot(db: Session, skill: Skill) -> SkillVersion:
    """锁定并验证来源发布快照与当前发布头内容一致。"""

    snapshot = db.exec(
        select(SkillVersion)
        .where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == skill.version,
            SkillVersion.status == "published",
        )
        .with_for_update()
    ).first()
    if snapshot is None:
        raise M55MigrationError(f"{skill.skill_id}@{skill.version} 缺少来源发布快照")
    if skill_content_checksum(snapshot.content_json) != skill_content_checksum(skill.content_json):
        raise M55MigrationError(f"{skill.skill_id}@{skill.version} 发布头与快照内容不一致")
    return snapshot


def _assert_target_snapshot(db: Session, skill: Skill) -> None:
    """验证已迁移发布头仍有内容一致的新版本快照，实现重复执行幂等。"""

    snapshot = _source_snapshot(db, skill)
    if snapshot.derived_from_version_id is None:
        raise M55MigrationError(f"{skill.skill_id}@{skill.version} 缺少派生来源")


def _target_version_id(skill_id: str, version: str) -> str:
    """生成跨双方言稳定且可审计的 M5.5-D 版本主键。"""

    return f"skillver_m55d_{skill_id}_{version.replace('.', '_')}"


def _upgrade_exchange_definition(content: dict[str, object]) -> None:
    """把换货流程的输入收集、订单查询和结果回复拆成三个确定性节点。"""

    nodes = [dict(node) for node in content.get("nodes", []) if isinstance(node, dict)]
    target = next(
        (node for node in nodes if node.get("node_id") == "collect_exchange_order_info"),
        None,
    )
    if target is None:
        raise M55MigrationError("售后换货流程缺少 collect_exchange_order_info")
    target.update(
        {
            "type": "collect_info",
            "instruction": "收集订单号和换货原因；只追问缺失信息，不调用工具。",
            "allowed_actions": ["ask_user", "continue_flow"],
            "expected_user_info": ["order_id", "exchange_reason"],
        }
    )
    nodes.extend(
        [
            {
                "node_id": "query_exchange_order",
                "type": "tool_call",
                "name": "查询换货订单",
                "instruction": "只使用已确认的订单号查询订单状态，不承诺一定可以换货。",
                "allowed_actions": ["call_tool:order.query"],
                "expected_user_info": [],
                "metadata": {
                    "operation_input": {"order_id": "slots.order_id"},
                    "operation_result_key": "exchange_order",
                },
            },
            {
                "node_id": "reply_exchange_guidance",
                "type": "response",
                "name": "反馈换货处理建议",
                "instruction": "依据订单查询回执说明换货下一步；政策不确定或订单不匹配时转人工。",
                "allowed_actions": ["answer_user", "handoff_human"],
                "expected_user_info": [],
            },
        ]
    )
    edges = [dict(edge) for edge in content.get("edges", []) if isinstance(edge, dict)]
    edges.extend(
        [
            {
                "source_node_id": "collect_exchange_order_info",
                "next_node_id": "query_exchange_order",
                "condition": None,
                "priority": 1,
                "label": "",
            },
            {
                "source_node_id": "query_exchange_order",
                "next_node_id": "reply_exchange_guidance",
                "condition": None,
                "priority": 2,
                "label": "",
            },
        ]
    )
    content["nodes"] = nodes
    content["edges"] = edges
    content["terminal_node_ids"] = ["reply_exchange_guidance"]
    content["execution_mode"] = "deterministic"
    content["condition_schemas"] = {
        **dict(content.get("condition_schemas") or {}),
        "slots": {
            "type": "object",
            "properties": {
                "exchange_type": {"type": "string"},
                "order_id": {"type": "string"},
                "exchange_reason": {"type": "string"},
            },
        },
    }


def _upgrade_purchase_definition(content: dict[str, object]) -> None:
    """把购买流程固定为确认后单次 product.purchase，再由独立回复节点展示回执。"""

    nodes = [dict(node) for node in content.get("nodes", []) if isinstance(node, dict)]
    by_id = {str(node.get("node_id")): node for node in nodes}
    for node_id in ("collect_user_name", "confirm_purchase"):
        if node_id not in by_id:
            raise M55MigrationError(f"购买流程缺少 {node_id}")
        by_id[node_id]["type"] = "collect_info"
    confirmation = by_id["confirm_purchase"]
    confirmation["metadata"] = {
        **dict(confirmation.get("metadata") or {}),
        "confirmation_policy": {
            "slot_name": "purchase_confirmed",
            "prompt": "请确认姓名、商品和数量；回复“确认下单”后才会创建订单。",
            "phrase_values": {
                "确认": "confirmed",
                "确认下单": "confirmed",
                "取消": "cancelled",
                "取消下单": "cancelled",
            },
        },
    }
    execute = by_id.get("confirm_product")
    response = by_id.get("create_order")
    if execute is None or response is None:
        raise M55MigrationError("购买流程缺少执行或回复节点")
    execute.update(
        {
            "type": "tool_call",
            "name": "创建购买订单",
            "instruction": "只在明确确认后调用一次 product.purchase 创建订单。",
            "allowed_actions": ["call_tool:product.purchase"],
            "expected_user_info": [],
            "metadata": {
                "operation_input": {
                    "user_id": "slots.user_name",
                    "product_id": "slots.product_id",
                    "quantity": "slots.quantity",
                },
                "operation_result_key": "purchase_order",
            },
        }
    )
    response.update(
        {
            "type": "response",
            "instruction": "只依据购买工具回执反馈订单号、商品、数量、金额和状态。",
            "allowed_actions": ["answer_user"],
            "expected_user_info": [],
        }
    )
    nodes.append(
        {
            "node_id": "reply_purchase_cancelled",
            "type": "response",
            "name": "反馈取消下单",
            "instruction": "确认用户已取消，本次没有创建订单。",
            "allowed_actions": ["answer_user"],
            "expected_user_info": [],
        }
    )
    edges = [
        dict(edge)
        for edge in content.get("edges", [])
        if isinstance(edge, dict)
        and not (
            edge.get("source_node_id") == "confirm_purchase"
            and edge.get("next_node_id") == "confirm_product"
        )
    ]
    edges.extend(
        [
            {
                "source_node_id": "confirm_purchase",
                "next_node_id": "confirm_product",
                "condition": {
                    "op": "eq",
                    "left": {"path": "slots.purchase_confirmed"},
                    "right": {"value": "confirmed"},
                },
                "priority": 100,
                "label": "确认",
            },
            {
                "source_node_id": "confirm_purchase",
                "next_node_id": "reply_purchase_cancelled",
                "condition": {"op": "always"},
                "priority": 0,
                "label": "取消",
            },
        ]
    )
    content["nodes"] = nodes
    content["edges"] = edges
    content["terminal_node_ids"] = ["create_order", "reply_purchase_cancelled"]
    content["execution_mode"] = "deterministic"
    content["condition_schemas"] = {
        **dict(content.get("condition_schemas") or {}),
        "slots": {
            "type": "object",
            "properties": {
                "user_name": {"type": "string"},
                "product_id": {"type": "string"},
                "quantity": {"type": "integer"},
                "purchase_confirmed": {"type": "string"},
            },
        },
    }

