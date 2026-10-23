"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : sd6a_fullstack_fixture.py
@CallChain  : 隔离Playwright服务 → 合成记忆/SOP → 真实LLMClient/SDK → 安全原始观察
@Description: 仅准备测试事实并观察真实SDK，不替换模型回答、计划或记忆提取结果。
"""

import hashlib
import json
import os
import re
import time
from functools import partial
from pathlib import Path
from threading import Lock


def prepare_sd6a(runtime: Path, *, reuse: bool) -> None:
    """仅显式SD6A隔离LIVE运行启用，首次seed合成事实，重启保持原数据库。"""
    if os.environ.get("FULLSTACK_SD6A") != "1":
        return
    if os.environ.get("LIVE_ATTACHMENT_E2E") != "1":
        raise RuntimeError("SD6A_BROWSER_REQUIRES_REAL_PROVIDER")
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not control.is_relative_to(runtime.parent.resolve()):
        raise RuntimeError("SD6A_CONTROL_OUTSIDE_ISOLATED_RUNTIME")
    control.mkdir(parents=True, exist_ok=True)
    if not reuse:
        _seed()
    observe_sd6a_sdk(runtime)
    _install_safe_state()


def observe_sd6a_sdk(runtime: Path) -> None:
    """预检前启用同一真实SDK连接池；只允许显式隔离LIVE范围，不替换预检结果。"""
    if os.environ.get("FULLSTACK_SD6A") != "1":
        return
    if os.environ.get("LIVE_ATTACHMENT_E2E") != "1":
        raise RuntimeError("SD6A_BROWSER_REQUIRES_REAL_PROVIDER")
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not control.is_relative_to(runtime.parent.resolve()):
        raise RuntimeError("SD6A_CONTROL_OUTSIDE_ISOLATED_RUNTIME")
    control.mkdir(parents=True, exist_ok=True)
    _observe_sdk(control)


def _seed() -> None:
    """在当前隔离库准备三例/对照及等待恢复作用域，公开管理事实保持完整。"""
    from sqlmodel import Session

    from app.agents.branching import ensure_agent_skill_branch, ensure_private_resource_binding
    from app.db import engine
    from app.db.models import AgentProfile, AgentUsage, MemoryRecord, Skill, SkillVersion
    from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card

    with Session(engine) as db:
        for branch in ("sop", "dta"):
            for case in ("1", "2", "3", "control", "freeze", "clear", "revoke"):
                agent_id = f"agent_sd6a_{branch}_{case}"
                owner = "admin" if case in {"freeze", "revoke"} else "member_e2e"
                db.add(AgentProfile(
                    id=agent_id, tenant_id="tenant_demo", name=f"SD6A {branch.upper()} {case}",
                    owner_user_id=owner, status="active", published_to_gallery=owner == "admin",
                    visibility_scope="tenant", description="隔离完整记忆验收员工。",
                    metadata_json={"owner_user_id": owner, "owner_username":
                                   "admin" if owner == "admin" else "member"},
                ))
                db.flush()
                db.add(AgentUsage(tenant_id="tenant_demo", user_id="member_e2e", agent_id=agent_id))
                for suffix, kind, key, content in (
                    ("name", "profile", "preferred_name", "青禾"),
                    ("style", "preference", "reply_style", "输出偏好：简洁要点；原样保留代码 a  b\n\t校验结束。"),
                    ("large", "fact", "reference_code", "口令：SD6A-UNSELECTED-SECRET。" + "完整长事实。" * 700),
                ):
                    db.add(MemoryRecord(id=f"mem_sd6a_{branch}_{case}_{suffix}",
                                        tenant_id="tenant_demo", user_id="member_e2e", username="member",
                                        agent_id=agent_id, kind=kind, content=content,
                                        metadata_json={"key": key}, importance=0.9))
                if branch == "sop":
                    skill_id = f"sd6a_memory_check_{case}"
                    fields = ["user_name", "reply_style", "omitted_code"]
                    content = {
                        "skill_id": skill_id, "name": "完整记忆核验流程", "version": "1.0.0",
                        "execution_mode": "deterministic", "description":
                        "办理记忆核验流程；Router填写user_name、reply_style、omitted_code。"
                        "user_name为实际可用姓名。reply_style只填输出格式名称，冒号后的名称完整保留，"
                        "分号后说明和代码不属于名称；本轮明确纠正优先，未知格式填未知。"
                        "omitted_code只填实际投影已知口令，未知口令填未知。"
                        "不能把用户要求的关键词等格式要求当作字段事实。",
                        "required_info": fields, "condition_schemas": {"slots": {
                            "type": "object", "properties": {
                                "user_name": {"type": "string", "description": "实际可用姓名"},
                                "reply_style": {"type": "string", "description":
                                    "仅输出格式名称；条目冒号后的名称完整保留，分号后的说明和代码不属于名称。"
                                    "本轮明确纠正优先；没有实际可用格式时填未知，不把关键词等格式要求当作事实。"},
                                "omitted_code": {"type": "string", "description":
                                    "仅实际投影中已知口令；没有可用事实时填未知，不推断未投影条目。"},
                            }}},
                        "nodes": [{"node_id": "collect", "type": "collect_input", "name": "核对记忆字段",
                                   "expected_user_info": fields, "allowed_actions": ["ask_user", "continue_flow"]},
                                  {"node_id": "done", "type": "terminal", "name": "核验完成"}],
                        "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
                        "start_node_id": "collect", "terminal_node_ids": ["done"],
                    }
                    definition = compile_legacy_skill_card(content)
                    skill = Skill(tenant_id="tenant_demo", skill_id=skill_id, version="1.0.0",
                                  name="完整记忆核验流程", description=content["description"],
                                  content_json=content, status="published")
                    db.add(skill)
                    db.flush()
                    db.add(SkillVersion(id=f"skillver_sd6a_{case}", tenant_id="tenant_demo",
                                        skill_id=skill_id, version="1.0.0", name=skill.name,
                                        content_json=content, status="published",
                                        compiled_definition_checksum=definition.checksum))
                    db.flush()
                    ensure_agent_skill_branch(db, "tenant_demo", agent_id, skill)
                    ensure_private_resource_binding(db, "tenant_demo", agent_id, "skill", skill.id, "active")
        db.commit()


def _observe_sdk(control: Path) -> None:
    """包装真实SDK只记录摘要/usage/完整性；没有受控模型响应或预置成功。"""
    import httpx
    from openai import DefaultHttpxClient
    from openai._constants import DEFAULT_CONNECTION_LIMITS

    from app.llm import client as client_module
    from app.llm.client import LLMClient, _loads_llm_json, _request_tokens
    from app.memory.projection import MemoryProjectionPolicy
    from app.memory.request_projection import MemoryRequestPayload
    from app.observability.spans import current_llm_operation

    if getattr(LLMClient, "_sd6a_sdk_observed", False):
        return
    original_init = LLMClient.__init__
    original_json = LLMClient.generate_json
    lock = Lock()
    original_sdk = client_module.OpenAI
    transport = httpx.HTTPTransport(limits=DEFAULT_CONNECTION_LIMITS)

    class IsolatedTransport(httpx.BaseTransport):
        """共享真实连接池；各SDK客户端取消/关闭只终止本客户端，不关闭其他模型调用。"""

        def handle_request(self, request):
            """真实传输仅在发送正文前的瞬时连接拒绝重试两次，超时和已发请求不重试。"""
            for attempt in range(3):
                try:
                    return transport.handle_request(request)
                except httpx.ConnectError as exc:
                    if attempt == 2 or "refused" not in str(exc).casefold():
                        raise
                    time.sleep(0.25)
            raise RuntimeError("ISOLATED_CONNECT_RETRY_EXHAUSTED")

        def close(self):
            """底层池归本次独占服务进程；退出由操作系统回收，不影响同进程其他SDK。"""

    def sdk_with_pool(**settings):
        """隔离服务共享SDK默认HTTP池，避免本机转发器反复新建连接；仍真实TLS/供应商。"""
        settings.setdefault("http_client", DefaultHttpxClient(transport=IsolatedTransport()))
        return original_sdk(**settings)

    client_module.OpenAI = sdk_with_pool

    def digest(value: object) -> str:
        """以稳定摘要替代请求、回复及身份正文。"""
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         default=str).encode()).hexdigest()

    def generate(client, system_prompt, user_payload, **kwargs):
        """沿真实生成执行，仅为事先固定的对照作用域收紧记忆预算。"""
        client._sd6a_payload = user_payload
        client._sd6a_operation = current_llm_operation()
        if isinstance(user_payload, MemoryRequestPayload):
            envelope = user_payload.envelope
            if any(item.scope[2].endswith("_control") for item in envelope.candidates):
                envelope.policy = MemoryProjectionPolicy(configured_memory_limit=0,
                    max_items=envelope.policy.max_items, max_fact_characters=envelope.policy.max_fact_characters)
        return original_json(client, system_prompt, user_payload, **kwargs)

    def initialize(client, *args, **kwargs):
        """保持真实凭据/模型/SDK配置，观测每次实际Chat Completions发送。"""
        original_init(client, *args, **kwargs)
        original_create = client.client.chat.completions.create
        original_options = client.client.with_options

        def create(*, _transport=original_create, **request):
            """只在真实SDK完成后写安全原始事实，供应商错误不落原文。"""
            payload = getattr(client, "_sd6a_payload", None)
            envelope = payload.envelope if isinstance(payload, MemoryRequestPayload) else None
            selected = envelope.selected or () if envelope is not None else ()
            messages = request.get("messages", [])
            text = "\n".join(str(message.get("content", "")) for message in messages)
            structured_contents = []

            def collect(value):
                """解析JSON完整值计数，避免把换行转义误报为正文截断。"""
                if isinstance(value, dict):
                    memories = value.get("memory_context")
                    if isinstance(memories, list):
                        structured_contents.extend(item.get("content") for item in memories if isinstance(item, dict))
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            for message in messages:
                try:
                    collect(json.loads(message.get("content", "")))
                except (ValueError, TypeError):
                    pass
            revoked = (tuple(item for item in envelope.candidates if envelope.validate
                             and not envelope.validate(item)) if envelope else ())
            row = {"actual_provider_call": True, "operation": getattr(
                       client, "_sd6a_operation", current_llm_operation()),
                   "request_hash": digest(messages), "request_estimate": _request_tokens(messages),
                   "output_reserve": request.get("max_tokens"), "model_hash": digest(request.get("model")),
                   "source_input_id": envelope.source_input_id if envelope else "",
                   "selected": [{"id": item.id, "hash": hashlib.sha256(item.content.encode()).hexdigest(),
                                 "complete_in_sdk": (item.content in structured_contents
                                                     if structured_contents else item.content in text),
                                 "occurrences": (structured_contents.count(item.content)
                                                 if structured_contents else text.count(item.content))} for item in selected],
                   "sidecar_absent": all(key not in text for key in ("candidate_digest", "policy_version", "generations")),
                   "revoked_fact_markers_absent": all(item.content not in text for item in revoked),
                   "projection_audit": envelope.audits[-1] if envelope and envelope.audits else None}
            try:
                completion = _transport(**request)
                if request.get("stream"):
                    row.update(status="stream_opened", usage=None)
                    return completion
                message = completion.choices[0].message.content or ""
                row.update(status="completed", response_hash=digest(message),
                           response_id_hash=digest(completion.id), usage={
                           key: getattr(completion.usage, key, None) for key in
                           ("prompt_tokens", "completion_tokens", "total_tokens")})
                try:
                    parsed = _loads_llm_json(message)
                    hints = parsed.get("slot_hints") or {}
                    updates = parsed.get("slot_updates") or {}
                    markdown = (parsed.get("arguments") or {}).get("markdown", "")
                    labeled = dict(re.findall(r"\b(NAME|STYLE|CODE)\b[\s*`]*=[\s*`]*([^\r\n]+)",
                                              markdown))
                    labeled.update(re.findall(
                        r"^\s*\|[\s*`]*(NAME|STYLE|CODE)[\s*`]*\|[\s*`]*([^|\r\n]+)\|",
                        markdown, flags=re.MULTILINE))
                    labeled = {key: value.strip().rstrip(" *`") for key, value in labeled.items()}
                    row["label_observations"] = {
                        "router_name_matches": hints.get("user_name") == "青禾",
                        "router_style_base_matches": hints.get("reply_style") == "简洁要点",
                        "router_style_correction_matches": hints.get("reply_style") == "详细表格",
                        "router_name_omitted": "user_name" not in hints,
                        "router_name_unknown": hints.get("user_name") == "未知",
                        "router_style_unknown": hints.get("reply_style") == "未知",
                        "router_style_format_instruction": hints.get("reply_style") == "关键词",
                        "step_name_unknown": updates.get("user_name") == "未知",
                        "step_style_unknown": updates.get("reply_style") == "未知",
                        "step_style_format_instruction": updates.get("reply_style") == "关键词",
                        "answer_name_matches": labeled.get("NAME") == "青禾",
                        "answer_style_base_matches": labeled.get("STYLE") == "简洁要点",
                        "answer_style_correction_matches": labeled.get("STYLE") == "详细表格",
                        "unknown_code_matches": labeled.get("CODE") == "未知" or hints.get("omitted_code") == "未知",
                    }
                except (ValueError, TypeError, AttributeError):
                    row["label_observations"] = {"invalid_model_json": True}
                return completion
            except Exception as exc:
                causes = []
                error = exc
                while error is not None and len(causes) < 6:
                    causes.append(type(error).__name__)
                    error = error.__cause__
                row.update(status="failed", safe_error_type=type(exc).__name__, cause_types=causes)
                raise
            finally:
                with lock, (control / "sdk-observations.safe.jsonl").open("a") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        client.client.chat.completions.create = create

        def with_options(**options):
            """连同真实SDK超时克隆一起观测，保留SDK原传输及配置语义。"""
            configured = original_options(**options)
            configured.chat.completions.create = partial(create, _transport=configured.chat.completions.create)
            return configured

        client.client.with_options = with_options

    LLMClient.__init__ = initialize
    LLMClient.generate_json = generate
    LLMClient._sd6a_sdk_observed = True


def _install_safe_state() -> None:
    """只在隔离服务提供范围受限的只读摘要，浏览器以真实账本核对旧计划。"""
    from fastapi import HTTPException
    from sqlmodel import Session, select

    from app.db import engine
    from app.db.models import ChatSession, MemoryCaptureJob, MemoryRecord, SopInstance
    from app.main import app
    from app.memory.projection import MemoryCandidate
    from app.memory.service import MemoryService

    @app.get("/__e2e/sd6a/state/{agent_id}")
    def state(agent_id: str):
        """返回合成范围的选择摘要、状态与实际槽位，不输出模型请求/回复。"""
        if not agent_id.startswith("agent_sd6a_"):
            raise HTTPException(status_code=404)
        with Session(engine) as db:
            validate = MemoryService(db).projection_validator("tenant_demo", "member_e2e", agent_id=agent_id)
            instances = db.exec(select(SopInstance).where(SopInstance.agent_id == agent_id).order_by(SopInstance.created_at)).all()
            executions = []
            for instance in instances:
                snapshot = (instance.context_json or {}).get("memory_projection") or {}
                selected = snapshot.get("selected", [])
                executions.append({"id": instance.id, "kind": instance.kind, "status": instance.status,
                    "plan_checksum": instance.current_plan_checksum,
                    "slots": {key: instance.slots_json.get(key) for key in
                        ("user_name", "reply_style", "omitted_code")},
                    "projection_policy": snapshot.get("policy_version"),
                    "selected": [{"id": value["id"], "hash": hashlib.sha256(value["content"].encode()).hexdigest(),
                                  "currently_allowed": validate(MemoryCandidate.restore(value))} for value in selected]})
            sessions = db.exec(select(ChatSession).where(ChatSession.agent_id == agent_id).order_by(ChatSession.created_at)).all()
            records = db.exec(select(MemoryRecord).where(MemoryRecord.agent_id == agent_id)).all()
            jobs = {job.id: job for job in db.exec(select(MemoryCaptureJob).where(
                MemoryCaptureJob.agent_id == agent_id)).all()}
            return {"executions": executions,
                    "sessions": [{"id": value.id, "slots": {key: value.slots_json.get(key) for key in
                        ("user_name", "reply_style", "omitted_code")},
                        "router_audit": value.context_state_json.get("memory_projection_audit", [])} for value in sessions],
                    "records": [{"id": value.id, "characters": len(value.content),
                                 "source_sequence": value.source_sequence,
                                 "source_input_id": (jobs[value.capture_job_id].source_message_id
                                     if value.capture_job_id in jobs else None),
                                 "hash": hashlib.sha256(value.content.encode()).hexdigest()} for value in records]}
