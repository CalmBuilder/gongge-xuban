"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f1_fullstack_fixture.py
@CallChain  : 显式F1临时服务 → 原SDK/HTTP/Composer → Operation/Inbox/Signal双消费者
@Description: 只准备合成资源及受控远端/模型，保留原业务推进、授权和持久账本。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

from fastapi import Request


TENANT = "tenant_demo"
SKILL = "final_f1_delivery_sop"
AGENTS = {"sop": "agent_f1_sop", "dta": "agent_f1_dta"}
POLICY = "CYGNUS77配送政策：每批上限750件，必须保留办理日期。"
MEMORIES = [
    "客户全名：CYGNUS77，完整名称不可添加说明或截断。",
    "收件说明：先检查外包装，再由指定收件人逐件核验。",
    "汇报偏好：请完整列出客户、商品、数量、目标日期及原始办理回执。",
]
CAPTURED_FACT = "F1 完整持久事实：办理后保存原始回执。"
LOCK = RLock()


def digest(value: object) -> str:
    """生成安全摘要，不归档原请求、回复或配置正文。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def load(control: Path, name: str, default: object) -> object:
    """读取自有测试远端状态；不存在时使用明确初始化值。"""
    path = control / name
    return json.loads(path.read_text()) if path.exists() else default


def save(control: Path, name: str, value: object) -> None:
    """以原子替换保存自有状态，跨真进程重启保留远端效果。"""
    path = control / name
    temp = control / (name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def walk(value: object):
    """遍历当前调用自身的结构化证据，不读取历史会话或数据库来生成答案。"""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def current_context(payload: dict) -> tuple[str, dict]:
    """只读取当前显式阶段或Execution投影的完整事实与冻结时间，拒绝历史补位。"""
    stage = payload.get("_agent_stage") or {}
    frame = (payload.get("provider_execution_view") or {}).get("execution_context") or {}
    memories = payload.get("memory_context")
    if not isinstance(memories, list):
        memories = frame.get("memory_context") or []
    memory = str(stage.get("memory") or "") or "\n".join(
        str(row.get("content") or "") for row in memories if isinstance(row, dict))
    context = payload.get("time_context")
    if not isinstance(context, dict):
        context = frame.get("time_context") or {}
    return memory, context


def current_user_content(payload: dict) -> str:
    """原提取快照的最后用户轮次是当前来源，不从历史、助手或既有事实补位。"""
    rows = (payload.get("conversation_context") or {}).get("messages") or []
    return next((str(row.get("content") or "") for row in reversed(rows)
                 if isinstance(row, dict) and row.get("role") == "user"), "")


def capture_payload_from_wire(messages: list[dict], payload: dict) -> dict:
    """将原SDK提取请求的角色历史与当前控制包装还原给受控模型。"""
    decoded = dict(payload)
    history = messages
    prefix = "本轮输入（仅用于当前调用，不写入对话历史）：\n"
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, str) and content.startswith(prefix):
            control = json.loads(content.removeprefix(prefix))
            if not isinstance(control, dict):
                raise ValueError("F1_CAPTURE_CONTROL_OBJECT_REQUIRED")
            decoded.update(control)
            history = messages[:-1]
    decoded["conversation_context"] = {"messages": [
        {"role": row["role"], "content": row["content"]}
        for row in history if row.get("role") in {"user", "assistant"}
        and isinstance(row.get("content"), str)
    ]}
    return decoded


def current_service_fact(payload: dict) -> str:
    """只从当前完整记忆投影取实际稳定服务内容，缺失时拒绝历史补答案。"""
    memory, _time = current_context(payload)
    fact = re.search(r"F1 完整持久事实：[^\n]+", memory)
    if fact is None:
        raise ValueError("F1_CURRENT_CAPTURED_MEMORY_MISSING")
    return fact.group(0).strip()


def dynamic_delivery_arguments(payload: dict) -> dict:
    """从当前计划目标、已选客户事实和原时间推导参数，不读取历史或数据库答案。"""
    frame = (payload.get("provider_execution_view") or {}).get("execution_context") or {}
    goal = frame.get("goal") or {}
    text = str(goal.get("goal") or "") if isinstance(goal, dict) else str(goal)
    product = re.search(r"商品\s*([A-Z0-9]+)", text)
    quantity = re.search(r"数量\s*(\d+)", text)
    if product is None or quantity is None:
        raise ValueError("F1_CURRENT_DELIVERY_GOAL_INCOMPLETE")
    memory, context = current_context(payload)
    customer = re.search(r"客户全名：([^，\n]+)", memory)
    if customer is None:
        raise ValueError("F1_CURRENT_FULL_MEMORY_CUSTOMER_MISSING")
    local = context.get("local_at")
    if not isinstance(local, str):
        raise ValueError("F1_CURRENT_TIME_CONTEXT_MISSING")
    due = (datetime.fromisoformat(local) + timedelta(days=1)).date().isoformat()
    return {"product_name": product.group(1), "quantity": int(quantity.group(1)),
            "customer": customer.group(1), "due_date": due + " 09:00"}


def provider_output_schema(*, detached: bool = False) -> dict:
    """异步回调直接业务值、同步HTTP原data包装分别声明，宿主投影不放宽。"""
    business = {"type": "object", "properties": {
        **{key: {"type": "string"} for key in
           ["receipt_id", "product_name", "customer", "due_date"]},
        "quantity": {"type": "integer"}, "finished": {"type": "boolean"}}}
    return business if detached else {"type": "object", "properties": {"data": business}}


def model_response(system: str, payload: dict) -> object:
    """受控模型只提议原阶段合法响应，执行结果必须来自当前模型输入。"""
    stage = payload.get("_agent_stage", {})
    phase = stage.get("phase", "") if isinstance(stage, dict) else ""
    system += "\n" + str(stage.get("instructions") or "")
    message = str(payload.get("user_message") or payload.get("goal") or "")
    _memory, context = current_context(payload)
    local = context.get("local_at")
    due = ((datetime.fromisoformat(local) + timedelta(days=1)).date().isoformat()
           if isinstance(local, str) else None)
    if "长期记忆抽取" in system:
        content = current_user_content(payload)
        prefix = "请记住我的稳定服务偏好："
        explicit = content.removeprefix(prefix).strip() if content.startswith(prefix) else ""
        return {"memories": ([{"operation": "upsert", "kind": "preference",
                               "key": "f1_capture", "content": explicit,
                               "importance": 0.8}] if explicit else []), "updated_summary": ""}
    if "文档路由助手" in system:
        return {"selected_document_ids": [row["id"] for row in payload.get("documents", [])]}
    if "selected_bucket_ids" in system and "buckets" in payload:
        return {"selected_bucket_ids": [row["id"] for row in payload.get("buckets", [])]}
    if phase == "Router":
        if message.startswith("请记住我的稳定服务偏好："):
            return {"decision": "answer_only", "confidence": 0.99,
                    "reason": "仅确认稳定服务偏好，本轮不创建业务办理"}
        if message.startswith("F1事实消费："):
            return {"decision": "answer_only", "confidence": 0.99,
                    "user_intent": "说明当前稳定服务要求：" + current_service_fact(payload),
                    "reason": "当前Router完整事实支持本轮咨询"}
        session = payload.get("current_session") or {}
        active_id = session.get("active_skill_id")
        hints = dict(session.get("slots") or {}) if active_id else {}
        if "数量3" in message:
            hints["quantity"] = 3
        if "明天09:00" in message:
            if due is None:
                raise ValueError("F1_CURRENT_TIME_CONTEXT_MISSING")
            hints["due_date"] = due + " 09:00"
        if "客户CYGNUS77" in message:
            hints["customer"] = "CYGNUS77"
        elif "记住的客户" in message:
            matched = re.search(r"客户全名：([^，\n]+)", str(stage.get("memory") or ""))
            if matched is None:
                raise ValueError("F1_CURRENT_FULL_MEMORY_CUSTOMER_MISSING")
            hints["customer"] = matched.group(1)
        if "ALPHA91" in message:
            hints["product_name"] = "ALPHA91"
        return {"decision": "continue_active" if active_id and "再次新办" not in message else "start_new_task",
                "target_skill_id": SKILL, "confidence": 0.99,
                "slot_hints": hints, "reason": "受控配送流程，确认仍由宿主原节点核验"}
    if phase == "Router / General Skill Selector":
        return {"use_general_skill": False, "selected_slug": None, "use_knowledge": False,
                "knowledge_query": None, "knowledge_mode": "disabled", "confidence": 0.99,
                "reason": "本轮使用原持久执行"}
    if phase in {"Router / Dynamic Task", "Router / Dynamic Task Shadow"}:
        if message.startswith("请记住我的稳定服务偏好："):
            return {"mode": "answer", "goal": None, "success_criteria": [],
                    "requires_durable_execution": False, "requires_artifact": False,
                    "capability_hints": [], "execution_intent": "none", "confidence": 0.99,
                    "reason": "显式长期偏好由原完成事务入队，本轮不创建业务计划"}
        criteria = (["准确解释当前完整稳定服务要求，不执行配送"]
                    if message.startswith("F1事实消费：") else
                    ["办理配送并取得完整原始回执及知识引用"])
        return {"mode": "dynamic_task", "goal": message,
                "success_criteria": criteria,
                "requires_durable_execution": True, "requires_artifact": False,
                "capability_hints": [], "clarification": None, "execution_intent": "new_task",
                "confidence": 0.99, "reason": "需要澄清、批准、异步等待及可核验成果"}
    if phase == "Reflection":
        return {"action": "pass", "needs_retry": False, "reason": "使用原Runtime核验",
                "target_skill_id": None, "target_step_id": None, "target_tool_name": None}
    if phase == "Step Agent":
        if message.startswith("请记住我的稳定服务偏好："):
            return {"action": "reply", "reply": "稳定服务偏好已接收，等待后台结算。",
                    "slot_updates": {}, "is_step_completed": True}
        active = payload.get("active_skill") or {}
        step = active.get("current_step") or {}
        node = step.get("node_id") or step.get("step_id")
        slots = payload.get("slots") or {}
        reply = "请补充商品名称，再确认办理。"
        if node == "confirm":
            reply = "请回复确认办理；此前不会提交。"
        elif node == "done":
            reply = response_text(payload)
        return {"action": "reply" if node == "done" else "ask_user", "reply": reply,
                "slot_updates": {}, "tool_call": None, "knowledge_query": None,
                "knowledge_results": [], "next_step_id": None,
                "is_step_completed": node == "done", "handoff": False,
                **({"slot_updates": {"product_name": "ALPHA91"}}
                   if "ALPHA91" in message and not slots.get("product_name") else {})}
    if "动态任务指导选择器" in system:
        return {"selected_skill_names": [], "reason": "只使用原明确业务能力"}
    if "受控动态任务规划器" in system:
        if message.startswith("F1事实消费："):
            current_service_fact(payload)
            return {"goal": message, "success_criteria": payload.get("success_criteria", []),
                    "constraints": [], "assumptions": [], "steps": [{
                        "draft_id": "answer", "title": "F1稳定服务要求", "kind": "answer",
                        "required": True, "depends_on": [], "capability_refs": [],
                        "guidance_skill_refs": [], "expected_output_schema": {}}],
                    "expected_artifacts": []}
        capabilities = payload.get("capabilities") or []
        names = [str(row.get("name") or "") for row in capabilities]
        submit = next(name for name in names if name.partition("@")[0] == "final_f1.submit")
        finish = next(name for name in names if name.partition("@")[0] == "final_f1.finish")
        steps = []
        for key, kind, refs in [("clarify", "clarification", []),
                                ("policy", "knowledge", ["knowledge.search"]),
                                ("submit", "tool.write", [submit]),
                                ("finish", "tool.write", [finish]), ("answer", "answer", [])]:
            steps.append({"draft_id": key, "title": "F1 " + key, "kind": kind,
                          "required": True, "depends_on": [steps[-1]["draft_id"]] if steps else [],
                          "capability_refs": refs, "guidance_skill_refs": [],
                          "expected_output_schema": {}})
        return {"goal": message, "success_criteria": payload.get("success_criteria", []),
                "constraints": [], "assumptions": [], "steps": steps, "expected_artifacts": []}
    view = payload.get("provider_execution_view") or {}
    current = payload.get("current_step") or view.get("current_step") or {}
    frame = view.get("execution_context") or {}
    if "受控动作" in system or current:
        kind = current.get("kind")
        base = {"capability_ref": None, "expected_output_schema": {}, "rationale": "原事实驱动"}
        if kind == "clarification":
            return {**base, "action_kind": "wait_input",
                    "arguments": {"question": "请确认商品ALPHA91、数量3及明天09:00配送",
                                  "options": ["确认配送参数"]}}
        if kind == "knowledge":
            return {**base, "action_kind": "query_knowledge", "capability_ref": "knowledge.search",
                    "arguments": {"query": "CYGNUS77配送政策", "desired_evidence": "CYGNUS77每批上限"}}
        if kind == "tool.write":
            ref = current["capability_refs"][0]
            if ref.partition("@")[0] == "final_f1.finish":
                receipt = next((row.get("receipt_id") for step in frame.get("completed_steps", [])
                                for row in walk(step.get("model_output"))
                                if row.get("receipt_id")), None)
                if receipt is None:
                    raise ValueError("F1_CURRENT_COMPLETED_RECEIPT_MISSING")
                arguments = {"receipt_id": receipt}
            else:
                arguments = dynamic_delivery_arguments(payload)
            return {**base, "action_kind": "call_tool", "arguments": arguments, "capability_ref": ref}
        if kind == "answer":
            completed = [row["step_key"] for row in frame.get("completed_steps", [])]
            criteria = [row["id"] for row in frame.get("success_criteria", [])]
            markdown = response_text(frame)
            goal = frame.get("goal") or {}
            if isinstance(goal, dict) and str(goal.get("goal") or "").startswith("F1事实消费："):
                markdown = current_service_fact(payload)
                completed = [str(current["step_key"])]
            return {**base, "action_kind": "answer", "arguments": {
                "markdown": markdown, "criterion_evidence": {k: completed for k in criteria},
                "pending_questions": []}}
    if phase or "回复" in system or "回答" in system:
        if message.startswith("请记住我的稳定服务偏好："):
            return "稳定服务偏好已接收，等待后台结算。"
        if message.startswith("F1事实消费："):
            # 原SD6A只有Router消费SOP记忆；answer_only不会经过Step，也不再注入回复阶段。
            return "本轮仅咨询稳定服务要求，未办理新的配送。"
        return response_text(payload)
    raise ValueError("F1_UNHANDLED_CONTROLLED_MODEL_PHASE")


def response_text(payload: dict) -> str:
    """只从当前模型输入拼原知识、业务回执；缺失时明确未取得证据。"""
    if "completed_steps" in payload:
        rows = [row for step in payload["completed_steps"] for row in walk(step.get("model_output"))]
    else:
        rows = list(walk(payload.get("tool_result") or payload.get("knowledge_results") or {}))
    receipts = [row for row in rows if row.get("receipt_id")]
    text = [str(row.get("content") or row.get("excerpt") or row.get("content_md") or "")
            for row in rows]
    policy = next((value for value in text if "CYGNUS77" in value and "750" in value), None)
    if not receipts:
        return "尚未取得原始业务回执，请等待原任务完成。"
    receipt = receipts[-1]
    return (f"配送回执 {receipt.get('receipt_id')}：商品{receipt.get('product_name')}，"
            f"数量{receipt.get('quantity')}，客户{receipt.get('customer')}，"
            f"目标日期{receipt.get('due_date')}。\n" + (policy or "当前模型输入没有完整政策证据。"))


def install_clock(control: Path) -> None:
    """只在原输入冻结函数给合成时刻；不改数据库时间、租约或任务截止。"""
    from app.core import time_context
    original = time_context.freeze_time_context

    def freeze(**kwargs):
        """显式既有锚点原样保持，只有新输入取受控时钟。"""
        if kwargs.get("anchor_at_utc") is None:
            value = load(control, "clock.json", "2026-10-03T15:59:00+00:00")
            kwargs["anchor_at_utc"] = datetime.fromisoformat(value).astimezone(UTC)
        return original(**kwargs)

    time_context.freeze_time_context = freeze
    for module in ("app.api.chat", "app.core.agent_loop", "app.scheduled_tasks.service"):
        import importlib
        target = importlib.import_module(module)
        if hasattr(target, "freeze_time_context"):
            target.freeze_time_context = freeze


def seed_resources(control: Path) -> None:
    """仅种合成账号、定义、固定知识和长期资源，原运行账本须为空。"""
    from sqlmodel import Session, select
    from app.agents.branching import (_ensure_binding, ensure_open_gallery_binding,
                                    sync_knowledge_branch_from_overall)
    from app.api.knowledge_bases import create_knowledge_base
    from app.api.skills import create_skill
    from app.db import engine, init_db
    from app.db.models import (AgentProfile, AgentUsage, KnowledgeBucket, KnowledgeChunk,
                               KnowledgeBase, KnowledgeDocument, MemoryRecord, ModelConfig, SopInstance,
                               SopOperation, Tool, User)
    from app.db.seed import seed_demo_data
    from app.dynamic_tasks.capability_catalog import (ToolReliabilityContract, capability_checksum,
                                                      publish_tool_contract)
    from app.knowledge.schema import KnowledgeBaseCreateRequest
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
        from app.security.auth import hash_password
        db.add(User(id="f1_reviewer", tenant_id=TENANT, username="f1_reviewer",
                    display_name="F1 独立批准人", role="admin",
                    password_hash=hash_password("f1-reviewer-synthetic-password")))
        for kind, agent in AGENTS.items():
            db.add(AgentProfile(id=agent, tenant_id=TENANT, name="F1 " + kind.upper() + "员工",
                                owner_user_id="admin", status="active"))
            db.add(AgentUsage(tenant_id=TENANT, user_id="admin", agent_id=agent))
            db.add(AgentUsage(tenant_id=TENANT, user_id="f1_reviewer", agent_id=agent))
        db.commit()
        kb_read = create_knowledge_base(KnowledgeBaseCreateRequest(tenant_id=TENANT, name="F1配送政策"),
                                   db=db, current_user=db.get(User, "admin"), agent_id=None)
        kb = db.get(KnowledgeBase, kb_read.id)
        assert kb is not None
        from app.agents.branching import ensure_knowledge_base_version
        version = ensure_knowledge_base_version(db, kb)
        document = KnowledgeDocument(tenant_id=TENANT, knowledge_base_id=kb.id,
                                     knowledge_base_version_id=version.id, filename="cygnus77.md",
                                     file_type="md", title="CYGNUS77配送政策", status="ready",
                                     normalized_source=POLICY, source_checksum=hashlib.sha256(POLICY.encode()).hexdigest(),
                                     bucket_count=1, chunk_count=1)
        db.add(document)
        db.flush()
        bucket = KnowledgeBucket(tenant_id=TENANT, knowledge_base_id=kb.id,
                                 knowledge_base_version_id=version.id, document_id=document.id,
                                 bucket_key="policy", title="CYGNUS77配送政策", summary=POLICY)
        db.add(bucket)
        db.flush()
        db.add(KnowledgeChunk(tenant_id=TENANT, knowledge_base_id=kb.id,
                              knowledge_base_version_id=version.id, document_id=document.id,
                              bucket_id=bucket.id, chunk_index=0, content=POLICY,
                              source_ref="cygnus77.md"))
        for agent in AGENTS.values():
            _ensure_binding(db, TENANT, agent, "knowledge_base", kb.id, "active")
            sync_knowledge_branch_from_overall(db, TENANT, agent, kb.id)
            for i, content in enumerate(MEMORIES):
                db.add(MemoryRecord(tenant_id=TENANT, agent_id=agent, agent_scope_key=agent,
                                    user_id="admin", username="admin", kind="preference",
                                    content=content, importance=0.8,
                                    metadata_json={"key": "f1_fact_" + str(i)}))
        for name, write in [("submit", True), ("finish", False)]:
            output_prefix = "output." if write else "output.data."
            properties = ({"product_name": {"type": "string"}, "quantity": {"type": "integer"},
                           "customer": {"type": "string"}, "due_date": {"type": "string"}}
                          if write else {"receipt_id": {"type": "string"}})
            contract = {"risk_class": "external_write", "side_effect": "external",
                        "confirmation_policy": "once", "timeout_policy": "unknown",
                        "idempotency": {"mode": "request_key", "remote_scope": "f1-provider"},
                        "reconcile": {"supported": True, "tool_name": "final_f1." + name,
                                      "reference_source": output_prefix + "receipt_id",
                                      "terminal_status_mapping": {"completed": "complete"}},
                        "dynamic_task_enabled": True,
                        "model_visibility": {"allowed_paths": ["input." + p for p in properties] +
                                              [output_prefix + p for p in ["receipt_id", "product_name",
                                                                       "quantity", "customer", "due_date", "finished"]],
                                             "user_display_paths": [output_prefix + "receipt_id"], "audit_only_paths": []}}
            if write:
                contract.update(execution_mode="detached", async_strategy="provider_task",
                                status_url=f"http://127.0.0.1:{os.environ['APP_PORT']}/__e2e/f1/provider/status/{{taskId}}",
                                status_mapping={"accepted": "working", "queued": "working"},
                                poll_interval_seconds=1, max_tracking_seconds=300)
            tool = Tool(id="tool_f1_" + name, tenant_id=TENANT, name="final_f1." + name,
                        display_name="F1 " + name, description="隔离回归业务供应商",
                        method="POST", url="/__e2e/f1/provider/" + name,
                        input_schema={"type": "object", "properties": properties,
                                      "required": list(properties), "additionalProperties": False},
                        output_schema=provider_output_schema(detached=write),
                        reliability_contract_json=ToolReliabilityContract.model_validate(contract).model_dump(mode="json"),
                        enabled=True)
            publish_tool_contract(tool, ToolReliabilityContract.model_validate(contract))
            db.add(tool)
            db.flush()
            ensure_open_gallery_binding(db, TENANT, "tool", tool.id, "active")
            for agent in AGENTS.values():
                _ensure_binding(db, TENANT, agent, "tool", tool.id, "active")
        db.commit()
        nodes = [
            {"node_id": "policy", "type": "knowledge_query", "name": "CYGNUS77配送政策",
             "allowed_actions": ["knowledge_query"], "metadata": {"operation_result_key": "policy",
              "knowledge_query": {"query_type": "policy_check", "desired_evidence": "CYGNUS77每批上限"}}},
            {"node_id": "collect", "type": "collect_info", "name": "补商品",
             "expected_user_info": ["product_name", "quantity", "customer", "due_date"],
             "allowed_actions": ["ask_user", "continue_flow"]},
            {"node_id": "confirm", "type": "collect_info", "name": "明确确认",
             "expected_user_info": ["confirmed"], "allowed_actions": ["ask_user", "continue_flow"],
             "metadata": {"confirmation_policy": {"slot_name": "confirmed",
                           "phrase_values": {"确认办理": "yes"}, "prompt": "请回复确认办理；此前不会提交。"}}},
            {"node_id": "submit", "type": "tool_call", "name": "配送异步提交",
             "allowed_actions": ["call_tool:final_f1.submit"], "metadata": {"operation_result_key": "delivery",
              "operation_input": {key: "slots." + key for key in ["product_name", "quantity", "customer", "due_date"]}}},
            {"node_id": "finish", "type": "tool_call", "name": "后继业务登记",
             "allowed_actions": ["call_tool:final_f1.finish"], "metadata": {"operation_result_key": "finish",
              "operation_input": {"receipt_id": "tool_result.delivery.data.data.receipt_id"}}},
            {"node_id": "done", "type": "response", "name": "原回执回答", "allowed_actions": ["answer_user"]}]
        card = SkillCard.model_validate({"skill_id": SKILL, "name": "F1配送SOP", "version": "1.0.0",
                 "execution_mode": "deterministic", "description": "CYGNUS77配送原始业务闭环",
                 "condition_schemas": {"slots": {"type": "object", "properties": {
                     "product_name": {"type": "string"}, "customer": {"type": "string"},
                     "due_date": {"type": "string"}, "quantity": {"type": "integer"},
                     "confirmed": {"type": "string"}}}, "tool_result": {"type": "object",
                    "properties": {"delivery": {"type": "object", "properties": {
                        "data": {"type": "object", "properties": {"data": {"type": "object",
                            "properties": {"receipt_id": {"type": "string"}}}}}}}}}}, "nodes": nodes,
                 "edges": [{"source_node_id": a["node_id"], "next_node_id": b["node_id"]}
                           for a, b in zip(nodes, nodes[1:])],
                 "start_node_id": "policy", "terminal_node_ids": ["done"]})
        created = create_skill(SkillCreateRequest(tenant_id=TENANT, content=card, status="published"),
                               agent_id=AGENTS["sop"], db=db, current_user=db.get(User, "admin"))
        caps = {"protocol_version": "dynamic-v1", "sdk_available": True, "credentials_verified": True,
                "structured_output": True, "tool_calling": True, "vision": False, "pdf_input": False}
        model = db.exec(select(ModelConfig).where(ModelConfig.is_default == True)).first()  # noqa: E712
        model.provider = "openai_compatible"
        model.base_url = f"http://127.0.0.1:{os.environ['APP_PORT']}/__e2e/f1/openai/v1"
        model.model = "f1-controlled-protocol"
        from app.security.encryption import encrypt_secret
        model.api_key_encrypted = encrypt_secret("f1-synthetic-model-key")
        model.capability_snapshot_json = caps
        model.capability_checksum = capability_checksum(caps)
        model.preflight_status = "ready"
        db.add(model)
        db.commit()
        assert not db.exec(select(SopInstance)).all() and not db.exec(select(SopOperation)).all()
        save(control, "initial.safe.json", {"agents": AGENTS, "skill_id": created.skill_id,
                "knowledge_base_id": kb.id, "knowledge_base_version_id": version.id,
                "document_id": document.id, "policy_hash": digest(POLICY),
                "memory_hashes": [digest(value) for value in MEMORIES],
                "initial_instances": 0, "initial_operations": 0, "real_provider_quality": False})


def snapshot(control: Path) -> dict:
    """白名单只读本测试两员工的实际账本，原自由正文仅记SHA。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (AgentProfile, ChatSession, ExecutionPlanRevision, ExecutionSignal, ExternalTaskEventInbox,
                               ActionProposalRecord, AgentEvent, ExecutionCommand, ExecutionResult,
                               KnowledgeChunk, KnowledgeDocument, MemoryCaptureJob, MemoryRecord, Message, SopInstance, SopNodeExecution,
                               SopWorkItem,
                               SopOperation, SopOperationAttempt)
    with Session(engine) as db:
        def business(value: object) -> dict:
            """只取原请求/回执的已登记业务字段，其他正文永不进入档案。"""
            return {key: item for key, item in (value.items() if isinstance(value, dict) else [])
                    if key in {"receipt_id", "product_name", "quantity", "customer", "due_date", "finished"}}

        def message_row(row) -> dict:
            """精确提取原消息来源/引用物理身份与正文hash，不展开私密metadata。"""
            meta = row.metadata_json or {}
            citations = []
            for citation in meta.get("knowledge_citations") or []:
                doc = db.get(KnowledgeDocument, citation.get("document_id"))
                citations.append({key: citation.get(key) for key in
                    ["id", "kind", "document_id", "bucket_id", "chunk_id"]} | {
                    "version_id": doc.knowledge_base_version_id if doc else None,
                    "tenant_valid": bool(doc and doc.tenant_id == TENANT),
                    "body_hash": digest(citation.get("content")),
                    "policy_complete": POLICY in str(citation.get("content") or "")})
            return {"id": row.id, "tenant_id": row.tenant_id, "session_id": row.session_id,
                    "role": row.role, "created_at": row.created_at, "content_hash": digest(row.content),
                    "captured_fact_in_answer": CAPTURED_FACT in row.content,
                    "metadata": {key: meta.get(key) for key in ["time_context", "user_message_id",
                        "turn_id", "client_turn_id", "execution_id", "result_id", "message_kind", "execution_engine"]},
                    "citations": citations}

        instances = db.exec(select(SopInstance).where(SopInstance.agent_id.in_(list(AGENTS.values())))).all()
        ids = [row.id for row in instances]
        rows = []
        for row in instances:
            plan = db.get(ExecutionPlanRevision, row.current_plan_revision_id) if row.current_plan_revision_id else None
            rows.append({"id": row.id, "tenant_id": row.tenant_id, "session_id": row.session_id, "agent_id": row.agent_id,
                         "initiator_user_id": row.initiator_user_id, "kind": row.kind, "status": row.status,
                         "revision": row.revision, "effect_state": row.effect_state,
                         "cancellation_disposition": row.cancellation_disposition,
                         "cancellation_requested_at": row.cancellation_requested_at,
                         "cancellation_requested_by": row.cancellation_requested_by,
                         "skill_id": row.skill_id, "skill_version_id": row.skill_version_id,
                         "definition_checksum": row.definition_checksum, "current_node_id": row.current_node_id,
                         "plan_revision_id": row.current_plan_revision_id,
                         "plan_checksum": plan.checksum if plan else None,
                         "plan_steps": [{key: step.get(key) for key in
                                         ["step_key", "kind", "capability_refs", "depends_on"]}
                                        for step in (plan.plan_json or {}).get("steps", [])] if plan else [],
                         "capability_checksum": plan.capability_checksum if plan else None,
                         "metadata": {k: v for k, v in (row.context_json or {}).items() if k in {"time_context", "source_input_id", "source_message_id"}},
                         "slots": {k: v for k, v in (row.slots_json or {}).items()
                                   if k in {"product_name", "quantity", "due_date", "customer", "confirmed"}}})
        ops = db.exec(select(SopOperation).where(SopOperation.instance_id.in_(ids))).all() if ids else []
        operations = [{"id": row.id, "instance_id": row.instance_id, "node_execution_id": row.node_execution_id,
                       "operation_name": row.operation_name, "status": row.status, "effect_kind": row.effect_kind,
                       "effect_state": row.effect_state, "cancellation_disposition": row.cancellation_disposition,
                       "logical_action_id": row.logical_action_id, "request_fingerprint": row.request_fingerprint,
                       "remote_idempotency_key_hash": digest(row.remote_idempotency_key),
                       "idempotency_key_hash": digest(row.idempotency_key),
                       "approved_by_user_id": row.approved_by_user_id,
                       "approved_at": row.approved_at,
                       "capability_checksum": row.capability_checksum,
                       "external_submission_id": row.external_submission_id, "external_observation_revision": row.external_observation_revision,
                       "request_hash": digest(row.request_json), "external_task_id": row.external_task_id,
                       "result_hash": digest(row.result_json),
                       "result": next((business(value) for value in walk(row.result_json)
                                       if value.get("receipt_id")), {}),
                       "error_code": (row.error_json or {}).get("code"),
                       "knowledge_sources": [{k: value.get(k) for k in ["document_id", "bucket_id", "chunk_id", "knowledge_base_version_id"]}
                            | {"version_id": (doc.knowledge_base_version_id if (doc := db.get(KnowledgeDocument, value.get("document_id"))) else None),
                               "body_hash": digest(chunk.content if (chunk := db.get(KnowledgeChunk, value.get("chunk_id"))) else None),
                               "body_source": "physical_chunk",
                               "policy_complete": bool(chunk and POLICY in chunk.content)}
                            for value in walk(row.result_json) if value.get("document_id")],
                       "request": business(row.request_json)} for row in ops]
        signals = db.exec(select(ExecutionSignal).where(ExecutionSignal.execution_id.in_(ids))).all() if ids else []
        events = db.exec(select(ExternalTaskEventInbox).where(ExternalTaskEventInbox.operation_id.in_([r.id for r in ops]))).all() if ops else []
        sessions = db.exec(select(ChatSession).where(ChatSession.agent_id.in_(list(AGENTS.values())))).all()
        session_ids = [row.id for row in sessions]
        messages = db.exec(select(Message).where(Message.session_id.in_(session_ids)).order_by(Message.created_at, Message.id)).all() if sessions else []
        agent_events = db.exec(select(AgentEvent).where(AgentEvent.session_id.in_(session_ids))).all() if sessions else []
        jobs = db.exec(select(MemoryCaptureJob).where(MemoryCaptureJob.agent_id.in_(list(AGENTS.values())))).all()
        attempts = db.exec(select(SopOperationAttempt).where(SopOperationAttempt.operation_id.in_([r.id for r in ops]))).all() if ops else []
        nodes = db.exec(select(SopNodeExecution).where(SopNodeExecution.instance_id.in_(ids))).all() if ids else []
        memories = db.exec(select(MemoryRecord).where(MemoryRecord.agent_id.in_(list(AGENTS.values())))).all()
        proposals = db.exec(select(ActionProposalRecord).where(ActionProposalRecord.execution_id.in_(ids))).all() if ids else []
        results = db.exec(select(ExecutionResult).where(ExecutionResult.execution_id.in_(ids))).all() if ids else []
        work_items = db.exec(select(SopWorkItem).where(SopWorkItem.instance_id.in_(ids))).all() if ids else []
        commands = db.exec(select(ExecutionCommand).where(ExecutionCommand.execution_id.in_(ids))).all() if ids else []
        provider = load(control, "provider.json", {"submits": [], "finishes": []})
        agents = db.exec(select(AgentProfile).where(AgentProfile.id.in_(list(AGENTS.values())))).all()
        return {"pid": os.getpid(), "instances": rows, "operations": operations,
                "agents": [{"id": r.id, "tenant_id": r.tenant_id, "status": r.status,
                            "profile_revision": r.profile_revision,
                            "memory_capture_generation": r.memory_capture_generation,
                            "updated_at": r.updated_at} for r in agents],
                "sessions": [{"id": r.id, "tenant_id": r.tenant_id, "user_id": r.user_id,
                              "agent_id": r.agent_id, "status": r.status,
                              "active_skill_id": r.active_skill_id, "active_step_id": r.active_step_id,
                              "has_pending_tasks": bool(r.pending_tasks_json),
                              "has_awaiting_input": bool(r.awaiting_input_json)} for r in sessions],
                "messages": [message_row(r) for r in messages],
                "nodes": [{"id": r.id, "instance_id": r.instance_id, "node_id": r.node_id, "status": r.status} for r in nodes],
                "attempts": [{"id": r.id, "operation_id": r.operation_id, "status": r.status,
                              "attempt_number": r.attempt_number} for r in attempts],
                "signals": [{"id": r.id, "execution_id": r.execution_id, "signal_type": r.signal_type,
                             "status": r.status, "causation_id": r.causation_id,
                             "dedupe_key": r.dedupe_key, "consumed_at": r.consumed_at} for r in signals],
                "inbox": [{"id": r.id, "operation_id": r.operation_id, "status": r.status,
                           "provider_event_id": r.provider_event_id,
                           "provider_task_id": r.provider_task_id, "observation_revision": r.observation_revision,
                           "external_submission_id": r.external_submission_id, "signal_id": r.signal_id,
                           "payload_checksum": r.payload_checksum,
                           "error_code": (r.last_error_json or {}).get("code")} for r in events],
                "jobs": [{"job_id": r.id, "tenant_id": r.tenant_id, "user_id": r.user_id,
                          "agent_id": r.agent_id, "source_input_id": r.source_message_id, "assistant_message_id": r.source_assistant_message_id,
                          "status": r.status, "attempt_count": r.attempt_count,
                          "model_dispatch_count": r.model_dispatch_count, "applied_count": r.applied_count,
                          "fencing_token": r.fencing_token, "source_sequence": r.source_sequence,
                          "source_hash": r.source_hash, "user_generation": r.user_generation,
                          "agent_generation": r.agent_generation, "user_permission_generation": r.user_permission_generation,
                          "agent_permission_generation": r.agent_permission_generation,
                          "agent_capture_generation": r.agent_capture_generation,
                          "model_contract_hash": r.model_contract_hash, "prompt_hash": r.prompt_hash} for r in jobs],
                "memories": [{"id": r.id, "agent_id": r.agent_id, "user_id": r.user_id,
                              "content_hash": digest(r.content), "capture_job_id": r.capture_job_id,
                              "source_hash": r.source_hash, "source_sequence": r.source_sequence,
                              "complete_initial_fact": r.content in MEMORIES,
                              "captured_fact": r.content == CAPTURED_FACT} for r in memories],
                "proposals": [{"id": r.id, "execution_id": r.execution_id, "plan_revision_id": r.plan_revision_id,
                               "step_key": r.step_key, "status": r.status, "proposal_checksum": r.proposal_checksum,
                               "consumed_operation_id": r.consumed_operation_id} for r in proposals],
                "results": [{"id": r.id, "execution_id": r.execution_id, "status": r.status,
                             "checksum": r.checksum, "result_hash": digest(r.result_json),
                             "criterion_evidence": (r.result_json or {}).get("criterion_evidence", {}),
                             "verification_passed": (r.verification_json or {}).get("passed"),
                             "verification_error_count": len((r.verification_json or {}).get("errors", [])),
                             "contains_current_receipt": any(remote.get("receipt_id") in str(r.result_json)
                                                             for remote in provider["submits"]
                                                             if remote.get("receipt_id")),
                             "verification_hash": digest(r.verification_json)} for r in results],
                "work_items": [{"id": r.id, "instance_id": r.instance_id, "status": r.status,
                                "attention_kind": r.attention_kind, "title": r.title,
                                "allowed_commands": r.allowed_commands_json,
                                "resolution_command": (r.resolution_json or {}).get("command"),
                                "resolution_hash": digest(r.resolution_json)}
                               for r in work_items],
                "commands": [{"id": r.id, "execution_id": r.execution_id, "command_type": r.command_type,
                              "command_id": r.command_id, "actor_user_id": r.actor_user_id,
                              "expected_revision": r.expected_execution_revision,
                              "status": r.status, "result_plan_revision_id": r.result_plan_revision_id,
                              "result_cancelled": (r.result_json or {}).get("cancelled"),
                              "result_execution_status": (r.result_json or {}).get("execution_status"),
                              "issued_at": r.issued_at, "consumed_at": r.consumed_at} for r in commands],
                "events": [{"id": r.id, "session_id": r.session_id, "event_type": r.event_type,
                            "aggregate_id": r.aggregate_id, "causation_id": r.causation_id,
                            **{key: (r.payload_json or {}).get(key) for key in [
                                "user_message_id", "source_message_id", "assistant_message_id", "job_id",
                                "command_id", "execution_status", "cancellation_disposition", "effect_state"]},
                            "payload_hash": digest(r.payload_json),
                            "source": (r.payload_json or {}).get("source"),
                            "error_code": ((r.payload_json or {}).get("code")
                                if re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}",
                                                str((r.payload_json or {}).get("code") or "")) else None),
                            "error_type": next(iter(re.findall(
                                r"\n([A-Za-z][A-Za-z0-9_.]*Error):",
                                str((r.payload_json or {}).get("error_traceback") or ""))), None),
                            "error_source_frames": re.findall(
                                r'File "[^"\n]*/backend/(app/[^"\n]+\.py)", line (\d+)',
                                str((r.payload_json or {}).get("error_traceback") or "")),
                            "idempotency_key_hash": digest((r.payload_json or {}).get("idempotency_key"))}
                           for r in agent_events],
                "provider": {"submits": [{k: r.get(k) for k in ["task_id", "key_hash", "status", "post_count",
                                "receipt_id", "effect_completed", "callback_header_received", "callback_key_hash"]}
                                | {"request": business(r.get("request"))} for r in provider["submits"]],
                             "finishes": [{k: r.get(k) for k in ["receipt_id", "request_hash"]}
                                          for r in provider["finishes"]]},
                "sdk": load(control, "sdk.safe.json", [])}


