"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_skill_candidate_authority_revisions.py
@CallChain  : 原账号/员工管理API → 授权撤销再授予 → 受管候选冻结代数
@Description: 真正写入入口必须留下ABA修订，无关文字或同值操作不制造撤销。
"""

from sqlmodel import Session

from app.agents.schema import AgentProfileUpdateRequest
from app.api.agents import update_agent
from app.api.auth import UserUpdateRequest, update_user
from app.db.models import AgentProfile, User
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _seed_scope

authority_revision_engine = _feedback_engine


def test_actor_role_aba_advances_authority_but_same_value_and_description_do_not(
    authority_revision_engine,
):
    """原授权写入口升权再撤权留下单调代数，即使最终role与初始完全相同。"""

    with Session(authority_revision_engine) as db:
        admin, _scopes = _seed_scope(db)
        target = User(id="authority-member", tenant_id=admin.tenant_id, username="member",
                      password_hash="synthetic", role="member")
        db.add(target)
        db.commit()
        start = target.auth_revision
        for role in ["admin", "member", "member"]:
            update_user(target.id, UserUpdateRequest(tenant_id=admin.tenant_id, role=role),
                        db=db, current_user=admin)
        current = db.get(User, target.id, populate_existing=True)
        assert current.role == "member"
        assert current.auth_revision == start + 2
        update_user(target.id, UserUpdateRequest(tenant_id=admin.tenant_id,
                                               display_name="普通资料编辑"),
                    db=db, current_user=admin)
        assert db.get(User, target.id, populate_existing=True).auth_revision == start + 2


def test_scope_lifecycle_aba_advances_existing_profile_revision(authority_revision_engine):
    """原员工归档再激活必须失效旧候选授权；同值和描述编辑保持代数。"""

    with Session(authority_revision_engine) as db:
        admin, scopes = _seed_scope(db)
        agent_id = scopes[1].id
        start = scopes[1].profile_revision
        for status in ["archived", "active", "active"]:
            update_agent(agent_id, AgentProfileUpdateRequest(tenant_id=admin.tenant_id,
                                                            status=status),
                         db=db, current_user=admin)
        agent = db.get(AgentProfile, agent_id, populate_existing=True)
        assert agent.status == "active"
        assert agent.profile_revision == start + 2
        update_agent(agent_id, AgentProfileUpdateRequest(tenant_id=admin.tenant_id,
                                                        description="普通资料编辑"),
                     db=db, current_user=admin)
        assert db.get(AgentProfile, agent_id, populate_existing=True).profile_revision == start + 2
