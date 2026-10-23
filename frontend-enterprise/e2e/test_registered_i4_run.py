"""
@Time       : 2026/10/12
@Author     : zhanglp8181
@File       : test_registered_i4_run.py
@CallChain  : 正式pytest → I4诊断父合同校验 → 新run预注册来源链
@Description: 确认继承父项只能是同方言、同单案且不计验收的已登记诊断。
"""

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


def _registration_module():
    """从独立证据目录加载登记器，不运行预注册或浏览器旅程。"""
    root = Path(__file__).resolve().parents[2]
    path = (root / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261012/"
            "i4-current-source/execution-harness/registered_i4_run.py")
    spec = importlib.util.spec_from_file_location("registered_i4_run_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("I4_REGISTERED_RUN_TEST_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_parent_registration(tmp_path: Path, module, run_id: str, contract: dict) -> Path:
    """写入自洽的合成父合同、源码冻结与单次调用回执。"""
    module.ROOT = tmp_path
    module.EVIDENCE = tmp_path
    architecture = tmp_path / "architecture"
    architecture.mkdir(exist_ok=True)
    source_map = {"synthetic_source.py": "a" * 64}
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    source_freeze = {
        "run_id": run_id,
        "entry_count": len(source_map),
        "source_map": source_map,
        "source_map_sha256": module._fingerprint(source_map),
        "model_configuration_hmac_sha256": contract["model_configuration_hmac_sha256"],
    }
    contract.update({
        "source_freeze_path": str(freeze_path.relative_to(tmp_path)),
        "source_map_sha256": source_freeze["source_map_sha256"],
        "source_map_entry_count": source_freeze["entry_count"],
        "case_criteria_sha256": hashlib.sha256(
            module._canonical_bytes(contract["case_criteria"])
        ).hexdigest(),
    })
    labels_field = "manual_business_labels" if "manual_business_labels" in contract else "manual_labels"
    label_hash_field = (
        "manual_business_labels_sha256" if labels_field == "manual_business_labels"
        else "manual_labels_sha256"
    )
    contract[label_hash_field] = hashlib.sha256(
        module._canonical_bytes(contract[labels_field])
    ).hexdigest()
    freeze_path.write_text(json.dumps(source_freeze), encoding="utf-8")
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    invocation = {
        "run_id": run_id,
        "dialect": contract["dialect"],
        "invocation_count": 1,
        "retry_count": 0,
        "model_hmac_matches_previous_full20": True,
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "source_freeze_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
    }
    (architecture / f"{run_id}-invocation.safe.json").write_text(
        json.dumps(invocation), encoding="utf-8"
    )
    return contract_path


@pytest.mark.parametrize("mismatch", [None, "dialect", "case", "criteria", "credit", "provider"])
def test_explicit_diagnostic_parent_must_match_frozen_contract(tmp_path: Path, mismatch: str | None):
    """父run必须与当前方言、完整单案标准及零提交诊断合同一致。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-h2-sdk-identity-closure-diagnostic-35"
    criteria = {"JRN08 exact H2 case": ["old", "new", "unknown"]}
    contract = {
        "run_id": run_id,
        "diagnostic_only": True,
        "acceptance_credit": False,
        "diagnostic_selection": "h2-dta-knowledge",
        "dialect": "sqlite",
        "case_criteria": criteria,
        "provider_kind": "simulated_async_service",
        "expected_provider_submissions": 0,
        "manual_business_labels": {"knowledge": "synthetic"},
        "model_configuration_hmac_sha256": "b" * 64,
    }
    if mismatch == "dialect":
        contract["dialect"] = "mysql"
    elif mismatch == "case":
        contract["diagnostic_selection"] = "h2-dta-editor-baseline"
    elif mismatch == "criteria":
        contract["case_criteria"] = {"JRN08 exact H2 case": ["old", "new"]}
    elif mismatch == "credit":
        contract["acceptance_credit"] = True
    elif mismatch == "provider":
        contract["expected_provider_submissions"] = 1
    _write_parent_registration(tmp_path, module, run_id, contract)

    if mismatch is None:
        assert module._load_previous_diagnostic_contract(
            "h2-dta-knowledge", "sqlite", run_id, criteria
        ) == contract
    else:
        with pytest.raises(RuntimeError, match="I4_PREVIOUS_DIAGNOSTIC_CONTRACT_MISMATCH"):
            module._load_previous_diagnostic_contract("h2-dta-knowledge", "sqlite", run_id, criteria)


def test_registered_artifact_validation_rejects_post_registration_tampering(tmp_path: Path):
    """浏览器启动前合同或源码冻结任一原字节改变都要拒绝执行。"""
    module = _registration_module()
    module.ROOT = tmp_path
    architecture = tmp_path / "architecture"
    architecture.mkdir()
    run_id = "i3-live-sqlite-h2-registered-integrity-test"
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    source_map = {"synthetic_source.py": "a" * 64}
    criteria = {"JRN08 exact H2 case": ["old", "new", "unknown"]}
    labels = {"fixture": "synthetic"}
    freeze = {
        "run_id": run_id,
        "entry_count": len(source_map),
        "source_map": source_map,
        "source_map_sha256": module._fingerprint(source_map),
    }
    contract = {
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "h2-dta-knowledge",
        "source_freeze_path": str(freeze_path.relative_to(tmp_path)),
        "source_map_sha256": freeze["source_map_sha256"],
        "source_map_entry_count": freeze["entry_count"],
        "case_criteria": criteria,
        "case_criteria_sha256": hashlib.sha256(module._canonical_bytes(criteria)).hexdigest(),
        "manual_business_labels": labels,
        "manual_business_labels_sha256": hashlib.sha256(module._canonical_bytes(labels)).hexdigest(),
    }
    freeze_bytes = json.dumps(freeze, sort_keys=True).encode()
    contract_bytes = json.dumps(contract, sort_keys=True).encode()
    freeze_path.write_bytes(freeze_bytes)
    contract_path.write_bytes(contract_bytes)
    invocation = {
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "h2-dta-knowledge",
        "invocation_count": 1,
        "retry_count": 0,
        "model_hmac_matches_previous_full20": True,
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "source_freeze_sha256": hashlib.sha256(freeze_bytes).hexdigest(),
    }

    assert module._registered_artifacts_valid(
        run_id, "sqlite", "h2-dta-knowledge", freeze_path, contract_path,
        freeze, contract, invocation,
    )
    for field, value in (
        ("invocation_count", 2),
        ("retry_count", 1),
        ("contract_sha256", "0" * 64),
        ("source_freeze_sha256", "0" * 64),
    ):
        changed_invocation = {**invocation, field: value}
        assert not module._registered_artifacts_valid(
            run_id, "sqlite", "h2-dta-knowledge", freeze_path, contract_path,
            freeze, contract, changed_invocation,
        )

    contract["manual_business_labels"]["fixture"] = "changed-after-registration"
    contract["manual_business_labels_sha256"] = hashlib.sha256(
        module._canonical_bytes(contract["manual_business_labels"])
    ).hexdigest()
    contract_path.write_text(json.dumps(contract, sort_keys=True), encoding="utf-8")
    assert not module._registered_artifacts_valid(
        run_id, "sqlite", "h2-dta-knowledge", freeze_path, contract_path,
        freeze, contract, invocation,
    )

    contract_path.write_bytes(contract_bytes)
    freeze["source_map"]["synthetic_source.py"] = "b" * 64
    freeze_path.write_text(json.dumps(freeze, sort_keys=True), encoding="utf-8")
    assert not module._registered_artifacts_valid(
        run_id, "sqlite", "h2-dta-knowledge", freeze_path, contract_path,
        freeze, contract, invocation,
    )


def test_memory_revoke_diagnostic_accepts_only_matching_legacy_zero_effect_parent(tmp_path: Path):
    """记忆撤权续诊只能继承原同方言单案和明确零业务效果父合同。"""
    module = _registration_module()
    parent_id = "i3-live-sqlite-memory-dta-revoke-diagnostic-11"
    criteria = {
        "F3-MEMORY revoke: restored real answer cannot disclose invalid frozen facts": [
            "memory-dta-revoke-quality"
        ]
    }
    contract = {
        "run_id": parent_id,
        "diagnostic_only": True,
        "acceptance_credit": False,
        "case_shard_name": "memory-dta-revoke",
        "dialect": "sqlite",
        "case_criteria": criteria,
        "provider_kind": "simulated_async_service",
        "effect_rule": "zero business submissions expected; answer must not expose revoked facts",
        "manual_labels": {"DTA_frozen_restore_correction_clear_revoke_and_fresh_sources": True},
        "model_configuration_hmac_sha256": "c" * 64,
    }
    path = _write_parent_registration(tmp_path, module, parent_id, contract)

    assert module._load_previous_diagnostic_contract(
        "memory-dta-revoke", "sqlite", parent_id, criteria
    ) == contract

    freeze_path = tmp_path / contract["source_freeze_path"]
    frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
    frozen["source_map"]["synthetic_source.py"] = "d" * 64
    freeze_path.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(RuntimeError, match="I4_PREVIOUS_DIAGNOSTIC_ARTIFACT_MISMATCH"):
        module._load_previous_diagnostic_contract("memory-dta-revoke", "sqlite", parent_id, criteria)

    source_map = {"synthetic_source.py": "a" * 64}
    frozen.update(source_map=source_map, source_map_sha256=module._fingerprint(source_map))
    freeze_path.write_text(json.dumps(frozen), encoding="utf-8")
    contract["effect_rule"] = "one business submission expected"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(RuntimeError, match="I4_PREVIOUS_DIAGNOSTIC_CONTRACT_MISMATCH"):
        module._load_previous_diagnostic_contract("memory-dta-revoke", "sqlite", parent_id, criteria)


def test_memory_revoke_contract_freezes_zero_effect_and_runtime_linkage():
    """记忆撤权续诊固定零业务效果并独立读回原轮询截止后的Runtime终态。"""
    metadata = _registration_module()._diagnostic_contract_metadata("memory-dta-revoke")

    assert metadata["real_model_requested"] is True
    assert metadata["minimum_model_invocations"] == 1
    assert metadata["expected_provider_submissions"] == 0
    assert metadata["target_document_business_data_write_expected"] is False
    assert metadata["memory_diagnostic_observations"] == {
        "branch": "DTA",
        "scenario": "revoke",
        "expected_revoked_fact_disclosures": 0,
        "expected_business_submissions": 0,
        "expected_business_effects": 0,
        "sdk_request_to_persisted_runtime_signal_binding_required": True,
        "persisted_terminal_readback_after_original_poll_cutoff_required": True,
    }


def test_memory_freeze_contract_freezes_quality_labels_and_zero_external_effects():
    """冻结记忆续诊需预登记旧计划、后续纠正、新来源消费和零业务副作用。"""
    metadata = _registration_module()._diagnostic_contract_metadata("memory-dta-freeze")

    assert metadata["real_model_requested"] is True
    assert metadata["minimum_model_invocations"] == 1
    assert metadata["maximum_model_invocations"] == 64
    assert metadata["model_dispatch_token_budget"] == {
        "maximum_prompt_tokens_per_call": 128000,
        "maximum_prompt_tokens_total": 8192000,
        "prompt_token_counting": "backend.app.llm.client._request_tokens",
        "maximum_completion_tokens_per_call": 8192,
        "maximum_completion_tokens_total": 524288,
        "reservation_persisted_before_final_sdk_dispatch": True,
        "budget_survives_application_restart": True,
    }
    registered = {
        "maximum_model_invocations": metadata["maximum_model_invocations"],
        "model_dispatch_token_budget": metadata["model_dispatch_token_budget"],
    }
    assert _registration_module()._diagnostic_model_budget_contract_valid(
        registered, "memory-dta-freeze",
    )
    registered["model_dispatch_token_budget"] = {
        **metadata["model_dispatch_token_budget"], "maximum_completion_tokens_total": 999999,
    }
    assert not _registration_module()._diagnostic_model_budget_contract_valid(
        registered, "memory-dta-freeze",
    )
    assert metadata["expected_provider_submissions"] == 0
    assert metadata["target_document_business_data_write_expected"] is False
    assert metadata["memory_capture_job_observation_timeout_seconds"] == 360
    assert metadata["memory_freeze_dta_and_capture_model_timeout_seconds"] == 180
    assert metadata["memory_freeze_playwright_timeout_seconds"] == 6330
    assert metadata["memory_freeze_max_observation_budget_seconds"] == 3030
    assert metadata["memory_freeze_private_api_request_budget"] == {
        "maximum_reads": 5700,
        "read_count_enforced_before_request_dispatch": True,
        "fetch_timeout_seconds": 30,
        "response_json_body_timeout_seconds": 30,
        "separate_from_page_json_body_budget": True,
        "included_in": ["active_observation_3030_seconds", "after_each_550_seconds",
                        "runner_total_7050_seconds"],
        "conservative_read_count_basis": {
            "source_persistence_polls": 4 * 401,
            "wait_memory_polls_and_terminal_readbacks": 3 * 602,
            "capture_job_polls_and_final_readbacks": 2 * 722,
            "sdk_quiescence_polls": 2 * 241,
            "restart_pid_polls": 241,
            "bootstrap_health_and_state_reads": 24,
            "derived_upper_bound": 5601,
            "enforcement_margin": 99,
        },
    }
    assert metadata["memory_freeze_provider_observation_read_budget"] == {
        "endpoint": "/simulation/observations",
        "maximum_reads": 2,
        "read_count_enforced_before_request_dispatch": True,
        "fetch_timeout_seconds": 30,
        "response_json_body_timeout_seconds": 30,
        "separate_from_private_api_request_budget": True,
        "included_in": ["active_observation_3030_seconds", "after_each_550_seconds",
                        "runner_total_7050_seconds"],
    }
    assert metadata["memory_freeze_provider_observation_read_budget"] == {
        "endpoint": "/simulation/observations",
        "maximum_reads": 2,
        "read_count_enforced_before_request_dispatch": True,
        "fetch_timeout_seconds": 30,
        "response_json_body_timeout_seconds": 30,
        "separate_from_private_api_request_budget": True,
        "included_in": ["active_observation_3030_seconds", "after_each_550_seconds",
                        "runner_total_7050_seconds"],
    }
    assert metadata["memory_freeze_total_deadline_enforcement"] == {
        "runner_signal_timer_armed_before_setup": True,
        "covers_setup_child_monitor_cleanup_and_archive": True,
        "runner_total_seconds_from_main_entry": 7050,
        "outer_watchdog_is_emergency_reap_only": True,
    }
    assert metadata["memory_freeze_observation_budget_components_seconds"] == {
        "wait_memory_three_calls": 1080,
        "capture_job_two_calls": 840,
        "source_turn_persistence_four_calls": 600,
        "restart_sdk_quiescence": 300,
        "restart_process_poll": 210,
    }
    assert metadata["memory_freeze_budget_details_seconds"] == {
        "wait_memory_poll_windows": 900,
        "wait_memory_inflight_fetches": 90,
        "wait_memory_terminal_readbacks": 90,
        "capture_serial_model_calls": 720,
        "capture_inflight_poll_fetches": 60,
        "capture_final_readbacks": 60,
        "source_persistence_poll_windows": 480,
        "source_persistence_inflight_fetches": 120,
        "restart_quiescence_poll": 240,
        "restart_quiescence_inflight_fetch": 30,
        "restart_before_state_readback": 30,
        "restart_process_poll": 120,
        "restart_process_poll_inflight_fetch": 30,
        "restart_after_state_readback": 30,
        "restart_page_reload": 30,
    }
    assert metadata["memory_freeze_active_budget_residual_after_observation_and_page_json_seconds"] == 3060
    deadline_budget = metadata["memory_freeze_absolute_deadline_budget_seconds"]
    assert deadline_budget == {
        "active_from_runner_entry_including_setup_observation_and_test": 6330,
        "after_each": 550,
        "browser_and_context_fixture_close": 30,
        "webserver_shutdown": 20,
        "application_and_provider_cleanup": 46,
        "parent_mysql_cleanup": 10,
        "parent_evidence_archive": 64,
        "total_from_runner_entry": 7050,
    }
    assert sum(value for key, value in deadline_budget.items() if key != "total_from_runner_entry") == \
        deadline_budget["total_from_runner_entry"]
    assert metadata["memory_freeze_page_json_body_read_path_maximum"] == {
        "login_and_context": 2,
        "login_token_readback": 1,
        "employee_refresh_and_use": 8,
        "composer_context_refresh": 4,
        "relation_response_readbacks": 2,
        "attention_list_readback": 1,
        "original_answer_message_readback": 2,
        "total": 20,
    }
    assert metadata["memory_freeze_page_json_body_read_path_maximum"]["total"] == sum(
        metadata["memory_freeze_page_json_body_read_path_maximum"][key]
        for key in (
            "login_and_context",
            "login_token_readback",
            "employee_refresh_and_use",
            "composer_context_refresh",
            "relation_response_readbacks",
            "attention_list_readback",
            "original_answer_message_readback",
        )
    )
    assert metadata["memory_freeze_page_json_body_cumulative_budget_seconds"] == 240
    assert metadata["memory_freeze_page_json_body_read_count_enforced_at_runtime"] is True
    assert metadata["memory_freeze_page_json_body_maximum_reads"] == 20
    assert metadata["memory_freeze_browser_operation_bounds"] == {
        "page_action_timeout_seconds": 30,
        "navigation_timeout_seconds": 30,
        "json_response_body_timeout_seconds": 30,
        "json_response_body_max_page_reads": 20,
        "json_response_body_cumulative_budget_seconds": 240,
        "webserver_startup_guard_seconds": 60,
        "composer_dom_probe_timeout_seconds": 30,
        "original_answer_fetch_timeout_seconds": 30,
        "simulated_provider_http_timeout_seconds": 30,
        "session_metadata_total_deadline_seconds": 180,
        "handoff_metadata_total_deadline_seconds": 60,
        "session_metadata_late_response_grace_seconds": 15,
        "session_metadata_max_request_wait_seconds": 60,
        "session_frame_deadline_seconds": 5,
    }
    assert sum(metadata["memory_freeze_budget_details_seconds"].values()) == \
        metadata["memory_freeze_max_observation_budget_seconds"]
    assert sum(metadata["memory_freeze_observation_budget_components_seconds"].values()) == \
        metadata["memory_freeze_max_observation_budget_seconds"]
    assert metadata["memory_freeze_max_observation_budget_seconds"] \
        + metadata["memory_freeze_page_json_body_cumulative_budget_seconds"] \
        + metadata["memory_freeze_active_budget_residual_after_observation_and_page_json_seconds"] \
        == metadata["memory_freeze_playwright_timeout_seconds"]
    assert metadata["memory_capture_job_final_readback_required"] is True
    assert metadata["post_deadline_readback_required_when_capture_poll_times_out"] is True
    assert metadata["capture_terminal_status_recorded_when_available"] is True
    assert metadata["memory_source_job_binding_requirements"] == {
        "correction_user_turn_to_unique_completed_capture_job": True,
        "capture_job_to_source_fact_and_capture_job_id": True,
        "sdk_task_id_to_exact_persisted_capture_job": True,
        "same_tenant_user_agent_session_and_source_turn": True,
        "fresh_user_turn_to_capture_job_and_fact_consumed_by_dta": True,
        "dta_consumer_to_persisted_execution_and_answer": True,
        "persisted_signal_binding_required_when_sdk_identity_carries_signal_id": True,
    }
    assert metadata["memory_diagnostic_observations"] == {
        "branch": "DTA",
        "scenario": "freeze_restart_correction_clear_fresh_source",
        "frozen_answer_labels": {"NAME": "青禾", "STYLE": "简洁要点", "CODE": "未知"},
        "later_correction_labels_must_not_replace_frozen_answer": {
            "NAME": "映竹", "STYLE": "详细表格",
        },
        "cleared_fresh_source_labels": {"NAME": "柳南", "STYLE": "简洁要点"},
        "old_projection_revived_after_regrant": False,
        "business_submissions": 0,
        "business_effects": 0,
        "knowledge_document_business_write": False,
    }


def test_frozen_diagnostic_wrapper_timeout_signals_only_owned_groups(monkeypatch, tmp_path):
    """登记器看门狗超时只向本runner与经身份绑定的浏览器进程组发有界信号。"""
    import signal

    module = _registration_module()
    calls = []

    class FakeProcess:
        """纯控制Popen替身，不派发真实runner或浏览器。"""

        pid = 654321
        returncode = -signal.SIGTERM

        def communicate(self, timeout=None):
            """首次模拟超时，收到TERM后返回合成stdout/stderr。"""
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise module.subprocess.TimeoutExpired("synthetic-runner", timeout,
                                                       output=b"partial", stderr=b"private")
            return b"complete", b"bounded"

        def poll(self):
            """模拟第二次bounded communicate完成后的runner退出。"""
            return self.returncode

        def terminate(self):
            """仅记录Popen所拥有runner的TERM，不对系统进程组发信号。"""
            calls.append(("runner_terminate", self.pid))

    fake = FakeProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(module, "MEMORY_FREEZE_REGISTERED_RUNNER_WATCHDOG_SECONDS", 5)
    monkeypatch.setattr(module, "_signal_private_browser_group",
                        lambda _private, _run_id, signum: calls.append(("browser", signum)) or True)

    completed, expired, watchdog = module._run_frozen_diagnostic_child(
        ["synthetic-runner"], {}, tmp_path / "owned", "i3-live-sqlite-memory-dta-freeze-capture-diagnostic-41",
    )

    assert expired is True
    assert completed.returncode == 124
    assert completed.stdout == b"complete"
    assert watchdog["runner_reaped"] is True
    assert watchdog["owned_browser_group_term_sent"] is True
    assert watchdog["owned_browser_group_kill_sent"] is False
    assert calls[0][0] == "communicate" and 4.9 < calls[0][1] <= 5
    assert calls[1:] == [
        ("browser", signal.SIGTERM), ("runner_terminate", fake.pid), ("communicate", 2),
    ]


def test_frozen_diagnostic_wrapper_bounds_hard_stop_wait(monkeypatch, tmp_path):
    """runner忽略TERM时登记器最多再等待硬上限，之后留下未回收状态。"""
    import signal

    module = _registration_module()
    calls = []

    class StuckProcess:
        """永久不完成的合成wrapper child，不产生真实进程。"""

        pid = 654322

        def communicate(self, timeout=None):
            """每次都按传入上限返回TimeoutExpired，不执行无界wait。"""
            calls.append(("communicate", timeout))
            raise module.subprocess.TimeoutExpired("synthetic-runner", timeout,
                                                   output=b"partial", stderr=b"partial")

        def poll(self):
            """模拟硬kill后仍未被内核确认回收。"""
            return None

        def terminate(self):
            """记录Popen所属runner收到TERM。"""
            calls.append(("runner_terminate", self.pid))

        def kill(self):
            """记录Popen所属runner收到SIGKILL。"""
            calls.append(("runner_kill", self.pid))

    fake = StuckProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(module, "MEMORY_FREEZE_REGISTERED_RUNNER_WATCHDOG_SECONDS", 5)
    monkeypatch.setattr(module, "_signal_private_browser_group",
                        lambda _private, _run_id, signum: calls.append(("browser", signum)) or True)

    completed, expired, watchdog = module._run_frozen_diagnostic_child(
        ["synthetic-runner"], {}, tmp_path / "owned", "i3-live-sqlite-memory-dta-freeze-capture-diagnostic-41",
    )

    assert expired is True
    assert completed.returncode == 124
    assert watchdog["runner_reaped"] is False
    assert watchdog["owned_browser_group_term_sent"] is True
    assert watchdog["owned_browser_group_kill_sent"] is True
    assert calls[0][0] == "communicate" and 4.9 < calls[0][1] <= 5
    assert calls[1:] == [
        ("browser", signal.SIGTERM), ("runner_terminate", fake.pid),
        ("communicate", 2),
        ("browser", signal.SIGKILL), ("runner_kill", fake.pid),
        ("communicate", 1),
    ]


def test_memory_freeze_diagnostic_registration_is_scoped_to_sqlite_and_unique_namespace():
    """本轮修补诊断只允许唯一编号的SQLite memory-dta-freeze登记组合。"""
    registration = _registration_module()
    valid_run = "i3-live-sqlite-memory-dta-freeze-capture-diagnostic-39"
    registration.validate_diagnostic_scope(valid_run, "sqlite", "memory-dta-freeze")
    with pytest.raises(RuntimeError, match="I4_MEMORY_FREEZE_DIAGNOSTIC_SQLITE_SCOPE_REQUIRED"):
        registration.validate_diagnostic_scope(valid_run, "mysql", "memory-dta-freeze")
    with pytest.raises(RuntimeError, match="I4_MEMORY_FREEZE_DIAGNOSTIC_SQLITE_SCOPE_REQUIRED"):
        registration.validate_diagnostic_scope("i3-live-unscoped-memory-freeze-39", "sqlite",
            "memory-dta-freeze")
    for diagnostic_name in (None, "full20", "memory-dta-revoke"):
        with pytest.raises(RuntimeError, match="I4_MEMORY_FREEZE_DIAGNOSTIC_SQLITE_SCOPE_REQUIRED"):
            registration.validate_diagnostic_scope(valid_run, "sqlite", diagnostic_name)
    with pytest.raises(RuntimeError, match="I4_MEMORY_FREEZE_DIAGNOSTIC_SQLITE_SCOPE_REQUIRED"):
        registration.validate_diagnostic_scope(
            "i3-live-sqlite-memory-dta-freeze-capture-diagnostic-39-extra",
            "sqlite", "memory-dta-freeze")
    registration.validate_diagnostic_scope("i3-live-mysql-memory-dta-revoke-39", "mysql",
        "memory-dta-revoke")


def test_memory_freeze_labels_do_not_inherit_unrelated_knowledge_template():
    """记忆单案人工标签独立于知识模板，并准确记录标签模板来源。"""
    module = _registration_module()
    diagnostic = module._diagnostic_contract_metadata("memory-dta-freeze")
    parent_labels = {
        "journey": "H2 DTA knowledge diagnostic",
        "knowledge": {"topic": "UNRELATED-KNOWLEDGE-TOPIC"},
        "memory_quality": {"raw_large_fact_published": False},
    }
    parent = {
        "run_id": "i3-live-sqlite-h2-dta-knowledge-scope-fix-diagnostic-25",
        "manual_business_labels": parent_labels,
        "manual_business_labels_sha256": hashlib.sha256(
            module._canonical_bytes(parent_labels)
        ).hexdigest(),
    }
    registered_at = module.datetime(2026, 10, 12, 9, 30, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    labels = module._memory_freeze_manual_labels(diagnostic, "sqlite", registered_at, parent)

    assert labels["memory_quality_subcase"] == diagnostic["memory_diagnostic_observations"]
    assert labels["label_template_run_id"] == parent["run_id"]
    assert "knowledge" not in labels
    assert "previous_run_id" not in labels
    assert labels["label_source"] == (
        "frontend-enterprise/e2e/staffdeck-final-f3-memory.live.fullstack.e2e.ts"
    )
    assert module._diagnostic_label_source_metadata(parent, None, "c" * 64) == {
        "label_template_run_id": parent["run_id"],
        "label_template_contract_sha256": "c" * 64,
    }
    assert module._diagnostic_label_source_metadata(parent, parent["run_id"], "c" * 64) == {
        "previous_run_id": parent["run_id"],
    }


def test_registered_attempt_claim_is_exclusive_before_child_dispatch(tmp_path: Path) -> None:
    """预先独占登记单次attempt，失败早退后相同run-id也不能再次派发。"""
    module = _registration_module()
    attempt_path = tmp_path / "architecture" / "synthetic-run-attempt.safe.json"
    claim = {
        "run_id": "i3-live-synthetic-once",
        "dialect": "sqlite",
        "diagnostic_selection": "memory-dta-freeze",
        "invocation_count": 1,
        "retry_count": 0,
    }
    dispatches = []

    def dispatch_first() -> int:
        """在真实派发stub运行前确认独占claim已安全落盘。"""
        assert attempt_path.is_file()
        assert json.loads(attempt_path.read_text(encoding="utf-8")) == claim
        dispatches.append("first")
        return 17

    first = module._run_registered_attempt_once(
        attempt_path, claim, dispatch_first,
    )
    with pytest.raises(RuntimeError, match="I4_REGISTERED_ATTEMPT_ALREADY_CLAIMED"):
        module._run_registered_attempt_once(
            attempt_path, claim, lambda: dispatches.append("retry") or 18,
        )

    assert first == 17
    assert dispatches == ["first"]
    assert json.loads(attempt_path.read_text(encoding="utf-8")) == claim
