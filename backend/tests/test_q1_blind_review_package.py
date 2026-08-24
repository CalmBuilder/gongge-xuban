"""
@Time       : 2026/08/23
@Author     : zhanglp8181
@File       : test_q1_blind_review_package.py
@CallChain  : pytest → build_q1_blind_review_package → Q1 批次选择契约
@Description: 防止匿名盲评生成器把上一轮证据误当作当前 release-candidate。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_builder():
    """加载脚本模块以检查不触碰文件系统的批次选择常量。"""

    path = ROOT / "scripts" / "build_q1_blind_review_package.py"
    spec = importlib.util.spec_from_file_location("q1_blind_review_builder", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载盲评生成器: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_aggregator():
    """加载聚合脚本模块以检查新批次拥有独立的角色文件路径。"""

    path = ROOT / "scripts" / "aggregate_q1_blind_review.py"
    spec = importlib.util.spec_from_file_location("q1_blind_review_aggregator", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载盲评聚合器: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_candidate_batch_has_distinct_report_prefix_and_output_stem() -> None:
    """release-candidate 必须读取当前 r9/r11/r8 报告并写入独立证据名。"""

    module = _load_builder()
    release_candidate = module.REPORT_GLOBS_BY_BATCH["release-candidate"]

    assert "release-candidate-r9" in release_candidate["writing-fair"]
    assert "release-candidate-r11" in release_candidate["diagnosing-positive"]
    assert "release-candidate-r8" in release_candidate["codebase-design"]
    assert module.OUTPUT_STEMS["release-candidate"] != module.OUTPUT_STEMS["current-source"]


def test_release_candidate_aggregator_uses_isolated_role_files() -> None:
    """新批次的角色评分和聚合报告不能覆盖旧批次证据。"""

    module = _load_aggregator()
    current = module.BATCH_PATHS["current-source"]
    release_candidate = module.BATCH_PATHS["release-candidate"]

    assert release_candidate["package"] != current["package"]
    assert release_candidate["key"] != current["key"]
    assert release_candidate["output"] != current["output"]
    assert all(
        path != current["roles"][role]
        for role, path in release_candidate["roles"].items()
    )


def test_current_final_batch_points_to_latest_three_profile_reports() -> None:
    """current-final 必须绑定最新 r31/r31/r32 报告，避免重新混入旧指纹。"""

    builder = _load_builder()
    aggregator = _load_aggregator()
    current_final = builder.REPORT_GLOBS_BY_BATCH["current-final"]

    assert "current-final-r31" in current_final["writing-fair"]
    assert "current-final-r31" in current_final["codebase-design"]
    assert "current-final-r32" in current_final["diagnosing-positive"]
    assert builder.OUTPUT_STEMS["current-final"] == "q1-current-final-blind-review"
    assert aggregator.BATCH_PATHS["current-final"]["package"].name == (
        "q1-current-final-blind-review-package.json"
    )
