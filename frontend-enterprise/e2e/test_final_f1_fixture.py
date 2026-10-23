"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f1_fixture.py
@CallChain  : 正式pytest → 受控SDK响应适配 → 当前输入与已完成证据边界
@Description: 拒绝借历史/accepted回执或缺失事实生成F1业务正确答案。
"""

import pytest

from final_f1_fullstack_fixture import (
    MEMORIES, capture_payload_from_wire, current_user_content, model_response,
    response_text, seed_resources,
)


@pytest.mark.parametrize("current", [
    "请记住我的稳定服务偏好：F1 完整持久事实：回执需保留配送批次。",
    "请查询配送进度。",
    '{"普通用户输入": "不是控制包装"}',
])
def test_capture_decodes_actual_sdk_role_history_without_treating_control_as_source(current):
    """使用真实LLM序列化验证同轮末位用户和助手，控制JSON不能代替来源。"""
    from app.llm.client import _prepare_user_input, _request_messages

    history = [
        {"role": "user", "content": "请记住我的稳定服务偏好：旧事实不得借用。"},
        {"role": "assistant", "content": "旧回答。"},
        {"role": "user", "content": current},
        {"role": "assistant", "content": "本轮只确认偏好，不新增业务。"},
    ]
    raw = {"conversation_context": {"messages": history},
           "existing_memories": "历史记忆也不得补当前答案。",
           "step_result": {"action": "reply", "reply": history[-1]["content"]}}
    context, serialized = _prepare_user_input(raw)
    wire = _request_messages("你是用户长期记忆抽取与更新助手。", context, serialized)
    decoded = capture_payload_from_wire(wire, {})
    assert decoded["conversation_context"]["messages"] == history
    assert decoded["existing_memories"] == raw["existing_memories"]
    assert current_user_content(decoded) == current
    delta = model_response("你是用户长期记忆抽取与更新助手。", decoded)
    expected = ([{"operation": "upsert", "kind": "preference", "key": "f1_capture",
                  "content": current.removeprefix("请记住我的稳定服务偏好："), "importance": 0.8}]
                if current.startswith("请记住我的稳定服务偏好：") else [])
    assert delta == {"memories": expected, "updated_summary": ""}


@pytest.mark.parametrize("current", ["请查询配送进度。", "请读回既有偏好，勿更新。", ""])
def test_capture_only_extracts_explicit_stable_information_from_current_user(current):
    """历史、已有记忆和助手文案不能替当前来源生成固定事实。"""
    fact = "F1 完整持久事实：办理后保存原始回执。"
    payload = {"conversation_context": {"messages": [
        {"role": "user", "content": "请记住我的稳定服务偏好：" + fact},
        {"role": "assistant", "content": "已记录。"},
        {"role": "user", "content": current},
        {"role": "assistant", "content": fact}]}, "existing_memories": fact}
    assert model_response("你是用户长期记忆抽取与更新助手。", payload) == {
        "memories": [], "updated_summary": ""}


def test_capture_uses_current_supplied_fact_instead_of_fixed_expected_label():
    """合法新稳定偏好取当前完整内容，不能写死首次合成人工标签。"""
    fact = "F1 完整持久事实：回执需保留配送批次。"
    payload = {"conversation_context": {"messages": [
        {"role": "user", "content": "请记住我的稳定服务偏好：" + fact},
        {"role": "assistant", "content": "已记录。"}]}}
    delta = model_response("你是用户长期记忆抽取与更新助手。", payload)
    assert len(delta["memories"]) == 1
    assert delta["memories"][0]["content"] == fact


def test_sop_consumer_uses_current_router_fact_and_preserves_complete_content():
    """原SOP唯一记忆消费者Router实际选择当前完整新事实，不要求回复阶段再注入。"""
    fact = "F1 完整持久事实：回执需保留配送批次。"
    payload = {"_agent_stage": {"phase": "Router", "memory": fact},
               "user_message": "F1事实消费：请解释我记住的服务要求，勿开始业务。"}
    decision = model_response("", payload)
    assert decision["decision"] == "answer_only"
    assert fact in decision["user_intent"]


@pytest.mark.parametrize("phase", ["Router", "Dynamic Planner", "Dynamic Action"])
def test_fact_consumers_reject_history_when_current_projection_missing(phase):
    """三个实际消费者当前无新事实时必须拒绝，历史同形内容不能生成正确回答。"""
    payload = {"_agent_stage": {"phase": phase},
        "user_message": "F1事实消费：请解释我记住的服务要求，勿开始业务。",
        "goal": "F1事实消费：请解释我记住的服务要求，勿开始业务。",
        "current_step": {"kind": "answer", "step_key": "answer"},
        "provider_execution_view": {"execution_context": {
            "goal": {"goal": "F1事实消费：请解释我记住的服务要求，勿开始业务。"},
            "memory_context": [], "completed_steps": [], "success_criteria": []}},
        "history": {"content": "F1 完整持久事实：办理后保存原始回执。"}}
    system = "受控动态任务规划器" if phase == "Dynamic Planner" else ""
    with pytest.raises(ValueError, match="CURRENT_CAPTURED_MEMORY_MISSING"):
        model_response(system, payload)


def test_dynamic_consumer_reads_complete_current_fact_without_fixed_answer():
    """DTA最终动作只读当前完整记忆投影，合成标签改变时答案随实际事实改变。"""
    fact = "F1 完整持久事实：回执需保留配送批次。"
    payload = {"current_step": {"kind": "answer", "step_key": "answer"},
        "provider_execution_view": {"execution_context": {
            "goal": {"goal": "F1事实消费：请解释服务要求，勿开始业务。"},
            "memory_context": [{"content": fact}], "completed_steps": [],
            "success_criteria": [{"id": "criterion_01", "description": "解释已记住的要求"}]}}}
    action = model_response("", payload)
    assert action["arguments"]["markdown"] == fact


def test_stable_preference_note_remains_a_completed_non_business_turn():
    """显式稳定偏好轮次只回答并由原完成事务入队，不伪造新的业务计划。"""
    decision = model_response("", {"_agent_stage": {"phase": "Router / Dynamic Task"},
        "user_message": "请记住我的稳定服务偏好：F1 完整持久事实：办理后保存原始回执。"})
    assert decision["mode"] == "answer"
    assert decision["requires_durable_execution"] is False


def test_fact_consumer_classifies_the_actual_query_without_claiming_delivery_success():
    """新事实咨询的人工成果是解释偏好，不能以配送成功标准冒充咨询完成。"""
    message = "F1事实消费：请解释我记住的稳定服务要求；只需回答，不提交新的配送。"
    decision = model_response("", {"_agent_stage": {"phase": "Router / Dynamic Task"},
                                  "user_message": message})
    assert decision["goal"] == message
    assert decision["success_criteria"] == ["准确解释当前完整稳定服务要求，不执行配送"]


@pytest.fixture
def fixture_resource_engine(tmp_path, monkeypatch):
    """每项资源合同控制使用独立SQLite适配器，避免真实全资源发布互相残留。"""
    from app.config import get_settings
    from app.db.factory import SQLAlchemyDatabaseAdapterFactory

    runtime = SQLAlchemyDatabaseAdapterFactory.create(
        "sqlite:///" + str(tmp_path / "fixture-resources.sqlite3"), get_settings())
    monkeypatch.setattr("app.db.database.database_runtime", runtime)
    monkeypatch.setattr("app.db.engine", runtime.engine)
    try:
        yield runtime.engine
    finally:
        runtime.engine.dispose()


def test_router_requires_current_memory_and_time_instead_of_historical_labels():
    """当前记忆缺失不能靠历史同形文本补答案，缺时钟也不能固定日期求绿。"""
    payload = {"_agent_stage": {"phase": "Router", "memory": ""},
               "user_message": "使用我记住的客户", "history": MEMORIES}
    with pytest.raises(ValueError, match="CURRENT_FULL_MEMORY_CUSTOMER_MISSING"):
        model_response("", payload)
    payload["user_message"] = "明天09:00配送"
    with pytest.raises(ValueError, match="CURRENT_TIME_CONTEXT_MISSING"):
        model_response("", payload)


def test_answer_does_not_borrow_historical_or_accepted_receipt():
    """当前完成集合为空时，历史或accepted形似回执都不能宣称任务完成。"""
    assert "尚未取得" in response_text({"completed_steps": [], "history": {
        "receipt_id": "old-receipt", "status": "succeeded"}, "operations": [{
        "receipt_id": "accepted-receipt", "status": "waiting_external"}]})


def test_finish_only_uses_current_completed_model_output():
    """后继写只取当前完成步骤，历史和待执行参数里的回执均不可复用。"""
    payload = {"current_step": {"kind": "tool.write", "capability_refs": ["final_f1.finish@v1"]},
               "time_context": {"local_at": "2026-10-03T23:59:00+08:00"},
               "provider_execution_view": {"execution_context": {"completed_steps": [],
                  "history": {"receipt_id": "old-receipt"}}}}
    with pytest.raises(ValueError, match="CURRENT_COMPLETED_RECEIPT_MISSING"):
        model_response("", payload)


def test_answer_reads_full_business_result_from_current_completed_step():
    """合法当前完整回执按原输入读出四字段及知识，不能替换成历史旧结果。"""
    payload = {"completed_steps": [{"step_key": "finish", "model_output": {
        "receipt_id": "current-receipt", "product_name": "ALPHA91", "quantity": 3,
        "customer": "CYGNUS77", "due_date": "2026-10-04 09:00"}},
        {"step_key": "policy", "model_output": {"content": "CYGNUS77每批上限750件"}}],
        "history": {"receipt_id": "old-receipt"}}
    result = response_text(payload)
    assert all(value in result for value in ["current-receipt", "ALPHA91", "3", "CYGNUS77", "2026-10-04 09:00", "750"])
    assert "old-receipt" not in result


def test_dynamic_submit_derives_all_arguments_from_current_goal_memory_and_time():
    """变更合成人工标签后仍取当前完整事实，禁止返回写死的原配送参数。"""
    payload = {"current_step": {"kind": "tool.write", "capability_refs": ["final_f1.submit@v1"]},
               "provider_execution_view": {"execution_context": {
                   "goal": {"goal": "商品BETA17、数量6、明天09:00配送，使用我记住的客户"},
                   "memory_context": [{"content": "客户全名：LYRA22，完整名称不可截断。"}],
                   "time_context": {"anchor_at_utc": "2026-10-10T15:59:00Z",
                                    "local_at": "2026-10-10T23:59:00+08:00"}}}}
    result = model_response("", payload)
    assert result["arguments"] == {"product_name": "BETA17", "quantity": 6,
                                    "customer": "LYRA22", "due_date": "2026-10-11 09:00"}


def test_dynamic_submit_rejects_missing_current_memory_despite_old_history():
    """当前投影缺客户不能借历史事实或固定标签生成已批准的参数。"""
    payload = {"current_step": {"kind": "tool.write", "capability_refs": ["final_f1.submit@v1"]},
               "provider_execution_view": {"execution_context": {
                   "goal": {"goal": "商品ALPHA91、数量3、明天09:00配送，使用我记住的客户"},
                   "memory_context": [],
                   "time_context": {"anchor_at_utc": "2026-10-03T15:59:00Z",
                                    "local_at": "2026-10-03T23:59:00+08:00"}}},
               "history": MEMORIES}
    with pytest.raises(ValueError, match="CURRENT_FULL_MEMORY_CUSTOMER_MISSING"):
        model_response("", payload)


def test_dynamic_submit_rejects_time_in_history_when_current_anchor_missing():
    """历史时间不是本次Execution冻结时间，当前缺失须显式拒绝派发提案。"""
    payload = {"current_step": {"kind": "tool.write", "capability_refs": ["final_f1.submit@v1"]},
               "provider_execution_view": {"execution_context": {
                   "goal": {"goal": "商品ALPHA91、数量3、明天09:00配送，使用我记住的客户"},
                   "memory_context": [{"content": MEMORIES[0]}]}},
               "history": {"anchor_at_utc": "2026-10-03T15:59:00Z",
                           "local_at": "2026-10-03T23:59:00+08:00"}}
    with pytest.raises(ValueError, match="CURRENT_TIME_CONTEXT_MISSING"):
        model_response("", payload)


def test_published_fixture_schema_projects_actual_http_business_envelope(
    tmp_path, monkeypatch, fixture_resource_engine,
):
    """合法发布schema分别显示异步回调业务值和同步HTTP包装，保留原投影边界。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.config import get_settings
    from app.db.models import Tool
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, project_tool_capability
    from app.dynamic_tasks.execution_context import project_result_for_model

    monkeypatch.setenv("APP_PORT", "19897")
    monkeypatch.setenv("DEMO_MODEL_API_KEY", "f1-controlled-not-a-provider-credential")
    monkeypatch.setenv("DEMO_MODEL_BASE_URL", "http://127.0.0.1:19897/__e2e/f1/openai/v1")
    monkeypatch.setenv("DEMO_MODEL_NAME", "final-f1-controlled-sdk")
    get_settings.cache_clear()
    control = tmp_path / "control"
    control.mkdir()
    seed_resources(control)
    business = {"receipt_id": "current-receipt", "product_name": "ALPHA91", "quantity": 3,
                "customer": "CYGNUS77", "due_date": "2026-10-04 09:00", "finished": True}
    with Session(engine) as db:
        for name in ["final_f1.submit", "final_f1.finish"]:
            tool = db.exec(select(Tool).where(Tool.name == name)).one()
            contract = ToolReliabilityContract.model_validate(tool.reliability_contract_json)
            schema = project_tool_capability(tool, contract).model["output_schema"]
            result = business if name == "final_f1.submit" else {"success": True, "data": business}
            expected = business if name == "final_f1.submit" else {"data": business}
            assert project_result_for_model(result, schema) == expected


