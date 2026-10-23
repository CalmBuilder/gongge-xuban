"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : sd8_fullstack_fixture.py
@CallChain  : SD8隔离全栈 → 合法旧发布/真实SDK → 页面任务 → 只读账本摘要
@Description: 仅准备资源，观察真实模型与物理来源；受控HTTP坏JSON与供应商质量分开。
"""

from __future__ import annotations

import ast
import base64
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import Thread
import time
from uuid import uuid4

TENANT = "tenant_demo"
AGENT = "agent_sd8_owner"
VIEWER_AGENT = "agent_sd8_viewer"
EMPTY_AGENT = "agent_sd8_empty"
KB_NAME = "SD8 CYPRESS42 政策库"
TITLE = "CYPRESS42 完整政策"
OLD_SOURCE = (
    "# CYPRESS42 完整政策\n\n## 额度\nCYPRESS42额度：600元。\n\n## 截止\nCYPRESS42截止：16:00。"
)
NEW_SOURCE = (
    "# CYPRESS42 完整政策\n\n## 额度\nCYPRESS42额度：1500元。\n\n## 截止\nCYPRESS42截止：17:30。"
)


def digest(value: object) -> str:
    """仅输出规范JSON的SHA，原请求/回复和配置不进入证据。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def save(path: Path, value: object) -> None:
    """写入明确安全的合成身份/状态/布尔摘要。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def walk(value: object):
    """只读递归实际持久JSON，供检索回执和冻结能力身份提取。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def knowledge_request_summary(value: object) -> dict:
    """只记录原请求的固定合成目标布尔与摘要，不复制自由 query、方面或私密字段。"""
    request = value if isinstance(value, dict) else {}
    query = request.get("query")
    desired = request.get("desired_evidence")
    return {
        "query_is_string": isinstance(query, str),
        "query_hash": digest(query),
        "query_has_known_policy": isinstance(query, str) and "CYPRESS42" in query,
        "query_has_unknown_policy": isinstance(query, str) and "ZIRCON999UNKNOWN" in query,
        "desired_evidence_is_string": isinstance(desired, str),
        "desired_evidence_nonempty": isinstance(desired, str) and bool(desired.strip()),
        "desired_evidence_hash": digest(desired),
        "desired_has_known_policy": isinstance(desired, str) and "CYPRESS42" in desired,
        "desired_has_unknown_policy": isinstance(desired, str) and "ZIRCON999UNKNOWN" in desired,
    }


def facts(text: str) -> dict[str, bool]:
    """按固定业务值与实际数值词形判定，普通进一步/一并/一点帮助不是政策数值。"""
    without_codes = re.sub(r"CYPRESS42|ZIRCON999UNKNOWN", "", text)
    numerals = r"[0-9零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+"
    boundary = r"(?=$|[\s，,。；;！？!?、)）]|左右|以上|以下|前|后|整)"
    monetary = (rf"{numerals}\s*(?:元|圆|块)(?!化)|"
                rf"(?:RMB|CNY|USD|EUR|HKD|[¥￥])\s*{numerals}")
    clock = rf"{numerals}\s*(?:点|时)(?:半|{numerals}\s*分?)?{boundary}"
    duration = rf"{numerals}\s*(?:小时|分钟|秒钟|秒){boundary}"
    labelled = (rf"(?:额度|金额|截止(?:时间)?|时间)\s*[:：]?\s*"
                rf"(?:(?:为|是|约|大约|上限为|最高为|最低为|至少|至多|不超过)\s*)?"
                rf"(?:(?:人民币|RMB|CNY)\s*)?{numerals}"
                rf"(?=$|[\s，,。；;！？!?、)）:%元圆块点时分]|左右|以上|以下)")
    return {
        "policy_number_present": bool(
            re.search(
                rf"{monetary}|{clock}|{duration}|\d{{1,2}}\s*[:：]\s*\d{{2}}|{labelled}",
                without_codes,
            )
        ),
        "old_amount": bool(re.search(r"(?<!\d)600(?!\d)", text)),
        "old_deadline": bool(re.search(r"16\s*[:：]\s*00|16\s*[点时]", text)),
        "new_amount": bool(re.search(r"(?<!\d)1[,]?500(?!\d)", text)),
        "new_deadline": bool(re.search(r"17\s*[:：]\s*30|17\s*[点时]\s*30", text)),
        "explicit_no_answer": bool(
            re.search(
                r"未找到|没有.*(?:资料|证据|政策)|无.*(?:资料|证据|答案)|"
                r"无法.*(?:回答|确认|确定|提供)|缺乏|不足|未检索到",
                text,
            )
        ),
    }


