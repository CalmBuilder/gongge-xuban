"""
@Time       : 2026/08/13 21:00
@Author     : zhanglp8181
@File       : publication_schema.py
@CallChain  : Skill/Agent 发布与组织广场 API → PublicationService
@Description: 定义类型化发布申请、Release 列表、Skill/Agent 主动采用及审核命令契约。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PublicationSubmitRequest(BaseModel):
    """提交本人资源当前不可变快照进入组织审核。"""

    resource_type: str = Field(pattern=r"^(general_skill|agent)$")
    resource_id: str = Field(min_length=1, max_length=128)
    expected_resource_revision: int = Field(ge=1)


class PublicationReviewRequest(BaseModel):
    """管理员以 CAS 审核冻结申请。"""

    command_id: str = Field(min_length=1, max_length=128)
    command: str = Field(pattern=r"^(approve|reject)$")
    expected_request_row_version: int = Field(ge=1)
    expected_attention_revision: int = Field(ge=0)
    comment: str | None = Field(default=None, max_length=2000)


class PublicationRequestRead(BaseModel):
    """返回发布申请及其审核身份，不返回私人正文或凭据。"""

    id: str
    resource_type: str
    resource_id: str
    snapshot_id: str
    snapshot_checksum: str
    attention_id: str | None
    status: str
    row_version: int


class PublicationReleaseRead(BaseModel):
    """返回组织广场可采用的固定发布物。"""

    id: str
    resource_type: str
    resource_id: str
    snapshot_id: str
    snapshot_checksum: str
    name: str
    description: str
    approved_revision_id: str | None = None
    components: list[dict[str, object]] = Field(default_factory=list)
    status: str = "active"
    row_version: int = 1


class PublicationAdoptRequest(BaseModel):
    """用户把 Release 主动采用到本人目标 Agent 或克隆为本人 Agent。"""

    target_agent_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class PublicationAdoptRead(BaseModel):
    """返回固定采用结果。"""

    release_id: str
    resource_type: str
    target_agent_id: str
    binding_id: str | None = None
    adopted_agent_id: str | None = None
    status: str = "adopted"


class PublicationReleaseTransitionRequest(BaseModel):
    """管理员以 CAS 普通下架或安全撤销 active Release。"""

    command_id: str = Field(min_length=1, max_length=128)
    command: str = Field(pattern=r"^(unpublish|security_revoke)$")
    expected_row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