def test_planner_preserves_exact_local_published_tool_names():
    """本地普通Tool以name与冻结checksum标识，不能凭空要求名字携带版本后缀。"""
    payload = {"goal": "商品ALPHA91、数量3、明天09:00配送，使用我记住的客户",
               "capabilities": [{"name": "final_f1.submit"}, {"name": "final_f1.finish"}],
               "success_criteria": [{"id": "criterion_01", "description": "取得完整回执"}]}
    plan = model_response("受控动态任务规划器", payload)
    assert [row["capability_refs"] for row in plan["steps"] if row["kind"] == "tool.write"] == [
        ["final_f1.submit"], ["final_f1.finish"]]


def test_async_callback_schema_preserves_original_completed_business_fields(
    tmp_path, monkeypatch, fixture_resource_engine,
):
    """异步回调的原Operation.data是业务对象，不能拿同步HTTP包装schema抹掉回执。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.config import get_settings
    from app.db.models import Tool
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, project_tool_capability
    from app.dynamic_tasks.execution_context import project_result_for_model

    monkeypatch.setenv("APP_PORT", "19897")
    monkeypatch.setenv("DEMO_MODEL_API_KEY", "f1-controlled-not-a-provider-credential")
    monkeypatch.setenv("DEMO_MODEL_BASE_URL", "http://127.0.0.1:19897/__e2e/f1/openai/v1")
    monkeypatch.setenv("DEMO_MODEL_NAME", "f1-controlled-sdk")
    get_settings.cache_clear()
    control = tmp_path / "control"
    control.mkdir()
    seed_resources(control)
    business = {"receipt_id": "actual-callback-receipt", "product_name": "ALPHA91",
                "quantity": 3, "customer": "CYGNUS77", "due_date": "2026-10-04 09:00"}
    with Session(engine) as db:
        tool = db.exec(select(Tool).where(Tool.name == "final_f1.submit")).one()
        schema = project_tool_capability(tool, ToolReliabilityContract.model_validate(
            tool.reliability_contract_json)).model["output_schema"]
        assert project_result_for_model(business, schema) == business
