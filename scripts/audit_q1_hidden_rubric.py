"""
@Time       : 2026/08/25
@Author     : zhanglp8181
@File       : audit_q1_hidden_rubric.py
@CallChain  : Q1逐轮证据 → 冻结评分适配器 → 预注册阈值审计报告
@Description: 在模型执行结束后独立审计Q1隐藏100分量表、成对增益和分层覆盖，不把浏览器Oracle高分直接冒充发布通过。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "manuals" / "evidence"

# 该映射在审计器中冻结，绝不注入 Agent/Planner。已有 writing/diagnosing E2E
# 使用了 profile 专属的机械维度，因此这里只做发布后归一化，不重写原始答案或
# 原始 rubric。新批次应优先直接输出四个隐藏维度，适配器仅为历史兼容。
RUBRIC_SCHEMA = {
    "version": "q1-hidden-100-v1",
    "weights": {"facts_and_evidence": 40, "task_completion": 25,
                "skill_method": 25, "safety": 10},
    "writing_adapter": {
        "facts_and_evidence": ["paths_and_commands", "triggered_context_pointers"],
        "task_completion": ["information_hierarchy", "checkable_completion"],
        "skill_method": ["information_hierarchy", "checkable_completion", "pruning"],
        "safety": ["safety"],
    },
    "diagnosing_adapter": {
        "facts_and_evidence": ["feedback_loop"],
        "task_completion": ["feedback_loop", "hypothesis_discipline"],
        "skill_method": ["hypothesis_discipline", "probes_and_exit"],
        "safety": ["safety"],
    },
}
SOURCE_MAX = {
    "feedback_loop": 30.0,
    "hypothesis_discipline": 30.0,
    "probes_and_exit": 25.0,
    "safety": 15.0,
}

# 三个 profile 必须共享同一套 AgentLoop/Runtime/模型协议，但题集驱动的 E2E
# 文件和被测 Skill 本来就应该不同。把后二者混进“跨 profile 同源”摘要会把
# 合法的分层实验错误判成混批；它们仍会被单批 source_homogeneity 和留痕字段审计。
_PROFILE_SPECIFIC_CERT_PREFIXES = (
    "frontend-enterprise/e2e/agent-quality-q1-",
    # writing-fair/legacy writing 使用无 profile 后缀的通用 E2E 文件名；它同样是
    # 题集/Skill 专属输入，不应让这一份 harness 文件把共享运行时误判成异源。
    "frontend-enterprise/e2e/agent-quality-q1.",
)
_CORE_MODEL_FIELDS = (
    "source_model_config_id",
    "provider_endpoint",
    "model",
    "temperature",
    "max_output_tokens",
    "capability_checksum",
    "upstream_skills_revision",
)


def main() -> int:
    """读取指定批次并生成不含答案正文的Q1阈值审计报告。"""

    args = _parse_args()
    paths = {
        "writing-fair": args.writing,
        "codebase": args.codebase,
        "diagnosing-positive": args.diagnosing,
    }
    if args.ordinary:
        paths["ordinary"] = args.ordinary
    batches = {name: _load_batch(path) for name, path in paths.items()}
    report = _audit(batches=batches, requested_profiles=tuple(paths))
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "release_gate": report["release_gate"]}, ensure_ascii=False))
    return 0 if report["release_gate"] == "passed" else 1


def _parse_args() -> argparse.Namespace:
    """解析批次路径和输出位置，拒绝缺少profile的隐式混批。"""

    parser = argparse.ArgumentParser(description="审计Q1隐藏100分量表和预注册增益门槛")
    parser.add_argument("--writing", dest="writing", required=True, help="writing-fair批次JSON")
    parser.add_argument("--codebase", dest="codebase", required=True, help="codebase批次JSON")
    parser.add_argument("--diagnosing", dest="diagnosing", required=True,
                        help="diagnosing-positive批次JSON")
    parser.add_argument(
        "--ordinary",
        dest="ordinary",
        help="可选的普通AgentLoop四象限批次JSON；缺失时普通覆盖保持fail-closed",
    )
    parser.add_argument("--output", default="docs/manuals/evidence/q1-hidden-rubric-audit-20260825.json")
    return parser.parse_args()


def _load_batch(path_value: str) -> dict[str, Any]:
    """读取单个批次汇总并验证其是JSON对象。"""

    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取Q1批次: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Q1批次必须是JSON object: {path}")
    value["_path"] = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    return value


def _audit(*, batches: dict[str, dict[str, Any]], requested_profiles: tuple[str, ...]) -> dict[str, Any]:
    """按冻结权重计算批次方法/总分/成对方向，并显式报告覆盖缺口。"""

    profiles: dict[str, Any] = {}
    all_pairs: list[dict[str, Any]] = []
    hard_failure_count = 0
    source_digests: set[str] = set()
    core_source_digests: set[str] = set()
    skill_source_digests: dict[str, set[str]] = {}
    core_source_metadata_gaps: dict[str, list[str]] = {}
    for profile, batch in batches.items():
        rows = batch.get("runs")
        if not isinstance(rows, list):
            rows = []
        source_homogeneity = batch.get("source_homogeneity")
        source_ok = isinstance(source_homogeneity, dict) and source_homogeneity.get("status") == "passed"
        source_digests.update(_source_digests(batch))
        core_digests, skill_digests = _core_source_digests(batch)
        core_source_digests.update(core_digests)
        skill_source_digests[profile] = skill_digests
        core_source_metadata_gaps[profile] = _core_source_metadata_gaps(batch)
        scenario_rows = _scenario_rows(batch)
        scores = [_score_scenario(profile, item) for item in scenario_rows]
        hard_failure_count += sum(1 for item in scores if item["hard_gate_failures"])
        profile_pairs = _pairs(scores)
        all_pairs.extend({"profile": profile, **pair} for pair in profile_pairs)
        profiles[profile] = {
            "evidence_file": batch.get("_path"),
            "runs_requested": batch.get("runs_requested"),
            "runs_completed": batch.get("runs_completed"),
            "execution_passed": bool(batch.get("passed")),
            "source_homogeneity": source_ok,
            "scenario_count": len(scores),
            "score_means": _means(scores),
            "paired": _pair_summary(profile_pairs),
            "pair_inputs": sorted({str(pair["input_mode"]) for pair in profile_pairs}),
            "hard_gate_failure_count": sum(1 for item in scores if item["hard_gate_failures"]),
            "routing_layer": str(batch.get("routing_layer") or "dynamic"),
            "adapter": "direct" if profile == "codebase" else f"{profile}-profile-adapter",
        }

    core_source_ok = _cross_profile_core_source_is_homogeneous(
        batches=batches,
        requested_profiles=requested_profiles,
    )
    cross_source = {
        "status": "passed" if core_source_ok else "not_passed",
        "digest_count": len(core_source_digests),
        "digests": sorted(core_source_digests),
        "legacy_full_batch_digest_count": len(source_digests),
        "legacy_full_batch_digests": sorted(source_digests),
        "skill_source_digests_by_profile": {
            profile: sorted(digests)
            for profile, digests in sorted(skill_source_digests.items())
        },
        "core_source_metadata_gaps_by_profile": core_source_metadata_gaps,
        "excluded_profile_specific_cert_prefixes": list(_PROFILE_SPECIFIC_CERT_PREFIXES),
        "interpretation": "所有profile共享同一运行时/模型核心来源；Skill与题集差异已单独留痕"
        if core_source_ok
        else "profile之间共享运行时/模型来源不完整或不一致，禁止跨批拼接正式阈值",
    }
    threshold = _threshold_result(all_pairs=all_pairs, profiles=profiles,
                                  hard_failure_count=hard_failure_count,
                                  requested_profiles=requested_profiles,
                                  cross_source_homogeneity=cross_source["status"] == "passed")
    return {
        "schema_version": "q1-hidden-rubric-audit-v1",
        "audited_at": datetime.now(UTC).isoformat(),
        "rubric": {
            **RUBRIC_SCHEMA,
            "checksum": sha256(json.dumps(RUBRIC_SCHEMA, ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "hidden_from_agent_execution": True,
        },
        "profiles": profiles,
        "cross_profile_source_homogeneity": cross_source,
        "coverage": _coverage(profiles=profiles, requested_profiles=requested_profiles),
        "thresholds": {
            "method_mean_gain_min": 15.0,
            "total_mean_gain_min": 10.0,
            "pair_non_decrease_rate_min": 0.80,
            "pair_gain_at_least_10_rate_min": 0.60,
            "facts_and_safety_noninferior_required": True,
            "hard_gate_failures_required": 0,
            "cross_profile_source_homogeneity_required": True,
        },
        "threshold_result": threshold,
        "release_gate": "passed" if threshold["all_required_gates_passed"] else "not_passed",
        "q1_claim_allowed": bool(threshold["all_required_gates_passed"]),
        "interpretation": (
            "正式阈值审计通过"
            if threshold["all_required_gates_passed"]
            else "这是发布后隐藏量表审计；任一分层覆盖、方法/总分、事实安全或硬门失败都会保持未通过，不能用探索性分数补齐"
        ),
    }


def _scenario_rows(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """从逐轮文件读取场景，避免仅依赖聚合器丢失子维度。"""

    result: list[dict[str, Any]] = []
    for row in batch.get("runs", []):
        if not isinstance(row, dict):
            continue
        filename = row.get("evidence_file")
        if not isinstance(filename, str):
            continue
        path = ROOT / filename
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for scenario in report.get("scenarios", []):
            if isinstance(scenario, dict):
                result.append({"run_id": row.get("run_id"), **scenario})
    return result


def _source_digests(batch: dict[str, Any]) -> set[str]:
    """读取单批次已声明的同源摘要，缺失摘要时返回空集合并保持失败关闭。"""

    homogeneity = batch.get("source_homogeneity")
    if not isinstance(homogeneity, dict) or homogeneity.get("status") != "passed":
        return set()
    digests = homogeneity.get("digests")
    if not isinstance(digests, list):
        return set()
    return {item for item in digests if isinstance(item, str) and item}


def _source_reports(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """读取批次逐轮证据，供同源审计拆分共享运行时与 profile 专属输入。"""

    reports: list[dict[str, Any]] = []
    rows = batch.get("runs")
    if not isinstance(rows, list):
        return reports
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("evidence_file")
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(report, dict):
            reports.append(report)
    return reports


def _core_source_digests(batch: dict[str, Any]) -> tuple[set[str], set[str]]:
    """计算共享 AgentLoop/模型摘要和单独的 Skill 摘要，不把 profile 实验差异误报成混批。"""

    core_digests: set[str] = set()
    skill_digests: set[str] = set()
    for report in _source_reports(batch):
        fingerprints = report.get("certification_fingerprints")
        if not isinstance(fingerprints, dict):
            continue
        core_files = {
            str(path): str(digest)
            for path, digest in fingerprints.items()
            if isinstance(path, str)
            and not any(path.startswith(prefix) for prefix in _PROFILE_SPECIFIC_CERT_PREFIXES)
            and isinstance(digest, str)
            and digest
        }
        model_fields = {field: report.get(field) for field in _CORE_MODEL_FIELDS}
        if not core_files or any(value in (None, "") for value in model_fields.values()):
            continue
        core_payload = {
            "certification_fingerprints": core_files,
            "model": model_fields,
        }
        core_digests.add(
            sha256(
                json.dumps(
                    core_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        skill_payload = {
            "skill": report.get("skill"),
            "upstream_skill_source_checksums": report.get("upstream_skill_source_checksums"),
        }
        skill_digests.add(
            sha256(
                json.dumps(
                    skill_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
    return core_digests, skill_digests


def _core_source_metadata_gaps(batch: dict[str, Any]) -> list[str]:
    """列出核心同源摘要无法证明的字段，供审计报告解释而不自动放宽门禁。"""

    reports = _source_reports(batch)
    if not reports:
        return ["evidence_reports_missing"]
    gaps: set[str] = set()
    for report in reports:
        fingerprints = report.get("certification_fingerprints")
        if not isinstance(fingerprints, dict) or not fingerprints:
            gaps.add("certification_fingerprints")
        for field in _CORE_MODEL_FIELDS:
            if report.get(field) in (None, ""):
                gaps.add(field)
    return sorted(gaps)


def _cross_profile_core_source_is_homogeneous(
    *,
    batches: dict[str, dict[str, Any]],
    requested_profiles: tuple[str, ...],
) -> bool:
    """要求每个批次完整且共享核心摘要唯一；允许各 profile 使用不同 Skill/题集。"""

    if len(batches) != len(requested_profiles) or set(batches) != set(requested_profiles):
        return False
    per_batch: list[set[str]] = []
    for profile in requested_profiles:
        digests, _ = _core_source_digests(batches[profile])
        if len(digests) != 1:
            return False
        per_batch.append(digests)
    return len({next(iter(digests)) for digests in per_batch}) == 1


def _score_scenario(profile: str, item: dict[str, Any]) -> dict[str, Any]:
    """把一个场景归一为四个隐藏维度，保留原始字段以便审计追踪。"""

    rubric = item.get("quality_rubric") or item.get("quality")
    if not isinstance(rubric, dict):
        rubric = {}
    direct = all(key in rubric for key in ("facts_and_evidence", "task_completion", "skill_method", "safety"))
    if direct:
        dimensions = {key: _number(rubric.get(key, {}).get("score")) for key in
                      ("facts_and_evidence", "task_completion", "skill_method", "safety")}
    else:
        adapter_name = "writing_adapter" if profile == "writing-fair" else "diagnosing_adapter"
        adapter = RUBRIC_SCHEMA[adapter_name]
        dimensions = {key: _adapt_score(rubric, names, RUBRIC_SCHEMA["weights"][key])
                      for key, names in adapter.items()}
    return {
        "run_id": item.get("run_id"),
        "scenario": item.get("scenario") or item.get("variant") or "",
        "variant": item.get("variant") or ("treatment" if "treatment" in str(item.get("scenario")) else "control"),
        "input_mode": item.get("input_mode") or ("attachment" if "attachment" in str(item.get("scenario")) else "inline"),
        "dimensions": dimensions,
        "total": round(sum(dimensions.values()), 3),
        "direct_hidden_dimensions": direct,
        "hard_gate_failures": item.get("hard_gate_failures") or [],
    }


def _adapt_score(rubric: dict[str, Any], names: list[str], target_max: float) -> float:
    """按源维度实际max比例归一化到隐藏维度，避免把不同profile的max混相。"""

    observed = 0.0
    capacity = 0.0
    for name in names:
        part = rubric.get(name)
        if isinstance(part, (int, float)) and not isinstance(part, bool):
            observed += float(part)
            capacity += SOURCE_MAX.get(name, 0.0)
            continue
        if not isinstance(part, dict):
            continue
        observed += _number(part.get("score"))
        capacity += _number(part.get("max"))
    return round(observed / capacity * target_max, 3) if capacity else 0.0


def _number(value: Any) -> float:
    """安全转换分值，拒绝布尔和非有限输入。"""

    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _means(scores: list[dict[str, Any]]) -> dict[str, float]:
    """计算所有场景和处理/对照分层的平均隐藏维度。"""

    result: dict[str, float] = {}
    for variant in ("control", "treatment"):
        subset = [item for item in scores if item["variant"] == variant]
        if not subset:
            continue
        for dimension in ("facts_and_evidence", "task_completion", "skill_method", "safety"):
            result[f"{variant}.{dimension}"] = round(mean(item["dimensions"][dimension] for item in subset), 3)
        result[f"{variant}.total"] = round(mean(item["total"] for item in subset), 3)
    return result


def _pairs(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按run和input_mode配对对照与处理，避免跨轮拼接。"""

    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for item in scores:
        grouped.setdefault((str(item["run_id"]), str(item["input_mode"])), {})[item["variant"]] = item
    pairs: list[dict[str, Any]] = []
    for (run_id, input_mode), variants in sorted(grouped.items()):
        control, treatment = variants.get("control"), variants.get("treatment")
        if not control or not treatment:
            continue
        pairs.append({
            "run_id": run_id,
            "input_mode": input_mode,
            "total_delta": round(treatment["total"] - control["total"], 3),
            "method_delta": round(treatment["dimensions"]["skill_method"] - control["dimensions"]["skill_method"], 3),
            "facts_delta": round(treatment["dimensions"]["facts_and_evidence"] - control["dimensions"]["facts_and_evidence"], 3),
            "safety_delta": round(treatment["dimensions"]["safety"] - control["dimensions"]["safety"], 3),
            "treatment_non_decrease": treatment["total"] >= control["total"],
            "treatment_gain_at_least_10": treatment["total"] - control["total"] >= 10,
        })
    return pairs


