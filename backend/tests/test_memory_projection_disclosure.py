"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : test_memory_projection_disclosure.py
@CallChain  : SQLite/随机MySQL → 真实Planner/清空或撤权提交 → 最终SDK修复
@Description: 锁定错误与Guidance派生草案的撤销合同，保留未撤销修复控制。
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import Session, SQLModel, create_engine

from app.api.agents import _mark_agent_used, remove_chat_agent_usage
from app.api.memories import clear_my_memories
from app.db.models import AgentProfile, AgentUsage, MemoryRecord, Tenant, User
from app.dynamic_tasks.planner_service import DynamicTaskPlanner
from app.dynamic_tasks.planning import SuccessCriterion
from app.memory.request_projection import MemoryContext, MemoryRequestEnvelope
from app.memory.service import MemoryService
from tests.test_memory_projection_consumers import SDK, llm

MARKER = "SYNTHETIC_SD6A_SELECTED_FACT_8C1E"


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def disclosure_engine(request, tmp_path):
    """SQLite用独立文件连接，MySQL沿原随机库/账号夹具和Alembic迁移。"""
    if request.param == "mysql":
        url = request.getfixturevalue("mysql_database_url")
        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine("sqlite:///" + str(tmp_path / "disclosure.sqlite"))
        SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def seed_disclosure(db):
    """种入合法完整原事实与使用关系，不伪造清空后来源许可。"""
    db.add(Tenant(id="disclosure-t", name="隔离"))
    db.add(User(id="disclosure-u", tenant_id="disclosure-t", username="isolated",
                password_hash="synthetic"))
    db.add(AgentProfile(id="disclosure-a", tenant_id="disclosure-t", name="隔离",
                        owner_user_id="disclosure-u"))
    db.commit()
    db.add(AgentUsage(tenant_id="disclosure-t", user_id="disclosure-u", agent_id="disclosure-a"))
    db.add(MemoryRecord(id="disclosure-m", tenant_id="disclosure-t", user_id="disclosure-u",
                        agent_id="disclosure-a", kind="profile", content=MARKER,
                        metadata_json={"key": "preferred_name"}))
    db.commit()
    service = MemoryService(db)
    candidates = service.projection_candidates("disclosure-t", "disclosure-u",
                                               agent_id="disclosure-a")
    assert len(candidates) == 1
    envelope = MemoryRequestEnvelope(candidates, "产生结论", validate=service.projection_validator(
        "disclosure-t", "disclosure-u", agent_id="disclosure-a"))
    return candidates, envelope


def commit_barrier(engine, barrier):
    """首SDK后另一真实Session调用正式API提交清空或撤权并重新授权。"""
    with Session(engine) as writer:
        user = writer.get(User, "disclosure-u")
        if barrier == "clear":
            assert clear_my_memories(tenant_id=user.tenant_id, agent_id="disclosure-a",
                                     current_user=user, db=writer)["deleted"] == 1
        elif barrier == "revoke":
            assert remove_chat_agent_usage("disclosure-a", tenant_id=user.tenant_id,
                                           current_user=user, db=writer)["removed"]
            _mark_agent_used(writer, user.tenant_id, user, "disclosure-a")


class CommittingSDK(SDK):
    """最终SDK只替换远端响应，所有Planner/LLM/权限预算处理保持真实。"""

    def __init__(self, outputs, engine, barrier):
        """固定远端完整草案及首发送后的已提交撤销边界。"""
        super().__init__(outputs)
        self.engine = engine
        self.barrier = barrier

    def create(self, **kwargs):
        """真实记录首次完整请求后提交屏障，下一SDK不得携旧来源派生值。"""
        result = super().create(**kwargs)
        if len(self.calls) == 1:
            commit_barrier(self.engine, self.barrier)
        return result


