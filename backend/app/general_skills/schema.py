"""
@Time       : 2026/07/29 16:20
@Author     : zhanglp8181
@File       : schema.py
@CallChain  : AgentLoop → GeneralSkillSelector → GeneralSkillSelection → 知识/通用技能执行
@Description: 定义通用技能导入、运行、能力选择及其降级状态的数据契约。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class GeneralSkillFile(BaseModel):
    path: str
    content: str
    size: Optional[int] = None
    mime_type: Optional[str] = None


class GeneralSkillImportRequest(BaseModel):
    """导入通用技能，并显式选择原子执行或规划指南语义。"""

    tenant_id: str
    agent_id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    markdown: Optional[str] = None
    files: list[GeneralSkillFile] = Field(default_factory=list)
    status: str = "published"
    usage_mode: Literal["atomic_execution", "planning_guidance"] = "atomic_execution"
    original_slug: Optional[str] = None


class GeneralSkillClawHubImportRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    source: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"


class GeneralSkillPackageUploadRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    filename: str
    content_base64: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"


class GeneralSkillRead(BaseModel):
    """返回通用技能当前内容和已发布规范快照元数据。"""

    id: str
    tenant_id: str
    slug: str
    name: str
    description: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files: list[GeneralSkillFile] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    permissions: dict[str, Any] = Field(default_factory=dict)
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    usage_mode: Literal["atomic_execution", "planning_guidance"] = "atomic_execution"
    capability_checksum: Optional[str] = None
    capability_published_at: Optional[str] = None
    owner_user_id: Optional[str] = None
    visibility_scope: str = "tenant_gallery"
    current_published_revision_id: Optional[str] = None
    row_version: int = 1
    binding_id: Optional[str] = None
    binding_status: Optional[str] = None
    binding_row_version: Optional[int] = None
    revision_policy: Optional[Literal["pinned", "follow_latest"]] = None
    pinned_revision_id: Optional[str] = None
    invocation_policy: Optional[Literal["model_allowed", "user_only"]] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class GeneralSkillRunRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    user_id: str = ""
    query: str
    session_id: Optional[str] = None
    model_config_id: Optional[str] = None
    max_attempts: int = Field(default=10, ge=1, le=10)


class GeneralSkillRunResponse(BaseModel):
    skill_slug: str
    execution_trace: list[dict[str, Any]] = Field(default_factory=list)
    generated_code: str = ""
    stdout: str = ""
    stderr: str = ""
    structured_result: dict[str, Any] = Field(default_factory=dict)
    reply: str


class GeneralSkillSelection(BaseModel):
    """表达通用技能与企业知识的独立选择，并区分自动预检索和明确禁用。"""

    use_general_skill: bool = False
    selected_slug: Optional[str] = None
    use_knowledge: bool = False
    knowledge_query: Optional[str] = None
    knowledge_mode: Literal["auto", "required", "disabled"] = "auto"
    confidence: float = 0.0
    reason: Optional[str] = None
    degraded: bool = False
    failure_code: Optional[str] = None


class GeneralSkillExecutionPlan(BaseModel):
    code: str
    runtime: str = "python"
    rationale: Optional[str] = None
    expected_output: Optional[str] = None


class GeneralSkillExecutionReview(BaseModel):
    result_sufficient: bool = False
    needs_retry: bool = False
    terminal: bool = False
    reason: str = ""
    repair_hint: Optional[str] = None


class GeneralSkillReply(BaseModel):
    reply: str
