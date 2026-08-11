"""
@Time       : 2026/08/12 23:59
@Author     : zhanglp8181
@File       : governance_schema.py
@CallChain  : Skill 治理页面/API → GeneralSkillGovernanceService → Revision/Binding
@Description: 定义 S2 绑定策略、停用、回滚和撤销的显式乐观锁 HTTP 契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GeneralSkillBindingUpdate(BaseModel):
    """表达绑定版本策略或启停状态的一次 CAS 更新。"""

    agent_id: str = Field(min_length=1, max_length=128)
    revision_policy: Literal["pinned", "follow_latest"]
    pinned_revision_id: str | None = None
    invocation_policy: Literal["model_allowed", "user_only"]
    status: Literal["active", "inactive"]
    expected_row_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_pinned_revision(self) -> "GeneralSkillBindingUpdate":
        """要求 pinned 明确目标，follow_latest 不接受潜伏 revision。"""

        if (self.revision_policy == "pinned") != bool(self.pinned_revision_id):
            raise ValueError("GENERAL_SKILL_PINNED_REVISION_INVALID")
        return self


class GeneralSkillBindingRead(BaseModel):
    """返回管理 UI 所需的绑定状态、策略与乐观锁版本。"""

    id: str
    agent_id: str
    skill_id: str
    status: str
    revision_policy: str
    pinned_revision_id: str | None
    invocation_policy: str
    row_version: int


class GeneralSkillRevisionRead(BaseModel):
    """返回不含 Skill 正文的修订治理摘要。"""

    id: str
    skill_id: str
    revision_number: int
    content_checksum: str
    manifest_checksum: str
    status: str
    row_version: int
    created_at: str
    published_at: str | None
    revoked_at: str | None


class GeneralSkillRollbackRequest(BaseModel):
    """表达回滚目标以及 Skill、目标修订两级 CAS 前置条件。"""

    target_revision_id: str = Field(min_length=1, max_length=128)
    expected_skill_row_version: int = Field(ge=1)
    expected_target_row_version: int = Field(ge=1)


class GeneralSkillRevokeRequest(BaseModel):
    """表达软撤销修订所需的 Skill 与修订 CAS 前置条件。"""

    expected_skill_row_version: int = Field(ge=1)
    expected_revision_row_version: int = Field(ge=1)
