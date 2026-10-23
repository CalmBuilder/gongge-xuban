"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_scope_versions_audit.py
@CallChain  : 独立F2来源审核 → 原Skill发布API → 员工投影 → Coordinator真实实例
@Description: 用双方言验证员工/整体同版本的准确发布身份、恢复冻结与租户隔离。
"""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.agents.branching import copy_overall_scope_to_agent, visible_skill_rows
from app.agents.schema import AgentResourceImportRequest
from app.api.agents import (
    import_agent_resources,
    promote_agent_skill_to_overall,
    sync_agent_skill_from_overall,
)
from app.api.skills import create_skill, publish_skill, rollback_skill_version, update_skill
from app.db.models import (
    AgentProfile,
    AgentSkillBranchVersion,
    ChatSession,
    Message,
    SkillVersion,
    SopInstance,
    Tenant,
    User,
)
from app.session.session_schema import StepAgentResult
from app.skills.skill_schema import SkillCard, SkillCreateRequest, SkillUpdateRequest
from app.sop_runtime.coordinator import DeterministicSopCoordinator
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_sop_coordinator import _content

scope_engine = _feedback_engine


def _card(tag: str, version: str) -> SkillCard:
    """构造有明确差异的等待定义，实际编译摘要必须区分各员工及整体说明。"""

    content = deepcopy(_content())
    content["version"] = version
    content["name"] = f"隔离来源-{tag}"
    content["nodes"][0]["instruction"] = f"按{tag}口径等待员工号，不执行后续动作。"
    return SkillCard.model_validate(content)


def _seed_scope(db: Session, tenant_id: str = "scope-tenant") -> tuple[User, list[AgentProfile]]:
    """只合成账号/范围，基础发布与员工领用通过原生产API和分支服务完成。"""

    owner = User(id=f"{tenant_id}-admin", tenant_id=tenant_id, username="scope-admin",
                 role="admin", password_hash="synthetic")
    agents = [AgentProfile(id=f"{tenant_id}-{label}", tenant_id=tenant_id, name=label,
                           is_overall=label == "overall",
                           metadata_json={"owner_user_id": owner.id})
              for label in ["overall", "alice", "bob"]]
    db.add_all([Tenant(id=tenant_id, name="隔离版本范围"), owner, *agents])
    db.commit()
    create_skill(SkillCreateRequest(tenant_id=tenant_id, content=_card("base", "2.0.0"),
                                   status="published"), agent_id=agents[0].id,
                 db=db, current_user=owner)
    for agent in agents[1:]:
        copy_overall_scope_to_agent(db, tenant_id, agent)
    db.commit()
    return owner, agents


def _publish(db: Session, owner: User, agent: AgentProfile, tag: str, version: str) -> None:
    """走原编辑与显式发布提交链，断言员工业务版本没有因scope绕冲突而被改名。"""

    skill_id = _card(tag, version).skill_id
    edited = update_skill(skill_id, SkillUpdateRequest(tenant_id=owner.tenant_id,
                                                       content=_card(tag, version)),
                          agent_id=agent.id, db=db, current_user=owner)
    assert edited.version == version
    published = publish_skill(skill_id, tenant_id=owner.tenant_id, agent_id=agent.id,
                              db=db, current_user=owner)
    assert published.version == version
    assert published.status == "published"


def _consume(db: Session, owner: User, agent: AgentProfile, suffix: str):
    """从真实可见投影启动等待实例，返回实际发布身份及独立编译的期望定义。"""

    projected = next(row for row in visible_skill_rows(db, owner.tenant_id, agent.id)
                     if row.skill_id == _card("base", "2.0.0").skill_id)
    expected = compile_legacy_skill_card(projected.content_json)
    chat = ChatSession(id=f"{agent.id}-{suffix}", tenant_id=owner.tenant_id, user_id=owner.id,
                       agent_id=agent.id, active_skill_id=projected.skill_id)
    turn = Message(id=f"{chat.id}-turn", tenant_id=owner.tenant_id, session_id=chat.id,
                   role="user", content="合成范围等待输入")
    db.add_all([chat, turn])
    db.commit()
    result = DeterministicSopCoordinator(db).prepare_step(chat, projected, StepAgentResult())
    instance = db.get(SopInstance, result.runtime_execution_id())
    assert instance is not None
    assert instance.agent_id == agent.id
    assert instance.tenant_id == owner.tenant_id
    assert instance.status == "waiting"
    db.commit()
    return instance, expected, projected, chat


def _assert_exact(db: Session, instance: SopInstance, expected, projected) -> SkillVersion:
    """实例与其不可变发布行必须精确等于原scope定义，而非同名整体或其他员工。"""

    version = db.get(SkillVersion, instance.skill_version_id)
    assert version is not None
    assert version.tenant_id == instance.tenant_id
    assert version.skill_id == projected.skill_id
    assert version.version == projected.version == instance.skill_version
    assert instance.definition_checksum == expected.checksum
    assert version.compiled_definition_checksum == expected.checksum
    assert version.content_json == projected.content_json
    return version


def test_employee_new_version_is_published_to_actual_runtime(scope_engine):
    """整体无该版本时，员工原API发布的新版本仍须有真实可消费的不可变身份。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-v2", "2.1.0")
        instance, expected, projected, _ = _consume(db, owner, agents[1], "new")
        _assert_exact(db, instance, expected, projected)
        overall = visible_skill_rows(db, owner.tenant_id, agents[0].id)[0]
        assert overall.version == "2.0.0"
        assert overall.name == "隔离来源-base"


