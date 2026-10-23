"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : sd7_f3_fullstack_fixture.py
@CallChain  : 隔离LIVE全栈 → 合成旧SOP/只读业务 → Composer/真实SDK → 安全账本旁证
@Description: 不初始化Execution/Operation/反馈/成功结果，只提供业务服务和真实调用观察。
"""

from __future__ import annotations

import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4

from pydantic import BaseModel

AGENT = "agent_sd7_feedback"
SKILL = "skill_sd7_feedback_quota"
BUSINESS = {"E001": 1500, "E002": 600}
_lock = Lock()
_identity: ContextVar[dict[str, str]] = ContextVar("sd7_f3_identity", default={})


class QuotaRequest(BaseModel):
    """业务服务仅接受一个员工编号，独立数据不由模型或Runtime推导。"""

    employee_id: str


def digest(value: object) -> str:
    """只记录规范JSON摘要，不返回请求、模型正文或配置值。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _control(runtime: Path) -> Path:
    """限制观察和重启文件只在本次临时隔离目录内。"""
    if os.environ.get("FULLSTACK_SD7_F3") != "1" or os.environ.get("LIVE_ATTACHMENT_E2E") != "1":
        raise RuntimeError("SD7_F3_REQUIRES_ISOLATED_REAL_PROVIDER")
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not runtime.resolve().is_relative_to(Path("/tmp")) or not control.is_relative_to(runtime.parent.resolve()):
        raise RuntimeError("SD7_F3_OUTSIDE_ISOLATED_RUNTIME")
    control.mkdir(parents=True, exist_ok=True)
    return control


def _append(control: Path, name: str, value: dict[str, object]) -> None:
    """原子追加调用身份、摘要、状态和固定业务参数，不写原始正文。"""
    with _lock, (control / name).open("a") as file:
        file.write(json.dumps(value, ensure_ascii=False) + "\n")


def _formal_identity() -> dict[str, object]:
    """返回当前 Composer 轮的脱敏身份和来源摘要，失败时保持缺失而不猜身份。"""

    current = dict(_identity.get())
    if not current:
        return {}
    identity = {
        key: digest(value)
        for key, value in current.items()
        if key in {"tenant_id", "user_id", "agent_id", "session_id", "source_input_id"}
        and isinstance(value, str) and value
    }
    source = current.get("source_content")
    return {
        "identity_sha256": identity,
        "source_sha256": digest(source) if isinstance(source, str) else None,
        "kind": current.get("kind"),
        "diagnostic_scope": None,
    }


def _install_formal_identity_binding() -> None:
    """把真实流式请求的已持久会话/用户输入绑定到同轮 SDK 观察上下文。"""

    from app.core import agent_loop as agent_loop_module
    from app.api import chat as chat_module

    if getattr(chat_module, "_sd7_f3_formal_identity", False):
        return
    original_append = agent_loop_module.AgentLoop._append_user_input_message
    original_stream = chat_module._handle_stream_with_time_context

    def append_user(self, request, session_id, *, time_context=None):
        """记录原AgentLoop刚落账的用户消息，不改写消息或事务。"""

        message, context = original_append(self, request, session_id, time_context=time_context)
        current = dict(_identity.get())
        current.update(
            session_id=session_id,
            source_input_id=message.id,
            source_content=message.content,
        )
        _identity.set(current)
        return message, context

    def stream(loop, request, time_context):
        """在原流式AgentLoop生成器外建立同轮身份上下文。"""

        token = _identity.set({
            "tenant_id": request.tenant_id,
            "user_id": request.user_id or "",
            "agent_id": request.agent_id or "",
            "session_id": request.session_id or "",
            "source_input_id": "",
            "source_content": "",
            "kind": "dynamic_task" if request.execution_engine == "dynamic_task" else "sop",
        })
        try:
            yield from original_stream(loop, request, time_context)
        finally:
            _identity.reset(token)

    agent_loop_module.AgentLoop._append_user_input_message = append_user
    chat_module._handle_stream_with_time_context = stream
    chat_module._sd7_f3_formal_identity = True


