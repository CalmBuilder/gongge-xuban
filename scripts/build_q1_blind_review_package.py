"""
@Time       : 2026/08/22
@Author     : zhanglp8181
@File       : build_q1_blind_review_package.py
@CallChain  : 当前 Q1 逐轮 JSON → 匿名答案包/审计映射 → 独立角色盲评入口
@Description: 从当前源码真实浏览器报告生成不暴露 A/B 身份的盲评材料；不执行附件内容，也不改写原始证据。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs/manuals/evidence"
PACKAGE_PATH = EVIDENCE_DIR / "q1-current-source-blind-review-package.json"
KEY_PATH = EVIDENCE_DIR / "q1-current-source-blind-review-key.json"

REPORT_GLOBS_BY_BATCH = {
    "current-source": {
        "writing-fair": "agent-quality-q1-writing-fair-q1-writing-fair-current-source-oracle-fixed-0[1-5].json",
        "codebase-design": "agent-quality-q1-codebase-q1-codebase-current-source-oracle-fixed-0[1-5].json",
        "diagnosing-positive": "agent-quality-q1-diagnosing-positive-q1-diagnosing-positive-current-source-metadata-ready-0[1-5].json",
    },
    "release-candidate": {
        "writing-fair": "agent-quality-q1-writing-fair-q1-writing-fair-release-candidate-r9-0[1-5].json",
        "codebase-design": "agent-quality-q1-codebase-q1-codebase-release-candidate-r8-0[1-5].json",
        "diagnosing-positive": "agent-quality-q1-diagnosing-positive-q1-diagnosing-positive-release-candidate-r11-0[1-5].json",
    },
    "current-final": {
        "writing-fair": "agent-quality-q1-writing-fair-q1-writing-fair-current-final-r31-0[1-5].json",
        "codebase-design": "agent-quality-q1-codebase-q1-codebase-current-final-r31-0[1-5].json",
        "diagnosing-positive": "agent-quality-q1-diagnosing-positive-q1-diagnosing-positive-current-final-r32-0[1-5].json",
    },
}
OUTPUT_STEMS = {
    "current-source": "q1-current-source-blind-review",
    "release-candidate": "q1-release-candidate-blind-review",
    "current-final": "q1-current-final-blind-review",
}


def _sha256(value: str) -> str:
    """计算盲评任务文本或答案的稳定 SHA-256，不把凭据写入输出。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deduplicate_repeated_text(value: str) -> str:
    """去除事件日志偶发的完整双写，只保留一次用户题面。"""

    if len(value) % 2 == 0 and value[: len(value) // 2] == value[len(value) // 2 :]:
        return value[: len(value) // 2]
    return value


def _task_text(scenario: dict[str, Any]) -> str:
    """从用户消息事件提取题面，缺失时返回稳定的不可评分标记。"""

    for event in scenario.get("events", []):
        if event.get("event_type") != "user_message_received":
            continue
        value = event.get("message") or (event.get("data") or {}).get("message")
        if isinstance(value, str) and value.strip():
            return _deduplicate_repeated_text(value.strip())
    return "[题面未从证据事件提取；该记录不能进入盲评分数]"


def _redact_for_blind(answer: str) -> str:
    """去除会直接暴露受管 Skill 或会话身份的字面量，但不改事实段落。"""

    redacted = answer
    for name in ("writing-for-agents", "codebase-design", "diagnosing-bugs"):
        redacted = redacted.replace(name, "[受管指导]")
    redacted = re.sub(r"\b(?:SkillUse|skill_use|Skill|skill)\b", "指导资料", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"\b(?:sopinst|execplan|gsuse|genskill|gsrev)_[A-Za-z0-9]+\b", "[opaque-id]", redacted)
    return redacted


def _pair_label(profile: str, scenario: dict[str, Any], index: int) -> str:
    """把同一轮的 control/treatment 映射到不暴露处理组的配对标签。"""

    name = str(scenario.get("scenario") or "")
    if name.endswith("-control") or name.endswith("-treatment"):
        name = name.rsplit("-", 1)[0]
    return f"{profile}:{name or 'published-check'}:r{index}"


def _collect(
    report_globs: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取三组当前源码报告，生成匿名记录和只供审计保存的身份映射。"""

    records: list[dict[str, Any]] = []
    keys: list[dict[str, Any]] = []
    for profile, pattern in report_globs.items():
        reports = sorted(EVIDENCE_DIR.glob(pattern))
        if len(reports) != 5:
            raise RuntimeError(f"{profile} requires five reports, found {len(reports)}")
        for run_index, report_path in enumerate(reports, start=1):
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for scenario_index, scenario in enumerate(report.get("scenarios", []), start=1):
                task = _task_text(scenario)
                answer = _redact_for_blind(str(scenario.get("raw_answer") or ""))
                source_key = f"{profile}|{run_index}|{scenario_index}|{scenario.get('variant')}"
                blind_id = f"blind-{_sha256(source_key)[:16]}"
                pair_id = _pair_label(profile, scenario, run_index)
                gate_passed = not bool(scenario.get("hard_gate_failures"))
                records.append(
                    {
                        "blind_id": blind_id,
                        "pair_id": pair_id,
                        "task_text": task,
                        "task_sha256": _sha256(task),
                        "input_mode": scenario.get("input_mode", "diagnosis"),
                        "attachment_sha256": scenario.get("attachment_sha256"),
                        "answer": answer,
                        "answer_sha256": _sha256(answer),
                        "mechanical_hard_gate_passed": gate_passed,
                        "review": {
                            "product_value": None,
                            "runtime_contract": None,
                            "skill_method_and_prompt": None,
                            "attachment_evidence_and_safety": None,
                            "reason": None,
                        },
                    }
                )
                keys.append(
                    {
                        "blind_id": blind_id,
                        "pair_id": pair_id,
                        "profile": profile,
                        "run_index": run_index,
                        "scenario_index": scenario_index,
                        "variant": scenario.get("variant"),
                        "scenario": scenario.get("scenario"),
                        "source_report": str(report_path.relative_to(ROOT)),
                        "mechanical_quality": scenario.get("quality_rubric") or scenario.get("quality"),
                    }
                )
    records.sort(key=lambda item: item["blind_id"])
    keys.sort(key=lambda item: item["blind_id"])
    return records, keys


def main(argv: list[str] | None = None) -> None:
    """按显式批次生成匿名盲评包和隔离身份映射，并报告样本完整性。"""

    parser = argparse.ArgumentParser(description="从指定 Q1 证据批次生成匿名盲评包")
    parser.add_argument(
        "--batch",
        choices=tuple(REPORT_GLOBS_BY_BATCH),
        default="current-source",
        help="要读取的五轮报告批次；默认保持历史 current-source 输出兼容",
    )
    args = parser.parse_args(argv)
    report_globs = REPORT_GLOBS_BY_BATCH[args.batch]
    output_stem = OUTPUT_STEMS[args.batch]
    package_path = EVIDENCE_DIR / f"{output_stem}-package.json"
    key_path = EVIDENCE_DIR / f"{output_stem}-key.json"
    records, keys = _collect(report_globs)
    created_at = (
        "2026-08-22T09:05:00+08:00"
        if args.batch == "current-source"
        else datetime.now(UTC).isoformat()
    )
    package_path.write_text(
        json.dumps(
            {
                "schema_version": "q1-blind-review-package-v1",
                "created_at": created_at,
                "review_status": "prepared_pending_independent_roles",
                "batch": args.batch,
                "answer_redaction": "仅替换 Skill 名称/术语和 opaque session/revision id；不删除事实、建议、证据或安全段落。原始答案仍留在逐轮证据文件，由审计映射追溯。",
                "source_batches": list(report_globs),
                "review_instruction": "只依据 task_text、attachment_sha256、answer 和允许的机械硬门结果评分；不得读取身份映射文件，也不得从答案猜测 A/B 后修改评分尺度。",
                "required_roles": [
                    "product_value",
                    "runtime_contract",
                    "skill_method_and_prompt",
                    "attachment_evidence_and_safety",
                ],
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    key_path.write_text(
        json.dumps(
            {
                "schema_version": "q1-blind-review-key-v1",
                "warning": "仅供审计仲裁；不得提供给盲评角色。",
                "records": keys,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"batch={args.batch} package={package_path.relative_to(ROOT)} "
        f"key={key_path.relative_to(ROOT)} blind_records={len(records)} "
        f"pairs={len({item['pair_id'] for item in records})}"
    )


if __name__ == "__main__":
    main()
