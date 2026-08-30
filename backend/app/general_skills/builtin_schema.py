"""
@Time       : 2026/08/29 16:20
@Author     : zhanglp8181
@File       : builtin_schema.py
@CallChain  : Skill 内置目录 API → Pydantic 请求/响应 → 快照导入与审核页面
@Description: 定义项目内置 Skill 候选目录、详情、筛选和快照导入的 HTTP 契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


CatalogStatus = Literal["draft", "published", "rejected", "archived"]
CatalogStability = Literal["stable", "beta", "misc"]
CatalogRiskLevel = Literal["low", "medium", "high"]
CatalogInvocationPolicy = Literal["model_allowed", "user_only"]
CatalogLifecycleAction = Literal["archive", "revoke"]


class BuiltinSkillCatalogItemRead(BaseModel):
    """返回目录卡片需要的候选来源、风险、版本和审核状态摘要。"""

    id: str
    slug: str
    name: str
    description: str
    category: str
    stability: CatalogStability
    risk_level: CatalogRiskLevel
    risk_findings: list[str] = Field(default_factory=list)
    invocation_policy: CatalogInvocationPolicy
    runtime_mode: Literal["guidance_only", "sandboxed"]
    source_kind: str
    review_status: str
    status: CatalogStatus
    source_repository: str
    source_revision: str
    source_path: str
    source_license: str
    source_package_checksum: str
    source_normalized_checksum: str
    content_checksum: str
    manifest_checksum: str
    revision_id: str | None
    revision_number: int | None
    revision_status: str | None
    resource_count: int
    row_version: int
    revision_row_version: int | None
    updated_at: str
    name_zh: str | None = None
    description_zh: str | None = None
    localization_status: str | None = None
    localization_source_content_checksum: str | None = None
    localization_checksum: str | None = None


class BuiltinSkillResourceRead(BaseModel):
    """返回详情页校验文件所需的相对路径和内容摘要，不返回二进制正文。"""

    relative_path: str
    content_checksum: str
    size: int
    media_type: str
    is_text: bool


class BuiltinSkillCatalogBindingSummaryRead(BaseModel):
    """返回当前租户已采用项目 Skill 的目标和状态摘要，不泄漏其他租户事实。"""

    binding_id: str
    agent_id: str
    agent_name: str
    governance_form: str
    status: str
    revision_policy: str
    pinned_revision_id: str | None = None
    invocation_policy: CatalogInvocationPolicy
    row_version: int


class BuiltinSkillCatalogDetailRead(BuiltinSkillCatalogItemRead):
    """返回管理员详情页审核 Skill 正文、解析元数据和资源清单。"""

    skill_markdown: str
    explanation_markdown_zh: str | None = None
    parsed_metadata: dict[str, object] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    argument_hint: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    resources: list[BuiltinSkillResourceRead] = Field(default_factory=list)
    bindings: list[BuiltinSkillCatalogBindingSummaryRead] = Field(default_factory=list)


class BuiltinSkillCatalogFacetsRead(BaseModel):
    """返回当前权限范围内可用于筛选的稳定聚合计数。"""

    category: dict[str, int] = Field(default_factory=dict)
    source_kind: dict[str, int] = Field(default_factory=dict)
    stability: dict[str, int] = Field(default_factory=dict)
    risk_level: dict[str, int] = Field(default_factory=dict)
    invocation_policy: dict[str, int] = Field(default_factory=dict)
    status: dict[str, int] = Field(default_factory=dict)


class BuiltinSkillCatalogPageRead(BaseModel):
    """返回候选目录分页、总数和筛选 facets。"""

    items: list[BuiltinSkillCatalogItemRead]
    total: int
    page: int
    page_size: int
    facets: BuiltinSkillCatalogFacetsRead


class BuiltinSkillCatalogImportRequest(BaseModel):
    """表达管理员重放固定内置快照导入命令的租户和幂等号。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)


