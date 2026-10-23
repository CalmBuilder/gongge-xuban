"""
@Time       : 2026/10/14
@Author     : zhanglp8181
@File       : test_final_f3_live_server.py
@CallChain  : pytest → 自有F3启动计划 → 私有边界/来源/双库身份/真模型配置
@Description: 无外呼验证模拟来源、诊断专属SDK预算隔离与真实模型配置边界。
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

import start_final_f3_live_server as live_server
from start_final_f3_live_server import (
    F3SessionListRequestProbe, LiveConfigurationError, build_live_plan, read_private,
)


def configuration() -> dict:
    """仅生成合成声明；真实模型端点和数据库密码不参与正式测试。"""
    return {"schema_version": "f3-live-simulated-v1", "run_id": "f3-owned-test",
            "port": 51444, "provider_kind": "simulated_async_service",
            "provider_base_url": "http://127.0.0.1:51445", "provider_namespace": "f3-sim-" + "a" * 16,
            "provider_key": "SIMULATION_PRIVATE_CANARY_" + "x" * 32,
            "provider_control_key": "SIMULATION_CONTROL_CANARY_" + "y" * 32,
            "model_base_url": "https://models.example.org/v1", "model_name": "private_unit_model",
            "model_api_key": "MODEL_PRIVATE_CANARY_" + "z" * 32,
            "model_temperature": 0, "model_max_tokens": 8192,
            "model_extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "dialect": "sqlite", "database_url": None,
            "initiator_username": "f3_initiator", "reviewer_username": "f3_reviewer",
            "initiator_password": "OWNED_SYNTHETIC_INITIATOR", "reviewer_password": "OWNED_SYNTHETIC_REVIEWER",
            "manual_labels": {"product_name": "ALPHA91", "quantity": 3,
                              "customer": "SYNTHETIC-F3", "due_date": "2026-10-05"}}


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    """每例新建700目录，只允许本测试拥有的隔离运行文件。"""
    path = tmp_path / "f3-live-sqlite-owned"
    path.mkdir(mode=0o700)
    return path


def test_live_plan_freezes_real_model_and_explicit_simulation(runtime: Path) -> None:
    """合法新计划保原模型参数与模拟来源，摘要不含秘密或供应商资格。"""
    config = configuration()
    plan = build_live_plan(runtime, config)
    assert plan.environment["DEMO_MODEL_BASE_URL"] == config["model_base_url"]
    assert plan.environment["DEMO_MODEL_API_KEY"] == config["model_api_key"]
    assert plan.environment["DATABASE_URL"] == f"sqlite:///{runtime}/journey.sqlite3"
    assert plan.environment["DYNAMIC_TASK_EXTERNAL_WRITE_ENABLED"] == "true"
    assert plan.environment["GENERAL_SKILL_IMPORT_V2_ENABLED"] == "true"
    summary = plan.safe_summary()
    assert summary["provider_kind"] == "simulated_async_service"
    assert summary["real_vendor_qualified"] is False and summary["JRN08_passed"] is False
    assert all(value not in json.dumps(summary) for value in
               (config["model_api_key"], config["provider_key"], config["initiator_password"]))
    config["model_base_url"] = "https://changed.example.org"
    assert plan.environment["DEMO_MODEL_BASE_URL"] == "https://models.example.org/v1"
    with pytest.raises(TypeError):
        plan.environment["DEMO_MODEL_NAME"] = "changed"
    with pytest.raises(FrozenInstanceError):
        plan.port = 1


def test_memory_freeze_run_enables_only_its_persistent_model_budget_profile(runtime: Path) -> None:
    """只有精确SQLite记忆冻结诊断编号才将真实模型预算开关传入应用进程。"""
    config = configuration()
    config["run_id"] = "f3-i3-live-sqlite-memory-dta-freeze-capture-diagnostic-42"
    plan = build_live_plan(runtime, config)
    assert plan.environment["F3_MODEL_BUDGET_PROFILE"] == "memory-dta-freeze-v1"
    config["run_id"] = "f3-i3-live-sqlite-memory-dta-freeze-capture-diagnostic-42-extra"
    other_runtime = runtime.parent / "f3-live-sqlite-unrelated"
    other_runtime.mkdir(mode=0o700)
    unrelated = build_live_plan(other_runtime, config)
    assert "F3_MODEL_BUDGET_PROFILE" not in unrelated.environment


def test_sop_first_three_run_enables_its_persistent_model_budget_profile(
        runtime: Path, monkeypatch) -> None:
    """只有本轮准确SOP三案run id才开启其最终SDK预算守卫。"""
    config = configuration()
    config["run_id"] = "f3-i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03"
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")
    monkeypatch.setenv("F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256", "a" * 64)
    plan = build_live_plan(runtime, config)
    assert plan.environment["F3_MODEL_BUDGET_PROFILE"] == "i2-sop-first-three-v1"

    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "dta-normal")
    other_runtime = runtime.parent / "f3-live-sqlite-unrelated-sop-profile"
    other_runtime.mkdir(mode=0o700)
    unrelated = build_live_plan(other_runtime, config)
    assert "F3_MODEL_BUDGET_PROFILE" not in unrelated.environment


def test_sop_first_three_plan_forwards_only_the_frozen_model_payload_fingerprint(
        runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SOP诊断服务子进程须收到已登记模型指纹，摘要不能泄露其值。"""
    fingerprint = "a" * 64
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")
    monkeypatch.setenv("F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256", fingerprint)

    plan = build_live_plan(runtime, configuration())

    assert plan.environment["F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256"] == fingerprint
    assert fingerprint not in json.dumps(plan.safe_summary())


