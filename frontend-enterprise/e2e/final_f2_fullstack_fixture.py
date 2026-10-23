"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f2_fullstack_fixture.py
@CallChain  : 隔离F2服务 → 合法身份资源/白名单旁证 → 真实页面/原API
@Description: 不预置消息、反馈、候选或执行成功；外租户沿原独立部署配置登录。
"""
from __future__ import annotations

import os
from pathlib import Path

from final_f1_fullstack_fixture import AGENTS, TENANT, digest

FOREIGN_TENANT = "tenant_f2_foreign"
FOREIGN_USER = "f2_foreign"
FOREIGN_AGENT = "agent_f2_foreign"
UNBOUND_AGENT = "agent_f2_unbound"
CANARIES = ("F2_OWNER_PRIVATE_CANARY", "F2_FOREIGN_PRIVATE_CANARY",
            "F2_CANDIDATE_PRIVATE_CANARY")


def seed_identity_resources() -> None:
    """只准备当前身份目标的账号、员工与个人事实，不创建任何运行或反馈历史。"""
    from sqlmodel import Session
    from app.db import engine
    from app.db.models import AgentProfile, AgentUsage, MemoryRecord, Tenant, User
    from app.security.auth import hash_password

    with Session(engine) as db:
        db.add(Tenant(id=FOREIGN_TENANT, name="F2隔离外租户"))
        db.add(User(id=FOREIGN_USER, tenant_id=FOREIGN_TENANT, username=FOREIGN_USER,
                    display_name="F2外租户成员", role="admin",
                    password_hash=hash_password("f2-foreign-synthetic-password")))
        db.add(AgentProfile(id=FOREIGN_AGENT, tenant_id=FOREIGN_TENANT,
                            name="F2外租户员工", owner_user_id=FOREIGN_USER))
        db.add(AgentUsage(tenant_id=FOREIGN_TENANT, user_id=FOREIGN_USER,
                          agent_id=FOREIGN_AGENT))
        db.add(AgentProfile(id=UNBOUND_AGENT, tenant_id=TENANT,
                            name="F2无知识绑定员工", owner_user_id="admin"))
        db.add(AgentUsage(tenant_id=TENANT, user_id="admin", agent_id=UNBOUND_AGENT))
        for user, tenant, agent, content in [
            ("admin", TENANT, AGENTS["sop"], CANARIES[0]),
            ("f1_reviewer", TENANT, AGENTS["sop"], "F2_OTHER_USER_STABLE_FACT"),
            (FOREIGN_USER, FOREIGN_TENANT, FOREIGN_AGENT, CANARIES[1]),
        ]:
            db.add(MemoryRecord(tenant_id=tenant, user_id=user, username=user,
                                agent_id=agent, agent_scope_key=agent, kind="preference",
                                content=content, importance=0.8,
                                metadata_json={"key": "f2_identity_fact"}))
        db.commit()


def identity_snapshot() -> dict:
    """读取本轮身份、清空代数和候选发布旁证，自由正文只记录散列和固定标记布尔。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (
        FeedbackMaterial, KnowledgeBase, KnowledgeDocument, MemoryCaptureScope,
        MemoryRecord, SkillEditCandidate, SkillVersion, SopInstance, User,
    )

    with Session(engine) as db:
        users = db.exec(select(User).where(User.id.in_(["admin", "f1_reviewer", FOREIGN_USER]))).all()
        memories = db.exec(select(MemoryRecord)).all()
        scopes = db.exec(select(MemoryCaptureScope)).all()
        materials = db.exec(select(FeedbackMaterial)).all()
        candidates = db.exec(select(SkillEditCandidate)).all()
        versions = db.exec(select(SkillVersion).where(SkillVersion.skill_id == "final_f1_delivery_sop")).all()
        documents = db.exec(select(KnowledgeDocument)).all()
        instances = db.exec(select(SopInstance)).all()
        return {
            "users": [{"id": r.id, "tenant_id": r.tenant_id, "role": r.role,
                       "membership_status": r.membership_status,
                       "auth_revision": r.auth_revision} for r in users],
            "all_instances": [{"id": r.id, "tenant_id": r.tenant_id,
                               "initiator_user_id": r.initiator_user_id,
                               "agent_id": r.agent_id, "kind": r.kind, "status": r.status}
                              for r in instances],
            "personal_memories": [{"id": r.id, "tenant_id": r.tenant_id,
                                  "user_id": r.user_id, "agent_id": r.agent_id,
                                  "content_hash": digest(r.content),
                                  "canary_flags": [value in r.content for value in CANARIES]}
                                 for r in memories],
            "memory_scopes": [{"user_id": r.user_id, "tenant_id": r.tenant_id,
                               "scope_kind": r.scope_kind, "agent_scope_key": r.agent_scope_key,
                               "clear_generation": r.clear_generation,
                               "permission_generation": r.permission_generation} for r in scopes],
            "materials": [{"id": r.id, "feedback_id": r.feedback_id,
                           "owner_user_id": r.owner_user_id, "target_agent_id": r.target_agent_id,
                           "status": r.status, "material_checksum": r.material_checksum}
                          for r in materials],
            "candidates": [{"id": r.id, "actor_user_id": r.actor_user_id,
                            "scope_agent_id": r.scope_agent_id, "status": r.status,
                            "actor_auth_revision": r.actor_auth_revision,
                            "base_skill_version_id": r.base_skill_version_id,
                            "revision": r.revision, "generation": r.generation,
                            "candidate_hash": digest(r.candidate_json)} for r in candidates],
            "skill_versions": [{"id": r.id, "skill_id": r.skill_id,
                                "scope_agent_id": r.scope_agent_id,
                                "checksum": r.content_checksum,
                                "definition_checksum": r.compiled_definition_checksum} for r in versions],
            "documents": [{"id": r.id, "knowledge_base_id": r.knowledge_base_id,
                           "version_id": r.knowledge_base_version_id,
                           "source_hash": digest(r.normalized_source),
                           "published_head": (db.get(KnowledgeBase, r.knowledge_base_id).metadata_json or {}).get("published_version_id")}
                          for r in documents],
        }


def prepare_final_f2(*, reuse: bool) -> None:
    """自有应用启动前安装只读旁证及第二租户构建入口；重启不重种数据。"""
    from fastapi.responses import HTMLResponse
    from starlette.staticfiles import StaticFiles
    from app.main import app

    if not reuse:
        seed_identity_resources()
    foreign = Path(os.environ["FULLSTACK_F2_FOREIGN_DIST"]).resolve()
    if not (foreign / "index.html").is_file():
        raise RuntimeError("F2_REAL_FOREIGN_FRONTEND_BUILD_REQUIRED")

    async def facts() -> dict:
        """仅返回本轮隔离身份与持久记录白名单，不能改变任何业务状态。"""
        return identity_snapshot()

    async def idle() -> HTMLResponse:
        """闲置身份保留同源原登录态，卸载重页面以遵守原资源预算。"""
        return HTMLResponse("<!doctype html><title>F2 identity idle</title><p>F2 identity idle</p>")

    app.add_api_route("/__e2e/f2/facts", facts, methods=["GET"])
    app.add_api_route("/__e2e/f2/idle", idle, methods=["GET"])
    app.mount("/__e2e/f2/foreign", StaticFiles(directory=foreign, html=True))
