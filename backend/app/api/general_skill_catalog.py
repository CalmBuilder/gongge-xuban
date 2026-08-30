"""
@Time       : 2026/08/29 16:20
@Author     : zhanglp8181
@File       : general_skill_catalog.py
@CallChain  : 管理端 Skill 广场 → FastAPI → 内置快照目录/导入服务 → GeneralSkill 候选
@Description: 提供项目内置 Skill 候选的权限过滤、分页详情和固定快照导入入口。
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.agents.identity import project_agent_governance
from app.config import Settings, get_settings
from app.db import get_session
from app.db.models import AgentProfile, AgentResourceBinding, GeneralSkill, GeneralSkillRevision, User
from app.general_skills.builtin_catalog import (
    BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM,
    BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM,
    BUILTIN_SKILL_SOURCE_LICENSE,
    BUILTIN_SKILL_SOURCE_REPOSITORY,
    BUILTIN_SKILL_SOURCE_REVISION,
    BuiltinSkillCatalogImportError,
    BuiltinSkillCatalogService,
)
from app.general_skills.catalog_governance import (
    CatalogGovernanceError,
    GeneralSkillCatalogGovernanceService,
)
from app.general_skills.builtin_schema import (
    BuiltinSkillCatalogDetailRead,
    BuiltinSkillCatalogFacetsRead,
    BuiltinSkillCatalogImportRead,
    BuiltinSkillCatalogImportRequest,
    BuiltinSkillCatalogBindingRead,
    BuiltinSkillCatalogBindingRequest,
    BuiltinSkillCatalogBindingSummaryRead,
    BuiltinSkillCatalogItemRead,
    BuiltinSkillCatalogLifecycleRead,
    BuiltinSkillCatalogLifecycleRequest,
    BuiltinSkillCatalogPageRead,
    BuiltinSkillCatalogReviewRead,
    BuiltinSkillCatalogReviewRequest,
    BuiltinSkillResourceRead,
    ExternalSkillCatalogImportRead,
    ExternalSkillCatalogImportRequest,
)
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.localization import get_revision_localization, is_usable_localization
from app.general_skills.remote_source import RemoteFetcher, configured_secure_https_fetcher
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import ensure_tenant_admin, is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(
    prefix="/api/enterprise/general-skill-catalog",
    tags=["enterprise:general-skill-catalog"],
)


def get_catalog_remote_fetcher(settings: Settings = Depends(get_settings)) -> RemoteFetcher:
    """创建管理员目录导入使用的安全远程抓取器。"""

    return configured_secure_https_fetcher(settings.general_skill_dns_resolver)


@router.get("", response_model=BuiltinSkillCatalogPageRead)
def list_builtin_skill_catalog(
    tenant_id: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, max_length=128),
    category: str | None = Query(None, max_length=64),
    source_kind: Literal["platform_builtin", "github", "https", "skillhub"] | None = None,
    stability: Literal["stable", "beta", "misc"] | None = Query(None),
    risk_level: Literal["low", "medium", "high"] | None = Query(None),
    invocation_policy: Literal["model_allowed", "user_only"] | None = Query(None),
    status: Literal["draft", "published", "rejected", "archived"] | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogPageRead:
    """按当前账号权限列出内置 Skill，管理员可审核候选，成员只能发现已发布项。"""

    _ensure_catalog_tenant(db, tenant_id, current_user)
    rows = _visible_catalog_rows(db, tenant_id, current_user)
    rows = _filter_rows(
        db,
        rows,
        search=search,
        category=category,
        source_kind=source_kind,
        stability=stability,
        risk_level=risk_level,
        invocation_policy=invocation_policy,
        status=status,
    )
    rows.sort(key=lambda row: (row.name.casefold(), row.id))
    facets = _facets(rows)
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]
    return BuiltinSkillCatalogPageRead(
        items=[_item_read(db, row) for row in page_rows],
        total=total,
        page=page,
        page_size=page_size,
        facets=facets,
    )


@router.get("/{slug}", response_model=BuiltinSkillCatalogDetailRead)
def get_builtin_skill_catalog_detail(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogDetailRead:
    """读取单条项目级内置 Skill 详情，未发布候选仅对管理员可见。"""

    _ensure_catalog_tenant(db, tenant_id, current_user)
    row = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.catalog_scope == "platform",
            GeneralSkill.tenant_id.is_(None),
            GeneralSkill.slug == slug,
        )
    ).first()
    if row is None or not _is_managed_catalog(row):
        raise HTTPException(status_code=404, detail="Built-in Skill not found")
    if not is_admin_user(current_user) and not _is_member_visible(db, tenant_id, row):
        raise HTTPException(status_code=404, detail="Built-in Skill not found")
    revision = _latest_revision(db, row)
    return _detail_read(db, row, revision, tenant_id=tenant_id)


@router.post("/import", response_model=BuiltinSkillCatalogImportRead)
def import_builtin_skill_catalog(
    request: BuiltinSkillCatalogImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogImportRead:
    """仅允许管理员导入随应用交付的固定快照，并返回可重放命令回执。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    try:
        result = BuiltinSkillCatalogService(db).import_snapshot(
            tenant_id=request.tenant_id,
            command_id=request.command_id,
            actor_user_id=current_user.id,
        )
    except BuiltinSkillCatalogImportError as exc:
        raise _import_error(exc) from exc
    return BuiltinSkillCatalogImportRead(
        command_id=result.command_id,
        replayed=result.replayed,
        created_count=result.created_count,
        existing_count=result.existing_count,
        skill_count=len(result.items),
        source_repository=BUILTIN_SKILL_SOURCE_REPOSITORY,
        source_revision=BUILTIN_SKILL_SOURCE_REVISION,
        source_license=BUILTIN_SKILL_SOURCE_LICENSE,
        source_package_checksum=BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM,
        source_normalized_checksum=BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM,
        items=[dict(item) for item in result.items],
    )


