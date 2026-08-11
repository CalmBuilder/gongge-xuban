"""
@Time       : 2026/08/13 02:05
@Author     : zhanglp8181
@File       : runtime_schema.py
@CallChain  : Chat Skill API → GeneralSkillRuntimeService → session catalog/override/use
@Description: 定义会话 Skill 目录、mute、加载和资源分页的外部契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SessionGeneralSkillItemRead(BaseModel):
    """返回无正文的会话 Skill 菜单项及固定 revision。"""

    skill_id: str
    revision_id: str
    revision_number: int
    name: str
    description: str
    invocation_policy: str
    revision_policy: str
    enabled: bool = True
    override_row_version: int | None = None


class SessionGeneralSkillCatalogRead(BaseModel):
    """返回当前会话经用户/Agent/override 求交后的目录。"""

    session_id: str
    agent_id: str
    items: list[SessionGeneralSkillItemRead]


class SessionGeneralSkillOverrideWrite(BaseModel):
    """表达 mute 或恢复继承的一次 CAS 更新。"""

    agent_id: str = Field(min_length=1, max_length=128)
    enabled: bool
    expected_row_version: int | None = Field(default=None, ge=0)


class SessionGeneralSkillOverrideRead(BaseModel):
    """返回会话收窄决定及行版本。"""

    skill_id: str
    enabled: bool
    row_version: int


class GeneralSkillLoadRequest(BaseModel):
    """表达服务端确认过的单轮加载意图，正文中不存在隐式强制字段。"""

    agent_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    skill_id: str = Field(min_length=1, max_length=128)
    selection_mode: Literal["auto", "forced", "dependency"]
    parent_skill_use_id: str | None = Field(default=None, max_length=128)


class GeneralSkillLoadRead(BaseModel):
    """返回加载账本身份与固定 revision，不向普通 API 回传正文。"""

    use_id: str
    skill_id: str
    revision_id: str
    revision_number: int
    name: str
    selection_mode: str


class GeneralSkillResourceRead(BaseModel):
    """返回经过 manifest 授权的单页 UTF-8 文本资源。"""

    use_id: str
    resource_checksum: str
    offset: int
    content: str
    has_more: bool
