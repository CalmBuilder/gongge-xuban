"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_sd8_evidence.py
@CallChain  : 无模型pytest → G3质量/原事件观察器 → 数值、Runtime关联及安全失败旁证
@Description: 防止伪造政策、自动预检冒充节点或诊断正文进入验收记录。
"""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sd8_evidence_subject", Path(__file__).with_name("sd8_fullstack_fixture.py")
)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


@pytest.mark.parametrize("value", [None, {"query": "ZIRCON999UNKNOWN synthetic-private-query",
    "desired_evidence": "ZIRCON999UNKNOWN synthetic-private-aspect", "token": "private-token"},
    {"query": "CYPRESS42", "desired_evidence": None},
    {"query": {"nested": "ZIRCON999UNKNOWN"}, "desired_evidence": " "}])
def test_knowledge_request_summary_keeps_only_original_root_flags(value):
    """原 Operation 根参数仅合成目标及 hash 可见，嵌套同形和私密文字不得泄露。"""
    result = subject.knowledge_request_summary(value)
    request = value if isinstance(value, dict) else {}
    query, desired = request.get("query"), request.get("desired_evidence")
    assert result["query_hash"] == subject.digest(query)
    assert result["desired_evidence_hash"] == subject.digest(desired)
    assert result["query_has_unknown_policy"] is (
        isinstance(query, str) and "ZIRCON999UNKNOWN" in query)
    assert result["desired_evidence_nonempty"] is (
        isinstance(desired, str) and bool(desired.strip()))
    assert "synthetic-private" not in str(result) and "private-token" not in str(result)
    assert all(isinstance(child, bool) or (isinstance(child, str) and len(child) == 64)
               for child in result.values())


@pytest.mark.parametrize(
    ("reply", "has_policy_number"),
    [
        pytest.param("未找到ZIRCON999UNKNOWN资料，不能回答。", False, id="legal-fixed-code"),
        pytest.param("没有资料，无法确认额度或截止。", False, id="legal-no-number"),
        pytest.param("没有资料；额度888元，截止12:00。", True, id="invented-arabic"),
        pytest.param("未找到资料，额度RMB888元。", True, id="invented-currency-code"),
        pytest.param("无资料，额度八百元，截止下午三点。", True, id="invented-chinese"),
        pytest.param("资料不足；额度捌佰元，截止十七时三十分。", True, id="invented-financial"),
        pytest.param("无资料；金额为伍仟圆。", True, id="invented-financial-currency"),
        pytest.param("未找到；额度一千五百，截止17点30分。", True, id="invented-mixed"),
        pytest.param("未知政策额度和时间均不能确认，无法进一步提供。", False, id="legal-further"),
        pytest.param("没有资料，金额需一并确认。", False, id="legal-together"),
        pytest.param("缺乏额度、时间及来源，无法进一步确认。", False, id="legal-source-further"),
        pytest.param("未找到资料，无法提供一点帮助。", False, id="legal-some-help"),
        pytest.param("未找到资料；额度约888，截止未知。", True, id="invented-bare-arabic"),
        pytest.param("没有资料；金额为伍仟。", True, id="invented-bare-financial"),
        pytest.param("无资料；时间信息为下午三点。", True, id="invented-time-label"),
        pytest.param("无资料；截止11:30。", True, id="invented-bare-time"),
        pytest.param("没有资料；截止为两小时。", True, id="invented-hours"),
        pytest.param("无资料；截止需三十分钟。", True, id="invented-minutes"),
        pytest.param("没有来源；额度USD888。", True, id="invented-usd"),
        pytest.param("未找到资料，无法确认截止需要多少小时或分钟。", False, id="legal-duration-unknown"),
    ],
)
def test_no_answer_policy_values(reply: str, has_policy_number: bool) -> None:
    """只允许固定政策编码出现在合法无答案，任意中英金额/时间都须识别。"""
    assert subject.facts(reply)["policy_number_present"] is has_policy_number


@pytest.mark.parametrize("automatic", [None, False, True])
def test_sop_receipt_association_excludes_automatic_retrieval(automatic):
    """同轮自动预检可以与Runtime回执同形，但不能冒充节点本身的finished事件。"""
    from datetime import UTC, datetime
    from sqlmodel import Session, SQLModel, create_engine
    from app.db.models import AgentEvent, Message, SopInstance, SopNodeExecution, SopOperation

    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    user = Message(id="observer-user", tenant_id=subject.TENANT, session_id="observer-session",
                   role="user", content="合成查询", created_at=stamp)
    instance = SopInstance(id="observer-instance", tenant_id=subject.TENANT,
                          session_id=user.session_id, agent_id=subject.AGENT)
    node = SopNodeExecution(id="observer-node", tenant_id=subject.TENANT,
                           instance_id=instance.id, node_id="query", step_key="query")
    receipt = {"outcome": "no_match", "degraded": True, "evidence_sufficiency": {},
               "knowledge_base_version_ids": ["observer-version"],
               "counts": {"selected_documents": 0, "selected_buckets": 0,
                          "selected_concepts": 0, "chunks": 0, "evidence": 0}}
    operation = SopOperation(tenant_id=subject.TENANT, instance_id=instance.id,
        node_execution_id=node.id, operation_name="knowledge.search", idempotency_key="observer",
        logical_action_id="observer-action", request_fingerprint="observer", created_at=stamp,
        request_json={"query": "合成查询"}, result_json=receipt)
    payload = {**receipt, "query": operation.request_json, "source_message": user.content,
               "turn_id": user.id, "user_message_id": user.id}
    if automatic is not None:
        payload["auto"] = automatic
    event = AgentEvent(tenant_id=subject.TENANT, session_id=user.session_id,
                      event_type="knowledge_query_finished", payload_json=payload, created_at=stamp)
    with Session(engine) as db:
        db.add(user)
        db.add(node)
        db.commit()
        diagnostic = {}
        assert subject.matches_sop_event(db, operation, instance, event, diagnostic=diagnostic) is (automatic is not True)
        assert diagnostic == ({"failure_reason": "source_identity"} if automatic is True else {})
    engine.dispose()


@pytest.mark.parametrize("code", ["DYNAMIC_PLAN_INVALID", "ValueError", "Bearer secret", ""])
def test_planning_failure_summary_does_not_copy_diagnostic(code):
    """原失败事件可定位实例缺失，但模型诊断/未知字段和非符号码不得归档。"""
    from app.db.models import AgentEvent

    event = AgentEvent(id="observer-failed-event", tenant_id=subject.TENANT,
                      session_id="observer-failed-session", event_type="dynamic_task_delegation_failed",
                      payload_json={"code": code, "diagnostic": "synthetic-private-model-response",
                                    "authorization": "synthetic-private-header"})
    result = subject.planning_failure_summary(event)
    assert result["id"] == event.id and result["session_id"] == event.session_id
    assert result["code"] == (code if code in {"DYNAMIC_PLAN_INVALID", "ValueError"} else None)
    assert len(result["payload_hash"]) == 64
    assert "synthetic-private" not in str(result)
    assert set(result) == {"id", "session_id", "event_type", "code", "payload_hash", "source_error_matches"}
    assert result["source_error_matches"] == []


def test_sdk_knowledge_input_observation_preserves_only_actual_evidence_flags():
    """实际完成步骤重复投影须去重，原正文/侧带不输出，缺输出不能被包装成证据。"""
    import json

    evidence = {"outcome": "evidence_found", "evidence_sufficiency": {"satisfied": True},
                "evidence_pack": [{"content": "CYPRESS42额度：600元。"},
                                  {"content": "CYPRESS42截止：16:00。"}],
                "authorization": "synthetic-private-header"}
    steps = [{"step_key": "step_01", "model_output": evidence},
             {"step_key": "step_02", "model_output": None}]
    context = {"execution_id": "observer-execution", "completed_steps": steps}
    result = subject.sdk_knowledge_input_summary([{"role": "user", "content": json.dumps({
        "provider_execution_view": {"protocol_version": "dynamic-v1", "execution_context": context,
            "messages": [{"role": "system", "message_kind": "execution_context",
                          "content": {"execution_context": context}}]},
    })}])
    assert len(result) == 2
    assert result[0]["has_knowledge_result"] and result[0]["sufficient"]
    assert result[0]["evidence_facts"]["old_amount"] and result[0]["evidence_facts"]["old_deadline"]
    assert result[1]["missing_output"] and not result[1]["has_knowledge_result"]
    assert result[0]["execution_id"] == "observer-execution"
    assert result[0]["origins"] == ["execution_context", "execution_context_message"]
    assert "synthetic-private" not in str(result) and "600元" not in str(result)


@pytest.mark.parametrize("location", ["assistant", "system", "ordinary", "nested"])
def test_sdk_history_json_cannot_be_current_execution_evidence(location):
    """助手/系统历史及普通消息或任意嵌套同形 JSON 均不是正式根机械视图。"""
    import json

    context = {"completed_steps": [{"step_key": "step_01", "model_output": {
        "outcome": "evidence_found", "evidence_pack": [{"content": "CYPRESS42额度：600元。"}],
    }}]}
    view = {"protocol_version": "dynamic-v1", "execution_context": context, "messages": []}
    payload = {"provider_execution_view": view}
    role = location if location in {"assistant", "system"} else "user"
    if location == "ordinary":
        payload = {"provider_execution_view": {"protocol_version": "dynamic-v1", "messages": [
            {"role": "system", "message_kind": "ordinary", "content": {"execution_context": context}},
        ]}}
    elif location == "nested":
        payload = {"history": payload, "execution_context": context}
    assert subject.sdk_knowledge_input_summary([{"role": role, "content": json.dumps(payload)}]) == []


def test_planning_diagnostic_matches_only_original_source_error_location():
    """已知原校验失败可按固定源码定位，诊断和任意模型正文不复制进报告。"""
    from app.db.models import AgentEvent

    event = AgentEvent(tenant_id=subject.TENANT, session_id="observer-failed-session",
                      event_type="dynamic_task_delegation_failed", payload_json={
                          "code": "DYNAMIC_PLAN_SEMANTIC_INVALID",
                          "diagnostic": "已加载指导 Skill 必须且只能由计划步骤显式引用 synthetic-private",
                      })
    result = subject.planning_failure_summary(event)
    assert result["source_error_matches"]
    assert result["source_error_matches"][0]["file"] == "backend/app/dynamic_tasks/planner_service.py"
    assert "synthetic-private" not in str(result) and "已加载指导" not in str(result)


def test_planning_error_observer_propagates_original_exception(monkeypatch, tmp_path):
    """隔离观察器只抛同一异常对象，旁证不能复制模型文本或伪造执行结果。"""
    import json
    from app.dynamic_tasks.agent import DynamicTaskAgent

    failure = ValueError("synthetic-private-model-response")

    def fail(self, *args, **kwargs):
        """模拟原宿主失败且不制造数据库事实。"""
        raise failure

    monkeypatch.setattr(DynamicTaskAgent, "start_task", fail)
    subject.observe_planning_errors(tmp_path)
    with pytest.raises(ValueError) as caught:
        DynamicTaskAgent.start_task(None, tenant_id=subject.TENANT, session_id="observer-session")
    assert caught.value is failure
    recorded = json.loads((tmp_path / "planning-observations.safe.json").read_text())
    assert recorded[0]["session_id"] == "observer-session"
    assert recorded[0]["exception_type"] == "ValueError"
    assert "synthetic-private" not in str(recorded)


def test_planning_error_observer_filters_schema_input(monkeypatch, tmp_path):
    """真实 Pydantic 异常只留下白名单字段位置和类型，输入/ctx/诊断不归档。"""
    import json
    from pydantic import ValidationError
    from app.dynamic_tasks.agent import DynamicTaskAgent
    from app.dynamic_tasks.planning import DynamicPlanDraft

    def fail(self, *args, **kwargs):
        """经原计划 schema 制造未知私密字段反例。"""
        return DynamicPlanDraft.model_validate({"synthetic-private-extra": "synthetic-private-input"})

    monkeypatch.setattr(DynamicTaskAgent, "start_task", fail)
    subject.observe_planning_errors(tmp_path)
    with pytest.raises(ValidationError):
        DynamicTaskAgent.start_task(None, tenant_id=subject.TENANT, session_id="observer-session")
    recorded = json.loads((tmp_path / "planning-observations.safe.json").read_text())
    assert recorded[0]["schema_errors"]
    assert "synthetic-private" not in str(recorded)


@pytest.mark.parametrize("mode", ["truncated", "corrupt", "write_failure"])
def test_observer_sidecar_failure_keeps_original_exception(monkeypatch, tmp_path, mode):
    """无效截断schema、坏旧文件及写入失败均须重新抛出同一原ValidationError。"""
    import json
    from pydantic import ValidationError
    from app.dynamic_tasks.agent import DynamicTaskAgent
    from app.dynamic_tasks.planning import DynamicPlanDraft
    from app.dynamic_tasks import planner_service

    try:
        DynamicPlanDraft.model_validate({})
    except ValidationError as error:
        original_failure = error

    def fail(self, *args, **kwargs):
        """固定抛出原schema错误，不执行数据库或模型。"""
        raise original_failure

    def write_failure(*args, **kwargs):
        """只模拟自有旁证磁盘写入失败。"""
        raise OSError("synthetic-sidecar-write-failure")

    monkeypatch.setattr(DynamicTaskAgent, "start_task", fail)
    if mode == "truncated":
        monkeypatch.setattr(planner_service, "_planner_failure_message", lambda error: '{"schema_errors":[')
    elif mode == "corrupt":
        (tmp_path / "planning-observations.safe.json").write_text("{")
    else:
        monkeypatch.setattr(subject, "save", write_failure)
    subject.observe_planning_errors(tmp_path)
    with pytest.raises(ValidationError) as caught:
        DynamicTaskAgent.start_task(None, tenant_id=subject.TENANT, session_id="observer-session")
    assert caught.value is original_failure
    if mode != "write_failure":
        result = json.loads((tmp_path / "planning-observations.safe.json").read_text())[0]
        assert result["diagnostic_incomplete"] and result["exception_type"] == "ValidationError"


def test_planning_observer_keeps_success_return(monkeypatch, tmp_path):
    """正常原返回保持同一对象且不伪造异常旁证。"""
    from app.dynamic_tasks.agent import DynamicTaskAgent

    returned = (object(), True)

    def succeed(self, *args, **kwargs):
        """返回固定原对象，不调用模型或数据库。"""
        return returned

    monkeypatch.setattr(DynamicTaskAgent, "start_task", succeed)
    subject.observe_planning_errors(tmp_path)
    assert DynamicTaskAgent.start_task(None) is returned
    assert not (tmp_path / "planning-observations.safe.json").exists()
