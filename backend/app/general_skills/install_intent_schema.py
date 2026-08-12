"""
@Time       : 2026/08/12 11:55
@Author     : zhanglp8181
@File       : install_intent_schema.py
@CallChain  : Chat composer/install card → install intent API → ImportJob service
@Description: 定义对话显式安装的固定来源、持久卡和乐观锁办理契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.general_skills.import_schema import GeneralSkillImportCandidateRead


class GeneralSkillInstallIntentCreate(BaseModel):
    """创建只接受固定 GitHub commit 与目录的对话安装卡。"""

    agent_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["github"] = "github"
    source_url: str = Field(min_length=1, max_length=2048)
    revision: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    source_subpath: str = Field(min_length=1, max_length=512)


class GeneralSkillInstallIntentResolve(BaseModel):
    """确认或取消一张持久安装卡。"""

    command: Literal["confirm", "cancel"]
    expected_row_version: int = Field(ge=1)
    command_id: str = Field(min_length=1, max_length=128)


class GeneralSkillInstallIntentRead(BaseModel):
    """返回安全摘要、来源和可恢复状态，不返回 Skill 全文。"""

    id: str
    session_id: str
    agent_id: str
    source_kind: str
    source_reference_redacted: str | None
    source_revision: str | None
    status: str
    import_job_id: str
    raw_checksum: str | None
    normalized_checksum: str | None
    preview_checksum: str | None
    candidates: list[GeneralSkillImportCandidateRead]
    installed_revision_ids: list[str]
    error_code: str | None
    row_version: int
    created_at: str
    updated_at: str
