"""
@Time       : 2026/08/22
@Author     : zhanglp8181
@File       : aggregate_q1_blind_review.py
@CallChain  : 独立匿名角色评分 → 身份仲裁键 → Q1盲评汇总证据
@Description: 校验独立盲评角色覆盖并在审计阶段还原配对，不把盲评观察冒充预注册发布门禁。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "manuals" / "evidence"
PACKAGE_PATH = EVIDENCE_DIR / "q1-current-source-blind-review-package.json"
KEY_PATH = EVIDENCE_DIR / "q1-current-source-blind-review-key.json"
ROLE_PATHS = {
    "product_value": EVIDENCE_DIR / "q1-current-source-blind-role-product.json",
    "runtime_contract": EVIDENCE_DIR / "q1-current-source-blind-role-runtime.json",
    "skill_method_and_prompt": EVIDENCE_DIR / "q1-current-source-blind-role-skill.json",
    "attachment_evidence_and_safety": EVIDENCE_DIR / "q1-current-source-blind-role-attachment.json",
}
OUTPUT_PATH = EVIDENCE_DIR / "q1-current-source-blind-review-report.json"
BATCH_PATHS = {
    "current-source": {
        "package": PACKAGE_PATH,
        "key": KEY_PATH,
        "roles": ROLE_PATHS,
        "output": OUTPUT_PATH,
    },
    "release-candidate": {
        "package": EVIDENCE_DIR / "q1-release-candidate-blind-review-package.json",
        "key": EVIDENCE_DIR / "q1-release-candidate-blind-review-key.json",
        "roles": {
            "product_value": EVIDENCE_DIR / "q1-release-candidate-blind-role-product.json",
            "runtime_contract": EVIDENCE_DIR / "q1-release-candidate-blind-role-runtime.json",
            "skill_method_and_prompt": EVIDENCE_DIR / "q1-release-candidate-blind-role-skill.json",
            "attachment_evidence_and_safety": EVIDENCE_DIR / "q1-release-candidate-blind-role-attachment.json",
        },
        "output": EVIDENCE_DIR / "q1-release-candidate-blind-review-report.json",
    },
    "current-final": {
        "package": EVIDENCE_DIR / "q1-current-final-blind-review-package.json",
        "key": EVIDENCE_DIR / "q1-current-final-blind-review-key.json",
        "roles": {
            "product_value": EVIDENCE_DIR / "q1-current-final-blind-role-product.json",
            "runtime_contract": EVIDENCE_DIR / "q1-current-final-blind-role-runtime.json",
            "skill_method_and_prompt": EVIDENCE_DIR / "q1-current-final-blind-role-skill.json",
            "attachment_evidence_and_safety": EVIDENCE_DIR / "q1-current-final-blind-role-attachment.json",
        },
        "output": EVIDENCE_DIR / "q1-current-final-blind-review-report.json",
    },
}


def main(argv: list[str] | None = None) -> int:
    """按显式批次校验四个角色的匿名评分并生成配对观察报告。"""

    parser = argparse.ArgumentParser(description="汇总指定 Q1 批次的四角色匿名盲评")
    parser.add_argument(
        "--batch",
        choices=tuple(BATCH_PATHS),
        default="current-source",
        help="要汇总的盲评包批次；默认保持历史 current-source 输出兼容",
    )
    args = parser.parse_args(argv)
    paths = BATCH_PATHS[args.batch]
    package_path = paths["package"]
    key_path = paths["key"]
    role_paths = paths["roles"]
    output_path = paths["output"]
    package = _load_json(package_path)
    key = _load_json(key_path)
    package_ids = _package_ids(package)
    role_scores = {
        role: _validate_role_file(path, role=role, package=package, package_ids=package_ids)
        for role, path in role_paths.items()
    }
    identity = _identity_index(key, package_ids=package_ids)
    report = _build_report(
        package,
        key,
        identity=identity,
        role_scores=role_scores,
        package_path=package_path,
        key_path=key_path,
        role_paths=role_paths,
    )
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(output_path.relative_to(ROOT)),
        "roles": report["role_coverage"],
        "paired_count": report["paired_observations"]["pair_count"],
        "release_gate": report["release_gate"],
    }, ensure_ascii=False))
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    """读取对象型 JSON，缺失或非对象时立即失败，避免静默生成空报告。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取盲评证据: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"盲评证据必须是 JSON object: {path}")
    return value


