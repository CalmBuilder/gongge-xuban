"""
@Time       : 2026/08/29 15:00
@Author     : zhanglp8181
@File       : builtin_catalog.py
@CallChain  : 应用内置资源 → BuiltinSkillCatalog → normalize_zip_package → Skill 候选审核
@Description: 从随应用交付的固定 Skill 快照生成确定性候选目录，不读取外部目录或远程仓库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import paths
from app.audit.service import append_management_audit
from app.db.models import (
    GeneralSkill,
    GeneralSkillCatalogCommand,
    GeneralSkillRevision,
    Tenant,
    User,
)
from app.general_skills.package_security import (
    GeneralSkillPackageError,
    NormalizedResource,
    NormalizedSkillPackage,
    SkillCandidate,
    normalize_zip_package,
)
from app.general_skills.object_store import FileSystemSkillObjectStore
from app.general_skills.localization import reconcile_builtin_skill_localizations
from app.general_skills.remote_source import (
    GITHUB_ARCHIVE_HOSTS,
    SKILLHUB_DOWNLOAD_HOSTS,
    GeneralSkillRemoteSourceError,
    RemoteFetcher,
    github_archive_url,
    skillhub_archive_url,
    validated_remote_reference,
)


BUILTIN_SKILL_SOURCE_REPOSITORY = "https://github.com/mattpocock/skills"
BUILTIN_SKILL_SOURCE_REVISION = "6654f6b60cd9d5be8b54c6fafe44346dabeb3b76"
BUILTIN_SKILL_SOURCE_LICENSE = "MIT"
BUILTIN_SKILL_INITIAL_IMPORT_COMMAND_ID = "builtin-skill-initial-6654f6b6"
BUILTIN_SKILL_FIXTURE_RELATIVE_PATH = Path(
    "app",
    "db",
    "seed_fixtures",
    "otherpro_skills_catalog_6654f6b6.zip",
)
BUILTIN_SKILL_EXPECTED_COUNT = 37
BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM = (
    "74a2c34b31b5ec98574126861a58d90431543349805bea44b50c02156596aad8"
)
BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM = (
    "73242b81705d225548b725135a85174cf7af90b87d4a4079afe3ad8088b78075"
)

_CATEGORY_STABILITY = {
    "engineering": "stable",
    "productivity": "stable",
    "in-progress": "beta",
    "misc": "misc",
}
_SCRIPT_EXTENSIONS = frozenset({".js", ".jsx", ".py", ".sh", ".ts", ".tsx"})
_HIGH_RISK_PATTERNS = (
    ("network_access", re.compile(r"\b(?:curl|wget|fetch|requests\.|urllib\.)\b")),
    (
        "destructive_command",
        re.compile(r"\b(?:rm\s+-rf|sudo|drop\s+table|git\s+push)\b", re.IGNORECASE),
    ),
)
_MEDIUM_RISK_PATTERNS = (
    ("external_reference", re.compile(r"https?://")),
    (
        "credential_handling",
        re.compile(r"\b(?:api[_ -]?key|password|secret|token|credential)s?\b", re.IGNORECASE),
    ),
)


class BuiltinSkillCatalogError(ValueError):
    """表示项目内置 Skill 快照缺失、篡改或候选契约不满足。"""


class BuiltinSkillCatalogImportError(RuntimeError):
    """表示内置 Skill 快照导入未能安全完成或命令发生冲突。"""

    def __init__(self, error_code: str, detail: str) -> None:
        """保存供管理 API 和批次报告使用的稳定错误码。"""

        super().__init__(detail)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class BuiltinSkillFile:
    """保存一个相对 Skill 根目录的规范文件及内容摘要。"""

    relative_path: str
    content: bytes
    content_checksum: str
    size: int
    media_type: str
    is_text: bool

    def as_legacy_file(self) -> dict[str, Any]:
        """将规范文件转换为现有 GeneralSkill 行兼容的 JSON 文件项。"""

        return {
            "path": self.relative_path,
            "content": self.content.decode("utf-8") if self.is_text else "",
            "size": self.size,
            "mime_type": self.media_type,
            "content_checksum": self.content_checksum,
            "is_text": self.is_text,
        }

    def as_resource_manifest(self) -> dict[str, Any]:
        """将规范文件转换为 resolver 可重算 checksum 的资源清单项。"""

        return {
            "relative_path": self.relative_path,
            "content_checksum": self.content_checksum,
            "size": self.size,
            "media_type": self.media_type,
            "is_text": self.is_text,
        }


@dataclass(frozen=True, slots=True)
class BuiltinSkillCatalogItem:
    """表达一条尚未审核、不可自动运行的项目内置 Skill 候选。"""

    catalog_key: str
    slug: str
    name: str
    description: str
    category: str
    stability: str
    risk_level: str
    risk_findings: tuple[str, ...]
    upstream_invocation_policy: str
    invocation_policy: str
    runtime_mode: str
    review_status: str
    source_repository: str
    source_revision: str
    source_path: str
    source_license: str
    source_package_checksum: str
    source_normalized_checksum: str
    content_checksum: str
    manifest_checksum: str
    parsed_metadata: dict[str, Any]
    allowed_tools: tuple[str, ...]
    argument_hint: str | None
    skill_markdown: str
    files: tuple[BuiltinSkillFile, ...]
    source_kind: str = "platform_builtin"
    source_final_url: str | None = None

    def metadata_json(self, *, import_batch_id: str) -> dict[str, Any]:
        """生成写入 GeneralSkill.metadata_json 的受控目录投影。"""

        return {
            "managed_catalog": True,
            "catalog_key": self.catalog_key,
            "source_kind": self.source_kind,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "source_path": self.source_path,
            "source_license": self.source_license,
            "source_package_checksum": self.source_package_checksum,
            "source_normalized_checksum": self.source_normalized_checksum,
            "source_final_url": self.source_final_url,
            "content_checksum": self.content_checksum,
            "manifest_checksum": self.manifest_checksum,
            "category": self.category,
            "stability": self.stability,
            "risk_level": self.risk_level,
            "risk_findings": list(self.risk_findings),
            "upstream_invocation_policy": self.upstream_invocation_policy,
            "invocation_policy": self.invocation_policy,
            "allowed_tools": list(self.allowed_tools),
            "argument_hint": self.argument_hint,
            "runtime_mode": self.runtime_mode,
            "review_status": self.review_status,
            "import_batch_id": import_batch_id,
            "catalog_schema_version": 1,
        }

    def source_snapshot_json(self, *, import_batch_id: str) -> dict[str, Any]:
        """生成写入 GeneralSkillRevision 的不可变来源与风险证据。"""

        return {
            "source_kind": self.source_kind,
            "managed_catalog": True,
            "catalog_key": self.catalog_key,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "source_path": self.source_path,
            "source_license": self.source_license,
            "source_package_checksum": self.source_package_checksum,
            "source_normalized_checksum": self.source_normalized_checksum,
            "source_final_url": self.source_final_url,
            "content_checksum": self.content_checksum,
            "manifest_checksum": self.manifest_checksum,
            "risk_level": self.risk_level,
            "risk_findings": list(self.risk_findings),
            "runtime_mode": self.runtime_mode,
            "import_batch_id": import_batch_id,
        }

    def report_json(self) -> dict[str, Any]:
        """生成不含 Skill 正文的候选审核摘要。"""

        return {
            "catalog_key": self.catalog_key,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "stability": self.stability,
            "risk_level": self.risk_level,
            "risk_findings": list(self.risk_findings),
            "invocation_policy": self.invocation_policy,
            "runtime_mode": self.runtime_mode,
            "review_status": self.review_status,
            "source_revision": self.source_revision,
            "source_path": self.source_path,
            "source_license": self.source_license,
            "content_checksum": self.content_checksum,
            "manifest_checksum": self.manifest_checksum,
            "resource_count": len(self.files),
        }


@dataclass(frozen=True, slots=True)
class BuiltinSkillCatalog:
    """保存一次固定快照解析的全量摘要和按来源路径排序的候选。"""

    source_repository: str
    source_revision: str
    source_license: str
    source_package_checksum: str
    source_normalized_checksum: str
    items: tuple[BuiltinSkillCatalogItem, ...]
    source_kind: str = "platform_builtin"

    def report_json(self) -> dict[str, Any]:
        """返回可持久化的确定性批次预览，不包含主机路径和文件正文。"""

        return {
            "catalog_schema_version": 1,
            "source_kind": self.source_kind,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "source_license": self.source_license,
            "source_package_checksum": self.source_package_checksum,
            "source_normalized_checksum": self.source_normalized_checksum,
            "skill_count": len(self.items),
            "items": [item.report_json() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class _ExternalSourceDescriptor:
    """保存已规范化的外部来源抓取参数，不携带凭据或主机解析结果。"""

    source_kind: str
    source_reference: str
    source_repository: str
    revision: str | None
    source_subpath: str | None
    source_license: str
    fetch_url: str
    fetch_hosts: frozenset[str]
    request_json: dict[str, Any]


def _external_source_descriptor(
    *,
    source_kind: str,
    source_url: str,
    revision: str | None,
    source_subpath: str | None,
    source_license: str,
    https_allowed_hosts: frozenset[str] | None,
) -> _ExternalSourceDescriptor:
    """校验 GitHub/HTTPS/SkillHub 来源并构造不可漂移的抓取描述。"""

    normalized_license = source_license.strip()
    if not normalized_license or len(normalized_license) > 64:
        raise BuiltinSkillCatalogError("external Skill license evidence is required")
    cleaned_url = source_url.strip()
    if not cleaned_url:
        raise BuiltinSkillCatalogError("external Skill source is required")
    try:
        if source_kind == "github":
            if not revision or not source_subpath:
                raise BuiltinSkillCatalogError(
                    "GitHub catalog import requires a full commit and source subpath"
                )
            repository = validated_remote_reference(
                cleaned_url,
                allowed_hosts=frozenset({"github.com"}),
            )
            fetch_url = github_archive_url(repository, revision)
            fetch_hosts = GITHUB_ARCHIVE_HOSTS
            reference = repository
        elif source_kind == "skillhub":
            fetch_url, reference = skillhub_archive_url(cleaned_url)
            repository = reference
            fetch_hosts = SKILLHUB_DOWNLOAD_HOSTS
        elif source_kind == "https":
            if revision or source_subpath:
                raise BuiltinSkillCatalogError(
                    "HTTPS catalog import cannot carry revision or source subpath"
                )
            if not https_allowed_hosts:
                raise BuiltinSkillCatalogError(
                    "public HTTPS catalog sources are not configured for this deployment"
                )
            reference = validated_remote_reference(
                cleaned_url,
                allowed_hosts=https_allowed_hosts,
            )
            repository = reference
            fetch_url = reference
            fetch_hosts = https_allowed_hosts
        else:
            raise BuiltinSkillCatalogError("unsupported external Skill source kind")
    except GeneralSkillRemoteSourceError:
        raise
    return _ExternalSourceDescriptor(
        source_kind=source_kind,
        source_reference=reference,
        source_repository=repository,
        revision=revision,
        source_subpath=source_subpath.strip("/") if source_subpath else None,
        source_license=normalized_license,
        fetch_url=fetch_url,
        fetch_hosts=fetch_hosts,
        request_json={
            "source_kind": source_kind,
            "source_reference": reference,
            "revision": revision,
            "source_subpath": source_subpath.strip("/") if source_subpath else None,
            "source_license": normalized_license,
        },
    )


def _external_catalog(
    *,
    package: NormalizedSkillPackage,
    source: _ExternalSourceDescriptor,
    final_url: str,
) -> BuiltinSkillCatalog:
    """将远程规范化包转换为项目候选目录，并保留最终跳转证据。"""

    raw_checksum = package.raw_checksum
    normalized_checksum = package.normalized_checksum
    source_revision = source.revision or raw_checksum
    items = tuple(
        _catalog_item(
            candidate,
            raw_checksum,
            normalized_checksum,
            source_kind=source.source_kind,
            source_repository=source.source_repository,
            source_revision=source_revision,
            source_license=source.source_license,
            stability="beta",
            source_final_url=final_url,
        )
        for candidate in package.candidates
    )
    if not items:
        raise BuiltinSkillCatalogError("external Skill package contains no candidates")
    _ensure_unique_catalog_keys(items)
    return BuiltinSkillCatalog(
        source_repository=source.source_repository,
        source_revision=source_revision,
        source_license=source.source_license,
        source_package_checksum=raw_checksum,
        source_normalized_checksum=normalized_checksum,
        items=items,
        source_kind="platform_external",
    )


@dataclass(frozen=True, slots=True)
class BuiltinSkillCatalogImportResult:
    """表达一次内置 Skill 快照入库命令的可重放结果。"""

    command_id: str
    replayed: bool
    created_count: int
    existing_count: int
    items: tuple[dict[str, Any], ...]
    source_kind: str = "platform_builtin"
    source_repository: str = ""
    source_revision: str = ""
    source_license: str = ""
    source_package_checksum: str = ""
    source_normalized_checksum: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转换为命令回执 JSON，供 API 和审计摘要复用。"""

        return {
            "command_id": self.command_id,
            "replayed": self.replayed,
            "created_count": self.created_count,
            "existing_count": self.existing_count,
            "source_kind": self.source_kind,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "source_license": self.source_license,
            "source_package_checksum": self.source_package_checksum,
            "source_normalized_checksum": self.source_normalized_checksum,
            "items": [dict(item) for item in self.items],
        }


