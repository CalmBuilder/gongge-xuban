"""
@Time       : 2026/08/31
@Author     : zhanglp8181
@File       : builtin.py
@CallChain  : 应用启动 → 内置专家固定包校验 → tenant_demo/AgentProfile → 专家广场与聊天
@Description: 读取随产品交付的 Agency Agents 中文专家包，并将审核通过的内置专家幂等写入租户数据库。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlmodel import Session, select

from app import paths
from app.agents.identity import agent_is_imported_expert_template
from app.db.models import AgentProfile, Tenant, User, utc_now
from app.experts.schema import PreparedExpert


BUILTIN_EXPERT_SOURCE_REPOSITORY = "https://github.com/msitarzewski/agency-agents"
BUILTIN_EXPERT_SOURCE_COMMIT = "3c9588880b7cafaec325a104899fd8bbe27e7d72"
BUILTIN_EXPERT_SOURCE_LICENSE = "MIT"
BUILTIN_EXPERT_SOURCE_BATCH_ID = "expertimport_116aa5e464fd4c9c902e0d6bc9f8d5e2"
BUILTIN_EXPERT_IMPORT_BATCH_ID = f"builtin-agency-agents-{BUILTIN_EXPERT_SOURCE_COMMIT[:12]}"
BUILTIN_EXPERT_FIXTURE_RELATIVE_PATH = Path(
    "app",
    "experts",
    "data",
    "agency_agents_builtin_v2.json",
)
BUILTIN_EXPERT_EXPECTED_COUNT = 273
BUILTIN_EXPERT_FORMAT_VERSION = "1"
BUILTIN_EXPERT_TAXONOMY_VERSION = 2
BUILTIN_EXPERT_HISTORICAL_SOURCE_COMMIT = "459dce837db3bdfdc4763d3fefd1fd854e73c8f1"
BUILTIN_EXPERT_HISTORICAL_TRANSLATION_BATCH_ID = "expertimport_a51f0554f6cc4150890b954c56fb12aa"
BUILTIN_EXPERT_CURRENT_TRANSLATION_BATCH_ID = "expertimport_116aa5e464fd4c9c902e0d6bc9f8d5e2"
BUILTIN_EXPERT_EXPECTED_MANIFEST_SHA256 = (
    "d479ef1fd233a886086e2b8cfe5b0cb8ebda13de48c85d4986f68a6f976f4d7e"
)
BUILTIN_EXPERT_EXPECTED_FILE_SHA256 = (
    "b42150fbd2425abcc98d17bd15802168bfff8785ff6dbb113abb8ce8f2141968"
)


class BuiltinExpertPackageError(ValueError):
    """表示随产品交付的内置专家固定包缺失、篡改或结构不可信。"""


class BuiltinExpertRecord(BaseModel):
    """保存一条内置专家正文、能力清单、中文化来源和二级分类。"""

    model_config = ConfigDict(frozen=True)

    expert: PreparedExpert
    translation_source_commit: str
    translation_source_batch_id: str
    translation_source_manifest_sha256: str
    translation_sha256: str
    taxonomy_version: Literal[2] = 2
    expert_subcategory: str
    expert_subcategory_original: str = ""
    expert_subcategory_basis: Literal["upstream_directory", "curated_role_mapping"]


class BuiltinExpertManifest(BaseModel):
    """定义内置专家固定包的来源、版本、数量和总摘要契约。"""

    model_config = ConfigDict(frozen=True)

    format_version: Literal["1"] = "1"
    source_repository: str
    source_commit: str
    source_batch_id: str
    source_license: Literal["MIT"] = "MIT"
    translation_manifest_sha256: str
    experts: list[BuiltinExpertRecord]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class BuiltinExpertPackage:
    """返回已完成文件摘要和模型校验的内置专家包。"""

    manifest: BuiltinExpertManifest
    records: tuple[BuiltinExpertRecord, ...]


@dataclass(frozen=True, slots=True)
class BuiltinExpertSeedResult:
    """记录一次内置专家种子的新增、升级和保持不变数量。"""

    created_count: int
    updated_count: int
    unchanged_count: int
    total_count: int


def _canonical(value: object) -> bytes:
    """按固定 JSON 规则序列化校验对象，保证不同数据库和平台摘要一致。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    """计算固定专家包内容的 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def _fixture_hash(payload: bytes) -> str:
    """计算兼容 Git 跨平台换行转换的固定专家包摘要。"""

    return _sha256(payload.replace(b"\r\n", b"\n"))


def _manifest_hash(manifest: BuiltinExpertManifest) -> str:
    """计算不包含自身字段的内置专家清单摘要。"""

    return _sha256(_canonical(manifest.model_dump(mode="json", exclude={"manifest_sha256"})))


def _translation_hash(expert: PreparedExpert) -> str:
    """计算中文运行时字段和原始文件摘要的绑定 checksum。"""

    return _sha256(
        _canonical(
            {
                "source_content_sha256": expert.parsed.source_sha256,
                "name_zh": expert.translation.name_zh,
                "description_zh": expert.translation.description_zh,
                "markdown_zh": expert.translation.markdown_zh,
                "category_zh": expert.translation.category_zh,
                "tags_zh": expert.translation.tags_zh,
            }
        )
    )


def _prepared_hash(expert: PreparedExpert) -> str:
    """校验内置包中 PreparedExpert 的内容摘要，防止单条记录被静默改写。"""

    return _sha256(_canonical(expert.model_dump(mode="json", exclude={"content_sha256"})))


def _validate_relative_markdown_path(value: str) -> None:
    """拒绝绝对路径、父目录跳转和非专家 Markdown 路径。"""

    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md" or not value:
        raise BuiltinExpertPackageError(f"Invalid built-in expert path: {value}")


def _validate_manifest(manifest: BuiltinExpertManifest) -> None:
    """校验固定来源、数量、分类、译文和每条专家摘要。"""

    if manifest.format_version != BUILTIN_EXPERT_FORMAT_VERSION:
        raise BuiltinExpertPackageError("Unsupported built-in expert package format")
    if manifest.source_repository != BUILTIN_EXPERT_SOURCE_REPOSITORY:
        raise BuiltinExpertPackageError("Built-in expert source repository does not match")
    if manifest.source_commit != BUILTIN_EXPERT_SOURCE_COMMIT:
        raise BuiltinExpertPackageError("Built-in expert source commit does not match")
    if manifest.source_batch_id != BUILTIN_EXPERT_SOURCE_BATCH_ID:
        raise BuiltinExpertPackageError("Built-in expert source batch does not match")
    if len(manifest.experts) != BUILTIN_EXPERT_EXPECTED_COUNT:
        raise BuiltinExpertPackageError("Built-in expert count does not match")
    if len(manifest.translation_manifest_sha256) != 64:
        raise BuiltinExpertPackageError("Built-in translation manifest checksum is invalid")
    if manifest.manifest_sha256 != BUILTIN_EXPERT_EXPECTED_MANIFEST_SHA256:
        raise BuiltinExpertPackageError("Built-in expert manifest revision does not match")
    if _manifest_hash(manifest) != manifest.manifest_sha256:
        raise BuiltinExpertPackageError("Built-in expert manifest SHA-256 mismatch")

    valid_translation_source = {
        BUILTIN_EXPERT_SOURCE_COMMIT: BUILTIN_EXPERT_CURRENT_TRANSLATION_BATCH_ID,
        BUILTIN_EXPERT_HISTORICAL_SOURCE_COMMIT: BUILTIN_EXPERT_HISTORICAL_TRANSLATION_BATCH_ID,
    }
    seen_paths: set[str] = set()
    for record in manifest.experts:
        expert = record.expert
        upstream_path = expert.parsed.upstream_path
        _validate_relative_markdown_path(upstream_path)
        if upstream_path in seen_paths:
            raise BuiltinExpertPackageError(f"Duplicate built-in expert path: {upstream_path}")
        seen_paths.add(upstream_path)
        if expert.parsed.source_sha256 == "" or len(expert.parsed.source_sha256) != 64:
            raise BuiltinExpertPackageError(f"Invalid source checksum: {upstream_path}")
        if _prepared_hash(expert) != expert.content_sha256:
            raise BuiltinExpertPackageError(f"Prepared expert checksum mismatch: {upstream_path}")
        if _translation_hash(expert) != record.translation_sha256:
            raise BuiltinExpertPackageError(f"Translation checksum mismatch: {upstream_path}")
        if not expert.translation.name_zh.strip() or not expert.translation.markdown_zh.strip():
            raise BuiltinExpertPackageError(f"Built-in expert translation is empty: {upstream_path}")
        if not record.expert_subcategory.strip():
            raise BuiltinExpertPackageError(f"Built-in expert subcategory is empty: {upstream_path}")
        if record.translation_source_commit not in valid_translation_source:
            raise BuiltinExpertPackageError(
                f"Unsupported translation source commit: {upstream_path}"
            )
        if (
            record.translation_source_batch_id
            != valid_translation_source[record.translation_source_commit]
        ):
            raise BuiltinExpertPackageError(
                f"Translation source batch does not match: {upstream_path}"
            )
        if len(record.translation_source_manifest_sha256) != 64:
            raise BuiltinExpertPackageError(
                f"Invalid translation package checksum: {upstream_path}"
            )


def _load_package(payload: bytes) -> BuiltinExpertPackage:
    """从固定字节流解析并验证内置专家包。"""

    if _fixture_hash(payload) != BUILTIN_EXPERT_EXPECTED_FILE_SHA256:
        raise BuiltinExpertPackageError("Built-in expert fixture SHA-256 mismatch")
    try:
        manifest = BuiltinExpertManifest.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise BuiltinExpertPackageError(f"Invalid built-in expert manifest: {exc}") from exc
    _validate_manifest(manifest)
    return BuiltinExpertPackage(manifest=manifest, records=tuple(manifest.experts))


def _read_fixture(fixture_path: Path | None) -> bytes:
    """从开发态或 frozen 资源目录读取唯一内置专家 fixture。"""

    path = fixture_path or paths.resource_dir() / BUILTIN_EXPERT_FIXTURE_RELATIVE_PATH
    if not path.is_file():
        raise BuiltinExpertPackageError("Built-in expert fixture is missing")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BuiltinExpertPackageError("Built-in expert fixture cannot be read") from exc


def load_builtin_expert_package(
    *,
    payload: bytes | None = None,
    fixture_path: Path | None = None,
) -> BuiltinExpertPackage:
    """读取并缓存默认内置包；测试或诊断可传入独立 payload/路径。"""

    if payload is None and fixture_path is None:
        return _load_builtin_expert_package_cached()
    return _load_package(_read_fixture(fixture_path) if payload is None else payload)


@lru_cache(maxsize=1)
def _load_builtin_expert_package_cached() -> BuiltinExpertPackage:
    """缓存默认固定包校验结果，避免每个种子调用重复解析 273 条正文。"""

    return _load_package(_read_fixture(None))


def builtin_expert_agent_id(upstream_path: str, *, tenant_id: str | None = None) -> str:
    """根据租户和上游相对路径生成跨 SQLite/MySQL 的稳定 Agent 主键。"""

    identity = upstream_path if tenant_id in {None, "tenant_demo"} else f"{tenant_id}:{upstream_path}"
    return f"agent_builtin_expert_{_sha256(identity.encode('utf-8'))[:32]}"


def _translation_metadata(record: BuiltinExpertRecord) -> dict[str, object]:
    """生成审核通过且可追溯的中文化元数据。"""

    expert = record.expert
    translation = expert.translation
    return {
        "expert_translation_status": "verified",
        "expert_translation_sha256": record.translation_sha256,
        "expert_translation_source_content_sha256": expert.parsed.source_sha256,
        "expert_translation_source_batch_id": record.translation_source_batch_id,
        "expert_translation_source_commit": record.translation_source_commit,
        "expert_translation_package_sha256": record.translation_source_manifest_sha256,
        "expert_translation_locale": "zh-CN",
        "expert_translation_model_verified": True,
        "expert_translation_category": translation.category_zh,
    }


def _record_metadata(
    record: BuiltinExpertRecord,
    *,
    admin: User,
    manifest: BuiltinExpertManifest,
) -> dict[str, object]:
    """生成平台内置专家的完整来源、审核、分类和可用性投影。"""

    expert = record.expert
    parsed = expert.parsed
    translation = expert.translation
    return {
        "employee_type": "expert",
        "expert_source_code": "agency-agents",
        "expert_source_label": "Agency Agents",
        "role_name": translation.category_zh,
        "expert_category": translation.category_zh,
        "expert_category_original": parsed.category_original,
        "expert_subcategory": record.expert_subcategory,
        "expert_subcategory_original": record.expert_subcategory_original,
        "expert_subcategory_basis": record.expert_subcategory_basis,
        "expert_taxonomy_version": record.taxonomy_version,
        "expert_tags": list(translation.tags_zh),
        "expert_name_original": parsed.name,
        "expert_emoji": parsed.emoji,
        "expert_color": parsed.color,
        "expert_vibe": parsed.vibe,
        "expert_author": parsed.author,
        "expert_declared_tools": list(parsed.tools),
        "expert_services": [service.model_dump(mode="json") for service in parsed.services],
        "expert_capability_manifest": expert.capability_manifest.model_dump(mode="json"),
        "expert_prompt_estimated_tokens": expert.prompt_estimated_tokens,
        "upstream_path": parsed.upstream_path,
        "upstream_url": expert.upstream_url,
        "upstream_commit": manifest.source_commit,
        "upstream_license": BUILTIN_EXPERT_SOURCE_LICENSE,
        "upstream_source_sha256": parsed.source_sha256,
        "import_batch_id": BUILTIN_EXPERT_IMPORT_BATCH_ID,
        "import_content_sha256": expert.content_sha256,
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or admin.username,
        "owner_semantics": "platform_builtin",
        "governance_template": True,
        "system_builtin": True,
        "builtin_expert": True,
        "builtin_expert_key": f"agency-agents:{parsed.upstream_path}",
        "builtin_expert_source_commit": manifest.source_commit,
        "builtin_expert_source_batch_id": manifest.source_batch_id,
        "builtin_expert_package_manifest_sha256": manifest.manifest_sha256,
        "builtin_expert_status": "approved",
        "review_status": "approved",
        "approval_status": "approved",
        "audit_status": "approved",
        "availability_status": "available",
        "published_to_gallery": True,
        "gallery_publication_kind": "platform_builtin",
        "expert_sync_status": "accepted",
        "expert_last_synced_commit": manifest.source_commit,
        "expert_last_synced_batch_id": manifest.source_batch_id,
        "expert_last_accepted_name": translation.name_zh,
        "expert_last_accepted_description": translation.description_zh,
        "expert_last_accepted_persona_prompt": translation.markdown_zh,
        "expert_last_accepted_source_sha256": parsed.source_sha256,
        **_translation_metadata(record),
    }


def _matching_legacy_agent(
    rows_by_path: dict[str, list[AgentProfile]],
    upstream_path: str,
) -> AgentProfile | None:
    """把历史受控导入的同源模板升级为内置行，避免首次升级重复创建。"""

    candidates = [row for row in rows_by_path.get(upstream_path, []) if agent_is_imported_expert_template(row)]
    return sorted(candidates, key=lambda row: row.id)[0] if candidates else None


def _available_builtin_name(
    used_names: set[str],
    requested: str,
) -> str:
    """处理用户既有同名员工，确保内置专家仍能完整落库并可发现。"""

    if requested not in used_names:
        return requested
    suffix = "（平台内置）"
    candidate = f"{requested}{suffix}"
    ordinal = 2
    while candidate in used_names:
        candidate = f"{requested}{suffix}{ordinal}"
        ordinal += 1
    return candidate


def _apply_record(
    session: Session,
    record: BuiltinExpertRecord,
    *,
    manifest: BuiltinExpertManifest,
    admin: User,
    existing: AgentProfile | None,
    used_names: set[str],
) -> tuple[AgentProfile, bool, bool]:
    """创建或修复一条内置专家，并返回行、是否新增和是否发生更新。"""

    expert = record.expert
    translation = expert.translation
    desired_name = translation.name_zh
    if existing is None:
        desired_name = _available_builtin_name(used_names, desired_name)
        agent = AgentProfile(
            id=builtin_expert_agent_id(
                expert.parsed.upstream_path,
                tenant_id=admin.tenant_id,
            ),
            tenant_id=admin.tenant_id,
            name=desired_name,
            description=translation.description_zh,
            persona_prompt=translation.markdown_zh,
            original_name=expert.parsed.name,
            original_description=expert.parsed.description,
            original_persona_prompt=expert.parsed.source_markdown,
            original_locale="en-US",
            is_overall=False,
            status="active",
            owner_user_id=admin.id,
            published_to_gallery=True,
            gallery_published_at=utc_now(),
            gallery_published_by=admin.id,
            agent_category_code="professional",
            visibility_scope="tenant",
            metadata_json=_record_metadata(record, admin=admin, manifest=manifest),
        )
        if desired_name != translation.name_zh:
            agent.metadata_json["builtin_display_name"] = translation.name_zh
        session.add(agent)
        used_names.add(desired_name)
        return agent, True, True

    existing_metadata = existing.metadata_json if isinstance(existing.metadata_json, dict) else {}
    used_names.discard(existing.name)
    desired_name = (
        existing.name
        if existing_metadata.get("builtin_display_name")
        else desired_name
    )
    if desired_name != existing.name and desired_name in used_names:
        desired_name = _available_builtin_name(used_names, desired_name)
    desired_metadata = dict(existing.metadata_json or {})
    desired_metadata.update(_record_metadata(record, admin=admin, manifest=manifest))
    if desired_name != translation.name_zh:
        desired_metadata["builtin_display_name"] = translation.name_zh
    changed_fields = {
        "name": desired_name,
        "description": translation.description_zh,
        "persona_prompt": translation.markdown_zh,
        "original_name": expert.parsed.name,
        "original_description": expert.parsed.description,
        "original_persona_prompt": expert.parsed.source_markdown,
        "original_locale": "en-US",
        "is_overall": False,
        "status": "active",
        "owner_user_id": admin.id,
        "published_to_gallery": True,
        "agent_category_code": "professional",
        "visibility_scope": "tenant",
    }
    content_changed = any(getattr(existing, key) != value for key, value in changed_fields.items())
    metadata_changed = existing.metadata_json != desired_metadata
    publication_changed = (
        existing.gallery_published_at is None or existing.gallery_published_by != admin.id
    )
    changed = content_changed or metadata_changed or publication_changed
    if content_changed:
        existing.profile_revision += 1
    for field, value in changed_fields.items():
        setattr(existing, field, value)
    existing.metadata_json = desired_metadata
    if existing.gallery_published_at is None:
        existing.gallery_published_at = utc_now()
    existing.gallery_published_by = admin.id
    if changed:
        existing.updated_at = utc_now()
        session.add(existing)
    used_names.add(desired_name)
    return existing, False, changed


def seed_builtin_experts(
    session: Session,
    *,
    tenant_id: str = "tenant_demo",
    admin: User | None = None,
) -> BuiltinExpertSeedResult:
    """将审核通过的固定专家包幂等写入演示租户，供 SQLite/MySQL 和 frozen 版共同使用。"""

    package = load_builtin_expert_package()
    administrator = admin or session.exec(
        select(User).where(User.tenant_id == tenant_id, User.username == "admin")
    ).first()
    if administrator is None or administrator.tenant_id != tenant_id or administrator.role != "admin":
        raise BuiltinExpertPackageError("A tenant administrator is required for built-in experts")

    rows = session.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    rows_by_path: dict[str, list[AgentProfile]] = {}
    by_builtin_key: dict[str, AgentProfile] = {}
    used_names = {row.name for row in rows}
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        path = str(metadata.get("upstream_path") or "").strip()
        if path:
            rows_by_path.setdefault(path, []).append(row)
        key = str(metadata.get("builtin_expert_key") or "").strip()
        if key:
            if key in by_builtin_key and by_builtin_key[key].id != row.id:
                raise BuiltinExpertPackageError(f"Duplicate built-in expert key: {key}")
            by_builtin_key[key] = row

    created = 0
    updated = 0
    for record in package.records:
        upstream_path = record.expert.parsed.upstream_path
        key = f"agency-agents:{upstream_path}"
        existing = by_builtin_key.get(key) or _matching_legacy_agent(rows_by_path, upstream_path)
        if existing is None:
            stable_id = builtin_expert_agent_id(upstream_path, tenant_id=tenant_id)
            occupied = session.get(AgentProfile, stable_id)
            if occupied is not None:
                occupied_metadata = (
                    occupied.metadata_json if isinstance(occupied.metadata_json, dict) else {}
                )
                occupied_key = str(occupied_metadata.get("builtin_expert_key") or "")
                if occupied_key != key:
                    raise BuiltinExpertPackageError(
                        f"Built-in expert stable id conflicts with another Agent: {stable_id}"
                    )
                existing = occupied
        _agent, was_created, was_updated = _apply_record(
            session,
            record,
            manifest=package.manifest,
            admin=administrator,
            existing=existing,
            used_names=used_names,
        )
        if was_created:
            created += 1
        elif was_updated:
            updated += 1

    session.flush()
    return BuiltinExpertSeedResult(
        created_count=created,
        updated_count=updated,
        unchanged_count=BUILTIN_EXPERT_EXPECTED_COUNT - created - updated,
        total_count=BUILTIN_EXPERT_EXPECTED_COUNT,
    )


def _active_tenant_administrator(session: Session, tenant_id: str) -> User | None:
    """按稳定顺序选择租户内可承担内置专家治理责任的在职管理员。"""

    return session.exec(
        select(User)
        .where(
            User.tenant_id == tenant_id,
            User.role == "admin",
            User.membership_status == "active",
        )
        .order_by(User.id)
    ).first()


def seed_builtin_experts_for_tenant(
    session: Session,
    *,
    tenant_id: str,
) -> BuiltinExpertSeedResult | None:
    """为已有租户补齐内置专家；没有在职管理员的无效租户暂不写入。"""

    administrator = _active_tenant_administrator(session, tenant_id)
    if administrator is None:
        return None
    return seed_builtin_experts(session, tenant_id=tenant_id, admin=administrator)


def seed_builtin_experts_for_existing_tenants(
    session: Session,
    *,
    exclude_tenant_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, BuiltinExpertSeedResult]:
    """扫描现有租户并幂等补齐各自的已审核内置专家模板。"""

    results: dict[str, BuiltinExpertSeedResult] = {}
    tenants = session.exec(select(Tenant).order_by(Tenant.id)).all()
    for tenant in tenants:
        if tenant.id in exclude_tenant_ids:
            continue
        result = seed_builtin_experts_for_tenant(session, tenant_id=tenant.id)
        if result is not None:
            results[tenant.id] = result
    return results


def _builtin_experts_are_current(
    session: Session,
    *,
    tenant_id: str,
    package: BuiltinExpertPackage,
) -> bool:
    """逐项核对内置专家来源键、发布治理状态和中文正文是否仍与固定包一致。"""

    expected = {
        f"agency-agents:{record.expert.parsed.upstream_path}": record
        for record in package.records
    }
    rows = session.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    builtin_rows: dict[str, AgentProfile] = {}
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        key = str(metadata.get("builtin_expert_key") or "").strip()
        if key not in expected:
            continue
        if key in builtin_rows:
            return False
        builtin_rows[key] = row
    if set(builtin_rows) != set(expected):
        return False

    for key, record in expected.items():
        row = builtin_rows[key]
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        expert = record.expert
        translation = expert.translation
        if (
            row.status != "active"
            or row.published_to_gallery is not True
            or row.agent_category_code != "professional"
            or row.visibility_scope != "tenant"
            or row.description != translation.description_zh
            or row.persona_prompt != translation.markdown_zh
            or row.original_name != expert.parsed.name
            or row.original_description != expert.parsed.description
            or row.original_persona_prompt != expert.parsed.source_markdown
            or metadata.get("builtin_expert") is not True
            or metadata.get("import_content_sha256") != expert.content_sha256
            or metadata.get("builtin_expert_source_commit") != package.manifest.source_commit
            or metadata.get("builtin_expert_package_manifest_sha256")
            != package.manifest.manifest_sha256
            or metadata.get("review_status") != "approved"
            or metadata.get("approval_status") != "approved"
            or metadata.get("audit_status") != "approved"
            or metadata.get("availability_status") != "available"
        ):
            return False
    return True


def ensure_builtin_experts_for_tenant(
    session: Session,
    *,
    tenant_id: str,
) -> BuiltinExpertSeedResult | None:
    """在登录等运行时边界逐项校验并懒补租户的内置专家。"""

    package = load_builtin_expert_package()
    if _builtin_experts_are_current(session, tenant_id=tenant_id, package=package):
        return BuiltinExpertSeedResult(
            created_count=0,
            updated_count=0,
            unchanged_count=len(package.records),
            total_count=len(package.records),
        )
    return seed_builtin_experts_for_tenant(session, tenant_id=tenant_id)


__all__ = [
    "BUILTIN_EXPERT_EXPECTED_COUNT",
    "BUILTIN_EXPERT_FIXTURE_RELATIVE_PATH",
    "BuiltinExpertManifest",
    "BuiltinExpertPackage",
    "BuiltinExpertPackageError",
    "BuiltinExpertRecord",
    "BuiltinExpertSeedResult",
    "builtin_expert_agent_id",
    "ensure_builtin_experts_for_tenant",
    "load_builtin_expert_package",
    "seed_builtin_experts",
    "seed_builtin_experts_for_existing_tenants",
    "seed_builtin_experts_for_tenant",
]