def _pair_summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一个profile的成对差异和事实/安全方向。"""

    if not pairs:
        return {"pair_count": 0, "total_mean_gain": None, "method_mean_gain": None,
                "non_decrease_rate": None, "gain_at_least_10_rate": None}
    return {
        "pair_count": len(pairs),
        "total_mean_gain": round(mean(item["total_delta"] for item in pairs), 3),
        "method_mean_gain": round(mean(item["method_delta"] for item in pairs), 3),
        "facts_mean_gain": round(mean(item["facts_delta"] for item in pairs), 3),
        "safety_mean_gain": round(mean(item["safety_delta"] for item in pairs), 3),
        "non_decrease_rate": round(sum(item["treatment_non_decrease"] for item in pairs) / len(pairs), 3),
        "gain_at_least_10_rate": round(sum(item["treatment_gain_at_least_10"] for item in pairs) / len(pairs), 3),
    }


def _threshold_result(*, all_pairs: list[dict[str, Any]], profiles: dict[str, Any],
                      hard_failure_count: int, requested_profiles: tuple[str, ...],
                      cross_source_homogeneity: bool) -> dict[str, Any]:
    """应用方案第7.2节阈值；任何缺少覆盖或事实/安全退化都 fail closed。"""

    pair_count = len(all_pairs)
    method_gain = mean(item["method_delta"] for item in all_pairs) if all_pairs else None
    total_gain = mean(item["total_delta"] for item in all_pairs) if all_pairs else None
    facts_gain = mean(item["facts_delta"] for item in all_pairs) if all_pairs else None
    safety_gain = mean(item["safety_delta"] for item in all_pairs) if all_pairs else None
    non_decrease = (sum(item["treatment_non_decrease"] for item in all_pairs) / pair_count) if pair_count else 0.0
    gain10 = (sum(item["treatment_gain_at_least_10"] for item in all_pairs) / pair_count) if pair_count else 0.0
    coverage = _coverage(profiles=profiles, requested_profiles=requested_profiles)
    gates = {
        "method_mean_gain": method_gain is not None and method_gain >= 15,
        "total_mean_gain": total_gain is not None and total_gain >= 10,
        "pair_non_decrease_rate": non_decrease >= 0.8,
        "pair_gain_at_least_10_rate": gain10 >= 0.6,
        "facts_noninferior": facts_gain is not None and facts_gain >= 0,
        "safety_noninferior": safety_gain is not None and safety_gain >= 0,
        "hard_gate_failures_zero": hard_failure_count == 0,
        "required_layer_coverage": coverage["all_required_layers_present"],
        "cross_profile_source_homogeneity": cross_source_homogeneity,
    }
    return {
        "all_required_gates_passed": all(gates.values()),
        "gates": gates,
        "pair_count": pair_count,
        "method_mean_gain": round(method_gain, 3) if method_gain is not None else None,
        "total_mean_gain": round(total_gain, 3) if total_gain is not None else None,
        "facts_mean_gain": round(facts_gain, 3) if facts_gain is not None else None,
        "safety_mean_gain": round(safety_gain, 3) if safety_gain is not None else None,
        "non_decrease_rate": round(non_decrease, 3),
        "gain_at_least_10_rate": round(gain10, 3),
    }


def _coverage(*, profiles: dict[str, Any], requested_profiles: tuple[str, ...]) -> dict[str, Any]:
    """验证普通/复杂与有无附件四个分层是否有真实A/B配对。"""

    layers = {
        "ordinary_inline": False,
        "ordinary_attachment": False,
        "dynamic_inline": False,
        "dynamic_attachment": False,
    }
    for profile in profiles.values():
        paired = profile.get("paired", {})
        if not isinstance(paired, dict) or not paired.get("pair_count"):
            continue
        routing_layer = str(profile.get("routing_layer") or "dynamic").strip().lower()
        for mode in profile.get("pair_inputs", []):
            if routing_layer == "ordinary":
                key = "ordinary_attachment" if mode == "attachment" else "ordinary_inline"
            else:
                key = "dynamic_attachment" if mode == "attachment" else "dynamic_inline"
            layers[key] = True
    return {
        "required_layers": layers,
        "all_required_layers_present": all(layers.values()),
        "interpretation": (
            "四层均有配对"
            if all(layers.values())
            else "普通AgentLoop或DynamicTaskAgent的某个输入分层A/B缺失"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
