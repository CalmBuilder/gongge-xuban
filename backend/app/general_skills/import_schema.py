"""
@Time       : 2026/08/12 00:42
@Author     : zhanglp8181
@File       : import_schema.py
@CallChain  : Skill 导入页面/API → ImportJobService → preview/confirm response
@Description: 定义 S1 暂存导入、候选预览和原子确认的版本化 HTTP 契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GeneralSkillImportJobCreate(BaseModel):
    """创建一次上传来源导入作业，正文中的用户身份不参与授权。"""

    tenant_id: str = Field(min_length=1, max_length=512)
    target_agent_id: str = Field(min_length=1, max_length=512)
    source_kind: Literal["upload"] = "upload"
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class GeneralSkillImportCandidateRead(BaseModel):
    """返回用户确认所需但不包含附件正文的候选摘要。"""

    candidate_id: str
    manifest_path: str
    name: str
    description: str
    content_checksum: str
    manifest_checksum: str
    allowed_tools: list[str] = Field(default_factory=list)
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


class GeneralSkillImportConfirm(BaseModel):
    """以审核过的 preview checksum 原子确认一个或多个候选。"""

    preview_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_ids: list[str] = Field(min_length=1, max_length=50)
    expected_row_version: int = Field(ge=1)


class GeneralSkillImportCancel(BaseModel):
    """以乐观锁版本幂等取消尚未终止的导入作业。"""

    expected_row_version: int = Field(ge=1)
