"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : final_f3_live_identity.py
@CallChain  : 原固定调用点 → 精确持久身份 → 原实际SDK输入与人工事实安全旁证
@Description: 自有live旁证绑定真实调用来源，并将管理员模型预检独立归档，不授予业务资格。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from urllib.parse import parse_qs, unquote
from uuid import uuid4

from final_f3_preparation import digest
from final_f3_sdk_identity import (
    bind_model_preflight_admin,
    bind_model_preflight_config,
    bind_observation_handoff,
    bind_sdk_identity,
    model_preflight_probe,
    model_preflight_diagnostic_scope,
    model_preflight_request_scope,
    sdk_identity,
)

_frame: ContextVar[dict | None] = ContextVar("f3_live_caller_frame", default=None)


class ModelPreflightDiagnosticMiddleware:
    """只为租户模型管理路由建独立预检invocation，并以响应hash返回UI旁证。"""

    def __init__(self, app):
        """包装原ASGI应用，不读取认证头或请求正文。"""
        self.app = app

    async def __call__(self, scope, receive, send):
        """给模型测试/能力预检的原SDK派发添加请求边界并透传响应。"""
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        match = re.fullmatch(
            r"/api/enterprise/model-configs/([^/]+)/(test|preflight-dynamic)",
            str(scope.get("path") or ""),
        )
        if match is None:
            await self.app(scope, receive, send)
            return
        query = parse_qs(bytes(scope.get("query_string") or b"").decode("ascii", "ignore"))
        tenant_values = query.get("tenant_id", [])
        if len(tenant_values) != 1 or not tenant_values[0]:
            await self.app(scope, receive, send)
            return
        tenant_id = tenant_values[0]
        config_id = unquote(match.group(1))
        invocation_sha256 = digest({
            "nonce": uuid4().hex,
            "method": "POST",
            "route": match.group(2),
            "tenant_sha256": digest(tenant_id),
            "model_config_sha256": digest(config_id),
        })

        async def send_with_invocation(message):
            """响应头返回invocation与实际配置摘要，不暴露任何配置正文。"""
            if message.get("type") == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {
                               b"x-f3-diagnostic-invocation-sha256",
                               b"x-f3-diagnostic-config-fingerprint-sha256",
                           }]
                headers.append((b"x-f3-diagnostic-invocation-sha256", invocation_sha256.encode("ascii")))
                diagnostic = model_preflight_diagnostic_scope()
                config_fingerprint = (diagnostic or {}).get("config_fingerprint_sha256")
                if isinstance(config_fingerprint, str) and re.fullmatch(
                    r"[a-f0-9]{64}", config_fingerprint
                ):
                    headers.append((b"x-f3-diagnostic-config-fingerprint-sha256",
                                    config_fingerprint.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        with model_preflight_request_scope(
            invocation_sha256=invocation_sha256,
            tenant_id=tenant_id,
            model_config_id=config_id,
        ):
            await self.app(scope, receive, send_with_invocation)


def exact_caller_identity(db, *, kind: str, tenant_id: str, user_id: str,
                          agent_id: str, session_id: str | None, source_input_id: str | None,
                          client_turn_id: str | None, instance_id: str | None = None,
                          signal_id: str | None = None) -> dict[str, str]:
    """只关联固定调用参数或精确原实例，不使用最近消息、固定本人或历史补位。"""
    from app.db.models import ExecutionSignal, Message, SopInstance

    identity = {"kind": kind, "tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id}
    if kind not in {"sop", "dynamic_task"} or not all((tenant_id, user_id, agent_id)):
        raise ValueError("F3_LIVE_CALLER_IDENTITY_MISMATCH")
    if session_id:
        identity["session_id"] = session_id
    if instance_id:
        instance = db.get(SopInstance, instance_id)
        if (instance is None or instance.kind != kind or instance.tenant_id != tenant_id
                or instance.agent_id != agent_id or instance.initiator_user_id != user_id
                or instance.session_id != session_id):
            raise ValueError("F3_LIVE_CALLER_IDENTITY_MISMATCH")
        identity["instance_id"] = instance.id
        for key, field in (("plan_id", "current_plan_revision_id"), ("skill_version_id", "skill_version_id")):
            item = getattr(instance, field, None)
            if item:
                identity[key] = item
    if source_input_id:
        source = db.get(Message, source_input_id)
        if (source is None or source.role != "user" or source.tenant_id != tenant_id
                or source.session_id != session_id
                or client_turn_id and (source.metadata_json or {}).get("client_turn_id") != client_turn_id):
            raise ValueError("F3_LIVE_CALLER_IDENTITY_MISMATCH")
        identity.update(source_input_id=source.id, turn_id=source.id)
    if signal_id:
        signal = db.get(ExecutionSignal, signal_id)
        if signal is None or signal.tenant_id != tenant_id or signal.execution_id != instance_id:
            raise ValueError("F3_LIVE_CALLER_IDENTITY_MISMATCH")
        identity["signal_id"] = signal.id
    return identity


def exact_memory_capture_identity(db, *, job_id: str, source_sha256: str) -> dict[str, str] | None:
    """记忆worker只从指定持久job、用户消息和所属会话构造旁证身份。"""
    from app.db.models import ChatSession, MemoryCaptureJob, Message

    job = db.get(MemoryCaptureJob, job_id)
    if job is None or job.id != job_id:
        return None
    source = db.get(Message, job.source_message_id)
    if (source is None or source.id != job.source_message_id or source.role != "user"
            or source.tenant_id != job.tenant_id or not source.session_id):
        return None
    session = db.get(ChatSession, source.session_id)
    if (session is None or session.id != source.session_id or session.tenant_id != job.tenant_id
            or session.user_id != job.user_id or job.agent_id != session.agent_id):
        return None
    if job.source_assistant_message_id:
        assistant = db.get(Message, job.source_assistant_message_id)
        assistant_metadata = (assistant.metadata_json or {}) if assistant is not None else {}
        assistant_turn_matches = bool(
            all(str(assistant_metadata.get(key) or source.id) == source.id
                for key in ("turn_id", "user_message_id"))
            and any(assistant_metadata.get(key) == source.id
                    for key in ("turn_id", "user_message_id"))
        )
        if (assistant is None or assistant.id != job.source_assistant_message_id
                or assistant.role != "assistant" or assistant.tenant_id != job.tenant_id
                or assistant.session_id != session.id
                or not assistant_turn_matches):
            return None
    identity = {
        "tenant_id": job.tenant_id,
        "user_id": job.user_id,
        "session_id": session.id,
        "source_input_id": source.id,
        "turn_id": source.id,
        "task_id": job.id,
        "source_sha256": source_sha256,
    }
    agent_id = job.agent_id or session.agent_id
    if agent_id:
        identity["agent_id"] = agent_id
    return identity


def exact_session_title_identity(db, *, tenant_id: str, user_id: str, session_id: str,
                                 agent_id: str | None, source_sha256: str) -> dict[str, str] | None:
    """标题旁证复用原标题worker相同会话范围及六条消息窗口内的首个用户轮次。"""
    from app.db.models import ChatSession, Message
    from sqlmodel import select

    session = db.exec(select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id,
        ChatSession.user_id == user_id,
    )).first()
    if session is None or session.id != session_id or session.tenant_id != tenant_id \
            or session.user_id != user_id:
        return None
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
        .limit(6)
    ).all()
    source = next((row for row in messages if row.role == "user"), None)
    if (source is None or source.tenant_id != tenant_id or source.session_id != session_id
            or not source.id):
        return None
    effective_agent_id = agent_id or session.agent_id
    identity = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "source_input_id": source.id,
        "turn_id": source.id,
        "source_sha256": source_sha256,
    }
    if effective_agent_id:
        identity["agent_id"] = effective_agent_id
    return identity


