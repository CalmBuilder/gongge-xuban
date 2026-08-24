"""
@Time       : 2026/08/23
@Author     : zhanglp8181
@File       : build_skill_sampling_manifest.py
@CallChain  : otherpro/skills 只读清单 → 分层/风险标记 → Skill 闭环抽样计划证据
@Description: 只读取上游 Skill 文件生成可审计的分层清单；不导入、不执行、不修改上游内容。
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = ROOT / "otherpro" / "skills"
SKILLS_ROOT = UPSTREAM_ROOT / "skills"
DEFAULT_OUTPUT = ROOT / "docs" / "manuals" / "evidence" / "skill-sampling-manifest-current.json"
EXECUTION_MARKERS = (
    "bash",
    "shell",
    "python",
    "node",
    "javascript",
    "npm",
    "pytest",
    "curl",
    "git",
    "exec",
    "command",
    "run",
)
SAMPLE_QUOTAS = {"engineering": 12, "productivity": 5, "misc": 3}
ANCHOR_SKILLS = {"codebase-design", "diagnosing-bugs", "writing-for-agents"}


def _task_family(name: str, category: str) -> str:
    """按 Skill 的公开名称映射匹配题族，避免用同一道题强行测试无关能力。"""

    explicit = {
        "code-review": "review",
        "diagnosing-bugs": "diagnosis",
        "triage": "diagnosis",
        "wayfinder": "diagnosis",
        "grill-with-docs": "diagnosis",
        "codebase-design": "architecture",
        "domain-modeling": "architecture",
        "improve-codebase-architecture": "architecture",
        "prototype": "architecture",
        "research": "research",
        "implement": "implementation",
        "tdd": "implementation",
        "to-spec": "implementation",
        "to-tickets": "implementation",
        "resolving-merge-conflicts": "maintenance",
        "setup-matt-pocock-skills": "setup",
        "wizard": "setup",
        "writing-for-agents": "writing",
        "grill-me": "facilitation",
        "grilling": "facilitation",
        "teach": "teaching",
        "to-questionnaire": "facilitation",
        "wait-what": "facilitation",
        "handoff": "handoff",
        "ask-matt": "consultation",
        "git-guardrails-claude-code": "tooling",
        "migrate-to-shoehorn": "tooling",
        "scaffold-exercises": "tooling",
        "setup-pre-commit": "tooling",
    }
    return explicit.get(name, "general-productivity" if category == "productivity" else "general-engineering")


def _sha256(path: Path) -> str:
    """计算上游文件摘要，确保后续抽样不会混用漂移内容。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frontmatter_name(path: Path) -> str | None:
    """读取 SKILL.md 的 name 字段；异常或缺失时返回空，绝不执行正文。"""

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    frontmatter = text[3:end]
    match = re.search(r"^name:\s*([^#\n]+?)\s*$", frontmatter, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _risk_flags(skill_dir: Path) -> list[str]:
    """按字面标记可能涉及命令或代码的内容，标记仅用于人工审查而非执行。"""

    text_parts = []
    for path in skill_dir.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            try:
                text_parts.append(path.read_text(encoding="utf-8", errors="replace").lower())
            except OSError:
                continue
    text = "\n".join(text_parts)
    return [marker for marker in EXECUTION_MARKERS if re.search(rf"\b{re.escape(marker)}\b", text)]


def _skill_record(skill_file: Path) -> dict[str, object]:
    """生成单个 Skill 的路径、checksum、分层和风险记录。"""

    skill_dir = skill_file.parent
    relative_dir = skill_dir.relative_to(SKILLS_ROOT)
    category = relative_dir.parts[0] if relative_dir.parts else "uncategorized"
    files = sorted(
        str(path.relative_to(skill_dir))
        for path in skill_dir.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    name = _frontmatter_name(skill_file)
    symlink_found = any(path.is_symlink() for path in skill_dir.rglob("*"))
    eligible = bool(name) and category not in {"deprecated", "in-progress"} and not symlink_found
    return {
        "name": name,
        "category": category,
        "task_family": _task_family(name or "", category),
        "relative_path": str(skill_dir.relative_to(ROOT)),
        "skill_sha256": _sha256(skill_file),
        "files": files,
        "risk_flags": _risk_flags(skill_dir),
        "candidate_eligible": eligible,
        "eligibility_reason": (
            "frontmatter_name_and_stable_path"
            if eligible
            else "manual_review_required_or_nonproduction_category"
        ),
    }


def _source_revision() -> str:
    """读取只读上游 git revision，失败时返回明确的 unavailable 标记。"""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=UPSTREAM_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def _select_sample(records: list[dict[str, object]], revision: str) -> list[dict[str, object]]:
    """按类别配额和固定 hash 选择 20 个候选，保证已验证 Skill 作为锚点保留。"""

    eligible = [record for record in records if record["candidate_eligible"]]
    selected: list[dict[str, object]] = []
    for category, quota in SAMPLE_QUOTAS.items():
        category_records = [record for record in eligible if record["category"] == category]
        anchors = [record for record in category_records if record["name"] in ANCHOR_SKILLS]
        ranked = sorted(
            (record for record in category_records if record not in anchors),
            key=lambda record: hashlib.sha256(
                f"{revision}:{record['name']}".encode("utf-8")
            ).hexdigest(),
        )
        selected.extend(anchors[:quota])
        selected.extend(ranked[: max(0, quota - len(anchors))])
    if len(selected) != sum(SAMPLE_QUOTAS.values()):
        raise RuntimeError("Skill 抽样配额不足，不能生成冻结样本")
    return sorted(selected, key=lambda record: (str(record["category"]), str(record["name"])))


def build_manifest() -> dict[str, object]:
    """构建冻结 revision 下的只读 Skill 分层清单，不产生安装或运行副作用。"""

    if not SKILLS_ROOT.is_dir():
        raise RuntimeError(f"Skill 上游目录不存在: {SKILLS_ROOT}")
    skill_files = sorted(SKILLS_ROOT.rglob("SKILL.md"))
    records = [_skill_record(path) for path in skill_files]
    revision = _source_revision()
    sample = _select_sample(records, revision)
    category_counts = Counter(str(record["category"]) for record in records)
    eligible_count = sum(bool(record["candidate_eligible"]) for record in records)
    return {
        "schema_version": "skill-sampling-manifest-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_root": str(UPSTREAM_ROOT.relative_to(ROOT)),
        "source_revision": revision,
        "execution_policy": "read_only_inventory_no_skill_content_execution",
        "skill_count": len(records),
        "eligible_candidate_count": eligible_count,
        "category_counts": dict(sorted(category_counts.items())),
        "sampling_gate": {
            "mechanical_closed_loop_target": 0.95,
            "quality_gain_requires_task_skill_match": True,
            "sample_freeze_required": True,
            "note": "95% 是适用 Skill×任务的机械闭环观察门槛，不是任意 Skill 的质量提升保证。",
        },
        "frozen_sample": {
            "sample_size": len(sample),
            "category_quotas": SAMPLE_QUOTAS,
            "selection_method": "category_quota_then_sha256_rank_with_verified_anchors",
            "anchors": sorted(ANCHOR_SKILLS),
            "skills": [record["name"] for record in sample],
        },
        "skills": records,
    }


def main() -> int:
    """生成 Skill 抽样清单文件并输出数量摘要。"""

    parser = argparse.ArgumentParser(description="生成 otherpro/skills 的只读分层抽样清单")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = build_manifest()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"manifest={output.relative_to(ROOT)} skills={manifest['skill_count']} "
        f"eligible={manifest['eligible_candidate_count']} categories={manifest['category_counts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
