"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : benchmark_staffdeck_knowledge.py
@CallChain  : 隔离基准 runner → 合成事实/人工标签 → 真实 KnowledgeService → 原始样本
@Description: 固定 SD-8 数据与标签，分层记录无模型召回、SQL、时延及内存，支持独立重算。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import platform
import resource
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, func
from sqlmodel import Session, select

from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDocument,
    Tenant,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.service import KnowledgeService
from app.knowledge import okf

SEED = 20261003
DATA_VERSION = "sd8-facts-v1"
LABEL_VERSION = "sd8-human-expectations-v1"
STAMP = datetime(2026, 1, 1, tzinfo=UTC)
MODELS = (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk, KnowledgeConcept)


def digest(value: object) -> str:
    """对排序后的 JSON 数据生成与数据库方言无关的身份摘要。"""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str
    ).encode()).hexdigest()


def labels() -> list[dict]:
    """冻结人工合成业务事实的期望；不调用检索评分器或模型生成标签。"""
    specs = [
        ("old-concept-121", "control", "concept_control", "ORCHID121", [],
         ["orchid-old"], [], ["ORCHID121 额度 730"]),
        ("old-document-146", "doccontrol", "doc_control", "AMBER146", ["doccontrol-0"],
         ["doccontrol-concept-0"], ["doccontrol-chunk-0-0"], ["AMBER146 额度 820"]),
        ("scale-old", "main", "scale", "CEDAR001", ["main-0"],
         ["main-concept-0"], ["main-chunk-0-0"], ["CEDAR001 额度 910"]),
        ("complete", "main", "scale", "MAPLE002", ["main-1"],
         ["main-concept-1"], ["main-chunk-1-0", "main-chunk-1-1"],
         ["MAPLE002 额度 1500", "MAPLE002 截止 17:30"]),
        ("new-version", "new", "versions", "IRIS003", ["new-0"],
         ["new-concept-0"], ["new-chunk-0-0"], ["IRIS003 额度 1600"]),
        ("frozen-version", "old", "versions", "IRIS003", ["old-0"],
         ["old-concept-0"], ["old-chunk-0-0"], ["IRIS003 额度 600"]),
        ("hidden", "candidate", "versions", "IRIS003", [], [], [], []),
        ("foreign-version", "foreign", "foreign", "IRIS003", [], [], [], []),
        ("no-answer", "main", "scale", "ZIRCON999UNKNOWN", [], [], [], []),
        ("empty", "main", "scale", " ", [], [], [], []),
    ]
    return [dict(id=i, version=v, kb=k, query=q, documents=d, concepts=c, chunks=h,
                 buckets=[identity + "-bucket" for identity in d],
                 aspects=a, answerable=bool(a)) for i, v, k, q, d, c, h, a in specs]


