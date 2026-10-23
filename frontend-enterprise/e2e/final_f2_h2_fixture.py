"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f2_h2_fixture.py
@CallChain  : F2/JRN06自有启动 → 合法多文档资源 → 原UI/SDK/worker → 只读旁证
@Description: 固定知识和个人事实标签；仅控制自有派发/末端SDK时机，不预置业务成功。
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import time

import final_f1_fullstack_fixture as base

OLD_MARKER = "F2_POLICY_OLD_750"
NEW_MARKER = "F2_POLICY_NEW_1200"
STABLE_MARKER = "F2_STABLE_DOC_UNCHANGED"
OLD_BODY = "# CYGNUS77配送政策\n\nCYGNUS77每批上限750件，必须保留办理日期。\n唯一政策标记：" + OLD_MARKER
NEW_BODY = "# CYGNUS77配送政策\n\nCYGNUS77每批上限1200件，必须保留办理日期。\n唯一政策标记：" + NEW_MARKER
STABLE_BODY = "# 收件核验\n\n收件时核验商品与数量，独立文档标记：" + STABLE_MARKER
FACTS = {
    "base": "F1 完整持久事实：H2_BASE_完整收件要求，先检查外包装，再逐件核验。",
    "late": "F1 完整持久事实：H2_LATE_过期收件要求，不能覆盖更正。",
    "correct": "F1 完整持久事实：H2_CORRECT_完整更正要求，由指定收件人逐件核验并保留原始回执。",
    "clear": "F1 完整持久事实：H2_CLEAR_LATE_清空之前的要求，不得复活。",
    "fresh": "F1 完整持久事实：H2_FRESH_完整新要求，清空后由本人确认核验并保留独立回执。",
}


def control_directory() -> Path:
    """仅访问本次supervisor拥有的控制目录，不读取仓库配置或生产数据。"""
    return Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"])