def observe_sd7_sdk(runtime: Path) -> None:
    """包装真正SDK的create/with_options，保留实际传输、原回答、修复和重试。"""
    control = _control(runtime)
    from app.llm import client as module
    from app.observability.spans import current_llm_operation

    _install_formal_identity_binding()

    if getattr(module, "_sd7_f3_observed", False):
        return
    original_sdk = module.OpenAI

    def attach(client):
        """每个真实SDK副本继承同一安全观察，超时副本也不能漏记调用。"""
        create = client.chat.completions.create
        options = client.with_options

        def observed(**request):
            """观察真实请求完成和流消费；固定故障对照明确不是供应商质量。"""
            started = time.monotonic()
            messages = request.get("messages", [])
            text = json.dumps(messages, ensure_ascii=False)
            rewrite = SKILL in text and "feedback_materials" in text
            row = {"call_id": "sd7call_" + uuid4().hex,
                   "operation": current_llm_operation(), "rewrite": rewrite,
                   "request_hash": digest(messages), "model_hash": digest(request.get("model")),
                   "stream": bool(request.get("stream")), "actual_provider_call": True}
            if rewrite and (control / "model-failure").exists():
                row.update(actual_provider_call=False, status="controlled_failure", duration_ms=0)
                _append(control, "sdk-observations.safe.jsonl", row)
                raise RuntimeError("SD7_FIXED_PROTOCOL_FAILURE")
            try:
                result = create(**request)
            except BaseException:
                row.update(status="provider_failure", duration_ms=round((time.monotonic()-started)*1000, 3))
                _append(control, "sdk-observations.safe.jsonl", row)
                raise
            if not request.get("stream"):
                usage = getattr(result, "usage", None)
                row.update(status="completed", duration_ms=round((time.monotonic()-started)*1000, 3),
                           response_id_hash=digest(getattr(result, "id", None)),
                           response_hash=digest(result.model_dump(mode="json")),
                           usage={key: getattr(usage, key, None) for key in
                                  ("prompt_tokens", "completion_tokens", "total_tokens")})
                row.update(
                    request_sha256=row["request_hash"],
                    response_sha256=row["response_hash"],
                    observation_valid=True,
                    **_formal_identity(),
                )
                _append(control, "sdk-observations.safe.jsonl", row)
                return result

            class ObservedStream:
                """逐字透传真实流，安全摘要以实际消费终止为准。"""

                def __iter__(self):
                    """保持所有原chunk、usage及异常，观察不制造模型内容。"""
                    checksum = hashlib.sha256()
                    usage = None
                    status = "completed"
                    response_id = None
                    try:
                        for chunk in result:
                            checksum.update(chunk.model_dump_json().encode())
                            response_id = getattr(chunk, "id", response_id)
                            if getattr(chunk, "usage", None):
                                usage = chunk.usage
                            yield chunk
                    except BaseException:
                        status = "stream_interrupted"
                        raise
                    finally:
                        row.update(status=status, response_hash=checksum.hexdigest(),
                                   response_id_hash=digest(response_id),
                                   duration_ms=round((time.monotonic()-started)*1000, 3),
                                   usage={key: getattr(usage, key, None) for key in
                                          ("prompt_tokens", "completion_tokens", "total_tokens")})
                        if status == "completed":
                            row.update(
                                request_sha256=row["request_hash"],
                                response_sha256=row["response_hash"],
                                observation_valid=True,
                                **_formal_identity(),
                            )
                        _append(control, "sdk-observations.safe.jsonl", row)

                def close(self):
                    """取消沿原SDK流关闭，不改变取消、候选和Runtime状态。"""
                    result.close()

            return ObservedStream()

        def with_options(**settings):
            """真实with_options副本保留原超时参数及观察边界。"""
            return attach(options(**settings))

        client.chat.completions.create = observed
        client.with_options = with_options
        return client

    def sdk(**settings):
        """使用原供应商地址、凭据和SDK；无模型替身。"""
        return attach(original_sdk(**settings))

    module.OpenAI = sdk
    module._sd7_f3_observed = True


