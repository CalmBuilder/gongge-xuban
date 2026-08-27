"""
@Time       : 2026/08/24
@Author     : zhanglp8181
@File       : test_q1_certification_batch.py
@CallChain  : pytest → Q1 certification batch runner → 同源/成对证据摘要
@Description: 防止认证批把不同源码、模型或硬门失败轮次静默合并。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]


def _load_runner():
    """加载批次 runner 的纯函数，避免测试启动真实模型或浏览器。"""

    path = ROOT / "scripts" / "run_agent_quality_q1_certification_batch.py"
    spec = importlib.util.spec_from_file_location("q1_certification_batch_runner", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载 Q1 批次 runner: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_browser_runner():
    """加载真实浏览器 launcher 的环境投影函数，不启动服务或模型。"""

    path = ROOT / "scripts" / "run_agent_quality_q1_browser_regression.py"
    spec = importlib.util.spec_from_file_location("q1_browser_regression_runner", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载 Q1 浏览器 launcher: {path}")
    module = importlib.util.module_from_spec(spec)
    scripts_path = str(ROOT / "scripts")
    sys.path.insert(0, scripts_path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_path)
    return module


def test_source_fingerprint_digest_is_stable_and_changes_with_model() -> None:
    """源码/模型/Skill 指纹摘要必须稳定，模型配置变化必须形成新批次。"""

    runner = _load_runner()
    report = {
        "certification_fingerprints": {"backend/app/dynamic_tasks/agent.py": "a" * 64},
        "source_model_config_id": "model_a",
        "provider_endpoint": "https://example.test/v1",
        "model": "deepseek-test",
        "temperature": 0.0,
        "max_output_tokens": 8192,
        "capability_checksum": "cap-a",
        "upstream_skills_revision": "skill-rev",
        "upstream_skill_source_checksums": {"SKILL.md": "b" * 64},
    }

    first = runner._source_fingerprint_digest(report)
    assert first == runner._source_fingerprint_digest(dict(report))
    changed = {**report, "source_model_config_id": "model_b"}
    assert first != runner._source_fingerprint_digest(changed)


def test_quality_summary_pairs_control_and_treatment_without_hiding_hard_failures() -> None:
    """四象限摘要应保留均值、配对方向和硬门失败，不替模型质量门下结论。"""

    runner = _load_runner()
    rows = [
        {
            "run_id": "run-01",
            "scenarios": [
                {
                    "scenario": "inline-control",
                    "score": 70,
                    "hard_gate_failures": [],
                },
                {
                    "scenario": "inline-treatment",
                    "score": 90,
                    "hard_gate_failures": [],
                },
                {
                    "scenario": "attachment-control",
                    "score": 80,
                    "hard_gate_failures": ["result_verified"],
                },
                {
                    "scenario": "attachment-treatment",
                    "score": 85,
                    "hard_gate_failures": [],
                },
            ],
        }
    ]

    summary = runner._quality_summary(rows)

    assert summary["pair_count"] == 2
    assert summary["treatment_non_decrease_count"] == 2
    assert summary["by_scenario"]["attachment-control"]["hard_gate_failure_count"] == 1
    assert summary["by_scenario"]["inline-treatment"]["mean"] == 90


def test_quality_summary_pairs_published_check_control_and_treatment() -> None:
    """单一 published-check 场景也必须形成 control/treatment 配对。"""

    runner = _load_runner()
    summary = runner._quality_summary(
        [
            {
                "run_id": "diagnosis-01",
                "scenarios": [
                    {"scenario": "control", "score": 40, "hard_gate_failures": []},
                    {"scenario": "treatment", "score": 80, "hard_gate_failures": []},
                ],
            }
        ]
    )

    assert summary["pair_count"] == 1
    assert summary["pair_deltas"] == [
        {
            "run_id": "diagnosis-01",
            "input_mode": "published-check",
            "control": 40,
            "treatment": 80,
            "delta": 40.0,
            "treatment_non_decrease": True,
        }
    ]


def test_browser_environment_forwards_ordinary_benchmark(monkeypatch) -> None:
    """普通题集选择必须穿过 launcher 白名单，不能静默退回默认题集。"""

    runner = _load_browser_runner()
    monkeypatch.setattr(runner.live_launcher, "_browser_environment", lambda _env: {})
    browser_env = runner._q1_browser_environment(
        {"Q1_ORDINARY_BENCHMARK": "fair-v1"},
        fingerprints={"fingerprint": "a"},
        model_id="model_test",
        model_name="model-test",
        capability_checksum="cap-test",
        public_endpoint="https://provider.test/v1",
        profile_name="ordinary",
        skill_dir=None,
    )

    assert browser_env["Q1_ORDINARY_BENCHMARK"] == "fair-v1"
