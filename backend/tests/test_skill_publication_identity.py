"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_skill_publication_identity.py
@CallChain  : 原管理API → 精确发布响应 → Runtime消费及冻结恢复
@Description: 区分分支历史ID与实际发布ID，保持整体读取隔离和损坏头拒绝。
"""

import pytest
from sqlmodel import Session, select

from app.api.skills import create_skill, get_skill, get_skill_version, list_skill_versions
from app.db.models import AgentSkillBranch, SkillVersion
from app.skills.skill_schema import SkillCreateRequest
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card, _publish, _seed_scope

identity_engine = _feedback_engine


def test_employee_and_overall_management_reads_name_exact_publication(identity_engine):
    """同版本不同定义的管理响应必须给出各自真实ID，整体历史不能混入员工行。"""

    with Session(identity_engine) as db:
        owner, scopes = _seed_scope(db)
        for agent, tag in zip(scopes, ["overall", "alice", "bob"], strict=True):
            _publish(db, owner, agent, tag, "2.1.0")
        publications = []
        skill_id = _card("base", "2.0.0").skill_id
        for scope in scopes:
            head = get_skill(skill_id, tenant_id=owner.tenant_id, agent_id=scope.id, db=db)
            version = get_skill_version(skill_id, "2.1.0", tenant_id=owner.tenant_id,
                                       agent_id=scope.id, db=db)
            assert head.publication_id == version.runtime_version_id
            actual = db.get(SkillVersion, head.publication_id)
            assert actual.content_json == head.content.model_dump()
            assert head.publication_checksum == actual.compiled_definition_checksum
            assert head.publication_scope_agent_id == actual.scope_agent_id
            assert actual.scope_agent_id == ("" if scope.is_overall else scope.id)
            if not scope.is_overall:
                assert version.id != version.runtime_version_id
            publications.append(actual.id)
        assert len(set(publications)) == 3
        overall = list_skill_versions(skill_id, tenant_id=owner.tenant_id,
                                     agent_id=scopes[0].id, db=db)
        assert {item.version for item in overall} == {"2.0.0", "2.1.0"}
        assert all(item.scope_agent_id == "" and item.runtime_version_id == item.id
                   for item in overall)


def test_private_employee_creation_has_no_fictitious_overall_publication(identity_engine):
    """原私有创建只产生员工发布，返回准确ID，不把私有正文写成整体版本。"""

    with Session(identity_engine) as db:
        owner, scopes = _seed_scope(db)
        content = _card("private", "1.0.0").model_copy(update={"skill_id": "private-identity"})
        result = create_skill(SkillCreateRequest(tenant_id=owner.tenant_id, content=content,
                                                status="published"), agent_id=scopes[1].id,
                              current_user=owner, db=db)
        versions = db.exec(select(SkillVersion).where(
            SkillVersion.skill_id == content.skill_id
        )).all()
        assert len(versions) == 1
        assert versions[0].scope_agent_id == scopes[1].id
        assert result.publication_id == versions[0].id
        current = get_skill(content.skill_id, tenant_id=owner.tenant_id,
                            agent_id=scopes[1].id, db=db)
        assert current.publication_id == result.publication_id


def test_mismatched_employee_head_cannot_consume_old_same_version(identity_engine):
    """活动头正文被异常改写时不继续执行旧同号定义，也不伪装已有发布身份。"""

    with Session(identity_engine) as db:
        owner, scopes = _seed_scope(db)
        branch = db.exec(select(AgentSkillBranch).where(
            AgentSkillBranch.agent_id == scopes[1].id
        )).one()
        changed = {**branch.content_json, "name": "异常同号正文"}
        branch.content_json = changed
        db.add(branch)
        db.commit()
        current = get_skill(branch.skill_id, tenant_id=owner.tenant_id,
                            agent_id=scopes[1].id, db=db)
        assert current.publication_id is None
        from app.agents.branching import visible_skill

        projected = visible_skill(db, owner.tenant_id, branch.skill_id, scopes[1].id)
        with pytest.raises(ValueError, match="活动头与精确发布快照不一致"):
            DeterministicSopCoordinator(db)._published_definition(projected)