def sdk_knowledge_input_summary(messages: object) -> list[dict]:
    """只认动作请求根视图及正式机械消息，历史同形 JSON 不可冒充当前执行证据。"""
    output = []
    seen = {}
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        parts = [content] if not isinstance(content, list) else [
            part.get("text") for part in content if isinstance(part, dict)
        ]
        for part in parts:
            if isinstance(part, str):
                try:
                    part = json.loads(part)
                except ValueError:
                    continue
            view = part.get("provider_execution_view") if isinstance(part, dict) else None
            if not isinstance(view, dict) or view.get("protocol_version") != "dynamic-v1":
                continue
            frames = [("execution_context", view.get("execution_context"))]
            for entry in view.get("messages", []) if isinstance(view.get("messages"), list) else []:
                if (isinstance(entry, dict) and entry.get("role") == "system"
                        and entry.get("message_kind") == "execution_context"
                        and isinstance(entry.get("content"), dict)):
                    frames.append(("execution_context_message", entry["content"].get("execution_context")))
            for origin, frame in frames:
                if not isinstance(frame, dict):
                    continue
                completed = frame.get("completed_steps")
                if not isinstance(completed, list):
                    continue
                for item in completed:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("step_key") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", key):
                        continue
                    value = item.get("model_output")
                    identity = (key, digest(value), digest(frame))
                    if identity in seen:
                        if origin not in seen[identity]["origins"]:
                            seen[identity]["origins"].append(origin)
                        continue
                    row = {"step_key": key, "model_output_hash": identity[1],
                           "execution_context_hash": identity[2], "origins": [origin],
                           "missing_output": value is None, "has_knowledge_result": False}
                    execution_id = frame.get("execution_id")
                    if isinstance(execution_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
                        row["execution_id"] = execution_id
                    if isinstance(value, dict) and value.get("outcome") in {
                        "evidence_found", "no_match", "insufficient",
                    }:
                        content = "\n".join(
                            str(entry.get("content") or entry.get("excerpt") or "")
                            for field in ("evidence_pack", "selected_concepts", "chunks")
                            for entry in (value.get(field) if isinstance(value.get(field), list) else [])
                            if isinstance(entry, dict)
                        )
                        sufficiency = value.get("evidence_sufficiency")
                        row.update(has_knowledge_result=True, outcome=value["outcome"],
                            sufficient=isinstance(sufficiency, dict) and sufficiency.get("satisfied") is True,
                            evidence_facts=facts(content))
                    output.append(row)
                    seen[identity] = row
    return output


def observe_sdk(control: Path) -> None:
    """包装最终真实SDK并逐字透传；仅隔离知识协议故障走真实loopback HTTP。"""
    from app.llm import client as module
    from app.observability.spans import current_llm_operation

    original_sdk = module.OpenAI

    def capture(method):
        """只捕获原方法调用上下文到同一SDK对象；线程不继承ContextVar也不丢阶段。"""

        def observed_method(self, *args, **kwargs):
            """原参数与原返回逐字透传，不替换模型、请求、重试或取消守卫。"""
            operation = current_llm_operation()
            if operation != "llm.request":
                self.client._sd8_operation = operation
            return method(self, *args, **kwargs)

        return observed_method

    for name in ("generate_json", "generate_text", "generate_text_stream"):
        setattr(module.LLMClient, name, capture(getattr(module.LLMClient, name)))

    class FaultHandler(BaseHTTPRequestHandler):
        """用真实HTTP返回合法包络内的坏业务JSON，不生成质量答案。"""

        def do_POST(self):
            """消费请求但不保存内容，返回会触发原解析/修复失败的固定协议包。"""
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            packet = {
                "id": "sd8-controlled-protocol",
                "object": "chat.completion",
                "created": 0,
                "model": "sd8-protocol-fault",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{broken"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            body = json.dumps(packet).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            """关闭HTTP原请求日志，凭据与请求体均不输出。"""

    fault = ThreadingHTTPServer(("127.0.0.1", 0), FaultHandler)
    Thread(target=fault.serve_forever, daemon=True).start()

    def append(row):
        """每个调用写一行安全完整观察，不丢失败或流中断。"""
        with (control / "sdk-observations.safe.jsonl").open("a") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def attach(client, owner=None):
        """超时with_options副本仍使用同一观察边界，所有成功结果原样返回。"""
        create = client.chat.completions.create
        options = client.with_options
        owner = owner or client

        def observed(**request):
            """记录真实传输身份、usage和耗时，坏JSON传输不计供应商质量。"""
            operation = getattr(owner, "_sd8_operation", current_llm_operation())
            controlled = operation.startswith("knowledge.") and (control / "protocol").exists()
            started = time.monotonic()
            row = {
                "call_id": "sd8call_" + uuid4().hex,
                "pid": os.getpid(),
                "started_at_unix": time.time(),
                "operation": operation,
                "request_hash": digest(request.get("messages")),
                "model_hash": digest(request.get("model")),
                "stream": bool(request.get("stream")),
                "actual_sdk_transport": True,
                "actual_provider_call": not controlled,
                "real_provider_quality": not controlled,
                "completed_model_outputs": sdk_knowledge_input_summary(request.get("messages")),
            }
            try:
                if controlled:
                    with original_sdk(
                        api_key="sd8-synthetic-protocol-key",
                        base_url=f"http://127.0.0.1:{fault.server_port}/v1",
                        max_retries=0,
                    ) as local:
                        result = local.chat.completions.create(**request)
                else:
                    result = create(**request)
            except BaseException:
                row.update(
                    status="provider_failure", duration_ms=(time.monotonic() - started) * 1000
                )
                append(row)
                raise
            if not request.get("stream"):
                usage = getattr(result, "usage", None)
                row.update(
                    status="completed",
                    duration_ms=(time.monotonic() - started) * 1000,
                    response_id_hash=digest(getattr(result, "id", None)),
                    response_hash=digest(result.model_dump(mode="json")),
                    finish_reasons=[
                        getattr(choice, "finish_reason", None) if getattr(choice, "finish_reason", None) in {
                            "stop", "length", "content_filter", "tool_calls", "function_call",
                        } else "unknown" for choice in (getattr(result, "choices", None) or [])
                    ],
                    usage={
                        k: getattr(usage, k, None)
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                    },
                )
                append(row)
                return result

            class ObservedStream:
                """流chunk逐字透传，摘要以真正消费/中断边界为准。"""

                def __iter__(self):
                    """保留原chunk和错误，在finally结算实际usage与安全响应SHA。"""
                    checksum = hashlib.sha256()
                    usage = None
                    response_id = None
                    status = "completed"
                    try:
                        for chunk in result:
                            checksum.update(chunk.model_dump_json().encode())
                            response_id = getattr(chunk, "id", response_id)
                            usage = getattr(chunk, "usage", None) or usage
                            yield chunk
                    except BaseException:
                        status = "stream_interrupted"
                        raise
                    finally:
                        row.update(
                            status=status,
                            duration_ms=(time.monotonic() - started) * 1000,
                            response_hash=checksum.hexdigest(),
                            response_id_hash=digest(response_id),
                            usage={
                                k: getattr(usage, k, None)
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                            },
                        )
                        append(row)

                def close(self):
                    """取消沿原SDK关闭，不改变业务和Runtime状态。"""
                    result.close()

            return ObservedStream()

        def with_options(**settings):
            """保留真实超时/SDK设置，副本继续观察。"""
            return attach(options(**settings), owner)

        client.chat.completions.create = observed
        client.with_options = with_options
        return client

    def sdk(**settings):
        """用原凭据与地址建立真实供应商SDK，禁止注入答案。"""
        client = original_sdk(**settings)
        client._sd8_operation = current_llm_operation()
        return attach(client)

    module.OpenAI = sdk


def observe_planning_errors(control: Path) -> None:
    """仅在隔离进程旁记原 start_task 异常位置及 schema 码，然后原样重新抛出。"""
    from pydantic import ValidationError
    from app.dynamic_tasks.agent import DynamicTaskAgent
    from app.dynamic_tasks.planner_service import _planner_failure_message

    original = DynamicTaskAgent.start_task
    root = Path(__file__).resolve().parents[2]
    path = control / "planning-observations.safe.json"

    def observed(self, *args, **kwargs):
        """不改变参数、调用次数、返回或异常对象，未知模型文本不进入归档。"""
        try:
            return original(self, *args, **kwargs)
        except Exception as error:
            try:
                frames = []
                trace = error.__traceback__
                while trace is not None:
                    source = Path(trace.tb_frame.f_code.co_filename).resolve()
                    if source.is_relative_to(root / "backend/app"):
                        frames.append({"file": str(source.relative_to(root)), "line": trace.tb_lineno})
                    trace = trace.tb_next
                row = {"exception_type": type(error).__name__, "error_hash": digest(str(error)),
                       "source_frames": frames, "schema_errors": [], "diagnostic_incomplete": False}
                if isinstance(error, ValidationError):
                    try:
                        row["schema_errors"] = json.loads(_planner_failure_message(error)).get("schema_errors", [])
                    except (TypeError, ValueError, AttributeError):
                        row["diagnostic_incomplete"] = True
                for field in ("tenant_id", "session_id", "agent_id"):
                    value = kwargs.get(field)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                        row[field] = value
                try:
                    previous = json.loads(path.read_text()) if path.exists() else []
                    if not isinstance(previous, list):
                        previous = []
                        row["diagnostic_incomplete"] = True
                except (OSError, ValueError):
                    previous = []
                    row["diagnostic_incomplete"] = True
                save(path, [*previous, row])
            except Exception:
                # 旁证不可阻止原宿主失败、重试或取消；不打印未知异常/凭据。
                pass
            raise

    DynamicTaskAgent.start_task = observed


def seed(control: Path) -> None:
    """只初始化账号、资源和沿原CAS合法旧发布；运行账本必须为0。"""
    from sqlmodel import Session, select
    from app.agents.branching import _ensure_binding, sync_knowledge_branch_from_overall
    from app.api.knowledge_bases import create_knowledge_base, update_knowledge_governance
    from app.api.skills import create_skill
    from app.db import engine, init_db
    from app.db.models import (
        AgentProfile,
        AgentUsage,
        SopInstance,
        SopOperation,
        Tenant,
        User,
    )
    from app.db.seed import seed_demo_data
    from app.knowledge.revisions import INITIAL_REVISION_DOCUMENT_ID, run_compatibility_revision
    from app.knowledge.schema import (
        KnowledgeBaseCreateRequest,
        KnowledgeBaseGovernanceUpdateRequest,
    )
    from app.security.auth import hash_password
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
        db.add(
            User(
                id="sd8_viewer",
                tenant_id=TENANT,
                username="sd8-viewer",
                role="member",
                password_hash=hash_password("sd8-viewer"),
            )
        )
        for agent_id, name, owner in [
            (AGENT, "SD8 知识员工", "admin"),
            (VIEWER_AGENT, "SD8 旧版员工", "sd8_viewer"),
            (EMPTY_AGENT, "SD8 无知识员工", "admin"),
        ]:
            db.add(
                AgentProfile(
                    id=agent_id,
                    tenant_id=TENANT,
                    name=name,
                    owner_user_id=owner,
                    status="active",
                    metadata_json={"owner_user_id": owner},
                )
            )
            db.add(AgentUsage(tenant_id=TENANT, user_id=owner, agent_id=agent_id))
        db.commit()
        admin = db.get(User, "admin")
        kb = create_knowledge_base(
            KnowledgeBaseCreateRequest(tenant_id=TENANT, name=KB_NAME),
            agent_id=None,
            db=db,
            current_user=admin,
        )
        update_knowledge_governance(
            kb.id,
            KnowledgeBaseGovernanceUpdateRequest(
                tenant_id=TENANT,
                expected_revision=kb.revision,
                access_scope="tenant",
                download_policy="restricted",
            ),
            db=db,
            current_user=admin,
        )
        job = run_compatibility_revision(
            db,
            tenant_id=TENANT,
            knowledge_base_id=kb.id,
            scope="overall",
            actor_user_id="admin",
            document_id=INITIAL_REVISION_DOCUMENT_ID,
            operation_kind="upload",
            mutation_payload={
                "filename": "cypress42.md",
                "title": TITLE,
                "content_md": OLD_SOURCE,
                "content_base64": base64.b64encode(OLD_SOURCE.encode()).decode(),
            },
        )
        if job.status != "completed":
            raise RuntimeError("SD8_INITIAL_REAL_PUBLICATION_FAILED")
        for agent_id in (AGENT, VIEWER_AGENT):
            _ensure_binding(db, TENANT, agent_id, "knowledge_base", kb.id, "active")
            sync_knowledge_branch_from_overall(db, TENANT, agent_id, kb.id)
        db.commit()
        for skill_id, name, query, desired in [
            (
                "sd8_policy_sop",
                "CYPRESS42政策查询SOP",
                "CYPRESS42额度和截止",
                "CYPRESS42额度；CYPRESS42截止",
            ),
            (
                "sd8_unknown_sop",
                "ZIRCON999UNKNOWN未知政策SOP",
                "ZIRCON999UNKNOWN",
                "ZIRCON999UNKNOWN",
            ),
        ]:
            card = SkillCard.model_validate(
                {
                    "skill_id": skill_id,
                    "name": name,
                    "version": "1.0.0",
                    "execution_mode": "deterministic",
                    "description": name,
                    "trigger_intents": [name],
                    "goal": ["沿knowledge.search证据回答；缺乏证据明确无答案"],
                    "nodes": [
                        {
                            "node_id": "search",
                            "type": "knowledge_query",
                            "name": query,
                            "instruction": query,
                            "allowed_actions": ["knowledge_query"],
                            "metadata": {
                                "operation_result_key": "policy_evidence",
                                "knowledge_query": {
                                    "query_type": "policy_check",
                                    "desired_evidence": desired,
                                },
                            },
                        },
                        {
                            "node_id": "done",
                            "type": "response",
                            "name": "依据完整证据回答",
                            "instruction": "只用本次检索证据回答。无证据明确未找到资料，不编造数值或引用。",
                            "allowed_actions": ["answer_user"],
                        },
                    ],
                    "edges": [{"source_node_id": "search", "next_node_id": "done"}],
                    "start_node_id": "search",
                    "terminal_node_ids": ["done"],
                }
            )
            create_skill(
                SkillCreateRequest(tenant_id=TENANT, content=card, status="published"),
                agent_id=AGENT,
                db=db,
                current_user=admin,
            )
        count_instances = len(db.exec(select(SopInstance)).all())
        count_operations = len(db.exec(select(SopOperation)).all())
        if count_instances or count_operations:
            raise RuntimeError("SD8_FORBIDS_PRESEEDED_EXECUTION")
        db.add(Tenant(id="tenant_sd8_foreign", name="SD8 isolated foreign tenant"))
        foreign_user = User(
            id="sd8_foreign_admin",
            tenant_id="tenant_sd8_foreign",
            username="sd8-foreign-admin",
            role="admin",
            password_hash=hash_password("sd8-foreign-synthetic"),
        )
        db.add(foreign_user)
        db.add(
            AgentProfile(
                id="agent_sd8_foreign_overall",
                tenant_id="tenant_sd8_foreign",
                name="SD8 foreign overall",
                is_overall=True,
                owner_user_id=foreign_user.id,
            )
        )
        db.commit()
        foreign_kb = create_knowledge_base(
            KnowledgeBaseCreateRequest(
                tenant_id="tenant_sd8_foreign", name="SD8 foreign knowledge"
            ),
            agent_id=None,
            db=db,
            current_user=foreign_user,
        )
        from app.db.models import KnowledgeBase
        from app.agents.branching import ensure_knowledge_base_version

        foreign_version = ensure_knowledge_base_version(db, db.get(KnowledgeBase, foreign_kb.id))
        db.commit()
        save(
            control / "initial.safe.json",
            {
                "knowledge_base_id": kb.id,
                "old_version_id": job.published_version_id,
                "old_document_id": job.published_document_id,
                "initial_job_id": job.id,
                "old_source_hash": digest(OLD_SOURCE),
                "expected_new_source_hash": digest(NEW_SOURCE),
                "initial_execution_count": 0,
                "initial_operation_count": 0,
                "foreign_version_id": foreign_version.id,
            },
        )


def install_controls(control: Path) -> None:
    """只控制本次隔离故障/重启时机，并返回真实账本；不制造业务成功。"""
    from fastapi import HTTPException
    from app.knowledge import revisions
    from app.main import app

    original_publish = revisions.publish_revision
    original_build = revisions._build_revision_claimed

    def publish(db, job_id):
        """候选真实构建后短暂等待，释放原会话事务，再执行原CAS发布。"""
        if (control / "hold").exists():
            from app.db.models import KnowledgeIngestJob

            job = db.get(KnowledgeIngestJob, job_id)
            save(
                control / "candidate.safe.json",
                {
                    "job_id": job.id,
                    "candidate_version_id": job.candidate_version_id,
                    "status": job.status,
                },
            )
            db.rollback()
            limit = time.monotonic() + 45
            while (control / "hold").exists() and time.monotonic() < limit:
                time.sleep(0.05)
        return original_publish(db, job_id)

    def build(db, job_id, worker_id, fence):
        """一次真实构建故障收口沿原worker，随后重试不更改CAS语义。"""
        if (control / "build-failure").exists():
            (control / "build-failure").unlink()
            raise revisions.KnowledgeRevisionInputError("SD8_CONTROLLED_BUILD_FAILURE")
        return original_build(db, job_id, worker_id, fence)

    revisions.publish_revision = publish
    revisions._build_revision_claimed = build

    @app.post("/__e2e/sd8/control/{action}")
    def control_action(action: str):
        """只接收固定隔离控制动作，不接收正文、版本或运行状态写入。"""
        mapping = {
            "protocol-on": ("protocol", True),
            "protocol-off": ("protocol", False),
            "hold-on": ("hold", True),
            "release": ("hold", False),
            "restart": ("restart.request", True),
            "build-failure": ("build-failure", True),
        }
        if action not in mapping:
            raise HTTPException(404)
        name, active = mapping[action]
        if active:
            (control / name).touch()
        else:
            (control / name).unlink(missing_ok=True)
        return {"accepted": True}

    @app.get("/__e2e/sd8/state")
    def state():
        """返回合成知识/任务/引用的身份与人工方面摘要，不输出模型原文。"""
        snapshot = read_state(control)
        save(control / "ledger-last.safe.json", snapshot)
        return snapshot


def payload_summary(db, value: dict) -> dict:
    """独立回查四层物理tenant/version与证据方面，不能由回答推断检索成功。"""
    from app.db.models import KnowledgeBucket, KnowledgeChunk, KnowledgeConcept, KnowledgeDocument

    versions = value.get("knowledge_base_version_ids", [])
    identities = {}
    valid = True
    for key, model in [
        ("selected_documents", KnowledgeDocument),
        ("selected_concepts", KnowledgeConcept),
        ("selected_buckets", KnowledgeBucket),
        ("chunks", KnowledgeChunk),
    ]:
        rows = []
        for item in value.get(key, []):
            row = db.get(model, item.get("id"))
            allowed = bool(
                row and row.tenant_id == TENANT and row.knowledge_base_version_id in versions
            )
            valid = valid and allowed
            rows.append(
                {
                    "id": item.get("id"),
                    "concept_id": item.get("concept_id"),
                    "tenant_id": row.tenant_id if row else None,
                    "version_id": row.knowledge_base_version_id if row else None,
                    "valid": allowed,
                }
            )
        identities[key] = rows
    evidence = "\n".join(
        str(item.get("content") or item.get("excerpt") or "")
        for item in value.get("evidence_pack", [])
    )
    return {
        "version_ids": versions,
        "outcome": value.get("outcome"),
        "degraded": value.get("degraded", False),
        "sufficiency": {
            key: item
            for key, item in (value.get("evidence_sufficiency") or {}).items()
            if key not in {"required_aspects", "covered_aspects"}
        },
        "required_aspect_hashes": [
            digest(item)
            for item in (value.get("evidence_sufficiency") or {}).get("required_aspects", [])
        ],
        "covered_aspect_hashes": [
            digest(item)
            for item in (value.get("evidence_sufficiency") or {}).get("covered_aspects", [])
        ],
        "identities": identities,
        "identity_valid": valid,
        "evidence_facts": facts(evidence),
        "evidence_chunk_ids": [x.get("chunk_id") for x in value.get("evidence_pack", [])],
        "result_hash": digest(value),
    }


def citation_summary(db, cite: dict, payloads: list[dict]) -> dict:
    """将持久引用回查本轮检索物理来源；旧摘录有效性不替代当前授权。"""
    from app.db.models import KnowledgeChunk, KnowledgeConcept

    candidate_ids = []
    for payload in payloads:
        if cite.get("chunk_id"):
            candidate_ids.extend(
                item.get("id")
                for item in payload.get("chunks", [])
                if item.get("id") == cite["chunk_id"]
            )
        elif cite.get("concept_id"):
            candidate_ids.extend(
                item.get("id")
                for item in payload.get("selected_concepts", [])
                if item.get("concept_id") == cite["concept_id"]
                or item.get("id") == cite["concept_id"]
            )
    model = KnowledgeChunk if cite.get("chunk_id") else KnowledgeConcept
    rows = [db.get(model, physical_id) for physical_id in candidate_ids]
    excerpt = str(cite.get("excerpt") or cite.get("content") or "")
    matched = [
        row
        for row in rows
        if row
        and excerpt
        and excerpt in str(getattr(row, "content", None) or getattr(row, "content_md", ""))
    ]
    return {
        **{
            k: cite.get(k)
            for k in ("id", "kind", "document_id", "bucket_id", "chunk_id", "concept_id")
        },
        "source_ids": [row.id for row in matched],
        "version_ids": [row.knowledge_base_version_id for row in matched],
        "source_valid": bool(matched),
        "excerpt_hash": digest(excerpt),
    }


def matches_sop_event(db, operation, instance, event, *, diagnostic=None) -> bool:
    """精确匹配原事件，失败只旁记固定原因码，不能降低来源或完整回执合同。"""
    from app.db.models import Message, SopNodeExecution

    def reject(reason):
        """只写固定分支名称，原查询/回执/正文不进入旁证。"""
        if diagnostic is not None:
            diagnostic["failure_reason"] = reason
        return False

    payload = event.payload_json
    user_id = payload.get("user_message_id")
    user = db.get(Message, user_id) if user_id else None
    node = db.get(SopNodeExecution, operation.node_execution_id)
    if not (
        payload.get("auto") is not True
        and user
        and user.role == "user"
        and user.tenant_id == TENANT
        and user.session_id == instance.session_id == event.session_id
        and payload.get("turn_id") == user.id
        and payload.get("source_message") == user.content
        and node
        and node.tenant_id == operation.tenant_id == instance.tenant_id == TENANT
        and node.instance_id == operation.instance_id == instance.id
    ):
        return reject("source_identity")
    if not (
        user.created_at.replace(tzinfo=None)
        <= operation.created_at.replace(tzinfo=None)
        <= event.created_at.replace(tzinfo=None)
    ):
        return reject("time_order")
    query = payload.get("query") or {}
    if not operation.request_json or any(
        query.get(k) != v for k, v in operation.request_json.items()
    ):
        return reject("query")
    receipt = operation.result_json
    if any(
        payload.get(k) != receipt.get(k)
        for k in ("outcome", "degraded", "evidence_sufficiency", "knowledge_base_version_ids")
    ):
        return reject("receipt")
    evidence = payload.get("evidence_pack", [])
    refs = receipt.get("evidence_refs", [])
    if bool(evidence) != bool(refs) or len(refs) > len(evidence):
        return reject("evidence_shape")
    if any(
        any(evidence[index].get(k) != v for k, v in ref.items()) for index, ref in enumerate(refs)
    ):
        return reject("evidence_refs")
    counts = {
        k: len(payload.get(k, []))
        for k in ("selected_documents", "selected_buckets", "selected_concepts", "chunks")
    }
    counts["evidence"] = len(evidence)
    return True if counts == receipt.get("counts") else reject("counts")


@lru_cache(maxsize=1)
def planner_error_markers() -> tuple:
    """实读原规划源码的固定异常文字，仅返回供诊断匹配的静态字面量及源码定位。"""
    root = Path(__file__).resolve().parents[2]
    markers = []
    for relative in ("backend/app/dynamic_tasks/planning.py",
                     "backend/app/dynamic_tasks/planner_service.py"):
        source = root / relative
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Raise):
                for part in ast.walk(node):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str) and len(part.value) >= 8:
                        markers.append((relative, node.lineno, part.value))
    return tuple(markers)


def planning_failure_summary(event) -> dict:
    """只保留原规划失败的身份/稳定码/摘要，拒绝诊断正文及非符号码值。"""
    payload = event.payload_json if isinstance(event.payload_json, dict) else {}
    code = str(payload.get("code") or "")
    diagnostic = str(payload.get("diagnostic") or "")
    return {"id": event.id, "session_id": event.session_id, "event_type": event.event_type,
            "code": code if re.fullmatch(r"[A-Za-z0-9_]{1,128}", code) else None,
            "source_error_matches": [
                {"file": file, "line": line} for file, line, marker in planner_error_markers()
                if marker in diagnostic
            ],
            "payload_hash": digest(payload)}


def read_state(control: Path) -> dict:
    """只读实际资源/Execution/Operation/Message，安全归档可直接重算criterion。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (
        AgentEvent,
        AgentKnowledgeBranch,
        ChatSession,
        ExecutionResult,
        GeneralSkill,
        GeneralSkillRevision,
        GeneralSkillUse,
        KnowledgeBaseVersion,
        KnowledgeDocument,
        KnowledgeIngestJob,
        Message,
        SopInstance,
        SopOperation,
    )

    initial = json.loads((control / "initial.safe.json").read_text())
    with Session(engine) as db:
        kb_id = initial["knowledge_base_id"]
        guidance_revisions = db.exec(
            select(GeneralSkillRevision, GeneralSkill)
            .join(GeneralSkill, GeneralSkill.id == GeneralSkillRevision.skill_id)
            .where(GeneralSkillRevision.tenant_id == TENANT, GeneralSkill.tenant_id == TENANT,
                   GeneralSkillRevision.id == GeneralSkill.current_published_revision_id,
                   GeneralSkill.slug.in_(["sd8-knowledge-guidance", "sd8-viewer-knowledge-guidance"]))
        ).all()
        guidance_uses = db.exec(
            select(GeneralSkillUse).where(
                GeneralSkillUse.tenant_id == TENANT,
                GeneralSkillUse.skill_id.in_([skill.id for _, skill in guidance_revisions]),
                GeneralSkillUse.agent_id.in_([AGENT, VIEWER_AGENT]),
            )
        ).all()
        versions = db.exec(
            select(KnowledgeBaseVersion).where(KnowledgeBaseVersion.knowledge_base_id == kb_id)
        ).all()
        documents = db.exec(
            select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id)
        ).all()
        branches = db.exec(
            select(AgentKnowledgeBranch).where(AgentKnowledgeBranch.knowledge_base_id == kb_id)
        ).all()
        instances = db.exec(
            select(SopInstance)
            .where(SopInstance.agent_id.in_([AGENT, VIEWER_AGENT]))
            .order_by(SopInstance.created_at)
        ).all()
        ids = [row.id for row in instances]
        planning_failures = db.exec(
            select(AgentEvent).join(ChatSession, AgentEvent.session_id == ChatSession.id).where(
                AgentEvent.tenant_id == TENANT,
                ChatSession.tenant_id == TENANT,
                ChatSession.agent_id.in_([AGENT, VIEWER_AGENT]),
                AgentEvent.event_type.in_([
                    "dynamic_task_delegation_failed", "dynamic_task_continuation_failed",
                ]),
            ).order_by(AgentEvent.created_at, AgentEvent.id)
        ).all()
        operations = (
            db.exec(select(SopOperation).where(SopOperation.instance_id.in_(ids))).all()
            if ids
            else []
        )
        sessions = [row.session_id for row in instances]
        messages = (
            db.exec(
                select(Message).where(Message.session_id.in_(sessions)).order_by(Message.created_at)
            ).all()
            if sessions
            else []
        )
        jobs = db.exec(
            select(KnowledgeIngestJob).where(KnowledgeIngestJob.knowledge_base_id == kb_id)
        ).all()
        events = (
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id.in_(sessions),
                    AgentEvent.event_type == "knowledge_query_finished",
                )
            ).all()
            if sessions
            else []
        )
        instance_map = {row.id: row for row in instances}
        result_map = {
            row.id: row
            for row in db.exec(
                select(ExecutionResult).where(ExecutionResult.execution_id.in_(ids))
            ).all()
        } if ids else {}

        def result_binding(row: Message) -> dict:
            """仅实际当前verified结果、校验和及冻结正文均一致时证明DTA最终消息。"""
            metadata = row.metadata_json or {}
            instance = instance_map.get(metadata.get("execution_id"))
            result = result_map.get(metadata.get("result_id"))
            valid = bool(
                instance is not None
                and instance.kind == "dynamic_task"
                and instance.tenant_id == row.tenant_id
                and instance.session_id == row.session_id
                and instance.status == "succeeded"
                and result is not None
                and result.tenant_id == row.tenant_id
                and result.execution_id == instance.id
                and instance.current_result_id == result.id
                and result.status == "verified"
                and (result.verification_json or {}).get("passed") is True
                and metadata.get("result_checksum") == result.checksum
                and row.content == (result.result_json or {}).get("markdown")
            )
            return {
                "result_id": metadata.get("result_id"),
                "result_status": result.status if result is not None else None,
                "result_binding_valid": valid,
            }

        op_rows = []
        turn_payloads = {}
        execution_payloads = {}
        turn_operations = {}
        for row in operations:
            results = [
                value for value in walk(row.result_json) if "outcome" in value and "chunks" in value
            ]
            matched_events = []
            candidate_events = []
            source_user_id = instance_map[row.instance_id].source_ref
            event_match_count = 0
            if row.operation_name == "knowledge.search" and not results:
                for event in events:
                    if (event.session_id == instance_map[row.instance_id].session_id
                            and event.payload_json.get("user_message_id") == source_user_id):
                        summary = {"id": event.id, "automatic": event.payload_json.get("auto") is True}
                        summary["full_match"] = matches_sop_event(
                            db, row, instance_map[row.instance_id], event, diagnostic=summary,
                        )
                        candidate_events.append(summary)
                matching = [
                    event
                    for event in events
                    if matches_sop_event(db, row, instance_map[row.instance_id], event)
                ]
                event_match_count = len(matching)
                if len(matching) == 1:
                    results.append(matching[0].payload_json)
                    matched_events.append(matching[0].id)
                    source_user_id = matching[0].payload_json["user_message_id"]
            session_id = instance_map[row.instance_id].session_id
            turn_payloads.setdefault((session_id, source_user_id), []).extend(results)
            execution_payloads.setdefault(row.instance_id, []).extend(results)
            if results:
                turn_operations.setdefault((session_id, source_user_id), []).append(row.id)
            op_rows.append(
                {
                    "id": row.id,
                    "instance_id": row.instance_id,
                    "name": row.operation_name,
                    "status": row.status,
                    "request_hash": digest(row.request_json),
                    "knowledge_request": knowledge_request_summary(row.request_json)
                    if row.operation_name == "knowledge.search" else None,
                    "result_hash": digest(row.result_json),
                    "matched_event_ids": matched_events,
                    "event_match_count": event_match_count,
                    "event_candidates": candidate_events,
                    "source_user_message_id": source_user_id,
                    "node_execution_id": row.node_execution_id,
                    "retrieval": [payload_summary(db, value) for value in results],
                }
            )
        return {
            "initial": initial,
            "guidance_resources": [
                {"skill_id": skill.id, "revision_id": revision.id, "slug": skill.slug,
                 "usage_mode": skill.usage_mode, "status": revision.status,
                 "content_checksum": revision.content_checksum,
                 "markdown_hash": digest(revision.normalized_skill_markdown),
                 "native_wait_contract": all(word in revision.normalized_skill_markdown for word in (
                     "clarification", "wait_input", "completed_steps",
                 )) and "ask_user" not in revision.normalized_skill_markdown}
                for revision, skill in guidance_revisions
            ],
            "guidance_uses": [
                {key: getattr(use, key) for key in (
                    "id", "session_id", "execution_id", "agent_id", "user_id", "skill_id",
                    "revision_id", "content_checksum", "status",
                )}
                for use in guidance_uses
            ],
            "planning_failures": [planning_failure_summary(row) for row in planning_failures],
            "pid": os.getpid(),
            "versions": [
                {
                    "id": row.id,
                    "version": row.version,
                    "scope": row.scope,
                    "state": row.publication_state,
                    "parent_id": row.parent_version_id,
                }
                for row in versions
            ],
            "documents": [
                {
                    "id": row.id,
                    "version_id": row.knowledge_base_version_id,
                    "lineage_id": row.lineage_id,
                    "source_hash": digest(row.normalized_source),
                    "source_facts": facts(row.normalized_source or ""),
                }
                for row in documents
            ],
            "branches": [
                {
                    "agent_id": row.agent_id,
                    "head_version": row.head_version,
                    "revision": row.content_revision,
                }
                for row in branches
            ],
            "instances": [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "agent_id": row.agent_id,
                    "initiator_user_id": row.initiator_user_id,
                    "kind": row.kind,
                    "status": row.status,
                    "skill_version_id": row.skill_version_id,
                    "plan_checksum": row.current_plan_checksum,
                    "current_result_id": row.current_result_id,
                    "definition_checksum": row.definition_checksum,
                    "frozen_knowledge_versions": sorted(
                        {
                            str(card["version_id"])
                            for value in walk(row.capability_snapshot_json)
                            for card in value.get("knowledge_bases", [])
                            if isinstance(card, dict) and card.get("version_id")
                        }
                    ),
                }
                for row in instances
            ],
            "operations": op_rows,
            "messages": [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "role": row.role,
                    "content_hash": digest(row.content),
                    "source_user_message_id": (row.metadata_json or {}).get("user_message_id"),
                    "execution_id": (row.metadata_json or {}).get("execution_id"),
                    "message_kind": (row.metadata_json or {}).get("message_kind"),
                    "execution_status": (row.metadata_json or {}).get("execution_status"),
                    **result_binding(row),
                    "matched_operation_ids": turn_operations.get(
                        (row.session_id, (row.metadata_json or {}).get("user_message_id")), []
                    ),
                    "facts": facts(row.content),
                    "citations": [
                        citation_summary(
                            db,
                            cite,
                            execution_payloads.get(
                                (row.metadata_json or {}).get("execution_id"), []
                            )
                            if (row.metadata_json or {}).get("execution_id") in instance_map
                            and instance_map[(row.metadata_json or {})["execution_id"]].kind
                            == "dynamic_task"
                            and instance_map[(row.metadata_json or {})["execution_id"]].session_id
                            == row.session_id
                            else turn_payloads.get(
                                (row.session_id, (row.metadata_json or {}).get("user_message_id")),
                                [],
                            ),
                        )
                        for cite in (row.metadata_json or {}).get("knowledge_citations", [])
                    ],
                }
                for row in messages
            ],
            "jobs": [
                {
                    "id": row.id,
                    "document_id": row.document_id,
                    "status": row.status,
                    "candidate_version_id": row.candidate_version_id,
                    "published_version_id": row.published_version_id,
                    "published_document_id": row.published_document_id,
                    "error_code": row.error_code,
                }
                for row in jobs
            ],
            "candidate": json.loads((control / "candidate.safe.json").read_text())
            if (control / "candidate.safe.json").exists()
            else None,
            "sdk_calls": [
                json.loads(line)
                for line in (control / "sdk-observations.safe.jsonl").read_text().splitlines()
            ]
            if (control / "sdk-observations.safe.jsonl").exists()
            else [],
        }


def prepare_sd8(runtime: Path, *, reuse: bool) -> None:
    """限定本次真实临时SQLite/随机MySQL，重启只复用事实而不重置资源。"""
    from app.db import engine

    if engine.dialect.name == "mysql":
        if not (engine.url.database or "").startswith("gongge_xuban_test_"):
            raise RuntimeError("SD8_REQUIRES_RANDOM_MYSQL")
    elif engine.dialect.name != "sqlite" or not Path(
        engine.url.database or ""
    ).resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("SD8_REQUIRES_TEMPORARY_SQLITE")
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"]).resolve()
    if not control.is_relative_to(runtime.parent.resolve()):
        raise RuntimeError("SD8_CONTROL_OUTSIDE_OWNED_RUNTIME")
    control.mkdir(parents=True, exist_ok=True)
    observe_sdk(control)
    observe_planning_errors(control)
    if not reuse:
        seed(control)
    install_controls(control)