def test_twin_employee_and_overall_same_version_keep_distinct_publications(scope_engine):
    """两个员工与整体同skill/version异定义，三个真实实例不能共享错误发布身份。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        for agent, tag in zip(agents[1:], ["alice-v2", "bob-v2"], strict=True):
            _publish(db, owner, agent, tag, "2.1.0")
        _publish(db, owner, agents[0], "overall-v2", "2.1.0")
        observed = [_consume(db, owner, agent, "twins") for agent in agents]
        ids = [_assert_exact(db, instance, definition, projected).id
               for instance, definition, projected, _ in observed]
        assert len(set(ids)) == 3
        assert len({definition.checksum for _, definition, _, _ in observed}) == 3
        original_count = len(db.exec(select(SkillVersion)).all())
        for agent, (instance, _, projected, _) in zip(agents, observed, strict=True):
            publish_skill(projected.skill_id, tenant_id=owner.tenant_id, agent_id=agent.id,
                          db=db, current_user=owner)
            current = visible_skill_rows(db, owner.tenant_id, agent.id)[0]
            resolved, _ = DeterministicSopCoordinator(db)._published_definition(current)
            assert resolved.id == instance.skill_version_id
        assert len(db.exec(select(SkillVersion)).all()) == original_count


def test_employee_update_preserves_old_waiting_instance_and_new_consumes_new(scope_engine):
    """员工发布后新会话消费新scope定义，重启恢复的旧等待实例仍冻结原ID与内容。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        old, old_definition, old_projected, chat = _consume(db, owner, agents[1], "old")
        _assert_exact(db, old, old_definition, old_projected)
        original_id, original_checksum, chat_id = old.skill_version_id, old.definition_checksum, chat.id
        owner_id, agent_id = owner.id, agents[1].id
        _publish(db, owner, agents[1], "alice-v2", "2.1.0")
        instance, expected, projected, _ = _consume(db, owner, agents[1], "next")
        _assert_exact(db, instance, expected, projected)
        assert instance.skill_version_id != original_id

    with Session(scope_engine) as restored:
        owner, agent = restored.get(User, owner_id), restored.get(AgentProfile, agent_id)
        current = visible_skill_rows(restored, owner.tenant_id, agent.id)[0]
        chat = restored.get(ChatSession, chat_id)
        turn = Message(id=f"{chat.id}-resume-turn", tenant_id=owner.tenant_id,
                       session_id=chat.id, role="user", content="保持等待，不提供员工号")
        restored.add(turn)
        restored.commit()
        result = DeterministicSopCoordinator(restored).prepare_step(chat, current, StepAgentResult())
        old = restored.get(SopInstance, result.runtime_execution_id())
        assert old.skill_version_id == original_id
        assert old.definition_checksum == original_checksum
        frozen, compiled = DeterministicSopCoordinator(restored)._definition_for_instance(old)
        assert frozen.content_json["name"] == "隔离来源-base"
        assert compiled.checksum == original_checksum


