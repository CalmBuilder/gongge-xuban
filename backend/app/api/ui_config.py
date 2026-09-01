"""
@Time       : 2026/08/28 15:00
@Author     : zhanglp8181
@File       : ui_config.py
@CallChain  : Enterprise/Chat UI → UIConfig API → UIConfig → AgentLoop 上下文与执行限制
@Description: 提供租户级展示、执行上限和会话上下文压缩配置的读写接口。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import UIConfig, User, utc_now
from app.core.conversation_context import (
    MAX_CONTEXT_TOKEN_BUDGET,
    MAX_COMPACTION_TRIGGER_RATIO,
    MAX_RECENT_ROUND_LIMIT,
    MAX_SUMMARY_TOKEN_BUDGET,
    MIN_COMPACTION_TRIGGER_RATIO,
    MIN_CONTEXT_TOKEN_BUDGET,
    MIN_SUMMARY_TOKEN_BUDGET,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

enterprise_router = APIRouter(
    prefix="/api/enterprise/ui-config",
    tags=["enterprise:ui-config"],
    dependencies=[Depends(get_current_user)],
)
chat_router = APIRouter(prefix="/api/chat/ui-config", tags=["chat:ui-config"])


class UIConfigRead(BaseModel):
    tenant_id: str
    show_thinking_trace: bool
    show_skill_trace: bool
    show_tool_trace: bool
    reflection_max_rounds: int
    agent_loop_max_actions: int
    context_token_budget: int
    context_compaction_trigger_ratio: float
    context_recent_round_limit: int
    long_summary_token_budget: int
    medium_summary_token_budget: int
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class UIConfigUpdateRequest(BaseModel):
    tenant_id: str
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = Field(default=1, ge=0, le=5)
    agent_loop_max_actions: int = Field(default=6, ge=1, le=20)
    context_token_budget: int | None = Field(
        default=None,
        ge=MIN_CONTEXT_TOKEN_BUDGET,
        le=MAX_CONTEXT_TOKEN_BUDGET,
    )
    context_compaction_trigger_ratio: float | None = Field(
        default=None,
        ge=MIN_COMPACTION_TRIGGER_RATIO,
        le=MAX_COMPACTION_TRIGGER_RATIO,
    )
    context_recent_round_limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_RECENT_ROUND_LIMIT,
    )
    long_summary_token_budget: int | None = Field(
        default=None,
        ge=MIN_SUMMARY_TOKEN_BUDGET,
        le=MAX_SUMMARY_TOKEN_BUDGET,
    )
    medium_summary_token_budget: int | None = Field(
        default=None,
        ge=MIN_SUMMARY_TOKEN_BUDGET,
        le=MAX_SUMMARY_TOKEN_BUDGET,
    )

    @model_validator(mode="after")
    def validate_explicit_summary_budget(self) -> "UIConfigUpdateRequest":
        """校验客户端同时提交的摘要预算不能超过本次有效上下文预算。"""

        if (
            self.context_token_budget is None
            or self.long_summary_token_budget is None
            or self.medium_summary_token_budget is None
        ):
            return self
        token_budget = self.context_token_budget
        long_budget = self.long_summary_token_budget
        medium_budget = self.medium_summary_token_budget
        if long_budget + medium_budget > token_budget:
            raise ValueError("summary budgets must not exceed context token budget")
        return self


def ui_config_read(row: UIConfig) -> UIConfigRead:
    """把数据库中的租户配置转换为管理端和聊天端共用的响应契约。"""

    return UIConfigRead(
        tenant_id=row.tenant_id,
        show_thinking_trace=row.show_thinking_trace,
        show_skill_trace=row.show_skill_trace,
        show_tool_trace=row.show_tool_trace,
        reflection_max_rounds=row.reflection_max_rounds,
        agent_loop_max_actions=row.agent_loop_max_actions,
        context_token_budget=row.context_token_budget,
        context_compaction_trigger_ratio=row.context_compaction_trigger_ratio,
        context_recent_round_limit=row.context_recent_round_limit,
        long_summary_token_budget=row.long_summary_token_budget,
        medium_summary_token_budget=row.medium_summary_token_budget,
        updated_at=row.updated_at.isoformat(),
    )


def get_or_create_ui_config(db: Session, tenant_id: str) -> UIConfig:
    ensure_tenant(db, tenant_id)
    row = db.get(UIConfig, tenant_id)
    if not row:
        row = UIConfig(tenant_id=tenant_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _effective_context_summary_budgets(
    row: UIConfig,
    request: UIConfigUpdateRequest,
) -> tuple[int, int, int]:
    """合并局部更新后的上下文预算，并拒绝持久化超出总预算的摘要配置。"""

    context_token_budget = (
        request.context_token_budget
        if request.context_token_budget is not None
        else row.context_token_budget
    )
    long_summary_token_budget = (
        request.long_summary_token_budget
        if request.long_summary_token_budget is not None
        else row.long_summary_token_budget
    )
    medium_summary_token_budget = (
        request.medium_summary_token_budget
        if request.medium_summary_token_budget is not None
        else row.medium_summary_token_budget
    )
    if long_summary_token_budget + medium_summary_token_budget > context_token_budget:
        raise HTTPException(
            status_code=422,
            detail=(
                "long_summary_token_budget + medium_summary_token_budget "
                "must not exceed context_token_budget"
            ),
        )
    return context_token_budget, long_summary_token_budget, medium_summary_token_budget


@enterprise_router.get("", response_model=UIConfigRead, dependencies=[Depends(require_current_tenant)])
def get_enterprise_ui_config(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> UIConfigRead:
    return ui_config_read(get_or_create_ui_config(db, tenant_id))


@enterprise_router.put("", response_model=UIConfigRead)
def update_enterprise_ui_config(
    request: UIConfigUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> UIConfigRead:
    """校验租户管理员权限后更新展示、执行和上下文压缩配置。"""

    ensure_tenant_admin(request.tenant_id, current_user)
    row = get_or_create_ui_config(db, request.tenant_id)
    (
        context_token_budget,
        long_summary_token_budget,
        medium_summary_token_budget,
    ) = _effective_context_summary_budgets(row, request)
    row.show_thinking_trace = request.show_thinking_trace
    row.show_skill_trace = request.show_skill_trace
    row.show_tool_trace = request.show_tool_trace
    row.reflection_max_rounds = request.reflection_max_rounds
    row.agent_loop_max_actions = request.agent_loop_max_actions
    row.context_token_budget = context_token_budget
    if request.context_compaction_trigger_ratio is not None:
        row.context_compaction_trigger_ratio = request.context_compaction_trigger_ratio
    if request.context_recent_round_limit is not None:
        row.context_recent_round_limit = request.context_recent_round_limit
    row.long_summary_token_budget = long_summary_token_budget
    row.medium_summary_token_budget = medium_summary_token_budget
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return ui_config_read(row)


@chat_router.get("", response_model=UIConfigRead)
def get_chat_ui_config(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UIConfigRead:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    return ui_config_read(get_or_create_ui_config(db, tenant_id))
