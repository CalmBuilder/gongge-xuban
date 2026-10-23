"""
@Time       : 2026/10/15
@Author     : zhanglp8181
@File       : test_i2_case_binding_03_registration.py
@CallChain  : pytest → I2候选04登记器 → 精确诊断组合同
@Description: 锁定SOP三案预检组必须可精确登记、来源冻结并保持诊断零验收积分。
"""

from __future__ import annotations

import hashlib
import importlib.util
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / (
    "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/"
    "i2-case-sdk-binding-04/execution-harness"
)


def _registration_module():
    """加载候选04登记器，不读取私有模型配置或启动外部依赖。"""
    path = HARNESS / "registered_i4_run.py"
    spec = importlib.util.spec_from_file_location("i2_binding_03_registration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("I2_BINDING_03_REGISTRATION_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _browser_runner():
    """加载当前候选浏览器运行器，不派发子进程或读取真实配置。"""
    path = HARNESS / "run_browser_live.py"
    spec = importlib.util.spec_from_file_location("i2_binding_03_browser_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("I2_BINDING_03_RUNNER_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(HARNESS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HARNESS))
    return module


def _browser_child():
    """加载真实浏览器子runner模块，不启动模拟服务或业务应用。"""
    path = HARNESS / "live_browser_child.py"
    spec = importlib.util.spec_from_file_location("i2_binding_03_browser_child", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("I2_BINDING_03_BROWSER_CHILD_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_snapshot(runtime: Path, name: str, content: bytes) -> Path:
    """创建仅当前用户可读的合成快照文件。"""
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    path = runtime / name
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _configure_synthetic_registered_run(
        module, tmp_path: Path, monkeypatch, run_id: str, runner_path: Path):
    """构造仅含合成合同和临时路径的登记运行，不读取模型配置或业务数据库。"""
    criteria = {
        f"JRN08 SOP synthetic case {index}": [f"criterion-{index}"]
        for index in range(1, 4)
    }
    runner = SimpleNamespace(
        PRIVATE=tmp_path / "runner-private",
        EVIDENCE=tmp_path / "runner-evidence",
        PYTHON=Path(sys.executable),
        BUDGET=1_572_864,
        expected_case_criteria=lambda _name=None: criteria,
        private_tmpfs_preflight=lambda _path: {"valid": True},
    )
    source_map = {"synthetic-source.py": "a" * 64}
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "EVIDENCE", tmp_path / "registry-evidence")
    monkeypatch.setattr(module, "PRIVATE_RUN_LOGS", tmp_path / "private-logs")
    monkeypatch.setattr(
        module, "REGISTERED_HEAVY_JOB_LOCK_PATH",
        tmp_path / "private-state" / "registered-heavy-job.lock", raising=False,
    )
    monkeypatch.setattr(module, "RUNNER_PATH", runner_path)
    monkeypatch.setattr(module, "_source_map", lambda: (runner, source_map))
    monkeypatch.setattr(module, "_registered_artifacts_valid", lambda *_args: True)
    monkeypatch.setattr(module, "_model_hmac", lambda: "b" * 64)
    monkeypatch.setattr(module, "_model_payload_sha256", lambda: "c" * 64)
    monkeypatch.setattr(module, "_model_payload", lambda: {
        "base_url": "https://api.deepseek.com/v1",
        "model": "synthetic-model",
        "api_key": "SYNTHETIC_MODEL_KEY",
    })
    monkeypatch.setattr(module, "validate_diagnostic_scope", lambda *_args: None)
    architecture = module.EVIDENCE / "architecture"
    architecture.mkdir(parents=True)
    (architecture / f"{run_id}-source-freeze.safe.json").write_text(json.dumps({
        "source_map_sha256": module._fingerprint(source_map),
    }), encoding="utf-8")
    (architecture / f"{run_id}-preregistered-contract.safe.json").write_text(json.dumps({
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "diagnostic_only": True,
        "acceptance_credit": False,
        "case_criteria": criteria,
        "model_configuration_hmac_sha256": "b" * 64,
        "model_configuration_payload_sha256": "c" * 64,
    }), encoding="utf-8")
    (architecture / f"{run_id}-invocation.safe.json").write_text(json.dumps({
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "invocation_count": 1,
        "retry_count": 0,
    }), encoding="utf-8")
    return architecture


def test_diagnostic_label_template_points_to_audited_existing_source() -> None:
    """三案人工标签模板必须绑定到已审阅且真实存在的历史合同。"""
    module = _registration_module()
    expected = ROOT / (
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261012/"
        "i4-current-source/architecture/"
        "i3-live-sqlite-h2-dta-knowledge-scope-fix-diagnostic-25-"
        "preregistered-contract.safe.json"
    )

    assert module.DIAGNOSTIC_LABEL_TEMPLATE == expected
    assert expected.is_file()
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == (
        "52e141752675f326d8e626e23df88727252270e821e2d253b4bf4c5c3f07a2b0"
    )


@pytest.mark.parametrize("business_labels_match", [True, False])
def test_sop_first_three_group_preserves_only_source_matched_business_labels(
        tmp_path: Path, monkeypatch, business_labels_match: bool) -> None:
    """三案预检须保留精确业务标签并拒绝偏离已审核来源的标签。"""
    module = _registration_module()
    titles = {f"JRN08 SOP synthetic case {index}": {f"criterion-{index}"}
              for index in range(1, 4)}
    runner = SimpleNamespace(
        SINGLE_CASE_SHARD_NAMES=frozenset(),
        DIAGNOSTIC_CASE_GROUPS={"sop-first-three": tuple(titles)},
        BUDGET=1_572_864,
        expected_case_criteria=lambda name: titles if name == "sop-first-three" else {},
        FROZEN_FRONTEND_BUILD_ROOT=tmp_path / "frontend-build",
    )
    monkeypatch.setattr(module, "_source_map", lambda: (runner, {"candidate.py": "a" * 64}))
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "registered_i4_run.py"))
    monkeypatch.setattr(module, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(module, "RUNNER_PATH", tmp_path / "run_browser_live.py")
    monkeypatch.setattr(module, "_model_hmac", lambda: "b" * 64)
    monkeypatch.setattr(module, "_model_payload_sha256", lambda: "c" * 64)
    monkeypatch.setattr(module, "_model_payload", lambda: {
        "base_url": "https://api.deepseek.com/v1",
        "model": "synthetic-model",
        "api_key": "SYNTHETIC_MODEL_KEY",
    })
    model_contract = tmp_path / "model-contract.json"
    model_contract.write_text(json.dumps({
        "model_configuration": {"private_fingerprint_record_sha256": "b" * 64},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "MODEL_CONTRACT", model_contract)
    label_template = tmp_path / "label-template.json"
    template_business = dict(module.F3_BUSINESS_LABELS)
    if not business_labels_match:
        template_business["quantity"] += 1
    template = {"run_id": "synthetic-label-template", "manual_business_labels": {
        "journey": "synthetic source labels", "business": template_business,
    }}
    label_template.write_text(json.dumps(template), encoding="utf-8")
    monkeypatch.setattr(module, "DIAGNOSTIC_LABEL_TEMPLATE", label_template)

    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03"
    contract_path = (tmp_path / "evidence" / "architecture"
                     / f"{run_id}-preregistered-contract.safe.json")
    if business_labels_match:
        module.preregister(run_id, "sqlite", "sop-first-three")
    else:
        with pytest.raises(RuntimeError, match="I4_DIAGNOSTIC_MANUAL_BUSINESS_LABELS_MISMATCH"):
            module.preregister(run_id, "sqlite", "sop-first-three")
        assert not contract_path.exists()
        assert not (tmp_path / "evidence" / "architecture"
                    / f"{run_id}-source-freeze.safe.json").exists()
        return

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["browser_case_count"] == 3
    assert set(contract["case_criteria"]) == set(titles)
    assert contract["diagnostic_only"] is True
    assert contract["acceptance_credit"] is False
    assert contract["real_model_requested"] is True
    assert contract["model_preflight_required"] is True
    assert contract["expected_provider_submissions"] == 3
    assert contract["manual_business_labels"]["diagnostic_only_not_acceptance"] is True
    assert contract["manual_business_labels"]["business"] == module.F3_BUSINESS_LABELS
    assert contract["provider_account_status_probe"] == {
        "configured_model_host_uses_balance_probe": True,
        "maximum_balance_probe_calls_in_registered_group": 1,
    }
    assert contract["provider_account_status_probe_policy"]["included_in_sdk_dispatch_ledger"] is False
    assert len(contract["case_criteria_sha256"]) == 64
    assert hashlib.sha256(module._canonical_bytes(contract["case_criteria"])).hexdigest() == (
        contract["case_criteria_sha256"]
    )
    source_freeze_path = (tmp_path / "evidence" / "architecture"
                          / f"{run_id}-source-freeze.safe.json")
    source_freeze = json.loads(source_freeze_path.read_text(encoding="utf-8"))
    assert {
        "backend/app/api/model_configs.py",
        "backend/tests/test_model_configs_api.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261013/i2-case-sdk-binding-03/execution-harness/live_browser_child.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261013/i2-case-sdk-binding-03/execution-harness/live_owned_mysql.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261013/i2-case-sdk-binding-03/execution-harness/registered_i4_run.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261013/i2-case-sdk-binding-03/execution-harness/run_browser_live.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/i2-case-sdk-binding-04/execution-harness/live_browser_child.py",
        "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/i2-case-sdk-binding-04/execution-harness/live_owned_mysql.py",
        "frontend-enterprise/e2e/start_final_f3_live_server.py",
        "frontend-enterprise/e2e/test_final_f3_live_server.py",
        "frontend-enterprise/e2e/test_final_f3_live_harness.py",
        "backend/app/llm/client.py",
        "backend/tests/test_llm_client.py",
        "frontend-enterprise/e2e/final_f3_sdk_ledger.py",
        "frontend-enterprise/e2e/test_final_f3_sdk_ledger.py",
        "frontend-enterprise/e2e/test_i2_case_binding_03_registration.py",
    }.issubset(set(source_freeze["product_delta_scope"]))


@pytest.mark.parametrize("business_labels_present", [True, False])
def test_registered_sop_contract_requires_complete_business_labels(
        tmp_path: Path, monkeypatch, business_labels_present: bool) -> None:
    """attempt前合同校验须拒绝摘要自洽但缺业务标签的SOP诊断合同。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-05"
    architecture = tmp_path / "evidence" / "architecture"
    architecture.mkdir(parents=True)
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    source_map = {"candidate.py": "a" * 64}
    freeze = {
        "run_id": run_id,
        "source_map": source_map,
        "entry_count": len(source_map),
        "source_map_sha256": module._fingerprint(source_map),
    }
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    criteria = {"synthetic SOP case": ["normal-completion"]}
    labels = {"journey": "synthetic SOP diagnostic", "dialect": "sqlite"}
    if business_labels_present:
        labels["business"] = dict(module.F3_BUSINESS_LABELS)
    metadata = module._diagnostic_contract_metadata("sop-first-three")
    contract = {
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "source_freeze_path": str(freeze_path.relative_to(tmp_path)),
        "source_map_sha256": freeze["source_map_sha256"],
        "source_map_entry_count": freeze["entry_count"],
        "case_criteria": criteria,
        "case_criteria_sha256": hashlib.sha256(module._canonical_bytes(criteria)).hexdigest(),
        "manual_business_labels": labels,
        "manual_business_labels_sha256": hashlib.sha256(module._canonical_bytes(labels)).hexdigest(),
    }
    contract.update({key: metadata[key] for key in (
        "maximum_model_invocations", "model_dispatch_token_budget",
        "provider_account_status_probe_policy",
        "diagnostic_deadline_enforcement", "diagnostic_deadline_budget_seconds",
        "model_budget_profile", "registered_runner_watchdog_seconds",
        "registered_hard_memory_scope", "registered_runner_output_limits",
        "private_tmpfs_preflight_required",
        "private_tmpfs_monitoring_required",
    )})
    contract["provider_account_status_probe"] = {
        "configured_model_host_uses_balance_probe": True,
        "maximum_balance_probe_calls_in_registered_group": 1,
    }
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    invocation = {
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "invocation_count": 1,
        "retry_count": 0,
        "model_hmac_matches_previous_full20": True,
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "source_freeze_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
    }

    accepted = module._registered_artifacts_valid(
        run_id, "sqlite", "sop-first-three", freeze_path, contract_path,
        freeze, contract, invocation,
    )

    assert accepted is business_labels_present


def test_bounded_artifact_sha256_rejects_oversized_linked_or_growing_files(
        tmp_path: Path, monkeypatch) -> None:
    """父级回执只哈希有界、稳定的普通文件，拒绝超限、链接和增长文件。"""
    module = _registration_module()
    accepted = tmp_path / "result.safe.json"
    accepted.write_bytes(b"{\"ok\":true}")
    assert module._bounded_artifact_sha256(accepted, maximum_bytes=64) == hashlib.sha256(
        b"{\"ok\":true}"
    ).hexdigest()

    oversized = tmp_path / "oversized.safe.json"
    oversized.write_bytes(b"x" * 65)
    assert module._bounded_artifact_sha256(oversized, maximum_bytes=64) is None

    linked = tmp_path / "linked.safe.json"
    linked.symlink_to(accepted)
    assert module._bounded_artifact_sha256(linked, maximum_bytes=64) is None

    growing = tmp_path / "growing.safe.json"
    growing.write_bytes(b"{\"ok\":true}")
    original_read = os.read
    did_grow = False

    def read_then_grow(descriptor: int, size: int) -> bytes:
        """在首次有界读取后追加一个字节，模拟并发增长的结果文件。"""
        nonlocal did_grow
        content = original_read(descriptor, size)
        if content and not did_grow:
            with growing.open("ab") as stream:
                stream.write(b" ")
            did_grow = True
        return content

    monkeypatch.setattr(module.os, "read", read_then_grow)
    assert module._bounded_artifact_sha256(growing, maximum_bytes=64) is None


def test_registration_json_artifacts_are_owner_only(tmp_path: Path) -> None:
    """登记合同、源码冻结和调用凭据只能由当前账户读取。"""
    module = _registration_module()
    artifact = tmp_path / "private-registration.safe.json"

    module._write_new_json(artifact, {"synthetic": True})

    assert artifact.stat().st_mode & 0o777 == 0o600


def test_bounded_artifact_sha256_does_not_block_on_fifo(tmp_path: Path) -> None:
    """归档摘要入口遇到FIFO替换时须立即拒绝，不等待writer连接。"""
    fifo = tmp_path / "result.safe.json"
    os.mkfifo(fifo, 0o600)
    helper = HARNESS / "registered_i4_run.py"
    script = (
        "import importlib.util, sys; "
        "from pathlib import Path; "
        "p=Path(sys.argv[1]); "
        "s=importlib.util.spec_from_file_location('fifo_helper', p); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "r=m._bounded_artifact_sha256(Path(sys.argv[2]), maximum_bytes=64); "
        "print('NONE' if r is None else 'HASH')"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(helper), str(fifo)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, _stderr = child.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=2)
        pytest.fail("bounded artifact hashing blocked while opening FIFO")

    assert child.returncode == 0
    assert stdout.strip() == "NONE"


def test_registered_runner_result_summary_has_its_own_byte_limit(monkeypatch) -> None:
    """父回执不得把stdout中的超长结果行反序列化为不受控对象。"""
    module = _registration_module()
    monkeypatch.setattr(module, "REGISTERED_RUNNER_REPORTED_RESULT_MAX_BYTES", 24)

    assert module._bounded_runner_result(b'{"ok":true}\n') == {"ok": True}
    assert module._bounded_runner_result(b'{"payload":"' + b"x" * 32 + b'"}\n') == {
        "safe_runner_json_present": False,
        "safe_runner_json_rejected": "byte_limit",
    }
    assert module._bounded_runner_result(b"[]\n") == {"safe_runner_json_present": False}


def test_parent_receipt_budget_and_serialized_receipt_are_bounded() -> None:
    """父级receipt逾期必须可判为失败，最终JSON必须服从固定字节上限。"""
    module = _registration_module()

    assert not module._parent_receipt_budget_exceeded(
        1_000_000_000, budget_seconds=1, now_ns=1_999_999_999,
    )
    assert module._parent_receipt_budget_exceeded(
        1_000_000_000, budget_seconds=1, now_ns=2_000_000_001,
    )
    assert module._bounded_receipt_bytes({"ok": True}, maximum_bytes=64).endswith(b"\n")
    with pytest.raises(RuntimeError, match="I4_EXECUTION_RECEIPT_BYTE_LIMIT"):
        module._bounded_receipt_bytes({"payload": "x" * 64}, maximum_bytes=64)


def test_sop_first_three_contract_freezes_dispatch_caps_and_deadline(monkeypatch) -> None:
    """三案实际派发前必须冻结SDK调用、累计token和整轮单调总截止。"""
    registration = _registration_module()
    runner = _browser_runner()
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: 10_000_000_000)
    metadata = registration._diagnostic_contract_metadata("sop-first-three")

    assert metadata["maximum_model_invocations"] == 48
    assert metadata["model_dispatch_token_budget"] == {
        "maximum_prompt_tokens_per_call": 128000,
        "maximum_prompt_tokens_total": 4096000,
        "prompt_token_counting": "backend.app.llm.client._request_tokens",
        "prompt_token_counting_kind": (
            "message_estimator_plus_ceil_compact_json_utf8_bytes_divided_by_four_for_other_"
            "sdk_request_fields"
        ),
        "request_metadata_fields_included": True,
        "provider_reported_usage_is_enforced": False,
        "maximum_model_response_body_bytes": 8 * 1024 * 1024,
        "model_response_body_limit_enforcement": (
            "backend.app.llm.client._install_model_response_body_limit_raw_and_decoded_streams"
        ),
        "identity_content_encoding_requested": True,
        "non_identity_content_encoding_rejected_before_decode": True,
        "maximum_completion_tokens_per_call": 8192,
        "maximum_completion_tokens_total": 262144,
        "reservation_persisted_before_final_sdk_dispatch": True,
        "budget_survives_application_restart": True,
        "model_catalog_requests_budgeted": False,
    }
    assert metadata["diagnostic_deadline_enforcement"] == {
        "total_from_runner_entry_seconds": 3345,
        "covers_setup_cases_cleanup_and_archive": True,
        "outer_watchdog_is_emergency_reap_only": True,
    }
    assert metadata["diagnostic_deadline_budget_seconds"] == {
        "active_from_runner_entry_including_setup_and_three_cases": 3000,
        "after_each": 180,
        "browser_and_context_fixture_close": 30,
        "webserver_shutdown": 30,
        "application_and_provider_cleanup": 45,
        "parent_mysql_cleanup": 0,
        "parent_evidence_archive": 60,
        "total_from_runner_entry": 3345,
    }
    assert metadata["model_budget_profile"] == "i2-sop-first-three-v1"
    assert metadata["provider_account_status_probe_policy"] == {
        "kind": "conditional_provider_account_balance",
        "condition": "configured_model_host_uses_supported_balance_probe",
        "request_method": "GET",
        "request_path": "/user/balance",
        "maximum_calls_per_model_config_test": 1,
        "httpx_timeout_seconds": 15,
        "timeout_is_whole_request_wall_clock": False,
        "maximum_accepted_response_body_bytes": 64 * 1024,
        "maximum_stream_read_bytes": 64 * 1024 + 1,
        "maximum_overread_probe_bytes": 1,
        "response_encoding_requested": "identity",
        "included_in_sdk_dispatch_ledger": False,
        "body_limit_enforcement": (
            "backend.app.api.model_configs._read_bounded_deepseek_balance_body"
        ),
    }
    assert metadata["registered_hard_memory_scope"] == {
        "manager": "systemd-user-transient-scope",
        "unit_kind": "scope",
        "memory_max_bytes": 2 * 1024**3,
        "memory_swap_max_bytes": 0,
        "oom_policy": "kill",
        "memory_oom_group": True,
        "required_before_attempt_claim": True,
        "registration_parent_outside_scope": True,
        "runner_and_descendants_share_scope": True,
        "sampled_application_memory_budget_kib_remains": 1_572_864,
    }
    assert metadata["private_tmpfs_preflight_required"] is True
    assert metadata["private_tmpfs_monitoring_required"] is True
    assert metadata["registered_runner_output_limits"] == {
        "stdout_max_bytes": 1024 * 1024,
        "stderr_max_bytes": 8 * 1024 * 1024,
        "runner_reported_result_max_bytes": 8 * 1024,
        "result_file_sha256_max_bytes": 64 * 1024,
        "runner_entry_claim_sha256_max_bytes": 16 * 1024,
        "execution_receipt_max_bytes": 64 * 1024,
        "parent_receipt_archive_budget_seconds": 60,
        "parent_receipt_budget_is_soft_acceptance_gate": True,
        "parent_receipt_budget_scope": (
            "starts_after_runner_exit_and_covers_private_log_write_and_bounded_evidence_"
            "collection_through_prewrite_check"
        ),
        "parent_receipt_budget_is_hard_watchdog": False,
        "drained_after_limit_until_owned_runner_reaped": True,
    }
    schedule = runner.diagnostic_deadline_schedule("sop-first-three", 10_000_000_000)
    assert schedule["active_deadline_ns"] == 10_000_000_000 + 3000_000_000_000
    assert schedule["after_each_deadline_ns"] == 10_000_000_000 + 3180_000_000_000
    assert schedule["fixture_deadline_ns"] == 10_000_000_000 + 3210_000_000_000
    assert schedule["child_term_deadline_ns"] == 10_000_000_000 + 3240_000_000_000
    assert schedule["child_hard_deadline_ns"] == 10_000_000_000 + 3285_000_000_000
    assert schedule["archive_deadline_ns"] == 10_000_000_000 + 3345_000_000_000
    assert runner.playwright_case_timeout_ms(
        "sop-first-three", schedule["active_deadline_ns"] - 1_000_000_000,
    ) == 900_000
    frozen = {
        "maximum_model_invocations": metadata["maximum_model_invocations"],
        "model_dispatch_token_budget": metadata["model_dispatch_token_budget"],
        "provider_account_status_probe_policy": metadata[
            "provider_account_status_probe_policy"
        ],
        "provider_account_status_probe": {
            "configured_model_host_uses_balance_probe": True,
            "maximum_balance_probe_calls_in_registered_group": 1,
        },
    }
    assert registration._diagnostic_model_budget_contract_valid(frozen, "sop-first-three")
    frozen["maximum_model_invocations"] += 1
    assert not registration._diagnostic_model_budget_contract_valid(frozen, "sop-first-three")


def test_account_probe_contract_binds_only_a_safe_applicability_bit() -> None:
    """模型配置域名命中DeepSeek账户接口时单列一次旁路HTTP额度且不保存端点。"""
    registration = _registration_module()

    applicable = registration._provider_account_status_probe_contract(
        {"base_url": "https://api.deepseek.com/v1"}, "sop-first-three",
    )
    not_applicable = registration._provider_account_status_probe_contract(
        {"base_url": "https://models.example.org/v1"}, "sop-first-three",
    )

    assert applicable == {
        "configured_model_host_uses_balance_probe": True,
        "maximum_balance_probe_calls_in_registered_group": 1,
    }
    assert not_applicable == {
        "configured_model_host_uses_balance_probe": False,
        "maximum_balance_probe_calls_in_registered_group": 0,
    }
    assert "api.deepseek.com" not in json.dumps(applicable)


def test_registered_runner_output_capture_is_bounded_and_reaped(tmp_path: Path, monkeypatch) -> None:
    """登记器runner stdout/stderr超过硬上限时停止自有子树并保留定长输出。"""
    module = _registration_module()
    monkeypatch.setattr(module, "REGISTERED_RUNNER_STDOUT_MAX_BYTES", 4096, raising=False)
    monkeypatch.setattr(module, "REGISTERED_RUNNER_STDERR_MAX_BYTES", 8192, raising=False)
    monkeypatch.setattr(module, "_signal_private_browser_group", lambda *_args: False)

    completed, expired, watchdog = module._run_bounded_diagnostic_child(
        [module.sys.executable, "-c",
         "import os; os.write(1, b'x'*65536); os.write(2, b'y'*65536); import time; time.sleep(30)"],
        os.environ.copy(), tmp_path, "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03",
        watchdog_seconds=5,
    )

    assert expired is True
    assert completed.returncode != 0
    assert len(completed.stdout) <= 4096
    assert len(completed.stderr) <= 8192
    assert watchdog["runner_reaped"] is True
    assert watchdog["output_limit_exceeded"] is True
    assert watchdog["owned_browser_group_term_sent"] is False
    assert "SYSTEMD_SCOPE_STOP" in watchdog["signals_sent_to_runner"]


def test_registered_runner_watchdog_uses_monotonic_timeout_and_reaps_child(
        tmp_path: Path, monkeypatch) -> None:
    """登记器总看门狗到期后只终止并回收本轮runner。"""
    module = _registration_module()
    monkeypatch.setattr(module, "_signal_private_browser_group", lambda *_args: False)

    completed, expired, watchdog = module._run_bounded_diagnostic_child(
        [module.sys.executable, "-c", "import time; time.sleep(30)"],
        os.environ.copy(), tmp_path,
        "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03",
        watchdog_seconds=1,
    )

    assert expired is True
    assert completed.returncode == 124
    assert watchdog["wrapper_watchdog_expired"] is True
    assert watchdog["output_limit_exceeded"] is False
    assert watchdog["runner_reaped"] is True


@pytest.mark.parametrize(("field", "value"), [
    ("manager_active_state", "inactive"),
    ("manager_memory_max", "infinity"),
    ("manager_memory_max", "1073741824"),
    ("manager_memory_swap_max", "infinity"),
    ("manager_oom_policy", "continue"),
    ("manager_control_group", ""),
    ("process_cgroup", "0::/user.slice/unrelated.scope"),
    ("process_uid", -1),
    ("cgroup_memory_max", "max"),
    ("cgroup_memory_swap_max", "max"),
    ("cgroup_oom_group", "0"),
    ("parent_cgroup", "0::/user.slice/user-1000.slice/f3-i2-test.scope"),
])
def test_registered_scope_requires_exact_hard_memory_and_parent_separation(
        field: str, value: str | int) -> None:
    """硬内存预检必须同时绑定systemd属性、实际cgroup文件和外部登记父进程。"""
    module = _registration_module()
    expected = {
        "expected_unit": "f3-i2-test.scope",
        "expected_pid": 7654,
        "expected_uid": os.getuid(),
        "expected_memory_max_bytes": 2 * 1024**3,
        "manager_active_state": "active",
        "manager_memory_max": str(2 * 1024**3),
        "manager_memory_swap_max": "0",
        "manager_oom_policy": "kill",
        "manager_control_group": "/user.slice/user-1000.slice/f3-i2-test.scope",
        "process_cgroup": "0::/user.slice/user-1000.slice/f3-i2-test.scope",
        "process_uid": os.getuid(),
        "parent_cgroup": "0::/user.slice/user-1000.slice/session.scope",
        "cgroup_memory_max": str(2 * 1024**3),
        "cgroup_memory_swap_max": "0",
        "cgroup_oom_group": "1",
    }
    expected[field] = value

    assert module._registered_scope_observation_valid(**expected) is False


def test_registered_scope_accepts_only_exact_verified_observation() -> None:
    """登记前仅接受执行进程实际位于2GiB OOM组且父进程在组外的观测。"""
    module = _registration_module()

    assert module._registered_scope_observation_valid(
        expected_unit="f3-i2-test.scope",
        expected_pid=7654,
        expected_uid=os.getuid(),
        expected_memory_max_bytes=2 * 1024**3,
        manager_active_state="active",
        manager_memory_max=str(2 * 1024**3),
        manager_memory_swap_max="0",
        manager_oom_policy="kill",
        manager_control_group="/user.slice/user-1000.slice/f3-i2-test.scope",
        process_cgroup="0::/user.slice/user-1000.slice/f3-i2-test.scope",
        process_uid=os.getuid(),
        parent_cgroup="0::/user.slice/user-1000.slice/session.scope",
        cgroup_memory_max=str(2 * 1024**3),
        cgroup_memory_swap_max="0",
        cgroup_oom_group="1",
    ) is True


def _configure_fake_scope_preparation(tmp_path: Path, monkeypatch):
    """用pipe和内存cgroup数据配置不会启动systemd或操作系统child的scope预检。"""
    module = _registration_module()
    systemd_run = tmp_path / "systemd-run"
    systemctl = tmp_path / "systemctl"
    systemd_run.touch()
    systemctl.touch()
    cgroup = tmp_path / "fake-cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text(str(2 * 1024**3), encoding="ascii")
    (cgroup / "memory.swap.max").write_text("0", encoding="ascii")
    (cgroup / "memory.oom.group").write_text("1", encoding="ascii")
    (cgroup / "memory.events").write_text("oom 0\noom_kill 0\n", encoding="ascii")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "REGISTERED_SCOPE_SYSTEMD_RUN", systemd_run)
    monkeypatch.setattr(module, "REGISTERED_SCOPE_SYSTEMCTL", systemctl)
    monkeypatch.setattr(module, "_scope_cgroup_directory", lambda _value: cgroup)
    monkeypatch.setattr(module, "_scope_oom_detected", lambda _before, _after: False)
    monkeypatch.setattr(
        module, "_read_scope_memory_events", lambda *_args, **_kwargs: {
            "oom": 0, "oom_kill": 0,
        },
    )
    control_group = "/user.slice/user-1000.slice/f3-i2-fake.scope"
    monkeypatch.setattr(module, "_scope_systemd_properties", lambda _unit: {
        "ActiveState": "active",
        "ControlGroup": control_group,
        "MemoryMax": str(2 * 1024**3),
        "MemorySwapMax": "0",
        "OOMPolicy": "kill",
    })
    fake_pid = 987654321
    monkeypatch.setattr(
        module, "_process_cgroup",
        lambda pid: (f"0::{control_group}" if pid == fake_pid
                     else "0::/user.slice/user-1000.slice/session.scope"),
    )
    monkeypatch.setattr(module, "_process_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(module, "_registered_scope_observation_valid", lambda **_kwargs: True)
    return module, fake_pid


def test_scope_prepare_persists_identity_before_start_and_reports_verified_ready(
    tmp_path: Path, monkeypatch,
) -> None:
    """启动helper先通知可恢复unit身份，ready回调不包含释放nonce。"""
    module, fake_pid = _configure_fake_scope_preparation(tmp_path, monkeypatch)
    real_popen = module.subprocess.Popen
    events: list[dict[str, object]] = []

    class FakePopen(real_popen):
        """仅提供选择器握手所需pipe接口，不创建操作系统进程。"""

        def __init__(self, command, **_kwargs):
            """断言scope intent已登记，再通过pipe提供合成ready记录。"""
            assert events[0]["phase"] == "unit_reserved"
            self._child_created = False
            self.pid = fake_pid
            self.returncode = None
            self.stdin = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            self.stderr = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            read_fd, write_fd = os.pipe()
            unit = next(part.removeprefix("--unit=") for part in command
                        if part.startswith("--unit=")) + ".scope"
            ready = {
                "scope_gate_ready": True, "nonce": command[command.index("_scope_gate") + 1],
                "pid": fake_pid, "uid": os.getuid(), "cgroup": f"0::{control_group}",
            }
            os.write(write_fd, (json.dumps(ready, separators=(",", ":")) + "\n").encode())
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.args = command
            self.unit = unit

        def poll(self):
            """报告合成待命进程仍在等待父级GO。"""
            return self.returncode

    control_group = "/user.slice/user-1000.slice/f3-i2-fake.scope"
    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)

    scope = module._prepare_registered_scope(
        [str(tmp_path / "runner.py"), "synthetic"], {}, "synthetic-run",
        memory_max_bytes=2 * 1024**3,
        scope_lease_observer=events.append,
    )

    assert [event["phase"] for event in events] == [
        "unit_reserved", "scope_process_started", "scope_cgroup_discovered", "scope_prepared",
    ]
    assert all("nonce" not in event for event in events)
    assert scope["policy_verified"] is True
    os.close(scope["memory_events_fd"])
    for stream in (scope["process"].stdin, scope["process"].stdout, scope["process"].stderr):
        stream.close()


def test_scope_prepare_observer_failure_stops_and_reports_partial_child(
    tmp_path: Path, monkeypatch,
) -> None:
    """启动后lease写入失败会触发scope停止、进程回收与失败lease报告。"""
    module, fake_pid = _configure_fake_scope_preparation(tmp_path, monkeypatch)
    real_popen = module.subprocess.Popen
    events: list[dict[str, object]] = []
    recovery_calls: list[str] = []

    class FakePopen(real_popen):
        """只生成测试管道对象以证明partial-start回收不依赖systemd。"""

        def __init__(self, command, **_kwargs):
            """提供有效pipe并记录合成待命child身份。"""
            self._child_created = False
            self.pid = fake_pid
            self.returncode = None
            self.stdin = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            self.stderr = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            read_fd, write_fd = os.pipe()
            ready = {
                "scope_gate_ready": True,
                "nonce": command[command.index("_scope_gate") + 1],
                "pid": fake_pid,
                "uid": os.getuid(),
                "cgroup": f"0::{control_group}",
            }
            os.write(write_fd, (json.dumps(ready, separators=(",", ":")) + "\n").encode())
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.args = command

        def poll(self):
            """模拟stop前尚未退出的scope gate。"""
            return self.returncode

        def wait(self, timeout=None):
            """模拟SIGKILL后child完成回收。"""
            self.returncode = -9
            return self.returncode

    control_group = "/user.slice/user-1000.slice/f3-i2-fake.scope"
    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.os, "killpg", lambda *_args: recovery_calls.append("kill"))
    monkeypatch.setattr(module, "_stop_registered_scope", lambda *_args: {
        "scope_stop_requested": True,
        "scope_cleanup_complete": True,
        "scope_control_group_empty": True,
    })
    observer_calls = 0

    def fail_after_spawn(record: dict[str, object]) -> None:
        """在记录scope身份后仅对首次started更新注入私有写入失败。"""
        nonlocal observer_calls
        observer_calls += 1
        events.append(record)
        if record["phase"] == "scope_process_started":
            raise OSError("synthetic durable lease update failure")

    with pytest.raises(OSError, match="durable lease update failure"):
        module._prepare_registered_scope(
            [str(tmp_path / "runner.py"), "synthetic"], {}, "synthetic-run",
            memory_max_bytes=2 * 1024**3,
            scope_lease_observer=fail_after_spawn,
        )

    assert events[0]["phase"] == "unit_reserved"
    assert events[-1]["phase"] == "scope_prepare_failed"
    assert events[-1]["cleanup"]["scope_cleanup_complete"] is True
    assert events[-1]["cleanup"]["runner_reaped"] is True
    assert recovery_calls == ["kill"]


@pytest.mark.parametrize("fail_before_selector_close", [False, True])
def test_scope_prepare_selector_close_failure_stops_before_return(
    tmp_path: Path, monkeypatch, fail_before_selector_close: bool,
) -> None:
    """selector成功关闭异常或此前准备错误时都须走回收路径并关闭FD。"""
    module, fake_pid = _configure_fake_scope_preparation(tmp_path, monkeypatch)
    real_popen = module.subprocess.Popen
    real_selector = module.selectors.DefaultSelector
    events: list[dict[str, object]] = []
    killed: list[int] = []
    tracked_memory_fds: list[int] = []
    selectors_created: list[object] = []
    selector_fds: list[int] = []

    class CloseFailingSelector:
        """第一次关闭模拟系统调用错误，第二次允许异常路径回收内部FD。"""

        def __init__(self) -> None:
            """创建只服务于pipe握手的真实selector后端。"""
            self.delegate = real_selector()
            self.close_attempts = 0
            selectors_created.append(self)

        def register(self, *args: object, **kwargs: object) -> object:
            """将登记的stdout pipe转交给隔离的selector后端。"""
            result = self.delegate.register(*args, **kwargs)
            selector_fds.append(self.delegate.fileno())
            return result

        def select(self, *args: object, **kwargs: object) -> object:
            """从合成ready pipe读取待命scope身份。"""
            return self.delegate.select(*args, **kwargs)

        def close(self) -> None:
            """注入一次close失败，随后在清理重试时真正关闭selector。"""
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise OSError("synthetic selector close failure")
            self.delegate.close()

    class FakePopen(real_popen):
        """仅提供握手pipe和可回收状态，不创建系统进程。"""

        def __init__(self, command, **_kwargs):
            """写入匹配本次nonce的ready行，模拟scope gate待命。"""
            self.pid = fake_pid
            self.returncode = None
            self.stdin = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            self.stderr = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
            read_fd, write_fd = os.pipe()
            control_group = "/user.slice/user-1000.slice/f3-i2-fake.scope"
            ready = {
                "scope_gate_ready": True,
                "nonce": command[command.index("_scope_gate") + 1],
                "pid": fake_pid,
                "uid": os.getuid(),
                "cgroup": f"0::{control_group}",
            }
            os.write(write_fd, (json.dumps(ready, separators=(",", ":")) + "\n").encode())
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.args = command

        def poll(self) -> int | None:
            """报告伪进程只在清理路径中退出。"""
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            """确认测试回收器已终止并回收伪子进程。"""
            self.returncode = -9
            return self.returncode

    def terminate(process: FakePopen, unit: str) -> dict[str, object]:
        """替代systemd清理并关闭三个pipe，验证异常到达兜底回收器。"""
        killed.append(process.pid)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        return {
            "control_group": "/user.slice/user-1000.slice/f3-i2-fake.scope",
            "scope_stop_requested": True,
            "scope_cleanup_complete": True,
            "scope_control_group_empty": True,
            "runner_reaped": process.poll() is not None,
            "descriptors_closed": True,
            "unit": unit,
        }

    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(module.selectors, "DefaultSelector", CloseFailingSelector)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, "_stop_registered_scope", lambda *_args: {
        "scope_stop_requested": True,
        "scope_cleanup_complete": True,
        "scope_control_group_empty": True,
    })
    monkeypatch.setattr(module, "_terminate_unverified_scope", terminate)
    if fail_before_selector_close:
        monkeypatch.setattr(
            module, "_scope_cgroup_directory",
            lambda _control_group: (_ for _ in ()).throw(OSError("synthetic cgroup read failure")),
        )
    monkeypatch.setattr(
        module, "_read_scope_memory_events",
        lambda _path, *, descriptor=None: (
            tracked_memory_fds.append(descriptor) or {"oom": 0, "oom_kill": 0}
            if descriptor is not None else {"oom": 0, "oom_kill": 0}
        ),
    )

    expected_error = (
        "synthetic cgroup read failure" if fail_before_selector_close
        else "synthetic selector close failure"
    )
    with pytest.raises(OSError, match=expected_error):
        module._prepare_registered_scope(
            [str(tmp_path / "runner.py"), "synthetic"], {}, "synthetic-run",
            memory_max_bytes=2 * 1024**3,
            scope_lease_observer=events.append,
        )

    assert events[-1]["phase"] == "scope_prepare_failed"
    assert events[-1]["cleanup"]["selector_closed"] is True
    assert events[-1]["cleanup"]["memory_events_fd_closed"] is True
    assert killed == [fake_pid]
    assert selectors_created[0].close_attempts == 2
    assert len(tracked_memory_fds) == (0 if fail_before_selector_close else 1)
    assert len(selector_fds) == 1
    for descriptor in (*tracked_memory_fds, *selector_fds):
        with pytest.raises(OSError):
            fcntl.fcntl(descriptor, fcntl.F_GETFD)


@pytest.mark.skipif(shutil.which("systemd-run") is None,
                    reason="systemd-run is unavailable for the local cgroup integration probe")
def test_registered_hard_scope_covers_python_descendants_and_keeps_registrar_outside(
        tmp_path: Path, monkeypatch) -> None:
    """真实user scope须限制登记child及其后代，并让独立登记进程留在scope外。"""
    module = _registration_module()
    monkeypatch.setattr(module, "_signal_private_browser_group", lambda *_args: False)
    child = (
        "import json,os,subprocess,sys; "
        "read=lambda pid:open(f'/proc/{pid}/cgroup').read().strip(); "
        "grand=subprocess.run([sys.executable,'-c',"
        "\"import os;print(os.getpid());print(open('/proc/self/cgroup').read().strip())\"],"
        "capture_output=True,text=True,check=True).stdout.splitlines(); "
        "print(json.dumps({'pid':os.getpid(),'cgroup':read('self'),"
        "'grandchild_pid':int(grand[0]),'grandchild_cgroup':grand[1]}),flush=True)"
    )

    completed, expired, watchdog = module._run_bounded_diagnostic_child(
        [sys.executable, "-c", child], os.environ.copy(), tmp_path,
        "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-scope-test",
        watchdog_seconds=10,
    )

    result = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    assert not expired and completed.returncode == 0
    expected_cgroup = f"0::{watchdog['scope_cgroup']}"
    assert result["cgroup"] == expected_cgroup
    assert result["grandchild_cgroup"] == expected_cgroup
    assert watchdog["hard_memory_scope_verified"] is True
    assert watchdog["scope_memory_max_bytes"] == 2 * 1024**3
    assert watchdog["scope_memory_oom_group"] is True
    assert watchdog["registration_parent_outside_scope"] is True
    assert watchdog["scope_cleanup_complete"] is True


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="Node.js is unavailable for the local Playwright scope probe")
def test_registered_scope_covers_provider_launcher_and_real_playwright_browser(
        tmp_path: Path, monkeypatch) -> None:
    """真实业务子进程启动器与Playwright/Chromium浏览器均须继承登记scope。"""
    module = _registration_module()
    harness_path = HARNESS.as_posix()
    frontend_path = (ROOT / "frontend-enterprise/node_modules/playwright").as_posix()
    browser_store = Path(
        "/home/zhengqi/.local/state/gongge-xuban-final-f1-20261003/browser"
    )
    assert browser_store.is_dir()
    node = shutil.which("node")
    assert node is not None
    node_script = f"""
const fs = require('fs');
const {{ chromium }} = require({frontend_path!r});
function cgroup(pid) {{ return fs.readFileSync(`/proc/${{pid}}/cgroup`, 'utf8').trim(); }}
function browserDescendants(rootPid) {{
  const rows = new Map();
  for (const value of fs.readdirSync('/proc')) {{
    if (!/^\\d+$/.test(value)) continue;
    try {{
      const tail = fs.readFileSync(`/proc/${{value}}/stat`, 'utf8').split(')').slice(1).join(')');
      const fields = tail.trim().split(/\\s+/);
      const pid = Number(value);
      const ppid = Number(fields[1]);
      const cmdline = fs.readFileSync(`/proc/${{value}}/cmdline`).toString().replace(/\\0/g, ' ');
      if (Number.isInteger(ppid)) rows.set(pid, {{pid, ppid, cmdline, cgroup: cgroup(pid)}});
    }} catch (_error) {{}}
  }}
  const pending = [rootPid];
  const descendants = [];
  while (pending.length) {{
    const parent = pending.shift();
    for (const row of rows.values()) {{
      if (row.ppid === parent) {{ descendants.push(row); pending.push(row.pid); }}
    }}
  }}
  return descendants.filter(row => /chrome|chromium/i.test(row.cmdline));
}}
(async () => {{
  const browser = await chromium.launch({{headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']}});
  const page = await browser.newPage();
  await page.goto('data:text/html,<title>scope-probe</title><main>offline</main>');
  await new Promise(resolve => setTimeout(resolve, 150));
  const result = {{nodePid: process.pid, nodeCgroup: cgroup(process.pid),
    pageTitle: await page.title(), browserProcesses: browserDescendants(process.pid)}};
  console.log(JSON.stringify(result));
  await browser.close();
}})().catch(error => {{ console.error(error.name); process.exitCode = 1; }});
"""
    child_code = f"""
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {harness_path!r})
import live_browser_child
import run_browser_live
import registered_i4_run
owned = Path({str(tmp_path / 'owned')!r})
destination = Path({str(tmp_path / 'browser-result')!r})
owned.mkdir(mode=0o700)
destination.mkdir(mode=0o700)
environment = os.environ.copy()
control_group = Path('/proc/self/cgroup').read_text().strip().removeprefix('0::')
scope_cgroup = Path('/sys/fs/cgroup') / control_group.lstrip('/')
scope_attempt = {{
    'hard_memory_scope_verified': True,
    'systemd_scope_unit': Path(control_group).name,
    'systemd_scope_control_group': control_group,
    'systemd_scope_memory_max_bytes': int((scope_cgroup / 'memory.max').read_text()),
    'systemd_scope_memory_swap_max_bytes': 0,
    'systemd_scope_memory_oom_group': True,
}}
runtime_scope = registered_i4_run.validate_registered_runtime_scope(scope_attempt)
environment['PLAYWRIGHT_BROWSERS_PATH'] = {str(browser_store)!r}
environment['F3_LIVE_RUN_ID'] = 'i2-scope-browser-probe'
provider_code = "import json,os; print(json.dumps({{'pid':os.getpid(),'cgroup':open('/proc/self/cgroup').read().strip()}}))"
provider = live_browser_child.launch_active_process(
    'provider_start', [sys.executable, '-c', provider_code], environment, owned,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
provider_out, provider_err = provider.communicate(timeout=5)
if provider.returncode != 0: raise SystemExit('PROVIDER_SCOPE_PROBE_FAILED')
provider_result = json.loads(provider_out)
environment['F3_LIVE_RUN_ID'] = 'i2-scope-playwright-probe'
status = run_browser_live.bounded_child(
    [{node!r}, '-e', {node_script!r}], environment, owned, destination,
    maximum_runtime_seconds=25)
browser_result = json.loads((owned / 'browser.private.log').read_text().splitlines()[-1])
print(json.dumps({{'provider':provider_result,'browser':browser_result,
                  'runtime_scope':runtime_scope,'bounded_child_exit':status}}))
"""
    completed, expired, watchdog = module._run_bounded_diagnostic_child(
        [sys.executable, "-c", child_code], os.environ.copy(), tmp_path,
        "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-browser-scope-probe",
        watchdog_seconds=40,
    )

    assert not expired and completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    expected_cgroup = f"0::{watchdog['scope_cgroup']}"
    assert result["provider"]["cgroup"] == expected_cgroup
    assert result["browser"]["nodeCgroup"] == expected_cgroup
    assert result["browser"]["pageTitle"] == "scope-probe"
    assert result["browser"]["browserProcesses"]
    assert all(row["cgroup"] == expected_cgroup
               for row in result["browser"]["browserProcesses"])
    assert result["bounded_child_exit"] == 0
    assert result["runtime_scope"]["verified"] is True
    assert watchdog["hard_memory_scope_verified"] is True
    assert watchdog["scope_cleanup_complete"] is True


def test_scope_preflight_failure_does_not_consume_attempt_or_dispatch(
        tmp_path: Path, monkeypatch) -> None:
    """systemd限额或cgroup属性预检失败必须在attempt和正式runner派发前停止。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-scope-preflight-fail"
    runner_script = tmp_path / "runner.py"
    runner_script.write_text("raise SystemExit(99)\n", encoding="utf-8")
    architecture = _configure_synthetic_registered_run(
        module, tmp_path, monkeypatch, run_id, runner_script,
    )
    preflight_calls = []

    def reject_scope(*_args, **_kwargs):
        """在正式attempt前模拟systemd未提供冻结的硬内存边界。"""
        preflight_calls.append("checked")
        raise RuntimeError("I4_REGISTERED_HARD_MEMORY_SCOPE_UNVERIFIED")

    monkeypatch.setattr(module, "_prepare_registered_scope", reject_scope)

    with pytest.raises(RuntimeError, match="I4_REGISTERED_HARD_MEMORY_SCOPE_UNVERIFIED"):
        module.run_registered(run_id, "sqlite", "sop-first-three")

    assert preflight_calls == ["checked"]
    assert not (architecture / f"{run_id}-attempt.safe.json").exists()
    assert not (architecture / f"{run_id}-execution-receipt.safe.json").exists()
    assert not (module.EVIDENCE / run_id).exists()


@pytest.mark.skipif(shutil.which("systemd-run") is None,
                    reason="systemd-run is unavailable for the local cgroup OOM integration probe")
def test_scoped_oom_persists_one_failure_receipt_without_retry_or_credit(
        tmp_path: Path, monkeypatch) -> None:
    """低限额合成OOM须只杀scope，登记父级保存失败receipt且重入不重派。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-scope-oom"
    runner_script = tmp_path / "oom_runner.py"
    runner_script.write_text(
        "import mmap,time\n"
        "mapping=mmap.mmap(-1,512*1024*1024)\n"
        "print('synthetic-scope-oom-started',flush=True)\n"
        "for offset in range(0,len(mapping),4096):\n"
        "    mapping[offset]=1\n"
        "    if offset % (1024*1024) == 0: time.sleep(0.005)\n",
        encoding="utf-8",
    )
    architecture = _configure_synthetic_registered_run(
        module, tmp_path, monkeypatch, run_id, runner_script,
    )
    monkeypatch.setattr(module, "REGISTERED_SCOPE_MEMORY_MAX_BYTES", 96 * 1024**2)
    prepare_calls = []
    real_prepare = module._prepare_registered_scope

    def count_scope_preparations(*args, **kwargs):
        """记录唯一硬scope启动次数，同时保留真实systemd及cgroup检查。"""
        prepare_calls.append("prepared")
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(module, "_prepare_registered_scope", count_scope_preparations)

    exit_code = module.run_registered(run_id, "sqlite", "sop-first-three")

    attempt = architecture / f"{run_id}-attempt.safe.json"
    receipt_path = architecture / f"{run_id}-execution-receipt.safe.json"
    claim = json.loads(attempt.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert claim["invocation_count"] == 1 and claim["retry_count"] == 0
    assert claim["hard_memory_scope_verified"] is True
    assert receipt["acceptance_credit"] is False
    watchdog = receipt["registered_wrapper_watchdog"]
    assert watchdog["hard_memory_scope_verified"] is True
    assert watchdog["scope_memory_max_bytes"] == 96 * 1024**2
    assert watchdog["scope_memory_swap_max_bytes"] == 0
    assert watchdog["runner_terminated_by_signal"] is True
    assert watchdog["runner_child_returncode"] == -9
    assert watchdog["scope_oom_detected"] is None
    assert watchdog["scope_ancestor_oom_event_increased"] is True
    assert watchdog["scope_cleanup_complete"] is True
    assert prepare_calls == ["prepared"]

    with pytest.raises(RuntimeError, match="I4_REGISTERED_ATTEMPT_ALREADY_CLAIMED"):
        module.run_registered(run_id, "sqlite", "sop-first-three")
    assert prepare_calls == ["prepared"]


def test_registered_runner_output_drain_start_failure_is_archived_after_reap(
        tmp_path: Path, monkeypatch) -> None:
    """登记器输出线程无法启动时停止runner并返回可归档的固定失败状态。"""
    module = _registration_module()
    children = []

    class ThreadStartFailure:
        """在runner启动后模拟资源耗尽导致的drainer创建失败。"""

        def __init__(self, **_kwargs):
            """不保留runner输出，仅用于触发线程创建错误。"""

        def start(self):
            """确定性注入线程启动失败。"""
            raise RuntimeError("SYNTHETIC_REGISTERED_DRAIN_START_FAILURE")

    real_prepare_scope = module._prepare_registered_scope

    def capture_scope(*args, **kwargs):
        """使用真实systemd硬作用域并记录其中的单次测试runner。"""
        scope = real_prepare_scope(*args, **kwargs)
        children.append(scope["process"])
        return scope

    monkeypatch.setattr(module, "_prepare_registered_scope", capture_scope)
    monkeypatch.setattr(module.threading, "Thread", ThreadStartFailure)
    monkeypatch.setattr(module, "_signal_private_browser_group", lambda *_args: False)

    completed, expired, watchdog = module._run_bounded_diagnostic_child(
        [module.sys.executable, "-c", "import time; time.sleep(30)"],
        os.environ.copy(), tmp_path,
        "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03",
        watchdog_seconds=5,
    )

    assert expired is True
    assert completed.returncode == 125
    assert watchdog["output_drain_start_failed"] is True
    assert watchdog["runner_reaped"] is True
    assert len(children) == 1 and children[0].poll() is not None


def test_registered_run_refuses_second_run_id_while_global_heavy_lock_is_held(
        tmp_path: Path, monkeypatch) -> None:
    """不同run id共享一个非阻塞重任务锁，锁占用时attempt和源码扫描都不开始。"""
    module = _registration_module()
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(module, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path, raising=False)
    monkeypatch.setattr(
        module, "_source_map",
        lambda: (_ for _ in ()).throw(AssertionError("global lock must precede source scan")),
    )

    try:
        with pytest.raises(RuntimeError, match="I4_REGISTERED_HEAVY_JOB_ALREADY_RUNNING"):
            module.run_registered(
                "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-88",
                "sqlite", "sop-first-three",
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_registered_heavy_lock_survives_parent_unwind_while_runner_child_lives(
        tmp_path: Path, monkeypatch) -> None:
    """父登记上下文退出后，继承锁FD的活动runner仍阻止另一个run进入扫描。"""
    module = _registration_module()
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    monkeypatch.setattr(module, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path, raising=False)
    monkeypatch.setattr(
        module, "_source_map",
        lambda: (_ for _ in ()).throw(AssertionError("global lock must precede source scan")),
    )
    child = None
    try:
        with module._registered_heavy_job_lock() as descriptor:
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import time; print('LEASE_READY', flush=True); time.sleep(30)"],
                pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "LEASE_READY"

        with pytest.raises(RuntimeError, match="I4_REGISTERED_HEAVY_JOB_ALREADY_RUNNING"):
            module.run_registered(
                "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-87",
                "sqlite", "sop-first-three",
            )
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()


def test_browser_child_inherits_registered_lease_after_runner_is_stopped(
        tmp_path: Path, monkeypatch) -> None:
    """runner异常停止后，仍活动的浏览器工作进程继续持有全局登记租约。"""
    module = _registration_module()
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    owned = tmp_path / "browser-owned"
    destination = tmp_path / "browser-evidence"
    root = tmp_path / "project"
    for directory in (owned, destination, root / "frontend-enterprise"):
        directory.mkdir(mode=0o700, parents=True)
    ready_path = tmp_path / "browser-child-ready.private"
    monkeypatch.setattr(module, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path, raising=False)
    monkeypatch.setattr(
        module, "_source_map",
        lambda: (_ for _ in ()).throw(AssertionError("lease must reject before source scan")),
    )
    runner_script = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(HARNESS)!r})
import run_browser_live as runner
runner.ROOT = Path({str(root)!r})
runner.PRIVATE = Path({str(tmp_path / 'runner-private')!r})
registration = runner._load_registered_i4_module()
registration.REGISTERED_HEAVY_JOB_LOCK_PATH = Path({str(lock_path)!r})
runner._load_registered_i4_module = lambda: registration
child_code = (
    "import os,pathlib,time; fd=int(os.environ['F3_REGISTERED_HEAVY_LOCK_FD']); "
    "info=os.fstat(fd); pathlib.Path({str(ready_path)!r}).write_text("
    "str(os.getpid())+' '+str(info.st_ino)); time.sleep(30)"
)
checks = runner.load(runner.PREVIOUS / "run_checks_bounded.py")
environment = runner._registered_browser_child_environment(
    checks, Path({str(tmp_path / 'browser-owned-environment')!r})
)
environment["PATH"] = os.environ.get("PATH", "")
runner.bounded_child([sys.executable, "-c", child_code], environment,
                     Path({str(owned)!r}), Path({str(destination)!r}))
"""
    active_runner = None
    browser_pid = None
    try:
        with module._registered_heavy_job_lock() as descriptor:
            active_runner = subprocess.Popen(
                [sys.executable, "-c", runner_script],
                env={"PATH": os.environ.get("PATH", ""),
                     "F3_REGISTERED_HEAVY_LOCK_FD": str(descriptor)},
                pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not ready_path.exists():
                if active_runner.poll() is not None:
                    break
                time.sleep(0.05)
            if active_runner.poll() is not None and not ready_path.exists():
                _stdout, stderr = active_runner.communicate(timeout=3)
                raise AssertionError(
                    "registered runner failed before the owned browser child was ready: "
                    + (stderr or "")
                )
            assert ready_path.is_file(), "bounded browser child did not inherit the lease FD"
            browser_pid = int(ready_path.read_text(encoding="utf-8").split()[0])

        with pytest.raises(RuntimeError, match="I4_REGISTERED_HEAVY_JOB_ALREADY_RUNNING"):
            module.run_registered(
                "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-86",
                "sqlite", "sop-first-three",
            )

        active_runner.terminate()
        active_runner.communicate(timeout=3)

        with pytest.raises(RuntimeError, match="I4_REGISTERED_HEAVY_JOB_ALREADY_RUNNING"):
            module.run_registered(
                "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-85",
                "sqlite", "sop-first-three",
            )
    finally:
        if active_runner is not None and active_runner.poll() is None:
            active_runner.terminate()
            try:
                active_runner.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                active_runner.kill()
                active_runner.communicate(timeout=2)
        if browser_pid is None and ready_path.is_file():
            try:
                browser_pid = int(ready_path.read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                browser_pid = None
        if browser_pid is not None:
            try:
                os.killpg(browser_pid, module.signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_owned_worker_inherits_registered_lease_after_browser_supervisor_stops(
        tmp_path: Path, monkeypatch) -> None:
    """provider/Playwright owned worker存活时，其browser supervisor退出仍须保持租约。"""
    registration = _registration_module()
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    worker_ready = tmp_path / "owned-worker-ready.private"
    monkeypatch.setattr(registration, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path,
                        raising=False)
    monkeypatch.setattr(
        registration, "_source_map",
        lambda: (_ for _ in ()).throw(AssertionError("descendant lease must reject before scan")),
    )
    worker_code = f"""
import os, pathlib, time
fd = int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"])
try:
    info = os.fstat(fd)
except OSError:
    pathlib.Path({str(worker_ready)!r}).write_text("fd-closed", encoding="utf-8")
else:
    pathlib.Path({str(worker_ready)!r}).write_text(
        str(os.getpid()) + " " + str(info.st_ino), encoding="utf-8")
time.sleep(30)
"""
    supervisor_code = f"""
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {str(HARNESS)!r})
import live_browser_child as child
worker = child.launch_active_process(
    "provider_start", [sys.executable, "-c", {worker_code!r}],
    dict(os.environ), Path({str(owned)!r}), stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not Path({str(worker_ready)!r}).exists():
    time.sleep(0.02)
print("SUPERVISOR_READY", flush=True)
time.sleep(30)
"""
    supervisor = None
    worker_pid = None
    try:
        with registration._registered_heavy_job_lock(lock_path) as descriptor:
            supervisor_environment = {
                "PATH": os.environ.get("PATH", ""),
                "F3_REGISTERED_HEAVY_LOCK_FD": str(descriptor),
            }
            supervisor = subprocess.Popen(
                [sys.executable, "-c", supervisor_code], env=supervisor_environment,
                pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            assert supervisor.stdout is not None
            assert supervisor.stdout.readline().strip() == "SUPERVISOR_READY"
            assert worker_ready.is_file()
            worker_fields = worker_ready.read_text(encoding="utf-8").split()
            assert worker_fields != ["fd-closed"]
            worker_pid = int(worker_fields[0])

        supervisor.kill()
        supervisor.communicate(timeout=3)
        with pytest.raises(RuntimeError, match="I4_REGISTERED_HEAVY_JOB_ALREADY_RUNNING"):
            registration.run_registered(
                "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-84",
                "sqlite", "sop-first-three",
            )
    finally:
        if supervisor is not None and supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=3)
        if worker_pid is None and worker_ready.is_file():
            try:
                worker_fields = worker_ready.read_text(encoding="utf-8").split()
                if worker_fields != ["fd-closed"]:
                    worker_pid = int(worker_fields[0])
            except (OSError, ValueError, IndexError):
                worker_pid = None
        if worker_pid is not None:
            try:
                os.kill(worker_pid, registration.signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_live_browser_child_rejects_unregistered_entry_before_starting_provider(
        tmp_path: Path, monkeypatch) -> None:
    """直接调用live browser子runner缺少同轮claim和租约时须在首个Popen前拒绝。"""
    child = _browser_child()
    owned = tmp_path / "unregistered-owned"
    starts = []
    monkeypatch.setattr(child.os, "umask", lambda _value: 0)
    monkeypatch.setattr(child, "launch_active_process",
                        lambda *args, **kwargs: starts.append((args, kwargs)))
    monkeypatch.setenv("F3_LIVE_OWNED", str(owned))
    monkeypatch.setenv("F3_LIVE_DIALECT", "sqlite")
    monkeypatch.setenv("F3_LIVE_RUN_ID", "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03")
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")
    monkeypatch.delenv("F3_REGISTERED_HEAVY_LOCK_FD", raising=False)

    with pytest.raises(RuntimeError, match="F3_REGISTERED_CHILD_ENTRY_REQUIRED"):
        child.main()

    assert starts == []
    assert not owned.exists()


def test_live_browser_child_rejects_lock_holder_without_attempt_or_runner_claim(
        tmp_path: Path, monkeypatch) -> None:
    """租约FD本身不能授权裸child；缺少本run attempt和entry claim仍须零启动。"""
    child = _browser_child()
    owned = tmp_path / "unclaimed-owned"
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    lock_path = tmp_path / "registered-heavy-job.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    starts = []
    registration = SimpleNamespace(
        EVIDENCE=evidence,
        validate_diagnostic_scope=lambda *_args: None,
    )

    def read_missing_record(_path, error_code):
        """模拟不存在的attempt/claim文件，保持拒绝码与生产安全读取器一致。"""
        raise RuntimeError(error_code)

    runner = SimpleNamespace(
        _load_registered_i4_module=lambda: registration,
        _validate_registered_runner_lock=lambda _registration, values: os.fstat(
            int(values["F3_REGISTERED_HEAVY_LOCK_FD"])
        ),
        _read_registered_runner_record=read_missing_record,
    )
    monkeypatch.setattr(child, "_load_registered_runner_helpers", lambda: runner)
    monkeypatch.setattr(child.os, "umask", lambda _value: 0)
    monkeypatch.setattr(child, "launch_active_process",
                        lambda *args, **kwargs: starts.append((args, kwargs)))
    monkeypatch.setenv("F3_LIVE_OWNED", str(owned))
    monkeypatch.setenv("F3_LIVE_DIALECT", "sqlite")
    monkeypatch.setenv("F3_LIVE_RUN_ID", "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03")
    monkeypatch.setenv("F3_DIAGNOSTIC_SELECTION", "sop-first-three")

    with lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setenv("F3_REGISTERED_HEAVY_LOCK_FD", str(lock_file.fileno()))
        with pytest.raises(RuntimeError, match="F3_REGISTERED_CHILD_ENTRY_REQUIRED"):
            child.main()

    assert starts == []
    assert not owned.exists()


def test_registered_scope_cleans_orphaned_browser_group_after_runner_sigkill(
        tmp_path: Path, monkeypatch) -> None:
    """runner与浏览器leader均SIGKILL后仍存活的group worker被登记器有界回收。"""
    registration = _registration_module()
    private = tmp_path / "private-run"
    private.mkdir(mode=0o700)
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    monkeypatch.setattr(registration, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path,
                        raising=False)
    monkeypatch.setattr(registration, "ROOT", tmp_path)
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-83"
    worker_ready = tmp_path / "orphan-worker-ready.private"
    worker_code = f"""
import os, pathlib, time
fd = int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"])
os.fstat(fd)
pathlib.Path({str(worker_ready)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
"""
    browser_leader_code = f"""
import os, subprocess, sys, time
from pathlib import Path
worker = subprocess.Popen([sys.executable, "-c", {worker_code!r}],
                          env=os.environ.copy(),
                          pass_fds=(int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"]),))
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not Path({str(worker_ready)!r}).exists():
    time.sleep(0.02)
"""
    runner_code = f"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
leader = subprocess.Popen([sys.executable, "-c", {browser_leader_code!r}],
                          start_new_session=True, env=os.environ.copy(),
                          pass_fds=(int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"]),))
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not Path({str(worker_ready)!r}).exists():
    time.sleep(0.02)
fields = (Path("/proc") / str(leader.pid) / "stat").read_text().rsplit(")", 1)[1].split()
uid = next(int(line.partition(":")[2].split()[0])
           for line in (Path("/proc") / str(leader.pid) / "status").read_text().splitlines()
           if line.startswith("Uid:"))
Path({str(private / 'owned-browser-child.private.json')!r}).write_text(json.dumps({{
    "run_id": {run_id!r}, "identity_available": True, "pid": leader.pid,
    "process_group_id": int(fields[2]), "session_id": int(fields[3]),
    "start_ticks": int(fields[19]), "uid": uid,
}}), encoding="utf-8")
os.chmod({str(private / 'owned-browser-child.private.json')!r}, 0o600)
os.kill(leader.pid, signal.SIGKILL)
leader.wait(timeout=2)
os.kill(os.getpid(), signal.SIGKILL)
"""
    worker_pid = None
    try:
        with registration._registered_heavy_job_lock(lock_path) as descriptor:
            environment = os.environ.copy()
            environment["F3_REGISTERED_HEAVY_LOCK_FD"] = str(descriptor)
            completed, interrupted, watchdog = registration._run_bounded_diagnostic_child(
                [sys.executable, "-c", runner_code], environment, private, run_id,
                watchdog_seconds=10, lock_descriptor=descriptor,
            )
            assert completed.returncode != 0
            assert interrupted is True
            assert watchdog["scope_cleanup_complete"] is True
            assert watchdog["owned_browser_group_cleanup_complete"] is True
            assert worker_ready.is_file()
            worker_pid = int(worker_ready.read_text(encoding="utf-8"))
    finally:
        if worker_pid is None and worker_ready.is_file():
            try:
                worker_pid = int(worker_ready.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                worker_pid = None
        if worker_pid is not None:
            try:
                os.kill(worker_pid, registration.signal.SIGKILL)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and Path("/proc", str(worker_pid)).exists():
        try:
            state = Path("/proc", str(worker_pid), "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            break
        if state in {"Z", "X"}:
            break
        time.sleep(0.02)
    if Path("/proc", str(worker_pid)).exists():
        state = Path("/proc", str(worker_pid), "stat").read_text().rsplit(")", 1)[1].split()[0]
        assert state in {"Z", "X"}
    with registration._registered_heavy_job_lock(lock_path):
        pass


def test_browser_supervisor_kill_reaps_live_owned_process_group_descendant(
        tmp_path: Path, monkeypatch) -> None:
    """browser supervisor被SIGKILL后，bounded_child仍定位并限时回收同组worker。"""
    registration = _registration_module()
    runner = _browser_runner()
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    owned, destination = tmp_path / "browser-owned", tmp_path / "browser-evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    worker_ready = tmp_path / "bounded-orphan-worker.private"
    monkeypatch.setattr(registration, "REGISTERED_HEAVY_JOB_LOCK_PATH", lock_path,
                        raising=False)
    worker_code = f"""
import os, pathlib, time
fd = int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"])
os.fstat(fd)
pathlib.Path({str(worker_ready)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
"""
    supervisor_code = f"""
import os, signal, subprocess, sys, time
from pathlib import Path
descriptor = int(os.environ["F3_REGISTERED_HEAVY_LOCK_FD"])
worker = subprocess.Popen([sys.executable, "-c", {worker_code!r}],
                          env=os.environ.copy(), pass_fds=(descriptor,))
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not Path({str(worker_ready)!r}).exists():
    time.sleep(0.02)
os.kill(os.getpid(), signal.SIGKILL)
"""
    worker_pid = None
    try:
        with registration._registered_heavy_job_lock(lock_path) as descriptor:
            result = runner.bounded_child(
                [sys.executable, "-c", supervisor_code],
                {"PATH": os.environ.get("PATH", ""),
                 "F3_REGISTERED_HEAVY_LOCK_FD": str(descriptor),
                 "F3_LIVE_RUN_ID": "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-82"},
                owned, destination, maximum_runtime_seconds=10,
            )
            assert result != 0
            summary = json.loads((destination / "resource-summary.safe.json").read_text())
            assert summary["owned_browser_group_descendants_detected"] is True
            assert summary["owned_browser_group_cleanup_complete"] is True
            assert worker_ready.is_file()
            worker_pid = int(worker_ready.read_text(encoding="utf-8"))
    finally:
        if worker_pid is None and worker_ready.is_file():
            try:
                worker_pid = int(worker_ready.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                worker_pid = None
        if worker_pid is not None:
            try:
                os.kill(worker_pid, registration.signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_registered_runner_drain_failure_is_saved_as_final_receipt_without_retry(
        tmp_path: Path, monkeypatch, request) -> None:
    """输出drainer启动失败仍形成单次attempt和失败receipt，重入不能再次派发。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-89"
    criteria = {f"JRN08 SOP synthetic case {index}": [f"criterion-{index}"]
                for index in range(1, 4)}
    runner = SimpleNamespace(
        PRIVATE=tmp_path / "runner-private",
        EVIDENCE=tmp_path / "runner-evidence",
        PYTHON=Path(sys.executable),
        BUDGET=1_572_864,
        expected_case_criteria=lambda _name: criteria,
        private_tmpfs_preflight=lambda _path: {"valid": True},
    )
    source_map = {"synthetic-source.py": "a" * 64}
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "EVIDENCE", tmp_path / "registry-evidence")
    monkeypatch.setattr(module, "PRIVATE_RUN_LOGS", tmp_path / "private-logs")
    monkeypatch.setattr(
        module, "REGISTERED_HEAVY_JOB_LOCK_PATH",
        tmp_path / "private-state" / "registered-heavy-job.lock", raising=False,
    )
    monkeypatch.setattr(module, "_source_map", lambda: (runner, source_map))
    monkeypatch.setattr(module, "_registered_artifacts_valid", lambda *_args: True)
    monkeypatch.setattr(module, "_model_hmac", lambda: "b" * 64)
    monkeypatch.setattr(module, "_model_payload_sha256", lambda: "c" * 64)
    dispatches = []
    children = []
    runner_script = tmp_path / "synthetic_runner.py"
    runner_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(module, "RUNNER_PATH", runner_script)

    def reap_synthetic_children() -> None:
        """即使断言提前失败也终止并回收本测试创建的合成长存活runner。"""
        for process, unit in children:
            if process.poll() is None:
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", "stop", unit],
                    capture_output=True, timeout=5, check=False,
                )
            if process.poll() is None:
                try:
                    os.killpg(process.pid, module.signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)

    request.addfinalizer(reap_synthetic_children)

    class ThreadStartFailure:
        """真实runner child启动后确定性模拟输出drainer线程启动失败。"""

        def __init__(self, **_kwargs):
            """只提供真实线程构造器需要的测试替身接口。"""

        def start(self):
            """在runner已经派发后触发有界停止和回收分支。"""
            raise RuntimeError("SYNTHETIC_REGISTERED_DRAIN_START_FAILURE")

    def count_dispatches(command, *args, **kwargs):
        """记录登记器实际调用真实runner派发helper的次数。"""
        dispatches.append(command)
        return real_bounded_child(command, *args, **kwargs)

    real_bounded_child = module._run_bounded_diagnostic_child
    real_prepare_scope = module._prepare_registered_scope

    def capture_scope(*args, **kwargs):
        """为失败回执测试保留真实systemd scope与其被回收的child进程。"""
        scope = real_prepare_scope(*args, **kwargs)
        children.append((scope["process"], scope["unit"]))
        return scope

    monkeypatch.setattr(module, "_prepare_registered_scope", capture_scope)
    monkeypatch.setattr(module.threading, "Thread", ThreadStartFailure)
    monkeypatch.setattr(module, "_run_bounded_diagnostic_child", count_dispatches)
    architecture = module.EVIDENCE / "architecture"
    architecture.mkdir(parents=True)
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    invocation_path = architecture / f"{run_id}-invocation.safe.json"
    freeze_path.write_text(json.dumps({"source_map_sha256": module._fingerprint(source_map)}))
    contract_path.write_text(json.dumps({
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "diagnostic_only": True,
        "acceptance_credit": False,
        "case_criteria": criteria,
        "model_configuration_hmac_sha256": "b" * 64,
        "model_configuration_payload_sha256": "c" * 64,
    }))
    invocation_path.write_text(json.dumps({
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "invocation_count": 1,
        "retry_count": 0,
    }))

    exit_code = module.run_registered(run_id, "sqlite", "sop-first-three")

    architecture_receipt = architecture / f"{run_id}-execution-receipt.safe.json"
    receipt = json.loads(architecture_receipt.read_text(encoding="utf-8"))
    attempt_path = architecture / f"{run_id}-attempt.safe.json"
    assert exit_code == 125
    assert attempt_path.is_file()
    assert receipt["runner_process_exit_code"] == 125
    assert receipt["registered_wrapper_watchdog"]["output_drain_start_failed"] is True
    assert receipt["runner_reported_result"]["safe_runner_json_present"] is False
    assert receipt["result_file_present"] is False
    assert receipt["acceptance_credit"] is False
    assert receipt["registered_runner_entry_claim_present"] is False
    assert len(dispatches) == 1
    assert len(children) == 1 and children[0][0].poll() is not None
    assert receipt["registered_wrapper_watchdog"]["runner_reaped"] is True

    with pytest.raises(RuntimeError, match="I4_REGISTERED_ATTEMPT_ALREADY_CLAIMED"):
        module.run_registered(run_id, "sqlite", "sop-first-three")
    assert len(dispatches) == 1
    assert len(children) == 1


def test_live_runner_rejects_bare_invocation_before_execution(tmp_path: Path, monkeypatch) -> None:
    """裸runner CLI没有登记合同和attempt时必须在setup、浏览器及模型前拒绝。"""
    runner = _browser_runner()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-90"
    invoked = []
    monkeypatch.setattr(runner, "EVIDENCE", tmp_path / "full20")
    monkeypatch.setattr(runner, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(runner.sys, "argv", [str(HARNESS / "run_browser_live.py"), run_id,
                                               "sqlite", "sop-first-three"])
    monkeypatch.setattr(runner, "arm_runner_total_deadline", lambda _deadlines: None)
    monkeypatch.setattr(runner, "restore_runner_total_deadline", lambda _handler: None)
    monkeypatch.setattr(
        runner, "_execute_registered_live_run",
        lambda *_args: invoked.append("executed") or 0,
    )

    with pytest.raises(RuntimeError, match="F3_REGISTERED_ATTEMPT_REQUIRED"):
        runner.main()

    assert invoked == []
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "full20").exists()


def test_live_runner_claims_frozen_attempt_before_setup_and_rejects_reentry(
        tmp_path: Path, monkeypatch) -> None:
    """合法attempt必须绑定当前冻结指纹、继承全局锁并只允许一次runner入口。"""
    runner = _browser_runner()
    registration = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-91"
    evidence = tmp_path / "registered-evidence"
    architecture = evidence / "architecture"
    architecture.mkdir(parents=True)
    lock_path = tmp_path / "private-state" / "registered-heavy-job.lock"
    lock_path.parent.mkdir(mode=0o700)
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source_map = {"synthetic-source.py": "a" * 64}
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    invocation_path = architecture / f"{run_id}-invocation.safe.json"
    attempt_path = architecture / f"{run_id}-attempt.safe.json"
    contract = {
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "model_configuration_hmac_sha256": "b" * 64,
        "model_configuration_payload_sha256": "c" * 64,
        "provider_account_status_probe": registration._provider_account_status_probe_contract(
            registration._model_payload(), "sop-first-three",
        ),
    }
    freeze = {"source_map_sha256": registration._fingerprint(source_map)}
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    invocation_path.write_text(json.dumps({"invocation_count": 1}), encoding="utf-8")
    attempt_path.write_text(json.dumps({
        "run_id": run_id,
        "dialect": "sqlite",
        "diagnostic_selection": "sop-first-three",
        "invocation_count": 1,
        "retry_count": 0,
        "claimed_before_runner_dispatch": True,
        "hard_memory_scope_verified": True,
        "systemd_scope_unit": "f3-i2-test.scope",
        "systemd_scope_control_group": "/user.slice/user-1000.slice/f3-i2-test.scope",
        "systemd_scope_memory_max_bytes": 2 * 1024**3,
        "systemd_scope_memory_swap_max_bytes": 0,
        "systemd_scope_memory_oom_group": True,
        "preregistered_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "source_map_sha256": freeze["source_map_sha256"],
    }), encoding="utf-8")
    registration.validate_diagnostic_scope = lambda *_args: None
    registration.validate_registered_runtime_scope = lambda _attempt: {
        "verified": True, "unit": "f3-i2-test.scope",
        "control_group": "/user.slice/user-1000.slice/f3-i2-test.scope",
        "memory_max_bytes": 2 * 1024**3,
        "memory_swap_max_bytes": 0, "memory_oom_group": True,
    }
    registration._registered_artifacts_valid = lambda *_args: True
    registration._source_map = lambda: (None, source_map)
    registration._model_hmac = lambda: "b" * 64
    registration._model_payload_sha256 = lambda: "c" * 64
    registration.REGISTERED_HEAVY_JOB_LOCK_PATH = lock_path
    monkeypatch.setattr(runner, "_load_registered_i4_module", lambda: registration)
    monkeypatch.setattr(runner, "REGISTERED_EVIDENCE", evidence)
    monkeypatch.setenv("F3_REGISTERED_HEAVY_LOCK_FD", str(lock_descriptor))
    monkeypatch.setenv("F3_EXPECTED_MODEL_CONFIG_PAYLOAD_SHA256", "c" * 64)

    try:
        runner_entry_claim = architecture / f"{run_id}-runner-entry-claim.safe.json"
        registration.validate_registered_runtime_scope = lambda _attempt: (
            (_ for _ in ()).throw(RuntimeError("F3_REGISTERED_HARD_MEMORY_SCOPE_UNVERIFIED"))
        )
        source_scans = []
        registration._source_map = lambda: source_scans.append("scan") or (None, source_map)
        with pytest.raises(RuntimeError, match="F3_REGISTERED_HARD_MEMORY_SCOPE_UNVERIFIED"):
            runner._validate_registered_runner_entry(run_id, "sqlite", "sop-first-three")
        assert source_scans == []
        assert not runner_entry_claim.exists()
        registration.validate_registered_runtime_scope = lambda _attempt: {
            "verified": True, "unit": "f3-i2-test.scope",
            "control_group": "/user.slice/user-1000.slice/f3-i2-test.scope",
            "memory_max_bytes": 2 * 1024**3,
            "memory_swap_max_bytes": 0, "memory_oom_group": True,
        }
        result = runner._validate_registered_runner_entry(run_id, "sqlite", "sop-first-three")
        claim_path = runner_entry_claim
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        assert result["path"] == str(claim_path)
        assert claim["acceptance_credit"] is False
        assert claim["hard_memory_scope_verified"] is True
        assert claim["systemd_scope_memory_swap_max_bytes"] == 0
        assert claim["attempt_claim_sha256"] == hashlib.sha256(attempt_path.read_bytes()).hexdigest()
        with pytest.raises(RuntimeError, match="F3_REGISTERED_RUNNER_ENTRY_ALREADY_CLAIMED"):
            runner._validate_registered_runner_entry(run_id, "sqlite", "sop-first-three")
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def test_live_runner_rejects_already_settled_attempt_before_source_rescan(
        tmp_path: Path, monkeypatch) -> None:
    """失败attempt已有最终receipt时，裸runner入口不得借旧claim再执行。"""
    runner = _browser_runner()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-92"
    evidence = tmp_path / "registered-evidence"
    architecture = evidence / "architecture"
    architecture.mkdir(parents=True)
    (architecture / f"{run_id}-attempt.safe.json").write_text("{}", encoding="utf-8")
    (architecture / f"{run_id}-execution-receipt.safe.json").write_text("{}", encoding="utf-8")
    registration = SimpleNamespace(validate_diagnostic_scope=lambda *_args: None)
    monkeypatch.setattr(runner, "REGISTERED_EVIDENCE", evidence)
    monkeypatch.setattr(runner, "_load_registered_i4_module", lambda: registration)

    with pytest.raises(RuntimeError, match="F3_REGISTERED_ATTEMPT_ALREADY_SETTLED"):
        runner._validate_registered_runner_entry(run_id, "sqlite", "sop-first-three")


def test_simulation_provider_output_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """模拟业务服务私有日志超限后继续排空并封顶保留字节。"""
    child = _browser_child()
    monkeypatch.setattr(child, "PROVIDER_LOG_MAX_BYTES", 64 * 1024, raising=False)
    process = subprocess.Popen(
        [child.sys.executable, "-c",
         "import os; chunk=b'z'*65536\nfor _ in range(4): os.write(1, chunk)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    target = tmp_path / "provider.private.log"
    state, overflow, drainer = child.drain_provider_process_output(process, target)
    assert process.wait(timeout=2) == 0
    drainer.join(timeout=2)
    assert target.stat().st_size == 64 * 1024
    assert target.stat().st_mode & 0o777 == 0o600
    assert state["bytes_captured"] == 64 * 1024
    assert state["truncated"] is True
    assert state["drain_complete"] is True
    assert overflow.is_set() is True


def test_simulation_provider_drain_start_failure_reaps_owned_process(
        tmp_path: Path, monkeypatch) -> None:
    """模拟服务日志drainer无法启动时及时回收provider并关闭日志描述符。"""
    child = _browser_child()
    process = subprocess.Popen(
        [child.sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None

    class ThreadStartFailure:
        """制造日志排空线程创建失败。"""

        def __init__(self, **_kwargs):
            """不保存任何业务数据，仅触发启动故障。"""

        def start(self):
            """在provider已创建后模拟资源耗尽。"""
            raise RuntimeError("SYNTHETIC_PROVIDER_DRAIN_START_FAILURE")

    monkeypatch.setattr(child.threading, "Thread", ThreadStartFailure)
    target = tmp_path / "provider.private.log"
    try:
        try:
            child.drain_provider_process_output(process, target)
        except RuntimeError as error:
            assert str(error) == "F3_PROVIDER_OUTPUT_DRAIN_START_FAILED"
        else:
            raise AssertionError("provider drain startup failure unexpectedly continued")
        assert process.poll() is not None
        assert process.stdout.closed
        assert target.stat().st_size == 0
        assert target.stat().st_mode & 0o777 == 0o600
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_sop_first_three_registration_requires_sqlite_scope(tmp_path: Path) -> None:
    """三案候选编号只能登记SQLite，不能把准备组抢跑到MySQL。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03"
    module.validate_diagnostic_scope(run_id, "sqlite", "sop-first-three")
    try:
        module.validate_diagnostic_scope(run_id, "mysql", "sop-first-three")
    except RuntimeError as error:
        assert str(error) == "I4_SOP_FIRST_THREE_DIAGNOSTIC_SQLITE_SCOPE_REQUIRED"
    else:
        raise AssertionError("SOP first-three diagnostic unexpectedly allowed MySQL")


def test_registered_sop_group_checks_tmpfs_before_attempt_claim(tmp_path: Path,
                                                                 monkeypatch) -> None:
    """三案tmpfs空间不足时登记器须在消费一次性attempt前安全拒绝。"""
    module = _registration_module()
    run_id = "i3-live-sqlite-sop-first-three-case-sdk-binding-20261013-03"
    criteria = {title: ["synthetic-criterion"] for title in (
        "JRN08 SOP normal", "JRN08 SOP cancel", "JRN08 SOP safe decline",
    )}
    runner = SimpleNamespace(
        PRIVATE=tmp_path / "private", EVIDENCE=tmp_path / "public",
        PYTHON=Path("/synthetic/python"), BUDGET=1_572_864,
        expected_case_criteria=lambda _name: criteria,
        private_tmpfs_preflight=lambda _path: {"valid": False, "reason": "insufficient_free_space"},
    )
    architecture = tmp_path / "public" / "architecture"
    architecture.mkdir(parents=True)
    freeze_path = architecture / f"{run_id}-source-freeze.safe.json"
    contract_path = architecture / f"{run_id}-preregistered-contract.safe.json"
    invocation_path = architecture / f"{run_id}-invocation.safe.json"
    freeze_path.write_text(json.dumps({"source_map_sha256": "frozen-map"}), encoding="utf-8")
    contract_path.write_text(json.dumps({
        "run_id": run_id, "dialect": "sqlite", "diagnostic_only": True,
        "diagnostic_selection": "sop-first-three", "acceptance_credit": False,
        "case_criteria": criteria, "model_configuration_hmac_sha256": "frozen-hmac",
        "model_configuration_payload_sha256": "frozen-payload",
    }), encoding="utf-8")
    invocation_path.write_text(json.dumps({
        "invocation_count": 1, "retry_count": 0,
        "diagnostic_selection": "sop-first-three",
    }), encoding="utf-8")
    monkeypatch.setattr(module, "EVIDENCE", runner.EVIDENCE)
    monkeypatch.setattr(module, "_source_map", lambda: (runner, {"source.py": "hash"}))
    monkeypatch.setattr(module, "_fingerprint", lambda _value: "frozen-map")
    monkeypatch.setattr(module, "_model_hmac", lambda: "frozen-hmac")
    monkeypatch.setattr(module, "_model_payload_sha256", lambda: "frozen-payload")
    monkeypatch.setattr(module, "_registered_artifacts_valid", lambda *_args: True)

    try:
        module.run_registered(run_id, "sqlite", "sop-first-three")
    except RuntimeError as error:
        assert str(error) == "F3_PRIVATE_TMPFS_PREFLIGHT_FAILED"
    else:
        raise AssertionError("insufficient private tmpfs unexpectedly reached dispatch")

    assert not (architecture / f"{run_id}-attempt.safe.json").exists()


def test_identity_archive_rejects_runtime_directory_without_private_mode(tmp_path: Path) -> None:
    """原始业务快照目录必须由当前UID独占，宽权限目录不得产生闭合结果。"""
    runner = _browser_runner()
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    (runtime / "facts-after-case-one.private.json").write_text(
        json.dumps({"original_facts": {"sdk_calls": []}}), encoding="utf-8",
    )
    evidence.mkdir()

    result = runner.archive_identity_bindings(runtime, evidence)

    assert result["valid"] is False
    assert result["verification_error_class"] == "RuntimeError"


def test_identity_archive_accounts_actual_bytes_after_snapshot_preflight(tmp_path: Path,
                                                                          monkeypatch) -> None:
    """预检后文件扩容仍按真正读取字节累计八MiB总预算并失败关闭。"""
    runner = _browser_runner()
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()
    names = [f"facts-after-case-{index:02d}.private.json" for index in range(9)]
    for name in names:
        _private_snapshot(runtime, name, b'{"original_facts":{"sdk_calls":[]}}')
    original_open = runner.os.open
    expanded = False

    def expand_after_first_open(path, flags, *args, **kwargs):
        """在首个文件已打开后扩展后续文件，制造lstat后实际read字节变化。"""
        nonlocal expanded
        descriptor = original_open(path, flags, *args, **kwargs)
        name = Path(path).name if isinstance(path, (str, Path)) else ""
        if name == names[0] and not expanded:
            expanded = True
            for sibling in names[1:]:
                with (runtime / sibling).open("r+b") as stream:
                    stream.seek(0)
                    stream.write(b'{"original_facts":{"sdk_calls":[]}}')
                    stream.write(b" " * (runner.IDENTITY_SNAPSHOT_MAX_FILE_BYTES - 34))
                    stream.truncate(runner.IDENTITY_SNAPSHOT_MAX_FILE_BYTES)
        return descriptor

    monkeypatch.setattr(runner.os, "open", expand_after_first_open)

    result = runner.archive_identity_bindings(runtime, evidence)

    assert expanded is True
    assert result["valid"] is False
    assert result["verification_error_class"] == "RuntimeError"


def test_identity_snapshot_open_uses_nonblocking_flag_across_fifo_replacement(
        tmp_path: Path, monkeypatch) -> None:
    """regular快照在预检后被替换成FIFO时，打开标志必须避免归档线程阻塞。"""
    runner = _browser_runner()
    runtime, evidence = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    evidence.mkdir()
    target = _private_snapshot(
        runtime, "facts-after-case-one.private.json",
        b'{"original_facts":{"sdk_calls":[]}}',
    )
    original_open = runner.os.open
    opened_flags: list[int] = []
    replaced = False

    def replace_with_fifo(path, flags, *args, **kwargs):
        """在文件打开瞬间换成FIFO，并捕获实现是否先使用O_NONBLOCK。"""
        nonlocal replaced
        name = Path(path).name if isinstance(path, (str, Path)) else ""
        if name == target.name and not replaced:
            replaced = True
            target.unlink()
            os.mkfifo(target, 0o600)
            opened_flags.append(flags)
            if not flags & os.O_NONBLOCK:
                raise BlockingIOError(errno.EAGAIN, "synthetic nonblocking guard")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runner.os, "open", replace_with_fifo)

    result = runner.archive_identity_bindings(runtime, evidence)

    assert replaced is True
    assert opened_flags and opened_flags[0] & os.O_NONBLOCK
    assert result["valid"] is False


def test_report_limit_is_checked_before_json_decoder(tmp_path: Path, monkeypatch) -> None:
    """原Playwright报告超过预算时不得先进入JSON对象分配。"""
    runner = _browser_runner()
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    report = owned / "report.private.json"
    report.write_bytes(b" " * (runner.RUNTIME_REPORT_MAX_BYTES + 1))
    report.chmod(0o600)
    descriptor = runner._open_checked_directory_fd(
        owned, "TEST_PRIVATE_DIRECTORY_REQUIRED", private=True,
    )
    decoder_called = False
    original_loads = runner.json.loads

    def observed_loads(value):
        """确认限额在JSON解码器被调用前生效。"""
        nonlocal decoder_called
        decoder_called = True
        return original_loads(value)

    monkeypatch.setattr(runner.json, "loads", observed_loads)
    try:
        try:
            runner._read_bounded_json_at(
                descriptor, report.name, runner.RUNTIME_REPORT_MAX_BYTES,
                "TEST_REPORT_LIMIT", budget={"maximum": runner.RUNTIME_REPORT_MAX_BYTES, "used": 0},
                private_file=True,
            )
        except RuntimeError as error:
            assert str(error) == "TEST_REPORT_LIMIT"
        else:
            raise AssertionError("oversized report unexpectedly parsed")
    finally:
        os.close(descriptor)
    assert decoder_called is False


def test_report_attachment_projection_hides_raw_ids_and_free_text(tmp_path: Path) -> None:
    """安全criterion可留业务判定与ID摘要，不得把附件中的ID/凭据/正文原样归档。"""
    runner = _browser_runner()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private_values = {
        "session_id": "PRIVATE-SESSION-CANARY",
        "api_key": "PRIVATE-KEY-CANARY",
        "message": "PRIVATE-MESSAGE-CANARY",
    }
    criterion = {
        "criterion": "synthetic-boundary-check", "passed": True,
        "real_vendor_qualified": False, "business_provider_kind": "simulated_async_service",
        **private_values,
    }
    report = {"suites": [{"specs": [{"title": "synthetic boundary case", "tests": [{
        "results": [{"status": "passed", "retry": 0, "duration": 1, "errors": [],
                     "attachments": [{"name": "f3-safe-criterion",
                                      "body": __import__("base64").b64encode(
                                          json.dumps(criterion).encode()).decode()}]}]}]}]}]}

    assert runner.archive(
        report, evidence,
        {"synthetic boundary case": {"synthetic-boundary-check"}},
    ) is True
    serialized = (evidence / "criteria.safe.json").read_text(encoding="utf-8")
    assert all(value not in serialized for value in private_values.values())
    archived = json.loads(serialized)[0]
    assert archived["session_id_sha256"] == hashlib.sha256(
        private_values["session_id"].encode()
    ).hexdigest()
    assert "api_key" not in archived and "message" not in archived


def test_report_criterion_rejects_path_only_attachment_without_opening_path(
        tmp_path: Path, monkeypatch) -> None:
    """原报告附件必须内嵌且受限，不能借路径字段打开任意文件。"""
    runner = _browser_runner()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"criterion": "path-read"}), encoding="utf-8")

    def forbid_read_bytes(_self):
        """路径附件不应触发任何文件读取。"""
        raise AssertionError("path attachment was opened")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    report = {"suites": [{"specs": [{"title": "synthetic path case", "tests": [{
        "results": [{"status": "passed", "retry": 0, "errors": [], "attachments": [{
            "name": "f3-safe-criterion", "path": str(outside),
        }]}]}]}]}]}
    try:
        runner.archive(report, evidence)
    except RuntimeError as error:
        assert str(error) == "F3_SAFE_CRITERION_ATTACHMENT_LIMIT"
    else:
        raise AssertionError("path-only criterion attachment unexpectedly accepted")


def test_frozen_frontend_snapshot_hashes_only_private_bounded_artifacts(
        tmp_path: Path, monkeypatch) -> None:
    """构建清单、递归产物摘要和登录租户均从受限私有目录闭合。"""
    runner = _browser_runner()
    root = tmp_path / "frozen"
    root.mkdir(mode=0o700)
    dist = root / "dist"
    dist.mkdir(mode=0o700)
    assets = dist / "assets"
    assets.mkdir(mode=0o700)
    login_tenant = "f3_ui_tenant_live_" + "a" * 24
    files = {"index.html": b"<!doctype html><html></html>",
             "assets/app.js": login_tenant.encode("ascii")}
    for relative, body in files.items():
        path = dist / relative
        path.write_bytes(body)
        path.chmod(0o600)
    artifacts = runner.hash_private_tree(
        dist, runner.FRONTEND_ARTIFACT_MAX_BYTES,
        runner.FRONTEND_ARTIFACT_MAX_TOTAL_BYTES, 4096, "TEST_BUILD_LIMIT",
    )
    manifest = {
        "schema": "gongge_xuban.f3_frozen_frontend_build.v2", "exit_code": 0,
        "source_unchanged": True, "source_snapshot_sha256_before": "b" * 64,
        "source_snapshot_sha256_after": "b" * 64, "frontend_dist_path": str(dist),
        "artifacts": artifacts, "artifact_count": len(artifacts),
        "login_tenant_matches_bundle": True, "login_tenant_id": login_tenant,
    }
    manifest_path = root / "frontend-build.safe.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    monkeypatch.setattr(runner, "FROZEN_FRONTEND_BUILD_ROOT", root)

    build, raw, observed = runner.frozen_frontend_build_snapshot()

    assert build["login_tenant_id"] == login_tenant
    assert raw == manifest_path.read_bytes()
    assert observed == artifacts


def test_i2_candidate_default_build_root_matches_frozen_build03_contract() -> None:
    """预注册器和运行器必须默认绑定同一已冻结构建目录，不能回退dist。"""
    registration = _registration_module()
    runner = _browser_runner()
    assert registration.FROZEN_BUILD == runner.FROZEN_FRONTEND_BUILD_ROOT


def test_source_snapshot_streams_required_sources_without_generated_dist() -> None:
    """源码冻结覆盖后端、正式E2E及实际辅助脚本，并排除不受控生成dist。"""
    runner = _browser_runner()

    source_map = runner.snapshot_product_sources()

    assert "backend/app/memory/service.py" in source_map
    assert "frontend-enterprise/e2e/final_f3_live_memory.py" in source_map
    assert any("i2-case-sdk-binding-04/execution-harness/run_browser_live.py" in key
               for key in source_map)
    assert not any("/frontend-enterprise/dist/" in key for key in source_map)


def test_source_snapshot_excludes_generated_pytest_cache(tmp_path: Path, monkeypatch) -> None:
    """源码映射排除由测试运行临时产生的pytest缓存，保持候选冻结可复现。"""
    runner = _browser_runner()
    root = tmp_path / "repo"
    required_files = (
        "backend/pyproject.toml",
        "backend/alembic.ini",
        "backend/single_port_app.py",
        "backend/desktop_launcher.py",
        "backend/app/service.py",
        "backend/tests/test_service.py",
        "backend/alembic/versions/revision.py",
        "frontend-enterprise/src/app.ts",
        "frontend-enterprise/e2e/test_contract.py",
        "frontend-enterprise/package.json",
    )
    for relative in required_files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    previous = root / "previous"
    previous.mkdir()
    for name in ("run_checks_bounded.py", "formal_child.py"):
        (previous / name).write_text("fixture", encoding="utf-8")
    cache_file = root / "frontend-enterprise/e2e/.pytest_cache/v/cache/nodeids"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("generated", encoding="utf-8")
    candidate_harness = root / "candidate-harness"
    candidate_harness.mkdir()
    runner_file = candidate_harness / "run_browser_live.py"
    runner_file.write_text("fixture", encoding="utf-8")

    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "PREVIOUS", previous)
    monkeypatch.setattr(runner, "FROZEN_FRONTEND_BUILD_ROOT", root / "frozen")
    monkeypatch.setattr(runner, "__file__", str(runner_file))
    monkeypatch.setattr(runner, "frozen_frontend_build_snapshot", lambda: ({}, b"{}", {}))

    source_map = runner.snapshot_product_sources()

    assert "frontend-enterprise/e2e/test_contract.py" in source_map
    assert "frontend-enterprise/e2e/.pytest_cache/v/cache/nodeids" not in source_map


def test_private_memory_snapshot_rejects_excessive_nodes_before_json_decode(
        tmp_path: Path, monkeypatch) -> None:
    """私有记忆快照在对象树分配前拒绝超过结构节点上限的JSON。"""
    runner = _browser_runner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    raw = b"[" + b"0," * runner.MEMORY_PRIVATE_SNAPSHOT_MAX_NODES + b"0]"
    snapshot = runtime / "facts.private.json"
    snapshot.write_bytes(raw)
    snapshot.chmod(0o600)
    decoder_calls = []

    def forbidden_decoder(_raw):
        """记录任何过早进入标准JSON对象分配器的尝试。"""
        decoder_calls.append(True)
        raise AssertionError("JSON_DECODER_REACHED_BEFORE_NODE_LIMIT")

    monkeypatch.setattr(runner.json, "loads", forbidden_decoder)
    try:
        runner._read_private_memory_snapshot(runtime, snapshot, len(raw))
    except RuntimeError as error:
        assert str(error) == "F3_MEMORY_PRIVATE_SNAPSHOT_COMPLEXITY_LIMIT"
    else:
        raise AssertionError("oversized-node private memory snapshot unexpectedly decoded")
    assert decoder_calls == []


def test_browser_child_output_is_capped_and_owned_writer_is_stopped(
        tmp_path: Path, monkeypatch) -> None:
    """真实合成child无限写stdout时，日志封顶并只停止该次新会话进程组。"""
    runner = _browser_runner()
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "PRIVATE", owned)
    monkeypatch.setattr(runner, "RUNTIME_LOG_MAX_BYTES", 64 * 1024)
    monkeypatch.setattr(runner, "_owned_memory_sample", lambda pid, _dependencies: ({
        "owned_budget_charge_kib": 1024, "owned_tree_rss_kib": 2048,
        "owned_tree_pss_kib": 1024, "owned_tree_swap_pss_kib": 0,
        "pss_complete": True,
    }, {pid}))
    original_sleep = runner.time.sleep
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: original_sleep(0.01))

    code = runner.bounded_child([
        runner.sys.executable, "-c",
        "import os; chunk=b'x'*65536\nwhile True: os.write(1, chunk)",
    ], {"PATH": os.environ.get("PATH", ""),
        "F3_DIAGNOSTIC_SELECTION": "sop-first-three"}, owned, destination,
       maximum_runtime_seconds=10)

    summary = json.loads((destination / "resource-summary.safe.json").read_text())
    log = owned / "browser.private.log"
    assert code != 0
    assert summary["cutoff"] == "owned_browser_process_output_limit"
    assert summary["browser_log_truncated"] is True
    assert summary["browser_log_bytes_captured"] == 64 * 1024
    assert summary["browser_log_drain_complete"] is True
    assert summary["private_tmpfs_monitored"] is True
    assert summary["private_tmpfs_minimum_free_bytes"] == runner.MEMORY_SNAPSHOT_MINIMUM_FREE_BYTES
    assert log.stat().st_size == 64 * 1024
    assert log.stat().st_mode & 0o777 == 0o600


def test_browser_output_drain_start_failure_reaps_only_the_new_child(
        tmp_path: Path, monkeypatch) -> None:
    """日志排空线程无法启动时，runner必须回收刚创建的自有child及pipe描述符。"""
    runner = _browser_runner()
    owned, destination = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    real_popen = runner.subprocess.Popen
    children = []
    drain_descriptors = []

    def capture_owned_child(*args, **kwargs):
        """只捕获本测试通过真实Popen创建的独立进程供回收断言。"""
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    class ThreadStartFailure:
        """模拟当前进程无法再创建日志drainer线程的资源故障。"""

        def __init__(self, *, args, **_kwargs):
            """保存候选传入的pipe副本描述符，仅供验证泄漏清理。"""
            drain_descriptors.append(args[1])

        def start(self):
            """在child已启动后复现线程创建失败。"""
            raise RuntimeError("SYNTHETIC_DRAIN_THREAD_START_FAILURE")

    monkeypatch.setattr(runner.subprocess, "Popen", capture_owned_child)
    monkeypatch.setattr(runner.threading, "Thread", ThreadStartFailure)
    try:
        try:
            runner.bounded_child([
                runner.sys.executable, "-c", "import time; time.sleep(30)",
            ], {"PATH": os.environ.get("PATH", "")}, owned, destination,
               maximum_runtime_seconds=10)
        except RuntimeError as error:
            assert str(error) == "F3_BROWSER_OUTPUT_DRAIN_START_FAILED"
        else:
            raise AssertionError("drain thread startup failure unexpectedly continued")
        assert len(children) == 1
        assert children[0].poll() is not None
        assert len(drain_descriptors) == 1
        try:
            os.fstat(drain_descriptors[0])
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("failed output drainer leaked its duplicate log descriptor")
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    runner.os.killpg(child.pid, runner.signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
        for descriptor in drain_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_safe_junit_rejects_dtd_and_removes_original_parameter_and_failure_text(tmp_path: Path) -> None:
    """JUnit先拒绝DTD，再仅保留原计数与参数/错误摘要，不泄露原文字段。"""
    runner = _browser_runner()
    destination = tmp_path / "junit.safe.xml"
    raw = (b'<testsuite><testcase name="case[PRIVATE-PARAM-CANARY">'
           b'<failure message="PRIVATE-MESSAGE-CANARY">PRIVATE-ERROR-CANARY</failure>'
           b'</testcase></testsuite>')
    original_sha = runner.safe_junit_bytes(raw, destination)
    safe = destination.read_bytes()
    assert original_sha == hashlib.sha256(raw).hexdigest()
    assert b"PRIVATE-PARAM-CANARY" not in safe
    assert b"PRIVATE-MESSAGE-CANARY" not in safe
    assert b"PRIVATE-ERROR-CANARY" not in safe
    try:
        runner.safe_junit_bytes(b"<!DOCTYPE x [<!ENTITY e 'x'>]><x>&e;</x>", destination)
    except RuntimeError as error:
        assert str(error) == "F3_JUNIT_XML_LIMIT_OR_DTD"
    else:
        raise AssertionError("DTD-bearing JUnit unexpectedly accepted")


def test_private_inventory_hashes_originals_without_copying_or_parsing(tmp_path: Path) -> None:
    """原报告和日志只留受限摘要收据，私有原件不会被复制到公开证据目录。"""
    runner = _browser_runner()
    owned, evidence = tmp_path / "owned", tmp_path / "evidence"
    owned.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    originals = {
        "report.private.json": b'{"private":"PRIVATE-REPORT-CANARY"}',
        "junit.private.xml": b"PRIVATE-JUNIT-CANARY",
        "browser.private.log": b"PRIVATE-LOG-CANARY",
        "input.private.json": b'{"api_key":"PRIVATE-KEY-CANARY"}',
    }
    for name, raw in originals.items():
        path = owned / name
        path.write_bytes(raw)
        path.chmod(0o600)

    inventory = runner.archive_private_artifact_inventory(owned, evidence)

    assert inventory["valid"] is True
    serialized = (evidence / "private-artifact-inventory.safe.json").read_text(encoding="utf-8")
    assert all(value.decode() not in serialized for value in originals.values())
    assert not any((evidence / name).exists() for name in originals)
    assert {item["name"] for item in inventory["files"]} == set(originals)


def test_safe_sdk_and_wire_projection_remove_raw_scope_ids() -> None:
    """公共SDK回执/输入记录只能携带规范hash，不泄露租户用户会话和知识版本ID。"""
    runner = _browser_runner()
    private_values = ["PRIVATE-TENANT", "PRIVATE-USER", "PRIVATE-SESSION", "PRIVATE-KNOWLEDGE"]
    identity = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in zip(
        ("tenant_id", "user_id", "session_id", "agent_id"), private_values,
    )}
    wire = runner._safe_wire_rows([{
        "pid": 424242, "identity_sha256": identity,
        "request_sha256": "a" * 64, "knowledge_version_ids": [private_values[-1]],
        "operation": "dynamic_task.answer", "memory_complete": [True],
    }])
    receipt = runner._safe_sdk_receipt_rows([{
        "call_id": "f3_sdk_" + "a" * 32, "request_sha256": "b" * 64,
        "identity_sha256": identity, "status": "completed", "usage": {"prompt_tokens": 9},
    }])
    serialized = json.dumps([wire, receipt])
    assert all(value not in serialized for value in private_values)
    assert wire[0]["knowledge_version_count"] == 1
    assert wire[0]["knowledge_version_ids_sha256"] == hashlib.sha256(
        json.dumps([private_values[-1]], separators=(",", ":")).encode()
    ).hexdigest()


def test_safe_sdk_receipt_rejects_unrecognized_hashed_identity_key() -> None:
    """SDK公开身份hash仅接受固定绑定字段，未知键不能绕过字段白名单。"""
    runner = _browser_runner()
    row = {
        "call_id": "f3_sdk_" + "a" * 32,
        "request_sha256": "b" * 64,
        "identity_sha256": {"tenant_id": "c" * 64, "private_reference": "d" * 64},
    }
    try:
        runner._safe_sdk_receipt_rows([row])
    except RuntimeError as error:
        assert str(error) == "F3_FINAL_SDK_RECEIPT_IDENTITY_INVALID"
    else:
        raise AssertionError("unrecognized SDK receipt identity key unexpectedly accepted")
