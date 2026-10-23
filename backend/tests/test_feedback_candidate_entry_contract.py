"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_feedback_candidate_entry_contract.py
@CallChain  : 当前编辑页原HTTP保存/draft → 原Skill API → 受管候选与活动版边界
@Description: 复现候选身份被旧输入丢弃后直接改活动版或下架，保留普通编辑对照。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.skills import router
from app.db import get_session
from app.db.models import Skill, User
from app.security.auth import get_current_user
from tests.test_feedback_materials import feedback_engine as _feedback_engine
from tests.test_feedback_scope_versions_audit import _card, _seed_scope

candidate_entry_engine = _feedback_engine


def _client(engine):
    """仅隔离认证/会话依赖；请求实际经过原生产router的解析、权限和提交。"""

    with Session(engine) as db:
        actor, _scopes = _seed_scope(db)
        actor_id = actor.id
    app = FastAPI()
    app.include_router(router)

    def session():
        """每次HTTP使用独立真实连接，提交行为仍由生产函数决定。"""

        with Session(engine) as db:
            yield db

    def current_actor():
        """只替代登录身份获取，不改变实际资源归属或管理权限校验。"""

        with Session(engine) as db:
            actor = db.get(User, actor_id)
            db.expunge(actor)
            return actor

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user] = current_actor
    return TestClient(app)


@pytest.mark.parametrize("action", ["save", "draft"])
def test_managed_candidate_identity_cannot_be_ignored_by_legacy_activity_write(
    candidate_entry_engine, action,
):
    """旧保存和下架入口不能丢弃受管身份后改活动版，必须引导隔离候选入口。"""

    with _client(candidate_entry_engine) as client:
        skill_id = _card("base", "2.0.0").skill_id
        path = f"/api/enterprise/skills/{skill_id}"
        if action == "save":
            result = client.put(path, json={
                "tenant_id": "scope-tenant", "content": _card("candidate", "2.1.0").model_dump(),
                "feedback_candidate_id": "untrusted-candidate-identity",
            })
        else:
            result = client.post(path + "/draft", params={
                "tenant_id": "scope-tenant",
                "feedback_candidate_id": "untrusted-candidate-identity",
            })
        with Session(candidate_entry_engine) as db:
            active = db.exec(select(Skill).where(Skill.skill_id == skill_id)).one()
            assert active.status == "published"
            assert active.version == "2.0.0"
            assert active.content_json["name"] == "隔离来源-base"
        assert result.status_code == 409


def test_normal_editor_without_feedback_context_keeps_original_save_and_draft_contract(
    candidate_entry_engine,
):
    """从未进入反馈上下文的普通编辑仍可原保存/下架，不全局禁用旧能力。"""

    with _client(candidate_entry_engine) as client:
        skill_id = _card("base", "2.0.0").skill_id
        path = f"/api/enterprise/skills/{skill_id}"
        result = client.put(path, json={"tenant_id": "scope-tenant",
                                      "content": _card("normal", "2.1.0").model_dump()})
        assert result.status_code == 200
        assert result.json()["version"] == "2.1.0"
        result = client.post(path + "/draft", params={"tenant_id": "scope-tenant"})
        assert result.status_code == 200
        with Session(candidate_entry_engine) as db:
            active = db.exec(select(Skill).where(Skill.skill_id == skill_id)).one()
            assert active.version == "2.1.0"
            assert active.status == "draft"