def seed_h2_resources() -> None:
    """建立两份初始知识正文及其合法派生；实例、任务和修订均须随后UI创建。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (
        KnowledgeBaseVersion, KnowledgeBucket, KnowledgeChunk, KnowledgeConcept, KnowledgeDocument,
        utc_now,
    )
    from app.knowledge.okf import build_okf_for_document, upsert_concepts
    from app.knowledge.service import _build_document_card, _build_section_nodes

    resource = base.load(control_directory(), "initial.safe.json", {})
    with Session(engine) as db:
        target = db.get(KnowledgeDocument, resource["document_id"])
        target.filename = "receiving.md"
        target.title = "独立收件核验"
        for document, body in [
            (target, STABLE_BODY),
            (KnowledgeDocument(tenant_id=base.TENANT,
                               knowledge_base_id=target.knowledge_base_id,
                               knowledge_base_version_id=target.knowledge_base_version_id,
                               filename="cygnus77-policy.md", file_type="md",
                               title="CYGNUS77配送政策", status="ready"), OLD_BODY),
        ]:
            document.normalized_source = body
            document.lineage_id = document.id
            document.source_kind = "normalized"
            document.updated_at = utc_now()
            document.source_checksum = sha256(body.encode()).hexdigest()
            sections = _build_section_nodes(body)
            card = _build_document_card(title=document.title, filename=document.filename,
                                        file_type="md", text=body, section_nodes=sections)
            document.metadata_json = {"document_card": card, "section_tree": sections}
            db.add(document)
            db.commit()

            # 合法初始资源沿F1直接建四层资产；不调用要求持久ingest job的worker私有入口。
            for model in [KnowledgeChunk, KnowledgeConcept, KnowledgeBucket]:
                for previous in db.exec(select(model).where(model.document_id == document.id)).all():
                    db.delete(previous)
            db.flush()
            bucket = KnowledgeBucket(tenant_id=base.TENANT,
                                     knowledge_base_id=document.knowledge_base_id,
                                     knowledge_base_version_id=document.knowledge_base_version_id,
                                     document_id=document.id, bucket_key="policy", title=document.title,
                                     summary=body, metadata_json={"section_ids": [r["section_id"] for r in sections]})
            db.add(bucket)
            db.flush()
            db.add(KnowledgeChunk(tenant_id=base.TENANT,
                                  knowledge_base_id=document.knowledge_base_id,
                                  knowledge_base_version_id=document.knowledge_base_version_id,
                                  document_id=document.id, bucket_id=bucket.id,
                                  chunk_index=0, content=body, source_ref=document.filename))
            document.chunk_count = 1
            document.bucket_count = 1
            upsert_concepts(db, base.TENANT, document.knowledge_base_id,
                            document.knowledge_base_version_id,
                            build_okf_for_document(document, sections, [bucket]))
            db.add(document)
            db.commit()

        # 初始资源也是现代整库快照：以真实资产清单标识，不能留下legacy空manifest。
        version = db.get(KnowledgeBaseVersion, resource["knowledge_base_version_id"])
        manifest = {
            "initial_resource_snapshot": True,
            "assets": {
                model.__tablename__: sorted(r.id for r in db.exec(select(model).where(
                    model.knowledge_base_version_id == version.id)).all())
                for model in [KnowledgeDocument, KnowledgeBucket, KnowledgeChunk, KnowledgeConcept]
            },
        }
        version.content_manifest_json = manifest
        version.content_manifest_hash = sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        db.add(version)
        db.commit()
        # 原页面选择created_at倒序首文档；目标真实后创建，不伪造时间或增加编辑器语义。
        base.save(control_directory(), "initial.safe.json", {**resource, "document_id": document.id})


def policy_reply(payload: dict) -> str:
    """回复只取本次模型输入中的业务回执和知识正文，不从fixture读取新旧答案。"""
    rows = ([r for step in payload["completed_steps"] for r in base.walk(step.get("model_output"))]
            if "completed_steps" in payload else
            list(base.walk(payload.get("tool_result") or payload.get("knowledge_results") or {})))
    receipts = [r for r in rows if r.get("receipt_id")]
    texts = [str(r.get("content") or r.get("excerpt") or r.get("content_md") or "") for r in rows]
    policy = next((v for v in texts if "CYGNUS77" in v and "每批上限" in v), None)
    if not receipts:
        return "尚未取得原始业务回执，请等待原任务完成。"
    r = receipts[-1]
    return (f"配送回执 {r.get('receipt_id')}：商品{r.get('product_name')}，数量{r.get('quantity')}，"
            f"客户{r.get('customer')}，目标日期{r.get('due_date')}。\n"
            + (policy or "当前模型输入没有完整政策证据。"))


def install_h2_controls() -> None:
    """在原守卫之后控制SDK迟到，在原领取之前暂停知识worker；原CAS/事务保持。"""
    from fastapi import Request
    from app.api import knowledge as knowledge_api
    from app.knowledge import revisions
    from app.main import app

    control = control_directory()
    original_runner = revisions.run_revision_job
    original_build = revisions._build_revision_claimed
    original_response = base.model_response
    base.response_text = policy_reply

    def delayed_runner(job_id: str) -> None:
        """暂停尚未领取的原job；重启后仍由原持久恢复扫描领取同一任务。"""
        if (control / "knowledge.pause").exists():
            (control / "knowledge.started").write_text(job_id)
            # 模拟未送达唤醒；不占住线程/租约，重启后的原scanner重新发现持久queued。
            return
        original_runner(job_id)

    def fail_once(db, job_id, worker_id, fence):
        """模拟构建资源一次不可用，异常由原worker持久失败，head不得提前改变。"""
        marker = control / "knowledge.fail-once"
        if marker.exists():
            marker.unlink()
            raise revisions.KnowledgeRevisionInputError("F2_H2_SYNTHETIC_BUILD_UNAVAILABLE")
        return original_build(db, job_id, worker_id, fence)

    def observed_response(system: str, payload: dict) -> object:
        """记录实际消费者本轮完整选择，阶段身份和固定标签之外不公开正文。"""
        memory, context = base.current_context(payload)
        stage = payload.get("_agent_stage") or {}
        phase = stage.get("phase") or (
            "Memory Capture" if "长期记忆抽取" in system else
            "Dynamic Action" if payload.get("current_step") else
            "Dynamic Planner" if "受控动态任务规划器" in system else "non_unified")
        result = original_response(system, payload)
        rendered = json.dumps(result, ensure_ascii=False)
        provider_view = payload.get("provider_execution_view")
        execution_context = (provider_view.get("execution_context")
                             if isinstance(provider_view, dict) else None)
        completed_steps = (execution_context.get("completed_steps")
                           if isinstance(execution_context, dict) else None)
        completed_outputs = [row.get("model_output") for row in completed_steps
                             if isinstance(row, dict) and isinstance(row.get("model_output"), dict)] \
            if isinstance(completed_steps, list) else []
        completed_output_json = json.dumps(completed_outputs, ensure_ascii=False)
        reference_fields = sorted({str(name) for output in completed_outputs for name in output
                                   if str(name).casefold().endswith(("_id", "_key", "_ref",
                                                                     "_reference", "_receipt"))})
        row = {"phase": phase, "pid": os.getpid(), "source_input_id": context.get("source_input_id"),
               "request_payload_hash": base.digest(payload), "memory_hash": base.digest(memory),
               "memory_fact_flags": {k: v in memory for k, v in FACTS.items()},
               "completed_output_reference_fields": reference_fields,
               "completed_output_customer_present": any("customer" in output for output in completed_outputs),
               "completed_output_fact_flags": {k: v in completed_output_json for k, v in FACTS.items()},
               "result_fact_flags": {k: v in rendered for k, v in FACTS.items()},
               "actual_sdk_transport": True, "real_provider_quality": False}
        with base.LOCK:
            rows = base.load(control, "h2-sdk.safe.json", [])
            base.save(control, "h2-sdk.safe.json", [*rows, row])
        return result

    base.model_response = observed_response
    revisions.run_revision_job = delayed_runner
    knowledge_api.run_revision_job = delayed_runner
    revisions._build_revision_claimed = fail_once
    for route in app.routes:
        if getattr(route, "path", "") != "/__e2e/f1/openai/v1/chat/completions":
            continue
        original_endpoint = route.dependant.call

        async def delayed_sdk(request: Request):
            """旧提取已合法发送后暂缓其响应，允许并发更正/清空；不替换源身份。"""
            wire = await request.json()
            messages = wire.get("messages") or []
            system = "\n".join(str(r.get("content") or "") for r in messages if r.get("role") == "system")
            if "长期记忆抽取" in system:
                current = base.current_user_content(base.capture_payload_from_wire(messages, {}))
                label = next((k for k in ["late", "clear"] if current.endswith(FACTS[k])), None)
                if label:
                    (control / f"memory-{label}.started").write_text(base.digest(current))
                    deadline = time.monotonic() + 60
                    while not (control / f"memory-{label}.release").exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("F2_H2_DELAY_RELEASE_REQUIRED")
                        await asyncio.sleep(0.1)
            return await original_endpoint(request)

        route.dependant.call = delayed_sdk


def h2_snapshot() -> dict:
    """只读取四层物理资产、原head、manifest、个人来源代数及冻结选择，不写状态。"""
    from sqlmodel import Session, select
    from app.db import engine
    from app.db.models import (
        AgentKnowledgeBranch, ExecutionSignal, KnowledgeBase, KnowledgeBaseVersion, KnowledgeBucket,
        KnowledgeChunk, KnowledgeConcept, KnowledgeDocument, KnowledgeIngestJob,
        MemoryCaptureJob, MemoryRecord, Message, SopInstance, SopNodeExecution, SopOperation,
    )

    resource = base.load(control_directory(), "initial.safe.json", {})
    kb_id = resource["knowledge_base_id"]
    def projection(value: object) -> dict | None:
        """冻结来源只公开身份/撤销向量及完整正文hash，不复制模型历史或自由输入。"""
        if not isinstance(value, dict):
            return None
        return {"source_input_id": value.get("source_input_id"),
                "projection_id": value.get("projection_id"),
                "policy_version": value.get("policy_version"),
                "derived_values_revoked": value.get("derived_values_revoked"),
                "selected": [{k: r.get(k) for k in ["id", "kind", "key", "source_sequence",
                                                    "scope", "generations", "agent_capture_generation"]}
                             | {"content_hash": base.digest(r.get("content"))}
                             for r in value.get("selected", [])]}
    with Session(engine) as db:
        kb = db.get(KnowledgeBase, kb_id)
        versions = db.exec(select(KnowledgeBaseVersion).where(KnowledgeBaseVersion.knowledge_base_id == kb_id)).all()
        branches = db.exec(select(AgentKnowledgeBranch).where(AgentKnowledgeBranch.knowledge_base_id == kb_id)).all()
        assets = []
        for kind, model in [("document", KnowledgeDocument), ("bucket", KnowledgeBucket),
                            ("chunk", KnowledgeChunk), ("concept", KnowledgeConcept)]:
            for r in db.exec(select(model).where(model.knowledge_base_id == kb_id)).all():
                data = r.model_dump(exclude={"created_at", "updated_at"})
                text = json.dumps(data, ensure_ascii=False, default=str)
                assets.append({"kind": kind, "id": r.id, "version_id": r.knowledge_base_version_id,
                               "document_id": getattr(r, "document_id", None),
                               "bucket_id": getattr(r, "bucket_id", None),
                               "lineage_id": getattr(r, "lineage_id", None),
                               "asset_hash": base.digest(data), "old": OLD_MARKER in text,
                               "new": NEW_MARKER in text, "stable": STABLE_MARKER in text})
        jobs = db.exec(select(KnowledgeIngestJob).where(KnowledgeIngestJob.knowledge_base_id == kb_id)).all()
        memories = db.exec(select(MemoryRecord).where(MemoryRecord.agent_id.in_(list(base.AGENTS.values())))).all()
        captures = db.exec(select(MemoryCaptureJob).where(MemoryCaptureJob.agent_id.in_(list(base.AGENTS.values())))).all()
        instances = db.exec(select(SopInstance).where(SopInstance.agent_id.in_(list(base.AGENTS.values())))).all()
        operations = db.exec(select(SopOperation).where(SopOperation.operation_name == "knowledge.search")).all()
        citations = []
        for message in db.exec(select(Message).where(Message.tenant_id == base.TENANT)).all():
            for citation in (message.metadata_json or {}).get("knowledge_citations", []):
                concept = db.get(KnowledgeConcept, citation.get("concept_id")) if citation.get("concept_id") else None
                document_id = citation.get("document_id") or (concept.document_id if concept else None)
                document = db.get(KnowledgeDocument, document_id) if document_id else None
                content = str(citation.get("content") or citation.get("excerpt") or "")
                citations.append({"message_id": message.id, "session_id": message.session_id,
                                  "id": citation.get("id"), "kind": citation.get("kind"),
                                  "concept_id": concept.id if concept else None,
                                  "concept_path": concept.concept_id if concept else None,
                                  "document_id": document_id,
                                  "version_id": concept.knowledge_base_version_id if concept else
                                                (document.knowledge_base_version_id if document else None),
                                  "knowledge_base_id": concept.knowledge_base_id if concept else
                                                       (document.knowledge_base_id if document else None),
                                  "old": OLD_MARKER in content, "new": NEW_MARKER in content})
        return {"pid": os.getpid(), "knowledge_base_id": kb_id,
                "overall_version": (kb.metadata_json or {}).get("current_version") or "1.0.0",
                "overall_revision": kb.content_revision,
                "versions": [{"id": r.id, "version": r.version, "publication_state": r.publication_state,
                              "manifest": r.content_manifest_json, "manifest_hash": r.content_manifest_hash,
                              "parent_version_id": r.parent_version_id} for r in versions],
                "branches": [{"agent_id": r.agent_id, "head_version": r.head_version,
                              "content_revision": r.content_revision, "status": r.status} for r in branches],
                "assets": assets, "citations": citations,
                "runtime_closure": [{"execution_id": r.id, "status": r.status,
                                     "lease_owner": r.lease_owner,
                                     "lease_expires_at": str(r.lease_expires_at) if r.lease_expires_at else None,
                                     "fencing_token": r.fencing_token,
                                     "terminal_code": (r.terminal_reason_json or {}).get("code"),
                                     "signals": [{"id": s.id, "status": s.status,
                                                  "signal_type": s.signal_type,
                                                  "attempt_count": s.attempt_count,
                                                  "lease_owner": s.lease_owner,
                                                  "error_code": (s.last_error_json or {}).get("code")}
                                                 for s in db.exec(select(ExecutionSignal).where(
                                                     ExecutionSignal.execution_id == r.id)).all()],
                                     "nodes": [{"id": n.id, "step_key": n.step_key, "status": n.status}
                                               for n in db.exec(select(SopNodeExecution).where(
                                                   SopNodeExecution.instance_id == r.id)).all()],
                                     "operations": [{"id": o.id, "status": o.status,
                                                     "operation_name": o.operation_name}
                                                    for o in db.exec(select(SopOperation).where(
                                                        SopOperation.instance_id == r.id)).all()]}
                                    for r in instances if r.kind == "dynamic_task"],
                "knowledge_operations": [{"id": r.id, "execution_id": r.instance_id,
                                        "status": r.status,
                                        "result_hash": base.digest(r.result_json),
                                        "result_keys": sorted((r.result_json or {}).keys()),
                                        "scalar_diagnostics": [{k: v for k, v in item.items()
                                                                if k in {"outcome", "status", "error_code", "code", "phase"}
                                                                and isinstance(v, (str, bool, int))}
                                                               for item in base.walk(r.result_json)],
                                        "old_fact": OLD_MARKER in json.dumps(r.result_json, ensure_ascii=False),
                                        "new_fact": NEW_MARKER in json.dumps(r.result_json, ensure_ascii=False),
                                        "document_ids": sorted({str(item["document_id"]) for item in base.walk(r.result_json)
                                                                if item.get("document_id")})} for r in operations],
                "revision_jobs": [{"id": r.id, "status": r.status, "scope": r.scope,
                                   "source_version_id": r.source_version_id,
                                   "candidate_version_id": r.candidate_version_id,
                                   "published_version_id": r.published_version_id,
                                   "document_id": r.document_id, "expected_revision": r.expected_revision,
                                   "fence": r.fence, "attempt_count": r.attempt_count,
                                   "error_code": r.error_code, "request_hash": r.request_hash} for r in jobs],
                "memories": [{"id": r.id, "user_id": r.user_id, "agent_id": r.agent_id,
                             "content_hash": base.digest(r.content), "source_sequence": r.source_sequence,
                             "capture_job_id": r.capture_job_id,
                             "fact_flags": {k: r.content == v for k, v in FACTS.items()}} for r in memories],
                "capture_jobs": [{"id": r.id, "source_message_id": r.source_message_id,
                                 "assistant_message_id": r.source_assistant_message_id,
                                 "agent_id": r.agent_id, "source_sequence": r.source_sequence,
                                 "status": r.status, "applied_count": r.applied_count,
                                 "discarded_count": r.discarded_count, "terminal_reason": r.terminal_reason,
                                 "model_dispatch_count": r.model_dispatch_count,
                                 "user_generation": r.user_generation, "agent_generation": r.agent_generation}
                                for r in captures],
                "projections": [{"execution_id": r.id,
                                "initial": projection((r.context_json or {}).get("memory_projection")),
                                "active": projection((r.context_json or {}).get("memory_projection_active"))}
                               for r in instances if r.kind == "dynamic_task"],
                "sdk": base.load(control_directory(), "h2-sdk.safe.json", [])}


def prepare_h2(*, reuse: bool) -> None:
    """H2条件启动，原H1无新行为；真重启只装控制与只读旁证，不重种资源。"""
    from app.main import app

    if not reuse:
        seed_h2_resources()
    install_h2_controls()

    def facts() -> dict:
        """返回自有H2安全旁证，不暴露自由正文或执行数据库写。"""
        return h2_snapshot()

    app.add_api_route("/__e2e/f2/h2/facts", facts, methods=["GET"])
