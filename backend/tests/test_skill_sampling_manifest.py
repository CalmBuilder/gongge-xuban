"""
@Time       : 2026/08/23
@Author     : zhanglp8181
@File       : test_skill_sampling_manifest.py
@CallChain  : pytest → build_skill_sampling_manifest → Skill 分层抽样清单契约
@Description: 验证上游 Skill 清单只读、分层和非生产目录排除规则。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_manifest_module():
    """加载抽样清单脚本，避免把上游目录加入应用 import 路径。"""

    path = ROOT / "scripts" / "build_skill_sampling_manifest.py"
    spec = importlib.util.spec_from_file_location("skill_sampling_manifest", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载 Skill 抽样清单脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_read_only_and_excludes_in_progress_skills() -> None:
    """清单必须固定当前上游 revision，并排除 in-progress 目录的候选资格。"""

    module = _load_manifest_module()
    manifest = module.build_manifest()

    assert manifest["execution_policy"] == "read_only_inventory_no_skill_content_execution"
    assert manifest["skill_count"] == 35
    assert manifest["eligible_candidate_count"] == 29
    assert manifest["category_counts"]["in-progress"] == 6
    assert manifest["frozen_sample"]["sample_size"] == 20
    assert manifest["frozen_sample"]["category_quotas"] == {
        "engineering": 12,
        "productivity": 5,
        "misc": 3,
    }
    assert set(manifest["frozen_sample"]["anchors"]) <= set(manifest["frozen_sample"]["skills"])
    assert all(
        not (record["category"] == "in-progress" and record["candidate_eligible"])
        for record in manifest["skills"]
    )


def test_manifest_exposes_risk_flags_without_executing_content() -> None:
    """包含命令字样的 Skill 只产生风险标记，不改变只读清单策略。"""

    module = _load_manifest_module()
    manifest = module.build_manifest()
    diagnosing = next(item for item in manifest["skills"] if item["name"] == "diagnosing-bugs")

    assert "bash" in diagnosing["risk_flags"]
    assert "command" in diagnosing["risk_flags"]
    assert manifest["sampling_gate"]["quality_gain_requires_task_skill_match"] is True
