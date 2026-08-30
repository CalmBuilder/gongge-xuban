"""
@Time       : 2026/08/29 14:00
@Author     : zhanglp8181
@File       : sync_apply.py
@CallChain  : 同步计划 → 当前导入/中文化包复核 → 租户专家受控写入 → apply 结果
@Description: 按显式批准、内容基线和治理事实安全更新 Agency Agents 专家，不自动删除或覆盖受保护对象。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db.models import AgentProfile, User, utc_now
from app.experts.import_service import ExpertImportError, expert_metadata, validate_admin
from app.experts.localization_package import load_and_verify_localization_package
from app.experts.localization_schema import LocalizedExpert
from app.experts.package import ImportPackageError, load_and_verify_package
from app.experts.schema import ImportManifest, PreparedExpert
from app.experts.sync_plan import (
    SyncPlanItem,
    SyncPlanResult,
    profile_content_sha256,
)


SessionFactory = Callable[[], Session]

SyncApplyItemStatus = Literal[
    "created",
    "updated",
    "metadata_updated",
    "pending",
    "skipped_not_approved",
    "skipped_review",
    "skipped_unsafe",
    "skipped_missing_translation",
    "skipped_stale_plan",
    "skipped_source_removed",
    "failed",
]

HARD_REVIEW_FLAGS = frozenset(
    {
        "published",
        "resource_bound",
        "model_bound",
        "used",
        "has_session",
        "name_conflict",
        "locally_modified",
    }
)
SOFT_REVIEW_FLAGS = frozenset(
    {
        "high_risk_content",
        "declared_external_service",
        "external_capability",
        "unresolved_capability",
        "taxonomy_mapping_required",
        "translation_package_required",
    }
)
SOURCE_METADATA_KEYS = frozenset(
    {
        "employee_type",
        "expert_source_code",
        "role_name",
        "expert_category",
        "expert_category_original",
        "expert_tags",
        "expert_name_original",
        "expert_emoji",
        "expert_color",
        "expert_vibe",
        "expert_author",
        "expert_declared_tools",
        "expert_services",
        "expert_capability_manifest",
        "expert_prompt_estimated_tokens",
        "upstream_path",
        "upstream_url",
        "upstream_commit",
        "upstream_license",
        "import_batch_id",
        "import_content_sha256",
        "owner_semantics",
        "governance_template",
    }
)


class ExpertSyncApplyError(ValueError):
    """同步 apply 的输入、计划或租户前置条件不满足。"""


class SyncAgentSnapshot(BaseModel):
    """保存 apply 前会被同步流程改写的专家字段，供受保护回滚使用。"""

    name: str
    description: str | None = None
    persona_prompt: str | None = None
    original_name: str | None = None
    original_description: str | None = None
    original_persona_prompt: str | None = None
    original_locale: str | None = None
    profile_revision: int
    updated_at: str
    metadata_json: dict[str, object] = Field(default_factory=dict)


class SyncApplyItem(BaseModel):
    """一个同步计划条目的受控写入结果。"""

    model_config = ConfigDict(frozen=True)

    upstream_path: str
    status: SyncApplyItemStatus
    agent_id: str | None = None
    name: str | None = None
    source_sha256: str | None = None
    profile_revision: int | None = None
    updated_at: str | None = None
    applied_content_sha256: str | None = None
    applied_metadata_sha256: str | None = None
    previous_state: SyncAgentSnapshot | None = None
    message: str | None = None


class SyncApplyResult(BaseModel):
    """可审计的专家同步写入结果。"""

    model_config = ConfigDict(frozen=True)

    operation: Literal["apply"] = "apply"
    tenant_id: str
    plan_path: Path
    source_batch_id: str
    source_commit: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    approved_paths: list[str] = Field(default_factory=list)
    acknowledged_review_flags: dict[str, list[str]] = Field(default_factory=dict)
    items: list[SyncApplyItem] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """返回按 apply 状态聚合的数量。"""

        return dict(Counter(item.status for item in self.items))


def _iso_now() -> str:
    """返回当前 UTC 时间的 ISO 文本。"""

    return utc_now().isoformat()


def _atomic_write(path: Path, content: bytes) -> None:
    """以同目录临时文件原子写入 apply 结果。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_result(result: SyncApplyResult) -> None:
    """持久化本地 apply 结果，不把结果写入业务数据库。"""

    _atomic_write(
        result.result_path,
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _load_plan(plan_path: Path, tenant_id: str) -> SyncPlanResult:
    """读取并校验只读同步计划的租户和操作类型。"""

    try:
        plan = SyncPlanResult.model_validate_json(plan_path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise ExpertSyncApplyError(f"Invalid sync plan: {exc}") from exc
    if plan.operation != "plan":
        raise ExpertSyncApplyError("Sync apply requires a plan result")
    if plan.tenant_id != tenant_id:
        raise ExpertSyncApplyError("Sync plan tenant does not match command tenant")
    return plan


def metadata_sha256(metadata: dict[str, object] | None) -> str:
    """计算专家元数据的稳定摘要，回滚前用它确认没有发生隐式修改。"""

    content = json.dumps(
        metadata if isinstance(metadata, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _agent_snapshot(agent: AgentProfile) -> SyncAgentSnapshot:
    """提取 apply 前状态，避免回滚覆盖 apply 之后的其他业务变更。"""

    return SyncAgentSnapshot(
        name=agent.name,
        description=agent.description,
        persona_prompt=agent.persona_prompt,
        original_name=agent.original_name,
        original_description=agent.original_description,
        original_persona_prompt=agent.original_persona_prompt,
        original_locale=agent.original_locale,
        profile_revision=agent.profile_revision,
        updated_at=agent.updated_at.isoformat(),
        metadata_json=dict(agent.metadata_json or {}),
    )


def _prepared_map(experts: list[PreparedExpert]) -> dict[str, PreparedExpert]:
    """把当前导入包转换为唯一 upstream_path 映射。"""

    result: dict[str, PreparedExpert] = {}
    for expert in experts:
        path = expert.parsed.upstream_path
        if path in result:
            raise ExpertSyncApplyError(f"Duplicate upstream path in package: {path}")
        result[path] = expert
    return result


def _localized_map(experts: list[LocalizedExpert]) -> dict[str, LocalizedExpert]:
    """把当前中文化包转换为唯一 upstream_path 映射。"""

    result: dict[str, LocalizedExpert] = {}
    for expert in experts:
        if expert.upstream_path in result:
            raise ExpertSyncApplyError(
                f"Duplicate upstream path in localization package: {expert.upstream_path}"
            )
        result[expert.upstream_path] = expert
    return result


def _load_current_packages(
    package_dir: Path,
    localization_dir: Path | None,
    tenant_id: str,
) -> tuple[ImportManifest, dict[str, PreparedExpert], dict[str, LocalizedExpert]]:
    """校验当前导入包和与其绑定的中文化包，拒绝跨批次或跨提交混用。"""

    try:
        manifest, prepared = load_and_verify_package(package_dir, tenant_id)
        current = _prepared_map(prepared)
        localized: dict[str, LocalizedExpert] = {}
        if localization_dir is not None:
            localization_manifest, localized_experts = load_and_verify_localization_package(
                localization_dir,
                tenant_id,
            )
            if (
                localization_manifest.source_batch_id != manifest.batch_id
                or localization_manifest.source_commit != manifest.source_commit
            ):
                raise ExpertSyncApplyError(
                    "Localization package must match current import batch and commit"
                )
            localized = _localized_map(localized_experts)
            for path, expert in localized.items():
                source = current.get(path)
                if source is None:
                    raise ExpertSyncApplyError(
                        f"Localization package contains unknown upstream path: {path}"
                    )
                if (
                    expert.source_batch_id != manifest.batch_id
                    or expert.source_commit != manifest.source_commit
                    or expert.source_content_sha256 != source.content_sha256
                    or expert.original_name != source.parsed.name
                    or expert.original_description != source.parsed.description
                    or expert.original_prompt != source.parsed.source_markdown
                ):
                    raise ExpertSyncApplyError(
                        f"Localization source does not match current import expert: {path}"
                    )
        return manifest, current, localized
    except (ExpertSyncApplyError, ImportPackageError, OSError, ValueError) as exc:
        raise ExpertSyncApplyError(f"Invalid current sync package: {exc}") from exc


def _validate_plan_against_package(
    plan: SyncPlanResult,
    manifest: ImportManifest,
    current: dict[str, PreparedExpert],
) -> dict[str, SyncPlanItem]:
    """确认计划仍然针对同一当前包，防止将旧计划套到新内容上。"""

    if plan.source_batch_id != manifest.batch_id or plan.source_commit != manifest.source_commit:
        raise ExpertSyncApplyError("Sync plan does not match current import batch and commit")
    items: dict[str, SyncPlanItem] = {}
    for item in plan.items:
        if item.upstream_path in items:
            raise ExpertSyncApplyError(f"Duplicate path in sync plan: {item.upstream_path}")
        items[item.upstream_path] = item
        source = current.get(item.upstream_path)
        if source is None:
            if item.status != "source_removed":
                raise ExpertSyncApplyError(
                    f"Sync plan contains non-removed path absent from current package: "
                    f"{item.upstream_path}"
                )
            continue
        if item.status == "source_removed":
            raise ExpertSyncApplyError(
                f"Sync plan marks current package path as removed: {item.upstream_path}"
            )
        if item.current_source_sha256 != source.parsed.source_sha256:
            raise ExpertSyncApplyError(
                f"Sync plan source digest does not match current package: {item.upstream_path}"
            )
    missing = sorted(set(current) - set(items))
    if missing:
        raise ExpertSyncApplyError(
            "Sync plan does not cover current package paths: " + ", ".join(missing[:5])
        )
    return items


def _matching_agents(
    db: Session,
    tenant_id: str,
    upstream_path: str,
    *,
    for_update: bool = False,
) -> list[AgentProfile]:
    """读取来源记录；apply 时锁定候选行，避免检查后被并发修改。"""

    statement = select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)
    if for_update:
        statement = statement.with_for_update()
    rows = db.exec(statement).all()
    return [
        row
        for row in rows
        if isinstance(row.metadata_json, dict)
        and row.metadata_json.get("employee_type") == "expert"
        and row.metadata_json.get("expert_source_code") == "agency-agents"
        and row.metadata_json.get("upstream_path") == upstream_path
    ]


def _stale_reason(item: SyncPlanItem, agent: AgentProfile | None) -> str | None:
    """比较计划时的身份、更新时间和运行时摘要，返回首个过期原因。"""

    if item.agent_id != (agent.id if agent is not None else None):
        return "agent identity changed after plan"
    if agent is None:
        return None
    if (
        item.observed_profile_revision is not None
        and item.observed_profile_revision != agent.profile_revision
    ):
        return "agent profile revision changed after plan"
    if item.observed_updated_at != agent.updated_at.isoformat():
        return "agent updated after plan"
    if item.observed_content_sha256 != profile_content_sha256(agent):
        return "agent content changed after plan"
    return None


def _review_blockers(
    item: SyncPlanItem,
    localized: LocalizedExpert | None,
    acknowledged: set[str],
) -> tuple[list[str], list[str]]:
    """拆分不可覆盖门禁和需要管理员明确确认的软风险。"""

    flags = set(item.review_flags)
    if localized is not None:
        flags.discard("translation_package_required")
    hard = sorted(flags & HARD_REVIEW_FLAGS)
    unknown = flags - HARD_REVIEW_FLAGS - SOFT_REVIEW_FLAGS
    hard.extend(sorted(unknown))
    soft = sorted((flags & SOFT_REVIEW_FLAGS) - acknowledged)
    return hard, soft


def _merged_metadata(
    expert: PreparedExpert,
    manifest: ImportManifest,
    existing: AgentProfile | None,
    admin: User,
    localized: LocalizedExpert | None,
    accepted_content: tuple[str | None, str | None, str | None],
) -> dict[str, object]:
    """合并最新来源事实并保存下一轮计划可验证的接受基线。"""

    incoming = expert_metadata(expert, manifest, admin)
    metadata = dict(existing.metadata_json or {}) if existing is not None else {}
    if existing is None:
        metadata.update(incoming)
    else:
        for key in SOURCE_METADATA_KEYS:
            metadata[key] = incoming[key]
    metadata.update(
        {
            "upstream_source_sha256": expert.parsed.source_sha256,
            "expert_last_synced_commit": manifest.source_commit,
            "expert_last_synced_batch_id": manifest.batch_id,
            "expert_sync_status": "accepted",
            "expert_sync_applied_at": _iso_now(),
            "expert_last_accepted_name": accepted_content[0],
            "expert_last_accepted_description": accepted_content[1],
            "expert_last_accepted_persona_prompt": accepted_content[2],
            "expert_last_accepted_source_sha256": expert.parsed.source_sha256,
        }
    )
    if localized is not None:
        metadata.update(
            {
                "expert_translation_status": "verified",
                "expert_translation_sha256": localized.translation_sha256,
                "expert_translation_source_content_sha256": localized.source_content_sha256,
                "expert_translation_source_batch_id": localized.source_batch_id,
                "expert_translation_source_commit": localized.source_commit,
            }
        )
    return metadata


def _apply_localized_content(agent: AgentProfile, localized: LocalizedExpert) -> None:
    """把已校验的中文化内容应用到现有专家，保留专家主键和治理关系。"""

    agent.original_name = localized.original_name
    agent.original_description = localized.original_description
    agent.original_persona_prompt = localized.original_prompt
    agent.original_locale = "en-US"
    agent.name = localized.localized_name
    agent.description = localized.localized_description
    agent.persona_prompt = localized.localized_prompt


def _new_agent(
    tenant_id: str,
    expert: PreparedExpert,
    manifest: ImportManifest,
    localized: LocalizedExpert,
    admin: User,
) -> AgentProfile:
    """根据当前包和中文化内容构造新专家，初始状态保持私有且未发布。"""

    accepted = (
        localized.localized_name,
        localized.localized_description,
        localized.localized_prompt,
    )
    return AgentProfile(
        tenant_id=tenant_id,
        name=localized.localized_name,
        description=localized.localized_description,
        persona_prompt=localized.localized_prompt,
        original_name=localized.original_name,
        original_description=localized.original_description,
        original_persona_prompt=localized.original_prompt,
        original_locale="en-US",
        is_overall=False,
        status="active",
        owner_user_id=admin.id,
        agent_category_code="professional",
        published_to_gallery=False,
        visibility_scope="private",
        metadata_json=_merged_metadata(
            expert,
            manifest,
            None,
            admin,
            localized,
            accepted,
        ),
    )


def apply_sync_plan(
    db_factory: SessionFactory,
    package_dir: Path,
    plan_path: Path,
    tenant_id: str,
    admin_username: str,
    *,
    localization_dir: Path | None = None,
    approved_paths: set[str] | None = None,
    acknowledged_review_flags: dict[str, set[str]] | None = None,
    output_path: Path | None = None,
) -> SyncApplyResult:
    """按计划和逐路径批准受控同步，保护人工修改、治理关系与上游删除对象。"""

    plan_path = plan_path.expanduser().resolve(strict=True)
    try:
        plan = _load_plan(plan_path, tenant_id)
        manifest, current, localized = _load_current_packages(
            package_dir,
            localization_dir,
            tenant_id,
        )
        plan_items = _validate_plan_against_package(plan, manifest, current)
        result_path = (
            output_path.expanduser().resolve()
            if output_path is not None
            else package_dir.expanduser().resolve()
            / f"sync-apply-{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
        )
        if result_path == plan_path:
            raise ExpertSyncApplyError("Apply result must not overwrite sync plan")
        if result_path.exists():
            raise ExpertSyncApplyError(f"Sync apply output already exists: {result_path}")
        with db_factory() as db:
            admin = validate_admin(db, tenant_id, admin_username)
            admin_snapshot = User.model_validate(admin, from_attributes=True)
    except (ExpertImportError, ExpertSyncApplyError, ImportPackageError, OSError, ValueError) as exc:
        raise ExpertSyncApplyError(str(exc)) from exc

    approved = {path.strip() for path in (approved_paths or set()) if path.strip()}
    acknowledgements = {
        path: {flag.strip() for flag in flags if flag.strip()}
        for path, flags in (acknowledged_review_flags or {}).items()
    }
    result = SyncApplyResult(
        tenant_id=tenant_id,
        plan_path=plan_path,
        source_batch_id=manifest.batch_id,
        source_commit=manifest.source_commit,
        started_at=_iso_now(),
        result_path=result_path,
        approved_paths=sorted(approved),
        acknowledged_review_flags={
            path: sorted(flags) for path, flags in sorted(acknowledgements.items())
        },
    )
    _write_result(result)
    items: list[SyncApplyItem] = []
    for path in sorted(plan_items):
        item = plan_items[path]
        expert = current.get(path)
        localized_expert = localized.get(path)
        if item.status == "source_removed":
            applied_item = SyncApplyItem(
                upstream_path=path,
                status="skipped_source_removed",
                agent_id=item.agent_id,
                name=item.name,
                message="上游已移除；保留项目专家，不自动删除",
            )
        elif path not in approved:
            applied_item = SyncApplyItem(
                upstream_path=path,
                status="skipped_not_approved",
                agent_id=item.agent_id,
                name=item.name,
                source_sha256=item.current_source_sha256,
                message="未提供该 upstream_path 的显式批准",
            )
        elif item.status in {"baseline_unknown", "duplicate_source_path"}:
            applied_item = SyncApplyItem(
                upstream_path=path,
                status="skipped_unsafe",
                agent_id=item.agent_id,
                name=item.name,
                source_sha256=item.current_source_sha256,
                message=f"计划状态 {item.status} 不允许自动 apply",
            )
        elif expert is None:
            applied_item = SyncApplyItem(
                upstream_path=path,
                status="skipped_unsafe",
                agent_id=item.agent_id,
                name=item.name,
                message="当前专家包缺少该路径",
            )
        elif item.status in {"new", "upstream_changed"} and localized_expert is None:
            applied_item = SyncApplyItem(
                upstream_path=path,
                status="skipped_missing_translation",
                agent_id=item.agent_id,
                name=item.name,
                source_sha256=expert.parsed.source_sha256,
                message="新增或上游变更必须提供同批次已校验中文化包",
            )
        else:
            hard_flags, soft_flags = _review_blockers(
                item,
                localized_expert,
                acknowledgements.get(path, set()),
            )
            if item.status in {"unchanged", "upstream_changed"} and item.local_change != "clean":
                hard_flags.append(f"local_change={item.local_change}")
            if hard_flags:
                applied_item = SyncApplyItem(
                    upstream_path=path,
                    status="skipped_unsafe",
                    agent_id=item.agent_id,
                    name=item.name,
                    source_sha256=expert.parsed.source_sha256,
                    message="不可覆盖门禁：" + "、".join(sorted(set(hard_flags))),
                )
            elif soft_flags:
                applied_item = SyncApplyItem(
                    upstream_path=path,
                    status="skipped_review",
                    agent_id=item.agent_id,
                    name=item.name,
                    source_sha256=expert.parsed.source_sha256,
                    message="需要确认：" + "、".join(soft_flags),
                )
            else:
                with db_factory() as db:
                    try:
                        matches = _matching_agents(db, tenant_id, path, for_update=True)
                        if len(matches) > 1:
                            applied_item = SyncApplyItem(
                                upstream_path=path,
                                status="skipped_unsafe",
                                source_sha256=expert.parsed.source_sha256,
                                message="租户内存在重复 upstream_path，拒绝写入",
                            )
                        else:
                            agent = matches[0] if matches else None
                            stale = _stale_reason(item, agent)
                            if stale:
                                applied_item = SyncApplyItem(
                                    upstream_path=path,
                                    status="skipped_stale_plan",
                                    agent_id=agent.id if agent else item.agent_id,
                                    name=agent.name if agent else item.name,
                                    source_sha256=expert.parsed.source_sha256,
                                    message=stale,
                                )
                            elif item.status == "new":
                                if localized_expert is None:
                                    raise ExpertSyncApplyError(
                                        "localized content is required for a new expert"
                                    )
                                conflict = db.exec(
                                    select(AgentProfile.id).where(
                                        AgentProfile.tenant_id == tenant_id,
                                        AgentProfile.name == localized_expert.localized_name,
                                    )
                                ).first()
                                if conflict is not None:
                                    applied_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="skipped_unsafe",
                                        source_sha256=expert.parsed.source_sha256,
                                        message="中文展示名已被租户内其他数字员工占用",
                                    )
                                else:
                                    created = _new_agent(
                                        tenant_id,
                                        expert,
                                        manifest,
                                        localized_expert,
                                        admin_snapshot,
                                    )
                                    db.add(created)
                                    db.flush()
                                    pending_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="pending",
                                        agent_id=created.id,
                                        name=created.name,
                                        source_sha256=expert.parsed.source_sha256,
                                        profile_revision=created.profile_revision,
                                        updated_at=created.updated_at.isoformat(),
                                        applied_content_sha256=profile_content_sha256(created),
                                        applied_metadata_sha256=metadata_sha256(
                                            created.metadata_json
                                        ),
                                    )
                                    _write_result(
                                        result.model_copy(update={"items": [*items, pending_item]})
                                    )
                                    db.commit()
                                    db.refresh(created)
                                    applied_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="created",
                                        agent_id=created.id,
                                        name=created.name,
                                        source_sha256=expert.parsed.source_sha256,
                                        profile_revision=created.profile_revision,
                                        updated_at=created.updated_at.isoformat(),
                                        applied_content_sha256=profile_content_sha256(created),
                                        applied_metadata_sha256=metadata_sha256(
                                            created.metadata_json
                                        ),
                                    )
                            elif agent is None:
                                applied_item = SyncApplyItem(
                                    upstream_path=path,
                                    status="skipped_stale_plan",
                                    agent_id=item.agent_id,
                                    name=item.name,
                                    source_sha256=expert.parsed.source_sha256,
                                    message="计划中的租户专家已不存在",
                                )
                            elif item.status == "upstream_changed":
                                if localized_expert is None:
                                    raise ExpertSyncApplyError(
                                        "localized content is required for an upstream change"
                                    )
                                conflict = db.exec(
                                    select(AgentProfile.id).where(
                                        AgentProfile.tenant_id == tenant_id,
                                        AgentProfile.id != agent.id,
                                        AgentProfile.name == localized_expert.localized_name,
                                    )
                                ).first()
                                if conflict is not None:
                                    applied_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="skipped_unsafe",
                                        agent_id=agent.id,
                                        name=agent.name,
                                        source_sha256=expert.parsed.source_sha256,
                                        message="更新后的中文展示名已被其他数字员工占用",
                                    )
                                else:
                                    previous_state = _agent_snapshot(agent)
                                    _apply_localized_content(agent, localized_expert)
                                    agent.profile_revision = max(
                                        int(agent.profile_revision or 1), 1
                                    ) + 1
                                    accepted = (
                                        agent.name,
                                        agent.description,
                                        agent.persona_prompt,
                                    )
                                    agent.metadata_json = _merged_metadata(
                                        expert,
                                        manifest,
                                        agent,
                                        admin_snapshot,
                                        localized_expert,
                                        accepted,
                                    )
                                    agent.updated_at = utc_now()
                                    db.add(agent)
                                    pending_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="pending",
                                        agent_id=agent.id,
                                        name=agent.name,
                                        source_sha256=expert.parsed.source_sha256,
                                        profile_revision=agent.profile_revision,
                                        updated_at=agent.updated_at.isoformat(),
                                        applied_content_sha256=profile_content_sha256(agent),
                                        applied_metadata_sha256=metadata_sha256(
                                            agent.metadata_json
                                        ),
                                        previous_state=previous_state,
                                    )
                                    _write_result(
                                        result.model_copy(update={"items": [*items, pending_item]})
                                    )
                                    db.commit()
                                    db.refresh(agent)
                                    applied_item = SyncApplyItem(
                                        upstream_path=path,
                                        status="updated",
                                        agent_id=agent.id,
                                        name=agent.name,
                                        source_sha256=expert.parsed.source_sha256,
                                        profile_revision=agent.profile_revision,
                                        updated_at=agent.updated_at.isoformat(),
                                        applied_content_sha256=profile_content_sha256(agent),
                                        applied_metadata_sha256=metadata_sha256(
                                            agent.metadata_json
                                        ),
                                        previous_state=previous_state,
                                    )
                            elif item.status == "unchanged":
                                previous_state = _agent_snapshot(agent)
                                accepted = (
                                    agent.name,
                                    agent.description,
                                    agent.persona_prompt,
                                )
                                agent.metadata_json = _merged_metadata(
                                    expert,
                                    manifest,
                                    agent,
                                    admin_snapshot,
                                    None,
                                    accepted,
                                )
                                agent.updated_at = utc_now()
                                db.add(agent)
                                pending_item = SyncApplyItem(
                                    upstream_path=path,
                                    status="pending",
                                    agent_id=agent.id,
                                    name=agent.name,
                                    source_sha256=expert.parsed.source_sha256,
                                    profile_revision=agent.profile_revision,
                                    updated_at=agent.updated_at.isoformat(),
                                    applied_content_sha256=profile_content_sha256(agent),
                                    applied_metadata_sha256=metadata_sha256(agent.metadata_json),
                                    previous_state=previous_state,
                                )
                                _write_result(
                                    result.model_copy(update={"items": [*items, pending_item]})
                                )
                                db.commit()
                                db.refresh(agent)
                                applied_item = SyncApplyItem(
                                    upstream_path=path,
                                    status="metadata_updated",
                                    agent_id=agent.id,
                                    name=agent.name,
                                    source_sha256=expert.parsed.source_sha256,
                                    profile_revision=agent.profile_revision,
                                    updated_at=agent.updated_at.isoformat(),
                                    applied_content_sha256=profile_content_sha256(agent),
                                    applied_metadata_sha256=metadata_sha256(agent.metadata_json),
                                    previous_state=previous_state,
                                )
                            else:
                                applied_item = SyncApplyItem(
                                    upstream_path=path,
                                    status="skipped_unsafe",
                                    agent_id=agent.id,
                                    name=agent.name,
                                    source_sha256=expert.parsed.source_sha256,
                                    message=f"计划状态 {item.status} 不支持写入",
                                )
                    except ExpertSyncApplyError:
                        db.rollback()
                        raise
                    except (SQLAlchemyError, ValueError) as exc:
                        db.rollback()
                        applied_item = SyncApplyItem(
                            upstream_path=path,
                            status="failed",
                            agent_id=item.agent_id,
                            name=item.name,
                            source_sha256=expert.parsed.source_sha256,
                            message=str(exc),
                        )
        items.append(applied_item)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result)
    return result