class ExternalSkillCatalogImportRequest(BaseModel):
    """表达管理员将固定外部来源导入项目候选库的请求。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["github", "https", "skillhub"]
    source_url: str = Field(min_length=1, max_length=2048)
    source_license: str = Field(min_length=1, max_length=64)
    revision: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    source_subpath: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_source_contract(self) -> "ExternalSkillCatalogImportRequest":
        """要求 GitHub 固定完整提交和子路径，其他远程来源不携带漂移字段。"""

        if self.source_kind == "github" and (not self.revision or not self.source_subpath):
            raise ValueError("GENERAL_SKILL_CATALOG_GITHUB_REVISION_AND_SUBPATH_REQUIRED")
        if self.source_kind in {"https", "skillhub"} and (self.revision or self.source_subpath):
            raise ValueError("GENERAL_SKILL_CATALOG_SOURCE_REVISION_NOT_ALLOWED")
        if not self.source_license.strip():
            raise ValueError("GENERAL_SKILL_CATALOG_LICENSE_REQUIRED")
        return self


class BuiltinSkillCatalogImportRead(BaseModel):
    """返回固定快照导入的可重放结果和来源摘要。"""

    command_id: str
    replayed: bool
    created_count: int
    existing_count: int
    skill_count: int
    source_repository: str
    source_revision: str
    source_license: str
    source_package_checksum: str
    source_normalized_checksum: str
    items: list[dict[str, object]] = Field(default_factory=list)


class ExternalSkillCatalogImportRead(BuiltinSkillCatalogImportRead):
    """返回外部来源候选导入的来源类型和可重放结果。"""

    source_kind: str
    source_url: str


class BuiltinSkillCatalogReviewItem(BaseModel):
    """表达一条候选 Skill 的审核决定和两级 CAS 版本。"""

    skill_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "reject"]
    expected_skill_row_version: int = Field(ge=1)
    expected_revision_row_version: int = Field(ge=1)
    review_note: str | None = Field(default=None, max_length=2000)


class BuiltinSkillCatalogReviewRequest(BaseModel):
    """表达管理员一次原子批量审核的租户、幂等号和逐项决定。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    items: list[BuiltinSkillCatalogReviewItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_skills(self) -> "BuiltinSkillCatalogReviewRequest":
        """拒绝同一批次重复审核同一个 Skill，避免顺序依赖和歧义回执。"""

        if len({item.skill_id for item in self.items}) != len(self.items):
            raise ValueError("GENERAL_SKILL_CATALOG_REVIEW_DUPLICATE_SKILL")
        return self


class BuiltinSkillCatalogReviewRead(BaseModel):
    """返回批量审核的幂等回执、逐项前后状态和统计摘要。"""

    command_id: str
    replayed: bool
    approved_count: int
    rejected_count: int
    items: list[dict[str, object]] = Field(default_factory=list)


class BuiltinSkillCatalogBindingRequest(BaseModel):
    """表达把已发布项目 Skill 安装到能力分身或绑定到组织数字员工。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    skill_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    mode: Literal["install", "bind"]
    revision_policy: Literal["pinned", "follow_latest"] = "pinned"
    pinned_revision_id: str | None = None
    invocation_policy: CatalogInvocationPolicy = "model_allowed"

    @model_validator(mode="after")
    def validate_pinned_revision(self) -> "BuiltinSkillCatalogBindingRequest":
        """要求 pinned 明确修订，follow_latest 不接受隐藏的客户端目标。"""

        if (self.revision_policy == "pinned") != bool(self.pinned_revision_id):
            raise ValueError("GENERAL_SKILL_CATALOG_PINNED_REVISION_INVALID")
        return self


class BuiltinSkillCatalogBindingRead(BaseModel):
    """返回项目 Skill 目标绑定的动作、治理模式和版本事实。"""

    action: Literal["created", "updated", "unchanged"]
    mode: Literal["install", "bind"]
    binding_id: str
    agent_id: str
    skill_id: str
    status: str
    revision_policy: str
    pinned_revision_id: str | None
    invocation_policy: str
    row_version: int


class BuiltinSkillCatalogLifecycleRequest(BaseModel):
    """表达管理员对已发布平台 Skill 执行下架或安全撤销的 CAS 请求。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    skill_id: str = Field(min_length=1, max_length=128)
    action: CatalogLifecycleAction
    expected_skill_row_version: int = Field(ge=1)
    expected_revision_row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class BuiltinSkillCatalogLifecycleRead(BaseModel):
    """返回平台 Skill 生命周期操作的幂等回执、版本和受影响绑定摘要。"""

    command_id: str
    replayed: bool
    action: CatalogLifecycleAction
    skill_id: str
    slug: str
    skill_status: CatalogStatus
    revision_id: str
    revision_status: str
    skill_row_version: int
    revision_row_version: int
    deactivated_binding_count: int
