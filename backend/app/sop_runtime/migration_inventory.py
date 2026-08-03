"""
@Time       : 2026/07/29 09:42
@Author     : zhanglp8181
@File       : migration_inventory.py
@CallChain  : SOP 迁移预检 API/验收脚本 → 当前发布头与运行快照 → 稳定迁移分类报告
@Description: 只读盘点 SOP 头版本、发布快照和活动实例，形成不改写历史状态的迁移门禁。
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import Field
from sqlmodel import Session, select

from app.db.models import Skill, SkillVersion, SopInstance, SopWorkItem
from app.sop_runtime.capabilities import DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.compatibility import CompatibilityStatus
from app.sop_runtime.contracts import RuntimeContract
from app.sop_runtime.dependency_inventory import (
    DependencyReadiness,
    SopDependencyAssessment,
    build_sop_dependency_assessment,
)
from app.sop_runtime.execution_store import ACTIVE_INSTANCE_STATUSES
from app.sop_runtime.legacy_skill_card_adapter import (
    SopCompilationError,
    compile_legacy_skill_card,
)
from app.sop_runtime.versioning import skill_content_checksum
from app.sop_runtime.work_items import ACTIVE_WORK_ITEM_STATUSES


class MigrationDisposition(StrEnum):
    """描述当前发布头在 M5.5 中允许采取的迁移处置。"""

    NO_MIGRATION = "no_migration"
    AUTO_NEW_VERSION = "auto_new_version"
    BUSINESS_CONFIRMATION = "business_confirmation"
    TEMPORARILY_UNSUPPORTED = "temporarily_unsupported"


class SopMigrationInventoryEntry(RuntimeContract):
    """单个当前发布 SOP 的版本、运行占用与迁移分类摘要。"""

    skill_id: str
    name: str
    current_version: str
    current_version_id: str
    derived_from_version_id: str | None = None
    compatibility_status: CompatibilityStatus
    disposition: MigrationDisposition
    reason_code: str
    diagnostic_codes: tuple[str, ...] = ()
    non_executable_capabilities: tuple[str, ...] = ()
    published_version_count: int = Field(ge=0)
    active_instance_count: int = Field(ge=0)
    active_historical_instance_count: int = Field(ge=0)
    active_work_item_count: int = Field(ge=0)
    dependency_assessment: SopDependencyAssessment | None = None


class SopMigrationInventory(RuntimeContract):
    """租户当前全部发布头的确定性迁移预检报告。"""

    tenant_id: str
    total: int = Field(ge=0)
    disposition_counts: dict[str, int]
    dependency_counts: dict[str, int]
    active_instance_count: int = Field(ge=0)
    active_historical_instance_count: int = Field(ge=0)
    active_work_item_count: int = Field(ge=0)
    entries: tuple[SopMigrationInventoryEntry, ...]


class SopDependencyCoverageEntry(RuntimeContract):
    """投影单个发布 SOP 的请求人默认策略和统一依赖评估明细。"""

    skill_id: str
    name: str
    current_version: str
    requester_policy: str = "active_tenant_member"
    requester_policy_explicit: bool = False
    dependency_assessment: SopDependencyAssessment


class SopDependencyCoverageReport(RuntimeContract):
    """保存租户全部发布 SOP 的确定性、只读依赖覆盖报告。"""

    tenant_id: str
    total: int = Field(ge=0)
    readiness_counts: dict[str, int]
    entries: tuple[SopDependencyCoverageEntry, ...]


def build_sop_dependency_coverage(
    db: Session,
    tenant_id: str,
) -> SopDependencyCoverageReport:
    """复用迁移预检的同一评估结果生成治理页面所需覆盖明细。

    当前 SOP 没有独立启动角色契约，平台入口统一要求活动租户成员，因此报告把该事实明确
    标为平台默认策略，而不是伪装成每个 SOP 已显式声明的启动规则。
    """

    inventory = build_sop_migration_inventory(db, tenant_id)
    entries = tuple(
        SopDependencyCoverageEntry(
            skill_id=entry.skill_id,
            name=entry.name,
            current_version=entry.current_version,
            dependency_assessment=entry.dependency_assessment
            or SopDependencyAssessment(
                readiness=DependencyReadiness.BLOCKED,
                issue_codes=("DEPENDENCY_ASSESSMENT_MISSING",),
                human_task_count=0,
                tool_operation_count=0,
                knowledge_task_count=0,
                bound_agent_count=0,
                executable_agent_count=0,
            ),
        )
        for entry in inventory.entries
    )
    return SopDependencyCoverageReport(
        tenant_id=tenant_id,
        total=len(entries),
        readiness_counts=dict(inventory.dependency_counts),
        entries=entries,
    )


def build_sop_migration_inventory(db: Session, tenant_id: str) -> SopMigrationInventory:
    """只读扫描当前发布头，并逐项隔离编译失败后生成稳定分类。"""

    heads = db.exec(
        select(Skill)
        .where(Skill.tenant_id == tenant_id, Skill.status == "published")
        .order_by(Skill.skill_id, Skill.version)
    ).all()
    versions = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id,
            SkillVersion.status == "published",
        )
    ).all()
    active_instances = db.exec(
        select(SopInstance).where(
            SopInstance.tenant_id == tenant_id,
            SopInstance.status.in_(ACTIVE_INSTANCE_STATUSES),
        )
    ).all()
    active_work_items = db.exec(
        select(SopWorkItem).where(
            SopWorkItem.tenant_id == tenant_id,
            SopWorkItem.status.in_(ACTIVE_WORK_ITEM_STATUSES),
        )
    ).all()

    versions_by_skill: dict[str, list[SkillVersion]] = {}
    current_version_ids: dict[tuple[str, str], str] = {}
    current_versions: dict[tuple[str, str], SkillVersion] = {}
    version_skill_ids: dict[str, str] = {}
    for version in versions:
        versions_by_skill.setdefault(version.skill_id, []).append(version)
        current_version_ids[(version.skill_id, version.version)] = version.id
        current_versions[(version.skill_id, version.version)] = version
        version_skill_ids[version.id] = version.skill_id

    instances_by_skill: Counter[str] = Counter()
    historical_instances_by_skill: Counter[str] = Counter()
    head_versions = {head.skill_id: head.version for head in heads}
    for instance in active_instances:
        instances_by_skill[instance.skill_id] += 1
        current_version_id = current_version_ids.get(
            (instance.skill_id, head_versions.get(instance.skill_id, ""))
        )
        if current_version_id is None or instance.skill_version_id != current_version_id:
            historical_instances_by_skill[instance.skill_id] += 1

    work_items_by_skill: Counter[str] = Counter()
    for work_item in active_work_items:
        skill_id = version_skill_ids.get(work_item.skill_version_id)
        if skill_id is not None:
            work_items_by_skill[skill_id] += 1

    entries = tuple(
        _inventory_entry(
            db,
            head,
            versions_by_skill=versions_by_skill,
            current_version_ids=current_version_ids,
            current_versions=current_versions,
            instances_by_skill=instances_by_skill,
            historical_instances_by_skill=historical_instances_by_skill,
            work_items_by_skill=work_items_by_skill,
        )
        for head in heads
    )
    disposition_counts = Counter(entry.disposition.value for entry in entries)
    dependency_counts = Counter(
        entry.dependency_assessment.readiness.value
        for entry in entries
        if entry.dependency_assessment is not None
    )
    return SopMigrationInventory(
        tenant_id=tenant_id,
        total=len(entries),
        disposition_counts={
            disposition.value: disposition_counts[disposition.value]
            for disposition in MigrationDisposition
        },
        dependency_counts={
            readiness.value: dependency_counts[readiness.value]
            for readiness in DependencyReadiness
        },
        active_instance_count=len(active_instances),
        active_historical_instance_count=sum(historical_instances_by_skill.values()),
        active_work_item_count=len(active_work_items),
        entries=entries,
    )


def _inventory_entry(
    db: Session,
    head: Skill,
    *,
    versions_by_skill: dict[str, list[SkillVersion]],
    current_version_ids: dict[tuple[str, str], str],
    current_versions: dict[tuple[str, str], SkillVersion],
    instances_by_skill: Counter[str],
    historical_instances_by_skill: Counter[str],
    work_items_by_skill: Counter[str],
) -> SopMigrationInventoryEntry:
    """编译一个发布头并在本项内完成处置判断，避免失败扩散到整批。"""

    try:
        compiled = compile_legacy_skill_card(head.content_json)
    except SopCompilationError:
        compiled = None
    dependency_assessment = (
        build_sop_dependency_assessment(
            db,
            skill=head,
            compiled_definition=compiled,
        )
        if compiled is not None
        else SopDependencyAssessment(
            readiness=DependencyReadiness.BLOCKED,
            issue_codes=("DEFINITION_NOT_COMPILABLE",),
            human_task_count=0,
            tool_operation_count=0,
            knowledge_task_count=0,
            bound_agent_count=0,
            executable_agent_count=0,
        )
    )
    current_version_id = current_version_ids.get((head.skill_id, head.version), "")
    if not current_version_id:
        return SopMigrationInventoryEntry(
            skill_id=head.skill_id,
            name=head.name,
            current_version=head.version,
            current_version_id="",
            derived_from_version_id=None,
            compatibility_status=CompatibilityStatus.BLOCKED,
            disposition=MigrationDisposition.TEMPORARILY_UNSUPPORTED,
            reason_code="CURRENT_PUBLISHED_SNAPSHOT_MISSING",
            diagnostic_codes=("CURRENT_PUBLISHED_SNAPSHOT_MISSING",),
            published_version_count=len(versions_by_skill.get(head.skill_id, ())),
            active_instance_count=instances_by_skill[head.skill_id],
            active_historical_instance_count=historical_instances_by_skill[head.skill_id],
            active_work_item_count=work_items_by_skill[head.skill_id],
            dependency_assessment=dependency_assessment,
        )
    current_version = current_versions[(head.skill_id, head.version)]
    if skill_content_checksum(head.content_json) != skill_content_checksum(
        current_version.content_json
    ):
        return SopMigrationInventoryEntry(
            skill_id=head.skill_id,
            name=head.name,
            current_version=head.version,
            current_version_id=current_version_id,
            derived_from_version_id=current_version.derived_from_version_id,
            compatibility_status=CompatibilityStatus.BLOCKED,
            disposition=MigrationDisposition.TEMPORARILY_UNSUPPORTED,
            reason_code="CURRENT_PUBLISHED_SNAPSHOT_MISMATCH",
            diagnostic_codes=("CURRENT_PUBLISHED_SNAPSHOT_MISMATCH",),
            published_version_count=len(versions_by_skill.get(head.skill_id, ())),
            active_instance_count=instances_by_skill[head.skill_id],
            active_historical_instance_count=historical_instances_by_skill[head.skill_id],
            active_work_item_count=work_items_by_skill[head.skill_id],
            dependency_assessment=dependency_assessment,
        )
    try:
        if compiled is None:
            compiled = compile_legacy_skill_card(head.content_json)
    except SopCompilationError as exc:
        return SopMigrationInventoryEntry(
            skill_id=head.skill_id,
            name=head.name,
            current_version=head.version,
            current_version_id=current_version_id,
            derived_from_version_id=current_version.derived_from_version_id,
            compatibility_status=CompatibilityStatus.BLOCKED,
            disposition=MigrationDisposition.TEMPORARILY_UNSUPPORTED,
            reason_code="COMPILATION_BLOCKED",
            diagnostic_codes=tuple(sorted({item.code for item in exc.diagnostics})),
            published_version_count=len(versions_by_skill.get(head.skill_id, ())),
            active_instance_count=instances_by_skill[head.skill_id],
            active_historical_instance_count=historical_instances_by_skill[head.skill_id],
            active_work_item_count=work_items_by_skill[head.skill_id],
            dependency_assessment=dependency_assessment,
        )

    warning_codes = tuple(sorted({item.code for item in compiled.diagnostics}))
    unsupported = tuple(
        sorted(
            {
                capability
                for _node_id, capability in DEFAULT_CAPABILITY_REGISTRY.non_executable_nodes(
                    compiled
                )
            }
        )
    )
    compatibility_status = (
        CompatibilityStatus.COMPILES_WITH_WARNINGS
        if warning_codes
        else CompatibilityStatus.STRUCTURALLY_READY
    )
    disposition, reason_code = _classify(
        warning_codes=warning_codes,
        non_executable_capabilities=unsupported,
    )
    if (
        dependency_assessment is not None
        and dependency_assessment.readiness is DependencyReadiness.BLOCKED
    ):
        disposition = MigrationDisposition.TEMPORARILY_UNSUPPORTED
        reason_code = "BUSINESS_DEPENDENCY_BLOCKED"
    return SopMigrationInventoryEntry(
        skill_id=head.skill_id,
        name=head.name,
        current_version=head.version,
        current_version_id=current_version_id,
        derived_from_version_id=current_version.derived_from_version_id,
        compatibility_status=compatibility_status,
        disposition=disposition,
        reason_code=reason_code,
        diagnostic_codes=warning_codes,
        non_executable_capabilities=unsupported,
        published_version_count=len(versions_by_skill.get(head.skill_id, ())),
        active_instance_count=instances_by_skill[head.skill_id],
        active_historical_instance_count=historical_instances_by_skill[head.skill_id],
        active_work_item_count=work_items_by_skill[head.skill_id],
        dependency_assessment=dependency_assessment,
    )


def _classify(
    *,
    warning_codes: tuple[str, ...],
    non_executable_capabilities: tuple[str, ...],
) -> tuple[MigrationDisposition, str]:
    """按保守门禁分类；当前不把任何语义告警声明为可自动迁移。"""

    if non_executable_capabilities:
        return (
            MigrationDisposition.TEMPORARILY_UNSUPPORTED,
            "RUNTIME_CAPABILITY_UNAVAILABLE",
        )
    if warning_codes:
        return (
            MigrationDisposition.BUSINESS_CONFIRMATION,
            "SEMANTIC_CONFIRMATION_REQUIRED",
        )
    return MigrationDisposition.NO_MIGRATION, "CURRENT_VERSION_READY"
