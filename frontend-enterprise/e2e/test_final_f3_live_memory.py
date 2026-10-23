"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : test_final_f3_live_memory.py
@CallChain  : 正式pytest → 独立记忆资源/最终SDK观察/精确零预算边界
@Description: 先复现缺实现，再验证原发布/完整事实/合法重试/撤销只读，禁止真实模型网络。
"""

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import final_f3_live_memory
from final_f3_live_memory import (
    LiveMemoryError, LiveMemoryLimitError, MemoryWireObserver, _sdk_calls, install_api, memory_dispatch_hook, seed, state,
    validate_memory_quality,
)
from final_f3_preparation import digest
from final_f3_sdk_identity import bind_sdk_identity


def memory_spec() -> dict:
    """只生成固定合成身份与原人工标签，不含模型key、真实用户或数据库连接。"""
    return {
        "tenant": {"id": "tenant_memory_unit"},
        "owner": {"id": "owner_memory_unit", "username": "memory-owner"},
        "reviewer": {"id": "reviewer_memory_unit", "username": "memory-reviewer"},
        "agents": {"SOP": {"id": "business_sop"}, "DTA": {"id": "business_dta"}},
        "control_key": "synthetic-memory-observation-key",
        "memory_quality": {
            "agents": {branch: {case: {"id": f"memory_{branch}_{case}",
                                      "name": f"F3记忆 {branch} {case}"} for case in cases}
                       for branch, cases in (("SOP", ("1", "2", "3", "control", "freeze", "clear")),
                                             ("DTA", ("1", "2", "3", "control", "freeze", "clear", "revoke")))},
            "labels": {"name": "青禾", "style": "输出偏好：简洁要点；原样保留代码 a  b\n\t校验结束。",
                       "large": "口令：F3-UNSELECTED-SECRET。" + "完整长事实。" * 700},
            "skill_prefix": "f3_memory_unit",
        },
    }


@pytest.fixture
def spec():
    """每例获得独立输入字典，不能用前例修改影响合同标签。"""
    return memory_spec()


@pytest.fixture
def owned_db(tmp_path, monkeypatch, spec):
    """只建本项自有SQLite资源表，原发布生成版本，模型与执行始终不预置。"""
    from sqlmodel import Session, SQLModel, create_engine
    from app.db import models  # noqa: F401
    from app.db.models import AgentProfile, Tenant, User

    runtime = tmp_path / "f3-live-memory-unit"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(runtime))
    url = f"sqlite:///{runtime / 'journey.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = spec["tenant"]["id"]
        db.add(Tenant(id=tenant, name="独立记忆合成租户"))
        for role in ("owner", "reviewer"):
            actor = spec[role]
            db.add(User(id=actor["id"], tenant_id=tenant, username=actor["username"], role="admin",
                        password_hash="synthetic-unused-password-hash"))
        db.add(AgentProfile(id="memory_overall", tenant_id=tenant, name="记忆整体资源池",
                            is_overall=True, owner_user_id=spec["owner"]["id"]))
        db.commit()
        yield db, engine, runtime
    engine.dispose()


def test_quality_contract_has_thirteen_distinct_scopes_and_detached_complete_labels(spec):
    """原13作用域完整标签被冻结且不授予真实供应商或模型资格。"""
    quality = validate_memory_quality(spec)
    assert sum(len(cases) for cases in quality["agents"].values()) == 13
    assert len({agent["id"] for cases in quality["agents"].values() for agent in cases.values()}) == 13
    assert "a  b\n\t" in quality["labels"]["style"]
    spec["memory_quality"]["labels"]["name"] = "后改标签"
    assert quality["labels"]["name"] == "青禾"


@pytest.mark.parametrize("change", ["missing_case", "extra_case", "duplicate_agent", "business_agent",
                                     "short_large", "collapsed_style", "extra_success"])
def test_quality_contract_refuses_missing_control_and_label_or_scope_drift(spec, change):
    """缺原三例/屏障或错误作用域/完整标签不能按新输出放宽验收。"""
    quality = spec["memory_quality"]
    if change == "missing_case":
        del quality["agents"]["DTA"]["revoke"]
    elif change == "extra_case":
        quality["agents"]["SOP"]["extra"] = {"id": "extra", "name": "额外"}
    elif change == "duplicate_agent":
        quality["agents"]["DTA"]["1"]["id"] = quality["agents"]["SOP"]["1"]["id"]
    elif change == "business_agent":
        quality["agents"]["DTA"]["1"]["id"] = spec["agents"]["DTA"]["id"]
    elif change == "short_large":
        quality["labels"]["large"] = "口令：短事实。"
    elif change == "collapsed_style":
        quality["labels"]["style"] = quality["labels"]["style"].replace("a  b", "a b")
    else:
        quality["passed"] = True
    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_SPEC_INVALID"):
        validate_memory_quality(spec)


def test_seed_uses_original_publisher_and_keeps_runtime_empty(spec, owned_db):
    """13员工/39完整事实及6 SOP分支原发布，不预填执行、Operation或记忆Job成功。"""
    from sqlmodel import select
    from app.db.models import (
        AgentResourceBinding, AgentSkillBranchVersion, MemoryCaptureJob, MemoryRecord,
        SkillVersion, SopInstance, SopOperation,
    )

    db, _, _ = owned_db
    result = seed(db, spec)
    assert len(result["agents"]["SOP"]) == 6 and len(result["agents"]["DTA"]) == 7
    for branch, cases in result["agents"].items():
        for case, item in cases.items():
            actor = spec["reviewer" if case in {"freeze", "revoke"} else "owner"]["id"]
            assert item["gallery_label"] == item["name"] + " @" + actor
    memories = db.exec(select(MemoryRecord)).all()
    assert len(memories) == 39
    assert all(row.agent_scope_key == row.agent_id and row.user_id == spec["owner"]["id"]
               and row.source_sequence == 0 and row.capture_job_id is None for row in memories)
    assert sum(len(row.content) > 4000 for row in memories) == 13
    versions = db.exec(select(SkillVersion)).all()
    branches = db.exec(select(AgentSkillBranchVersion)).all()
    assert len(versions) == len(branches) == 6
    assert all(row.status == "published" and row.compiled_definition_checksum for row in versions)
    skill_by_id = {row.skill_id: row for row in versions}
    for case in ("freeze", "clear"):
        nodes = skill_by_id[spec["memory_quality"]["skill_prefix"] + "_" + case].content_json["nodes"]
        confirm = next(node for node in nodes if node["node_id"] == "confirm")
        assert confirm["expected_user_info"] == ["confirmed"]
        assert confirm["metadata"]["confirmation_policy"]["slot_name"] == "confirmed"
    for case in ("1", "2", "3", "control"):
        nodes = skill_by_id[spec["memory_quality"]["skill_prefix"] + "_" + case].content_json["nodes"]
        assert all(node["node_id"] != "confirm" for node in nodes)
    assert not db.exec(select(SopInstance)).all() and not db.exec(select(SopOperation)).all()
    assert not db.exec(select(MemoryCaptureJob)).all()
    assert not db.exec(select(AgentResourceBinding).where(
        AgentResourceBinding.resource_type.in_(("knowledge_base", "tool")))).all()
    assert result["real_vendor_qualified"] is False and result["real_model_quality_passed"] is False


def test_seed_freezes_reply_style_value_as_colon_body_not_memory_label(spec, owned_db):
    """发布期必须明确要求模型取冒号后的格式正文，不能把“输出偏好”标签当槽位值。"""
    from sqlmodel import select
    from app.db.models import SkillVersion

    db, _, _ = owned_db
    seed(db, spec)
    version = db.exec(select(SkillVersion).where(
        SkillVersion.skill_id == spec["memory_quality"]["skill_prefix"] + "_1"
    )).one()
    description = str(version.content_json["description"])
    reply_style = version.content_json["condition_schemas"]["slots"]["properties"]["reply_style"]
    collect = next(node for node in version.content_json["nodes"] if node["node_id"] == "collect")
    style_aliases = collect["metadata"]["value_aliases"]["reply_style"]
    assert "值是冒号后的正文关键词" in description
    assert "不能写字段标签“输出偏好”" in description
    assert "简洁要点" not in description
    assert "值是冒号后的正文关键词" in reply_style["description"]
    assert "不能写字段标签“输出偏好”" in reply_style["description"]
    assert "简洁要点" not in reply_style["description"]
    assert style_aliases["简洁要点；原样保留代码 a  b"] == "简洁要点"


def test_seed_rejects_replacing_existing_baseline(spec, owned_db):
    """重启或再次准备不能重种已清空/纠正的记忆基线。"""
    db, _, _ = owned_db
    seed(db, spec)
    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_SEED_EXISTS"):
        seed(db, spec)


@pytest.mark.parametrize("agent_id", ["foreign", "business_dta", "memory_DTA_unregistered"])
def test_state_refuses_unregistered_employee_even_with_valid_control_spec(spec, owned_db, agent_id):
    """私有观察也只读精确13白名单，不能读取其他员工或业务会话。"""
    db, _, _ = owned_db
    seed(db, spec)
    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_SCOPE_FORBIDDEN"):
        state(db, spec, agent_id)


def test_state_reads_frozen_selected_and_current_revocation_without_refilling(spec, owned_db):
    """原selected持久身份保持，清空代数失效后validator为false而不是新查询补旧事实。"""
    from sqlmodel import select
    from app.db.models import ChatSession, MemoryRecord, SopInstance
    from app.memory.request_projection import MemoryRequestEnvelope
    from app.memory.service import MemoryService
    from app.memory.capture import advance_memory_clear_generation

    db, _, _ = owned_db
    seed(db, spec)
    agent = spec["memory_quality"]["agents"]["DTA"]["clear"]["id"]
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    candidates = MemoryService(db).projection_candidates(tenant, owner, agent_id=agent)
    selected = tuple(item for item in candidates if item.kind in {"profile", "preference"})
    envelope = MemoryRequestEnvelope(candidates, "原合成等待输入", selected=selected, source_input_id="source")
    db.add(ChatSession(id="memory_session", tenant_id=tenant, user_id=owner, username="memory-owner",
                       agent_id=agent, status="active"))
    db.add(SopInstance(id="memory_execution", tenant_id=tenant, agent_id=agent,
        initiator_user_id=owner, session_id="memory_session", kind="dynamic_task", status="waiting",
        active_slot_key="memory_session::dynamic_task", goal_snapshot_json={},
        current_plan_revision_id="memory_plan", capability_snapshot_json={},
        current_plan_checksum="a" * 64, context_json={"memory_projection": envelope.snapshot()}))
    db.commit()
    before = state(db, spec, agent)
    assert len(before["executions"][0]["selected"]) == 2
    assert all(item["currently_allowed"] for item in before["executions"][0]["selected"])
    advance_memory_clear_generation(db, tenant_id=tenant, user_id=owner, agent_id=agent)
    for row in db.exec(select(MemoryRecord).where(MemoryRecord.agent_id == agent)).all():
        db.delete(row)
    db.commit()
    after = state(db, spec, agent)
    assert after["executions"][0]["plan_checksum"] == "a" * 64
    assert [item["id"] for item in before["executions"][0]["selected"]] == [
        item["id"] for item in after["executions"][0]["selected"]]
    assert all(not item["currently_allowed"] for item in after["executions"][0]["selected"])
    assert after["records"] == []


@pytest.mark.parametrize("key,status", [(None, 403), ("wrong-synthetic-key", 403),
                                       ("synthetic-memory-observation-key", 200)])
def test_state_api_is_control_authenticated_and_readonly(spec, owned_db, key, status):
    """未认证读取拒绝，合法GET只读且无修改/恢复路由。"""
    from sqlmodel import Session

    db, engine, runtime = owned_db
    seed(db, spec)
    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    routes = [route for route in application.routes if route.path.startswith("/__e2e/f3/memory/")]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    agent = spec["memory_quality"]["agents"]["SOP"]["1"]["id"]
    response = TestClient(application).get("/__e2e/f3/memory/" + agent,
        headers={"X-F3-Control-Key": key} if key else {})
    assert response.status_code == status
    if status == 200:
        assert len(response.json()["records"]) == 3
        assert "青禾" not in response.text and spec["control_key"] not in response.text


def test_memory_state_rejects_cross_employee_session_and_hides_other_scope_sdk_receipt(
        spec, owned_db):
    """同租户本人跨员工传session仍拒绝，另一员工的SDK调用不进入当前响应。"""
    from sqlmodel import Session
    from app.db.models import ChatSession

    db, engine, runtime = owned_db
    seed(db, spec)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent_sop = spec["memory_quality"]["agents"]["SOP"]["1"]["id"]
    agent_dta = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    session_sop, session_dta = "session_scope_sop", "session_scope_dta"
    for session_id, agent_id in ((session_sop, agent_sop), (session_dta, agent_dta)):
        db.add(ChatSession(id=session_id, tenant_id=tenant, user_id=owner,
                           username=spec["owner"]["username"], agent_id=agent_id,
                           status="active"))
    db.commit()

    identity = {key: digest(value) for key, value in {
        "tenant_id": tenant, "user_id": owner, "agent_id": agent_dta,
        "session_id": session_dta,
    }.items()}
    request_sha = digest("cross-scope synthetic model request")
    receipt = {"pid": 424242, "call_id": "f3_sdk_" + "a" * 32,
               "identity_sha256": identity, "request_sha256": request_sha,
               "observation_valid": True, "status": "completed",
               "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
    wire = {"pid": 424242, "identity_sha256": identity, "sdk_request_sha256": request_sha,
            "phase_sha256": digest("dynamic_task.answer"), "actual_provider_call": True,
            "selected": []}
    for filename, row in (("sdk-observations-424242.safe.jsonl", receipt),
                          ("memory-wire-424242.safe.jsonl", wire)):
        path = runtime / filename
        path.write_text(json.dumps(row) + "\n")
        path.chmod(0o600)

    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    client = TestClient(application)
    headers = {"X-F3-Control-Key": spec["control_key"]}
    crossed = client.get(f"/__e2e/f3/memory/{agent_sop}",
                         params={"session_id": session_dta}, headers=headers)
    assert crossed.status_code == 403

    sop_state = client.get(f"/__e2e/f3/memory/{agent_sop}", headers=headers)
    dta_state = client.get(f"/__e2e/f3/memory/{agent_dta}", headers=headers)
    assert sop_state.status_code == dta_state.status_code == 200
    assert sop_state.json()["sessions"] == [{
        "id": session_sop, "slots": {key: None for key in ("user_name", "reply_style", "omitted_code")},
        "router_audit": [],
    }]
    assert sop_state.json()["sdk_calls"] == []
    assert len(dta_state.json()["sdk_calls"]) == 1
    assert dta_state.json()["sdk_calls"][0]["session_id"] == session_dta
    assert session_dta not in sop_state.text
    assert agent_dta not in sop_state.text


def test_memory_state_rejects_second_employee_session_and_excludes_its_sdk_log(spec, owned_db):
    """员工A不能读取员工B本人会话，且B身份的同员工SDK旁证不得混入A读回。"""
    from sqlmodel import Session
    from app.db.models import ChatSession

    db, engine, runtime = owned_db
    seed(db, spec)
    tenant = spec["tenant"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    session_a, session_b = "session_employee_a", "session_employee_b"
    db.add(ChatSession(id=session_a, tenant_id=tenant, user_id=spec["owner"]["id"],
        username=spec["owner"]["username"], agent_id=agent, status="active"))
    db.add(ChatSession(id=session_b, tenant_id=tenant, user_id=spec["reviewer"]["id"],
        username=spec["reviewer"]["username"], agent_id=agent, status="active"))
    db.commit()

    other_identity = {key: digest(value) for key, value in {
        "tenant_id": tenant, "user_id": spec["reviewer"]["id"],
        "agent_id": agent, "session_id": session_b,
    }.items()}
    request_sha = digest("employee B isolated synthetic SDK request")
    receipt = {"pid": 424243, "call_id": "f3_sdk_" + "b" * 32,
        "identity_sha256": other_identity, "request_sha256": request_sha,
        "observation_valid": True, "status": "completed", "usage": {"prompt_tokens": 9}}
    wire = {"pid": 424243, "identity_sha256": other_identity,
        "sdk_request_sha256": request_sha, "phase_sha256": digest("dynamic_task.answer"),
        "actual_provider_call": True, "selected": []}
    for filename, row in (("sdk-observations-424243.safe.jsonl", receipt),
                          ("memory-wire-424243.safe.jsonl", wire)):
        path = runtime / filename
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        path.chmod(0o600)

    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    client = TestClient(application)
    headers = {"X-F3-Control-Key": spec["control_key"]}
    crossed = client.get(f"/__e2e/f3/memory/{agent}", params={"session_id": session_b},
                         headers=headers)
    assert crossed.status_code == 403
    employee_a = client.get(f"/__e2e/f3/memory/{agent}",
        params={"session_id": session_a}, headers=headers)
    assert employee_a.status_code == 200
    assert [row["id"] for row in employee_a.json()["sessions"]] == [session_a]
    assert employee_a.json()["sdk_calls"] == []
    assert session_b not in employee_a.text
    assert spec["reviewer"]["id"] not in employee_a.text


def test_memory_state_rejects_excessive_rows_before_returning_a_snapshot(spec, owned_db):
    """私有state观察超出单表行数合同必须失败关闭，不返回无界列表。"""
    from sqlmodel import select
    from app.db.models import MemoryRecord

    db, _, _ = owned_db
    seed(db, spec)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["freeze"]["id"]
    for index in range(254):
        db.add(MemoryRecord(id=f"memory_extra_{index}", tenant_id=tenant, user_id=owner,
            username=spec["owner"]["username"], agent_id=agent, agent_scope_key=agent,
            kind="fact", content="synthetic bounded row", metadata_json={}))
    db.commit()
    assert len(db.exec(select(MemoryRecord).where(MemoryRecord.tenant_id == tenant,
        MemoryRecord.user_id == owner, MemoryRecord.agent_id == agent)).all()) == 257

    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_STATE_ROW_LIMIT"):
        state(db, spec, agent)


def test_memory_state_api_refuses_over_row_limit_projection(spec, owned_db, monkeypatch):
    """GET必须在JSON正文生成前拒绝超行数的私有诊断投影。"""
    from sqlmodel import Session

    _, engine, runtime = owned_db
    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    agent = spec["memory_quality"]["agents"]["DTA"]["freeze"]["id"]
    client = TestClient(application)
    headers = {"X-F3-Control-Key": spec["control_key"]}
    projection = {key: [] for key in final_f3_live_memory.MEMORY_STATE_LIST_KEYS}

    monkeypatch.setattr(final_f3_live_memory, "state", lambda *_args, **_kwargs: {
        **projection, "records": [{} for _ in range(257)]})
    too_many_rows = client.get("/__e2e/f3/memory/" + agent, headers=headers)
    assert too_many_rows.status_code == 413
    assert too_many_rows.json()["detail"] == "F3_LIVE_MEMORY_STATE_LIMIT"


def test_state_serialization_rejects_oversized_string_before_json_encoder(spec, monkeypatch):
    """单个超大JSON字符串须在UTF-8编码和完整JSON迭代器之前被限额拒绝。"""
    import json

    projection = {key: [] for key in final_f3_live_memory.MEMORY_STATE_LIST_KEYS}
    projection["oversized"] = "x" * (final_f3_live_memory.MAX_MEMORY_STATE_RESPONSE_BYTES + 1)
    encoder_called = False
    original = json.JSONEncoder.iterencode

    def observe_encoder(encoder, value):
        """标记完整编码器是否在字节预检拒绝前启动。"""
        nonlocal encoder_called
        encoder_called = True
        return original(encoder, value)

    monkeypatch.setattr(json.JSONEncoder, "iterencode", observe_encoder)
    with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_STATE_RESPONSE_LIMIT"):
        final_f3_live_memory._state_response_bytes(projection)
    assert not encoder_called


def test_state_serialization_preflight_matches_utf8_json_wire_bytes():
    """预遍历长度对中文、引号、反斜线、控制符及数字与标准JSON字节完全一致。"""
    projection = {key: [] for key in final_f3_live_memory.MEMORY_STATE_LIST_KEYS}
    projection["text"] = '青禾 "简洁" \\ 保留\n\t' + chr(1)
    projection["values"] = [None, True, False, 0, -17, 1.25, -0.0]
    expected = json.dumps(projection, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")
    assert final_f3_live_memory._state_response_bytes(projection) == expected


def test_state_db_preflights_aggregate_memory_content_before_selecting_text(spec, owned_db):
    """数据库长字段须先用长度聚合超限拒绝，不能由ORM拉取完整正文再hash。"""
    from sqlalchemy import event
    from app.db.models import MemoryRecord

    db, engine, _ = owned_db
    seed(db, spec)
    agent = spec["memory_quality"]["agents"]["DTA"]["freeze"]["id"]
    source_limit = 1024 * 1024
    db.add(MemoryRecord(id="memory_oversized_state", tenant_id=spec["tenant"]["id"],
        user_id=spec["owner"]["id"], username=spec["owner"]["username"], agent_id=agent,
        agent_scope_key=agent, kind="fact", content="x" *
        (source_limit + 1), metadata_json={}))
    db.commit()
    statements = []
    def observe_sql(_connection, _cursor, sql, *_args):
        """只记录本测试连接的SQL形状，避免保存任何参数或记忆正文。"""
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_SOURCE_LIMIT"):
            state(db, spec, agent)
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    memory_selects = [sql for sql in statements if "FROM memories" in sql]
    assert memory_selects
    assert not any("SELECT memories.id, memories.tenant_id" in sql for sql in memory_selects)


@pytest.mark.parametrize("projection_field", [
    "memory_record_kind", "plan_revision_checksum", "execution_signal_causation_id",
])
def test_state_db_preflights_every_projected_scalar_before_materializing_it(
    spec, owned_db, projection_field,
):
    """SQLite忽略VARCHAR长度时，诊断投影的任一大标量也须先由SQL总预算拒绝。"""
    from sqlalchemy import event, text
    from sqlmodel import select
    from app.db.models import (
        ChatSession, ExecutionPlanRevision, ExecutionSignal, MemoryRecord, SopInstance,
    )

    db, engine, _ = owned_db
    seed(db, spec)
    agent = spec["memory_quality"]["agents"]["DTA"]["freeze"]["id"]
    source_limit = final_f3_live_memory.MAX_MEMORY_STATE_SOURCE_BYTES
    oversized = "x" * (source_limit + 1)
    projection_sql = ""
    if projection_field == "memory_record_kind":
        record = db.exec(select(MemoryRecord).where(
            MemoryRecord.tenant_id == spec["tenant"]["id"],
            MemoryRecord.agent_id == agent, MemoryRecord.kind == "fact")).first()
        assert record is not None
        db.execute(text("UPDATE memories SET kind = :value WHERE id = :row_id"),
            {"value": oversized, "row_id": record.id})
        projection_sql = "select memories.id, memories.kind"
    else:
        tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
        db.add(ChatSession(id="memory_budget_session", tenant_id=tenant, user_id=owner,
            username=spec["owner"]["username"], agent_id=agent))
        db.add(SopInstance(id="memory_budget_execution", tenant_id=tenant,
            session_id="memory_budget_session", agent_id=agent, initiator_user_id=owner,
            kind="dynamic_task", status="waiting", active_slot_key="memory_budget_slot",
            goal_snapshot_json={}, current_plan_revision_id="memory_budget_plan",
            current_plan_checksum="a" * 64, capability_snapshot_json={}))
        if projection_field == "plan_revision_checksum":
            db.add(ExecutionPlanRevision(id="memory_budget_plan", tenant_id=tenant,
                execution_id="memory_budget_execution", revision_number=1,
                plan_json={}, checksum="b" * 64, capability_snapshot_json={},
                capability_checksum="c" * 64))
            db.commit()
            db.execute(text("UPDATE execution_plan_revisions SET checksum = :value WHERE id = :row_id"),
                {"value": oversized, "row_id": "memory_budget_plan"})
            projection_sql = "select execution_plan_revisions.id"
        else:
            db.add(ExecutionSignal(id="memory_budget_signal", tenant_id=tenant,
                execution_id="memory_budget_execution", signal_type="timer",
                dedupe_key="memory_budget_dedupe", causation_type="execution",
                causation_id=oversized, payload_checksum="d" * 64))
            projection_sql = "select execution_signals.id"
    db.commit()
    statements = []

    def observe_sql(_connection, _cursor, sql, *_args):
        """记录查询形状以证明超限发生在投影字段SELECT之前。"""
        statements.append(" ".join(sql.lower().replace('"', "").split()))

    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_SOURCE_LIMIT"):
            state(db, spec, agent)
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert not any(statement.startswith(projection_sql) for statement in statements)


def test_memory_state_jsonl_refuses_row_overflow_before_splitlines_materializes_array(owned_db, monkeypatch):
    """JSONL行数在逐行读取时超限，不能先对32MiB缓冲创建完整splitlines列表。"""
    _, _, runtime = owned_db
    path = runtime / "memory-wire-123.safe.jsonl"
    path.write_bytes(b"{}\n" * (final_f3_live_memory.MAX_MEMORY_STATE_JSONL_ROWS + 1))
    path.chmod(0o600)
    splitlines_called = False

    class ObservedBytearray(bytearray):
        """记录旧全量分割路径是否被调用，输入仍只包含合成空对象。"""

        def splitlines(self):
            """标记整批行对象数组分配。"""
            nonlocal splitlines_called
            splitlines_called = True
            return super().splitlines()

    monkeypatch.setattr(final_f3_live_memory, "bytearray", ObservedBytearray, raising=False)
    with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_STATE_ROW_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)
    assert not splitlines_called


def test_memory_state_jsonl_rejects_oversized_line_before_json_decode(owned_db, monkeypatch):
    """单个JSONL正文行超出预算时，必须在json.loads创建对象图前拒绝。"""
    _, _, runtime = owned_db
    path = runtime / "memory-wire-123.safe.jsonl"
    path.write_bytes(b'{"payload":"' + b"x" * (256 * 1024) + b'"}\n')
    path.chmod(0o600)
    json_loads_called = False
    original_loads = json.loads

    def observe_json_loads(value):
        """记录正文解析器是否在单行字节检查前启动。"""
        nonlocal json_loads_called
        json_loads_called = True
        return original_loads(value)

    monkeypatch.setattr(final_f3_live_memory.json, "loads", observe_json_loads)
    with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_STATE_JSONL_ROW_BYTE_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)
    assert not json_loads_called


def test_memory_state_jsonl_rejects_complex_row_before_json_decode(owned_db, monkeypatch):
    """单行内的数组节点和嵌套深度预算在JSON解析器分配对象图前生效。"""
    _, _, runtime = owned_db
    path = runtime / "memory-wire-123.safe.jsonl"
    path.write_bytes(b"[" + b"0," * 30_000 + b"0]\n")
    path.chmod(0o600)
    json_loads_called = False
    original_loads = json.loads

    def observe_json_loads(value):
        """记录复杂对象解析器是否在结构预检拒绝前启动。"""
        nonlocal json_loads_called
        json_loads_called = True
        return original_loads(value)

    monkeypatch.setattr(final_f3_live_memory.json, "loads", observe_json_loads)
    with pytest.raises(LiveMemoryError) as rejected:
        final_f3_live_memory._read_bounded_jsonl(runtime)
    assert isinstance(rejected.value, LiveMemoryLimitError)
    assert str(rejected.value) == "F3_LIVE_MEMORY_STATE_JSONL_COMPLEXITY_LIMIT"
    assert not json_loads_called


def test_memory_state_jsonl_accepts_measured_full20_observation_complexity(owned_db):
    """20案观测日志实测54732节点，12个PID分片仍应在有界预算内完成只读读回。"""
    _, _, runtime = owned_db
    expected = {"sdk-observations": 1_956, "memory-wire": 2_600}
    for prefix, values in expected.items():
        for worker in range(6):
            path = runtime / f"{prefix}-{worker}.safe.jsonl"
            path.write_text(json.dumps({"events": [0] * values}, separators=(",", ":")) + "\n")
            path.chmod(0o600)

    sdk_rows, memory_rows = final_f3_live_memory._read_bounded_jsonl(runtime)

    assert len(sdk_rows) == len(memory_rows) == 6
    assert all(len(row["events"]) == expected["sdk-observations"] for row in sdk_rows)
    assert all(len(row["events"]) == expected["memory-wire"] for row in memory_rows)


def test_memory_state_jsonl_accepts_measured_current_full_matrix_complexity(owned_db):
    """当前23案矩阵实测约69324节点，新增分片不能被过时的65K聚合上限误拒。"""
    _, _, runtime = owned_db
    values = 2_200
    for prefix in ("sdk-observations", "memory-wire"):
        for worker in range(8):
            path = runtime / f"{prefix}-{worker}.safe.jsonl"
            path.write_text(json.dumps({"events": [0] * values}, separators=(",", ":")) + "\n")
            path.chmod(0o600)

    sdk_rows, memory_rows = final_f3_live_memory._read_bounded_jsonl(runtime)

    assert len(sdk_rows) == len(memory_rows) == 8
    assert all(len(row["events"]) == values for row in sdk_rows)
    assert all(len(row["events"]) == values for row in memory_rows)


def test_memory_state_jsonl_aggregate_node_limit_still_fails_before_overflow_row_decode(
    owned_db, monkeypatch,
):
    """跨文件超过aggregate节点预算时仍须在解析超限行前关闭。"""
    _, _, runtime = owned_db
    values_per_row = final_f3_live_memory.MAX_MEMORY_STATE_JSONL_NODES // 8 + 1
    for prefix in ("sdk-observations", "memory-wire"):
        for worker in range(2):
            path = runtime / f"{prefix}-limit-{worker}.safe.jsonl"
            path.write_text(json.dumps({"events": [0] * values_per_row}, separators=(",", ":")) + "\n")
            path.chmod(0o600)

    json_loads_calls = 0
    original_loads = json.loads

    def observe_json_loads(value):
        """记录第一行已解析、跨文件超限的第二行未交给JSON解析器。"""
        nonlocal json_loads_calls
        json_loads_calls += 1
        return original_loads(value)

    monkeypatch.setattr(final_f3_live_memory.json, "loads", observe_json_loads)
    with pytest.raises(LiveMemoryLimitError,
                       match="F3_LIVE_MEMORY_STATE_JSONL_COMPLEXITY_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)
    assert json_loads_calls == 3


def test_memory_state_private_api_reads_all_scopes_at_measured_full20_complexity(spec, owned_db):
    """实测full20观测量下13个私有状态GET均返回完整记录且仍受2MiB响应上限约束。"""
    from sqlmodel import Session

    db, engine, runtime = owned_db
    seeded = seed(db, spec)
    for prefix, values in {"sdk-observations": 1_956, "memory-wire": 2_600}.items():
        for worker in range(6):
            path = runtime / f"{prefix}-{worker}.safe.jsonl"
            path.write_text(json.dumps({"events": [0] * values}, separators=(",", ":")) + "\n")
            path.chmod(0o600)

    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    client = TestClient(application)
    for branch, cases in seeded["agents"].items():
        for case, employee in cases.items():
            response = client.get("/__e2e/f3/memory/" + employee["id"],
                headers={"X-F3-Control-Key": spec["control_key"]})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert len(response.content) <= final_f3_live_memory.MAX_MEMORY_STATE_RESPONSE_BYTES
            assert response.json()["agent_id"] == employee["id"]
            assert len(response.json()["records"]) == 3


def test_memory_state_jsonl_rejects_deep_row_before_json_decode(owned_db, monkeypatch):
    """超过固定嵌套深度的JSONL行必须在递归解析器创建对象前失败。"""
    _, _, runtime = owned_db
    depth = final_f3_live_memory.MAX_MEMORY_STATE_JSONL_DEPTH + 1
    path = runtime / "memory-wire-123.safe.jsonl"
    path.write_bytes(b"[" * depth + b"0" + b"]" * depth + b"\n")
    path.chmod(0o600)
    json_loads_called = False
    original_loads = json.loads

    def observe_json_loads(value):
        """记录解析器是否在深度预检拒绝前启动。"""
        nonlocal json_loads_called
        json_loads_called = True
        return original_loads(value)

    monkeypatch.setattr(final_f3_live_memory.json, "loads", observe_json_loads)
    with pytest.raises(LiveMemoryLimitError,
                       match="F3_LIVE_MEMORY_STATE_JSONL_COMPLEXITY_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)
    assert not json_loads_called



def test_memory_state_api_refuses_oversized_projection(spec, owned_db, monkeypatch):
    """GET必须在JSON正文生成前拒绝超字节上限的私有诊断投影。"""
    from sqlmodel import Session

    _, engine, runtime = owned_db
    application = FastAPI()
    install_api(application, runtime, spec, session_factory=lambda: Session(engine))
    agent = spec["memory_quality"]["agents"]["DTA"]["freeze"]["id"]
    projection = {key: [] for key in final_f3_live_memory.MEMORY_STATE_LIST_KEYS}
    monkeypatch.setattr(final_f3_live_memory, "state", lambda *_args, **_kwargs: {
        **projection, "large_projection": "x" * (2 * 1024 * 1024)})
    response = TestClient(application).get("/__e2e/f3/memory/" + agent,
        headers={"X-F3-Control-Key": spec["control_key"]})
    assert response.status_code == 413
    assert response.json()["detail"] == "F3_LIVE_MEMORY_STATE_LIMIT"


def test_memory_state_jsonl_reader_rejects_excessive_rows(tmp_path):
    """状态接口在解析JSONL前对SDK和wire合计行数执行硬上限。"""
    runtime = tmp_path / "private"
    runtime.mkdir(mode=0o700)
    path = runtime / "memory-wire-12345.safe.jsonl"
    path.write_bytes(b"{}\n" * (final_f3_live_memory.MAX_MEMORY_STATE_JSONL_ROWS + 1))
    path.chmod(0o600)

    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_STATE_ROW_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)


def test_memory_state_jsonl_reader_rejects_excessive_bytes_before_read(tmp_path):
    """状态接口检查JSONL总字节大小后才读取，超限稀疏文件不物化。"""
    runtime = tmp_path / "private"
    runtime.mkdir(mode=0o700)
    path = runtime / "memory-wire-12345.safe.jsonl"
    with path.open("wb") as stream:
        stream.truncate(final_f3_live_memory.MAX_MEMORY_STATE_JSONL_BYTES + 1)
    path.chmod(0o600)

    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_STATE_JSONL_BYTE_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)


def test_memory_state_jsonl_reader_rejects_excessive_file_count(tmp_path):
    """状态接口拒绝超过64个原SDK旁证文件的运行根。"""
    runtime = tmp_path / "private"
    runtime.mkdir(mode=0o700)
    for index in range(final_f3_live_memory.MAX_MEMORY_STATE_JSONL_FILES + 1):
        path = runtime / f"memory-wire-{index}.safe.jsonl"
        path.touch(mode=0o600)

    with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_STATE_FILE_LIMIT"):
        final_f3_live_memory._read_bounded_jsonl(runtime)


def payload_for(spec, branch="DTA", case="1", *, scope=None, source="source"):
    """原MemoryRequestPayload携带完整候选，正文不是预期模型输出。"""
    from app.memory.projection import MemoryCandidate, MemoryProjectionPolicy
    from app.memory.request_projection import MemoryRequestEnvelope, MemoryRequestPayload

    agent = spec["memory_quality"]["agents"][branch][case]["id"]
    selected_scope = scope or (spec["tenant"]["id"], spec["owner"]["id"], agent)
    labels = spec["memory_quality"]["labels"]
    candidates = tuple(MemoryCandidate(id=f"{agent}_{suffix}", kind=kind, key=key,
        content=labels[suffix], scope=selected_scope) for suffix, kind, key in
        (("name", "profile", "preferred_name"), ("style", "preference", "reply_style")))
    envelope = MemoryRequestEnvelope(candidates, "只沿本次实际长期记忆姓名和输出偏好回答。",
        policy=MemoryProjectionPolicy(max_items=20, max_fact_characters=4000), source_input_id=source)
    return MemoryRequestPayload({"memory_context": [item.provider_payload() for item in candidates],
                                 "user_message": envelope.current_input}, envelope)


def original_renderer_module():
    """包装真实原最终渲染函数的独立模块引用，不覆盖生产模块全局符号。"""
    from app.llm.client import _memory_request_messages

    return SimpleNamespace(_memory_request_messages=_memory_request_messages)


def render(module, payload):
    """让原完整事实预算生成实际SDK messages，测试不连接模型。"""
    client = SimpleNamespace(model="synthetic-memory-model", verified_context_window=128000,
                             memory_projection_model_identity={"id": "synthetic-model-id"})
    return module._memory_request_messages(client, "合成原字段提取合同。", payload, 8192)


def test_zero_hook_changes_only_exact_declared_control_and_restores_original(spec, tmp_path, monkeypatch):
    """两个精确control预算归零，普通同租户员工保持原策略，离开恢复原方法。"""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    module = original_renderer_module()
    original = module._memory_request_messages
    observer = MemoryWireObserver(tmp_path)
    with memory_dispatch_hook(module, spec, observer):
        for branch in ("SOP", "DTA"):
            payload = payload_for(spec, branch, "control")
            render(module, payload)
            assert payload.envelope.policy.configured_memory_limit == 0
            assert payload.envelope.selected == ()
        normal = payload_for(spec)
        render(module, normal)
        assert normal.envelope.policy.configured_memory_limit == 2048
        assert len(normal.envelope.selected) == 2
    assert module._memory_request_messages is original


@pytest.mark.parametrize("scope", [("foreign", "owner_memory_unit", "memory_DTA_control"),
                                    ("tenant_memory_unit", "other", "memory_DTA_control"),
                                    ("tenant_memory_unit", "owner_memory_unit", "unregistered_control")])
def test_zero_hook_does_not_match_suffix_or_foreign_scope(spec, tmp_path, monkeypatch, scope):
    """外租户/其他本人/未登记同后缀不能命中精确零预算规则。"""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    module, observer = original_renderer_module(), MemoryWireObserver(tmp_path)
    with memory_dispatch_hook(module, spec, observer):
        payload = payload_for(spec, case="control", scope=scope)
        render(module, payload)
        assert payload.envelope.policy.configured_memory_limit == 2048
        assert len(payload.envelope.selected) == 2


def test_zero_hook_refuses_mixed_candidates_and_missing_current_source(spec, tmp_path, monkeypatch):
    """混合scope及未绑定来源不能借一个control候选进入测试策略。"""
    from dataclasses import replace

    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    module, observer = original_renderer_module(), MemoryWireObserver(tmp_path)
    with memory_dispatch_hook(module, spec, observer):
        payload = payload_for(spec, case="control")
        payload.envelope.candidates = (payload.envelope.candidates[0],
            replace(payload.envelope.candidates[1], scope=("foreign", "other", "other")))
        with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_CONTROL_IDENTITY_INVALID"):
            render(module, payload)
        with pytest.raises(LiveMemoryError, match="F3_LIVE_MEMORY_CONTROL_IDENTITY_INVALID"):
            render(module, payload_for(spec, case="control", source=""))


class OriginalClient:
    """只在正式观察器控制中透传合成结果/异常，不是浏览器模型替身。"""

    def __init__(self, result, calls, error=None):
        """超时clone共享原记录列表，原SDK对象不被替换。"""
        self.result, self.calls, self.error = result, calls, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, *args, **kwargs):
        """记录原实参并返回同一原对象或抛同一原异常。"""
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    def with_options(self, **_options):
        """原选项副本继续保留原结果、异常和记录。"""
        return OriginalClient(self.result, self.calls, self.error)


def read_observations(runtime):
    """只读取本项自有白名单旁证，不访问旧档或模型配置。"""
    paths = list(runtime.glob("memory-wire-*.safe.jsonl"))
    assert len(paths) == 1
    return [json.loads(line) for line in paths[0].read_text().splitlines()]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_sdk_state_uses_matched_original_receipt_status_and_usage(spec, tmp_path, status):
    """最终状态和计费只能取同PID/身份/请求的原receipt，不沿用wire较早成功值。"""
    tmp_path.chmod(0o700)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    identity = {key: digest(value) for key, value in {
        "tenant_id": tenant, "user_id": owner, "agent_id": agent,
        "session_id": "session", "source_input_id": "source", "instance_id": "execution",
    }.items()}
    usage = {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38}
    wire = {"pid": 12345, "identity_sha256": identity, "sdk_request_sha256": digest("request"),
            "phase_sha256": digest("dynamic_task.answer"), "status": "completed",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "actual_provider_call": True, "selected": []}
    receipt = {"pid": 12345, "identity_sha256": identity, "request_sha256": digest("request"),
               "observation_valid": True, "status": status, "usage": usage}
    for name, row in (("memory-wire-12345.safe.jsonl", wire),
                      ("sdk-observations-12345.safe.jsonl", receipt)):
        path = tmp_path / name
        path.write_text(json.dumps(row) + "\n")
        path.chmod(0o600)
    rows = _sdk_calls(tmp_path, tenant, owner, agent, ["session"], ["execution"],
                      [SimpleNamespace(id="source", role="user")])
    assert len(rows) == 1 and rows[0]["original_sdk_receipt_found"] is True
    assert rows[0]["status"] == status
    assert rows[0]["usage"] == usage


def test_actual_sdk_full_fact_counts_usage_and_retry_clone_keep_original(spec, tmp_path, monkeypatch):
    """原最终SDK完整换行/双空格/usage及合法clone两次保存，原对象返回不变。"""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    module, observer = original_renderer_module(), MemoryWireObserver(tmp_path)
    result = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=17, completion_tokens=5, total_tokens=22))
    calls = []
    client = observer.observe(OriginalClient(result, calls))
    payload = payload_for(spec)
    identity = {"kind": "dynamic_task", "tenant_id": spec["tenant"]["id"], "user_id": spec["owner"]["id"],
                "agent_id": spec["memory_quality"]["agents"]["DTA"]["1"]["id"],
                "source_input_id": "source", "session_id": "session", "instance_id": "execution"}
    with bind_sdk_identity(**identity), memory_dispatch_hook(module, spec, observer):
        for current in (client, client.with_options(timeout=2)):
            messages = render(module, payload)
            assert current.chat.completions.create(model="synthetic", messages=messages) is result
    observations = read_observations(tmp_path)
    assert len(calls) == len(observations) == 2
    assert all(row["status"] == "completed" and row["actual_provider_call"] for row in observations)
    assert all(len(row["selected"]) == 2 and all(item["complete_in_sdk"] for item in row["selected"])
               and row["sidecar_absent"] and row["usage"]["prompt_tokens"] == 17 for row in observations)
    assert all(row["request_estimate"] <= 128000 and row["real_vendor_qualified"] is False
               and row["real_model_quality_passed"] is False for row in observations)
    assert observations[0]["sdk_request_sha256"] == digest({"args": (), "request": calls[0][1]})
    assert spec["memory_quality"]["labels"]["style"] not in json.dumps(observations, ensure_ascii=False)


def test_actual_wire_missing_fact_and_sidecar_marker_fail_independent_input_checks(spec, tmp_path):
    """capture的完整标签不能把最终被截断或带侧带字段的SDK输入判完整。"""
    tmp_path.chmod(0o700)
    observer = MemoryWireObserver(tmp_path)
    payload = payload_for(spec)
    payload.envelope.selected = payload.envelope.candidates
    complete = [{"role": "user", "content": json.dumps(dict(payload), ensure_ascii=False)}]
    observer.capture(payload, complete)
    # 最终messages发生变化，不能借前一个渲染摘要补为合法来源。
    final = [{"role": "user", "content": "candidate_digest 未投影事实"}]
    client = observer.observe(OriginalClient(SimpleNamespace(usage=None), []))
    client.chat.completions.create(messages=final)
    row = read_observations(tmp_path)[0]
    assert row["memory_binding_valid"] is False
    assert row["sidecar_absent"] is False
    assert row["selected"] == []


def test_observer_original_exception_and_sticky_io_failure_preserve_transport(spec, tmp_path, monkeypatch):
    """SDK原异常身份保持；旁证写失败不变为成功、健康永久无效。"""
    tmp_path.chmod(0o700)
    observer = MemoryWireObserver(tmp_path)
    original = RuntimeError("synthetic original provider error")
    payload = payload_for(spec)
    payload.envelope.selected = payload.envelope.candidates
    messages = [{"role": "user", "content": json.dumps(dict(payload), ensure_ascii=False)}]
    observer.capture(payload, messages)
    client = observer.observe(OriginalClient(None, [], original))
    with pytest.raises(RuntimeError) as caught:
        client.chat.completions.create(messages=messages)
    assert caught.value is original
    assert read_observations(tmp_path)[0]["status"] == "error"
    monkeypatch.setattr(observer, "_append", lambda _row: (_ for _ in ()).throw(OSError("synthetic")))
    result = object()
    success = observer.observe(OriginalClient(result, []))
    observer.capture(payload, messages)
    assert success.chat.completions.create(messages=messages) is result
    assert observer.health()["valid"] is False and observer.health()["error_count"] == 1
    assert observer.health()["real_model_quality_passed"] is False


def test_memory_wire_aggregate_row_budget_survives_observer_restart(spec, tmp_path, monkeypatch):
    """新进程另开PID日志后仍须遵守同一运行目录的合计行预算。"""
    tmp_path.chmod(0o700)
    monkeypatch.setattr(final_f3_live_memory, "MAX_MEMORY_STATE_JSONL_ROWS", 1)
    first = MemoryWireObserver(tmp_path)
    first._append({"row": 1})
    restarted = MemoryWireObserver(tmp_path)
    restarted.path = tmp_path / "memory-wire-999999.safe.jsonl"
    with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_STATE_ROW_LIMIT"):
        restarted._append({"row": 2})
    rows = [line for path in tmp_path.glob("memory-wire-*.safe.jsonl") for line in path.read_text().splitlines()]
    assert rows == ['{"row":1}']


def test_memory_wire_aggregate_byte_budget_survives_observer_restart(spec, tmp_path, monkeypatch):
    """PID日志轮换不能绕过同一运行根的累计字节限额。"""
    tmp_path.chmod(0o700)
    monkeypatch.setattr(final_f3_live_memory, "MAX_MEMORY_STATE_JSONL_BYTES", 8)
    first = MemoryWireObserver(tmp_path)
    first._append({"a": 1})
    restarted = MemoryWireObserver(tmp_path)
    restarted.path = tmp_path / "memory-wire-999998.safe.jsonl"
    with pytest.raises(LiveMemoryLimitError, match="F3_LIVE_MEMORY_STATE_JSONL_BYTE_LIMIT"):
        restarted._append({"b": 2})


@pytest.mark.parametrize("mode", ["matched", "foreign_tenant", "other_instance"])
def test_memory_state_decodes_historical_plan_frozen_version_and_signal_in_exact_scope(
    spec, owned_db, mode,
):
    """记忆state保存原恢复Signal及历史plan，不从当前版本或另一合法实例猜身份。"""
    from sqlmodel import select
    from app.db.models import SkillVersion
    from test_final_f3_live_fixture import seed_identity_observation_rows

    db, _, runtime = owned_db
    seed(db, spec)
    agents = {branch: spec["memory_quality"]["agents"][branch]["1"]["id"]
              for branch in ("SOP", "DTA")}
    version = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == spec["tenant"]["id"],
        SkillVersion.skill_id == spec["memory_quality"]["skill_prefix"] + "_1")).one()
    original = seed_identity_observation_rows(db, runtime, tenant=spec["tenant"]["id"],
        owner=spec["owner"]["id"], agents=agents, version=version, mode=mode)
    for branch in ("SOP", "DTA"):
        actual = state(db, spec, agents[branch])
        call = next(row for row in actual["sdk_calls"]
                    if row["source_input_id"] == original["sources"][branch])
        assert {key: call[key] for key in ("plan_id", "skill_version_id", "signal_id")} == (
            original["expected"][branch]
        )
        assert "identity-signal-" + branch in {row["id"] for row in actual["signals"]}
        assert "identity-signal-foreign" not in {row["id"] for row in actual["signals"]}
        assert {row["id"] for row in actual["plan_revisions"]} == ({
            "identity-plan-old", "identity-plan-current", "identity-plan-other",
        } if branch == "DTA" else set())


def sdk_call_id_control(spec, runtime, statuses, *, matched=True):
    """仅种本项私有合成receipt/wire控制，原call身份不能从wire预填或请求摘要猜测。"""
    runtime.chmod(0o700)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    identity = {key: digest(value) for key, value in {
        "tenant_id": tenant, "user_id": owner, "agent_id": agent,
        "session_id": "session", "instance_id": "execution", "source_input_id": "source",
        "turn_id": "source",
    }.items()}
    receipts, wires = [], []
    for number, status in enumerate(statuses, start=1):
        receipts.append({"call_id": "f3_sdk_" + f"{number:032x}", "pid": 12345,
            "identity_sha256": identity, "request_sha256": digest("same-request"),
            "observation_valid": True, "status": status,
            "usage": {"prompt_tokens": number + 10, "completion_tokens": number,
                      "total_tokens": number * 2 + 10}})
        wires.append({"call_id": "f3_sdk_" + "f" * 32, "pid": 12345,
            "identity_sha256": identity, "sdk_request_sha256": digest("same-request"),
            "phase_sha256": digest("dynamic_task.answer"), "status": "completed",
            "actual_provider_call": status == "completed", "selected": []})
    for name, values in (("sdk-observations-12345.safe.jsonl", receipts if matched else []),
                         ("memory-wire-12345.safe.jsonl", wires)):
        path = runtime / name
        path.write_text("".join(json.dumps(row) + "\n" for row in values))
        path.chmod(0o600)
    result = _sdk_calls(runtime, tenant, owner, agent, ["session"], ["execution"],
        [SimpleNamespace(id="source", role="user", tenant_id=tenant, session_id="session")],
        session_rows=[SimpleNamespace(id="session", tenant_id=tenant, user_id=owner, agent_id=agent)],
        execution_rows=[SimpleNamespace(id="execution", tenant_id=tenant, initiator_user_id=owner,
                                       session_id="session", agent_id=agent, kind="dynamic_task")])
    return result, receipts


def test_sdk_state_exposes_only_exact_matched_original_call_id(spec, tmp_path):
    """透出原receipt.call_id，缺receipt时保持None而不沿用wire不可信的预填ID。"""
    rows, receipts = sdk_call_id_control(spec, tmp_path, ["completed"])
    assert len(rows) == 1 and rows[0]["original_sdk_receipt_found"] is True
    assert rows[0]["call_id"] == receipts[0]["call_id"]
    unmatched, _ = sdk_call_id_control(spec, tmp_path, ["completed"], matched=False)
    assert unmatched[0]["call_id"] is None
    assert unmatched[0]["original_sdk_receipt_found"] is False


def test_same_request_failed_then_successful_retry_preserves_two_original_call_ids(spec, tmp_path):
    """同PID/请求/身份的合法失败重试是两次原调用，分别保留call_id、状态及usage。"""
    rows, receipts = sdk_call_id_control(spec, tmp_path, ["failed", "completed"])
    assert len(rows) == 2
    assert rows[0]["sdk_request_sha256"] == rows[1]["sdk_request_sha256"]
    assert [row["call_id"] for row in rows] == [row["call_id"] for row in receipts]
    assert len({row["call_id"] for row in rows}) == 2
    assert [row["status"] for row in rows] == ["failed", "completed"]
    assert [row["usage"] for row in rows] == [row["usage"] for row in receipts]
    assert all(row["persistent_binding_valid"] is True for row in rows)


@pytest.mark.parametrize("duplicate_job", [False, True])
def test_memory_sdk_task_id_requires_one_exact_persistent_capture_job(spec, tmp_path, duplicate_job):
    """原memory.capture task_id必须唯一绑定同scope、同session和同轮来源的持久作业。"""
    tmp_path.chmod(0o700)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    task_id, session_id, source_id, assistant_id = "memory-task", "memory-session", "memory-source", "memory-assistant"
    identity_values = {"tenant_id": tenant, "user_id": owner, "agent_id": agent,
        "session_id": session_id, "source_input_id": source_id, "turn_id": source_id,
        "task_id": task_id}
    identity = {key: digest(value) for key, value in identity_values.items()}
    wire = {"pid": 12345, "identity_sha256": identity, "sdk_request_sha256": digest("memory request"),
        "phase_sha256": digest("memory.capture"), "actual_provider_call": True, "selected": []}
    receipt = {"pid": 12345, "identity_sha256": identity, "request_sha256": digest("memory request"),
        "observation_valid": True, "status": "completed", "usage": {"prompt_tokens": 10}}
    for name, row in (("memory-wire-12345.safe.jsonl", wire),
                      ("sdk-observations-12345.safe.jsonl", receipt)):
        path = tmp_path / name
        path.write_text(json.dumps(row) + "\n")
        path.chmod(0o600)
    source = SimpleNamespace(id=source_id, tenant_id=tenant, session_id=session_id, role="user")
    assistant = SimpleNamespace(id=assistant_id, tenant_id=tenant, session_id=session_id,
        role="assistant", metadata_json={"turn_id": source_id, "user_message_id": source_id})
    session = SimpleNamespace(id=session_id, tenant_id=tenant, user_id=owner, agent_id=agent)
    job = SimpleNamespace(id=task_id, tenant_id=tenant, user_id=owner, agent_id=agent,
        source_message_id=source_id, source_assistant_message_id=assistant_id)
    jobs = [job, SimpleNamespace(**vars(job))] if duplicate_job else [job]

    rows = _sdk_calls(tmp_path, tenant, owner, agent, [session_id], [], [source, assistant],
        session_rows=[session], memory_jobs=jobs)

    assert len(rows) == 1
    assert rows[0]["operation"] == "memory.capture"
    assert rows[0]["task_id"] == (None if duplicate_job else task_id)
    assert rows[0]["persistent_binding_valid"] is (not duplicate_job)


def test_memory_state_binds_real_sdk_task_to_database_capture_job(spec, owned_db):
    """afterEach实际memory state必须从SQLite持久作业解码SDK task_id而非只看wire摘要。"""
    import hashlib

    from app.db.models import ChatSession, MemoryCaptureJob, Message

    db, _, runtime = owned_db
    seed(db, spec)
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    agent = spec["memory_quality"]["agents"]["DTA"]["1"]["id"]
    session_id, source_id, assistant_id, task_id = (
        "capture-session", "capture-source", "capture-assistant", "capture-task"
    )
    db.add(ChatSession(id=session_id, tenant_id=tenant, user_id=owner, agent_id=agent))
    db.add(Message(id=source_id, tenant_id=tenant, session_id=session_id, role="user",
        content="synthetic source", metadata_json={"client_turn_id": "synthetic-turn"}))
    db.add(Message(id=assistant_id, tenant_id=tenant, session_id=session_id, role="assistant",
        content="synthetic response", metadata_json={"turn_id": source_id, "user_message_id": source_id}))
    job = MemoryCaptureJob(id=task_id, tenant_id=tenant, user_id=owner, agent_id=agent,
        agent_scope_key="agent:" + agent, scope_id=agent, source_message_id=source_id,
        source_assistant_message_id=assistant_id, source_hash="a" * 64,
        model_config_id="synthetic-model", model_contract_hash="b" * 64)
    db.add(job)
    db.commit()
    identity = {key: digest(value) for key, value in {
        "tenant_id": tenant, "user_id": owner, "agent_id": agent, "session_id": session_id,
        "source_input_id": source_id, "turn_id": source_id, "task_id": task_id,
    }.items()}
    request_hash = digest("actual synthetic memory SDK request")
    wire = {"pid": 12345, "identity_sha256": identity, "sdk_request_sha256": request_hash,
        "phase_sha256": digest("memory.capture"), "actual_provider_call": True, "selected": []}
    receipt = {"pid": 12345, "identity_sha256": identity, "request_sha256": request_hash,
        "call_id": "f3_sdk_" + "a" * 32, "observation_valid": True, "status": "completed",
        "usage": {"prompt_tokens": 11}}
    for name, row in (("memory-wire-12345.safe.jsonl", wire),
                      ("sdk-observations-12345.safe.jsonl", receipt)):
        path = runtime / name
        path.write_text(json.dumps(row) + "\n")
        path.chmod(0o600)

    actual = state(db, spec, agent, session_id=session_id)

    call = actual["sdk_calls"][0]
    assert call["operation"] == "memory.capture"
    assert call["task_id"] == task_id
    assert call["source_input_id"] == source_id and call["turn_id"] == source_id
    assert call["persistent_binding_valid"] is True
    assert actual["jobs"] == [{"id": task_id, "tenant_id": tenant, "user_id": owner,
        "agent_id": agent, "status": "pending", "source_message_id": source_id,
        "source_assistant_message_id": assistant_id, "source_sequence": 0}]
    persisted_answer = next(row for row in actual["messages"] if row["id"] == assistant_id)
    assert persisted_answer["content_sha256"] == hashlib.sha256(b"synthetic response").hexdigest()