def install_routes(control: Path) -> None:
    """受控远端仅改自身状态，回调仍走真实认证API及原Inbox/Signal。"""
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from app.main import app

    async def model(request: Request):
        """真正SDK HTTP请求经原JSON/stream协议返回；仅存摘要和固定事实布尔。"""
        body = await request.json()
        payload = {}
        messages = body.get("messages", [])
        system = "\n".join(str(row.get("content") or "") for row in messages if row.get("role") == "system")
        for row in messages:
            if row.get("role") != "user" or not isinstance(row.get("content"), str):
                continue
            try:
                candidate = json.loads(row["content"])
                if isinstance(candidate, dict):
                    payload = candidate
            except ValueError:
                wire = row["content"]
                if "当前阶段独有内容：\n" in wire:
                    sections = wire.split("\n\n")
                    def section(label: str) -> str:
                        """只解码原统一协议段，合成输入不含段标签。"""
                        return next((part[len(label) + 2:] for part in sections
                                     if part.startswith(label + "：\n")), "")
                    payload = json.loads(section("当前阶段独有内容"))
                    payload["user_message"] = section("本轮用户输入")
                    payload["_agent_stage"] = {"phase": section("当前阶段"),
                        "instructions": section("阶段规则"), "memory": section("用户记忆")}
                    if section("本轮时间锚点"):
                        payload["time_context"] = json.loads(section("本轮时间锚点"))
        if "长期记忆抽取" in system:
            payload = capture_payload_from_wire(messages, payload)
        serialized = json.dumps(messages, ensure_ascii=False)
        stage = payload.get("_agent_stage") or {}
        memory, current_time = current_context(payload)
        if not stage.get("phase"):
            stage["phase"] = ("Memory Capture" if "长期记忆抽取" in system else
                              "Dynamic Action" if payload.get("current_step") else
                              "Dynamic Planner" if "受控动态任务规划器" in system else "non_unified")
        record = {"phase": stage.get("phase") or "non_unified",
                  "request_hash": digest(body), "pid": os.getpid(),
                  "actual_sdk_transport": True, "real_provider_quality": False,
                  "memory_complete": [value in memory for value in MEMORIES],
                  "captured_memory_complete": CAPTURED_FACT in memory,
                  "capture_current_user_content_hash": (digest(current_user_content(payload))
                                                        if stage["phase"] == "Memory Capture" else None),
                  "capture_current_assistant_content_hash": (digest(next((str(row.get("content") or "")
                      for row in reversed((payload.get("conversation_context") or {}).get("messages") or [])
                      if isinstance(row, dict) and row.get("role") == "assistant"), ""))
                      if stage["phase"] == "Memory Capture" else None),
                  "all_wire_memory_complete": [value in serialized for value in MEMORIES],
                  "payload_keys": sorted(payload), "system_hash": digest(system)}
        record["time_context"] = current_time or None
        record["source_input_id"] = current_time.get("source_input_id")
        try:
            result = model_response(system, payload)
            content = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            record.update(status="completed", response_hash=digest(content),
                          captured_fact_selected=bool(stage.get("phase") == "Router"
                              and isinstance(result, dict)
                              and CAPTURED_FACT in str(result.get("user_intent") or "")))
        except Exception as exc:
            record.update(status="controlled_model_error", error_type=type(exc).__name__, error_hash=digest(str(exc)))
            with LOCK:
                rows = load(control, "sdk.safe.json", [])
                rows.append(record)
                save(control, "sdk.safe.json", rows)
            return JSONResponse({"error": {"message": "F1_CONTROLLED_MODEL_ERROR", "type": "invalid_request_error"}}, status_code=400)
        with LOCK:
            rows = load(control, "sdk.safe.json", [])
            rows.append(record)
            save(control, "sdk.safe.json", rows)
        packet = {"id": "f1-controlled-" + uuid4().hex, "object": "chat.completion", "created": 0,
                  "model": "f1-controlled-protocol", "choices": [{"index": 0,
                  "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 32, "completion_tokens": 32, "total_tokens": 64}}
        if not body.get("stream"):
            return JSONResponse(packet)

        async def stream():
            """遵守OpenAI原SSE包络，不替换应用自身SSE。"""
            for delta, finish in [({"role": "assistant", "content": content}, None), ({}, "stop")]:
                chunk = {**{k: packet[k] for k in ["id", "created", "model"]},
                         "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def submit(request: Request):
        """记录真实HTTP提交及幂等头；accepted绝不当业务成功。"""
        body = await request.json()
        key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
        callback_key = request.headers.get("X-Gongge-Xuban-External-Task-Token")
        with LOCK:
            state = load(control, "provider.json", {"submits": [], "finishes": []})
            matching = next((row for row in state["submits"] if key and row["key_hash"] == digest(key)), None)
            if matching is None:
                matching = {"task_id": "f1-provider-" + uuid4().hex, "key_hash": digest(key),
                            "request": body, "status": "accepted", "post_count": 1,
                            "receipt_id": "receipt-f1-" + uuid4().hex, "effect_completed": False}
                state["submits"].append(matching)
                private = load(control, "callback-keys.private.json", {})
                private[matching["task_id"]] = callback_key
                save(control, "callback-keys.private.json", private)
                matching["callback_header_received"] = bool(callback_key)
                matching["callback_key_hash"] = digest(callback_key)
            else:
                matching["post_count"] += 1
            save(control, "provider.json", state)
            return {"taskId": matching["task_id"], "status": matching["status"]}

    def status(task_id: str):
        """原poll只读受控远端的真实状态。"""
        with LOCK:
            state = load(control, "provider.json", {"submits": [], "finishes": []})
            row = next(item for item in state["submits"] if item["task_id"] == task_id)
            return {"taskId": task_id, "status": row["status"], "data": {**row["request"], "receipt_id": row["receipt_id"]}}

    async def complete(task_id: str):
        """只完成远端效果，再经真实callback API回报；不写本地Runtime状态。"""
        import httpx
        from sqlmodel import Session, select
        from app.db import engine
        from app.db.models import SopOperation
        with LOCK:
            state = load(control, "provider.json", {"submits": [], "finishes": []})
            row = next(item for item in state["submits"] if item["task_id"] == task_id)
            row.update(status="completed", effect_completed=True)
            save(control, "provider.json", state)
            payload = {**row["request"], "receipt_id": row["receipt_id"]}
        with Session(engine) as db:
            op = db.exec(select(SopOperation).where(SopOperation.external_task_id == task_id)).one()
            operation_id = op.id
            observation_revision = op.external_observation_revision + 1
            token = load(control, "callback-keys.private.json", {}).get(task_id) or ""
        with LOCK:
            observations = load(control, "callback-revisions.json", {})
            observation_revision = observations.setdefault(task_id, observation_revision)
            save(control, "callback-revisions.json", observations)
        async with httpx.AsyncClient() as client:
            response = await client.post(f"http://127.0.0.1:{os.environ['APP_PORT']}/api/external-tasks/{operation_id}/callback",
                headers={"X-Gongge-Xuban-External-Task-Token": token}, json={
                    "provider_task_id": task_id, "provider_event_id": "event-" + task_id,
                    "provider_status": "completed", "observation_revision": observation_revision,
                    "payload": {"data": payload}}, timeout=15)
        return {"http_status": response.status_code, "operation_id": operation_id,
                "event_hash": digest(response.json()), "remote_effect_completed": True}

    def receipt(task_id: str):
        """从远端已发生效果生成只读原回执页，accepted不能伪装完成回执。"""
        from fastapi import HTTPException
        with LOCK:
            state = load(control, "provider.json", {"submits": [], "finishes": []})
            row = next((item for item in state["submits"] if item["task_id"] == task_id), None)
            if row is None or not row["effect_completed"]:
                raise HTTPException(status_code=404, detail="F1_REMOTE_RECEIPT_NOT_COMPLETED")
            cells = "".join("<tr><th>" + label + "</th><td>" + escape(str(row["request"][key]))
                            + "</td></tr>" for key, label in [("product_name", "商品"),
                            ("quantity", "数量"), ("customer", "客户"), ("due_date", "目标日期")])
            return HTMLResponse("<!doctype html><html lang='zh'><meta charset='utf-8'>"
                "<title>配送回执</title><h1>配送回执</h1><p id='receipt-id'>"
                + escape(row["receipt_id"]) + "</p><table>" + cells + "</table></html>")

    async def finish(request: Request):
        """后继业务写用原回执查远端实效，不生成测试预置成功。"""
        body = await request.json()
        with LOCK:
            state = load(control, "provider.json", {"submits": [], "finishes": []})
            row = next(item for item in state["submits"] if item["receipt_id"] == body.get("receipt_id") and item["effect_completed"])
            state["finishes"].append({"receipt_id": row["receipt_id"], "request_hash": digest(body)})
            save(control, "provider.json", state)
            return {"success": True, "data": {**row["request"], "receipt_id": row["receipt_id"], "finished": True}}

    def facts():
        """只读原账本，返回原Operation和provider独立效果。"""
        return snapshot(control)

    app.add_api_route("/__e2e/f1/openai/v1/chat/completions", model, methods=["POST"])
    app.add_api_route("/__e2e/f1/provider/submit", submit, methods=["POST"])
    app.add_api_route("/__e2e/f1/provider/finish", finish, methods=["POST"])
    app.add_api_route("/__e2e/f1/provider/status/{task_id}", status, methods=["GET"])
    app.add_api_route("/__e2e/f1/provider/complete/{task_id}", complete, methods=["POST"])
    app.add_api_route("/__e2e/f1/provider/receipt/{task_id}", receipt, methods=["GET"])
    app.add_api_route("/__e2e/f1/facts", facts, methods=["GET"])


def prepare_final_f1(runtime: Path, *, reuse: bool) -> None:
    """显式自有临时库才启用受控资源；真重启不种状态、不重置远端任务。"""
    from app.db import engine
    if os.environ.get("FULLSTACK_FINAL_F1") != "1":
        raise RuntimeError("F1_EXPLICIT_ISOLATION_REQUIRED")
    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("F1_RANDOM_MYSQL_REQUIRED")
    elif engine.dialect.name != "sqlite" or not Path(engine.url.database or "").resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("F1_OWNED_SQLITE_REQUIRED")
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not control.is_relative_to(runtime.parent.resolve()):
        raise RuntimeError("F1_OWNED_CONTROL_REQUIRED")
    control.mkdir(parents=True, exist_ok=True)
    if not reuse:
        seed_resources(control)
    install_clock(control)
    install_routes(control)
    save(control, "server-pid.safe.json", {"pid": os.getpid(), "reuse": reuse})
