"""
@Time       : 2026/07/27
@Author     : zhanglp8181
@File       : schema.py
@CallChain  : 数字员工管理 API → 请求/响应模型 → AgentProfile 持久化
@Description: 定义数字员工资料、资源、模型及广场发布治理命令的数据契约。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AgentResourceType = Literal["skill", "general_skill", "knowledge_base", "tool"]
AgentWorkRecordEventKind = Literal["chat", "task", "sop", "tool", "knowledge", "skill"]
AgentWorkRecordEventPhase = Literal["reply", "last_run", "next_run", "assigned"]
AgentListScope = Literal["manageable", "owned", "used", "gallery", "expert"]
AgentGalleryScope = Literal["owned", "used", "gallery", "expert"]
AgentManagementView = Literal["all", "online", "offline", "pending", "expert", "governance"]


class AgentProfileCreateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = False
    source_mode: Literal["copy", "blank"] = "copy"
    copy_from_agent_id: Optional[str] = None
    agent_category_code: str = "assistant"
    visibility_scope: Literal["private", "tenant"] = "private"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class AgentGalleryPublicationRequest(BaseModel):
    tenant_id: str
    published: bool


class AgentResponsibilityUpdateRequest(BaseModel):
    """声明数字员工的治理责任组织，不改变使用、执行或数据访问权限。"""

    tenant_id: str
    responsible_org_unit_id: Optional[str] = None


class AgentResourceBindingRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    resource_type: AgentResourceType
    resource_id: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentProfileRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool
    status: str
    owner_user_id: Optional[str] = None
    responsible_org_unit_id: Optional[str] = None
    responsible_org_unit_name: Optional[str] = None
    source_agent_id: Optional[str] = None
    source_agent_version: Optional[str] = None
    profile_revision: int = 1
    published_to_gallery: bool = False
    gallery_published_at: Optional[str] = None
    gallery_published_by: Optional[str] = None
    agent_category_code: str = "assistant"
    visibility_scope: Literal["private", "tenant"] = "private"
    owned_by_current_user: bool = False
    used_by_current_user: bool = False
    manageable_by_current_user: bool = False
    view_level: Literal["manager", "user", "governance"] = "user"
    copy_summary: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[AgentResourceBindingRead] = Field(default_factory=list)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentScopeRead(BaseModel):
    tenant_id: str
    agents: list[AgentProfileRead] = Field(default_factory=list)


class AgentGalleryFacetRead(BaseModel):
    """专家目录筛选项及其在当前上级范围内的员工数量。"""

    value: str
    label: str
    count: int


class AgentGalleryFacetsRead(BaseModel):
    """专家目录的来源、专业部门和专业方向级联筛选项。"""

    sources: list[AgentGalleryFacetRead] = Field(default_factory=list)
    departments: list[AgentGalleryFacetRead] = Field(default_factory=list)
    directions: list[AgentGalleryFacetRead] = Field(default_factory=list)


class AgentGalleryPageRead(BaseModel):
    """数字员工广场关系视图分页响应。"""

    items: list[AgentProfileRead]
    total: int
    scope_counts: dict[str, int] = Field(default_factory=dict)
    facets: AgentGalleryFacetsRead = Field(default_factory=AgentGalleryFacetsRead)
    page: int
    page_size: int


class AgentManagementPageRead(BaseModel):
    """管理端数字员工状态/类型视图分页响应。"""

    items: list[AgentProfileRead]
    total: int
    view_counts: dict[str, int] = Field(default_factory=dict)
    facets: AgentGalleryFacetsRead = Field(default_factory=AgentGalleryFacetsRead)
    page: int
    page_size: int


class AgentWorkRecordReplyStatsRead(BaseModel):
    total: int = 0
    today: int = 0
    by_day: dict[str, int] = Field(default_factory=dict)


class AgentWorkRecordEventRead(BaseModel):
    id: str
    kind: AgentWorkRecordEventKind
    phase: AgentWorkRecordEventPhase
    timestamp: str
    label: str = ""


class AgentWorkRecordRead(BaseModel):
    agent_id: str
    timezone: str
    generated_at: str
    reply_stats: AgentWorkRecordReplyStatsRead
    events: list[AgentWorkRecordEventRead] = Field(default_factory=list)


class AgentResourceBindingInput(BaseModel):
    resource_type: AgentResourceType
    resource_id: str
    status: Literal["active", "inactive"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResourcesUpdateRequest(BaseModel):
    tenant_id: str
    resources: list[AgentResourceBindingInput] = Field(default_factory=list)


class AgentResourceImportRequest(BaseModel):
    tenant_id: str
    source_agent_id: str
    resource_type: AgentResourceType
    resource_ids: list[str] = Field(default_factory=list)


class AgentModelBindingInput(BaseModel):
    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class AgentModelsUpdateRequest(BaseModel):
    tenant_id: str
    bindings: list[AgentModelBindingInput] = Field(default_factory=list)


class AgentSkillRollbackRequest(BaseModel):
    tenant_id: str
    version: str