def prepare_sd7_f3(runtime: Path, *, reuse: bool) -> None:
    """只在临时SQLite/随机MySQL初始化账号、原发布和业务工具，重启不重置事实。"""
    control = _control(runtime)
    from app.db import engine

    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("SD7_F3_REQUIRES_RANDOM_MYSQL_DATABASE")
    elif engine.dialect.name != "sqlite" or not Path(engine.url.database or "").resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("SD7_F3_REQUIRES_TEMPORARY_DATABASE")
    observe_sd7_sdk(runtime)
    if not reuse:
        _seed()
    _install_business_and_state(control)


def _seed() -> None:
    """仅合成账号/范围/旧错绑SOP，发布沿原API，全部运行事实由页面产生。"""
    from sqlmodel import Session
    from app.agents.branching import ensure_private_resource_binding
    from app.api.skills import create_skill
    from app.db import engine
    from app.db.models import AgentProfile, AgentUsage, Tool, User
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, publish_tool_contract
    from app.security.auth import hash_password
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    with Session(engine) as db:
        admin = db.get(User, "admin")
        admin.password_hash = hash_password(os.environ["FULLSTACK_SD7_ADMIN_PASSWORD"])
        db.add(admin)
        for user_id, username, key in (("sd7_owner", "sd7-owner", "FULLSTACK_SD7_OWNER_PASSWORD"),
                                       ("sd7_manager", "sd7-manager", "FULLSTACK_SD7_MANAGER_PASSWORD")):
            db.add(User(id=user_id, tenant_id="tenant_demo", username=username, role="member",
                        password_hash=hash_password(os.environ[key])))
        db.add(AgentProfile(id=AGENT, tenant_id="tenant_demo", name="SD7 Feedback Employee",
                            owner_user_id="sd7_manager", status="active", published_to_gallery=True,
                            visibility_scope="tenant", description="本人额度查询；关联人不代替本人",
                            metadata_json={"owner_user_id": "sd7_manager", "owner_username": "sd7-manager"}))
        db.flush()
        for actor in ("sd7_owner", "sd7_manager"):
            db.add(AgentUsage(tenant_id="tenant_demo", user_id=actor, agent_id=AGENT))
        tool = Tool(id="tool_sd7_quota", tenant_id="tenant_demo", name="sd7.quota_query",
                    display_name="隔离本人额度查询", description="按employee_id读取员工独立额度",
                    method="POST", url=f"http://127.0.0.1:{os.environ['FULLSTACK_E2E_PORT']}/__e2e/sd7/business/quota",
                    input_schema={"type": "object", "properties": {"employee_id": {"type": "string"}},
                                  "required": ["employee_id"], "additionalProperties": False},
                    output_schema={"type": "object"}, allowed_skills_json=[SKILL], enabled=True)
        publish_tool_contract(tool, ToolReliabilityContract(risk_class="read", side_effect="none",
                              confirmation_policy="none", timeout_policy="failed", dynamic_task_enabled=True))
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, "tenant_demo", AGENT, "tool", tool.id, "active")
        db.commit()
        card = SkillCard.model_validate({
            "skill_id": SKILL, "name": "本人额度查询", "version": "1.0.0",
            "execution_mode": "deterministic", "description": "查询本人额度，employee_id是本人编号，sponsor_id是关联人编号。",
            "trigger_intents": ["查询本人额度"], "goal": ["返回本人员工号对应的真实额度"],
            "required_info": ["employee_id", "sponsor_id"],
            "condition_schemas": {"slots": {"type": "object", "properties": {
                "employee_id": {"type": "string", "description": "需要查询额度的本人编号"},
                "sponsor_id": {"type": "string", "description": "关联人编号，不能作为本人编号"}}},
                "tool_result": {"type": "object", "properties": {"quota": {"type": "object"}}}},
            "nodes": [{"node_id": "collect", "type": "collect_info", "name": "收集员工信息",
                       "instruction": "分别收集本人员工号employee_id和关联人编号sponsor_id，不互相代替。",
                       "expected_user_info": ["employee_id", "sponsor_id"], "allowed_actions": ["ask_user", "continue_flow"]},
                      {"node_id": "query", "type": "tool_call", "name": "查询本人额度",
                       "instruction": "调用sd7.quota_query查询本人额度。", "allowed_actions": ["call_tool:sd7.quota_query"],
                       "metadata": {"operation_input": {"employee_id": "slots.sponsor_id"}, "operation_result_key": "quota"}},
                      {"node_id": "done", "type": "response", "name": "返回额度", "allowed_actions": ["answer_user"]}],
            "edges": [{"source_node_id": "collect", "next_node_id": "query"},
                      {"source_node_id": "query", "next_node_id": "done"}],
            "start_node_id": "collect", "terminal_node_ids": ["done"],
        })
        create_skill(SkillCreateRequest(tenant_id="tenant_demo", content=card, status="published"),
                     agent_id=AGENT, db=db, current_user=db.get(User, "admin"))


