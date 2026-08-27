"""
@Time       : 2026/08/21
@Author     : zhanglp8181
@File       : run_agent_quality_q1_certification_batch.py
@CallChain  : Q1认证批Runner → run_agent_quality_q1_browser_regression.py → Chromium/AgentLoop
@Description: 串行运行固定Skill与随机顺序的Q1配对批，固化每轮退出码和脱敏质量证据。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import signal
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"
LAUNCHER = ROOT / "scripts" / "run_agent_quality_q1_browser_regression.py"
EVIDENCE_DIR = ROOT / "docs" / "manuals" / "evidence"


def main() -> int:
    """按固定种子串行运行Q1批次并写出无密钥的汇总报告。"""

    args = _parse_args()
    if args.runs < 1 or args.runs > 20:
        raise SystemExit("--runs 必须在 1..20 之间")
    if not BACKEND_PYTHON.is_file():
        raise SystemExit(f"缺少后端虚拟环境解释器: {BACKEND_PYTHON}")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    batch_slug = _safe_batch_slug(args.seed_prefix)
    for index in range(1, args.runs + 1):
        seed = f"{args.seed_prefix}-{index:02d}"
        run_id = f"{args.profile}-cert-{index:02d}"
        evidence_name = f"agent-quality-q1-{args.profile}-{batch_slug}-{index:02d}.json"
        runtime_dir = Path(f"/tmp/gongge-q1-{args.profile}-cert-{index:02d}")
        port = args.base_port + index - 1
        env = os.environ.copy()
        env.update(
            {
                "Q1_PROFILE": args.profile,
                "Q1_ORDER_SEED": seed,
                "Q1_CERTIFICATION_RUN_ID": run_id,
                "Q1_DIAGNOSING_EVIDENCE_FILE": evidence_name,
                "Q1_DIAGNOSING_POSITIVE_EVIDENCE_FILE": evidence_name,
                "Q1_CODEBASE_EVIDENCE_FILE": evidence_name,
                "Q1_AGENT_QUALITY_EVIDENCE_FILE": evidence_name,
                "Q1_PLAIN_EVIDENCE_FILE": evidence_name,
                "Q1_PLAIN_SIMPLE_EVIDENCE_FILE": evidence_name,
                "Q1_CROSS_TURN_EVIDENCE_FILE": evidence_name,
                "Q1_UNRELATED_EVIDENCE_FILE": evidence_name,
                "FULLSTACK_E2E_PORT": str(port),
                "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
            }
        )
        if args.profile == "writing-fair":
            env["Q1_WRITING_BENCHMARK"] = "fair-v2"
        command = [str(BACKEND_PYTHON), str(LAUNCHER)]
        print(f"[Q1] run={run_id} seed={seed} port={port} profile={args.profile}", flush=True)
        completed_code = _run_isolated_child(command, env=env)
        report_path = EVIDENCE_DIR / evidence_name
        report = _read_report(report_path)
        rows.append(
            {
                "run_id": run_id,
                "seed": seed,
                "port": port,
                "evidence_file": str(report_path.relative_to(ROOT)),
                "exit_code": completed_code,
                "test_status": report.get("test_status", "missing"),
                "source_fingerprint_digest": _source_fingerprint_digest(report),
                "scenarios": _scenario_summary(report),
            }
        )
        if args.stop_on_failure and completed_code != 0:
            break

    aggregate = {
        "suite": "Q1 certification batch",
        "profile": args.profile,
        "routing_layer": "ordinary" if args.profile == "ordinary" else "dynamic",
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "runs_requested": args.runs,
        "runs_completed": len(rows),
        "seed_prefix": args.seed_prefix,
        "stop_on_failure": args.stop_on_failure,
        "batch_runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "quality_gain_threshold_enforced": False,
        "runs": rows,
    }
    passed = len(rows) == args.runs and all(
        row["exit_code"] == 0
        and row["test_status"] == "passed"
        and bool(row["scenarios"])
        and not any(item.get("hard_gate_failures") for item in row["scenarios"])
        for row in rows
    )
    source_digests = sorted(
        {
            str(row["source_fingerprint_digest"])
            for row in rows
            if str(row["source_fingerprint_digest"])
        }
    )
    source_homogeneous = len(rows) == args.runs and len(source_digests) == 1
    aggregate["source_homogeneity"] = {
        "status": "passed" if source_homogeneous else "not_passed",
        "digest_count": len(source_digests),
        "digests": source_digests,
        "interpretation": (
            "所有轮次使用相同源码/模型/Skill来源指纹"
            if source_homogeneous
            else "轮次缺少统一源码/模型/Skill来源指纹，不能合并成认证批"
        ),
    }
    aggregate["quality_summary"] = _quality_summary(rows)
    aggregate["passed"] = bool(passed and source_homogeneous)
    aggregate_path = _aggregate_report_path(profile=args.profile, batch_slug=batch_slug)
    aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[Q1] aggregate={aggregate_path} passed={aggregate['passed']}", flush=True)
    return 0 if aggregate["passed"] else 1


def _parse_args() -> argparse.Namespace:
    """解析有界批量参数，拒绝无限重跑或隐式改变认证 profile。"""

    parser = argparse.ArgumentParser(description="运行Q1真实浏览器认证批")
    parser.add_argument(
        "--profile",
        default="diagnosing",
        choices=(
            "diagnosing", "diagnosing-positive", "writing", "writing-fair", "codebase",
            "plain", "plain-simple", "ordinary", "cross-turn", "unrelated",
        ),
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed-prefix", default="q1-cert")
    parser.add_argument("--base-port", type=int, default=41600)
    parser.add_argument("--stop-on-failure", action="store_true")
    return parser.parse_args()


def _safe_batch_slug(seed_prefix: str) -> str:
    """把批次种子转换为稳定文件名，避免后续批次覆盖历史逐轮证据。"""

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", seed_prefix).strip("-._")
    return slug[:64] or "batch"


def _aggregate_report_path(*, profile: str, batch_slug: str) -> Path:
    """返回批次汇总路径；显式文件名用于防止不同批次覆盖历史审计证据。"""

    configured = os.environ.get("Q1_BATCH_EVIDENCE_FILE", "").strip()
    if configured:
        filename = Path(configured).name
        if not filename or filename in {".", ".."} or not filename.endswith(".json"):
            raise SystemExit("Q1_BATCH_EVIDENCE_FILE必须是当前证据目录下的.json文件名")
        return EVIDENCE_DIR / filename
    return EVIDENCE_DIR / f"agent-quality-q1-{profile}-{batch_slug}-certification-batch.json"


def _run_isolated_child(command: list[str], *, env: dict[str, str]) -> int:
    """以独立进程组运行单轮launcher，父进程中断时不遗留Chromium或全栈服务。"""

    child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    try:
        return child.wait()
    except BaseException:
        _terminate_child_group(child)
        raise
    finally:
        if child.poll() is None:
            _terminate_child_group(child)


def _terminate_child_group(child: subprocess.Popen[bytes]) -> None:
    """先终止整棵测试进程组，超时后升级为SIGKILL并等待组长退出。"""

    try:
        os.killpg(child.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        child.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    child.wait(timeout=10)


def _read_report(path: Path) -> dict[str, Any]:
    """只读取本批浏览器写出的JSON；缺失或损坏报告统一记为missing。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"test_status": "missing"}
    return value if isinstance(value, dict) else {"test_status": "invalid"}


