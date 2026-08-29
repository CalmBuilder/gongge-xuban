"""
@Time       : 2026/08/29 12:00
@Author     : zhanglp8181
@File       : sync_plan.py
@CallChain  : 同步计划 CLI → 导入/中文化包校验 → 租户专家只读查询 → 可审计计划报告
@Description: 比较当前 Agency Agents 包与租户、历史导入基线，生成不写业务库的更新计划。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.agents.identity import agent_is_published
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    utc_now,
)
from app.experts.import_service import ExpertImportError, validate_admin
from app.experts.localization_package import load_and_verify_localization_package
from app.experts.localization_schema import LocalizedExpert
from app.experts.package import ImportPackageError, load_and_verify_package
from app.experts.schema import PreparedExpert


SessionFactory = Callable[[], Session]
SyncItemStatus = Literal[
    "new",
    "unchanged",
    "upstream_changed",
    "baseline_unknown",
    "source_removed",
    "duplicate_source_path",
]
LocalChangeState = Literal["clean", "modified", "unknown", "not_applicable"]

SOURCE_CODE = "agency-agents"
HIGH_RISK_SOURCE_TERMS = (
    "healthcare",
    "medical",
    "medication",
    "hipaa",
    "clinical",
    "diagnos",
    "privacy",
    "pii",
    "legal",
    "financial",
    "security",
    "safety",
    "parent care",
    "医疗",
    "健康",
    "用药",
    "隐私",
    "法律",
    "财务",
    "安全",
)


class ExpertSyncPlanError(ValueError):
    """同步计划输入、租户或历史基线不满足安全要求。"""


class SyncPlanItem(BaseModel):
    """一个上游路径与租户当前专家之间的只读差异。"""

    model_config = ConfigDict(frozen=True)

    upstream_path: str
    status: SyncItemStatus
    agent_id: str | None = None
    name: str | None = None
    current_source_sha256: str | None = None
    baseline_source_sha256: str | None = None
    observed_updated_at: str | None = None
    observed_profile_revision: int | None = None
    observed_content_sha256: str | None = None
    local_change: LocalChangeState = "not_applicable"
    review_flags: list[str] = Field(default_factory=list)
    message: str | None = None


class SyncPlanResult(BaseModel):
    """可审计的专家同步计划，不包含任何数据库写入结果。"""

    model_config = ConfigDict(frozen=True)

    operation: Literal["plan"] = "plan"
    tenant_id: str
    source_batch_id: str
    source_commit: str
    baseline_batch_id: str | None = None
    baseline_localization_batch_id: str | None = None
    started_at: str
    finished_at: str | None = None
    result_path: Path
    report_path: Path
    items: list[SyncPlanItem] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """返回按主状态聚合的计划数量。"""

        return dict(Counter(item.status for item in self.items))

    @property
    def local_change_counts(self) -> dict[str, int]:
        """返回按本地内容基线状态聚合的数量。"""

        return dict(Counter(item.local_change for item in self.items))

    @property
    def review_count(self) -> int:
        """返回至少需要人工复核一个风险面的专家数量。"""

        return sum(bool(item.review_flags) for item in self.items)


def _iso_now() -> str:
    """返回无时区格式的当前 UTC 时间，沿用项目数据库时间语义。"""

    return utc_now().isoformat()


def _atomic_write(path: Path, content: bytes) -> None:
    """以同目录临时文件原子写入本地计划产物。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_result(result: SyncPlanResult) -> None:
    """写入 JSON 计划结果，不触碰业务数据库。"""

    _atomic_write(
        result.result_path,
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _render_report(result: SyncPlanResult) -> str:
    """渲染供管理员阅读的同步计划摘要。"""

    lines = [
        "# Agency Agents 专家只读同步计划",
        "",
        f"- 租户：`{result.tenant_id}`",
        f"- 当前提交：`{result.source_commit}`",
        f"- 基线导入批次：`{result.baseline_batch_id or '未提供'}`",
        f"- 计划条目数：{len(result.items)}",
        f"- 风险/人工复核项：{result.review_count}",
        "",
        "## 主状态",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(result.counts.items()))
    lines.extend(["", "## 本地内容基线", ""])
    lines.extend(
        f"- {key}: {value}" for key, value in sorted(result.local_change_counts.items())
    )
    lines.extend(["", "## 明细", ""])
    for item in result.items:
        flags = "、".join(item.review_flags) or "无"
        details = [item.status, f"local={item.local_change}", f"review={flags}"]
        if item.message:
            details.append(item.message)
        lines.append(f"- `{item.upstream_path}`：" + "；".join(details))
    return "\n".join(lines) + "\n"


def _expert_maps(experts: list[PreparedExpert]) -> dict[str, PreparedExpert]:
    """把已校验包转换为唯一来源路径映射并拒绝重复路径。"""

    result: dict[str, PreparedExpert] = {}
    for expert in experts:
        path = expert.parsed.upstream_path
        if path in result:
            raise ExpertSyncPlanError(f"Duplicate upstream path in package: {path}")
        result[path] = expert
    return result


def _metadata(row: AgentProfile) -> dict[str, object]:
    """以安全字典形式读取专家元数据。"""

    return row.metadata_json if isinstance(row.metadata_json, dict) else {}


def profile_content_sha256(row: AgentProfile) -> str:
    """计算专家运行时字段摘要，供 apply 防止计划检查与写入之间发生竞态。"""

    content = json.dumps(
        [row.name, row.description, row.persona_prompt],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _local_change_state(
    row: AgentProfile,
    localized_baseline: LocalizedExpert | None,
) -> LocalChangeState:
    """比较运行时字段与最后已接受的中文基线，无法证明时保持保守未知。"""

    metadata = _metadata(row)
    accepted = (
        metadata.get("expert_last_accepted_name"),
        metadata.get("expert_last_accepted_description"),
        metadata.get("expert_last_accepted_persona_prompt"),
    )
    if all(isinstance(value, str) for value in accepted):
        current = (row.name, row.description, row.persona_prompt)
        return "clean" if current == accepted else "modified"
    if localized_baseline is not None:
        expected = (
            localized_baseline.localized_name,
            localized_baseline.localized_description,
            localized_baseline.localized_prompt,
        )
        current = (row.name, row.description, row.persona_prompt)
        return "clean" if current == expected else "modified"
    original = (row.original_name, row.original_description, row.original_persona_prompt)
    if all(isinstance(value, str) for value in original):
        current = (row.name, row.description, row.persona_prompt)
        if current == original:
            return "clean"
    return "unknown"


def _review_flags(
    expert: PreparedExpert,
    status: SyncItemStatus,
    governance_flags: list[str] | None = None,
) -> list[str]:
    """从源码、声明能力和分类变更中提取需要人工确认的风险旗标。"""

    corpus = " ".join(
        [
            expert.parsed.upstream_path,
            expert.parsed.name,
            expert.parsed.description,
            expert.parsed.source_markdown,
        ]
    ).casefold()
    flags: list[str] = []
    if expert.translation.high_risk or any(term in corpus for term in HIGH_RISK_SOURCE_TERMS):
        flags.append("high_risk_content")
    if expert.parsed.services:
        flags.append("declared_external_service")
    if expert.capability_manifest.capability_type in {"P2", "P3"}:
        flags.append("external_capability")
    if expert.capability_manifest.unresolved_requirements:
        flags.append("unresolved_capability")
    if expert.parsed.category_original == "research":
        flags.append("taxonomy_mapping_required")
    if status in {"new", "upstream_changed", "baseline_unknown"} and (
        expert.translation.name_zh == expert.parsed.name
        or expert.translation.markdown_zh == expert.parsed.source_markdown
    ):
        flags.append("translation_package_required")
    flags.extend(governance_flags or [])
    return flags


def _governance_flags(
    db: Session,
    tenant_id: str,
    rows: list[AgentProfile],
) -> dict[str, list[str]]:
    """读取发布、绑定和使用事实，供后续同步审核而不修改任何关系。"""

    flags = {row.id: [] for row in rows}
    agent_ids = set(flags)
    for row in rows:
        if agent_is_published(row):
            flags[row.id].append("published")
    if not agent_ids:
        return flags
    relation_queries = (
        (AgentResourceBinding, "resource_bound"),
        (AgentModelBinding, "model_bound"),
        (AgentUsage, "used"),
        (ChatSession, "has_session"),
    )
    for model, flag in relation_queries:
        statement = select(model.agent_id).where(
            model.tenant_id == tenant_id,
            model.agent_id.in_(agent_ids),
        )
        for agent_id in db.exec(statement).all():
            flags[str(agent_id)].append(flag)
    return flags


def _load_baselines(
    package_dir: Path | None,
    localization_dir: Path | None,
    tenant_id: str,
) -> tuple[
    dict[str, PreparedExpert],
    str | None,
    dict[str, LocalizedExpert],
    str | None,
]:
    """加载可选的旧导入包和旧中文包，均只执行文件校验。"""

    baseline: dict[str, PreparedExpert] = {}
    baseline_batch: str | None = None
    baseline_commit: str | None = None
    localized: dict[str, LocalizedExpert] = {}
    localized_batch: str | None = None
    try:
        if package_dir is not None:
            manifest, experts = load_and_verify_package(package_dir, tenant_id)
            baseline = _expert_maps(experts)
            baseline_batch = manifest.batch_id
            baseline_commit = manifest.source_commit
        if localization_dir is not None:
            manifest, experts = load_and_verify_localization_package(localization_dir, tenant_id)
            if baseline_batch and (
                manifest.source_batch_id != baseline_batch
                or manifest.source_commit != baseline_commit
            ):
                raise ExpertSyncPlanError(
                    "Baseline localization must match baseline import batch and commit"
                )
            localized = {expert.upstream_path: expert for expert in experts}
            localized_batch = manifest.source_batch_id
    except (ImportPackageError, OSError, ValueError) as exc:
        raise ExpertSyncPlanError(f"Invalid sync baseline: {exc}") from exc
    return baseline, baseline_batch, localized, localized_batch


def _source_status(
    current: PreparedExpert,
    row: AgentProfile,
    baseline_sha: str | None,
    source_commit: str,
) -> SyncItemStatus:
    """根据原始源码摘要和旧提交信息判定上游是否变化。"""

    metadata = _metadata(row)
    accepted_sha = metadata.get("expert_last_accepted_source_sha256")
    synced_sha = metadata.get("upstream_source_sha256")
    if accepted_sha == current.parsed.source_sha256 or synced_sha == current.parsed.source_sha256:
        return "unchanged"
    if baseline_sha:
        return (
            "unchanged"
            if current.parsed.source_sha256 == baseline_sha
            else "upstream_changed"
        )
    if metadata.get("upstream_commit") == source_commit:
        return "unchanged"
    return "baseline_unknown"


def _name_conflict(
    expert: PreparedExpert,
    row: AgentProfile | None,
    names: dict[str, set[str]],
) -> bool:
    """检查当前导入包展示名是否会与同租户其他员工冲突。"""

    candidate = expert.translation.name_zh.strip()
    if not candidate:
        return False
    owners = names.get(candidate, set())
    return any(owner != row.id for owner in owners) if row is not None else bool(owners)


def build_sync_plan(
    db_factory: SessionFactory,
    package_dir: Path,
    tenant_id: str,
    admin_username: str,
    *,
    baseline_package_dir: Path | None = None,
    baseline_localization_dir: Path | None = None,
    output_path: Path | None = None,
) -> SyncPlanResult:
    """只读比较当前专家包、历史基线和目标租户，生成同步计划文件。"""

    try:
        manifest, prepared = load_and_verify_package(package_dir, tenant_id)
        current = _expert_maps(prepared)
        baseline, baseline_batch, localized, localized_batch = _load_baselines(
            baseline_package_dir,
            baseline_localization_dir,
            tenant_id,
        )
        with db_factory() as db:
            validate_admin(db, tenant_id, admin_username)
            rows = list(
                db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
            )
            governance = _governance_flags(db, tenant_id, rows)
    except (ExpertImportError, ExpertSyncPlanError, ImportPackageError, OSError, ValueError) as exc:
        raise ExpertSyncPlanError(str(exc)) from exc

    imported_rows = [
        row
        for row in rows
        if _metadata(row).get("employee_type") == "expert"
        and _metadata(row).get("expert_source_code") == SOURCE_CODE
    ]
    by_path: dict[str, AgentProfile] = {}
    duplicate_paths: set[str] = set()
    for row in imported_rows:
        path = str(_metadata(row).get("upstream_path") or "")
        if path in by_path:
            duplicate_paths.add(path)
        else:
            by_path[path] = row
    names: dict[str, set[str]] = {}
    for row in rows:
        names.setdefault(row.name, set()).add(row.id)
    items: list[SyncPlanItem] = []
    for path, expert in current.items():
        row = by_path.get(path)
        if row is None:
            status: SyncItemStatus = "new"
            local_change: LocalChangeState = "not_applicable"
            baseline_sha = baseline[path].parsed.source_sha256 if path in baseline else None
            message = None
        elif path in duplicate_paths:
            status = "duplicate_source_path"
            local_change = "unknown"
            baseline_sha = baseline[path].parsed.source_sha256 if path in baseline else None
            message = "租户内存在多个相同 upstream_path，禁止自动同步"
        else:
            baseline_sha = baseline[path].parsed.source_sha256 if path in baseline else None
            status = _source_status(expert, row, baseline_sha, manifest.source_commit)
            local_change = _local_change_state(row, localized.get(path))
            message = None
            if local_change == "modified":
                message = "运行时字段与最后已接受内容不一致"
            elif local_change == "unknown":
                message = "缺少可证明的历史运行时内容基线"
        flags = _review_flags(expert, status, governance.get(row.id, []) if row else None)
        if _name_conflict(expert, row, names):
            flags.append("name_conflict")
        if local_change == "modified":
            flags.append("locally_modified")
        items.append(
            SyncPlanItem(
                upstream_path=path,
                status=status,
                agent_id=row.id if row else None,
                name=row.name if row else expert.translation.name_zh,
                current_source_sha256=expert.parsed.source_sha256,
                baseline_source_sha256=baseline_sha,
                observed_updated_at=row.updated_at.isoformat() if row else None,
                observed_profile_revision=row.profile_revision if row else None,
                observed_content_sha256=profile_content_sha256(row) if row else None,
                local_change=local_change,
                review_flags=list(dict.fromkeys(flags)),
                message=message,
            )
        )
    for path, row in sorted(by_path.items()):
        if path in current:
            continue
        metadata = _metadata(row)
        baseline_sha = baseline[path].parsed.source_sha256 if path in baseline else None
        local_change = _local_change_state(row, localized.get(path))
        flags = ["source_removed"]
        flags.extend(governance.get(row.id, []))
        if local_change == "modified":
            flags.append("locally_modified")
        if baseline_sha is None and not metadata.get("upstream_source_sha256"):
            flags.append("baseline_unknown")
        if path in duplicate_paths:
            flags.append("duplicate_source_path")
        items.append(
            SyncPlanItem(
                upstream_path=path,
                status="source_removed",
                agent_id=row.id,
                name=row.name,
                baseline_source_sha256=baseline_sha,
                observed_updated_at=row.updated_at.isoformat(),
                observed_profile_revision=row.profile_revision,
                observed_content_sha256=profile_content_sha256(row),
                local_change=local_change,
                review_flags=flags,
                message="当前上游包中不存在此路径，不自动删除项目专家",
            )
        )
    items.sort(key=lambda item: item.upstream_path)
    result_path = (
        output_path.expanduser().resolve()
        if output_path is not None
        else package_dir.expanduser().resolve()
        / f"sync-plan-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
    )
    report_path = result_path.with_suffix(".md")
    if result_path.exists() or report_path.exists():
        raise ExpertSyncPlanError(f"Sync plan output already exists: {result_path}")
    result = SyncPlanResult(
        tenant_id=tenant_id,
        source_batch_id=manifest.batch_id,
        source_commit=manifest.source_commit,
        baseline_batch_id=baseline_batch,
        baseline_localization_batch_id=localized_batch,
        started_at=_iso_now(),
        finished_at=_iso_now(),
        result_path=result_path,
        report_path=report_path,
        items=items,
    )
    _write_result(result)
    _atomic_write(report_path, _render_report(result).encode("utf-8"))
    return result