def test_employee_rollback_derives_runtime_publication_without_affecting_other_scope(scope_engine):
    """原员工回滚API派生新发布，旧scope快照保持，另一员工与整体不被改写。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-v2", "2.1.0")
        _publish(db, owner, agents[2], "bob-v2", "2.1.0")
        before = {agent.id: deepcopy(visible_skill_rows(db, owner.tenant_id, agent.id)[0].content_json)
                  for agent in [agents[0], agents[2]]}
        result = rollback_skill_version(_card("base", "2.0.0").skill_id, "2.0.0",
                                        tenant_id=owner.tenant_id, agent_id=agents[1].id,
                                        db=db, current_user=owner)
        assert result.version not in {"2.0.0", "2.1.0"}
        instance, expected, projected, _ = _consume(db, owner, agents[1], "rollback")
        _assert_exact(db, instance, expected, projected)
        assert projected.content_json["name"] == "隔离来源-base"
        for agent in [agents[0], agents[2]]:
            assert visible_skill_rows(db, owner.tenant_id, agent.id)[0].content_json == before[agent.id]


def test_same_named_versions_are_isolated_between_tenants(scope_engine):
    """同名同版本的两租户各消费原发布，跨租户管理和可见投影拒绝。"""

    with Session(scope_engine) as db:
        owners, agents = [], []
        for tenant_id in ["scope-tenant-a", "scope-tenant-b"]:
            owner, scopes = _seed_scope(db, tenant_id)
            _publish(db, owner, scopes[0], tenant_id, "2.1.0")
            owners.append(owner)
            agents.append(scopes)
        actual = [_consume(db, owner, scopes[0], "tenant")
                  for owner, scopes in zip(owners, agents, strict=True)]
        ids = [_assert_exact(db, instance, definition, projected).id
               for instance, definition, projected, _ in actual]
        assert len(set(ids)) == 2
        assert visible_skill_rows(db, owners[0].tenant_id, agents[1][0].id) == []
        with pytest.raises(HTTPException) as caught:
            publish_skill(_card("base", "2.0.0").skill_id, tenant_id=owners[1].tenant_id,
                          agent_id=agents[1][0].id, db=db, current_user=owners[0])
        assert caught.value.status_code == 403
        db.rollback()


def test_sync_from_overall_materializes_exact_employee_publication(scope_engine):
    """真实同步合法整体发布后，员工准确消费自己的发布ID且旧等待定义仍冻结。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        old, expected_old, old_projected, _ = _consume(db, owner, agents[1], "pre-sync")
        old_version = _assert_exact(db, old, expected_old, old_projected)
        old_id, old_content = old_version.id, deepcopy(old_version.content_json)
        _publish(db, owner, agents[0], "overall-next", "2.1.0")
        sync_agent_skill_from_overall(
            agents[1].id, old_projected.skill_id, tenant_id=owner.tenant_id,
            db=db, current_user=owner,
        )
        current, expected, projected, _ = _consume(db, owner, agents[1], "post-sync")
        employee_version = _assert_exact(db, current, expected, projected)
        overall, overall_expected, overall_projected, _ = _consume(
            db, owner, agents[0], "synced-overall"
        )
        overall_version = _assert_exact(db, overall, overall_expected, overall_projected)
        assert projected.version == "2.1.0"
        assert projected.name == "隔离来源-overall-next"
        assert employee_version.id != overall_version.id
        assert employee_version.content_json == overall_version.content_json
        frozen, definition = DeterministicSopCoordinator(db)._definition_for_instance(old)
        assert frozen.id == old_id
        assert frozen.content_json == old_content
        assert definition.checksum == expected_old.checksum


