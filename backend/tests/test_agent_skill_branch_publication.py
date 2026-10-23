"""
@Time       : 2026/10/22
@Author     : zhanglp8181
@File       : test_agent_skill_branch_publication.py
@CallChain  : pytest → ensure_agent_skill_branch → 员工范围 SkillVersion 补齐
@Description: 回归既有活动分支缺少精确发布快照时的确定性修复，避免 Runtime 误报缺少发布版本。
"""

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_skill_branch
from app.db.models import AgentProfile, AgentSkillBranch, Skill, SkillVersion
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card


def _branch_publication_content() -> dict[str, object]:
    """构造可编译的最小确定性 SkillCard，供分支发布回归复用。"""

    return {
        "skill_id": "branch-publication-regression",
        "name": "分支发布回归",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "nodes": [{"node_id": "done", "type": "terminal", "name": "完成"}],
        "edges": [],
        "start_node_id": "done",
        "terminal_node_ids": ["done"],
    }


def test_existing_active_branch_backfills_exact_published_version() -> None:
    """已有活动分支缺少范围发布行时，重新读取应补齐同一正文的精确快照。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    content = _branch_publication_content()
    definition = compile_legacy_skill_card(content)

    with Session(engine) as db:
        db.add(
            AgentProfile(
                id="agent_branch_publication_regression",
                tenant_id="tenant_branch_publication",
                name="分支发布回归员工",
                owner_user_id="user_branch_publication",
                status="active",
            )
        )
        db.add(
            Skill(
                id="skill_branch_publication_source",
                tenant_id="tenant_branch_publication",
                skill_id="branch-publication-regression",
                version="1.0.0",
                name="分支发布回归",
                content_json=content,
                status="published",
            )
        )
        db.add(
            SkillVersion(
                id="skill_version_branch_publication_global",
                tenant_id="tenant_branch_publication",
                skill_id="branch-publication-regression",
                scope_agent_id="",
                version="1.0.0",
                name="分支发布回归",
                content_json=content,
                status="published",
                compiled_definition_checksum=definition.checksum,
            )
        )
        db.add(
            AgentSkillBranch(
                id="agent_branch_publication_existing",
                tenant_id="tenant_branch_publication",
                agent_id="agent_branch_publication_regression",
                skill_id="branch-publication-regression",
                source_skill_id="skill_branch_publication_source",
                base_version="1.0.0",
                head_version="1.0.0",
                content_json=content,
                status="active",
                sync_state="synced",
            )
        )
        db.commit()

        skill = db.get(Skill, "skill_branch_publication_source")
        assert skill is not None
        ensure_agent_skill_branch(
            db,
            "tenant_branch_publication",
            "agent_branch_publication_regression",
            skill,
        )
        db.commit()

        version = db.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == "tenant_branch_publication",
                SkillVersion.skill_id == "branch-publication-regression",
                SkillVersion.scope_agent_id == "agent_branch_publication_regression",
                SkillVersion.version == "1.0.0",
                SkillVersion.status == "published",
            )
        ).one()
        assert version.compiled_definition_checksum == definition.checksum