def dataset(scale: int) -> dict:
    """构造实际 1K/10K 主文档及独立 121/146 控制，旧新版本候选均有派生资源。"""
    if scale < 146:
        raise ValueError("scale must cover the 146-document control")
    rows: dict[str, list[dict]] = {m.__tablename__: [] for m in MODELS}
    versions = [("main", "scale", "published", "own"),
                ("control", "concept_control", "published", "own"),
                ("doccontrol", "doc_control", "published", "own"),
                ("old", "versions", "published", "own"),
                ("new", "versions", "published", "own"),
                ("candidate", "versions", "candidate", "own"),
                ("foreign", "foreign", "published", "other")]
    for version, kb, _, tenant in versions:
        count = scale if version == "main" else 146 if version == "doccontrol" else 0 if version == "control" else 1
        for index in range(count):
            identity = f"{version}-{index}"
            text = f"NEBULA{index:05d} unrelated synthetic register"
            if index == 0 and version in {"main", "doccontrol"}:
                text = "CEDAR001 额度 910" if version == "main" else "AMBER146 额度 820"
            if index == 1 and version == "main":
                text = "MAPLE002 额度 1500\n\nMAPLE002 截止 17:30"
            if version in {"old", "new", "candidate", "foreign"}:
                text = f"IRIS003 额度 {600 if version == 'old' else 1600}"
            common = dict(tenant_id=tenant, knowledge_base_id=kb,
                          knowledge_base_version_id=version, created_at=STAMP,
                          updated_at=STAMP + timedelta(seconds=index))
            rows[KnowledgeDocument.__tablename__].append(dict(
                **common, id=identity, title=text.splitlines()[0], filename=identity + ".md",
                file_type="md", status="ready", normalized_source=text,
                metadata_json={"document_card": {"summary": text}},
            ))
            rows[KnowledgeBucket.__tablename__].append(dict(
                **common, id=identity + "-bucket", document_id=identity, bucket_key=identity,
                title=text.splitlines()[0], summary=text, metadata_json={"content": text},
            ))
            for part, content in enumerate(text.split("\n\n")):
                rows[KnowledgeChunk.__tablename__].append(dict(
                    **common, id=f"{version}-chunk-{index}-{part}", document_id=identity,
                    bucket_id=identity + "-bucket", chunk_index=part, content=content,
                    source_ref=identity + ".md",
                ))
            rows[KnowledgeConcept.__tablename__].append(dict(
                **common, id=f"{version}-concept-row-{index}",
                concept_id=f"{version}-concept-{index}", concept_type="Topic", title=text,
                document_id=identity, content_md=text,
                source_refs_json=[{"document_id": identity}],
            ))
    for index in range(121):
        text = "ORCHID121 额度 730" if index == 0 else f"PLUTO{index:05d} unrelated entry"
        rows[KnowledgeConcept.__tablename__].append(dict(
            id=f"control-row-{index}", tenant_id="own", knowledge_base_id="concept_control",
            knowledge_base_version_id="control", concept_id="orchid-old" if index == 0 else f"control-{index}",
            concept_type="Business Rule", title=text, content_md=text,
            created_at=STAMP, updated_at=STAMP + timedelta(seconds=index),
        ))
    return dict(seed=SEED, version=DATA_VERSION, scale=scale, versions=versions, rows=rows)


def seed(db: Session, data: dict) -> dict:
    """仅向调用方自有空库写入固定数据，回读按 tenant/version/table 分层的真实行数。"""
    for tenant in ("own", "other"):
        db.add(Tenant(id=tenant, name=f"SD8 synthetic {tenant}"))
    seen = set()
    for version, kb, state, tenant in data["versions"]:
        if kb not in seen:
            db.add(KnowledgeBase(id=kb, tenant_id=tenant, name=kb))
            seen.add(kb)
        db.add(KnowledgeBaseVersion(id=version, tenant_id=tenant, knowledge_base_id=kb,
                                   name=version, version=version, publication_state=state))
    db.commit()
    for model in MODELS:
        for start in range(0, len(data["rows"][model.__tablename__]), 500):
            db.execute(model.__table__.insert(), [model(**row).model_dump() for row in
                       data["rows"][model.__tablename__][start:start + 500]])
    db.commit()
    counts = {}
    for model in MODELS:
        counts[model.__tablename__] = [list(row) for row in db.exec(select(
            model.tenant_id, model.knowledge_base_version_id, func.count()
        ).group_by(model.tenant_id, model.knowledge_base_version_id).order_by(
            model.tenant_id, model.knowledge_base_version_id
        )).all()]
    return counts


class MeasuredService(KnowledgeService):
    """只观察真实候选加载结果，不更改候选、排序、模型策略或最终证据。"""

    def _load_concepts_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeConcept]:
        """原样调用生产概念加载器并保留进入评分层的物理身份。"""
        rows = super()._load_concepts_for_search(request)
        self.concept_candidates = [row.concept_id for row in rows]
        self.concept_candidate_identities = [dict(id=row.id, concept_id=row.concept_id,
            tenant=row.tenant_id, version=row.knowledge_base_version_id) for row in rows]
        return rows

    def _load_documents_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeDocument]:
        """原样调用生产文档加载器并保留所有候选身份。"""
        rows = super()._load_documents_for_search(request)
        self.document_candidates = [row.id for row in rows]
        return rows


def recall(expected: list[str], actual: list[str]) -> float | None:
    """按人工相关身份重算召回率，空期望不伪造为质量满分。"""
    return len(set(expected) & set(actual)) / len(set(expected)) if expected else None


def quantile(values: list[float], proportion: float) -> float:
    """使用明确的 nearest-rank 总体分位数，不平均各轮分位数。"""
    if not values or not 0 < proportion <= 1:
        raise ValueError("invalid quantile input")
    return sorted(values)[math.ceil(proportion * len(values)) - 1]