def test_sync_same_version_collision_never_overwrites_employee_history(scope_engine):
    """员工/整体同号异定义同步只能派生或409，不能覆写员工已发布历史。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-original", "2.1.0")
        old, expected_old, old_projected, _ = _consume(db, owner, agents[1], "collision-old")
        old_version = _assert_exact(db, old, expected_old, old_projected)
        old_id, old_content = old_version.id, deepcopy(old_version.content_json)
        _publish(db, owner, agents[0], "overall-conflicting", "2.1.0")
        rejected = False
        try:
            sync_agent_skill_from_overall(
                agents[1].id, old_projected.skill_id, tenant_id=owner.tenant_id,
                db=db, current_user=owner,
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
            rejected = True
        current, expected, projected, _ = _consume(db, owner, agents[1], "collision-next")
        _assert_exact(db, current, expected, projected)
        if rejected:
            assert projected.content_json == old_content
            assert current.skill_version_id == old_id
        else:
            assert projected.version != "2.1.0"
            assert projected.name == "隔离来源-overall-conflicting"
            assert current.skill_version_id != old_id
        db.expire_all()
        assert db.get(SkillVersion, old_id).content_json == old_content
        frozen, definition = DeterministicSopCoordinator(db)._definition_for_instance(old)
        assert frozen.content_json == old_content
        assert definition.checksum == expected_old.checksum


def test_promote_creates_distinct_overall_and_employee_runtime_identity(scope_engine):
    """原提升API发布整体并同步发起员工，各scope有准确ID，另一员工及旧实例保留。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        old, expected_old, old_projected, _ = _consume(db, owner, agents[1], "pre-promote")
        old_id = _assert_exact(db, old, expected_old, old_projected).id
        _publish(db, owner, agents[1], "alice-promoted", "2.1.0")
        promote_agent_skill_to_overall(
            agents[1].id, old_projected.skill_id, tenant_id=owner.tenant_id,
            db=db, current_user=owner,
        )
        observed = [_consume(db, owner, agent, "post-promote") for agent in agents]
        versions = [_assert_exact(db, instance, expected, projected)
                    for instance, expected, projected, _ in observed]
        assert versions[0].id != versions[1].id
        assert versions[0].content_json == versions[1].content_json
        assert versions[0].content_json["name"] == "隔离来源-alice-promoted"
        assert versions[2].content_json["name"] == "隔离来源-base"
        frozen, definition = DeterministicSopCoordinator(db)._definition_for_instance(old)
        assert frozen.id == old_id
        assert definition.checksum == expected_old.checksum


