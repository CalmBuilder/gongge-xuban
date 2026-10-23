"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f2_h3_fixture.py
@CallChain  : 自有F2服务 → 原Composer/反馈/SkillEditor/发布 → 实际SDK/业务HTTP与只读账本
@Description: 只初始化合法旧资源，固定业务标签；故障只暂停原守卫前入口或SDK响应。
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel

import final_f1_fullstack_fixture as base

AGENT = "agent_f2_feedback"
SKILL = "skill_f2_feedback_quota"
TOOL = "f2.quota_query"
BUSINESS = {"E001": 1500, "E002": 600}


class QuotaRequest(BaseModel):
    """独立只读业务服务只接受一个实际员工编号，不接受模型生成的额度。"""

    employee_id: str


def canonical_hash(value: object) -> str:
    """独立规范JSON散列，不调用被测Operation指纹函数。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def control_directory() -> Path:
    """所有控制只在本次显式隔离运行目录，拒绝其他数据库与生产启动。"""
    if (os.environ.get("FULLSTACK_FINAL_F2") != "1"
            or os.environ.get("FULLSTACK_F2_JOURNEY") != "JRN07"):
        raise RuntimeError("F2_H3_EXPLICIT_OWNED_RUN_REQUIRED")
    runtime = Path(os.environ["FULLSTACK_E2E_RUNTIME_DIR"]).resolve()
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if control.parent != runtime.parent:
        raise RuntimeError("F2_H3_OWNED_CONTROL_REQUIRED")
    from app.db import engine
    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("F2_H3_RANDOM_NATIVE_DATABASE_REQUIRED")
    elif (engine.dialect.name != "sqlite"
          or not Path(engine.url.database or "").resolve().is_relative_to(runtime)):
        raise RuntimeError("F2_H3_OWNED_SQLITE_REQUIRED")
    return control


def initial_card() -> dict:
    """返回合法而业务错绑的原定义，初始输入和工具没有固定员工号或额度答案。"""
    return {
        "skill_id": SKILL, "name": "本人额度查询", "version": "1.0.0",
        "execution_mode": "deterministic", "description": "employee_id为本人，sponsor_id为关联人。",
        "trigger_intents": ["查询本人额度"], "goal": ["返回本人员工号对应的真实额度"],
        "required_info": ["employee_id", "sponsor_id"],
        "condition_schemas": {"slots": {"type": "object", "properties": {
            "employee_id": {"type": "string"}, "sponsor_id": {"type": "string"}}},
            "tool_result": {"type": "object", "properties": {"quota": {"type": "object"}}}},
        "nodes": [
            {"node_id": "collect", "type": "collect_info", "name": "收集编号",
             "instruction": "分别收集本人员工号employee_id和关联人编号sponsor_id。",
             "expected_user_info": ["employee_id", "sponsor_id"],
             "allowed_actions": ["ask_user", "continue_flow"]},
            {"node_id": "query", "type": "tool_call", "name": "查询本人额度",
             "instruction": "调用f2.quota_query读取额度。", "allowed_actions": ["call_tool:" + TOOL],
             "metadata": {"operation_input": {"employee_id": "slots.sponsor_id"},
                          "operation_result_key": "quota"}},
            {"node_id": "done", "type": "response", "name": "返回实际额度",
             "allowed_actions": ["answer_user"]}],
        "edges": [{"source_node_id": "collect", "next_node_id": "query"},
                  {"source_node_id": "query", "next_node_id": "done"}],
        "start_node_id": "collect", "terminal_node_ids": ["done"],
    }


def seed_resources() -> None:
    """只创建合成员工、工具与原API发布；不创建消息、反馈、候选或运行事实。"""
    from sqlmodel import Session
    from app.agents.branching import ensure_private_resource_binding
    from app.api.skills import create_skill
    from app.db import engine
    from app.db.models import AgentProfile, AgentUsage, Tool, User
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, publish_tool_contract
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    with Session(engine) as db:
        db.add(AgentProfile(id=AGENT, tenant_id=base.TENANT, name="F2反馈员工",
                            owner_user_id="admin", status="active", published_to_gallery=True, visibility_scope="tenant",
                            description="查询本人员工额度", metadata_json={"owner_user_id": "admin"}))
        for user in ["admin", "f1_reviewer"]:
            db.add(AgentUsage(tenant_id=base.TENANT, user_id=user, agent_id=AGENT))
        db.flush()
        tool = Tool(id="tool_f2_quota", tenant_id=base.TENANT, name=TOOL,
                    display_name="隔离本人额度查询", description="按实际employee_id读取独立业务数据",
                    method="POST", url=f"http://127.0.0.1:{os.environ['APP_PORT']}/__e2e/f2/h3/business/quota",
                    input_schema={"type": "object", "properties": {"employee_id": {"type": "string"}},
                                  "required": ["employee_id"], "additionalProperties": False},
                    output_schema={"type": "object", "properties": {
                        "employee_id": {"type": "string"}, "quota": {"type": "integer"}}},
                    allowed_skills_json=[SKILL], enabled=True)
        publish_tool_contract(tool, ToolReliabilityContract(risk_class="read", side_effect="none",
                              confirmation_policy="none", timeout_policy="failed", dynamic_task_enabled=True))
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, base.TENANT, AGENT, "tool", tool.id, "active")
        db.commit()
        published = create_skill(SkillCreateRequest(tenant_id=base.TENANT,
            content=SkillCard.model_validate(initial_card()), status="published"),
            agent_id=AGENT, db=db, current_user=db.get(User, "admin"))
        base.save(control_directory(), "h3-initial.safe.json", {
            "agent_id": AGENT, "skill_id": SKILL, "original_version_id": published.publication_id,
            "original_definition_checksum": published.publication_checksum,
            "business": BUSINESS, "business_hash": canonical_hash(BUSINESS),
            "initial_card_hash": canonical_hash(initial_card()), "real_provider_quality": False})


def descriptor(kind: str) -> dict:
    """读取本轮事前登记的故障名字与切点，不读取候选/数据库业务答案。"""
    value = base.load(control_directory(), "h3-" + kind + "-case.json", {})
    if not isinstance(value, dict):
        raise ValueError("F2_H3_CASE_OBJECT_REQUIRED")
    name = value.get("case", "ordinary-" + kind)
    mode = value.get("mode", "normal")
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,60}", name)
            or mode not in {"normal", "bad_first", "fail", "hold_first", "before_guard", "before_settle"}):
        raise ValueError("F2_H3_CASE_INVALID")
    return {"case": name, "mode": mode}


def append(name: str, value: dict) -> None:
    """保存白名单事件；不复制请求、模型正文、认证头或任意异常。"""
    control = control_directory()
    with base.LOCK:
        rows = base.load(control, name, [])
        base.save(control, name, [*rows, value])


def wait_release(case: str, phase: str) -> None:
    """暂停原worker或结算入口，等待UI提交后原函数继续执行其自身守卫/CAS。"""
    control = control_directory()
    (control / f"h3-{case}-{phase}.entered").touch()
    deadline = time.monotonic() + 120
    while not (control / f"h3-{case}-{phase}.release").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("F2_H3_RELEASE_REQUIRED")
        time.sleep(0.05)


def quota_text(payload: dict) -> str:
    """只从当前实际工具结果取得编号和额度，不从用户标签或数据库补答案。"""
    values = list(base.walk(payload.get("tool_result") or payload.get("task_results") or {}))
    rows = [r for r in values if isinstance(r.get("quota"), int) and r.get("employee_id")]
    if not rows:
        return "当前输入没有实际工具额度，不能宣称查询完成。"
    result = rows[-1]
    return f"实际查询员工{result['employee_id']}的额度为{result['quota']}。"


def proposal_from_instruction(payload: dict) -> dict:
    """从当前card和人工明确局部指令生成patch，不注入完整正确候选或业务数值。"""
    card = payload["current_skill"]
    requested = re.search(r"nodes\.([A-Za-z0-9_-]+)\.metadata\.operation_input\.employee_id"
                          r"\s*改为\s*slots\.([A-Za-z0-9_]+)", str(payload.get("instruction") or ""))
    if requested is None:
        raise ValueError("F2_H3_EXPLICIT_PATCH_REQUIRED")
    node_id, slot = requested.groups()
    if slot not in card.get("required_info", []):
        raise ValueError("F2_H3_CURRENT_SLOT_REQUIRED")
    node = next((row for row in card["nodes"] if row["node_id"] == node_id), None)
    if node is None:
        raise ValueError("F2_H3_CURRENT_NODE_REQUIRED")
    metadata = deepcopy(node.get("metadata") or {})
    metadata["operation_input"] = {**metadata.get("operation_input", {}), "employee_id": "slots." + slot}
    return {"patches": [{"path": "nodes." + node_id + ".metadata", "value": metadata}],
            "assistant_message": "已按明确局部指令调整绑定，等待人工审核与实际业务验证。",
            "changed_paths": ["/nodes/" + node_id + "/metadata/operation_input/employee_id"],
            "warnings": [], "tool_mentions": []}


def install_protocol_and_cuts() -> None:
    """原SDK走真实HTTP，原SkillEditor/派发守卫/最终CAS不变；只控制协议和暂停。"""
    from app.api import skills
    from app.main import app
    from app.skills.stream_jobs import stream_jobs

    control = control_directory()
    original_response = base.model_response
    original_worker = skills._run_feedback_rewrite_stream_job
    original_settle = skills.settle_candidate_rewrite

    def controlled_response(system: str, payload: dict) -> object:
        """为实际当前调用返回受控协议；额度回复只使用真实tool_result。"""
        kind = "feedback" if "feedback" in payload else "candidate"
        is_feedback = "feedback" in payload
        current = payload.get("current_skill") or payload.get("candidate_skill") or {}
        if is_feedback or current.get("skill_id") == SKILL:
            cut = descriptor(kind)
            rows = base.load(control, "h3-sdk.safe.json", [])
            count = len([r for r in rows if r["case"] == cut["case"] and r["kind"] == kind])
            if cut["mode"] == "fail" or (count == 1 and cut["mode"] in {"bad_first", "hold_first"}):
                return "{ invalid-json"
            if is_feedback:
                return {"bucket": "skill_issue", "confidence": 0.5,
                        "reason": "受控协议归因，与原执行事实分开。", "summary": "等待人工核对绑定。",
                        "evidence": [], "suggested_action": "按原业务结果审核。"}
            if "candidate_skill" in payload:
                return {"passed": True, "summary": "仅受控结构反思，不证明业务效果。", "rubric_results": []}
            return proposal_from_instruction(payload)
        stage = payload.get("_agent_stage") or {}
        phase = stage.get("phase") or ""
        message = str(payload.get("user_message") or payload.get("goal") or "")
        if phase == "Router" and "查询本人额度" in message:
            offered = payload.get("available_skills") or []
            if not any(row.get("skill_id") == SKILL for row in offered):
                raise ValueError("F2_H3_CURRENT_SKILL_NOT_OFFERED")
            session = payload.get("current_session") or {}
            hints = dict(session.get("slots") or {})
            for key, label in [("employee_id", "本人员工号"), ("sponsor_id", "关联人编号")]:
                found = re.search(label + r"(?:是|为)?\s*(E\d+)", message)
                if found:
                    hints[key] = found.group(1)
            return {"decision": "continue_active" if session.get("active_skill_id") else "start_new_task",
                    "target_skill_id": SKILL, "slot_hints": hints, "confidence": 0.99,
                    "reason": "当前可见技能与本人明确查询匹配。"}
        active = payload.get("active_skill") or {}
        # 原 compact_step_skill_context 只提供当前节点，不传完整 skill_id。
        if phase == "Step Agent" and (
            "查询本人额度" in message
            or (active.get("current_step") or {}).get("name")
            in {"收集编号", "返回实际额度"}
        ):
            node = (active.get("current_step") or {}).get("node_id")
            return {"action": "reply" if node == "done" else "ask_user",
                    "reply": quota_text(payload) if node == "done" else "请补充本人员工号和关联人编号。",
                    "slot_updates": {}, "is_step_completed": node == "done"}
        if message.startswith("F2-DTA编号对照") and phase in {"Router / Dynamic Task", "Router / Dynamic Task Shadow"}:
            return {"mode": "dynamic_task", "goal": message,
                    "success_criteria": ["解释本人编号与关联人编号的区别，不调用工具"],
                    "requires_durable_execution": True, "requires_artifact": False,
                    "execution_intent": "new_task", "capability_hints": [], "confidence": 0.99,
                    "reason": "普通DTA解释任务，与SOP发布无关。"}
        if message.startswith("F2-DTA编号对照") and "受控动态任务规划器" in system:
            return {"goal": message, "success_criteria": payload.get("success_criteria", []),
                    "constraints": [], "assumptions": [], "steps": [{"draft_id": "answer",
                        "title": "编号语义解释", "kind": "answer", "required": True, "depends_on": [],
                        "capability_refs": [], "guidance_skill_refs": [], "expected_output_schema": {}}],
                    "expected_artifacts": []}
        view = payload.get("provider_execution_view") or {}
        frame = view.get("execution_context") or {}
        goal = frame.get("goal") or {}
        if isinstance(goal, dict) and str(goal.get("goal") or "").startswith("F2-DTA编号对照"):
            current_step = payload.get("current_step") or view.get("current_step") or {}
            return {"action_kind": "answer", "capability_ref": None, "expected_output_schema": {},
                    "rationale": "只解释当前明确字段，不办理业务。", "arguments": {
                        "markdown": "employee_id标识本人，sponsor_id标识关联人，不能互相代替。",
                        "criterion_evidence": {r["id"]: [current_step["step_key"]]
                                               for r in frame.get("success_criteria", [])},
                        "pending_questions": []}}
        if phase == "Response Generator" and "查询本人额度" in message:
            return quota_text(payload)
        return original_response(system, payload)

    def worker(job_id, prepared):
        """记录实际job/fence；切点释放后必须执行原worker，而非替代其授权和结算。"""
        cut = descriptor("candidate")
        row = {**cut, "job_id": job_id, "candidate_id": prepared.fence.candidate_id,
               "actor_id": prepared.fence.actor_id, "revision": prepared.fence.revision,
               "generation": prepared.fence.generation, "pid": os.getpid()}
        with base.LOCK:
            mapping = base.load(control, "h3-job-map.safe.json", {})
            mapping[cut["case"]] = row
            base.save(control, "h3-job-map.safe.json", mapping)
        try:
            if cut["mode"] == "before_guard":
                wait_release(cut["case"], "guard")
            return original_worker(job_id, prepared)
        finally:
            job = stream_jobs.get(job_id)
            append("h3-job-terminal.safe.json", {**row,
                "status": job.status if job else "missing", "cancel_requested": bool(job and job.cancel_requested),
                "cache_event_names": [event.event for event in job.events] if job else [],
                "cache_has_complete": bool(job and any(e.event == "complete" for e in job.events)),
                "cache_payload_hash": base.digest([e.data for e in job.events]) if job else None})

    def settle(engine, fence, response):
        """在原最终CAS入口之前暂停，来源撤回后仍由原函数决定旧输出能否写回。"""
        mapping = base.load(control, "h3-job-map.safe.json", {})
        cut = next((r for r in mapping.values() if r["candidate_id"] == fence.candidate_id
                    and r["generation"] == fence.generation), None)
        if cut and cut["mode"] == "before_settle":
            wait_release(cut["case"], "settle")
        return original_settle(engine, fence, response)

    base.model_response = controlled_response
    skills._run_feedback_rewrite_stream_job = worker
    skills.settle_candidate_rewrite = settle
    for route in app.routes:
        if getattr(route, "path", "") != "/__e2e/f1/openai/v1/chat/completions":
            continue
        original_endpoint = route.dependant.call

        async def observed_sdk(request: Request):
            """只观察真正SDK HTTP；首响应暂停允许另一页面正式撤回/取消/撤权。"""
            body = await request.json()
            payload = {}
            for message in body.get("messages", []):
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    try:
                        decoded = json.loads(message["content"])
                        if isinstance(decoded, dict):
                            payload = decoded
                    except ValueError:
                        pass
            card = payload.get("current_skill") or payload.get("candidate_skill") or {}
            kind = "feedback" if "feedback" in payload else "candidate"
            observed = kind == "feedback" or card.get("skill_id") == SKILL
            if observed:
                cut = descriptor(kind)
                job = base.load(control, "h3-job-map.safe.json", {}).get(cut["case"], {})
                rows = base.load(control, "h3-sdk.safe.json", [])
                nth = 1 + len([r for r in rows if r["case"] == cut["case"] and r["kind"] == kind])
                source = payload.get("source") or {}
                append("h3-sdk.safe.json", {**cut, "kind": kind, "nth": nth, "pid": os.getpid(),
                    "job_id": job.get("job_id"), "candidate_id": job.get("candidate_id"),
                    "fence_revision": job.get("revision"), "fence_generation": job.get("generation"),
                    "material_ids": [r.get("id") for r in payload.get("feedback_materials", [])],
                    "source_execution_id": source.get("execution_id"),
                    "source_message_id": source.get("message_id"), "source_turn_id": source.get("turn_id"),
                    "feedback_revision": (payload.get("feedback") or {}).get("revision"),
                    "request_hash": base.digest(body), "stream": bool(body.get("stream")),
                    "actual_sdk_transport": True, "real_provider_quality": False,
                    "reflection": "candidate_skill" in payload})
                if cut["mode"] == "hold_first" and nth == 1:
                    (control / f"h3-{cut['case']}-sdk.entered").touch()
                    deadline = time.monotonic() + 120
                    while not (control / f"h3-{cut['case']}-sdk.release").exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("F2_H3_SDK_RELEASE_REQUIRED")
                        await asyncio.sleep(0.05)
            return await original_endpoint(request)

        route.dependant.call = observed_sdk


def _feedback_source_projection(message_id: str) -> dict | None:
    """每条原回复单独验证并立即关闭FOR UPDATE读事务，不累积观察器行锁。"""
    from sqlmodel import Session
    from app.db import engine
    from app.db.models import Message
    from app.feedback.source import feedback_source

    with Session(engine) as source_db:
        message = source_db.get(Message, message_id, populate_existing=True)
        return feedback_source(source_db, message).model_dump() if message else None


def snapshot() -> dict:
    """读取实际持久身份/发布/来源/Operation及安全调用旁证，正文仅散列。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (
        ActionProposalRecord, AgentSkillBranch, ChatSession, ExecutionPlanRevision, ExecutionResult,
        ExecutionSignal, FeedbackMaterial, Message, MessageFeedback,
        SkillEditCandidate, SkillVersion, SopInstance, SopNodeExecution, SopOperation, SopOperationAttempt,
    )
    control = control_directory()
    with Session(engine) as db:
        instances = db.exec(select(SopInstance).where(SopInstance.agent_id == AGENT)).all()
        ids = [r.id for r in instances]
        sessions = db.exec(select(ChatSession).where(ChatSession.agent_id == AGENT)).all()
        session_ids = [r.id for r in sessions]
        messages = db.exec(select(Message).where(Message.session_id.in_(session_ids))).all() if session_ids else []
        feedback = db.exec(select(MessageFeedback).where(MessageFeedback.session_id.in_(session_ids))).all() if session_ids else []
        versions = db.exec(select(SkillVersion).where(SkillVersion.skill_id == SKILL,
                               SkillVersion.scope_agent_id == AGENT)).all()
        candidates = db.exec(select(SkillEditCandidate).where(SkillEditCandidate.skill_id == SKILL)).all()
        materials = db.exec(select(FeedbackMaterial).where(FeedbackMaterial.target_agent_id == AGENT)).all()
        operations = db.exec(select(SopOperation).where(SopOperation.instance_id.in_(ids))).all() if ids else []
        nodes = db.exec(select(SopNodeExecution).where(SopNodeExecution.instance_id.in_(ids))).all() if ids else []
        attempts = db.exec(select(SopOperationAttempt).where(SopOperationAttempt.instance_id.in_(ids))).all() if ids else []
        signals = db.exec(select(ExecutionSignal).where(ExecutionSignal.execution_id.in_(ids))).all() if ids else []
        plans = db.exec(select(ExecutionPlanRevision).where(ExecutionPlanRevision.execution_id.in_(ids))).all() if ids else []
        proposals = db.exec(select(ActionProposalRecord).where(ActionProposalRecord.execution_id.in_(ids))).all() if ids else []
        results = db.exec(select(ExecutionResult).where(ExecutionResult.execution_id.in_(ids))).all() if ids else []
        branch = db.exec(select(AgentSkillBranch).where(AgentSkillBranch.agent_id == AGENT,
                                                        AgentSkillBranch.skill_id == SKILL)).first()
        result = {
            "pid": os.getpid(), "labels": base.load(control, "h3-initial.safe.json", {}),
            "business_calls": base.load(control, "h3-business.safe.json", []),
            "sdk": base.load(control, "h3-sdk.safe.json", []),
            "runtime_sdk": base.load(control, "sdk.safe.json", []),
            "job_map": base.load(control, "h3-job-map.safe.json", {}),
            "job_terminal": base.load(control, "h3-job-terminal.safe.json", []),
            "publication_count": len(versions),
            "branch": {"id": branch.id, "head_version": branch.head_version,
                       "published_version_id": ((branch.metadata_json or {}).get("runtime_publication") or {}).get("id"),
                       "content_hash": base.digest(branch.content_json)} if branch else None,
            "versions": [{"id": r.id, "skill_id": r.skill_id, "scope_agent_id": r.scope_agent_id,
                          "version": r.version, "content_checksum": r.content_checksum,
                          "definition_checksum": r.compiled_definition_checksum,
                          "content_hash": base.digest(r.content_json)} for r in versions],
            "instances": [{"id": r.id, "session_id": r.session_id, "kind": r.kind, "status": r.status,
                "tenant_id": r.tenant_id, "initiator_user_id": r.initiator_user_id, "agent_id": r.agent_id,
                "skill_id": r.skill_id, "skill_version_id": r.skill_version_id,
                "current_plan_revision_id": r.current_plan_revision_id,
                "definition_checksum": r.definition_checksum, "current_node_id": r.current_node_id,
                "time_context": (r.context_json or {}).get("time_context"),
                "time_context_hash": base.digest((r.context_json or {}).get("time_context"))} for r in instances],
            "messages": [{"id": r.id, "session_id": r.session_id, "role": r.role,
                          "content_hash": base.digest(r.content),
                          "time_context": (r.metadata_json or {}).get("time_context"),
                          "source": None}
                         for r in messages],
            "feedback": [{"id": r.id, "message_id": r.message_id, "session_id": r.session_id,
                          "user_id": r.user_id, "revision": r.revision,
                          "analysis_generation": r.analysis_generation, "analysis_status": r.analysis_status}
                         for r in feedback],
            "materials": [{"id": r.id, "feedback_id": r.feedback_id, "feedback_revision": r.feedback_revision,
                           "owner_user_id": r.owner_user_id, "status": r.status,
                           "target_agent_id": r.target_agent_id, "checksum": r.material_checksum,
                           "body_hash": base.digest(r.material_json)} for r in materials],
            "candidates": [{"id": r.id, "actor_user_id": r.actor_user_id, "scope_agent_id": r.scope_agent_id,
                "base_skill_version_id": r.base_skill_version_id, "base_checksum": r.base_checksum,
                "revision": r.revision, "generation": r.generation, "status": r.status,
                "actor_auth_revision": r.actor_auth_revision, "source_refs": r.source_refs_json,
                "content_hash": base.digest(r.candidate_json), "receipt": r.publish_receipt_json} for r in candidates],
            "operations": [{"id": r.id, "instance_id": r.instance_id, "status": r.status,
                "operation_name": r.operation_name, "node_execution_id": r.node_execution_id,
                "request": {k: v for k, v in r.request_json.items() if k == "employee_id"},
                "request_fingerprint": r.request_fingerprint,
                "result": {k: v for k, v in (r.result_json or {}).items() if k in {"employee_id", "quota"}}}
                for r in operations],
            "nodes": [{"id": r.id, "instance_id": r.instance_id, "node_id": r.node_id,
                       "step_key": r.step_key, "attempt": r.attempt, "status": r.status,
                       "input_time_context": (r.input_json or {}).get("time_context"),
                       "resume_time_context": (r.input_json or {}).get("resume_time_context")} for r in nodes],
            "attempts": [{"id": r.id, "operation_id": r.operation_id, "instance_id": r.instance_id,
                          "attempt_number": r.attempt_number, "status": r.status} for r in attempts],
            "signals": [{"id": r.id, "execution_id": r.execution_id, "signal_type": r.signal_type,
                         "status": r.status} for r in signals],
            "plans": [{"id": r.id, "execution_id": r.execution_id, "status": r.status,
                       "checksum": r.checksum, "plan_hash": base.digest(r.plan_json),
                       "steps": [{k: step.get(k) for k in ["step_key", "kind", "capability_refs"]}
                                 for step in (r.plan_json or {}).get("steps", [])]} for r in plans],
            "proposals": [{"id": r.id, "execution_id": r.execution_id,
                           "plan_revision_id": r.plan_revision_id, "step_key": r.step_key,
                           "status": r.status, "checksum": r.proposal_checksum} for r in proposals],
            "results": [{"id": r.id, "execution_id": r.execution_id, "status": r.status,
                         "checksum": r.checksum, "result_hash": base.digest(r.result_json),
                         "criterion_evidence": (r.result_json or {}).get("criterion_evidence", {}),
                         "verification_passed": (r.verification_json or {}).get("passed"),
                         "verification_hash": base.digest(r.verification_json),
                         "describes_both_fields": all(value in str(r.result_json)
                                                       for value in ["employee_id", "sponsor_id"])} for r in results],
        }
    for row in result["messages"]:
        if row["role"] == "assistant":
            row["source"] = _feedback_source_projection(row["id"])
    return result


def prepare_h3(*, reuse: bool) -> None:
    """只在自有F2启动安装夹具；重启保留同库原运行和业务记录。"""
    from app.main import app
    from fastapi import HTTPException

    control_directory()
    if not reuse:
        seed_resources()
    install_protocol_and_cuts()

    @app.post("/__e2e/f2/h3/business/quota")
    def quota(request: QuotaRequest):
        """真实工具HTTP独立读回固定业务数据；不改变Runtime或候选。"""
        if request.employee_id not in BUSINESS:
            raise HTTPException(404, "F2_H3_SYNTHETIC_EMPLOYEE_NOT_FOUND")
        result = {"employee_id": request.employee_id, "quota": BUSINESS[request.employee_id]}
        append("h3-business.safe.json", {"id": "h3business_" + uuid4().hex,
            "request": request.model_dump(), "response": result,
            "request_hash": canonical_hash(request.model_dump()),
            "response_hash": canonical_hash(result), "actual_http": True})
        return result

    @app.get("/__e2e/f2/h3/facts")
    def facts():
        """仅返回本次合成旅程白名单，禁止通过观察端点推进业务。"""
        return snapshot()
