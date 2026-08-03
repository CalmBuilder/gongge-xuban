"""检查并幂等写入 Agency Agents 专家二级分类。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db.models import AgentProfile, utc_now
from app.experts.import_service import ExpertImportError, validate_admin
from app.experts.taxonomy_schema import (
    DEFAULT_TAXONOMY_PATH,
    ExpertTaxonomyError,
    TaxonomyDocument,
    TaxonomyEntry,
    TaxonomyItem,
    TaxonomyResult,
    load_agency_agents_taxonomy,
)


SessionFactory = Callable[[], Session]
TAXONOMY_KEYS = (
    "expert_subcategory",
    "expert_subcategory_original",
    "expert_subcategory_basis",
    "expert_taxonomy_version",
)


class ExpertTaxonomyApplyError(ValueError):
    """分类检查或写入的顶层前置条件不满足。"""


def _iso_now() -> str:
    return utc_now().isoformat()


def _result_path(taxonomy_path: Path, operation: str) -> Path:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%S%f")
    return taxonomy_path.resolve().parent / f"taxonomy-{operation}-{timestamp}.json"


def _write_result(result: TaxonomyResult) -> None:
    target = result.result_path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    content = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _load(
    db_factory: SessionFactory,
    taxonomy_path: Path,
    tenant_id: str,
    admin_username: str,
    expected_count: int | None,
) -> TaxonomyDocument:
    try:
        taxonomy = load_agency_agents_taxonomy(
            taxonomy_path,
            expected_count=expected_count,
        )
        with db_factory() as db:
            validate_admin(db, tenant_id, admin_username)
    except (ExpertTaxonomyError, ExpertImportError) as exc:
        raise ExpertTaxonomyApplyError(str(exc)) from exc
    return taxonomy


def _agency_agents(db: Session, tenant_id: str) -> list[AgentProfile]:
    rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)).all()
    return [
        row
        for row in rows
        if isinstance(row.metadata_json, dict)
        and row.metadata_json.get("employee_type") == "expert"
        and row.metadata_json.get("expert_source_code") == "agency-agents"
    ]


def _desired(entry: TaxonomyEntry, version: int) -> dict[str, object]:
    return {
        "expert_subcategory": entry.subcategory,
        "expert_subcategory_original": entry.subcategory_original,
        "expert_subcategory_basis": entry.basis,
        "expert_taxonomy_version": version,
    }


def _inspect(
    db_factory: SessionFactory,
    taxonomy: TaxonomyDocument,
    tenant_id: str,
) -> list[TaxonomyItem]:
    with db_factory() as db:
        agents = _agency_agents(db, tenant_id)
        by_path: dict[str, AgentProfile] = {}
        duplicate_paths: set[str] = set()
        for agent in agents:
            path = str(agent.metadata_json.get("upstream_path") or "")
            if path in by_path:
                duplicate_paths.add(path)
            else:
                by_path[path] = agent
        taxonomy_paths = {entry.upstream_path for entry in taxonomy.experts}
        items: list[TaxonomyItem] = []
        for entry in taxonomy.experts:
            agent = by_path.get(entry.upstream_path)
            if agent is None:
                items.append(TaxonomyItem(upstream_path=entry.upstream_path, status="missing"))
                continue
            metadata = agent.metadata_json
            if entry.upstream_path in duplicate_paths:
                items.append(
                    TaxonomyItem(
                        upstream_path=entry.upstream_path,
                        status="failed",
                        message="Multiple imported experts use the same upstream path",
                    )
                )
                continue
            if metadata.get("expert_category") != entry.category:
                items.append(
                    TaxonomyItem(
                        upstream_path=entry.upstream_path,
                        status="category_mismatch",
                        agent_id=agent.id,
                        category=entry.category,
                        subcategory=entry.subcategory,
                        message=f"Database category is {metadata.get('expert_category')!r}",
                    )
                )
                continue
            desired = _desired(entry, taxonomy.version)
            unchanged = all(metadata.get(key) == value for key, value in desired.items())
            items.append(
                TaxonomyItem(
                    upstream_path=entry.upstream_path,
                    status="skipped_unchanged" if unchanged else "ready",
                    agent_id=agent.id,
                    category=entry.category,
                    subcategory=entry.subcategory,
                )
            )
        for path, agent in sorted(by_path.items()):
            if path not in taxonomy_paths:
                items.append(
                    TaxonomyItem(
                        upstream_path=path,
                        status="unmapped_agent",
                        agent_id=agent.id,
                        message="Imported expert is absent from this taxonomy version",
                    )
                )
        return items


def check_taxonomy(
    db_factory: SessionFactory,
    taxonomy_path: Path | None,
    tenant_id: str,
    admin_username: str,
    *,
    expected_count: int | None = 263,
) -> TaxonomyResult:
    """只读检查分类表与目标租户专家的覆盖和差异。"""

    source = taxonomy_path or DEFAULT_TAXONOMY_PATH
    taxonomy = _load(db_factory, source, tenant_id, admin_username, expected_count)
    result = TaxonomyResult(
        operation="check",
        tenant_id=tenant_id,
        taxonomy_version=taxonomy.version,
        source_commit=taxonomy.source_commit,
        started_at=_iso_now(),
        finished_at=_iso_now(),
        result_path=_result_path(source, "check"),
        items=_inspect(db_factory, taxonomy, tenant_id),
    )
    _write_result(result)
    return result


def apply_taxonomy(
    db_factory: SessionFactory,
    taxonomy_path: Path | None,
    tenant_id: str,
    admin_username: str,
    *,
    expected_count: int | None = 263,
) -> TaxonomyResult:
    """逐专家事务写入固定二级分类，保留所有其他字段。"""

    source = taxonomy_path or DEFAULT_TAXONOMY_PATH
    taxonomy = _load(db_factory, source, tenant_id, admin_username, expected_count)
    entries = {entry.upstream_path: entry for entry in taxonomy.experts}
    inspected = _inspect(db_factory, taxonomy, tenant_id)
    result_path = _result_path(source, "apply")
    result = TaxonomyResult(
        operation="apply",
        tenant_id=tenant_id,
        taxonomy_version=taxonomy.version,
        source_commit=taxonomy.source_commit,
        started_at=_iso_now(),
        result_path=result_path,
        items=[],
    )
    _write_result(result)
    items: list[TaxonomyItem] = []
    for item in inspected:
        if item.status != "ready" or not item.agent_id:
            items.append(item)
            result = result.model_copy(update={"items": list(items)})
            _write_result(result)
            continue
        entry = entries[item.upstream_path]
        with db_factory() as db:
            try:
                agent = db.get(AgentProfile, item.agent_id)
                if agent is None or agent.tenant_id != tenant_id:
                    updated = item.model_copy(
                        update={"status": "failed", "message": "Agent disappeared"}
                    )
                else:
                    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
                    if (
                        metadata.get("expert_source_code") != "agency-agents"
                        or metadata.get("upstream_path") != entry.upstream_path
                        or metadata.get("expert_category") != entry.category
                    ):
                        updated = item.model_copy(
                            update={"status": "failed", "message": "Agent metadata changed"}
                        )
                    else:
                        next_metadata = dict(metadata)
                        next_metadata.update(_desired(entry, taxonomy.version))
                        agent.metadata_json = next_metadata
                        db.add(agent)
                        db.commit()
                        updated = item.model_copy(update={"status": "updated"})
            except (SQLAlchemyError, ValueError) as exc:
                db.rollback()
                updated = item.model_copy(update={"status": "failed", "message": str(exc)})
        items.append(updated)
        result = result.model_copy(update={"items": list(items)})
        _write_result(result)
    result = result.model_copy(update={"items": items, "finished_at": _iso_now()})
    _write_result(result)
    return result