def _package_ids(package: dict[str, Any]) -> set[str]:
    """返回匿名包中唯一答案 ID，并校验包仍处于预期 schema。"""

    if package.get("schema_version") != "q1-blind-review-package-v1":
        raise SystemExit("匿名包 schema_version 不匹配")
    records = package.get("records")
    if not isinstance(records, list) or not records:
        raise SystemExit("匿名包没有 records")
    ids = [str(item.get("blind_id") or "") for item in records if isinstance(item, dict)]
    if len(ids) != len(records) or any(not item for item in ids) or len(set(ids)) != len(ids):
        raise SystemExit("匿名包 blind_id 缺失或重复")
    return set(ids)


def _validate_role_file(
    path: Path,
    *,
    role: str,
    package: dict[str, Any],
    package_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """校验一个角色只评分匿名包内的每条记录且分值在 0..10。"""

    value = _load_json(path)
    if value.get("role") != role:
        raise SystemExit(f"角色文件声明不匹配: {path}")
    if value.get("package_created_at") != package.get("created_at"):
        raise SystemExit(f"角色文件与匿名包时间不一致: {path}")
    rows = value.get("record_scores")
    if not isinstance(rows, list):
        raise SystemExit(f"角色文件缺少 record_scores: {path}")
    scores: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SystemExit(f"角色评分行不是 object: {path}")
        blind_id = str(row.get("blind_id") or "")
        score = row.get("score")
        if blind_id not in package_ids or blind_id in scores:
            raise SystemExit(f"角色评分 blind_id 非法或重复: {path}: {blind_id}")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 10:
            raise SystemExit(f"角色评分必须在 0..10: {path}: {blind_id}")
        scores[blind_id] = {
            "score": float(score),
            "rationale_short": str(row.get("rationale_short") or ""),
        }
    missing = package_ids - set(scores)
    if missing:
        raise SystemExit(f"角色评分缺失 {len(missing)} 条: {path}")
    return scores


def _identity_index(key: dict[str, Any], *, package_ids: set[str]) -> dict[str, dict[str, Any]]:
    """在盲评完成后读取仲裁键，并要求每个匿名答案恰有一个身份记录。"""

    if key.get("schema_version") != "q1-blind-review-key-v1":
        raise SystemExit("盲评 key schema_version 不匹配")
    rows = key.get("records")
    if not isinstance(rows, list):
        raise SystemExit("盲评 key 没有 records")
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SystemExit("盲评 key 记录不是 object")
        blind_id = str(row.get("blind_id") or "")
        if blind_id not in package_ids or blind_id in index:
            raise SystemExit(f"盲评 key blind_id 非法或重复: {blind_id}")
        variant = str(row.get("variant") or "")
        if variant not in {"control", "treatment"}:
            raise SystemExit(f"盲评 key variant 非法: {blind_id}")
        index[blind_id] = row
    if set(index) != package_ids:
        raise SystemExit("盲评 key 与匿名包 ID 集合不一致")
    return index


def _build_report(
    package: dict[str, Any],
    key: dict[str, Any],
    *,
    identity: dict[str, dict[str, Any]],
    role_scores: dict[str, dict[str, dict[str, Any]]],
    package_path: Path,
    key_path: Path,
    role_paths: dict[str, Path],
) -> dict[str, Any]:
    """合并角色评分并计算每个固定 pair 的方向性观察，不替换 hidden rubric。"""

    rows: list[dict[str, Any]] = []
    for blind_id, key_row in identity.items():
        row_scores = {
            role: values[blind_id]["score"] for role, values in role_scores.items()
        }
        rows.append({
            "blind_id": blind_id,
            "pair_id": str(key_row.get("pair_id") or ""),
            "profile": str(key_row.get("profile") or ""),
            "scenario": str(key_row.get("scenario") or ""),
            "input_mode": _input_mode(key_row),
            "variant": str(key_row.get("variant") or ""),
            "role_scores": row_scores,
            "composite_mean": round(mean(row_scores.values()), 3),
        })
    pair_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pair_rows[row["pair_id"]].append(row)
    paired: list[dict[str, Any]] = []
    for pair_id, pair in sorted(pair_rows.items()):
        if len(pair) != 2 or {item["variant"] for item in pair} != {"control", "treatment"}:
            raise SystemExit(f"pair 必须恰有 control/treatment 各一条: {pair_id}")
        control = next(item for item in pair if item["variant"] == "control")
        treatment = next(item for item in pair if item["variant"] == "treatment")
        role_deltas = {
            role: round(treatment["role_scores"][role] - control["role_scores"][role], 3)
            for role in role_scores
        }
        paired.append({
            "pair_id": pair_id,
            "profile": control["profile"],
            "scenario": control["scenario"],
            "input_mode": control["input_mode"],
            "control_blind_id": control["blind_id"],
            "treatment_blind_id": treatment["blind_id"],
            "control_composite": control["composite_mean"],
            "treatment_composite": treatment["composite_mean"],
            "composite_delta": round(
                treatment["composite_mean"] - control["composite_mean"], 3
            ),
            "role_deltas": role_deltas,
        })

    role_summary = {
        role: _role_summary(rows, role=role) for role in role_scores
    }
    return {
        "schema_version": "q1-blind-review-report-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "review_status": "completed_supplementary_blind_observation",
        "package": str(package_path.relative_to(ROOT)),
        "package_sha256": sha256(package_path.read_bytes()).hexdigest(),
        "identity_key": str(key_path.relative_to(ROOT)),
        "identity_key_sha256": sha256(key_path.read_bytes()).hexdigest(),
        "role_files": {
            role: {
                "path": str(path.relative_to(ROOT)),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for role, path in role_paths.items()
        },
        "role_coverage": {
            "required_roles": list(role_paths),
            "completed_roles": list(role_scores),
            "role_count": len(role_scores),
            "scored_records_per_role": len(rows),
            "missing_count": 0,
            "independent_roles_gate": "passed",
        },
        "role_summary": role_summary,
        "paired_observations": {
            "pair_count": len(paired),
            "pairs": paired,
            "overall_composite_control_mean": round(
                mean(item["control_composite"] for item in paired), 3
            ),
            "overall_composite_treatment_mean": round(
                mean(item["treatment_composite"] for item in paired), 3
            ),
            "overall_composite_delta": round(
                mean(item["composite_delta"] for item in paired), 3
            ),
            "treatment_non_decrease_count": sum(
                item["composite_delta"] >= 0 for item in paired
            ),
            "treatment_gain_at_least_one_point_count": sum(
                item["composite_delta"] >= 1 for item in paired
            ),
        },
        "mechanical_hard_gate": {
            "all_package_records_passed": all(
                bool(item.get("mechanical_hard_gate_passed"))
                for item in package.get("records", ())
                if isinstance(item, dict)
            ),
            "failure_count": sum(
                not bool(item.get("mechanical_hard_gate_passed"))
                for item in package.get("records", ())
                if isinstance(item, dict)
            ),
        },
        "release_gate": {
            "status": "not_passed",
            "reason": (
                "独立角色覆盖已完成，但盲评分值是0..10的补充观察，不等同隐藏100分量表；"
                "Q1预注册总分/方法分阈值、事实安全历史非劣、成本预算和历史同题部署仍未闭合。"
            ),
            "q1_claim_allowed": False,
        },
        "rows": rows,
    }


def _input_mode(key_row: dict[str, Any]) -> str:
    """把 key 中场景名压缩为报告分层，不修改盲评答案内容。"""

    scenario = str(key_row.get("scenario") or "")
    if "attachment" in scenario:
        return "attachment"
    if "inline" in scenario:
        return "inline"
    return "diagnosis"


def _role_summary(rows: list[dict[str, Any]], *, role: str) -> dict[str, Any]:
    """按角色和变体汇总盲评分，供审计查看而非自动放宽门禁。"""

    values = [float(row["role_scores"][role]) for row in rows]
    by_variant: dict[str, list[float]] = defaultdict(list)
    by_profile: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_variant[row["variant"]].append(float(row["role_scores"][role]))
        by_profile[row["profile"]][row["variant"]].append(float(row["role_scores"][role]))
    return {
        "mean": round(mean(values), 3),
        "control_mean": round(mean(by_variant["control"]), 3),
        "treatment_mean": round(mean(by_variant["treatment"]), 3),
        "treatment_minus_control": round(
            mean(by_variant["treatment"]) - mean(by_variant["control"]), 3
        ),
        "by_profile": {
            profile: {
                "control_mean": round(mean(variants["control"]), 3),
                "treatment_mean": round(mean(variants["treatment"]), 3),
                "delta": round(
                    mean(variants["treatment"]) - mean(variants["control"]), 3
                ),
            }
            for profile, variants in sorted(by_profile.items())
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