def test_promote_collision_is_atomic_and_never_overwrites_employee_snapshot(scope_engine):
    """整体派生号在发起员工已被不同内容占用时，提升须派生或稳定409且整体原子。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-history", "2.1.0")
        old, expected_old, old_projected, _ = _consume(db, owner, agents[1], "promote-collision-old")
        old_version = _assert_exact(db, old, expected_old, old_projected)
        old_id, old_content = old_version.id, deepcopy(old_version.content_json)
        _publish(db, owner, agents[1], "alice-latest", "2.2.0")
        before = {agent.id: deepcopy(visible_skill_rows(db, owner.tenant_id, agent.id)[0].content_json)
                  for agent in agents}
        rejected = False
        try:
            promote_agent_skill_to_overall(
                agents[1].id, old_projected.skill_id, tenant_id=owner.tenant_id,
                db=db, current_user=owner,
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
            rejected = True
        observed = [_consume(db, owner, agent, "promote-collision-next") for agent in agents]
        versions = [_assert_exact(db, instance, expected, projected)
                    for instance, expected, projected, _ in observed]
        if rejected:
            for agent, (_, _, projected, _) in zip(agents, observed, strict=True):
                assert projected.content_json == before[agent.id]
        else:
            assert versions[0].content_json["name"] == "隔离来源-alice-latest"
            assert versions[1].content_json["name"] == "隔离来源-alice-latest"
            assert versions[1].version != "2.1.0"
            assert versions[0].id != versions[1].id
            assert versions[2].content_json == before[agents[2].id]
        db.expire_all()
        assert db.get(SkillVersion, old_id).content_json == old_content
        frozen, definition = DeterministicSopCoordinator(db)._definition_for_instance(old)
        assert frozen.content_json == old_content
        assert definition.checksum == expected_old.checksum


def test_employee_import_preserves_source_and_materializes_target_publication(scope_engine):
    """真实批量导入合法员工版本后，目标准确消费自己的发布ID，源员工不被改写。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-import", "2.1.0")
        source, expected_source, projected_source, _ = _consume(
            db, owner, agents[1], "import-source"
        )
        source_version = _assert_exact(db, source, expected_source, projected_source)
        result = import_agent_resources(
            agents[2].id,
            AgentResourceImportRequest(
                tenant_id=owner.tenant_id, source_agent_id=agents[1].id,
                resource_type="skill", resource_ids=[projected_source.id],
            ),
            db=db, current_user=owner,
        )
        assert result["missing"] == []
        assert len(result["imported"]) == 1
        target, expected_target, projected_target, _ = _consume(
            db, owner, agents[2], "import-target"
        )
        target_version = _assert_exact(db, target, expected_target, projected_target)
        assert target_version.content_json == source_version.content_json
        assert target_version.id != source_version.id
        assert visible_skill_rows(db, owner.tenant_id, agents[1].id)[0].content_json == (
            projected_source.content_json
        )


def test_employee_import_same_version_collision_keeps_published_and_branch_history(scope_engine):
    """导入同号异定义只能派生或409，原目标发布及分支历史不能被导入代码覆写。"""

    with Session(scope_engine) as db:
        owner, agents = _seed_scope(db)
        _publish(db, owner, agents[1], "alice-import", "2.1.0")
        _publish(db, owner, agents[2], "bob-original", "2.1.0")
        old, expected_old, old_projected, _ = _consume(db, owner, agents[2], "import-old")
        old_version = _assert_exact(db, old, expected_old, old_projected)
        old_id, old_content = old_version.id, deepcopy(old_version.content_json)
        branch_history = db.exec(select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == owner.tenant_id,
            AgentSkillBranchVersion.agent_id == agents[2].id,
            AgentSkillBranchVersion.skill_id == old_projected.skill_id,
            AgentSkillBranchVersion.version == "2.1.0",
        )).one()
        history_id, history_content = branch_history.id, deepcopy(branch_history.content_json)
        rejected = False
        try:
            import_agent_resources(
                agents[2].id,
                AgentResourceImportRequest(
                    tenant_id=owner.tenant_id, source_agent_id=agents[1].id,
                    resource_type="skill", resource_ids=[old_projected.id],
                ),
                db=db, current_user=owner,
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
            rejected = True
        current, expected, projected, _ = _consume(db, owner, agents[2], "import-next")
        _assert_exact(db, current, expected, projected)
        if rejected:
            assert projected.content_json == old_content
            assert current.skill_version_id == old_id
        else:
            assert projected.version != "2.1.0"
            assert projected.name == "隔离来源-alice-import"
            assert current.skill_version_id != old_id
        db.expire_all()
        assert db.get(SkillVersion, old_id).content_json == old_content
        assert db.get(AgentSkillBranchVersion, history_id).content_json == history_content
        frozen, definition = DeterministicSopCoordinator(db)._definition_for_instance(old)
        assert frozen.content_json == old_content
        assert definition.checksum == expected_old.checksum
