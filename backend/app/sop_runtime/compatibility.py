"""
@Time       : 2026/07/22 05:20
@Author     : zhanglp8181
@File       : compatibility.py
@CallChain  : 已发布 Skill 查询 → build_compatibility_report → Markdown/发布门禁
@Description: 汇总旧版 SkillCard 到统一 SOP 元模型的编译兼容性和阻断诊断。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.sop_runtime.contracts import RuntimeContract
from app.sop_runtime.definition import HumanTaskKind, HumanTaskNode
from app.sop_runtime.legacy_skill_card_adapter import (
    SopCompilationError,
    compile_legacy_skill_card,
)


class CompatibilityStatus(StrEnum):
    """单个 SOP 定义的编译兼容状态。"""

    STRUCTURALLY_READY = "structurally_ready"
    COMPILES_WITH_WARNINGS = "compiles_with_warnings"
    BLOCKED = "blocked"


class CompatibilityCandidate(RuntimeContract):
    """兼容扫描所需的最小已发布 SOP 输入。"""

    skill_id: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    content: dict[str, Any]


class CompatibilityEntry(RuntimeContract):
    """单个 SOP 的编译结果摘要。"""

    skill_id: str
    version: str
    status: CompatibilityStatus
    node_count: int = Field(ge=0)
    node_type_counts: dict[str, int] = Field(default_factory=dict)
    participant_scope_counts: dict[str, int] = Field(default_factory=dict)
    candidate_source_counts: dict[str, int] = Field(default_factory=dict)
    warning_codes: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    checksum: str | None = None


class CompatibilityReport(RuntimeContract):
    """一批 SOP 定义的稳定兼容性报告。"""

    total: int = Field(ge=0)
    compilable: int = Field(ge=0)
    structurally_ready: int = Field(ge=0)
    compiles_with_warnings: int = Field(ge=0)
    blocked: int = Field(ge=0)
    entries: tuple[CompatibilityEntry, ...]


def build_compatibility_report(
    candidates: Iterable[CompatibilityCandidate],
) -> CompatibilityReport:
    """逐项编译候选定义，并在单项失败时继续收集完整批次结果。"""

    entries: list[CompatibilityEntry] = []
    for candidate in sorted(candidates, key=lambda item: (item.skill_id, item.version)):
        try:
            compiled = compile_legacy_skill_card(candidate.content)
        except SopCompilationError as exc:
            entries.append(
                CompatibilityEntry(
                    skill_id=candidate.skill_id,
                    version=candidate.version,
                    status=CompatibilityStatus.BLOCKED,
                    node_count=len(candidate.content.get("nodes") or []),
                    error_codes=tuple(sorted({item.code for item in exc.diagnostics})),
                )
            )
            continue
        type_counts = Counter(str(node.type) for node in compiled.nodes)
        structured_work_items = [
            node
            for node in compiled.nodes
            if isinstance(node, HumanTaskNode)
            and node.config.kind is HumanTaskKind.STRUCTURED_WORK_ITEM
        ]
        participant_scope_counts = Counter(
            node.config.participant_scope_resolver.value for node in structured_work_items
        )
        candidate_source_counts: Counter[str] = Counter()
        for node in structured_work_items:
            if node.config.candidate_user_ids:
                candidate_source_counts["direct_user"] += 1
            if node.config.candidate_role_codes:
                candidate_source_counts["business_role"] += 1
        warning_codes = tuple(sorted({diagnostic.code for diagnostic in compiled.diagnostics}))
        entries.append(
            CompatibilityEntry(
                skill_id=candidate.skill_id,
                version=candidate.version,
                status=(
                    CompatibilityStatus.COMPILES_WITH_WARNINGS
                    if warning_codes
                    else CompatibilityStatus.STRUCTURALLY_READY
                ),
                node_count=len(compiled.nodes),
                node_type_counts=dict(sorted(type_counts.items())),
                participant_scope_counts=dict(sorted(participant_scope_counts.items())),
                candidate_source_counts=dict(sorted(candidate_source_counts.items())),
                warning_codes=warning_codes,
                checksum=compiled.checksum,
            )
        )
    structurally_ready = sum(
        entry.status is CompatibilityStatus.STRUCTURALLY_READY for entry in entries
    )
    compiles_with_warnings = sum(
        entry.status is CompatibilityStatus.COMPILES_WITH_WARNINGS for entry in entries
    )
    return CompatibilityReport(
        total=len(entries),
        compilable=structurally_ready + compiles_with_warnings,
        structurally_ready=structurally_ready,
        compiles_with_warnings=compiles_with_warnings,
        blocked=len(entries) - structurally_ready - compiles_with_warnings,
        entries=tuple(entries),
    )


def render_compatibility_markdown(report: CompatibilityReport) -> str:
    """将兼容报告渲染为可直接写入规划文档的稳定 Markdown 表格。"""

    lines = [
        f"- 总数：{report.total}",
        f"- 可编译：{report.compilable}",
        f"- 结构就绪：{report.structurally_ready}",
        f"- 可编译但需升级：{report.compiles_with_warnings}",
        f"- 阻断：{report.blocked}",
        "",
        "| SOP | 版本 | 结果 | 节点规范化统计 | 参与范围 | 候选来源 | 警告/错误 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in report.entries:
        type_summary = (
            ", ".join(f"{node_type}={count}" for node_type, count in entry.node_type_counts.items())
            or "-"
        )
        diagnostics = ", ".join((*entry.warning_codes, *entry.error_codes)) or "-"
        scope_summary = (
            ", ".join(
                f"{resolver}={count}" for resolver, count in entry.participant_scope_counts.items()
            )
            or "-"
        )
        source_summary = (
            ", ".join(
                f"{source}={count}" for source, count in entry.candidate_source_counts.items()
            )
            or "-"
        )
        lines.append(
            f"| `{entry.skill_id}` | `{entry.version}` | {entry.status.value} | "
            f"{type_summary} | {scope_summary} | {source_summary} | {diagnostics} |"
        )
    return "\n".join(lines)
