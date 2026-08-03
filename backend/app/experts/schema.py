"""专家导入预览包使用的不可变值对象。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.experts.parser import ParsedExpert


CapabilityType = Literal["P0", "P1", "P2", "P3"]
CapabilityReadiness = Literal["ready", "partial", "blocked"]


class CapabilityAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    required_capabilities: list[str] = Field(default_factory=list)
    orchestration_required: bool = False
    core_execution_requires_external_capability: bool = False
    evidence: list[str] = Field(default_factory=list)


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    capability_type: CapabilityType
    readiness: CapabilityReadiness
    required_capabilities: list[str]
    resolved_capabilities: list[str]
    unresolved_requirements: list[str]
    orchestration_required: bool
    core_execution_requires_external_capability: bool
    evidence: list[str]


class ExpertTranslation(BaseModel):
    model_config = ConfigDict(frozen=True)

    name_zh: str
    description_zh: str
    category_zh: str
    tags_zh: list[str]
    markdown_zh: str
    high_risk: bool
    capability_analysis: CapabilityAnalysis


class PreparedExpert(BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: ParsedExpert
    translation: ExpertTranslation
    capability_manifest: CapabilityManifest
    prompt_estimated_tokens: int
    upstream_url: str
    content_sha256: str


class PrepareError(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    stage: Literal["source", "parse", "translation", "package"]
    message: str


class ManifestExpert(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    filename: str
    sha256: str


class ImportManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_version: Literal["2"] = "2"
    batch_id: str
    generated_at: datetime
    tenant_id: str
    source_absolute_path: str
    source_remote_url: str
    source_commit: str
    source_verified: bool
    source_license: Literal["MIT"] = "MIT"
    candidate_count: int
    success_count: int
    failed_count: int
    experts: list[ManifestExpert]
    manifest_sha256: str


class ApplyItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    status: Literal["created", "skipped_existing", "failed_name_conflict", "failed"]
    name: str | None = None
    agent_id: str | None = None
    content_sha256: str
    imported_updated_at: str | None = None
    message: str | None = None


class ApplyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    tenant_id: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[ApplyItem]

    @property
    def created_count(self) -> int:
        return sum(item.status == "created" for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped_existing" for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.status.startswith("failed") for item in self.items)


class RollbackItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    upstream_path: str
    agent_id: str | None = None
    status: Literal["deleted", "skipped_modified_or_used", "skipped_not_created", "failed"]
    message: str | None = None


class RollbackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    tenant_id: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[RollbackItem]

    @property
    def deleted_count(self) -> int:
        return sum(item.status == "deleted" for item in self.items)
