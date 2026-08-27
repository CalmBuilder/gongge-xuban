"""
@Time       : 2026/08/25
@Author     : zhanglp8181
@File       : test_q1_hidden_rubric.py
@CallChain  : pytest → Q1隐藏量表审计器 → 分层/成对阈值与fail-closed覆盖
@Description: 验证Q1发布后评分不会把探索性机械分数误报为正式通过。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "audit_q1_hidden_rubric.py"
SPEC = importlib.util.spec_from_file_location("audit_q1_hidden_rubric", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _batch(*scenarios: dict[str, Any]) -> dict[str, Any]:
    """构造只含逐轮指针的最小批次，模拟真实聚合报告结构。"""

    return {
        "runs": [
            {"run_id": "run-01", "evidence_file": "missing.json"},
        ],
        "passed": True,
        "source_homogeneity": {"status": "passed"},
        "scenarios": scenarios,
    }


def test_direct_hidden_dimensions_are_scored_without_rewriting_total() -> None:
    """验证已有四维隐藏分值按原始max读取，不能由总分猜测子维度。"""

    item = {
        "run_id": "run-01",
        "scenario": "inline-treatment",
        "variant": "treatment",
        "input_mode": "inline",
        "quality_rubric": {
            "facts_and_evidence": {"score": 40, "max": 40},
            "task_completion": {"score": 25, "max": 25},
            "skill_method": {"score": 25, "max": 25},
            "safety": {"score": 10, "max": 10},
            "total": 100,
        },
    }
    scored = MODULE._score_scenario("codebase", item)
    assert scored["direct_hidden_dimensions"] is True
    assert scored["dimensions"]["skill_method"] == 25
    assert scored["total"] == 100


def test_legacy_adapter_normalizes_profile_specific_dimensions() -> None:
    """验证旧writing/diagnosing字段通过冻结适配器归一，不伪造缺失字段。"""

    item = {
        "run_id": "run-01",
        "scenario": "inline-treatment",
        "variant": "treatment",
        "input_mode": "inline",
        "quality_rubric": {
            "paths_and_commands": {"score": 20, "max": 20},
            "triggered_context_pointers": {"score": 20, "max": 20},
            "information_hierarchy": {"score": 20, "max": 20},
            "checkable_completion": {"score": 20, "max": 20},
            "pruning": {"score": 10, "max": 10},
            "safety": {"score": 10, "max": 10},
        },
    }
    scored = MODULE._score_scenario("writing-fair", item)
    assert scored["direct_hidden_dimensions"] is False
    assert scored["dimensions"] == {
        "facts_and_evidence": 40.0,
        "task_completion": 25.0,
        "skill_method": 25.0,
        "safety": 10.0,
    }


def test_threshold_is_fail_closed_when_ordinary_layer_is_missing() -> None:
    """验证只有Dynamic配对时，普通AgentLoop覆盖缺失会否决总发布。"""

    profiles = {
        "writing-fair": {
            "paired": {"pair_count": 1},
            "pair_inputs": ["inline", "attachment"],
        },
        "codebase": {
            "paired": {"pair_count": 1},
            "pair_inputs": ["inline", "attachment"],
        },
        "diagnosing-positive": {
            "paired": {"pair_count": 1},
            "pair_inputs": ["inline"],
        },
    }
    coverage = MODULE._coverage(profiles=profiles, requested_profiles=tuple(profiles))
    assert coverage["required_layers"]["dynamic_inline"] is True
    assert coverage["required_layers"]["dynamic_attachment"] is True
    assert coverage["required_layers"]["ordinary_inline"] is False
    assert coverage["all_required_layers_present"] is False


def test_coverage_keeps_ordinary_and_dynamic_routing_layers_separate() -> None:
    """普通四象限不能被误算为 Dynamic，四个输入分层需各自有成对证据。"""

    profiles = {
        "ordinary": {
            "routing_layer": "ordinary",
            "paired": {"pair_count": 1},
            "pair_inputs": ["inline", "attachment"],
        },
        "writing-fair": {
            "routing_layer": "dynamic",
            "paired": {"pair_count": 1},
            "pair_inputs": ["inline", "attachment"],
        },
    }
    coverage = MODULE._coverage(profiles=profiles, requested_profiles=tuple(profiles))
    assert coverage["required_layers"] == {
        "ordinary_inline": True,
        "ordinary_attachment": True,
        "dynamic_inline": True,
        "dynamic_attachment": True,
    }
    assert coverage["all_required_layers_present"] is True


def test_source_digest_adapter_rejects_missing_or_mixed_batch_identity() -> None:
    """验证正式审计不能把不同源码指纹的profile拼成一个Q1结论。"""

    assert MODULE._source_digests({"source_homogeneity": {"status": "passed", "digests": ["a"]}}) == {"a"}
    assert MODULE._source_digests({"source_homogeneity": {"status": "passed", "digests": ["a", "b"]}}) == {"a", "b"}
    assert MODULE._source_digests({"source_homogeneity": {"status": "not_passed", "digests": ["a"]}}) == set()


def test_cross_profile_source_gate_ignores_expected_skill_and_harness_differences(tmp_path: Path) -> None:
    """验证跨 profile 门只比较共享运行时/模型，不把不同 Skill 和题集误判为混批。"""

    reports: list[Path] = []
    for profile, skill_name, e2e_digest in (
        ("writing-fair", "writing-for-agents", "e2e-writing"),
        ("codebase", "codebase-design", "e2e-codebase"),
        ("diagnosing-positive", "diagnosing-bugs", "e2e-diagnosing"),
    ):
        path = tmp_path / f"{profile}.json"
        path.write_text(
            json.dumps(
                {
                    "certification_fingerprints": {
                        "backend/app/dynamic_tasks/agent.py": "runtime",
                        "frontend-enterprise/e2e/agent-quality-q1-" + profile + ".ts": e2e_digest,
                        "scripts/run_agent_quality_q1_browser_regression.py": "runner",
                    },
                    "source_model_config_id": "model",
                    "provider_endpoint": "https://provider.invalid",
                    "model": "model-v1",
                    "temperature": 0.2,
                    "max_output_tokens": 16384,
                    "capability_checksum": "capability",
                    "upstream_skills_revision": "skills-rev",
                    "skill": {"name": skill_name},
                    "upstream_skill_source_checksums": {"SKILL.md": skill_name},
                }
            ),
            encoding="utf-8",
        )
        reports.append(path)

    batches = {
        profile: {
            "runs": [{"evidence_file": str(path)}],
            "source_homogeneity": {"status": "passed", "digests": [profile]},
        }
        for profile, path in zip(("writing-fair", "codebase", "diagnosing-positive"), reports)
    }
    assert MODULE._cross_profile_core_source_is_homogeneous(
        batches=batches,
        requested_profiles=("writing-fair", "codebase", "diagnosing-positive"),
    ) is True
    assert len(MODULE._core_source_digests(batches["writing-fair"])[1]) == 1


def test_cross_profile_source_gate_ignores_legacy_generic_writing_harness(tmp_path: Path) -> None:
    """通用 writing E2E 文件名没有 profile 后缀时，也不能污染共享核心摘要。"""

    reports: list[Path] = []
    for profile, harness in (
        ("writing-fair", "frontend-enterprise/e2e/agent-quality-q1.live.fullstack.e2e.ts"),
        ("codebase", "frontend-enterprise/e2e/agent-quality-q1-codebase.live.fullstack.e2e.ts"),
        ("diagnosing-positive", "frontend-enterprise/e2e/agent-quality-q1-diagnosing-positive.live.fullstack.e2e.ts"),
    ):
        path = tmp_path / f"{profile}.json"
        path.write_text(
            json.dumps(
                {
                    "certification_fingerprints": {
                        "backend/app/dynamic_tasks/agent.py": "runtime",
                        harness: f"e2e-{profile}",
                        "scripts/run_agent_quality_q1_browser_regression.py": "runner",
                    },
                    "source_model_config_id": "model",
                    "provider_endpoint": "https://provider.invalid",
                    "model": "model-v1",
                    "temperature": 0.2,
                    "max_output_tokens": 16384,
                    "capability_checksum": "capability",
                    "upstream_skills_revision": "skills-rev",
                }
            ),
            encoding="utf-8",
        )
        reports.append(path)

    batches = {
        profile: {
            "runs": [{"evidence_file": str(path)}],
            "source_homogeneity": {"status": "passed", "digests": [profile]},
        }
        for profile, path in zip(("writing-fair", "codebase", "diagnosing-positive"), reports)
    }
    assert MODULE._cross_profile_core_source_is_homogeneous(
        batches=batches,
        requested_profiles=("writing-fair", "codebase", "diagnosing-positive"),
    ) is True


def test_core_source_metadata_gaps_are_reported_fail_closed(tmp_path: Path) -> None:
    """缺少模型采样元数据时只报告缺口，不能静默补默认值。"""

    report = tmp_path / "missing-model-metadata.json"
    report.write_text(
        json.dumps(
            {
                "certification_fingerprints": {"backend/app/dynamic_tasks/agent.py": "x"},
                "source_model_config_id": "model",
                "provider_endpoint": "https://provider.invalid",
                "model": "model-v1",
                "capability_checksum": "capability",
                "upstream_skills_revision": "skills-rev",
            }
        ),
        encoding="utf-8",
    )
    gaps = MODULE._core_source_metadata_gaps({"runs": [{"evidence_file": str(report)}]})
    assert gaps == ["max_output_tokens", "temperature"]