@pytest.mark.parametrize("fingerprint", ["invalid", "A" * 64, "a" * 63])
def test_sop_first_three_plan_rejects_malformed_frozen_model_fingerprint(
        runtime: Path, monkeypatch: pytest.MonkeyPatch, fingerprint: str) -> None:
    """SOP诊断不得把缺格式校验的模型配置身份送进真实应用。"""
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")
    monkeypatch.setenv("F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256", fingerprint)

    with pytest.raises(LiveConfigurationError, match="F3_MODEL_CONFIG_FROZEN_FINGERPRINT_INVALID"):
        build_live_plan(runtime, configuration())


def test_sop_first_three_plan_requires_the_frozen_model_fingerprint(
        runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少已登记模型指纹时服务必须在启动前拒绝SOP真实模型诊断。"""
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")
    monkeypatch.delenv("F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256", raising=False)

    with pytest.raises(LiveConfigurationError, match="F3_MODEL_CONFIG_FROZEN_FINGERPRINT_REQUIRED"):
        build_live_plan(runtime, configuration())


def test_live_plan_sets_positive_safety_alert_thresholds_for_exact_write_scope(runtime: Path) -> None:
    """仅自有高风险写测试配置正向停止阈值，不能因默认零值静默移除工具。"""
    plan = build_live_plan(runtime, configuration())
    threshold_names = (
        "DYNAMIC_TASK_ALERT_SIGNAL_BACKLOG_THRESHOLD",
        "DYNAMIC_TASK_ALERT_DEAD_LETTER_THRESHOLD",
        "DYNAMIC_TASK_ALERT_UNKNOWN_OPERATION_THRESHOLD",
        "DYNAMIC_TASK_ALERT_PUBLICATION_BACKLOG_THRESHOLD",
        "DYNAMIC_TASK_ALERT_WAITING_AGE_SECONDS",
    )
    assert all(int(plan.environment[name]) > 0 for name in threshold_names)


def test_server_retains_only_exactly_registered_session_stack_probe_flag() -> None:
    """应用环境重置只保留本轮已登记探针，不透传其他调试标志或秘密。"""
    assert live_server.preserved_server_diagnostic_flags({
        "F3_DIAGNOSTIC_SESSION_STACK": "1", "F3_DIAGNOSTIC_DUMP_STACK": "1",
        "PRIVATE_CANARY": "do-not-forward",
    }) == {"F3_DIAGNOSTIC_SESSION_STACK": "1", "F3_DIAGNOSTIC_DUMP_STACK": "1"}
    assert live_server.preserved_server_diagnostic_flags({
        "F3_DIAGNOSTIC_SESSION_STACK": "true",
    }) == {}
    assert live_server.preserved_server_diagnostic_flags({
        "F3_DIAGNOSTIC_SESSION_STACK": "0",
    }) == {}
    assert live_server.preserved_server_diagnostic_flags({
        "F3_DIAGNOSTIC_DUMP_STACK": "true",
    }) == {}


def test_signal_stack_dump_requires_exact_private_diagnostic_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台栈转储只在单例诊断开关精确为1时登记，不改变默认服务信号。"""
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(live_server.faulthandler, "register", lambda sig, **kwargs: calls.append((sig, kwargs)))
    assert live_server.register_diagnostic_stack_dump({"F3_DIAGNOSTIC_DUMP_STACK": "0"}) is False
    assert calls == []
    assert live_server.register_diagnostic_stack_dump({"F3_DIAGNOSTIC_DUMP_STACK": "1"}) is True
    assert len(calls) == 1
    assert calls[0][1] == {"file": live_server.sys.stderr, "all_threads": True, "chain": False}


@pytest.mark.parametrize("change", ["claimed_vendor", "local_model", "wrong_database",
                                    "same_actor", "unregistered_field", "same_auth", "bad_port"])
def test_non_owned_or_false_provenance_configuration_is_rejected(runtime: Path, change: str) -> None:
    """拒绝伪造供应商、固定本地模型、真实库和本人自批配置，不回显配置。"""
    config = configuration()
    if change == "claimed_vendor":
        config["provider_kind"] = "vendor_sandbox"
    elif change == "local_model":
        config["model_base_url"] = "http://127.0.0.1:52010/v1"
    elif change == "wrong_database":
        config["database_url"] = "sqlite:////outside/database.sqlite3"
    elif change == "same_actor":
        config["reviewer_username"] = config["initiator_username"]
    elif change == "unregistered_field":
        config["fake_model_output"] = "PRIVATE_NEW_CANARY"
    elif change == "same_auth":
        config["provider_control_key"] = config["provider_key"]
    else:
        config["port"] = True
    with pytest.raises(LiveConfigurationError) as captured:
        build_live_plan(runtime, config)
    assert config["model_api_key"] not in str(captured.value)


def test_restart_keeps_original_configuration_and_owned_database(runtime: Path) -> None:
    """真重启复用本轮原配置及原库，改配置或缺库不能重新播种掩盖恢复。"""
    config = configuration()
    first = build_live_plan(runtime, config)
    database = runtime / "journey.sqlite3"
    database.write_bytes(b"SYNTHETIC_ORIGINAL_DATABASE_BYTES")
    database.chmod(0o600)
    second = build_live_plan(runtime, config, reuse=True)
    assert first.configuration_sha256 == second.configuration_sha256
    config["manual_labels"]["quantity"] = 4
    with pytest.raises(LiveConfigurationError):
        build_live_plan(runtime, config, reuse=True)
    assert database.read_bytes() == b"SYNTHETIC_ORIGINAL_DATABASE_BYTES"


def test_private_configuration_loader_refuses_links_and_public_mode(runtime: Path) -> None:
    """配置引用必须私有普通文件；公开模式及链接不能伪装隔离来源。"""
    path = runtime / "configuration.private.json"
    path.write_text(json.dumps(configuration()))
    path.chmod(0o644)
    with pytest.raises(LiveConfigurationError):
        read_private(path)
    path.chmod(0o600)
    assert read_private(path)["provider_kind"] == "simulated_async_service"
    link = runtime / "alias.private.json"
    link.symlink_to(path)
    with pytest.raises(LiveConfigurationError):
        read_private(link)


def test_session_request_probe_records_only_safe_route_timing_and_pending_stack(
    runtime: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅记录会话列表状态与耗时，屏蔽查询身份且其他路由透明透传。"""
    dumps: list[dict[str, object]] = []
    monkeypatch.setattr(live_server.faulthandler, "dump_traceback", lambda **kwargs: dumps.append(kwargs))

    async def exercise() -> None:
        """用合成ASGI响应验证挂起观察器不改变原响应。"""
        emitted: list[dict[str, object]] = []

        async def app(scope, receive, send) -> None:
            """模拟短暂挂起的原路由，不访问数据库或外部服务。"""
            del receive
            await asyncio.sleep(0.02)
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"[]", "more_body": False})

        async def receive() -> dict[str, object]:
            """满足ASGI签名；合成应用不消费输入。"""
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            """收集透传响应以断言探针没有改写状态或正文。"""
            emitted.append(message)

        probe = F3SessionListRequestProbe(app, runtime, stack_after_seconds=0.001)
        await probe({"type": "http", "method": "GET", "path": "/api/chat/sessions",
                     "query_string": b"tenant_id=PRIVATE_SYNTHETIC_TENANT"}, receive, send)
        await probe({"type": "http", "method": "GET", "path": "/api/auth/context",
                     "query_string": b"private=ignored"}, receive, send)
        assert emitted == [
            {"type": "http.response.start", "status": 200},
            {"type": "http.response.body", "body": b"[]", "more_body": False},
            {"type": "http.response.start", "status": 200},
            {"type": "http.response.body", "body": b"[]", "more_body": False},
        ]

    asyncio.run(exercise())
    journal = runtime / "session-metadata-diagnostic.safe.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [row["event"] for row in rows] == [
        "request_start", "request_pending", "response_start", "response_complete",
    ]
    assert rows[-2]["status"] == rows[-1]["status"] == 200
    assert all("PRIVATE_SYNTHETIC_TENANT" not in json.dumps(row) for row in rows)
    assert journal.stat().st_mode & 0o077 == 0
    assert len(dumps) == 1 and dumps[0]["all_threads"] is True
