"""
@Time       : 2026/08/03 22:20
@Author     : zhanglp8181
@File       : executions.py
@CallChain  : 执行卡/Chat 控制 → FastAPI → ExecutionControlService/Execution Store
@Description: 提供统一 Execution 查询、cancel/steer 命令和不可变结果读取接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import (
    ExecutionCommand,
    ExecutionPublication,
    ExecutionResult,
    SopInstance,
    User,
)
from app.organization.permissions import user_permission_codes
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.sop_runtime.execution_control import ExecutionControlError, ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionConflictError, SopExecutionStore


router = APIRouter(prefix="/api/executions", tags=["executions"])


class ExecutionCommandRequest(BaseModel):
    """提交 cancel/steer 的 tenant、幂等键和 Execution CAS 信封。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    command_id: str = Field(min_length=1, max_length=128)
    command_type: str = Field(pattern="^(cancel|steer)$")
    expected_revision: int = Field(ge=0)
    payload: dict[str, object] = Field(default_factory=dict)
    source_message_id: str | None = Field(default=None, max_length=512)


class ExecutionCommandRead(BaseModel):
    """返回命令持久状态和服务端处置结果。"""

    command_id: str
    command_type: str
    status: str
    expected_revision: int
    result: dict[str, object]
    reason_code: str | None
    issued_at: str
    consumed_at: str | None


class ExecutionRead(BaseModel):
    """返回执行卡可直接使用、无需由聊天消息推导的权威状态。"""

    id: str
    tenant_id: str
    session_id: str
    kind: str
    status: str
    revision: int
    effect_state: str
    cancellation_disposition: str
    current_plan_revision_id: str | None
    current_result_id: str | None
    terminal_reason: dict[str, object]


class ExecutionResultRead(BaseModel):
    """返回不可变验证结果和全部发布处置，不把 verified 等同 delivered。"""

    id: str
    revision: int
    status: str
    checksum: str
    result: dict[str, object]
    verification: dict[str, object]
    publications: list[dict[str, object]]
    created_at: str


