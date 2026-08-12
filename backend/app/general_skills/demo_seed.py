"""
@Time       : 2026/08/12 13:05
@Author     : zhanglp8181
@File       : demo_seed.py
@CallChain  : demo_cli/浏览器夹具 → initialize_skill_five_closure_demo → User/AgentProfile
@Description: 幂等建立 Skill 五闭环演示所需身份映射和目标数字员工，不伪造能力或审批证据。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.db.models import AgentProfile, User, utc_now


SKILL_DEMO_SEED_SOURCE = "skill_five_closure_demo_v1"


@dataclass(frozen=True, slots=True)
class SkillDemoAgentDefinition:
    """声明一个演示数字员工的稳定身份、用途和所有者角色。"""

    id: str
    name: str
    description: str
    owner_role: str
    scenario_codes: tuple[str, ...]


SKILL_DEMO_AGENT_DEFINITIONS = (
    SkillDemoAgentDefinition(
        id="agent_skill_demo_a_docs",
        name="Skill演示A｜文档规范分身",
        description="演示从固定 GitHub commit 导入 writing-for-agents，并复用到本人多个数字员工。",
        owner_role="owner",
        scenario_codes=("G1-A",),
    ),
    SkillDemoAgentDefinition(
        id="agent_skill_demo_b_diagnosis",
        name="Skill演示B｜故障诊断分身",
        description="演示在对话中明确安装并使用 diagnosing-bugs。",
        owner_role="owner",
        scenario_codes=("G1-B",),
    ),
    SkillDemoAgentDefinition(
        id="agent_skill_demo_c_test_first",
        name="Skill演示C｜测试先行分身",
        description="演示 Agent 建议采用 tdd 与自主沉淀退款复核 Skill 的两条所有者确认闭环。",
        owner_role="owner",
        scenario_codes=("G1-C1", "G1-C2"),
    ),
    SkillDemoAgentDefinition(
        id="agent_skill_demo_d_publisher",
        name="Skill演示D｜问卷发布分身",
        description="演示导入 to-questionnaire、提交组织审核并形成不可变 Release。",
        owner_role="owner",
        scenario_codes=("G1-D",),
    ),
    SkillDemoAgentDefinition(
        id="agent_skill_demo_d_adopter",
        name="Skill演示D｜问卷采用分身",
        description="演示另一用户从组织广场主动采用并在对话中显式消费已审 Skill。",
        owner_role="adopter",
        scenario_codes=("G1-D",),
    ),
)


class SkillDemoSeedError(RuntimeError):
    """表示初始化身份、职责分离或既有记录所有权不满足安全边界。"""


def initialize_skill_five_closure_demo(
    db: Session,
    *,
    tenant_id: str,
    owner_username: str,
    adopter_username: str,
    reviewer_username: str,
) -> dict[str, object]:
    """幂等建立五闭环目标 Agent，并拒绝接管非本演示创建的同 ID/同名记录。"""

    users = {
        role: _active_user(db, tenant_id, username)
        for role, username in (
            ("owner", owner_username),
            ("adopter", adopter_username),
            ("reviewer", reviewer_username),
        )
    }
    if len({row.id for row in users.values()}) != 3:
        raise SkillDemoSeedError("owner, adopter and reviewer must be three distinct users")
    if users["reviewer"].role != "admin":
        raise SkillDemoSeedError("reviewer must be an active tenant administrator")

    created: list[str] = []
    unchanged: list[str] = []
    for definition in SKILL_DEMO_AGENT_DEFINITIONS:
        owner = users[definition.owner_role]
        by_id = db.get(AgentProfile, definition.id)
        by_name = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.name == definition.name,
            )
        ).first()
        agent = by_id or by_name
        if agent is not None:
            metadata = dict(agent.metadata_json or {})
            if (
                agent.tenant_id != tenant_id
                or metadata.get("seed_source") != SKILL_DEMO_SEED_SOURCE
                or agent_owner_user_id(agent) != owner.id
            ):
                raise SkillDemoSeedError(
                    f"refusing to take over existing agent identity: {definition.id}/{definition.name}"
                )
            unchanged.append(agent.id)
            continue
        agent = AgentProfile(
            id=definition.id,
            tenant_id=tenant_id,
            name=definition.name,
            description=definition.description,
            persona_prompt=(
                "你是 Skill 五闭环验收数字员工。只消费当前用户有权使用且经统一 resolver "
                "返回的固定 Skill 修订；安装、沉淀与发布均遵循页面确认和审批边界。"
            ),
            status="active",
            owner_user_id=owner.id,
            agent_category_code="professional",
            visibility_scope="private",
            metadata_json={
                "seed_source": SKILL_DEMO_SEED_SOURCE,
                "managed_by_seed": True,
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "scenario_codes": list(definition.scenario_codes),
                "demo_evidence_policy": "real_actions_only",
            },
        )
        db.add(agent)
        created.append(agent.id)
    db.commit()
    return {
        "tenant_id": tenant_id,
        "seed_source": SKILL_DEMO_SEED_SOURCE,
        "users": {
            role: {"id": user.id, "username": user.username, "role": user.role}
            for role, user in users.items()
        },
        "created_agent_ids": created,
        "unchanged_agent_ids": unchanged,
        "agents": [asdict(row) for row in SKILL_DEMO_AGENT_DEFINITIONS],
        "initialized_at": utc_now().isoformat(),
    }


def inspect_skill_five_closure_demo(
    db: Session,
    *,
    tenant_id: str,
) -> dict[str, object]:
    """返回初始化身份事实和各 Agent 当前存在性，供 CLI 与验收脚本复核。"""

    rows = []
    for definition in SKILL_DEMO_AGENT_DEFINITIONS:
        agent = db.get(AgentProfile, definition.id)
        rows.append(
            {
                **asdict(definition),
                "exists": bool(agent and agent.tenant_id == tenant_id),
                "owner_user_id": agent_owner_user_id(agent) if agent else None,
                "profile_revision": agent.profile_revision if agent else None,
                "status": agent.status if agent else None,
            }
        )
    return {
        "tenant_id": tenant_id,
        "seed_source": SKILL_DEMO_SEED_SOURCE,
        "ready": all(bool(row["exists"]) for row in rows),
        "agents": rows,
    }


def _active_user(db: Session, tenant_id: str, username: str) -> User:
    """按租户和用户名解析活动账号，避免初始化命令创建或重置任何密码。"""

    user = db.exec(
        select(User).where(
            User.tenant_id == tenant_id,
            User.username == username,
            User.membership_status == "active",
        )
    ).first()
    if user is None:
        raise SkillDemoSeedError(f"active demo user not found: {username}")
    return user