@router.post("/import-external", response_model=ExternalSkillCatalogImportRead, status_code=202)
def import_external_skill_catalog(
    request: ExternalSkillCatalogImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    fetcher: RemoteFetcher = Depends(get_catalog_remote_fetcher),
) -> ExternalSkillCatalogImportRead:
    """由管理员将固定外部 Skill 包导入项目候选库，不自动发布或绑定。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    try:
        result = BuiltinSkillCatalogService(db).import_external(
            tenant_id=request.tenant_id,
            command_id=request.command_id,
            actor_user_id=current_user.id,
            source_kind=request.source_kind,
            source_url=request.source_url,
            source_license=request.source_license,
            revision=request.revision,
            source_subpath=request.source_subpath,
            fetcher=fetcher,
            https_allowed_hosts=settings.general_skill_https_allowed_host_set,
            object_store=FileSystemSkillObjectStore(settings.general_skill_object_store_path),
        )
    except BuiltinSkillCatalogImportError as exc:
        raise _import_error(exc) from exc
    return ExternalSkillCatalogImportRead(
        command_id=result.command_id,
        replayed=result.replayed,
        created_count=result.created_count,
        existing_count=result.existing_count,
        skill_count=len(result.items),
        source_kind=result.source_kind,
        source_url=result.source_repository,
        source_repository=result.source_repository,
        source_revision=result.source_revision,
        source_license=result.source_license,
        source_package_checksum=result.source_package_checksum,
        source_normalized_checksum=result.source_normalized_checksum,
        items=[dict(item) for item in result.items],
    )


@router.post("/review", response_model=BuiltinSkillCatalogReviewRead)
def review_builtin_skill_catalog(
    request: BuiltinSkillCatalogReviewRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogReviewRead:
    """由管理员以两级 CAS 原子审核候选，批准才进入普通成员广场。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    try:
        result = GeneralSkillCatalogGovernanceService(db).review(
            tenant_id=request.tenant_id,
            command_id=request.command_id,
            actor_user_id=current_user.id,
            items=[item.model_dump(mode="json") for item in request.items],
        )
    except CatalogGovernanceError as exc:
        raise _governance_error(exc) from exc
    return BuiltinSkillCatalogReviewRead(
        command_id=result.command_id,
        replayed=result.replayed,
        approved_count=result.approved_count,
        rejected_count=result.rejected_count,
        items=[dict(item) for item in result.items],
    )


