"""
@Time       : 2026/08/23
@Author     : zhanglp8181
@File       : run_skill_sampling_browser_batch.py
@CallChain  : 冻结 Skill 清单 → skill-sample 启动器 → 真实 Chromium/模型 → 机械闭环汇总
@Description: 对冻结候选执行普通/附件真实浏览器闭环；不执行 Skill 或附件中的命令，只消费平台回执。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "manuals" / "evidence" / "skill-sampling-manifest-current.json"
EVIDENCE_DIR = ROOT / "docs" / "manuals" / "evidence"
LAUNCHER = ROOT / "scripts" / "run_agent_quality_q1_browser_regression.py"
_ACTIVE_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_ACTIVE_LOCK = threading.Lock()


def _load_manifest() -> dict[str, Any]:
    """读取已冻结清单并验证样本结构，禁止临时改变样本集合。"""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sample = manifest.get("frozen_sample")
    if not isinstance(sample, dict) or int(sample.get("sample_size", 0)) != 20:
        raise RuntimeError("Skill 抽样清单不是预期的 20 个冻结样本")
    if manifest.get("execution_policy") != "read_only_inventory_no_skill_content_execution":
        raise RuntimeError("Skill 抽样清单执行策略不安全")
    names = [str(name) for name in sample.get("skills", [])]
    records = {
        str(record.get("name")): record
        for record in manifest.get("skills", [])
        if isinstance(record, dict) and record.get("candidate_eligible")
    }
    if len(names) != 20 or any(name not in records for name in names):
        raise RuntimeError("Skill 抽样清单候选记录不完整")
    return manifest


def _run_one(
    *,
    skill: dict[str, Any],
    input_mode: str,
    index: int,
    base_port: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """运行一个样本×输入形态的真实浏览器闭环并读取其硬门禁结果。"""

    name = str(skill["name"])
    relative_path = str(skill["relative_path"])
    skill_dir = ROOT / relative_path
    evidence_name = f"skill-sampling-{name}-{input_mode}-r1.json"
    runtime_dir = Path(f"/tmp/gongge-skill-sampling-{name}-{input_mode}-r1")
    env = os.environ.copy()
    env.update(
        {
            "Q1_PROFILE": "skill-sample",
            "Q1_SAMPLE_SKILL_DIR": str(skill_dir),
            "Q1_ONLY_SCENARIO": f"{input_mode}-treatment",
            "Q1_CERTIFICATION_RUN_ID": f"skill-sampling-{name}-{input_mode}-r1",
            "Q1_AGENT_QUALITY_EVIDENCE_FILE": evidence_name,
            "Q1_WRITING_BENCHMARK": "fair-v2",
            "Q1_SAMPLE_SKILL_NAME": name,
            "Q1_SAMPLE_TASK_FAMILY": str(skill.get("task_family") or "general-engineering"),
            "FULLSTACK_E2E_PORT": str(base_port + index),
            "FULLSTACK_E2E_RUNTIME_DIR": str(runtime_dir),
        }
    )
    command = [str(ROOT / "backend" / ".venv" / "bin" / "python"), str(LAUNCHER)]
    process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES[process.pid] = process
    try:
        try:
            _, _ = process.communicate(timeout=timeout_seconds)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            returncode = 124
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROCESSES.pop(process.pid, None)
    evidence_path = EVIDENCE_DIR / evidence_name
    evidence: dict[str, Any] = {}
    if evidence_path.is_file():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    scenarios = evidence.get("scenarios") if isinstance(evidence, dict) else []
    scenario = scenarios[0] if isinstance(scenarios, list) and scenarios else {}
    failures = scenario.get("hard_gate_failures") if isinstance(scenario, dict) else []
    return {
        "skill": name,
        "category": skill["category"],
        "input_mode": input_mode,
        "returncode": returncode,
        "evidence_file": str(evidence_path.relative_to(ROOT)),
        "hard_gate_failures": failures if isinstance(failures, list) else ["invalid_evidence"],
        "execution_status": (scenario.get("execution") or {}).get("status")
        if isinstance(scenario, dict)
        else None,
        "result_status": (scenario.get("result") or {}).get("status")
        if isinstance(scenario, dict)
        else None,
    }


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """终止一个样本及其浏览器/全栈子进程，避免人工暂停留下服务。"""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    """并发运行冻结样本并生成 fail-closed 机械闭环汇总。"""

    parser = argparse.ArgumentParser(description="运行冻结 Skill 样本的真实浏览器闭环")
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--base-port", type=int, default=None)
    args = parser.parse_args(argv)
    manifest = _load_manifest()
    sample_by_name = {
        str(record["name"]): record
        for record in manifest["skills"]
        if record.get("candidate_eligible")
    }
    sample_names = [str(name) for name in manifest["frozen_sample"]["skills"]]
    max_workers = max(1, args.max_workers or int(os.environ.get("SKILL_SAMPLE_MAX_WORKERS", "2")))
    timeout_seconds = max(
        300,
        args.timeout_seconds or int(os.environ.get("SKILL_SAMPLE_TIMEOUT_SECONDS", "2400")),
    )
    base_port = args.base_port or int(os.environ.get("SKILL_SAMPLE_BASE_PORT", "48100"))
    jobs = [
        (sample_by_name[name], input_mode, index)
        for index, (name, input_mode) in enumerate(
            (item for name in sample_names for item in ((name, "inline"), (name, "attachment")))
        )
    ]
    results: list[dict[str, Any]] = []
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
        executor.submit(
            _run_one,
            skill=skill,
            input_mode=input_mode,
            index=index,
            base_port=base_port,
            timeout_seconds=timeout_seconds,
        ): (skill["name"], input_mode)
        for skill, input_mode, index in jobs
    }
    try:
        for future in as_completed(futures):
            results.append(future.result())
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        with _ACTIVE_LOCK:
            active = list(_ACTIVE_PROCESSES.values())
        for process in active:
            _terminate_process(process)
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    results.sort(key=lambda item: (str(item["skill"]), str(item["input_mode"])))
    passed = [
        item
        for item in results
        if item["returncode"] == 0
        and not item["hard_gate_failures"]
        and item["execution_status"] == "succeeded"
        and item["result_status"] == "verified"
    ]
    report = {
        "schema_version": "skill-sampling-browser-batch-v1",
        "completed_at": datetime.now(UTC).isoformat(),
        "source_manifest": str(MANIFEST_PATH.relative_to(ROOT)),
        "sample_size": len(sample_names),
        "scenario_count": len(results),
        "passed_scenarios": len(passed),
        "observed_closed_loop_rate": len(passed) / len(results) if results else 0.0,
        "mechanical_target": 0.95,
        "quality_gain_evaluated": False,
        "results": results,
    }
    output = EVIDENCE_DIR / "skill-sampling-browser-batch-current-r1.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"summary={output.relative_to(ROOT)} passed={len(passed)}/{len(results)} "
        f"rate={report['observed_closed_loop_rate']:.3f} target={report['mechanical_target']:.2f}"
    )
    return 0 if len(passed) / len(results) >= report["mechanical_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