def run(
    engine, output: Path, scale: int, rounds: int = 5,
    *, strategies: dict[str, Callable[[], None]] | None = None,
) -> dict:
    """执行完整查询集一轮预热和五轮测量；SDK 禁止调用，原始身份/错误样本全部保存。"""
    output.mkdir(parents=True, exist_ok=False)
    data = dataset(scale)
    queries = labels()
    strategies = strategies or {"production": lambda: None}
    with Session(engine) as db:
        counts = seed(db, data)
    contract = dict(dataset_hash=digest(data), labels_hash=digest(queries), labels=queries,
                    counts=counts, seed=SEED, scale=scale, data_version=DATA_VERSION,
                    label_version=LABEL_VERSION, warmups=1, rounds=rounds,
                    dialect=engine.dialect.name, model_enabled=False,
                    strategies=list(strategies),
                    strategy_order="forward even rounds; reverse odd rounds; same database/data",
                    memory_method="ru_maxrss process lifetime high-water KiB; tracemalloc per query peak bytes",
                    sql_method="before_cursor_execute during service.search only; includes scope resolution",
                    quantile_method="nearest-rank pooled measured queries excluding warmup",
                    machine=dict(platform=platform.platform(), cpu=platform.processor()),
                    sources={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                             (Path(__file__), Path(KnowledgeService.search.__code__.co_filename),
                              Path(okf.__file__))})
    contract["machine"].update(cpu_count=os.cpu_count(),
        memory_total=[line for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:")][0],
        cpu_model=next((line for line in Path("/proc/cpuinfo").read_text().splitlines()
                        if line.startswith("model name")), "unavailable"))
    from sqlalchemy import text
    with engine.connect() as connection:
        contract["database_settings"] = (dict(connection.execute(text(
            "SELECT VERSION() AS version, @@time_zone AS timezone, "
            "@@collation_connection AS collation, @@innodb_buffer_pool_size AS buffer_pool_bytes"
        )).mappings().one()) if engine.dialect.name == "mysql" else
            dict(version=connection.execute(text("SELECT sqlite_version()" )).scalar(),
                 journal_mode=connection.execute(text("PRAGMA journal_mode")).scalar()))
    (output / "contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2))
    sql = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        """只保存 SQL 语句摘要，不写可能包含认证或数据库地址的参数。"""
        sql.append(hashlib.sha256(statement.encode()).hexdigest())

    event.listen(engine, "before_cursor_execute", observe)
    samples = []
    try:
        with gzip.open(output / "samples.jsonl.gz", "wt", encoding="utf-8") as stream, patch(
            "app.llm.client.LLMClient.generate_json", side_effect=AssertionError("NO_MODEL_BENCHMARK")
        ), patch("app.llm.client.LLMClient.generate_text", side_effect=AssertionError("NO_MODEL_BENCHMARK")):
            for round_index in range(rounds + 1):
                ordering = list(strategies.items())
                if round_index % 2:
                    ordering.reverse()
                for strategy_name, activate, label in [
                    (name, activate, label) for name, activate in ordering for label in queries
                ]:
                    activate()
                    sql.clear()
                    with Session(engine) as db:
                        service = MeasuredService(db)
                        service.concept_candidates = []
                        service.concept_candidate_identities = []
                        service.document_candidates = []
                        request = KnowledgeSearchRequest(tenant_id="own", query=label["query"],
                            knowledge_base_ids=[label["kb"]], knowledge_base_version_ids=[label["version"]],
                            desired_evidence="；".join(label["aspects"]) or None)
                        tracemalloc.start()
                        started = time.perf_counter_ns()
                        error = None
                        try:
                            response = service.search(request, model_config=None)
                        except Exception as exc:
                            from app.knowledge.schema import KnowledgeSearchResponse
                            error = dict(type=type(exc).__name__, digest=digest(str(exc)))
                            response = KnowledgeSearchResponse()
                            db.rollback()
                        elapsed = (time.perf_counter_ns() - started) / 1_000_000
                        _current, peak = tracemalloc.get_traced_memory()
                        tracemalloc.stop()
                        bodies = "\n".join([row.content for row in response.chunks] +
                                           [str(row.get("content", "")) for row in response.selected_concepts])
                        sample = dict(round=round_index, strategy=strategy_name,
                            warmup=round_index == 0, query_id=label["id"],
                            latency_ms=elapsed, python_peak_bytes=peak,
                            process_highwater_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                            sql_count=len(sql), sql_hashes=list(sql), error=error,
                            concept_candidates=service.concept_candidates,
                            concept_candidate_identities=service.concept_candidate_identities,
                            document_candidates=service.document_candidates,
                            concepts=[r["concept_id"] for r in response.selected_concepts],
                            documents=[r["id"] for r in response.selected_documents],
                            chunks=[r.id for r in response.chunks],
                            buckets=[r.id for r in response.selected_buckets],
                            aspect_matches=[aspect for aspect in label["aspects"] if aspect in bodies],
                            evidence_aspect_matches=[aspect for aspect in label["aspects"]
                                if aspect in "\n".join(str(row.get("content", ""))
                                                       for row in response.evidence_pack)],
                            evidence_ids=[r.get("chunk_id") for r in response.evidence_pack],
                            outcome=response.outcome, degraded=response.degraded,
                            sufficiency=response.evidence_sufficiency.model_dump(),
                            citations=knowledge_citations_from_results([response.model_dump()]))
                        # 身份回读发生在计时和 SQL 计数边界之外。
                        result_ids = {
                            KnowledgeConcept: [r["id"] for r in response.selected_concepts],
                            KnowledgeDocument: sample["documents"],
                            KnowledgeBucket: sample["buckets"],
                            KnowledgeChunk: sample["chunks"],
                        }
                        sample["result_identities"] = {
                            model.__tablename__: [dict(id=row.id, tenant=row.tenant_id,
                                version=row.knowledge_base_version_id,
                                concept_id=getattr(row, "concept_id", None)) for row in
                                (db.exec(select(model).where(model.id.in_(ids))).all() if ids else [])]
                            for model, ids in result_ids.items()
                        }
                        sample["versions"] = sorted({r["version"] for rows in
                            sample["result_identities"].values() for r in rows})
                        sample["sql_count"] = len(sample["sql_hashes"])
                    stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    stream.flush()
                    samples.append(sample)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        if tracemalloc.is_tracing():
            tracemalloc.stop()
    summary = (summarize(samples, queries) if len(strategies) == 1 else
               dict(strategies={name: summarize([s for s in samples if s["strategy"] == name], queries)
                                for name in strategies}, samples=len(samples)))
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def summarize(samples: list[dict], queries: list[dict]) -> dict:
    """完全从原始逐查询样本和冻结标签重算质量、总体分位数及硬合同。"""
    measured = [s for s in samples if not s["warmup"]]
    per_query = {}
    for label in queries:
        rows = [s for s in measured if s["query_id"] == label["id"]]
        per_query[label["id"]] = [dict(
            concept_candidate_recall=recall(label["concepts"], s["concept_candidates"]),
            document_candidate_recall=recall(label["documents"], s["document_candidates"]),
            selected_document_recall=recall(label["documents"], s["documents"][:5]),
            chunk_recall_at_8=recall(label["chunks"], s["chunks"][:8]),
            bucket_recall_at_4=recall(label["buckets"], s["buckets"][:4]),
            concept_recall_at_4=recall(label["concepts"], s["concepts"][:4]),
            completeness=recall(label["aspects"], s["aspect_matches"]),
            evidence_pack_completeness=(recall(label["aspects"], s["evidence_aspect_matches"])
                                        if label["chunks"] else None),
            unauthorized=sum(r["tenant"] != "own" or r["version"] != label["version"]
                for rows in s["result_identities"].values() for r in rows),
            invalid_citations=sum(
                (c.get("chunk_id") not in s["chunks"] if c.get("chunk_id") else
                 c.get("concept_id") not in s["concepts"])
                for c in s["citations"]),
            no_answer_false_citations=(len(s["chunks"]) + len(s["concepts"])) if not label["answerable"] else 0,
            latency_ms=s["latency_ms"], sql_count=s["sql_count"],
        ) for s in rows]
    return dict(samples=len(samples), measured=len(measured), per_query=per_query,
                p50_ms=quantile([s["latency_ms"] for s in measured], .5),
                p95_ms=quantile([s["latency_ms"] for s in measured], .95),
                errors=sum(s["error"] is not None for s in samples))
