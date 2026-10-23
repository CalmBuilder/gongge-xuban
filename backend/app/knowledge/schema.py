"""
@Time        : 2026/10/03
@Author      : zhanglp8181
@File        : schema.py
@CallChain   : Knowledge API/AgentLoop/DTA → 知识请求/证据结果/模型白名单投影
@Description : 定义知识治理和检索数据契约，并声明内置只读知识结果的模型输出形态。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseCreateRequest(BaseModel):
    tenant_id: str
    name: str
    description: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseOrgAccessInput(BaseModel):
    """声明一个知识访问组织根及后代包含规则。"""

    org_unit_id: str
    include_descendants: bool = True


class KnowledgeBaseOrgAccessRead(KnowledgeBaseOrgAccessInput):
    """返回已保存的知识访问组织根。"""

    id: str
    status: str


class KnowledgeBaseGovernanceUpdateRequest(BaseModel):
    """以乐观锁更新知识责任、访问范围与下载策略。"""

    tenant_id: str
    expected_revision: int = Field(ge=1)
    responsible_org_unit_id: Optional[str] = None
    access_scope: Literal["owner", "organization", "tenant"]
    download_policy: Literal["allowed", "restricted"]
    organization_access: list[KnowledgeBaseOrgAccessInput] = Field(default_factory=list)


class KnowledgeBaseUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBaseRollbackRequest(BaseModel):
    tenant_id: str
    agent_id: str
    version: str


class KnowledgeBaseRead(BaseModel):
    """返回知识库治理事实、当前版本统计和调用者正文访问解释。"""

    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    status: str
    owner_user_id: Optional[str] = None
    responsible_org_unit_id: Optional[str] = None
    access_scope: Literal["owner", "organization", "tenant"] = "owner"
    download_policy: Literal["allowed", "restricted"] = "restricted"
    revision: int = 1
    content_revision: int = 1
    organization_access: list[KnowledgeBaseOrgAccessRead] = Field(default_factory=list)
    content_access_allowed: bool = True
    content_access_reason: str = "allowed"
    version: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    branch_head_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0
    bucket_count: int = 0
    chunk_count: int = 0
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentUploadRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    title: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestJobRead(BaseModel):
    """返回入库或正文修订任务的受权状态，不暴露候选正文和内部候选 ID。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: Optional[str] = None
    filename: str
    job_kind: str = "legacy"
    scope: str = "overall"
    source_version_id: Optional[str] = None
    expected_revision: Optional[int] = None
    published_version_id: Optional[str] = None
    published_document_id: Optional[str] = None
    content_revision: Optional[int] = None
    manifest_hash: Optional[str] = None
    error_code: Optional[str] = None
    status: str
    stage: str
    progress: float
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    filename: str
    file_type: str
    title: Optional[str] = None
    status: str
    bucket_count: int
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentContentRead(BaseModel):
    """返回当前可编辑 head 的完整规范正文及其持久版本事实。"""

    document_id: str
    lineage_id: str
    knowledge_base_version_id: str
    scope: str
    head_version_id: str
    content_revision: int
    content_md: str
    source_checksum: Optional[str] = None
    source_kind: str