@router.get("/{execution_id}", response_model=ExecutionRead)
def get_execution(
    execution_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ExecutionRead:
    """按 tenant 和显式 Execution 管理资格读取执行卡状态。"""

    instance = _authorized_execution(db, tenant_id, execution_id, current_user)
    return _execution_read(instance)


@router.post("/{execution_id}/commands", response_model=ExecutionCommandRead)
def issue_execution_command(
    execution_id: str,
    request: ExecutionCommandRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ExecutionCommandRead:
    """持久登记命令；cancel 同事务收敛，steer 保持 pending 等待 B1.2 消费。"""

    instance = _authorized_execution(db, request.tenant_id, execution_id, current_user)
    store = SopExecutionStore(db)
    control = ExecutionControlService(db, store)
    try:
        command, _ = control.issue_command(
            instance,
            command_id=request.command_id,
            command_type=request.command_type,
            actor_user_id=current_user.id,
            expected_execution_revision=request.expected_revision,
            payload=request.payload,
            source_message_id=request.source_message_id,
        )
        if command.command_type == "cancel" and command.status == "pending":
            with store.owned(
                instance,
                worker_id=f"command-{command.id[-16:]}",
            ):
                control.apply_cancel_command(
                    instance,
                    command,
                    worker_id=f"command-{command.id[-16:]}",
                )
        db.commit()
        db.refresh(command)
    except (ExecutionControlError, SopExecutionConflictError) as error:
        db.rollback()
        raise _execution_error(error) from error
    return _command_read(command)


@router.get("/{execution_id}/commands/{command_id}", response_model=ExecutionCommandRead)
def get_execution_command(
    execution_id: str,
    command_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ExecutionCommandRead:
    """读取指定幂等命令的最终或等待状态，拒绝跨 Execution 重用 command id。"""

    _authorized_execution(db, tenant_id, execution_id, current_user)
    command = db.exec(
        select(ExecutionCommand).where(
            ExecutionCommand.tenant_id == tenant_id,
            ExecutionCommand.execution_id == execution_id,
            ExecutionCommand.command_id == command_id,
        )
    ).first()
    if command is None:
        raise HTTPException(status_code=404, detail="EXECUTION_COMMAND_NOT_FOUND")
    return _command_read(command)


@router.get("/{execution_id}/result", response_model=ExecutionResultRead)
def get_execution_result(
    execution_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ExecutionResultRead:
    """读取当前冻结结果及 required publication 状态，未生成时返回明确 404。"""

    instance = _authorized_execution(db, tenant_id, execution_id, current_user)
    result = db.get(ExecutionResult, instance.current_result_id) if instance.current_result_id else None
    if result is None or result.tenant_id != tenant_id or result.execution_id != execution_id:
        raise HTTPException(status_code=404, detail="EXECUTION_RESULT_NOT_FOUND")
    publications = db.exec(
        select(ExecutionPublication).where(
            ExecutionPublication.tenant_id == tenant_id,
            ExecutionPublication.execution_id == execution_id,
            ExecutionPublication.result_id == result.id,
        )
    ).all()
    return ExecutionResultRead(
        id=result.id,
        revision=result.result_revision,
        status=result.status,
        checksum=result.checksum,
        result=dict(result.result_json or {}),
        verification=dict(result.verification_json or {}),
        publications=[
            {
                "id": item.id,
                "target_type": item.target_type,
                "target_ref": item.target_ref,
                "required": item.required,
                "status": item.status,
                "receipt": dict(item.receipt_json or {}),
            }
            for item in publications
        ],
        created_at=result.created_at.isoformat(),
    )


def _authorized_execution(
    db: Session,
    tenant_id: str,
    execution_id: str,
    current_user: User,
) -> SopInstance:
    """只允许发起人或持有 execution.manage 的当前租户成员访问控制接口。"""

    ensure_current_user_tenant(tenant_id, current_user)
    instance = db.get(SopInstance, execution_id)
    if instance is None or instance.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="EXECUTION_NOT_FOUND")
    if instance.initiator_user_id != current_user.id and "execution.manage" not in set(
        user_permission_codes(db, tenant_id=tenant_id, user_id=current_user.id)
    ):
        raise HTTPException(status_code=403, detail="EXECUTION_MANAGE_FORBIDDEN")
    return instance


def _execution_read(instance: SopInstance) -> ExecutionRead:
    """把 Execution 聚合映射成稳定 API 契约。"""

    return ExecutionRead(
        id=instance.id,
        tenant_id=instance.tenant_id,
        session_id=instance.session_id,
        kind=instance.kind,
        status=instance.status,
        revision=instance.revision,
        effect_state=instance.effect_state,
        cancellation_disposition=instance.cancellation_disposition,
        current_plan_revision_id=instance.current_plan_revision_id,
        current_result_id=instance.current_result_id,
        terminal_reason=dict(instance.terminal_reason_json or {}),
    )


def _command_read(command: ExecutionCommand) -> ExecutionCommandRead:
    """把命令实体映射成不暴露内部 lease 的客户端投影。"""

    return ExecutionCommandRead(
        command_id=command.command_id,
        command_type=command.command_type,
        status=command.status,
        expected_revision=command.expected_execution_revision,
        result=dict(command.result_json or {}),
        reason_code=command.reason_code,
        issued_at=command.issued_at.isoformat(),
        consumed_at=command.consumed_at.isoformat() if command.consumed_at else None,
    )


def _execution_error(error: ExecutionControlError | SopExecutionConflictError) -> HTTPException:
    """将 Execution 控制拒绝映射为 CAS/租约冲突或非法请求。"""

    code = getattr(error, "code", "EXECUTION_CONFLICT")
    status = 409 if "CONFLICT" in code or "FENCED" in code else 400
    return HTTPException(status_code=status, detail=code)