def _scenario_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    """提取每格硬门、分数和执行耗时，不复制原始模型答案到汇总报告。"""

    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        return []
    summary: list[dict[str, Any]] = []
    for item in scenarios:
        if not isinstance(item, dict):
            continue
        rubric = item.get("quality_rubric")
        quality = item.get("quality")
        summary.append(
            {
                "scenario": item.get("scenario") or item.get("variant", ""),
                "hard_gate_failures": item.get("hard_gate_failures", []),
                "score": (
                    rubric.get("total")
                    if isinstance(rubric, dict)
                    else quality.get("total") if isinstance(quality, dict) else None
                ),
                "duration_ms": item.get("duration_ms"),
            }
        )
    return summary


def _source_fingerprint_digest(report: dict[str, Any]) -> str:
    """对不含密钥的源码、模型和 Skill 来源指纹做稳定摘要，防止混批。"""

    fields = (
        "certification_fingerprints",
        "source_model_config_id",
        "provider_endpoint",
        "model",
        "temperature",
        "max_output_tokens",
        "capability_checksum",
        "upstream_skills_revision",
        "upstream_skill_source_checksums",
    )
    payload = {field: report.get(field) for field in fields}
    if not isinstance(payload["certification_fingerprints"], dict):
        return ""
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _quality_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总本批四象限分数、硬门和成对方向，只作当前源码观察。"""

    by_scenario: dict[str, list[float]] = {}
    hard_failures: dict[str, int] = {}
    pair_deltas: list[dict[str, Any]] = []
    for row in rows:
        per_run: dict[str, dict[str, Any]] = {}
        for item in row.get("scenarios", []):
            if not isinstance(item, dict):
                continue
            scenario = str(item.get("scenario") or "")
            score = item.get("score")
            if scenario and isinstance(score, (int, float)) and not isinstance(score, bool):
                by_scenario.setdefault(scenario, []).append(float(score))
            if scenario and item.get("hard_gate_failures"):
                hard_failures[scenario] = hard_failures.get(scenario, 0) + 1
            if scenario:
                per_run[scenario] = item
        prefixes = {name.rsplit("-", 1)[0] for name in per_run if "-" in name}
        if "control" in per_run and "treatment" in per_run:
            prefixes.add("")
        for prefix in sorted(prefixes):
            control_name = f"{prefix}-control" if prefix else "control"
            treatment_name = f"{prefix}-treatment" if prefix else "treatment"
            control = per_run.get(control_name)
            treatment = per_run.get(treatment_name)
            if (
                isinstance(control, dict)
                and isinstance(treatment, dict)
                and isinstance(control.get("score"), (int, float))
                and isinstance(treatment.get("score"), (int, float))
            ):
                delta = float(treatment["score"]) - float(control["score"])
                pair_deltas.append(
                    {
                        "run_id": row.get("run_id"),
                        "input_mode": prefix or "published-check",
                        "control": control["score"],
                        "treatment": treatment["score"],
                        "delta": round(delta, 3),
                        "treatment_non_decrease": delta >= 0,
                    }
                )
    return {
        "by_scenario": {
            scenario: {
                "count": len(values),
                "mean": round(mean(values), 3),
                "hard_gate_failure_count": hard_failures.get(scenario, 0),
            }
            for scenario, values in sorted(by_scenario.items())
        },
        "pair_count": len(pair_deltas),
        "pair_deltas": pair_deltas,
        "treatment_non_decrease_count": sum(
            bool(item["treatment_non_decrease"]) for item in pair_deltas
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