@router.post("/bindings", response_model=BuiltinSkillCatalogBindingRead)
def bind_builtin_skill_catalog_skill(
    request: BuiltinSkillCatalogBindingRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogBindingRead:
    """安装项目 Skill 到能力分身，或由管理员绑定到已组织发布数字员工。"""

    ensure_tenant(db, request.tenant_id)
    ensure_current_user_tenant(request.tenant_id, current_user)
    try:
        result = GeneralSkillCatalogGovernanceService(db).bind(
            current_user=current_user,
            skill_id=request.skill_id,
            agent_id=request.agent_id,
            mode=request.mode,
            revision_policy=request.revision_policy,
            pinned_revision_id=request.pinned_revision_id,
            invocation_policy=request.invocation_policy,
        )
    except CatalogGovernanceError as exc:
        raise _governance_error(exc) from exc
    binding = result.binding
    metadata = binding.metadata_json or {}
    return BuiltinSkillCatalogBindingRead(
        action=result.action,
        mode=result.mode,
        binding_id=binding.id,
        agent_id=binding.agent_id,
        skill_id=binding.resource_id,
        status=binding.status,
        revision_policy=str(metadata.get("revision_policy") or ""),
        pinned_revision_id=(
            str(metadata["pinned_revision_id"])
            if metadata.get("pinned_revision_id")
            else None
        ),
        invocation_policy=str(metadata.get("invocation_policy") or ""),
        row_version=binding.row_version,
    )


@router.post("/lifecycle", response_model=BuiltinSkillCatalogLifecycleRead)
def transition_builtin_skill_catalog_lifecycle(
    request: BuiltinSkillCatalogLifecycleRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BuiltinSkillCatalogLifecycleRead:
    """由租户管理员以双 CAS 执行平台 Skill 普通下架或安全撤销。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    try:
        result = GeneralSkillCatalogGovernanceService(db).lifecycle(
            current_user=current_user,
            skill_id=request.skill_id,
            command_id=request.command_id,
            action=request.action,
            expected_skill_row_version=request.expected_skill_row_version,
            expected_revision_row_version=request.expected_revision_row_version,
            reason=request.reason,
        )
    except CatalogGovernanceError as exc:
        raise _governance_error(exc) from exc
    return BuiltinSkillCatalogLifecycleRead(
        command_id=result.command_id,
        replayed=result.replayed,
        action=result.action,
        skill_id=result.skill_id,
        slug=result.slug,
        skill_status=result.skill_status,
        revision_id=result.revision_id,
        revision_status=result.revision_status,
        skill_row_version=result.skill_row_version,
        revision_row_version=result.revision_row_version,
        deactivated_binding_count=result.deactivated_binding_count,
    )


def _ensure_catalog_tenant(db: Session, tenant_id: str, current_user: User) -> None:
    """校验请求租户作为操作者上下文存在且与 token 一致。"""

    ensure_tenant(db, tenant_id)
    ensure_current_user_tenant(tenant_id, current_user)


def _visible_catalog_rows(
    db: Session,
    tenant_id: str,
    current_user: User,
) -> list[GeneralSkill]:
    """返回管理员全量候选或成员可发现的项目级已发布 Skill。"""

    rows = [
        row
        for row in db.exec(
            select(GeneralSkill).where(
                GeneralSkill.catalog_scope == "platform",
                GeneralSkill.tenant_id.is_(None),
            )
        ).all()
        if _is_managed_catalog(row)
    ]
    if is_admin_user(current_user):
        return rows
    return [row for row in rows if _is_member_visible(db, tenant_id, row)]


def _is_managed_catalog(row: GeneralSkill) -> bool:
    """只把受控 catalog 元数据标记的 Skill 投影到该目录。"""

    return bool((row.metadata_json or {}).get("managed_catalog") is True)


def _is_member_visible(db: Session, tenant_id: str, row: GeneralSkill) -> bool:
    """平台候选发布即进入项目广场，租户安装/绑定另由 Agent binding 控制。"""

    del db, tenant_id
    return (
        row.catalog_scope == "platform"
        and row.tenant_id is None
        and row.visibility_scope == "platform_gallery"
        and row.status == "published"
    )


def _filter_rows(
    db: Session,
    rows: list[GeneralSkill],
    *,
    search: str | None,
    category: str | None,
    source_kind: str | None = None,
    stability: str | None,
    risk_level: str | None,
    invocation_policy: str | None,
    status: str | None,
) -> list[GeneralSkill]:
    """在 Python 投影上执行跨 SQLite/MySQL 一致的目录筛选，搜索可用中文摘要。"""

    needle = search.strip().casefold() if search else None
    filtered: list[GeneralSkill] = []
    for row in rows:
        metadata = row.metadata_json or {}
        if needle:
            revision = _latest_revision(db, row)
            localization = get_revision_localization(
                db,
                revision.id if revision else None,
                skill_id=row.id,
            )
            localized_name = (
                localization.localized_name
                if is_usable_localization(localization, revision)
                else ""
            )
            localized_description = (
                localization.localized_description or ""
                if is_usable_localization(localization, revision)
                else ""
            )
            searchable = " ".join(
                (row.name, row.description or "", localized_name, localized_description)
            )
            if needle not in searchable.casefold():
                continue
        if category and metadata.get("category") != category:
            continue
        if source_kind and metadata.get("source_kind") != source_kind:
            continue
        if stability and metadata.get("stability") != stability:
            continue
        if risk_level and metadata.get("risk_level") != risk_level:
            continue
        if invocation_policy and metadata.get("invocation_policy") != invocation_policy:
            continue
        if status and row.status != status:
            continue
        filtered.append(row)
    return filtered


def _facets(rows: list[GeneralSkill]) -> BuiltinSkillCatalogFacetsRead:
    """由当前权限和筛选结果生成稳定排序的 facets 计数。"""

    def count(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str((row.metadata_json or {}).get(key, "")) for row in rows).items()))

    return BuiltinSkillCatalogFacetsRead(
        category=count("category"),
        source_kind=count("source_kind"),
        stability=count("stability"),
        risk_level=count("risk_level"),
        invocation_policy=count("invocation_policy"),
        status=dict(sorted(Counter(row.status for row in rows).items())),
    )


def _latest_revision(db: Session, row: GeneralSkill) -> GeneralSkillRevision | None:
    """读取同一 Skill 最大修订号，详情页不依赖数据库 JSON 查询。"""

    return db.exec(
        select(GeneralSkillRevision)
        .where(
            GeneralSkillRevision.catalog_scope == row.catalog_scope,
            GeneralSkillRevision.tenant_id == row.tenant_id,
            GeneralSkillRevision.skill_id == row.id,
        )
        .order_by(GeneralSkillRevision.revision_number.desc())
    ).first()


def _item_read(db: Session, row: GeneralSkill) -> BuiltinSkillCatalogItemRead:
    """把一行受控 Skill 和最新修订转换为列表摘要。"""

    revision = _latest_revision(db, row)
    metadata = row.metadata_json or {}
    source = revision.source_snapshot_json if revision else {}
    localization = _localization_fields(db, row, revision)
    return BuiltinSkillCatalogItemRead(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        category=_text(metadata.get("category"), "misc"),
        stability=_text(metadata.get("stability"), "misc"),
        risk_level=_text(metadata.get("risk_level"), "medium"),
        risk_findings=_string_list(metadata.get("risk_findings")),
        invocation_policy=_text(metadata.get("invocation_policy"), "user_only"),
        runtime_mode=_text(metadata.get("runtime_mode"), "guidance_only"),
        source_kind=_text(metadata.get("source_kind"), "platform_builtin"),
        review_status=_text(metadata.get("review_status"), "pending"),
        status=_text(row.status, "draft"),
        source_repository=_text(source.get("source_repository"), ""),
        source_revision=_text(source.get("source_revision"), ""),
        source_path=_text(source.get("source_path"), ""),
        source_license=_text(source.get("source_license"), ""),
        source_package_checksum=_text(source.get("source_package_checksum"), ""),
        source_normalized_checksum=_text(source.get("source_normalized_checksum"), ""),
        content_checksum=_text(source.get("content_checksum"), ""),
        manifest_checksum=_text(source.get("manifest_checksum"), ""),
        revision_id=revision.id if revision else None,
        revision_number=revision.revision_number if revision else None,
        revision_status=revision.status if revision else None,
        resource_count=len(revision.resource_manifest_json or []) if revision else len(row.skill_files_json or []),
        row_version=row.row_version,
        revision_row_version=revision.row_version if revision else None,
        updated_at=row.updated_at.isoformat(),
        **localization,
    )


def _detail_read(
    db: Session,
    row: GeneralSkill,
    revision: GeneralSkillRevision | None,
    *,
    tenant_id: str,
) -> BuiltinSkillCatalogDetailRead:
    """把受控 Skill 详情映射为不含主机路径且仅含当前租户绑定的审核响应。"""

    summary = _item_read_from_revision(db, row, revision)
    parsed_metadata = revision.parsed_metadata_json if revision else {}
    capabilities = revision.requested_capabilities_json if revision else {}
    resources = revision.resource_manifest_json if revision else row.skill_files_json
    localization = get_revision_localization(db, revision.id if revision else None, skill_id=row.id)
    return BuiltinSkillCatalogDetailRead(
        **summary.model_dump(),
        skill_markdown=row.skill_markdown,
        explanation_markdown_zh=(
            localization.explanation_markdown
            if localization is not None and is_usable_localization(localization, revision)
            else None
        ),
        parsed_metadata=dict(parsed_metadata or {}),
        allowed_tools=_string_list(capabilities.get("allowed_tools")),
        argument_hint=_text_or_none(capabilities.get("argument_hint")),
        metadata=dict(row.metadata_json or {}),
        resources=[BuiltinSkillResourceRead.model_validate(item) for item in resources or []],
        bindings=_binding_summaries(db, row.id, tenant_id),
    )


def _binding_summaries(
    db: Session,
    skill_id: str,
    tenant_id: str,
) -> list[BuiltinSkillCatalogBindingSummaryRead]:
    """投影当前租户的 Skill 绑定，屏蔽其他租户的 Agent 和治理事实。"""

    bindings = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == skill_id,
        )
        .order_by(AgentResourceBinding.updated_at.desc(), AgentResourceBinding.id)
    ).all()
    summaries: list[BuiltinSkillCatalogBindingSummaryRead] = []
    for binding in bindings:
        agent = db.get(AgentProfile, binding.agent_id)
        if agent is None or agent.tenant_id != tenant_id:
            continue
        metadata = binding.metadata_json or {}
        invocation_policy = metadata.get("invocation_policy")
        if invocation_policy not in {"model_allowed", "user_only"}:
            invocation_policy = "user_only"
        projection = project_agent_governance(db, agent)
        summaries.append(
            BuiltinSkillCatalogBindingSummaryRead(
                binding_id=binding.id,
                agent_id=agent.id,
                agent_name=agent.name,
                governance_form=projection.form,
                status=binding.status,
                revision_policy=str(metadata.get("revision_policy") or ""),
                pinned_revision_id=(
                    str(metadata["pinned_revision_id"])
                    if metadata.get("pinned_revision_id")
                    else None
                ),
                invocation_policy=invocation_policy,
                row_version=binding.row_version,
            )
        )
    return summaries


def _item_read_from_revision(
    db: Session,
    row: GeneralSkill,
    revision: GeneralSkillRevision | None,
) -> BuiltinSkillCatalogItemRead:
    """在详情转换中复用相同字段映射，避免列表与详情出现不同语义。"""

    metadata = row.metadata_json or {}
    source = revision.source_snapshot_json if revision else {}
    localization = _localization_fields(db, row, revision)
    return BuiltinSkillCatalogItemRead(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        category=_text(metadata.get("category"), "misc"),
        stability=_text(metadata.get("stability"), "misc"),
        risk_level=_text(metadata.get("risk_level"), "medium"),
        risk_findings=_string_list(metadata.get("risk_findings")),
        invocation_policy=_text(metadata.get("invocation_policy"), "user_only"),
        runtime_mode=_text(metadata.get("runtime_mode"), "guidance_only"),
        source_kind=_text(metadata.get("source_kind"), "platform_builtin"),
        review_status=_text(metadata.get("review_status"), "pending"),
        status=_text(row.status, "draft"),
        source_repository=_text(source.get("source_repository"), ""),
        source_revision=_text(source.get("source_revision"), ""),
        source_path=_text(source.get("source_path"), ""),
        source_license=_text(source.get("source_license"), ""),
        source_package_checksum=_text(source.get("source_package_checksum"), ""),
        source_normalized_checksum=_text(source.get("source_normalized_checksum"), ""),
        content_checksum=_text(source.get("content_checksum"), ""),
        manifest_checksum=_text(source.get("manifest_checksum"), ""),
        revision_id=revision.id if revision else None,
        revision_number=revision.revision_number if revision else None,
        revision_status=revision.status if revision else None,
        resource_count=len(revision.resource_manifest_json or []) if revision else len(row.skill_files_json or []),
        row_version=row.row_version,
        revision_row_version=revision.row_version if revision else None,
        updated_at=row.updated_at.isoformat(),
        **localization,
    )


def _localization_fields(
    db: Session | None,
    row: GeneralSkill,
    revision: GeneralSkillRevision | None,
) -> dict[str, object]:
    """投影 revision 级中文摘要，缺少会话时安全回退为未翻译。"""

    if db is None or revision is None:
        return {
            "name_zh": None,
            "description_zh": None,
            "localization_status": None,
            "localization_source_content_checksum": None,
            "localization_checksum": None,
        }
    localization = get_revision_localization(db, revision.id, skill_id=row.id)
    return {
        "name_zh": localization.localized_name if localization and is_usable_localization(localization, revision) else None,
        "description_zh": localization.localized_description if localization and is_usable_localization(localization, revision) else None,
        "localization_status": localization.translation_status if localization else None,
        "localization_source_content_checksum": localization.source_content_checksum if localization else None,
        "localization_checksum": localization.translation_checksum if localization else None,
    }


def _text(value: object, fallback: str) -> str:
    """将 JSON 元数据安全投影成有限文本字段。"""

    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _text_or_none(value: object) -> str | None:
    """将可选 JSON 文本转换为稳定的 None 或非空字符串。"""

    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: object) -> list[str]:
    """过滤 JSON 数组中的空值，避免把非字符串元数据泄漏到契约。"""

    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _import_error(exc: BuiltinSkillCatalogImportError) -> HTTPException:
    """把快照导入领域错误映射为稳定 HTTP 状态和错误码。"""

    status_code = 409 if exc.error_code.endswith("CONFLICT") else 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.error_code, "message": str(exc)},
    )


def _governance_error(exc: CatalogGovernanceError) -> HTTPException:
    """把目录审核/绑定领域错误映射为稳定 HTTP 错误。"""

    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )
