"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : test_final_f3_live_identity.py
@CallChain  : 正式pytest → 精确原调用身份/实际SDK输入旁证 → 透传与资格边界
@Description: 合成控制验证不猜来源、不丢合法重试、不以SDK输入旁证冒称业务通过。
"""

import asyncio
from types import SimpleNamespace

import pytest

from final_f3_live_identity import LiveWireObserver, exact_caller_identity, wire_fact_summary
from final_f3_preparation import digest


def test_model_preflight_middleware_returns_same_hash_scope_seen_by_sdk_context() -> None:
    """模型诊断响应摘要与ASGI下游真实SDK读取的invocation/租户/配置一致。"""
    from final_f3_live_identity import ModelPreflightDiagnosticMiddleware
    from final_f3_sdk_identity import sdk_identity

    observed = []
    sent = []

    async def downstream(scope, _receive, send):
        """模拟只读ASGI出口并读取与原SDK观察器相同的ContextVar。"""
        observed.append(sdk_identity()["diagnostic_scope"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        """提供空合成HTTP请求体，不触发数据库或模型。"""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        """收集本例响应头供关联断言。"""
        sent.append(message)

    scope = {
        "type": "http", "method": "POST",
        "path": "/api/enterprise/model-configs/SYNTHETIC_CONFIG/test",
        "query_string": b"tenant_id=SYNTHETIC_TENANT",
    }
    asyncio.run(ModelPreflightDiagnosticMiddleware(downstream)(scope, receive, send))

    assert len(observed) == 1 and observed[0] is not None
    diagnostic = observed[0]
    assert diagnostic["tenant_sha256"] == digest("SYNTHETIC_TENANT")
    assert diagnostic["model_config_sha256"] == digest("SYNTHETIC_CONFIG")
    assert diagnostic["admin_actor_sha256"] is None
    response_headers = dict(sent[0]["headers"])
    assert response_headers[b"x-f3-diagnostic-invocation-sha256"].decode() == diagnostic[
        "invocation_sha256"
    ]
    assert sdk_identity()["diagnostic_scope"] is None


def test_model_preflight_middleware_keeps_concurrent_invocations_separate() -> None:
    """并发模型诊断各自持有tenant/config/invocation上下文，不串给另一管理员请求。"""
    from final_f3_live_identity import ModelPreflightDiagnosticMiddleware
    from final_f3_sdk_identity import sdk_identity

    observed = []
    invocations = []

    async def downstream(_scope, _receive, send):
        """交错读取当前请求上下文，模拟同一ASGI进程中的并发预检。"""
        await asyncio.sleep(0)
        diagnostic = sdk_identity()["diagnostic_scope"]
        observed.append((diagnostic["tenant_sha256"], diagnostic["model_config_sha256"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        """提供空请求体以保持本测不进入API副作用。"""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def make_sender():
        """为每个请求创建独立响应收集器。"""
        messages = []

        async def send(message):
            """保存一个请求自己的ASGI响应帧。"""
            messages.append(message)

        return messages, send

    async def exercise():
        """并行执行两个不同配置路由并分别取出其响应invocation。"""
        middleware = ModelPreflightDiagnosticMiddleware(downstream)
        requests = []
        for tenant, config in (("TENANT_A", "CONFIG_A"), ("TENANT_B", "CONFIG_B")):
            messages, sender = await make_sender()
            scope = {"type": "http", "method": "POST",
                     "path": f"/api/enterprise/model-configs/{config}/preflight-dynamic",
                     "query_string": f"tenant_id={tenant}".encode()}
            requests.append((messages, middleware(scope, receive, sender)))
        await asyncio.gather(*(task for _messages, task in requests))
        for messages, _task in requests:
            header = dict(messages[0]["headers"])[b"x-f3-diagnostic-invocation-sha256"]
            invocations.append(header.decode())

    asyncio.run(exercise())

    assert set(observed) == {
        (digest("TENANT_A"), digest("CONFIG_A")),
        (digest("TENANT_B"), digest("CONFIG_B")),
    }
    assert len(set(invocations)) == 2
    assert sdk_identity()["diagnostic_scope"] is None


def test_model_preflight_scope_survives_concurrent_fastapi_sync_dependencies(monkeypatch) -> None:
    """真实FastAPI同步依赖/endpoint线程池并发时，管理员和配置摘要仍精确返回原请求。"""
    import httpx
    from fastapi import Depends, FastAPI, Query

    from final_f3_live_identity import ModelPreflightDiagnosticMiddleware
    from final_f3_preparation import model_config_payload_fingerprint
    from final_f3_sdk_identity import (
        bind_model_preflight_admin,
        bind_model_preflight_config,
        model_preflight_probe,
        sdk_identity,
    )

    monkeypatch.setattr("app.security.encryption.decrypt_secret",
                        lambda value: "SYNTHETIC_DECRYPTED_KEY"
                        if value == "SYNTHETIC_ENCRYPTED_KEY" else "")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(model_api_timeout_seconds=180))
    expected = model_config_payload_fingerprint(
        provider="openai_compatible", base_url="https://synthetic.invalid/v1",
        api_key="SYNTHETIC_DECRYPTED_KEY", model="synthetic-model", temperature=0.0,
        max_output_tokens=8192, extra_body={"thinking": {"type": "disabled"}},
        api_timeout_seconds=180,
    )
    app = FastAPI()
    app.add_middleware(ModelPreflightDiagnosticMiddleware)

    def authenticated_admin(tenant_id: str = Query(...)):
        """模拟原同步租户管理员依赖成功返回后执行的精确绑定。"""
        user = SimpleNamespace(id=f"ADMIN_{tenant_id}", tenant_id=tenant_id, role="admin")
        bind_model_preflight_admin(tenant_id, user)
        return user

    @app.post("/api/enterprise/model-configs/{config_id}/test")
    def model_test(config_id: str, tenant_id: str = Query(...),
                   _admin=Depends(authenticated_admin)):
        """模拟原同步路由用其本轮ModelConfig进入SDK探针包装器。"""
        config = SimpleNamespace(
            id=config_id, tenant_id=tenant_id, provider="openai_compatible",
            base_url="https://synthetic.invalid/v1", api_key_encrypted="SYNTHETIC_ENCRYPTED_KEY",
            model="synthetic-model", temperature=0.0, max_output_tokens=8192,
            extra_body_json={"thinking": {"type": "disabled"}},
        )
        bind_model_preflight_config(config, expected_payload_sha256=expected)
        with model_preflight_probe("model_connection"):
            diagnostic = sdk_identity()["diagnostic_scope"]
        return {"tenant_sha256": diagnostic["tenant_sha256"],
                "model_config_sha256": diagnostic["model_config_sha256"],
                "admin_actor_sha256": diagnostic["admin_actor_sha256"],
                "config_fingerprint_sha256": diagnostic["config_fingerprint_sha256"]}

    async def exercise_requests():
        """以ASGI并发客户端迫使两个同步依赖分别穿过FastAPI线程池。"""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(*(
                client.post(f"/api/enterprise/model-configs/CONFIG_{suffix}/test",
                            params={"tenant_id": f"TENANT_{suffix}"})
                for suffix in ("A", "B")
            ))

    responses = asyncio.run(exercise_requests())
    assert all(response.status_code == 200 for response in responses)
    bodies = [response.json() for response in responses]
    assert {body["tenant_sha256"] for body in bodies} == {
        digest("TENANT_A"), digest("TENANT_B"),
    }
    assert {body["model_config_sha256"] for body in bodies} == {
        digest("CONFIG_A"), digest("CONFIG_B"),
    }
    assert {body["admin_actor_sha256"] for body in bodies} == {
        digest("ADMIN_TENANT_A"), digest("ADMIN_TENANT_B"),
    }
    assert all(body["config_fingerprint_sha256"] == expected for body in bodies)
    invocation_headers = set()
    for response in responses:
        headers = response.headers
        invocation = headers["x-f3-diagnostic-invocation-sha256"]
        fingerprint = headers["x-f3-diagnostic-config-fingerprint-sha256"]
        assert len(invocation) == 64 and len(fingerprint) == 64
        assert fingerprint == expected
        invocation_headers.add(invocation)
    assert len(invocation_headers) == 2


def test_live_bindings_wrap_only_original_model_admin_diagnostic_dependencies(tmp_path, monkeypatch):
    """自有服务器仅包装原两个模型诊断route，授权仍由原租户管理员依赖决定。"""
    from app.db.models import User
    from app.security.permissions import require_tenant_admin
    from final_f3_live_identity import ModelPreflightDiagnosticMiddleware, live_caller_bindings
    from final_f3_sdk_identity import model_preflight_request_scope, sdk_identity
    from fastapi.routing import APIRoute
    from single_port_app import app

    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    diagnostic_paths = {
        "/api/enterprise/model-configs/{config_id}/test",
        "/api/enterprise/model-configs/{config_id}/preflight-dynamic",
    }
    routes = []
    for mounted in app.routes:
        router = getattr(mounted, "original_router", None)
        for route in getattr(router, "routes", ()):
            if isinstance(route, APIRoute) and route.path in diagnostic_paths:
                routes.append(route)
    assert {route.path for route in routes} == diagnostic_paths
    dependency = next(
        child for route in routes for child in route.dependant.dependencies
        if child.call is require_tenant_admin
    )
    original_middleware = list(app.user_middleware)
    admin = User(
        id="SYNTHETIC_ADMIN", tenant_id="SYNTHETIC_TENANT", username="synthetic-admin",
        role="admin", password_hash="synthetic-unused-hash",
    )

    with live_caller_bindings(tmp_path, SimpleNamespace(value={})):
        assert dependency.call is not require_tenant_admin
        assert any(item.cls is ModelPreflightDiagnosticMiddleware for item in app.user_middleware)
        effective_dependencies = []
        for mounted in app.routes:
            for context in getattr(mounted, "effective_route_contexts", lambda: ())():
                route = getattr(context, "original_route", None)
                if getattr(route, "path", None) in diagnostic_paths:
                    effective_dependencies.extend(
                        child.call for child in context.dependant.dependencies
                        if getattr(child.call, "__wrapped__", None) is require_tenant_admin
                    )
        assert len(effective_dependencies) == len(diagnostic_paths)
        with model_preflight_request_scope(
            invocation_sha256="c" * 64,
            tenant_id="SYNTHETIC_TENANT",
            model_config_id="SYNTHETIC_MODEL_CONFIG",
        ):
            assert dependency.call(tenant_id="SYNTHETIC_TENANT", current_user=admin) is admin
            assert sdk_identity()["diagnostic_scope"]["admin_actor_sha256"] == digest(
                "SYNTHETIC_ADMIN"
            )

    assert dependency.call is require_tenant_admin
    assert app.user_middleware == original_middleware


class ExactRows:
    """只允许按明确id取合成原行，任何最近查询皆为测试失败。"""

    def __init__(self, rows):
        """保留显式行映射，不生成缺失来源。"""
        self.rows = rows

    def get(self, _model, identity):
        """按原调用指明身份返回，缺失即None。"""
        return self.rows.get(identity)


def current_rows():
    """构造单个当前用户来源和明确实例，历史行不能成为替补。"""
    return ExactRows({
        "source": SimpleNamespace(id="source", tenant_id="tenant", session_id="session",
                                  role="user", metadata_json={"client_turn_id": "turn"}),
        "instance": SimpleNamespace(id="instance", tenant_id="tenant", session_id="session",
                                    agent_id="agent", initiator_user_id="owner", kind="dynamic_task",
                                    context_json={"source_input_id": "source"},
                                    current_plan_revision_id="plan", skill_version_id=None),
    })


def test_precise_caller_source_and_runtime_kind_are_preserved():
    """来源和分支只取明确原参数及原实例，不随审批actor替换initiator。"""
    actual = exact_caller_identity(current_rows(), kind="dynamic_task", tenant_id="tenant",
                                   user_id="owner", agent_id="agent", session_id="session",
                                   source_input_id="source", client_turn_id="turn", instance_id="instance")
    assert actual["source_input_id"] == "source" and actual["instance_id"] == "instance"
    assert actual["user_id"] == "owner" and actual["plan_id"] == "plan"
    assert actual["kind"] == "dynamic_task"


@pytest.mark.parametrize("field,value", [("tenant_id", "foreign"), ("agent_id", "other"),
                                        ("user_id", "reviewer"), ("kind", "sop")])
def test_original_instance_identity_drift_is_refused(field, value):
    """实际原实例的tenant/agent/initiator/kind漂移不能借caller参数补为合法。"""
    arguments = {"kind": "dynamic_task", "tenant_id": "tenant", "user_id": "owner",
                 "agent_id": "agent", "session_id": "session", "source_input_id": "source",
                 "client_turn_id": "turn", "instance_id": "instance"}
    arguments[field] = value
    with pytest.raises(ValueError, match="F3_LIVE_CALLER_IDENTITY_MISMATCH"):
        exact_caller_identity(current_rows(), **arguments)


def test_missing_source_remains_unbound_without_latest_message_fallback():
    """普通预检无业务来源时不虚构source，已有实例要求精确来源不缺失。"""
    actual = exact_caller_identity(current_rows(), kind="sop", tenant_id="tenant", user_id="owner",
                                   agent_id="agent", session_id=None, source_input_id=None,
                                   client_turn_id=None)
    assert "source_input_id" not in actual and "session_id" not in actual


@pytest.mark.parametrize("change", ["role", "tenant", "session", "client_turn"])
def test_source_message_exact_identity_drift_is_refused(change):
    """原来源不是当前user/tenant/session/client turn时拒绝，不借历史等形行。"""
    rows = current_rows()
    source = rows.rows["source"]
    if change == "role":
        source.role = "assistant"
    elif change == "tenant":
        source.tenant_id = "foreign"
    elif change == "session":
        source.session_id = "other"
    else:
        source.metadata_json["client_turn_id"] = "other"
    with pytest.raises(ValueError, match="F3_LIVE_CALLER_IDENTITY_MISMATCH"):
        exact_caller_identity(rows, kind="sop", tenant_id="tenant", user_id="owner", agent_id="agent",
                              session_id="session", source_input_id="source", client_turn_id="turn")


def test_wire_facts_read_only_actual_full_input_not_expected_output():
    """实际SDK kwargs缺某个人工事实时保持false，labels不能补成输入。"""
    kwargs = {"model": "synthetic", "messages": [{"role": "user", "content": "完整旧知识\n完整记忆"}]}
    labels = {"knowledge_old": "完整旧知识", "knowledge_new": "完整新知识",
              "memories": ["完整记忆", "另一个完整记忆"]}
    result = wire_fact_summary(kwargs, labels)
    assert result["request_sha256"] == digest(kwargs)
    assert result["knowledge_old_complete"] is True and result["knowledge_new_complete"] is False
    assert result["memory_complete"] == [True, False]
    assert result["real_vendor_qualified"] is False and result["business_quality_passed"] is False
    assert "完整旧知识" not in str(result) and "完整记忆" not in str(result)


def test_wire_facts_decode_original_json_payload_strings_without_truncating():
    """原SDK user内容JSON中的换行和完整事实仍按实际串读取，不借外部fixture回答。"""
    import json

    fact = "第一行\n第二行完整记忆"
    kwargs = {"messages": [{"role": "user", "content": json.dumps({"memory": fact}, ensure_ascii=False)}]}
    result = wire_fact_summary(kwargs, {"knowledge_old": "old", "knowledge_new": "new", "memories": [fact]})
    assert result["memory_complete"] == [True]


def test_wire_facts_decode_only_original_current_input_prefix_with_complete_facts():
    """原LLM固定前缀后的JSON仍证明完整多行事实与冻结知识版本消费。"""
    import json

    fact = "输出偏好：简洁要点；原样保留代码 a  b\n\t校验结束。"
    knowledge = "完整政策第一行\n第二行末尾不可丢失。"
    payload = {"memories": [{"content": fact}], "knowledge": {
        "content": knowledge, "knowledge_base_version_ids": ["original-frozen-version"]}}
    content = "本轮输入（仅用于当前调用，不写入对话历史）：\n" + json.dumps(
        payload, ensure_ascii=False,
    )
    kwargs = {"messages": [{"role": "user", "content": content}]}
    result = wire_fact_summary(kwargs, {"knowledge_old": knowledge, "memories": [fact]})
    assert result["memory_complete"] == [True]
    assert result["knowledge_old_complete"] is True
    assert result["has_knowledge_result"] is True
    assert result["knowledge_version_ids"] == ["original-frozen-version"]


@pytest.mark.parametrize("prefix", ["未知前缀：\n", "本轮输入（仅用于当前调用，不写入对话历史）：\n前言"])
def test_wire_facts_do_not_guess_json_from_unknown_or_malformed_prefixed_text(prefix):
    """未知文本或固定前缀后的非JSON不扫描花括号补事实及版本身份。"""
    import json

    fact = "完整事实第一行\n第二行"
    content = prefix + json.dumps({"content": fact, "knowledge_version_ids": ["unproven"]})
    result = wire_fact_summary({"messages": [{"content": content}]}, {"memories": [fact]})
    assert result["memory_complete"] == [False]
    assert result["has_knowledge_result"] is False
    assert result["knowledge_version_ids"] == []


def test_wire_observer_preserves_original_result_and_private_count(tmp_path):
    """观察结果和with_options副本不得改变原SDK返回，两个合法调用均保留。"""
    original_result = object()
    calls = []

    class Client:
        """最小原SDK双接口，合成正式控制不连接模型。"""

        def __init__(self):
            """每副本拥有原create，返回同一原对象。"""
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            """记录实际输入并原样返回，不产生业务状态。"""
            calls.append(kwargs)
            return original_result

        def with_options(self, **_kwargs):
            """原副本独立创建，调用参数不能被旁证改写。"""
            return Client()

    tmp_path.chmod(0o700)
    observer = LiveWireObserver(tmp_path, {"knowledge_old": "old", "knowledge_new": "new", "memories": []})
    client = observer.observe(Client())
    assert client.chat.completions.create(messages=[]) is original_result
    assert client.with_options(timeout=1).chat.completions.create(messages=[]) is original_result
    assert len(calls) == 2 and observer.health()["receipt_count"] == 2
    assert observer.health()["business_quality_passed"] is False


def test_wire_observer_records_model_catalog_request_for_sdk_receipt_closure(tmp_path):
    """模型目录请求也须有同摘要输入旁证，供最终SDK逐请求闭合。"""
    import json

    catalog = SimpleNamespace(data=[SimpleNamespace(id="PRIVATE_CATALOG_MODEL_CANARY")])
    calls = []

    def list_models(*args, **params):
        """保留原目录实参并返回同一原目录对象，不访问网络。"""
        calls.append((args, params))
        return catalog

    tmp_path.chmod(0o700)
    observer = LiveWireObserver(tmp_path, {"knowledge_old": "old", "knowledge_new": "new",
                                           "memories": []})
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: None)),
        models=SimpleNamespace(list=list_models),
    )
    observed = observer.observe(client)

    assert observed.models.list(limit=7) is catalog
    rows = [json.loads(line) for line in observer.path.read_text().splitlines()]
    assert calls == [((), {"limit": 7})]
    assert rows[0]["request_class"] == "model_catalog"
    assert rows[0]["sdk_request_sha256"] == digest({"args": (), "request": {"limit": 7}})
    assert rows[0]["catalog_count"] == 1
    assert "PRIVATE_CATALOG_MODEL_CANARY" not in observer.path.read_text()
    assert observer.health()["receipt_count"] == 1


def test_wire_observer_preserves_original_exception_identity(tmp_path):
    """旁证不能替换原SDK失败为成功或另一异常，失败请求仍可核输入。"""
    error = RuntimeError("synthetic original error")

    def create(**_kwargs):
        """只抛原异常对象，不接触网络。"""
        raise error

    tmp_path.chmod(0o700)
    observer = LiveWireObserver(tmp_path, {"knowledge_old": "old", "knowledge_new": "new", "memories": []})
    client = observer.observe(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(RuntimeError) as caught:
        client.chat.completions.create(messages=[])
    assert caught.value is error
    assert observer.health()["receipt_count"] == 1


def test_original_generate_json_cancellable_thread_keeps_exact_sdk_source_and_result(tmp_path, monkeypatch):
    """原JSON取消线程仅传观察身份；原模型ContextVar和解析结果不得被适配改变。"""
    from threading import get_ident

    from app.llm import client as llm_module
    from app.llm.client import LLMClient
    from app.observability.spans import current_llm_operation, llm_operation
    from final_f3_live_identity import _bound_frame, live_caller_bindings
    from final_f3_sdk_handoff import sdk_thread_handoff
    from final_f3_sdk_identity import sdk_identity

    completion = SimpleNamespace(id="original-synthetic-response", choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"original": true}'), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5))
    calls = []
    caller_thread = get_ident()

    def create(**kwargs):
        """原SDK终点在真实worker记录安全上下文，并透传同一合成completion对象。"""
        calls.append({"thread": get_ident(), "operation": current_llm_operation(),
                      "identity": sdk_identity(), "request": kwargs})
        return completion

    client = object.__new__(LLMClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.model = "non-network-synthetic-model"
    client.temperature = 0
    client.max_output_tokens = 64
    client.timeout_seconds = 3
    client.base_url = "https://model.example.invalid/v1"
    client.extra_body = {}
    client.thinking_mode = ""
    client.safe_error_code = None
    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))
    frame = {"db": current_rows(), "kind": "dynamic_task", "tenant_id": "tenant",
             "user_id": "owner", "agent_id": "agent", "session_id": "session",
             "source_input_id": "source", "client_turn_id": "turn", "instance_id": "instance"}
    with (
        sdk_thread_handoff(llm_module),
        live_caller_bindings(tmp_path, SimpleNamespace(value={})),
        _bound_frame(frame),
        llm_operation("dynamic_task.plan"),
    ):
        result = client.generate_json("合成原JSON请求", {"original_input": True}, is_cancelled=lambda: False)
    assert result == {"original": True}
    assert len(calls) == 1 and calls[0]["thread"] != caller_thread
    assert calls[0]["operation"] == "dynamic_task.plan"
    assert calls[0]["identity"]["identity_sha256"]["source_input_id"] == digest("source")
    assert calls[0]["identity"]["identity_sha256"]["instance_id"] == digest("instance")
    assert calls[0]["identity"]["identity_sha256"]["user_id"] == digest("owner")
    assert calls[0]["identity"]["phase_sha256"] == digest("dynamic_task.plan")
    assert calls[0]["identity"]["sdk_thread_phase_sha256"] == digest("dynamic_task.plan")
    assert calls[0]["identity"]["phase_source"] == "original_caller_observation_handoff"


def test_live_bindings_attach_persisted_memory_and_title_worker_sources(tmp_path, monkeypatch):
    """后台记忆与标题模型调用只绑定各自持久job、会话和原始来源轮次。"""
    from app.api import chat as chat_api
    from app.memory import worker as memory_worker
    from final_f3_live_identity import live_caller_bindings
    from final_f3_sdk_identity import bind_observation_handoff, bind_sdk_identity, sdk_identity

    source = SimpleNamespace(id="source-turn", tenant_id="tenant", session_id="session",
                             role="user", content="synthetic user input")
    assistant = SimpleNamespace(id="assistant-turn", tenant_id="tenant", session_id="session",
                                role="assistant", content="synthetic reply",
                                metadata_json={"turn_id": "source-turn"})
    session = SimpleNamespace(id="session", tenant_id="tenant", user_id="owner", agent_id="agent")
    job = SimpleNamespace(id="capture-job", tenant_id="tenant", user_id="owner", agent_id="agent",
                         source_message_id="source-turn", source_assistant_message_id="assistant-turn")

    class MemoryDB:
        """按模型主键提供本测试唯一的合成持久行。"""

        def get(self, _model, identity):
            """返回精确主键对应行，缺失时不执行近似回查。"""
            return {"capture-job": job, "source-turn": source, "assistant-turn": assistant,
                    "session": session}.get(identity)

    class QueryResult:
        """模拟 SQLModel 查询结果的首行和有序消息读取接口。"""

        def __init__(self, value):
            """持有该次查询预先登记的单行或消息序列。"""
            self.value = value

        def first(self):
            """返回当前租户会话的精确查询首行。"""
            return self.value

        def all(self):
            """返回本次会话标题查询选中的原消息序列。"""
            return self.value

    class TitleDB:
        """只为标题绑定提供会话行及该会话的原始有序消息。"""

        def __init__(self, session_row, messages):
            """初始化与生产标题读取相同顺序的两项查询结果。"""
            self.results = [QueryResult(session_row), QueryResult(messages)]

        def exec(self, _statement):
            """依次返回当前会话和生产payload读取的消息窗口。"""
            return self.results.pop(0)

    class SessionContext:
        """为标题后台函数提供隔离的伪数据库会话上下文。"""

        def __init__(self, db):
            """保留该标题查询使用的合成会话对象。"""
            self.db = db

        def __enter__(self):
            """返回唯一合成Session，不打开应用数据库。"""
            return self.db

        def __exit__(self, *_exc):
            """结束伪Session且不吞掉调用异常。"""
            return False

    memory_calls = []
    title_calls = []

    def process_memory_capture_job(db, *, job_id, worker_id):
        """在原worker边界读取测试侧观察身份，不访问模型或修改job。"""
        memory_calls.append(sdk_identity())
        return {"status": "observed", "job_id": job_id, "worker_id": worker_id}

    def summarize_title(tenant_id, user_id, session_id, agent_id):
        """在原标题任务边界读取测试侧身份，不运行模型或保存标题。"""
        title_calls.append(sdk_identity())
        return None

    monkeypatch.setattr(memory_worker, "process_memory_capture_job", process_memory_capture_job)
    monkeypatch.setattr(chat_api, "_summarize_session_title_once", summarize_title)
    monkeypatch.setattr(chat_api, "Session", lambda _engine: SessionContext(
        TitleDB(session, [source, assistant])))
    tmp_path.chmod(0o700)
    monkeypatch.setenv("FULLSTACK_FINAL_F3_LIVE", "1")
    monkeypatch.setenv("F3_LIVE_RUNTIME", str(tmp_path))

    with bind_sdk_identity(source_input_id="unrelated-parent-turn"):
        with live_caller_bindings(tmp_path, SimpleNamespace(value={})):
            stale_handoff = {"phase_sha256": digest("stale.parent.phase"), "identity_sha256": {},
                             "kind": None, "source_sha256": None}
            with bind_observation_handoff(stale_handoff):
                result = memory_worker.process_memory_capture_job(
                    MemoryDB(), job_id="capture-job", worker_id="synthetic-worker")
                chat_api._summarize_session_title_once("tenant", "owner", "session", "agent")
                memory_worker.process_memory_capture_job(
                    ExactRows({}), job_id="missing-job", worker_id="synthetic-worker")
                monkeypatch.setattr(chat_api, "Session", lambda _engine: SessionContext(TitleDB(None, [])))
                chat_api._summarize_session_title_once("tenant", "owner", "missing-session", "agent")
        assert sdk_identity()["identity_sha256"]["source_input_id"] == digest("unrelated-parent-turn")

    assert result["status"] == "observed"
    assert memory_calls[0]["identity_sha256"] == {
        "tenant_id": digest("tenant"), "user_id": digest("owner"),
        "agent_id": digest("agent"), "session_id": digest("session"),
        "source_input_id": digest("source-turn"), "turn_id": digest("source-turn"),
        "task_id": digest("capture-job"),
    }
    assert memory_calls[0]["kind"] is None
    assert title_calls[0]["identity_sha256"] == {
        "tenant_id": digest("tenant"), "user_id": digest("owner"),
        "agent_id": digest("agent"), "session_id": digest("session"),
        "source_input_id": digest("source-turn"), "turn_id": digest("source-turn"),
    }
    assert title_calls[0]["kind"] is None
    assert memory_calls[1]["identity_sha256"] == {}
    assert title_calls[1]["identity_sha256"] == {}


@pytest.mark.parametrize("case", [
    "wrong_agent", "missing_job_agent", "wrong_assistant_turn",
    "conflicting_assistant_turn_metadata", "wrong_assistant_role",
])
def test_exact_memory_capture_identity_rejects_job_agent_and_assistant_mismatch(case: str):
    """记忆任务拒绝错员工或非本轮assistant来源，不能只凭用户消息近似配对。"""
    from final_f3_live_identity import exact_memory_capture_identity

    source = SimpleNamespace(id="source-turn", tenant_id="tenant", session_id="session", role="user")
    assistant = SimpleNamespace(
        id="assistant-turn",
        tenant_id="tenant",
        session_id="session",
        role="user" if case == "wrong_assistant_role" else "assistant",
        metadata_json=(
            {"turn_id": source.id, "user_message_id": "other-turn"}
            if case == "conflicting_assistant_turn_metadata"
            else {"turn_id": "other-turn" if case == "wrong_assistant_turn" else source.id}
        ),
    )
    session = SimpleNamespace(id="session", tenant_id="tenant", user_id="owner", agent_id="agent")
    job = SimpleNamespace(
        id="capture-job",
        tenant_id="tenant",
        user_id="owner",
        agent_id=("other-agent" if case == "wrong_agent"
                  else None if case == "missing_job_agent" else "agent"),
        source_message_id=source.id,
        source_assistant_message_id=assistant.id,
    )
    rows = {"capture-job": job, source.id: source, assistant.id: assistant, session.id: session}

    class MemoryRows:
        """提供有限合成主键读取，缺失行不做模糊查询。"""

        def get(self, _model, identity):
            """按请求中的原ID返回一行。"""
            return rows.get(identity)

    assert exact_memory_capture_identity(MemoryRows(), job_id=job.id, source_sha256="a" * 64) is None
