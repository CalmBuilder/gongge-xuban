"""
@Time       : 2026/07/22 09:45
@Author     : zhanglp8181
@File       : curated_gallery_seed.py
@CallChain  : 数据库初始化 → 精选资源 fixture → 租户资源与不可变版本快照
@Description: 幂等导入精选 Agent、SOP、知识库和工具，并保护已发布 SOP 版本。
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, TypeVar
from urllib.parse import urlsplit

from sqlmodel import Session, SQLModel, select

from app.agents.branching import (
    agent_private_metadata,
    ensure_open_gallery_binding,
    open_gallery_metadata,
)
from app.config import get_settings
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    Tool,
    User,
    utc_now,
)
from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.versioning import write_skill_version


TENANT_ID = "tenant_demo"
ADMIN_USER_ID = "admin"
ADMIN_USERNAME = "admin"
ADMIN_DISPLAY_NAME = "Administrator"
SEED_SOURCE = "gongge_curated_gallery_seed"
FIXTURE_PATH = Path(__file__).resolve().parent / "seed_fixtures" / "gongge_curated_gallery_seed.json"

SELECTED_AGENT_NAMES = {"IT", "人事", "法务", "行政", "财务"}
LEGACY_PUBLIC_MOCK_NETLOC = "58.57.119.30:52008"
PUBLIC_MOCK_PATHS = frozenset(
    {
        "/api/mock/admin/room_book",
        "/api/mock/admin/supply_request",
        "/api/mock/contract/archive_query",
        "/api/mock/expense/submit",
        "/api/mock/expense/travel_policy_assess",
        "/api/mock/expense/quota_query",
        "/api/mock/hr/balance_query",
        "/api/mock/hr/cert_issue",
        "/api/mock/hr/leave_apply",
        "/api/mock/invoice/verify",
        "/api/mock/it/grant_permission",
        "/api/mock/it/ticket_create",
    }
)

JsonDict = dict[str, Any]
ModelT = TypeVar("ModelT", bound=SQLModel)


def seed_curated_gallery(session: Session) -> None:
    """Seed the curated Gongge Xuban gallery package as admin-owned resources."""

    if not FIXTURE_PATH.exists():
        return
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    id_maps: dict[str, dict[str, str]] = {
        "agent": {},
        "skill": {},
        "skill_business": {},
        "general_skill": {},
        "tool": {},
        "knowledge_base": {},
        "knowledge_base_version": {},
        "knowledge_document": {},
        "knowledge_bucket": {},
    }

    agents = [
        row for row in data.get("agent_profiles", []) if row.get("name") in SELECTED_AGENT_NAMES
    ]
    selected_agent_ids = {str(row.get("id") or "") for row in agents}
    active_bindings = [
        row
        for row in data.get("agent_resource_bindings", [])
        if row.get("status") == "active" and str(row.get("agent_id") or "") in selected_agent_ids
    ]
    resource_ids = _resource_ids_by_type(active_bindings)

    _seed_agents(session, agents, id_maps)
    _seed_skills(session, data.get("skills", []), data.get("skill_versions", []), resource_ids, id_maps)
    _seed_general_skills(session, data.get("general_skills", []), resource_ids, id_maps)
    _seed_tools(session, data.get("tools", []), resource_ids, id_maps)
    _seed_knowledge(session, data, resource_ids, id_maps)
    session.flush()
    _seed_agent_resource_bindings(session, active_bindings, id_maps)
    _seed_skill_branches(
        session,
        data.get("agent_skill_branches", []),
        data.get("agent_skill_branch_versions", []),
        id_maps,
    )
    _seed_knowledge_branches(session, active_bindings, id_maps)
    session.flush()
    _publish_gallery_resources(session, id_maps)
    _sync_seed_agents_to_current_admin(session, id_maps)


def _seed_agents(session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]) -> None:
    for row in rows:
        source_id = str(row.get("id") or "")
        name = str(row.get("name") or "").strip()
        if not source_id or not name:
            continue
        existing_by_id = session.get(AgentProfile, source_id)
        existing_by_name = session.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == TENANT_ID, AgentProfile.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        metadata = _agent_metadata(row.get("metadata_json"))
        if bool(row.get("is_overall", False)):
            metadata["published_to_gallery"] = False
            metadata.pop("gallery_published_by", None)
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "persona_prompt": row.get("persona_prompt"),
            "is_overall": bool(row.get("is_overall", False)),
            "status": row.get("status") or "active",
            "owner_user_id": (
                None if bool(row.get("is_overall", False)) else ADMIN_USER_ID
            ),
            "published_to_gallery": not bool(row.get("is_overall", False)),
            "gallery_published_by": (
                None if bool(row.get("is_overall", False)) else ADMIN_USER_ID
            ),
            "agent_category_code": (
                "professional"
                if metadata.get("employee_type") == "expert"
                else "assistant"
            ),
            "visibility_scope": (
                "private" if bool(row.get("is_overall", False)) else "tenant"
            ),
            "metadata_json": metadata,
        }
        if existing:
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            if changed:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
            id_maps["agent"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            agent = AgentProfile(id=source_id, **payload)
            session.add(agent)
            id_maps["agent"][source_id] = source_id


def _seed_skills(
    session: Session,
    skill_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """导入 SOP 编辑头，并通过统一版本策略保存精选发布快照。"""

    selected_ids = resource_ids.get("skill", set())
    selected_skill_ids: set[str] = set()
    for row in skill_rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        existing_by_id = session.get(Skill, source_id)
        existing_by_skill_id = session.exec(
            select(Skill).where(Skill.tenant_id == TENANT_ID, Skill.skill_id == skill_id)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_skill_id, source_id)
        content = _json_object(row.get("content_json"))
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": row.get("version") or "1.0.0"})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": row.get("version") or "1.0.0",
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            if existing.version == payload["version"]:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
            target_id = existing.id
        elif existing_by_id or existing_by_skill_id:
            continue
        else:
            skill = Skill(id=source_id, **payload)
            session.add(skill)
            target_id = source_id
        id_maps["skill"][source_id] = target_id
        id_maps["skill_business"][skill_id] = skill_id
        selected_skill_ids.add(skill_id)
    session.flush()

    for row in version_rows:
        skill_id = str(row.get("skill_id") or "").strip()
        if skill_id not in selected_skill_ids:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        content = _json_object(row.get("content_json"))
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": version})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": version,
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        snapshot_candidate = Skill(**payload)
        write_skill_version(
            session,
            snapshot_candidate,
            compiled_definition=compile_legacy_skill_card(content),
            version_id=source_id,
        )


def _seed_general_skills(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("general_skill", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        existing_by_id = session.get(GeneralSkill, source_id)
        existing_by_slug = session.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID, GeneralSkill.slug == slug)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_slug, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "slug": slug,
            "name": row.get("name") or slug,
            "description": row.get("description"),
            "homepage": row.get("homepage"),
            "skill_markdown": row.get("skill_markdown") or "",
            "skill_files_json": _json_list(row.get("skill_files_json")),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
            "status": "published",
            "permissions_json": _json_object(row.get("permissions_json")),
            "runtime_config_json": _json_object(row.get("runtime_config_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["general_skill"][source_id] = existing.id
        elif existing_by_id or existing_by_slug:
            continue
        else:
            session.add(GeneralSkill(id=source_id, **payload))
            id_maps["general_skill"][source_id] = source_id


def _seed_tools(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("tool", set())
    tool_base_url = get_settings().normalized_tool_base_url
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        row = _migrate_public_mock_tool(row, tool_base_url)
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(Tool, source_id)
        existing_by_name = session.exec(
            select(Tool).where(Tool.tenant_id == TENANT_ID, Tool.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "display_name": row.get("display_name"),
            "description": row.get("description"),
            "bucket": row.get("bucket") or "未分桶",
            "tool_type": row.get("tool_type") or "http",
            "method": row.get("method") or "POST",
            "url": row.get("url") or "",
            "headers_json": _json_object(row.get("headers_json")),
            "auth_json": _json_object(row.get("auth_json")),
            "config_json": _json_object(row.get("config_json")),
            "input_schema": _json_object(row.get("input_schema")),
            "output_schema": _json_object(row.get("output_schema")),
            "allowed_skills_json": _json_list(row.get("allowed_skills_json")),
            "mcp_server_id": row.get("mcp_server_id"),
            "enabled": bool(row.get("enabled", True)),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["tool"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(Tool(id=source_id, **payload))
            id_maps["tool"][source_id] = source_id


def _migrate_public_mock_tool(row: JsonDict, base_url: str) -> JsonDict:
    """Rewrite only the known source mock endpoints to this deployment."""
    migrated = dict(row)
    parsed = urlsplit(str(row.get("url") or ""))
    if parsed.netloc != LEGACY_PUBLIC_MOCK_NETLOC or parsed.path not in PUBLIC_MOCK_PATHS:
        return migrated
    migrated["url"] = f"{base_url.rstrip('/')}{parsed.path}"
    headers = _json_object(row.get("headers_json"))
    headers["X-API-Key"] = "${secret.PUBLIC_MOCK_API_KEY}"
    migrated["headers_json"] = headers
    return migrated


def _seed_knowledge(
    session: Session,
    data: JsonDict,
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("knowledge_base", set())
    for row in data.get("knowledge_bases", []):
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(KnowledgeBase, source_id)
        existing_by_name = session.exec(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID, KnowledgeBase.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(KnowledgeBase(id=source_id, **payload))
            id_maps["knowledge_base"][source_id] = source_id
    session.flush()

    _seed_knowledge_versions(session, data.get("knowledge_base_versions", []), id_maps)
    _seed_knowledge_documents(session, data.get("knowledge_documents", []), id_maps)
    _seed_knowledge_buckets(session, data.get("knowledge_buckets", []), id_maps)
    _seed_knowledge_chunks(session, data.get("knowledge_chunks", []), id_maps)
    _seed_knowledge_concepts(session, data.get("knowledge_concepts", []), id_maps)
    _seed_knowledge_discovery(session, data.get("knowledge_discovery_suggestions", []), id_maps)
    _seed_knowledge_jobs(session, data.get("knowledge_ingest_jobs", []), id_maps)


def _seed_knowledge_versions(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        if not kb_id:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(KnowledgeBaseVersion, source_id)
        existing_by_version = session.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.tenant_id == TENANT_ID,
                KnowledgeBaseVersion.knowledge_base_id == kb_id,
                KnowledgeBaseVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "version": version,
            "name": row.get("name") or version,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base_version"][source_id] = existing.id
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(KnowledgeBaseVersion(id=source_id, **payload))
            id_maps["knowledge_base_version"][source_id] = source_id
    session.flush()


def _seed_knowledge_documents(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeDocument, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "filename": row.get("filename") or "document",
            "file_type": row.get("file_type") or "text",
            "title": row.get("title"),
            "status": row.get("status") or "ready",
            "bucket_count": int(row.get("bucket_count") or 0),
            "chunk_count": int(row.get("chunk_count") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "error": row.get("error"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_document"][source_id] = existing.id
        else:
            session.add(KnowledgeDocument(id=source_id, **payload))
            id_maps["knowledge_document"][source_id] = source_id
    session.flush()


def _seed_knowledge_buckets(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeBucket, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_key": row.get("bucket_key") or source_id,
            "title": row.get("title") or "Untitled",
            "summary": row.get("summary") or "",
            "token_estimate": int(row.get("token_estimate") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_bucket"][source_id] = existing.id
        else:
            session.add(KnowledgeBucket(id=source_id, **payload))
            id_maps["knowledge_bucket"][source_id] = source_id
    session.flush()


def _seed_knowledge_chunks(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id or not bucket_id:
            continue
        existing = session.get(KnowledgeChunk, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "chunk_index": int(row.get("chunk_index") or 0),
            "content": row.get("content") or "",
            "summary": row.get("summary"),
            "source_ref": row.get("source_ref"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeChunk(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_concepts(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        concept_id = row.get("concept_id") or str(row.get("id") or "")
        existing = session.get(KnowledgeConcept, str(row.get("id") or "")) or session.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == TENANT_ID,
                KnowledgeConcept.knowledge_base_version_id == kb_ver_id,
                KnowledgeConcept.concept_id == concept_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "concept_id": concept_id,
            "concept_type": row.get("concept_type") or "Concept",
            "title": row.get("title") or concept_id,
            "description": row.get("description"),
            "content_md": row.get("content_md") or "",
            "frontmatter_json": _json_object(row.get("frontmatter_json")),
            "links_json": _json_list(row.get("links_json")),
            "citations_json": _json_list(row.get("citations_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "status": row.get("status") or "active",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeConcept(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_discovery(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        existing = session.get(KnowledgeDiscoverySuggestion, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "suggestion_type": row.get("suggestion_type") or "suggestion",
            "title": row.get("title") or "Suggestion",
            "status": row.get("status") or "pending",
            "payload_json": _json_object(row.get("payload_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "reason": row.get("reason"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeDiscoverySuggestion(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_jobs(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        existing = session.get(KnowledgeIngestJob, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "filename": row.get("filename") or "document",
            "status": row.get("status") or "completed",
            "stage": row.get("stage") or "completed",
            "progress": float(row.get("progress") or 0),
            "error": row.get("error"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "started_at": _parse_datetime(row.get("started_at")),
            "finished_at": _parse_datetime(row.get("finished_at")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeIngestJob(id=str(row.get("id") or ""), **payload))


def _seed_agent_resource_bindings(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """按资源事实幂等初始化 Agent 绑定，仅在契约字段变化时刷新修改时间。"""

    for row in rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        resource_type = str(row.get("resource_type") or "")
        resource_id = _mapped_resource_id(resource_type, str(row.get("resource_id") or ""), id_maps)
        if not agent_id or not resource_id:
            continue
        metadata = agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json")))
        existing = session.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == TENANT_ID,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == resource_type,
                AgentResourceBinding.resource_id == resource_id,
            )
        ).first()
        if existing and (existing.metadata_json or {}).get("seed_binding_suppressed") is True:
            continue
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": "active",
            "metadata_json": (
                agent_private_metadata(
                    agent_id,
                    {
                        **(existing.metadata_json or {}),
                        **metadata,
                    },
                )
                if existing
                else metadata
            ),
        }
        if existing:
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            if changed:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
        else:
            session.add(AgentResourceBinding(id=str(row.get("id") or ""), **payload))


def _seed_skill_branches(
    session: Session,
    branch_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    id_maps: dict[str, dict[str, str]],
) -> None:
    branch_keys: set[tuple[str, str]] = set()
    for row in branch_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        if not agent_id or skill_id not in id_maps["skill_business"]:
            continue
        branch_keys.add((agent_id, skill_id))
        existing = session.get(AgentSkillBranch, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == TENANT_ID,
                AgentSkillBranch.agent_id == agent_id,
                AgentSkillBranch.skill_id == skill_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "base_version": row.get("base_version") or "1.0.0",
            "head_version": row.get("head_version") or row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "metadata_json": agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json"))),
        }
        if existing:
            fixture_head = str(payload["head_version"])
            if existing.sync_state != "synced" or _is_newer_semantic_version(
                existing.head_version,
                fixture_head,
            ):
                continue
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            if changed:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
        else:
            session.add(AgentSkillBranch(id=str(row.get("id") or ""), **payload))
    session.flush()

    for row in version_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        version = str(row.get("version") or "1.0.0")
        if not agent_id or (agent_id, skill_id) not in branch_keys:
            continue
        existing = session.get(AgentSkillBranchVersion, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranchVersion).where(
                AgentSkillBranchVersion.tenant_id == TENANT_ID,
                AgentSkillBranchVersion.agent_id == agent_id,
                AgentSkillBranchVersion.skill_id == skill_id,
                AgentSkillBranchVersion.version == version,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "version": version,
            "base_version": row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "change_summary": row.get("change_summary"),
        }
        if existing:
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            if changed:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
        else:
            session.add(AgentSkillBranchVersion(id=str(row.get("id") or ""), **payload))


def _is_newer_semantic_version(current: str, fixture: str) -> bool:
    """只在双方都是三段式数字版本时判断现有分支是否比 fixture 更新。"""

    def parse(value: str) -> tuple[int, int, int] | None:
        """解析三段式数字版本；自定义版本返回空值并交给既有同步状态保护。"""

        parts = value.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])

    current_key = parse(current)
    fixture_key = parse(fixture)
    return current_key is not None and fixture_key is not None and current_key > fixture_key


def _seed_knowledge_branches(
    session: Session, binding_rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in binding_rows:
        if row.get("resource_type") != "knowledge_base":
            continue
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        kb_id = id_maps["knowledge_base"].get(str(row.get("resource_id") or ""))
        if not agent_id or not kb_id:
            continue
        kb = session.get(KnowledgeBase, kb_id)
        current_version = _knowledge_version_with_seed_content(session, kb_id, agent_id)
        if kb:
            kb_metadata = dict(kb.metadata_json or {})
            kb_metadata["current_version"] = current_version
            if kb.metadata_json != kb_metadata:
                kb.metadata_json = kb_metadata
                kb.updated_at = utc_now()
                session.add(kb)
        existing = session.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == TENANT_ID,
                AgentKnowledgeBranch.agent_id == agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "knowledge_base_id": kb_id,
            "base_version": current_version,
            "head_version": current_version,
            "status": "active",
            "sync_state": "synced",
            "metadata_json": agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json"))),
        }
        if existing:
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            if changed:
                _apply_payload(existing, payload)
                existing.updated_at = utc_now()
                session.add(existing)
        else:
            session.add(AgentKnowledgeBranch(**payload))


def _knowledge_version_with_seed_content(session: Session, kb_id: str, agent_id: str) -> str:
    documents = session.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == TENANT_ID,
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.knowledge_base_version_id != None,  # noqa: E711
        )
    ).all()
    fallback_version: str | None = None
    for document in documents:
        version = session.get(KnowledgeBaseVersion, document.knowledge_base_version_id)
        if not version:
            continue
        fallback_version = fallback_version or version.version
        if (version.metadata_json or {}).get("owner_agent_id") == agent_id:
            return version.version
        if f"branch.{agent_id}." in version.version:
            return version.version
    if fallback_version:
        return fallback_version
    kb = session.get(KnowledgeBase, kb_id)
    if kb:
        return str((kb.metadata_json or {}).get("current_version") or "1.0.0")
    return "1.0.0"


def _publish_gallery_resources(session: Session, id_maps: dict[str, dict[str, str]]) -> None:
    metadata = _open_gallery_seed_metadata({})
    for resource_type, model, map_key in (
        ("skill", Skill, "skill"),
        ("general_skill", GeneralSkill, "general_skill"),
        ("tool", Tool, "tool"),
        ("knowledge_base", KnowledgeBase, "knowledge_base"),
    ):
        for resource_id in set(id_maps[map_key].values()):
            resource = session.get(model, resource_id)
            if resource is None:
                continue
            if hasattr(resource, "metadata_json"):
                resource.metadata_json = _open_gallery_seed_metadata(getattr(resource, "metadata_json", None))
                resource.updated_at = utc_now()
                session.add(resource)
            ensure_open_gallery_binding(
                session,
                TENANT_ID,
                resource_type,
                resource_id,
                "active",
                metadata_json=metadata,
            )


def _sync_seed_agents_to_current_admin(
    session: Session, id_maps: dict[str, dict[str, str]]
) -> None:
    """将精选数字员工及其知识库的正式所有者同步为当前管理员。"""

    admin = session.exec(
        select(User).where(User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME)
    ).first()
    if not admin:
        return
    admin_metadata = {
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "created_by_user_id": admin.id,
        "created_by_username": admin.username,
        "created_by": admin.username,
        "created_by_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "creator_name": admin.username,
    }
    for agent_id in set(id_maps["agent"].values()):
        agent = session.get(AgentProfile, agent_id)
        if agent is None:
            continue
        metadata = dict(agent.metadata_json or {})
        metadata.update(admin_metadata)
        metadata["published_to_gallery"] = not agent.is_overall
        if agent.is_overall:
            metadata.pop("gallery_published_by", None)
        else:
            metadata["gallery_published_by"] = admin.username
        metadata["seed_source"] = SEED_SOURCE
        metadata["managed_by_seed"] = True
        desired = {
            "owner_user_id": None if agent.is_overall else admin.id,
            "published_to_gallery": not agent.is_overall,
            "gallery_published_at": (
                None if agent.is_overall else agent.gallery_published_at or utc_now()
            ),
            "gallery_published_by": None if agent.is_overall else admin.id,
            "agent_category_code": (
                "professional" if metadata.get("employee_type") == "expert" else "assistant"
            ),
            "visibility_scope": "private" if agent.is_overall else "tenant",
            "metadata_json": metadata,
        }
        if any(getattr(agent, key) != value for key, value in desired.items()):
            for key, value in desired.items():
                setattr(agent, key, value)
            agent.updated_at = utc_now()
            session.add(agent)
    for knowledge_base_id in set(id_maps["knowledge_base"].values()):
        knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            continue
        knowledge_base.owner_user_id = admin.id
        knowledge_base.access_scope = "owner"
        knowledge_base.updated_at = utc_now()
        session.add(knowledge_base)


def _resource_ids_by_type(rows: Iterable[JsonDict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in rows:
        resource_type = str(row.get("resource_type") or "")
        resource_id = str(row.get("resource_id") or "")
        if resource_type and resource_id:
            result.setdefault(resource_type, set()).add(resource_id)
    return result


def _mapped_resource_id(
    resource_type: str, source_id: str, id_maps: dict[str, dict[str, str]]
) -> str | None:
    map_key = {
        "skill": "skill",
        "general_skill": "general_skill",
        "tool": "tool",
        "knowledge_base": "knowledge_base",
    }.get(resource_type)
    if map_key is None:
        return None
    return id_maps[map_key].get(source_id)


def _agent_metadata(value: Any) -> JsonDict:
    metadata = _seed_metadata(value)
    metadata.update(
        {
            "published_to_gallery": True,
            "gallery_published_by": ADMIN_USERNAME,
            "seed_source": SEED_SOURCE,
            "managed_by_seed": True,
        }
    )
    metadata.pop("scope", None)
    metadata.pop("visibility", None)
    metadata.pop("owner_agent_id", None)
    metadata.pop("created_from_agent", None)
    return metadata


def _open_gallery_seed_metadata(value: Any) -> JsonDict:
    metadata = open_gallery_metadata(_seed_metadata(value))
    metadata["seed_source"] = SEED_SOURCE
    metadata["managed_by_seed"] = True
    return metadata


def _seed_metadata(value: Any) -> JsonDict:
    metadata = _json_object(value)
    metadata.update(
        {
            "owner_user_id": ADMIN_USER_ID,
            "owner_username": ADMIN_USERNAME,
            "owner_display_name": ADMIN_DISPLAY_NAME,
            "created_by_user_id": ADMIN_USER_ID,
            "created_by_username": ADMIN_USERNAME,
            "created_by": ADMIN_USERNAME,
            "created_by_display_name": ADMIN_DISPLAY_NAME,
            "creator_name": ADMIN_USERNAME,
        }
    )
    return metadata


def _seed_update_target(
    existing_by_id: ModelT | None,
    existing_by_key: ModelT | None,
    source_id: str,
) -> ModelT | None:
    if (
        existing_by_id is not None
        and existing_by_key is not None
        and getattr(existing_by_id, "id", None) != getattr(existing_by_key, "id", None)
    ):
        return existing_by_key if _is_seed_managed(existing_by_key, source_id) else None
    for candidate in (existing_by_id, existing_by_key):
        if candidate is not None and _is_seed_managed(candidate, source_id):
            return candidate
    return None


def _is_seed_managed(row: object, source_id: str) -> bool:
    if getattr(row, "id", None) == source_id:
        return True
    metadata = getattr(row, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return False
    return _is_managed_seed_metadata(metadata)


def _is_managed_seed_metadata(metadata: JsonDict) -> bool:
    source = metadata.get("seed_source")
    return source == SEED_SOURCE or metadata.get("managed_by_seed") is True


def _json_object(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _apply_payload(row: Any, payload: JsonDict) -> None:
    for key, value in payload.items():
        setattr(row, key, value)
