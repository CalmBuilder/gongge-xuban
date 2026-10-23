"""
@Time       : 2026/10/16
@Author     : zhanglp8181
@File       : test_final_f3_current_zero_sdk.py
@CallChain  : 当前F-3验收harness → 零SDK只读诊断 → 运行时证据归档
@Description: 固定当前harness对零模型只读诊断的空账本闭合合同。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / (
    "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/"
    "i2-case-sdk-binding-04/execution-harness"
)


def _load_current_harness():
    """加载当前 F-3 runner 而不启动服务或访问真实数据库。"""
    path = HARNESS / "run_browser_live.py"
    spec = importlib.util.spec_from_file_location("f3_current_zero_sdk_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("F3_CURRENT_HARNESS_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HARNESS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HARNESS))
    return module


def test_current_zero_sdk_runtime_accepts_empty_ledger(tmp_path: Path) -> None:
    """零模型只读诊断在没有生成JSONL账本时仍须完成运行时闭合。"""
    runner = _load_current_harness()
    owned = tmp_path / "owned"
    evidence = tmp_path / "evidence"
    runtime = owned / "f3-live-sqlite"
    runtime.mkdir(parents=True)
    evidence.mkdir()
    os.chmod(owned, 0o700)
    os.chmod(runtime, 0o700)
    os.chmod(evidence, 0o700)
    application_pid, provider_pid = 999998, 999997
    health_path = runtime / f"sdk-final-health-{application_pid}.safe.json"
    health_path.write_text(
        json.dumps(
            {
                "pid": application_pid,
                "original_sdk": {
                    "valid": False,
                    "client_state_count": 0,
                    "started_count": 0,
                    "settled_count": 0,
                    "in_flight_count": 0,
                    "observation_error_count": 0,
                    "receipt_count": 0,
                    "file_matches": True,
                    "write_failed": False,
                },
                "wire_inputs": {"valid": True, "receipt_count": 0, "error_count": 0},
            }
        )
    )
    os.chmod(health_path, 0o600)
    provider_observations = owned / "provider-final-observations.safe.json"
    provider_observations.write_text(
        json.dumps(
            {
                "observation_healthy": True,
                "observation_error_count": 0,
                "rows": [],
                "submit_http_count": 0,
            }
        )
    )
    os.chmod(provider_observations, 0o600)
    provider_cleanup = owned / "provider-process-cleanup.safe.json"
    provider_cleanup.write_text(
        json.dumps({"actual_pid": provider_pid, "process_absent": True})
    )
    os.chmod(provider_cleanup, 0o600)
    application_cleanup = owned / "application-process-cleanup.safe.json"
    application_cleanup.write_text(
        json.dumps({"cleanup_complete": True})
    )
    os.chmod(application_cleanup, 0o600)
    assert runner.archive_runtime_evidence(
        owned, evidence, "sqlite", require_model_usage=False
    ) is True