@pytest.mark.parametrize("barrier", ["clear", "revoke", "current"])
@pytest.mark.parametrize("invalid_kind", ["title", "extra_key", "dependency"])
def test_planner_semantic_repair_does_not_redisclose_revoked_output(
    disclosure_engine, barrier, invalid_kind,
):
    """原坏title、动态额外字段名和语义错误含旧事实时，第二真实SDK不能回显。"""
    with Session(disclosure_engine) as db:
        candidates, envelope = seed_disclosure(db)
        criterion = SuccessCriterion(id="c", type="assertion", spec={"required": True})
        valid = {"goal": "产生结论", "success_criteria": [criterion.model_dump(mode="json")],
                 "steps": [{"draft_id": "answer", "kind": "answer", "title": "结论"}]}
        invalid = json.loads(json.dumps(valid))
        if invalid_kind == "title":
            invalid["steps"][0]["title"] = [MARKER]
        elif invalid_kind == "extra_key":
            invalid[MARKER] = MARKER
        else:
            invalid["steps"][0]["depends_on"] = [MARKER]
        sdk = CommittingSDK([json.dumps(invalid), json.dumps(valid)], disclosure_engine, barrier)
        plan = DynamicTaskPlanner(llm(sdk)).create_plan(goal="产生结论",
            success_criteria=(criterion,), capabilities=(),
            memory_context=MemoryContext([candidates[0].provider_payload()], envelope))
        assert plan.steps and len(sdk.calls) == 2
        assert MARKER in str(sdk.calls[0])
        second = json.loads(sdk.calls[1]["messages"][-1]["content"])
        assert second["repair"]["failure_code"] == "DYNAMIC_PLAN_SEMANTIC_INVALID"
        if barrier != "current":
            assert not envelope.validate(candidates[0])
            assert second.get("memory_context", []) == []
            assert MARKER not in str(sdk.calls[1])
        else:
            assert envelope.validate(candidates[0])
            assert MARKER in str(sdk.calls[1])


@pytest.mark.parametrize("barrier", ["clear", "revoke", "current"])
@pytest.mark.parametrize("flow", ["identity", "requirements"])
def test_guidance_derived_slots_and_later_json_repair_share_revocation(
    disclosure_engine, barrier, flow,
):
    """实际Guidance修复及其JSON重试不能在选择已为空后回填派生草案。"""
    from app.dynamic_tasks.planner_service import (
        _guidance_source_contract,
        _planner_guidance_candidate_catalog,
        _repair_guidance_identity_fields,
        _repair_missing_guidance_requirements,
    )
    from app.memory.request_projection import MemoryRequestPayload

    with Session(disclosure_engine) as db:
        _, envelope = seed_disclosure(db)
        raw = {"guidance_requirements": [{"skill_ref": "design", "source_kind": "instructions",
            "source_ref": "instructions", "task_mapping": MARKER,
            "observable_acceptance": MARKER}]}
        outputs = [json.dumps(raw)]
        if flow == "identity":
            outputs += [MARKER + "not-json", '{"identities":[]}']
        else:
            outputs += [MARKER + "not-json", '{"guidance_requirements":[]}',
                        '{"guidance_requirements":[]}']
        sdk = CommittingSDK(outputs, disclosure_engine, barrier)
        client = llm(sdk)
        raw = client.generate_json("系统", MemoryRequestPayload(
            {"goal": "产生结论", "memory_context": []}, envelope))
        sources, _ = _guidance_source_contract([{"name": "design", "skills": [{
            "instructions": "Use a checkable acceptance criterion.", "reviewed_resources": [],
        }]}])
        catalog = _planner_guidance_candidate_catalog(sources)
        if flow == "identity":
            _repair_guidance_identity_fields(client, raw, candidate_catalog=catalog,
                                            memory_envelope=envelope)
        else:
            _repair_missing_guidance_requirements(client, raw, goal="产生结论", success_criteria=(),
                loaded_guidance=[{"name": "design", "skill_use_ids": ["use-design"]}],
                candidate_catalog=catalog, failure_message="缺少合法Guidance",
                memory_envelope=envelope)
        assert len(sdk.calls) >= 3 and MARKER in str(sdk.calls[0])
        if barrier != "current":
            assert envelope.selected == ()
            assert all(MARKER not in str(call) for call in sdk.calls[1:])
        else:
            assert any(MARKER in str(call) for call in sdk.calls[1:])