class BuiltinSkillCatalogService:
    """在项目级目录中幂等写入内置 Skill 候选和首个 draft 修订。"""

    command_type = "builtin_skill_import"

    def __init__(self, db: Session) -> None:
        """绑定负责目录范围、命令回执和候选入库的数据库会话。"""

        self.db = db

    def import_snapshot(
        self,
        *,
        tenant_id: str,
        command_id: str,
        actor_user_id: str,
    ) -> BuiltinSkillCatalogImportResult:
        """由管理员把固定快照导入项目目录，重放命令时返回原始业务结果。"""

        catalog = load_builtin_skill_catalog()
        result = self.import_catalog(
            catalog=catalog,
            tenant_id=tenant_id,
            command_id=command_id,
            actor_user_id=actor_user_id,
            command_type=self.command_type,
        )
        reconcile_builtin_skill_localizations(
            self.db,
            catalog_items=catalog.items,
            actor_user_id=actor_user_id,
        )
        return result

    def import_external(
        self,
        *,
        tenant_id: str,
        command_id: str,
        actor_user_id: str,
        source_kind: str,
        source_url: str,
        source_license: str,
        revision: str | None,
        source_subpath: str | None,
        fetcher: RemoteFetcher,
        https_allowed_hosts: frozenset[str] | None,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> BuiltinSkillCatalogImportResult:
        """抓取固定外部来源并把规范化候选固化为项目内置待审 Skill。"""

        self._ensure_admin(tenant_id, actor_user_id)
        source = _external_source_descriptor(
            source_kind=source_kind,
            source_url=source_url,
            revision=revision,
            source_subpath=source_subpath,
            source_license=source_license,
            https_allowed_hosts=https_allowed_hosts,
        )
        normalized_command_id = _validated_command_id(command_id)
        request_checksum = _json_checksum(
            {
                "catalog_scope": "platform",
                "command_type": "external_skill_import",
                "command_id": normalized_command_id,
                "source": source.request_json,
            }
        )
        previous = self.db.exec(
            select(GeneralSkillCatalogCommand).where(
                GeneralSkillCatalogCommand.catalog_scope == "platform",
                GeneralSkillCatalogCommand.scope_key == "platform",
                GeneralSkillCatalogCommand.command_type == "external_skill_import",
                GeneralSkillCatalogCommand.command_id == normalized_command_id,
            )
        ).first()
        if previous is not None:
            if previous.request_checksum != request_checksum:
                raise BuiltinSkillCatalogImportError(
                    "GENERAL_SKILL_CATALOG_COMMAND_CONFLICT",
                    "catalog command id was used for another external source request",
                )
            return _import_result_from_command(previous, replayed=True)

        try:
            result = fetcher.fetch(
                source.fetch_url,
                allowed_hosts=source.fetch_hosts,
            )
            package = normalize_zip_package(
                result.payload,
                source_subpath=source.source_subpath,
            )
            catalog = _external_catalog(
                package=package,
                source=source,
                final_url=result.final_url,
            )
        except (
            GeneralSkillRemoteSourceError,
            GeneralSkillPackageError,
            BuiltinSkillCatalogError,
        ) as exc:
            error_code = getattr(exc, "error_code", "GENERAL_SKILL_CATALOG_SOURCE_INVALID")
            raise BuiltinSkillCatalogImportError(error_code, str(exc)) from exc

        return self.import_catalog(
            catalog=catalog,
            tenant_id=tenant_id,
            command_id=normalized_command_id,
            actor_user_id=actor_user_id,
            command_type="external_skill_import",
            request_checksum=request_checksum,
            object_store=object_store,
        )

    def import_catalog(
        self,
        *,
        catalog: BuiltinSkillCatalog,
        tenant_id: str,
        command_id: str,
        actor_user_id: str,
        command_type: str,
        request_checksum: str | None = None,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> BuiltinSkillCatalogImportResult:
        """将候选写入唯一项目目录，以命令号提供跨租户可重放的幂等回执。"""

        self._ensure_admin(tenant_id, actor_user_id)
        normalized_command_id = _validated_command_id(command_id)
        if not command_type or len(command_type) > 64:
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_COMMAND_INVALID",
                "catalog command type is invalid",
            )
        request_checksum = request_checksum or _json_checksum(
            {
                "catalog_scope": "platform",
                "command_type": command_type,
                "command_id": normalized_command_id,
                "catalog": catalog.report_json(),
            }
        )
        previous = self.db.exec(
            select(GeneralSkillCatalogCommand).where(
                GeneralSkillCatalogCommand.catalog_scope == "platform",
                GeneralSkillCatalogCommand.scope_key == "platform",
                GeneralSkillCatalogCommand.command_type == command_type,
                GeneralSkillCatalogCommand.command_id == normalized_command_id,
            )
        ).first()
        if previous is not None:
            if previous.request_checksum != request_checksum:
                raise BuiltinSkillCatalogImportError(
                    "GENERAL_SKILL_CATALOG_COMMAND_CONFLICT",
                    "catalog command id was used for another snapshot request",
                )
            return _import_result_from_command(previous, replayed=True)

        try:
            result_items: list[dict[str, Any]] = []
            created_count = 0
            existing_count = 0
            for item in catalog.items:
                skill = self._find_catalog_skill(item.catalog_key)
                if skill is None:
                    skill, revision = self._create_candidate(
                        actor_user_id=actor_user_id,
                        command_id=normalized_command_id,
                        item=item,
                        object_store=object_store,
                    )
                    created_count += 1
                    action = "created"
                else:
                    revision = self._ensure_existing_candidate(
                        skill=skill,
                        item=item,
                        actor_user_id=actor_user_id,
                        command_id=normalized_command_id,
                        object_store=object_store,
                    )
                    existing_count += 1
                    action = "existing"
                result_items.append(
                    {
                        "catalog_key": item.catalog_key,
                        "skill_id": skill.id,
                        "revision_id": revision.id,
                        "action": action,
                        "status": skill.status,
                    }
                )

            result_json = {
                "source_kind": catalog.source_kind,
                "source_repository": catalog.source_repository,
                "source_revision": catalog.source_revision,
                "source_license": catalog.source_license,
                "source_package_checksum": catalog.source_package_checksum,
                "source_normalized_checksum": catalog.source_normalized_checksum,
                "created_count": created_count,
                "existing_count": existing_count,
                "skill_count": len(result_items),
                "items": result_items,
            }
            command = GeneralSkillCatalogCommand(
                tenant_id=None,
                catalog_scope="platform",
                scope_key="platform",
                command_type=command_type,
                command_id=normalized_command_id,
                request_checksum=request_checksum,
                source_revision=catalog.source_revision,
                status="committed",
                result_json=result_json,
            )
            self.db.add(command)
            self.db.flush()
            append_management_audit(
                self.db,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                actor_display_name=self._actor_display_name(actor_user_id),
                action="general_skill.catalog.import",
                action_kind="create" if created_count else "replay",
                outcome="success",
                resource_type="general_skill_catalog",
                resource_id=command.id,
                request_id=normalized_command_id,
                detail={
                    "command_type": command_type,
                    "source_kind": catalog.source_kind,
                    "source_revision": catalog.source_revision,
                    "skill_count": len(result_items),
                    "created_count": created_count,
                    "existing_count": existing_count,
                },
            )
            self.db.commit()
        except BuiltinSkillCatalogImportError:
            self.db.rollback()
            raise
        except IntegrityError as exc:
            self.db.rollback()
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog snapshot changed concurrently",
            ) from exc
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(command)
        return _import_result_from_command(command, replayed=False)

    def _ensure_admin(self, tenant_id: str, actor_user_id: str) -> None:
        """验证租户、操作者归属和管理员角色，不依赖客户端传入的其他租户事实。"""

        if self.db.get(Tenant, tenant_id) is None:
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_TENANT_NOT_FOUND", "catalog tenant is unavailable"
            )
        actor = self.db.get(User, actor_user_id)
        if actor is None or actor.tenant_id != tenant_id or actor.role != "admin":
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_FORBIDDEN", "only a tenant administrator can import catalog"
            )

    def _actor_display_name(self, actor_user_id: str) -> str | None:
        """读取审计显示名，不把账号凭据或完整用户对象写入回执。"""

        actor = self.db.get(User, actor_user_id)
        if actor is None:
            return None
        return actor.display_name or actor.username

    def _find_catalog_skill(self, catalog_key: str) -> GeneralSkill | None:
        """按项目级来源键查找唯一目录 Skill，避免方言专属 JSON 查询。"""

        matches = [
            skill
            for skill in self.db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.catalog_scope == "platform",
                    GeneralSkill.catalog_key == catalog_key,
                )
            ).all()
            if (skill.metadata_json or {}).get("catalog_key") == catalog_key
        ]
        if len(matches) > 1:
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog key is duplicated in platform catalog",
            )
        return matches[0] if matches else None

    def _create_candidate(
        self,
        *,
        actor_user_id: str,
        command_id: str,
        item: BuiltinSkillCatalogItem,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> tuple[GeneralSkill, GeneralSkillRevision]:
        """创建 draft Skill 和 draft revision，绝不创建 current 或 Agent binding。"""

        skill = GeneralSkill(
            tenant_id=None,
            catalog_scope="platform",
            catalog_key=item.catalog_key,
            slug=self._unique_slug(item.slug),
            name=item.name,
            description=item.description,
            homepage=(
                f"{item.source_repository}/blob/{item.source_revision}/{item.source_path}"
            ),
            skill_markdown=item.skill_markdown,
            skill_files_json=[file.as_legacy_file() for file in item.files],
            metadata_json=item.metadata_json(import_batch_id=command_id),
            status="draft",
            permissions_json={
                "managed_catalog": True,
                "requested_tools": list(item.allowed_tools),
                "atomic_execution_allowed": False,
            },
            runtime_config_json={
                "managed_catalog": True,
                "runtime": "guidance_only",
                "allow_subprocess": False,
            },
            usage_mode="planning_guidance",
            owner_user_id=None,
            visibility_scope="platform_gallery",
            current_published_revision_id=None,
            planning_guidance_json={},
            planning_guidance_checksum=None,
        )
        self.db.add(skill)
        self.db.flush()
        revision = self._create_revision(
            skill=skill,
            item=item,
            actor_user_id=actor_user_id,
            command_id=command_id,
            object_store=object_store,
        )
        return skill, revision

    def _ensure_existing_candidate(
        self,
        *,
        skill: GeneralSkill,
        item: BuiltinSkillCatalogItem,
        actor_user_id: str,
        command_id: str,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> GeneralSkillRevision:
        """确认同来源键只能对应同内容，缺失首个修订时安全补齐而不降级状态。"""

        revisions = self.db.exec(
            select(GeneralSkillRevision).where(
                GeneralSkillRevision.catalog_scope == "platform",
                GeneralSkillRevision.skill_id == skill.id,
            )
        ).all()
        matching = [revision for revision in revisions if revision.content_checksum == item.content_checksum]
        if any(revision.content_checksum != item.content_checksum for revision in revisions):
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_SNAPSHOT_CONFLICT",
                "catalog key already contains another content checksum",
            )
        if len(matching) > 1:
            raise BuiltinSkillCatalogImportError(
                "GENERAL_SKILL_CATALOG_STATE_CONFLICT",
                "catalog skill contains duplicate content revisions",
            )
        if matching:
            return matching[0]
        return self._create_revision(
            skill=skill,
            item=item,
            actor_user_id=actor_user_id,
            command_id=command_id,
            object_store=object_store,
        )

    def _create_revision(
        self,
        *,
        skill: GeneralSkill,
        item: BuiltinSkillCatalogItem,
        actor_user_id: str,
        command_id: str,
        object_store: FileSystemSkillObjectStore | None = None,
    ) -> GeneralSkillRevision:
        """按同一 Skill 的最大修订号追加 draft 修订，保留现有发布指针不变。"""

        latest = self.db.exec(
            select(func.max(GeneralSkillRevision.revision_number)).where(
                GeneralSkillRevision.catalog_scope == "platform",
                GeneralSkillRevision.skill_id == skill.id,
            )
        ).one()
        revision = GeneralSkillRevision(
            tenant_id=None,
            catalog_scope="platform",
            skill_id=skill.id,
            revision_number=int(latest or 0) + 1,
            content_checksum=item.content_checksum,
            manifest_checksum=item.manifest_checksum,
            normalized_skill_markdown=item.skill_markdown,
            parsed_metadata_json=dict(item.parsed_metadata),
            resource_manifest_json=[
                _resource_manifest(file, object_store=object_store) for file in item.files
            ],
            requested_capabilities_json={
                "allowed_tools": list(item.allowed_tools),
                "allowed_tools_declared": "allowed-tools" in item.parsed_metadata,
                "invocation_policy": item.invocation_policy,
                "upstream_invocation_policy": item.upstream_invocation_policy,
                "argument_hint": item.argument_hint,
                "runtime_mode": item.runtime_mode,
            },
            source_snapshot_json=item.source_snapshot_json(import_batch_id=command_id),
            status="draft",
            created_by=actor_user_id,
        )
        self.db.add(revision)
        self.db.flush()
        return revision

    def _unique_slug(self, slug: str) -> str:
        """为项目目录内 slug 冲突生成确定性后缀，不覆盖既有 Skill。"""

        base = re.sub(r"[^a-z0-9]+", "-", slug.casefold()).strip("-") or "builtin-skill"
        used = {
            value
            for value in self.db.exec(
                select(GeneralSkill.slug).where(GeneralSkill.catalog_scope == "platform")
            ).all()
            if isinstance(value, str)
        }
        if base not in used:
            return base[:160]
        candidate = f"{base[:145]}-builtin-{BUILTIN_SKILL_SOURCE_REVISION[:8]}"
        suffix = 2
        while candidate in used:
            candidate = f"{base[:140]}-builtin-{BUILTIN_SKILL_SOURCE_REVISION[:8]}-{suffix}"
            suffix += 1
        return candidate[:160]


def reconcile_builtin_skill_catalogs(
    db: Session,
    *,
    tenant_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """确保一次项目级快照导入，不再按租户复制 Skill 主体。

    ``tenant_ids`` 仅作为兼容旧启动调用的操作者候选范围；它不会决定目录资产的
    归属，也不会为每个租户创建 Skill 行。没有可用管理员时返回明确的待处理结果，
    由管理员稍后从任一租户管理入口重试。
    """

    requested = {str(item).strip() for item in tenant_ids or () if str(item).strip()}
    statement = (
        select(User)
        .where(User.role == "admin", User.membership_status == "active")
        .order_by(User.created_at.asc(), User.id.asc())
    )
    admins = db.exec(statement).all()
    admin = next((item for item in admins if not requested or item.tenant_id in requested), None)
    if admin is None:
        return [
            {
                "catalog_scope": "platform",
                "status": "skipped_no_active_admin",
                "created_count": 0,
                "existing_count": 0,
            }
        ]
    result = BuiltinSkillCatalogService(db).import_snapshot(
        tenant_id=admin.tenant_id,
        command_id=BUILTIN_SKILL_INITIAL_IMPORT_COMMAND_ID,
        actor_user_id=admin.id,
    )
    return [
        {
            "catalog_scope": "platform",
            "operator_tenant_id": admin.tenant_id,
            "status": "replayed" if result.replayed else "imported",
            "created_count": result.created_count,
            "existing_count": result.existing_count,
            "skill_count": len(result.items),
            "command_id": result.command_id,
        }
    ]


def load_builtin_skill_catalog(
    *,
    payload: bytes | None = None,
    fixture_path: Path | None = None,
) -> BuiltinSkillCatalog:
    """读取固定 fixture 并生成 37 条候选，checksum 不符时立即失败。"""

    archive = payload if payload is not None else _read_fixture(fixture_path)
    package = normalize_zip_package(archive)
    if package.raw_checksum != BUILTIN_SKILL_EXPECTED_PACKAGE_CHECKSUM:
        raise BuiltinSkillCatalogError("built-in Skill fixture checksum does not match")
    if package.normalized_checksum != BUILTIN_SKILL_EXPECTED_NORMALIZED_CHECKSUM:
        raise BuiltinSkillCatalogError("built-in Skill normalized checksum does not match")
    if len(package.candidates) != BUILTIN_SKILL_EXPECTED_COUNT:
        raise BuiltinSkillCatalogError("built-in Skill candidate count does not match")
    items = tuple(
        _catalog_item(
            candidate,
            package.raw_checksum,
            package.normalized_checksum,
        )
        for candidate in package.candidates
    )
    _ensure_unique_catalog_keys(items)
    return BuiltinSkillCatalog(
        source_repository=BUILTIN_SKILL_SOURCE_REPOSITORY,
        source_revision=BUILTIN_SKILL_SOURCE_REVISION,
        source_license=BUILTIN_SKILL_SOURCE_LICENSE,
        source_package_checksum=package.raw_checksum,
        source_normalized_checksum=package.normalized_checksum,
        items=items,
    )


def _read_fixture(fixture_path: Path | None) -> bytes:
    """从应用资源目录读取固定 fixture，不回退到工作区外部来源。"""

    path = fixture_path or paths.resource_dir() / BUILTIN_SKILL_FIXTURE_RELATIVE_PATH
    if not path.is_file():
        raise BuiltinSkillCatalogError("built-in Skill fixture is missing")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BuiltinSkillCatalogError("built-in Skill fixture cannot be read") from exc


def _catalog_item(
    candidate: SkillCandidate,
    package_checksum: str,
    normalized_checksum: str,
    *,
    source_kind: str = "platform_builtin",
    source_repository: str = BUILTIN_SKILL_SOURCE_REPOSITORY,
    source_revision: str = BUILTIN_SKILL_SOURCE_REVISION,
    source_license: str = BUILTIN_SKILL_SOURCE_LICENSE,
    stability: str | None = None,
    source_final_url: str | None = None,
) -> BuiltinSkillCatalogItem:
    """把通用包解析候选投影为内置目录项并执行风险扫描。"""

    category = _category(candidate.manifest_path, source_kind=source_kind)
    files = tuple(_skill_file(resource, candidate.root) for resource in candidate.resources)
    markdown_file = next((item for item in files if item.relative_path == "SKILL.md"), None)
    if markdown_file is None or not markdown_file.is_text:
        raise BuiltinSkillCatalogError("built-in Skill candidate has no text SKILL.md")
    findings = _risk_findings(files)
    risk_level = _risk_level(findings)
    invocation_policy = "user_only" if risk_level == "high" else candidate.invocation_policy
    catalog_key = (
        f"platform_builtin:{source_revision}:{candidate.manifest_path}"
        if source_kind == "platform_builtin"
        else (
            f"platform_external:{_source_token(source_repository)}:{source_revision}:"
            f"{candidate.manifest_path}"
        )
    )
    return BuiltinSkillCatalogItem(
        catalog_key=catalog_key,
        slug=candidate.name,
        name=candidate.name,
        description=candidate.description,
        category=category,
        stability=stability or _CATEGORY_STABILITY.get(category, "beta"),
        risk_level=risk_level,
        risk_findings=tuple(findings),
        upstream_invocation_policy=candidate.invocation_policy,
        invocation_policy=invocation_policy,
        runtime_mode="guidance_only",
        review_status="pending",
        source_repository=source_repository,
        source_revision=source_revision,
        source_path=candidate.manifest_path,
        source_license=source_license,
        source_package_checksum=package_checksum,
        source_normalized_checksum=normalized_checksum,
        content_checksum=candidate.content_checksum,
        manifest_checksum=candidate.manifest_checksum,
        parsed_metadata=dict(candidate.metadata),
        allowed_tools=candidate.allowed_tools,
        argument_hint=candidate.argument_hint,
        skill_markdown=markdown_file.content.decode("utf-8"),
        files=files,
        source_kind=source_kind,
        source_final_url=source_final_url,
    )


def _skill_file(resource: NormalizedResource, root: str) -> BuiltinSkillFile:
    """把规范资源路径裁剪为候选根内相对路径。"""

    prefix = f"{root}/" if root else ""
    if not resource.path.startswith(prefix):
        raise BuiltinSkillCatalogError("built-in Skill resource escaped candidate root")
    relative_path = resource.path[len(prefix) :]
    if not relative_path or relative_path.startswith("/"):
        raise BuiltinSkillCatalogError("built-in Skill resource path is empty")
    return BuiltinSkillFile(
        relative_path=relative_path,
        content=resource.content,
        content_checksum=resource.content_checksum,
        size=resource.size,
        media_type=resource.media_type,
        is_text=resource.is_text,
    )


def _category(manifest_path: str, *, source_kind: str = "platform_builtin") -> str:
    """从来源路径提取分类，固定内置目录之外的外部包进入可审查分类。"""

    parts = PurePosixPath(manifest_path).parts
    if len(parts) >= 3 and parts[0] == "skills":
        return parts[1]
    if source_kind != "platform_builtin" and parts:
        return parts[0] if len(parts) >= 2 else "misc"
    raise BuiltinSkillCatalogError("built-in Skill path is outside the skills catalog")


def _source_token(source_repository: str) -> str:
    """将外部来源压缩为稳定短令牌，避免目录键携带 URL 标点和超长路径。"""

    return hashlib.sha256(source_repository.encode("utf-8")).hexdigest()[:16]


def _resource_manifest(
    file: BuiltinSkillFile,
    *,
    object_store: FileSystemSkillObjectStore | None,
) -> dict[str, Any]:
    """生成兼容 legacy inline 与内容对象两种运行时读取方式的资源清单。"""

    manifest = file.as_resource_manifest()
    manifest["path"] = file.relative_path
    if object_store is None:
        manifest["legacy_inline"] = True
    else:
        manifest["object_key"] = object_store.put_object(file.content)
    return manifest


def _risk_findings(files: tuple[BuiltinSkillFile, ...]) -> list[str]:
    """对文件名和文本内容做不执行脚本的确定性风险扫描。"""

    findings: set[str] = set()
    for item in files:
        suffix = PurePosixPath(item.relative_path).suffix.lower()
        if suffix in _SCRIPT_EXTENSIONS:
            findings.add(f"script_file:{item.relative_path}")
        if not item.is_text:
            continue
        content = item.content.decode("utf-8")
        for code, pattern in (*_HIGH_RISK_PATTERNS, *_MEDIUM_RISK_PATTERNS):
            if pattern.search(content):
                findings.add(f"{code}:{item.relative_path}")
    return sorted(findings)


def _risk_level(findings: list[str]) -> str:
    """将风险证据投影为 low、medium 或 high，不替代人工审核。"""

    if any(finding.startswith(("network_access:", "destructive_command:")) for finding in findings):
        return "high"
    if findings:
        return "medium"
    return "low"


def _ensure_unique_catalog_keys(items: tuple[BuiltinSkillCatalogItem, ...]) -> None:
    """拒绝同一快照生成重复来源键，避免重命名导致错误合并。"""

    keys = [item.catalog_key for item in items]
    if len(keys) != len(set(keys)):
        raise BuiltinSkillCatalogError("built-in Skill catalog keys are not unique")


def _json_checksum(value: object) -> str:
    """对命令输入生成不依赖字典顺序的 SHA-256 请求摘要。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_command_id(value: str) -> str:
    """拒绝空白和控制字符命令号，避免幂等键被不同表示方式绕过。"""

    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise BuiltinSkillCatalogImportError(
            "GENERAL_SKILL_CATALOG_COMMAND_INVALID", "catalog command id is invalid"
        )
    return normalized


def _import_result_from_command(
    command: GeneralSkillCatalogCommand,
    *,
    replayed: bool,
) -> BuiltinSkillCatalogImportResult:
    """从持久化命令回执恢复稳定的导入结果，不重新扫描或写入业务事实。"""

    result = command.result_json or {}
    items = tuple(
        dict(item)
        for item in result.get("items", [])
        if isinstance(item, dict)
    )
    return BuiltinSkillCatalogImportResult(
        command_id=command.command_id,
        replayed=replayed,
        created_count=int(result.get("created_count", 0)),
        existing_count=int(result.get("existing_count", 0)),
        items=items,
        source_kind=str(result.get("source_kind") or "platform_builtin"),
        source_repository=str(result.get("source_repository") or ""),
        source_revision=str(result.get("source_revision") or command.source_revision),
        source_license=str(result.get("source_license") or ""),
        source_package_checksum=str(result.get("source_package_checksum") or ""),
        source_normalized_checksum=str(result.get("source_normalized_checksum") or ""),
    )
