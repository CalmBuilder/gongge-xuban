"""
@Time       : 2026/08/12 00:42
@Author     : zhanglp8181
@File       : import_schema.py
@CallChain  : Skill 导入页面/API → ImportJobService → preview/confirm response
@Description: 定义 S1 暂存导入、候选预览和原子确认的版本化 HTTP 契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GeneralSkillImportJobCreate(BaseModel):
    """创建上传、固定 GitHub revision 或公开 HTTPS ZIP 导入作业。"""

    tenant_id: str = Field(min_length=1, max_length=512)
    target_agent_id: str = Field(min_length=1, max_length=512)
    source_kind: Literal["upload", "github", "https"] = "upload"
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    content_base64: str | None = Field(default=None, min_length=1)
    source_url: str | None = Field(default=None, min_length=1, max_length=2048)
    revision: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    source_subpath: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_source_fields(self) -> "GeneralSkillImportJobCreate":
        """要求上传正文与远程 URL 互斥，GitHub 额外固定完整 commit。"""

        if self.source_kind == "upload":
            if (
                not self.filename
                or not self.content_base64
                or self.source_url
                or self.revision
                or self.source_subpath
            ):
                raise ValueError("GENERAL_SKILL_UPLOAD_SOURCE_INVALID")
            return self
        if self.filename or self.content_base64 or not self.source_url:
            raise ValueError("GENERAL_SKILL_REMOTE_SOURCE_INVALID")
        if self.source_kind == "github" and (not self.revision or not self.source_subpath):
            raise ValueError("GENERAL_SKILL_GITHUB_REVISION_AND_SUBPATH_REQUIRED")
        if self.source_kind == "https" and (self.revision or self.source_subpath):
            raise ValueError("GENERAL_SKILL_HTTPS_REVISION_NOT_ALLOWED")
        return self


class GeneralSkillImportCandidateRead(BaseModel):
    """返回用户确认所需但不包含附件正文的候选摘要。"""

    candidate_id: str
    manifest_path: str
    name: str
    description: str
    content_checksum: str
    manifest_checksum: str
    allowed_tools: list[str] = Field(default_factory=list)
    invocation_policy: Literal["model_allowed", "user_only"] = "model_allowed"
    argument_hint: str | None = None
    dependency_candidates: list[dict[str, object]] = Field(default_factory=list)
    platform_commands: list[str] = Field(default_factory=list)
    resources: list[dict[str, object]] = Field(default_factory=list)


class GeneralSkillImportJobRead(BaseModel):
    """返回本人可见的脱敏作业状态与 checksum 绑定预览。"""

    id: str
    tenant_id: str
    target_agent_id: str
    source_kind: str
    source_reference_redacted: str | None
    status: str
    attempt: int
    raw_checksum: str | None
    normalized_checksum: str | None
    preview_checksum: str | None
    quota_bytes: int
    error_code: str | None
    error_detail_redacted: str | None
    candidates: list[GeneralSkillImportCandidateRead] = Field(default_factory=list)
    expires_at: str
    row_version: int
    installed_revision_ids: list[str] = Field(default_factory=list)


class GeneralSkillDependencyDecision(BaseModel):
    """记录用户对正文引用候选的逐边裁决，忽略边不会进入运行依赖图。"""

    dependency_candidate_id: str = Field(pattern=r"^gsdepcand_[a-f0-9]{24}$")
    dependency_kind: Literal["required", "optional", "ignored"]


class GeneralSkillImportConfirm(BaseModel):
    """以审核过的 preview checksum 原子确认一个或多个候选及逐边依赖裁决。"""

    preview_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_ids: list[str] = Field(min_length=1, max_length=50)
    dependency_decisions: list[GeneralSkillDependencyDecision] = Field(default_factory=list)
    expected_row_version: int = Field(ge=1)


class GeneralSkillImportCancel(BaseModel):
    """以乐观锁版本幂等取消尚未终止的导入作业。"""

    expected_row_version: int = Field(ge=1)