def _decoded_input(value: str) -> object | None:
    """只解析原JSON或原LLM固定本轮前缀，不从自由文本搜索JSON片段。"""
    prefix = "本轮输入（仅用于当前调用，不写入对话历史）：\n"
    serialized = value[len(prefix):] if value.startswith(prefix) else value
    try:
        return json.loads(serialized)
    except (ValueError, TypeError):
        return None


def _strings(value: object):
    """提取实际消息及JSON内完整字符串，不根据人工标签合成内容。"""
    if isinstance(value, str):
        yield value
        decoded = _decoded_input(value)
        if isinstance(decoded, (dict, list)):
            yield from _strings(decoded)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def wire_fact_summary(kwargs: dict, labels: dict) -> dict:
    """读取实际SDK输入全消息，原请求摘要关联人工事实完整出现，不自评质量。"""
    text = list(_strings(kwargs.get("messages", [])))
    def complete(label: object) -> bool:
        """完整人工事实须出现于实际输入，空标签或输出期待不能证明消费。"""
        return isinstance(label, str) and bool(label) and any(label in value for value in text)

    versions = set()
    def version_strings(value: object) -> None:
        """从实际SDK JSON知识结果读取原版本数组，不按人工标签补位。"""
        if isinstance(value, str):
            decoded = _decoded_input(value)
            if isinstance(decoded, (dict, list)):
                version_strings(decoded)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"knowledge_base_version_ids", "knowledge_version_ids"} and isinstance(item, list):
                    versions.update(value for value in item if isinstance(value, str))
                else:
                    version_strings(item)
        elif isinstance(value, list):
            for item in value:
                version_strings(item)
    version_strings(kwargs.get("messages", []))
    return {"request_sha256": digest(kwargs), "knowledge_old_complete": complete(labels.get("knowledge_old")),
            "knowledge_new_complete": complete(labels.get("knowledge_new")),
            "memory_complete": [complete(item) for item in labels.get("memories", [])],
            "has_knowledge_result": bool(versions), "knowledge_version_ids": sorted(versions),
            "real_vendor_qualified": False, "business_quality_passed": False}


