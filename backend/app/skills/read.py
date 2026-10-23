"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : read.py
@CallChain  : 原Skill API/候选普通重载 → 同一管理序列化 → SkillRead
@Description: 共享原管理头/统计/准确发布身份序列化，领域服务不反向依赖API。
"""
from app.db.models import Skill
from app.skills.skill_schema import SkillCard, SkillRead
from app.skills.step_ids import skill_card_with_unique_step_ids


def skill_read(
    row: Skill,
    stats: dict[str, dict[str, float | int]] | None = None,
    recent_stats: dict[str, dict[str, object]] | None = None,
) -> SkillRead:
    """将可见业务头和统计序列化，发布ID独立于业务版本及管理历史ID。"""

    all_stats = stats or {}
    skill_stats = stats_for(all_stats, row.skill_id, row.version)
    total_stats = all_stats.get(row.skill_id, {})
    recent_skill_stats = (recent_stats or {}).get(row.skill_id, {})
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    branch_meta = getattr(row, "agent_branch_meta", {}) or {}
    return SkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        total_call_count=int(total_stats.get("call_count", 0)),
        total_positive_feedback_count=int(total_stats.get("positive_feedback_count", 0)),
        total_negative_feedback_count=int(total_stats.get("negative_feedback_count", 0)),
        total_positive_rate=float(total_stats.get("positive_rate", 0.0)),
        total_negative_rate=float(total_stats.get("negative_rate", 0.0)),
        recent_versions=list(recent_skill_stats.get("recent_versions", [])),
        recent_call_count=int(recent_skill_stats.get("call_count", 0)),
        recent_positive_feedback_count=int(recent_skill_stats.get("positive_feedback_count", 0)),
        recent_negative_feedback_count=int(recent_skill_stats.get("negative_feedback_count", 0)),
        recent_positive_rate=float(recent_skill_stats.get("positive_rate", 0.0)),
        recent_negative_rate=float(recent_skill_stats.get("negative_rate", 0.0)),
        agent_id=branch_meta.get("agent_id"),
        branch_status=branch_meta.get("status"),
        branch_sync_state=branch_meta.get("sync_state"),
        branch_base_version=branch_meta.get("base_version"),
        branch_head_version=branch_meta.get("head_version"),
        metadata=dict(branch_meta.get("metadata") or {}),
        publication_id=getattr(row, "publication_identity", {}).get("id"),
        publication_scope_agent_id=getattr(row, "publication_identity", {}).get(
            "scope_agent_id", branch_meta.get("agent_id") or ""
        ),
        publication_checksum=getattr(row, "publication_identity", {}).get("checksum"),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def stats_key(skill_id: str, version: str) -> str:
    """保留原统计键合同，不用它作为Runtime或候选版本身份。"""
    return f"{skill_id}@{version}"


def stats_for(
    stats: dict[str, dict[str, float | int]], skill_id: str, version: str
) -> dict[str, float | int]:
    """按原业务统计键取数，发布身份与统计用途保持独立。"""
    return stats.get(stats_key(skill_id, version), {})