class KnowledgeDocumentUpdateRequest(BaseModel):
    """声明文档兼容字段更新或一次带 CAS 的完整正文修订意图。"""

    tenant_id: str
    title: Optional[str] = None
    status: Optional[Literal["ready", "processing", "failed", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None
    content_md: Optional[str] = None
    expected_version_id: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)
    client_request_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class KnowledgeRevisionAccepted(BaseModel):
    """表示正文已受理为持久构建任务，但尚未成为可见已发布版本。"""

    job_id: str
    status: str
    source_version_id: str
    expected_revision: int


class KnowledgeBucketRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_key: str
    title: str
    summary: str
    token_estimate: int
    chunk_count: int = 0
    status: str = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBucketUpdateRequest(BaseModel):
    tenant_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeChunkRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: str
    chunk_index: int
    content: str
    summary: Optional[str] = None
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeChunkUpdateRequest(BaseModel):
    tenant_id: str
    content: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeConceptRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    document_id: Optional[str] = None
    concept_id: str
    concept_type: str
    title: str
    description: Optional[str] = None
    content_md: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    links: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeConceptUpdateRequest(BaseModel):
    tenant_id: str
    content_md: str
    document_id: Optional[str] = None
    status: Literal["active", "archived"] = "active"


class KnowledgeOkfImportRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    agent_id: Optional[str] = None


class KnowledgeSearchRequest(BaseModel):
    """声明一次受 tenant、agent 可见范围和检索预算约束的知识查询。"""

    tenant_id: str
    agent_id: Optional[str] = None
    query: str
    query_type: Literal["answer", "policy_check", "tool_discovery", "skill_discovery"] = "answer"
    desired_evidence: Optional[str] = Field(default=None, max_length=1000)
    scope: dict[str, Any] = Field(default_factory=dict)
    model_config_id: Optional[str] = None
    mode: Literal["chat", "skill_discovery", "debug"] = "chat"
    knowledge_base_ids: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    max_bucket_rounds: int = 2
    max_buckets: int = 4
    max_chunks: int = 8
    budget_tokens: int = 4000
    max_depth: int = 2
    need_evidence_pack: bool = True


class KnowledgeEvidenceSufficiency(BaseModel):
    """描述检索证据是否覆盖冻结的期望证据要求。"""

    required: bool = False
    satisfied: bool = False
    evidence_count: int = 0
    aligned_evidence_count: int = 0
    required_aspects: list[str] = Field(default_factory=list)
    covered_aspects: list[str] = Field(default_factory=list)
    max_alignment_score: Optional[float] = None
    alignment_threshold: Optional[float] = None
    reason: Literal[
        "evidence_available",
        "desired_evidence_aligned",
        "desired_evidence_not_aligned",
        "no_evidence",
    ] = "no_evidence"


class KnowledgeSearchResponse(BaseModel):
    outcome: Literal["evidence_found", "no_match", "insufficient"] = "no_match"
    degraded: bool = False
    selected_buckets: list[KnowledgeBucketRead] = Field(default_factory=list)
    chunks: list[KnowledgeChunkRead] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    route_trace: list[dict[str, Any]] = Field(default_factory=list)
    selected_documents: list[dict[str, Any]] = Field(default_factory=list)
    selected_concepts: list[dict[str, Any]] = Field(default_factory=list)
    expanded_sections: list[dict[str, Any]] = Field(default_factory=list)
    okf_citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_pack: list[dict[str, Any]] = Field(default_factory=list)
    evidence_sufficiency: KnowledgeEvidenceSufficiency = Field(
        default_factory=KnowledgeEvidenceSufficiency
    )


def withhold_insufficient_knowledge_candidates(value: dict[str, Any]) -> dict[str, Any]:
    """答案模型收到的知识必须整体满足必需方面，未满足时不透传部分候选正文。"""

    outcome = value.get("outcome")
    sufficiency = value.get("evidence_sufficiency")
    should_withhold = outcome in ("no_match", "insufficient")
    if isinstance(sufficiency, dict):
        should_withhold = should_withhold or (
            sufficiency.get("required") is True and sufficiency.get("satisfied") is not True
        )
    if not should_withhold:
        return value

    projected = dict(value)
    for field_name in (
        "chunks",
        "evidence_pack",
        "okf_citations",
        "selected_buckets",
        "selected_concepts",
        "selected_documents",
    ):
        if field_name in projected:
            projected[field_name] = []
    return projected


def knowledge_result_output_schema() -> dict[str, Any]:
    """声明原检索结果的业务证据白名单，供DTA冻结与旧卡片只读投影共用。

    不开放trace、metadata、凭据和未知侧带，数量/深度/字符串仍由原通用投影预算约束。
    """
    def object_fields(strings: tuple[str, ...], **extra: Any) -> dict[str, Any]:
        """显式列出安全字段，始终禁止未知对象属性进入模型上下文。"""
        return {"type": "object", "additionalProperties": False,
                "properties": {**{key: {"type": "string"} for key in strings}, **extra}}

    def array(items: dict[str, Any]) -> dict[str, Any]:
        """集合沿原通用预算投影，每项继续使用显式字段白名单。"""
        return {"type": "array", "items": items}

    strings = {"type": "string"}
    identities = ("id", "tenant_id", "knowledge_base_id", "knowledge_base_version_id",
                  "document_id", "bucket_id")
    source = object_fields(("document_id", "chunk_id", "source_path", "section_path"))
    citation = object_fields(("target", "text", "label"))
    evidence = object_fields(("chunk_id", "document_id", "bucket_id", "source_path",
        "section_path", "summary", "content", "excerpt", "confidence_reason", "related_group_id"),
        related_chunk_ids=array(strings), related_chunk_index={"type": "integer"},
        relevance_score={"type": "number"}, evidence_alignment_score={"type": "number"})
    sufficiency = object_fields(("reason",), required={"type": "boolean"},
        satisfied={"type": "boolean"}, evidence_count={"type": "integer"},
        aligned_evidence_count={"type": "integer"}, required_aspects=array(strings),
        covered_aspects=array(strings), max_alignment_score={"type": "number"},
        alignment_threshold={"type": "number"})
    return object_fields(("outcome",), degraded={"type": "boolean"},
        knowledge_base_ids=array(strings), knowledge_base_version_ids=array(strings),
        okf_citations=array(object_fields(("concept_id", "title", "label", "target"))),
        evidence_sufficiency=sufficiency, evidence_pack=array(evidence),
        selected_documents=array(object_fields((*identities, "title", "filename", "summary"))),
        selected_buckets=array(object_fields((*identities, "bucket_key", "title", "summary"))),
        selected_concepts=array(object_fields((*identities, "concept_id", "type", "title",
            "description", "content", "content_excerpt"), source_refs=array(source),
            citations=array(citation), links=array(citation))),
        chunks=array(object_fields((*identities, "content", "summary", "source_ref"),
                                  chunk_index={"type": "integer"})))


class KnowledgeDiscoveryRead(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: Optional[str] = None
    suggestion_type: Literal["skill", "tool", "warning"]
    title: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    reason: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
