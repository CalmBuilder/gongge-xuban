"""专家中文化预览包的不可变数据模型。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.experts.localization_translation import VerifiedChunkTranslation


class LocalizedExpert(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    source_batch_id: str
    source_commit: str
    source_content_sha256: str
    original_name: str
    original_description: str
    original_prompt: str
    localized_name: str
    localized_description: str
    localized_prompt: str
    category_zh: str
    chunks: list[VerifiedChunkTranslation]
    translation_sha256: str


class LocalizationManifestItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    filename: str
    sha256: str


class LocalizationError(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    message: str


class LocalizationManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_version: str = "1"
    generated_at: datetime
    tenant_id: str
    source_batch_id: str
    source_commit: str
    model_config_id: str
    model_name: str
    rules_version: str = "13"
    selected_count: int
    verified_count: int
    failed_count: int
    experts: list[LocalizationManifestItem] = Field(default_factory=list)
    manifest_sha256: str


class LocalizationApplyItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    agent_id: str | None = None
    status: Literal[
        "updated",
        "skipped_existing_translation",
        "skipped_modified",
        "failed_missing",
        "failed",
    ]
    message: str | None = None
    localized_content_sha256: str | None = None
    localized_updated_at: str | None = None


class LocalizationApplyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    source_batch_id: str
    translation_manifest_sha256: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[LocalizationApplyItem] = Field(default_factory=list)

    @property
    def updated_count(self) -> int:
        return sum(item.status == "updated" for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.status.startswith("skipped") for item in self.items)


class LocalizationRollbackItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    agent_id: str | None = None
    status: Literal["restored", "skipped_modified_or_used", "skipped_not_updated", "failed"]
    message: str | None = None


class LocalizationRollbackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    source_batch_id: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[LocalizationRollbackItem] = Field(default_factory=list)

    @property
    def restored_count(self) -> int:
        return sum(item.status == "restored" for item in self.items)
