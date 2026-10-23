"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : candidate_schema.py
@CallChain  : 当前Skill编辑页 → 隔离候选API → 材料/基础校验及原发布事务
@Description: 单SkillCard候选、人工确认和source-free普通编辑重载的显式契约。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.feedback.materials import FeedbackMaterialRead
from app.skills.skill_schema import SkillCard, SkillRead


class CandidateSourceRef(BaseModel):
    """服务器冻结有效材料修订和目标授权代数，客户端不能替换来源。"""

    id: str
    feedback_revision: int
    checksum: str
    target_agent_id: str
    target_profile_revision: int


class CandidateCreateRequest(BaseModel):
    """选择有效材料和实际已有SOP；当前基础正文与版本由服务器读取。"""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    agent_id: str | None = None
    material_ids: list[str] = Field(min_length=1, max_length=8)
    request_key: str = Field(min_length=1, max_length=128)


class CandidateActionRequest(BaseModel):
    """绑定当前候选修订，不接受仅凭已批准布尔值覆盖新编辑。"""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    expected_revision: int = Field(ge=1)


class CandidateSaveRequest(CandidateActionRequest):
    """人工编辑只保存单SkillCard隔离候选，可明确确认原来源与当前基础差异。"""

    content: SkillCard
    confirm_base_difference: bool = False


class CandidatePublishRequest(CandidateActionRequest):
    """人工发布确认绑定实际基础身份和修订，同参重放只返回原发布回执。"""

    expected_base_version_id: str
    expected_base_checksum: str = Field(min_length=64, max_length=64)
    request_key: str = Field(min_length=1, max_length=128)
    confirm: Literal[True]
    confirm_base_difference: bool = False


class CandidatePublishReceipt(BaseModel):
    """合法历史的最小回执，不继续携带分享材料或候选正文。"""

    candidate_id: str
    skill_id: str
    scope_agent_id: str
    skill_version_id: str
    version: str
    definition_checksum: str


class SkillCandidateRead(BaseModel):
    """原执行来源与当前真实基础分列，关闭或发布历史不返回旧材料正文。"""

    id: str
    tenant_id: str
    skill_id: str
    scope_agent_id: str
    revision: int
    status: Literal["draft", "ready", "rejected", "closed", "published"]
    base_skill_version_id: str
    base_checksum: str
    base_version: str | None = None
    base_difference: bool = False
    base_difference_confirmed: bool = False
    base_content: SkillCard | None = None
    content: SkillCard | None = None
    materials: list[FeedbackMaterialRead] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    published: CandidatePublishReceipt | None = None


class OrdinaryEditContext(BaseModel):
    """明确重载原版后签发的普通编辑身份，不能授权旧候选继续改写或发布。"""

    identity: str
    scope_agent_id: str
    actor_auth_revision: int
    scope_profile_revision: int | None = None
    scope_profile_id: str | None = None
    request_key: str
    request_hash: str


class CandidateResetRequest(CandidateActionRequest):
    """明确丢弃受管编辑并加载所选范围原版，允许退出已撤回来源。"""

    agent_id: str | None = None
    skill_id: str | None = None
    request_key: str = Field(min_length=1, max_length=128)
    discard_candidate: Literal[True]


class CandidateResetRead(BaseModel):
    """新的普通身份绑定实际范围和当前原版，旧nonce/旧来源不能被复活。"""

    candidate_id: str
    revision: int
    editor_context: str
    current_skill: SkillRead | None = None


class SkillEditorContextRead(BaseModel):
    """当前actor在租户内的唯一技能编辑关联，客户端换scope不能抹掉它。"""

    candidate_id: str | None = None
    candidate_revision: int | None = None
    status: str | None = None
    scope_agent_id: str = ""
    scope_profile_id: str | None = None
    ordinary: bool = False
    editor_context: str | None = None
