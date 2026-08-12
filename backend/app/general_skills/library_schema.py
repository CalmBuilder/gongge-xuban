"""
@Time       : 2026/08/12 11:20
@Author     : zhanglp8181
@File       : library_schema.py
@CallChain  : 我的 Skill 库页面 → library API → batch binding service
@Description: 定义用户私有 Skill 库及多 Agent 原子装配 preview/commit 契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.general_skills.governance_schema import GeneralSkillBindingRead


class MyGeneralSkillRead(BaseModel):
    """返回本人 Skill 根、当前不可变修订和所有本人 Agent 绑定。"""

    id: str
    name: str
    slug: str
    description: str | None
    visibility_scope: str
    status: str
    current_revision_id: str
    current_revision_number: int
    content_checksum: str
    manifest_checksum: str
    row_version: int
    source_kind: str | None
    bindings: list[GeneralSkillBindingRead] = Field(default_factory=list)


class MyGeneralSkillAgentRead(BaseModel):
    """返回可作为本人 Skill 装配目标的最小 Agent 身份。"""

    id: str
    name: str
    status: str
    profile_revision: int


class GeneralSkillBindingBatchTarget(BaseModel):
    """表达批量装配中一个目标 Agent 的完整期望状态。"""

    agent_id: str = Field(min_length=1, max_length=128)
    status: Literal["active", "inactive"] = "active"
    revision_policy: Literal["pinned", "follow_latest"] = "pinned"
    pinned_revision_id: str | None = None
    invocation_policy: Literal["model_allowed", "user_only"] = "model_allowed"
    expected_binding_row_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_revision_target(self) -> "GeneralSkillBindingBatchTarget":
        """要求 pinned 指明修订，follow_latest 不携带隐藏目标。"""

        if (self.revision_policy == "pinned") != bool(self.pinned_revision_id):
            raise ValueError("GENERAL_SKILL_PINNED_REVISION_INVALID")
        return self


class GeneralSkillBindingBatchPreviewRequest(BaseModel):
    """请求对一个 Skill 的多个本人 Agent 做无写入预检。"""

    skill_id: str = Field(min_length=1, max_length=128)
    targets: list[GeneralSkillBindingBatchTarget] = Field(min_length=1, max_length=50)


class GeneralSkillBindingBatchTargetPreview(BaseModel):
    """返回单个目标的动作、当前绑定版本和可提交性。"""

    agent_id: str
    agent_name: str
    action: Literal["create", "update", "unchanged"]
    current_binding_id: str | None
    current_binding_row_version: int | None
    eligible: bool
    error_code: str | None = None


class GeneralSkillBindingBatchPreviewRead(BaseModel):
    """返回与当前数据库事实绑定的批量装配 checksum。"""

    skill_id: str
    revision_id: str
    preview_checksum: str
    targets: list[GeneralSkillBindingBatchTargetPreview]


class GeneralSkillBindingBatchCommitRequest(GeneralSkillBindingBatchPreviewRequest):
    """携带 preview checksum 提交同一批装配意图。"""

    preview_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")


class GeneralSkillBindingBatchCommitRead(BaseModel):
    """返回幂等命令与各 Agent 的最终绑定事实。"""

    command_id: str
    replayed: bool = False
    bindings: list[GeneralSkillBindingRead]
