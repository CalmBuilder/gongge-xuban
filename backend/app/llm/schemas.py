"""
@Time       : 2026/08/04 00:35
@Author     : zhanglp8181
@File       : schemas.py
@CallChain  : ModelConfig API → LLM schema → provider preflight/management UI
@Description: 定义模型配置、脱敏读取与动态任务协议预检契约。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigCreateRequest(BaseModel):
    """创建租户模型配置，凭据仅用于服务端加密存储。"""

    tenant_id: str
    name: str
    provider: str = "openai_compatible"
    base_url: Optional[str] = None
    api_key: str = Field(default="", repr=False)
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    enabled: bool = True


class ModelConfigUpdateRequest(BaseModel):
    """局部更新模型配置，连接/协议变更由 API 撤销旧预检。"""

    tenant_id: str
    name: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    extra_body: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None


class ModelConfigRead(BaseModel):
    """返回脱敏模型配置与已验证能力事实。"""

    id: str
    tenant_id: str
    name: str
    provider: str
    base_url: Optional[str]
    api_key_masked: str
    model: str
    temperature: float
    max_output_tokens: int
    extra_body: dict[str, Any]
    capability_snapshot: dict[str, Any] = Field(default_factory=dict)
    capability_checksum: Optional[str] = None
    preflight_status: str = "unverified"
    preflight_error: Optional[str] = None
    capability_verified_at: Optional[str] = None
    is_default: bool
    enabled: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ModelConnectionCheck(BaseModel):
    """描述连接测试的单个机械阶段，便于页面区分认证、模型与计费故障。"""

    name: str
    status: Literal["passed", "failed", "skipped"]
    message: str


class ModelConfigTestResponse(BaseModel):
    """表达分阶段连接测试结果，不等价于动态能力预检。"""

    success: bool
    message: str
    output: Optional[str] = None
    error_code: Optional[str] = None
    http_status: Optional[int] = None
    provider_code: Optional[str] = None
    request_id: Optional[str] = None
    endpoint: Optional[str] = None
    model: Optional[str] = None
    suggestion: Optional[str] = None
    checks: list[ModelConnectionCheck] = Field(default_factory=list)


class ModelCapabilityPreflightResponse(BaseModel):
    """返回动态任务的 provider/model 协议预检状态和脱敏能力快照。"""

    success: bool
    status: Literal["ready", "failed"]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    checksum: Optional[str] = None
    message: str
