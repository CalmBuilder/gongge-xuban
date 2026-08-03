"""
@Time       : 2026/08/02 14:10
@Author     : zhanglp8181
@File       : web_search_migration.py
@CallChain  : 显式迁移脚本 → 联网查询发布头/不可变快照 → 跟随型数字员工分支
@Description: 把联网查询唯一遗留的自然语言分支条件升级为受限 DSL，并保持版本与分支幂等。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from sqlmodel import Session, select

from app.agents.branching import ensure_agent_skill_branch, sync_branch_from_overall
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


SOURCE_VERSION = "1.1.0"
TARGET_VERSION = "1.1.1"
SKILL_ID = "web_search"


class WebSearchMigrationError(RuntimeError):
    """表示联网查询发布头、快照或跟随分支不满足受控迁移前提。"""


@dataclass(frozen=True, slots=True)
class WebSearchMigrationReport:
    """汇总发布头是否迁移及其跟随型数字员工分支同步结果。"""

    migrated: bool
    already_migrated: bool
    synchronized_branch_ids: tuple[str, ...]
    already_synchronized_branch_ids: tuple[str, ...]


def apply_web_search_condition_upgrade(
    db: Session,
    *,
    tenant_id: str,
) -> WebSearchMigrationReport:
    """锁定来源快照，创建受限条件新版本并同步未分叉的活动 Agent 分支。"""

    skill = db.exec(
        select(Skill)
        .where(Skill.tenant_id == tenant_id, Skill.skill_id == SKILL_ID)
        .with_for_update()
    ).first()
    if skill is None or skill.status != "published":
        raise WebSearchMigrationError("联网查询当前没有 published 发布头")

    migrated = False
    already_migrated = False
    if skill.version == TARGET_VERSION:
        target_snapshot = _published_snapshot(db, skill, TARGET_VERSION)
        if target_snapshot.derived_from_version_id is None:
            raise WebSearchMigrationError("联网查询目标版本缺少派生来源")
        compiled = compile_legacy_skill_card(skill.content_json)
        if compiled.diagnostics:
            raise WebSearchMigrationError("联网查询目标版本仍存在编译诊断")
        already_migrated = True
    else:
        if skill.version != SOURCE_VERSION:
            raise WebSearchMigrationError(
                f"联网查询当前版本 {skill.version} 不等于受控来源 {SOURCE_VERSION}"
            )
        source_snapshot = _published_snapshot(db, skill, SOURCE_VERSION)
        target_content = build_web_search_upgraded_content(skill.content_json)
        compiled = compile_legacy_skill_card(target_content)
        if compiled.diagnostics:
            codes = ", ".join(item.code for item in compiled.diagnostics)
            raise WebSearchMigrationError(f"联网查询升级后仍有诊断：{codes}")
        skill.version = TARGET_VERSION
        skill.content_json = target_content
        skill.updated_at = utc_now()
        db.add(skill)
        db.flush()
        write_skill_version(
            db,
            skill,
            compiled_definition=compiled,
            derived_from_version_id=source_snapshot.id,
            version_id="skillver_m6_web_search_1_1_1",
        )
        migrated = True

    synchronized, already_synchronized = _sync_following_branches(db, skill)
    return WebSearchMigrationReport(
        migrated=migrated,
        already_migrated=already_migrated,
        synchronized_branch_ids=synchronized,
        already_synchronized_branch_ids=already_synchronized,
    )


def build_web_search_upgraded_content(source_content: dict[str, object]) -> dict[str, object]:
    """只替换联网查询的条件语言和声明 schema，不改节点目标、工具或失败语义。"""

    content = deepcopy(source_content)
    nodes = [dict(node) for node in content.get("nodes", []) if isinstance(node, dict)]
    node_ids = {str(node.get("node_id") or "") for node in nodes}
    required_nodes = {
        "collect_search_need",
        "execute_web_search",
        "format_and_reply",
        "failure_handling",
    }
    if not required_nodes.issubset(node_ids):
        raise WebSearchMigrationError("联网查询来源定义缺少受控节点")
    for node in nodes:
        node["condition"] = None

    edges = [dict(edge) for edge in content.get("edges", []) if isinstance(edge, dict)]
    by_route = {
        (str(edge.get("source_node_id") or ""), str(edge.get("next_node_id") or "")): edge
        for edge in edges
    }
    required_routes = {
        ("collect_search_need", "execute_web_search"),
        ("execute_web_search", "format_and_reply"),
        ("execute_web_search", "failure_handling"),
    }
    if set(by_route) != required_routes:
        raise WebSearchMigrationError("联网查询来源定义的分支集合发生未知变化")
    by_route[("collect_search_need", "execute_web_search")].update(
        {"condition": {"op": "always"}, "priority": 100}
    )
    by_route[("execute_web_search", "format_and_reply")].update(
        {
            "condition": {
                "op": "eq",
                "left": {"path": "tool_result.web_search_result.status"},
                "right": {"value": "succeeded"},
            },
            "priority": 100,
        }
    )
    by_route[("execute_web_search", "failure_handling")].update(
        {"condition": {"op": "always"}, "priority": 0}
    )
    content["version"] = TARGET_VERSION
    content["nodes"] = nodes
    content["edges"] = edges
    content["condition_schemas"] = {
        "slots": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string"},
                "count": {"type": "integer"},
                "search_recency_filter": {"type": "string"},
                "search_domain_filter": {"type": "string"},
            },
        },
        "tool_result": {
            "type": "object",
            "properties": {
                "web_search_result": {
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                }
            },
        },
    }
    return content


def _published_snapshot(db: Session, skill: Skill, version: str) -> SkillVersion:
    """锁定并校验指定发布快照与当前发布头的内容一致。"""

    snapshot = db.exec(
        select(SkillVersion)
        .where(
            SkillVersion.tenant_id == skill.tenant_id,
            SkillVersion.skill_id == skill.skill_id,
            SkillVersion.version == version,
            SkillVersion.status == "published",
        )
        .with_for_update()
    ).first()
    if snapshot is None:
        raise WebSearchMigrationError(f"联网查询 {version} 缺少 published 快照")
    if version == skill.version and skill_content_checksum(snapshot.content_json) != skill_content_checksum(
        skill.content_json
    ):
        raise WebSearchMigrationError(f"联网查询 {version} 发布头与快照内容不一致")
    return snapshot


def _sync_following_branches(
    db: Session,
    skill: Skill,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """同步显式绑定联网查询且仍跟随主干的活动数字员工分支。"""

    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == skill.tenant_id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == skill.id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    synchronized: list[str] = []
    already_synchronized: list[str] = []
    for binding in bindings:
        agent = db.get(AgentProfile, binding.agent_id)
        if agent is None or agent.status != "active" or agent.is_overall:
            continue
        branch_key = f"{agent.id}:{skill.skill_id}"
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == skill.tenant_id,
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
        if branch is None:
            ensure_agent_skill_branch(db, skill.tenant_id, agent.id, skill)
            synchronized.append(branch_key)
            continue
        if branch.status != "active" or branch.sync_state != "synced":
            raise WebSearchMigrationError(f"联网查询 Agent 分支不能自动覆盖：{branch_key}")
        if branch.head_version == skill.version:
            if skill_content_checksum(branch.content_json) != skill_content_checksum(
                skill.content_json
            ):
                raise WebSearchMigrationError(f"联网查询 Agent 分支版本相同但内容不一致：{branch_key}")
            already_synchronized.append(branch_key)
            continue
        sync_branch_from_overall(db, skill.tenant_id, agent.id, skill)
        synchronized.append(branch_key)
    return tuple(sorted(synchronized)), tuple(sorted(already_synchronized))