def _install_business_and_state(control: Path) -> None:
    """真实HTTP只读合成业务与账本旁证，不改变模型、Operation或执行状态。"""
    from fastapi import HTTPException
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import ChatSession, Message, MessageFeedback, SkillVersion, SopInstance, SopOperation
    from app.main import app
    from app.sop_runtime.execution_store import SopExecutionStore

    @app.post("/__e2e/sd7/business/quota")
    def quota(request: QuotaRequest):
        """真正工具HTTP调用登记安全计数，并按独立业务数据返回额度。"""
        if request.employee_id not in BUSINESS:
            raise HTTPException(404, "SD7_SYNTHETIC_EMPLOYEE_NOT_FOUND")
        _append(control, "business-calls.safe.jsonl", {"employee_id": request.employee_id,
                "quota": BUSINESS[request.employee_id],
                "request_hash": SopExecutionStore.request_fingerprint(request.model_dump())})
        return {"employee_id": request.employee_id, "quota": BUSINESS[request.employee_id]}

    @app.get("/__e2e/sd7/state")
    def state():
        """只读合成任务的实例/发布/Operation身份与冻结业务验证字段。"""
        with Session(engine) as db:
            instances = db.exec(select(SopInstance).where(SopInstance.agent_id == AGENT)).all()
            versions = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == "tenant_demo",
                               SkillVersion.skill_id == SKILL, SkillVersion.scope_agent_id == AGENT)).all()
            ids = [row.id for row in instances]
            operations = db.exec(select(SopOperation).where(SopOperation.instance_id.in_(ids))).all() if ids else []
            sessions = db.exec(select(ChatSession).where(ChatSession.agent_id == AGENT)).all()
            messages = db.exec(select(Message).where(Message.session_id.in_([row.id for row in sessions]))).all() if sessions else []
            feedback = db.exec(select(MessageFeedback).where(MessageFeedback.session_id.in_([row.id for row in sessions]))).all() if sessions else []
            calls = [json.loads(line) for line in (control / "business-calls.safe.jsonl").read_text().splitlines()] if (control / "business-calls.safe.jsonl").exists() else []
            return {"business": BUSINESS, "business_calls": calls, "publication_count": len(versions),
                    "sessions": [{"id": row.id, "title": row.title} for row in sessions],
                    "messages": [{"id": row.id, "session_id": row.session_id, "role": row.role,
                                  "content_hash": digest(row.content)} for row in messages],
                    "instances": [{"id": row.id, "session_id": row.session_id, "kind": row.kind, "status": row.status,
                                   "skill_version_id": row.skill_version_id, "checksum": row.definition_checksum,
                                   "slots": {key: row.slots_json.get(key) for key in ("employee_id", "sponsor_id")}} for row in instances],
                    "operations": [{"id": row.id, "instance_id": row.instance_id, "status": row.status,
                                    "employee_id": row.request_json.get("employee_id"), "result": row.result_json,
                                    "request_hash": row.request_fingerprint} for row in operations],
                    "feedback": [{"id": row.id, "message_id": row.message_id, "session_id": row.session_id} for row in feedback]}
