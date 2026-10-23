"""
@Time       : 2026/10/16
@Author     : zhanglp8181
@File       : test_final_f3_memory_registration.py
@CallChain  : pytest → 当前F3登记器 → memory-dta-freeze预算合同校验
@Description: 固定内存冻结诊断不需要供应商余额探针，同时拒绝预算篡改。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _current_registration_module():
    """加载本轮F3登记器，避免把历史证据目录的旧实现当作当前合同。"""
    root = Path(__file__).resolve().parents[2]
    path = (root / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/"
            "i2-case-sdk-binding-04/execution-harness/registered_i4_run.py")
    spec = importlib.util.spec_from_file_location("current_f3_registration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("F3_CURRENT_REGISTRATION_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_live_runner_module():
    """加载当前浏览器运行器以核对Python到Playwright的截止变量传递。"""
    root = Path(__file__).resolve().parents[2]
    harness = (root / "docs/StaffDeck能力蒸馏-7adc7c8/evidence/final-f3-20261014/"
               "i2-case-sdk-binding-04/execution-harness")
    path = harness / "run_browser_live.py"
    import sys

    sys.path.insert(0, str(harness))
    spec = importlib.util.spec_from_file_location("current_f3_live_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("F3_CURRENT_LIVE_RUNNER_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_freeze_budget_contract_does_not_require_balance_probe() -> None:
    """内存冻结只校验调用与token预算，余额探针只属于SOP三案合同。"""
    module = _current_registration_module()
    metadata = module._diagnostic_contract_metadata("memory-dta-freeze")
    registered = {
        "maximum_model_invocations": metadata["maximum_model_invocations"],
        "model_dispatch_token_budget": metadata["model_dispatch_token_budget"],
    }
    assert module._diagnostic_model_budget_contract_valid(registered, "memory-dta-freeze")
    registered["model_dispatch_token_budget"] = {
        **metadata["model_dispatch_token_budget"],
        "maximum_completion_tokens_total": 1,
    }
    assert not module._diagnostic_model_budget_contract_valid(registered, "memory-dta-freeze")


def test_memory_freeze_deadline_aliases_reach_playwright_fixture() -> None:
    """运行器必须把同一绝对截止映射到内存夹具约定的变量名。"""
    runner = _current_live_runner_module()
    deadlines = runner.diagnostic_deadline_schedule("memory-dta-freeze", 10)
    assert deadlines is not None
    environment = runner.diagnostic_deadline_environment("memory-dta-freeze", deadlines)
    assert environment["F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS"] == str(
        deadlines["active_deadline_ns"]
    )
    assert environment["F3_MEMORY_FREEZE_AFTER_EACH_DEADLINE_MONOTONIC_NS"] == str(
        deadlines["after_each_deadline_ns"]
    )
    assert environment["F3_MEMORY_FREEZE_FIXTURE_DEADLINE_MONOTONIC_NS"] == str(
        deadlines["fixture_deadline_ns"]
    )


def test_current_memory_contract_includes_sop_freeze_and_clear_cases() -> None:
    """当前MQ-I2合同必须登记新增SOP冻结与清除旅程，不能沿用历史20案快照。"""
    runner = _current_live_runner_module()
    contract = runner.expected_case_criteria()
    assert len(contract) == 23
    assert "F3-MEMORY frozen SOP restarts with original facts and clear blocks old projection" in contract
    assert "F3-MEMORY SOP clear: restored real answer cannot disclose invalid frozen facts" in contract