class LiveWireObserver:
    """原SDK输入侧car仅存白名单hash/bool，不替代SdkLedger或业务账本。"""

    def __init__(self, runtime: Path, labels: dict):
        """持有自有输出及人工标签，不连接模型或业务数据库。"""
        self.runtime, self.labels = runtime, labels
        self._lock = RLock()
        self._count = 0
        self._errors = 0
        self.path = runtime / f"live-wire-{os.getpid()}.private.jsonl"

    def _record(self, row: dict) -> None:
        """短写安全追加到UID自有私有目录，旁证故障使其无效而不改SDK语义。"""
        with self._lock:
            try:
                directory = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(directory)
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise ValueError("F3_LIVE_PRIVATE_WIRE_REQUIRED")
                    descriptor = os.open(self.path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                                         | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
                finally:
                    os.close(directory)
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                        raise ValueError("F3_LIVE_PRIVATE_WIRE_REQUIRED")
                    data = memoryview((json.dumps(row, ensure_ascii=False) + "\n").encode())
                    while data:
                        written = os.write(descriptor, data)
                        if written <= 0:
                            raise ValueError("F3_LIVE_WIRE_WRITE_FAILED")
                        data = data[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._count += 1
            except Exception:
                self._errors += 1

    def observe(self, client):
        """原create/with_options与流完整透传，只记录实际请求旁证。"""
        original = client.chat.completions.create
        if getattr(original, "_f3_live_wire_observed", False):
            return client
        original_options = getattr(client, "with_options", None)
        models = getattr(client, "models", None)
        original_list = getattr(models, "list", None)

        def create(*args, **kwargs):
            """最终SDK原输入写旁证后仅调用一次，保持原结果、异常与流对象。"""
            row = wire_fact_summary(kwargs, self.labels)
            row.update(sdk_identity())
            row.update(wire_id="f3_wire_" + uuid4().hex,
                       sdk_request_sha256=digest({"args": args, "request": kwargs}))
            self._record(row)
            return original(*args, **kwargs)

        create._f3_live_wire_observed = True
        client.chat.completions.create = create
        if callable(original_list) and not getattr(original_list, "_f3_live_wire_observed", False):
            def list_models(*args, **params):
                """目录原请求按最终SDK相同摘要留痕，不记录模型ID或改动返回。"""
                row = {"request_class": "model_catalog",
                       "sdk_request_sha256": digest({"args": args, "request": params})}
                row.update(sdk_identity())
                try:
                    response = original_list(*args, **params)
                except Exception:
                    row["status"] = "failed"
                    self._record(row)
                    raise
                data = getattr(response, "data", None)
                if isinstance(data, (list, tuple)):
                    row["catalog_count"] = len(data)
                row["status"] = "completed"
                self._record(row)
                return response

            list_models._f3_live_wire_observed = True
            models.list = list_models
        if callable(original_options):
            def with_options(*args, **kwargs):
                """原超时配置副本继续观察，不改变选项或新增网络调用。"""
                return self.observe(original_options(*args, **kwargs))
            client.with_options = with_options
        return client

    def health(self) -> dict:
        """仅报告侧car写入状态，业务与真实供应商资格始终未授予。"""
        with self._lock:
            return {"receipt_count": self._count, "error_count": self._errors,
                    "valid": self._errors == 0, "real_vendor_qualified": False,
                    "business_quality_passed": False}


@contextmanager
def _bound_frame(frame: dict):
    """每次原调用或生成器推进单独设置并恢复观察上下文，允许跨线程续接。"""
    token = _frame.set(frame)
    try:
        yield
    finally:
        _frame.reset(token)


def _iterate(iterator, frame: dict):
    """逐次推进原生成器，关闭与异常仍由原生成器负责。"""
    try:
        while True:
            try:
                with _bound_frame(frame):
                    item = next(iterator)
            except StopIteration:
                return
            yield item
    finally:
        with _bound_frame(frame):
            iterator.close()


@contextmanager
def live_caller_bindings(runtime: Path, spec):
    """显式自有进程中包装原固定调用点，退出必须恢复原方法。"""
    from app.api import model_configs
    from app.api import chat as chat_api
    from app.core.agent_loop import AgentLoop
    from app.db.models import Message, SopInstance
    from app.dynamic_tasks.agent import DynamicTaskAgent
    from app.llm.client import LLMClient
    from app.memory import worker as memory_worker
    from app.sop_runtime.execution_store import SopExecutionStore
    from app.security.permissions import require_tenant_admin
    from fastapi.routing import APIRoute
    from single_port_app import app as service_app

    if os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1" or os.environ.get("F3_LIVE_RUNTIME") != str(runtime):
        raise ValueError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")
    originals = []
    middleware_baseline = list(service_app.user_middleware)
    diagnostic_middleware_added = False

    def install(cls, name, wrapper):
        """仅包装已确定的原方法，并登记退出恢复顺序。"""
        original = getattr(cls, name)
        originals.append((cls, name, original))
        setattr(cls, name, wrapper(original))

    def source_hash(function) -> str:
        """以被包装原调用函数所在源码文件标识测试侧旁证来源。"""
        return hashlib.sha256(Path(function.__code__.co_filename).read_bytes()).hexdigest()

    def turn_wrapper(original):
        """原Composer参数建立观察帧，来源由原持久用户消息再绑定。"""
        @wraps(original)
        def call(self, request, *args, **kwargs):
            """原Turn入口不替换身份、执行器或请求。"""
            frame = {"db": self.db, "kind": "dynamic_task" if request.execution_engine == "dynamic_task" else "sop",
                     "tenant_id": request.tenant_id, "user_id": request.user_id,
                     "agent_id": request.agent_id, "session_id": request.session_id,
                     "source_input_id": None, "client_turn_id": request.client_turn_id}
            if original.__name__.endswith("stream"):
                return _iterate(original(self, request, *args, **kwargs), frame)
            with _bound_frame(frame):
                return original(self, request, *args, **kwargs)
        return call

    def payload_wrapper(original):
        """只在原调用明确传入持久消息ID时关联本轮来源。"""
        @wraps(original)
        def call(self, payload, user_message_id):
            """保留原payload字节和服务器turn规则，更新旁证帧。"""
            output = original(self, payload, user_message_id)
            frame = _frame.get()
            if frame and user_message_id:
                source = self.db.get(Message, user_message_id)
                if source:
                    frame.update(source_input_id=source.id, session_id=source.session_id)
            return output
        return call

    def dynamic_wrapper(original):
        """DTA固定入口读取明确实例，审批actor不能替换原initiator。"""
        @wraps(original)
        def call(self, *args, **kwargs):
            """保留原DTA参数并用精确持久身份建立同步观察帧。"""
            previous = _frame.get() or {}
            if original.__name__ == "start_task":
                frame = {"db": self.db, "kind": "dynamic_task", "tenant_id": kwargs["tenant_id"],
                         "user_id": kwargs["initiator_user_id"], "agent_id": kwargs["agent_id"],
                         "session_id": kwargs["session_id"], "source_input_id": kwargs.get("source_ref"),
                         "client_turn_id": previous.get("client_turn_id")}
            else:
                instance = self.db.get(SopInstance, kwargs["execution_id"])
                if instance is None:
                    return original(self, *args, **kwargs)
                source = kwargs.get("user_message_id") or (instance.context_json or {}).get("source_input_id") or instance.source_ref
                frame = {"db": self.db, "kind": "dynamic_task", "tenant_id": instance.tenant_id,
                         "user_id": instance.initiator_user_id, "agent_id": instance.agent_id,
                         "session_id": instance.session_id, "source_input_id": source,
                         "client_turn_id": previous.get("client_turn_id"), "instance_id": instance.id,
                         "signal_id": kwargs.get("resume_signal_id")}
            with _bound_frame(frame):
                return original(self, *args, **kwargs)
        return call

    def llm_wrapper(original):
        """调用原LLM前读取原固定入口帧，未绑定时明确保持缺失。"""
        @wraps(original)
        def call(self, *args, **kwargs):
            """实际模型参数、重试、取消及返回全部由原LLM执行。"""
            frame = _frame.get()
            if frame is None:
                return original(self, *args, **kwargs)
            identity = exact_caller_identity(**frame)
            identity["source_sha256"] = hashlib.sha256(Path(original.__code__.co_filename).read_bytes()).hexdigest()
            if original.__name__.endswith("stream"):
                def stream():
                    """原流在本迭代内使用精确SDK身份，退出立即恢复上下文。"""
                    with bind_sdk_identity(**identity):
                        yield from original(self, *args, **kwargs)
                return stream()
            with bind_sdk_identity(**identity):
                return original(self, *args, **kwargs)
        return call

    def sop_wrapper(original):
        """读取原消费者明确选中或新建的SOP实例，不自行做最近行查询。"""
        @wraps(original)
        def call(self, *args, **kwargs):
            """原选择结果经实际tenant/session/agent核对后仅绑定旁证身份。"""
            result = original(self, *args, **kwargs)
            frame = _frame.get()
            instance = result[0] if isinstance(result, tuple) else result
            if frame and instance is not None and instance.kind == "sop":
                if (instance.tenant_id == frame["tenant_id"] and instance.session_id == frame["session_id"]
                        and instance.agent_id == frame["agent_id"] and instance.initiator_user_id == frame["user_id"]):
                    frame["instance_id"] = instance.id
            return result
        return call

    def memory_capture_wrapper(original):
        """持久记忆任务按原job与source message建新边界，不继承聊天调用身份。"""
        @wraps(original)
        def call(db, *, job_id, worker_id):
            """只在可精确读回job、来源和会话时旁证记忆模型调用。"""
            try:
                identity = exact_memory_capture_identity(
                    db, job_id=job_id, source_sha256=source_hash(original))
            except Exception:
                identity = None
            with _bound_frame(None), bind_observation_handoff(None), bind_sdk_identity(
                    replace_existing=True, **(identity or {})):
                return original(db, job_id=job_id, worker_id=worker_id)
        return call

    def session_title_wrapper(original):
        """标题后台worker只绑定其原租户会话和payload消息窗口内的首轮。"""
        @wraps(original)
        def call(tenant_id, user_id, session_id, agent_id):
            """短读当前会话及原标题payload窗口，读不到时保留未绑定状态。"""
            try:
                with chat_api.Session(chat_api.engine) as db:
                    identity = exact_session_title_identity(
                        db, tenant_id=tenant_id, user_id=user_id, session_id=session_id,
                        agent_id=agent_id, source_sha256=source_hash(original))
            except Exception:
                identity = None
            with _bound_frame(None), bind_observation_handoff(None), bind_sdk_identity(
                    replace_existing=True, **(identity or {})):
                return original(tenant_id, user_id, session_id, agent_id)
        return call

    def model_probe_wrapper(probe_class):
        """只在原配置诊断探针内部标注有限类别，不包裹普通Composer调用。"""
        def wrap(original):
            """构造保留原描述符及参数的探针包装器。"""
            @wraps(original)
            def call(self, *args, **kwargs):
                """标注模型配置路由的真实SDK调用并原样返回原探针结果。"""
                with model_preflight_probe(probe_class):
                    return original(self, *args, **kwargs)
            return call
        return wrap

    def model_admin_wrapper(original):
        """原租户管理员校验成功后关联实际actor，不更改授权结果。"""
        @wraps(original)
        def call(tenant_id, current_user):
            """保留原依赖的租户/角色拒绝，并只旁证成功返回的用户。"""
            result = original(tenant_id, current_user)
            bind_model_preflight_admin(tenant_id, result)
            return result
        return call

    def install_model_admin_binding(route):
        """仅在两个明确模型诊断路由的原管理员依赖出口添加身份观察。"""
        visited = set()
        found = False

        def visit(dependant):
            """沿原依赖树定位管理员校验节点，保留其子依赖和FastAPI参数图。"""
            nonlocal found
            if id(dependant) in visited:
                return
            visited.add(id(dependant))
            if dependant.call is require_tenant_admin:
                original_dependency = dependant.call
                wrapped_dependency = model_admin_wrapper(original_dependency)
                originals.append((dependant, "call", original_dependency))
                dependant.call = wrapped_dependency
                dependencies = list(getattr(route, "dependencies", ()))
                rewritten_dependencies = []
                dependencies_changed = False
                for dependency in dependencies:
                    if dependency.dependency is original_dependency:
                        from fastapi import Depends

                        rewritten_dependencies.append(Depends(
                            wrapped_dependency,
                            use_cache=dependency.use_cache,
                            scope=dependency.scope,
                        ))
                        dependencies_changed = True
                    else:
                        rewritten_dependencies.append(dependency)
                if dependencies_changed:
                    originals.append((route, "dependencies", route.dependencies))
                    route.dependencies = rewritten_dependencies
                found = True
            for child in dependant.dependencies:
                visit(child)

        visit(route.dependant)
        if not found:
            raise ValueError("F3_MODEL_DIAGNOSTIC_ADMIN_DEPENDENCY_REQUIRED")

    try:
        diagnostic_paths = {
            "/api/enterprise/model-configs/{config_id}/test",
            "/api/enterprise/model-configs/{config_id}/preflight-dynamic",
        }
        found_diagnostic_paths = set()
        route_candidates = []
        for mounted in service_app.routes:
            if isinstance(mounted, APIRoute):
                route_candidates.append(mounted)
            else:
                route_candidates.extend(getattr(
                    getattr(mounted, "original_router", None), "routes", (),
                ))
        for route in route_candidates:
            if isinstance(route, APIRoute) and route.path in diagnostic_paths and "POST" in route.methods:
                found_diagnostic_paths.add(route.path)
                install_model_admin_binding(route)
        if found_diagnostic_paths != diagnostic_paths:
            raise ValueError("F3_MODEL_DIAGNOSTIC_ROUTES_REQUIRED")

        for mounted in service_app.routes:
            if hasattr(mounted, "effective_candidates"):
                mounted._effective_candidates = []
                mounted._effective_candidates_version = None
                mounted._effective_low_priority_routes = []
                mounted._effective_low_priority_routes_version = None

        original_model_client_factory = model_configs.LLMClient

        def model_client_factory(model_config, *args, **kwargs):
            """从路由实际加载的ModelConfig建立hash指纹后调用原LLMClient构造器。"""
            bind_model_preflight_config(model_config)
            return original_model_client_factory(model_config, *args, **kwargs)

        originals.append((model_configs, "LLMClient", original_model_client_factory))
        model_configs.LLMClient = model_client_factory
        install(LLMClient, "probe_model_catalog", model_probe_wrapper("model_connection"))
        install(LLMClient, "probe_text_connection", model_probe_wrapper("model_connection"))
        install(LLMClient, "preflight_dynamic_capabilities",
                model_probe_wrapper("native_capabilities"))
        service_app.add_middleware(ModelPreflightDiagnosticMiddleware)
        diagnostic_middleware_added = True

        for name in ("handle_turn", "handle_turn_stream"):
            install(AgentLoop, name, turn_wrapper)
        install(AgentLoop, "_turn_payload", payload_wrapper)
        for name in ("start_task", "run_until_blocked_or_complete"):
            install(DynamicTaskAgent, name, dynamic_wrapper)
        for name in ("generate_json", "generate_text", "generate_text_stream"):
            install(LLMClient, name, llm_wrapper)
        for name in ("active_instance", "start_instance"):
            install(SopExecutionStore, name, sop_wrapper)
        install(memory_worker, "process_memory_capture_job", memory_capture_wrapper)
        install(chat_api, "_summarize_session_title_once", session_title_wrapper)
        yield
    finally:
        for cls, name, original in reversed(originals):
            setattr(cls, name, original)
        for mounted in service_app.routes:
            if hasattr(mounted, "effective_candidates"):
                mounted._effective_candidates = []
                mounted._effective_candidates_version = None
                mounted._effective_low_priority_routes = []
                mounted._effective_low_priority_routes_version = None
        if diagnostic_middleware_added:
            service_app.user_middleware[:] = middleware_baseline
            service_app.middleware_stack = None
