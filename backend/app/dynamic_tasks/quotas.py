"""
@Time       : 2026/08/10 23:55
@Author     : zhanglp8181
@File       : quotas.py
@CallChain  : Agent Loop/动态工具 dispatch → DynamicTaskQuotaService → quota lease unique slots
@Description: 以数据库唯一槽位原子限制 tenant、Agent、用户和工具并发，并在权威终态释放临时租约。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import DynamicTaskQuotaLease, SopInstance, SopOperation


class DynamicTaskQuotaError(RuntimeError):
    """表示配额未配置或没有可用数据库槽位。"""

    code = "DYNAMIC_TASK_QUOTA_EXCEEDED"

    def __init__(self, code: str) -> None:
        """仅保存稳定错误码，避免把 scope 标识写入用户错误或日志。"""

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DynamicTaskQuotaLimits:
    """保存部署方按容量验证后显式配置的四级并发上限。"""

    tenant: int = 0
    agent: int = 0
    user: int = 0
    tool: int = 0

    @property
    def configured(self) -> bool:
        """只有四级上限均为正数时才允许报告配额门禁就绪。"""

        return all(value > 0 for value in (self.tenant, self.agent, self.user, self.tool))


class DynamicTaskQuotaService:
    """通过唯一槽位竞争提供 SQLite/MySQL 一致的跨进程并发上限。"""

    def __init__(self, db: Session) -> None:
        """绑定当前业务事务，槽位与 Execution/Operation 状态同事务提交。"""

        self.db = db

    def acquire_execution(
        self,
        instance: SopInstance,
        *,
        limits: DynamicTaskQuotaLimits,
    ) -> None:
        """为新动态 Execution 原子取得 tenant、Agent 和用户槽位，任一失败回滚全部。"""

        if instance.kind != "dynamic_task" or not instance.agent_id or not instance.initiator_user_id:
            raise DynamicTaskQuotaError("DYNAMIC_TASK_QUOTA_IDENTITY_INVALID")
        if not limits.configured:
            raise DynamicTaskQuotaError("DYNAMIC_TASK_QUOTA_NOT_CONFIGURED")
        with self.db.begin_nested():
            self._acquire(
                tenant_id=instance.tenant_id,
                scope_type="tenant",
                scope_ref=instance.tenant_id,
                limit=limits.tenant,
                holder_type="execution",
                holder_id=instance.id,
            )
            self._acquire(
                tenant_id=instance.tenant_id,
                scope_type="agent",
                scope_ref=instance.agent_id,
                limit=limits.agent,
                holder_type="execution",
                holder_id=instance.id,
            )
            self._acquire(
                tenant_id=instance.tenant_id,
                scope_type="user",
                scope_ref=instance.initiator_user_id,
                limit=limits.user,
                holder_type="execution",
                holder_id=instance.id,
            )

    def acquire_tool_operation(
        self,
        operation: SopOperation,
        *,
        limit: int,
    ) -> None:
        """在工具 dispatch 前取得 tenant 内工具槽位；unknown 对账完成前持续占用。"""

        if limit <= 0:
            raise DynamicTaskQuotaError("DYNAMIC_TASK_QUOTA_NOT_CONFIGURED")
        self._acquire(
            tenant_id=operation.tenant_id,
            scope_type="tool",
            scope_ref=operation.operation_name,
            limit=limit,
            holder_type="operation",
            holder_id=operation.id,
        )

    def acquire_parallel_contract(
        self,
        operation: SopOperation,
        *,
        concurrency_key: str,
        limit: int,
    ) -> None:
        """跨进程按 tenant+concurrency_key 取得契约槽，随 Operation 终态统一释放。"""

        if not concurrency_key.strip() or limit <= 0:
            raise DynamicTaskQuotaError("DYNAMIC_PARALLEL_CONTRACT_INVALID")
        self._acquire(
            tenant_id=operation.tenant_id,
            scope_type="parallel_contract",
            scope_ref=concurrency_key,
            limit=limit,
            holder_type="operation",
            holder_id=operation.id,
        )

    def release_execution(self, instance: SopInstance) -> None:
        """删除指定 Execution 的临时并发槽；重复终态调用保持幂等。"""

        self.db.exec(
            delete(DynamicTaskQuotaLease).where(
                DynamicTaskQuotaLease.tenant_id == instance.tenant_id,
                DynamicTaskQuotaLease.holder_type == "execution",
                DynamicTaskQuotaLease.holder_id == instance.id,
            )
        )

    def release_tool_operation(self, operation: SopOperation) -> None:
        """在工具确定终态后释放槽位；unknown 状态不得调用。"""

        self.db.exec(
            delete(DynamicTaskQuotaLease).where(
                DynamicTaskQuotaLease.tenant_id == operation.tenant_id,
                DynamicTaskQuotaLease.holder_type == "operation",
                DynamicTaskQuotaLease.holder_id == operation.id,
            )
        )

    def _acquire(
        self,
        *,
        tenant_id: str,
        scope_type: str,
        scope_ref: str,
        limit: int,
        holder_type: str,
        holder_id: str,
    ) -> None:
        """幂等复用 holder 槽，否则逐个竞争唯一 slot，耗尽后返回稳定拒绝码。"""

        existing = self.db.exec(
            select(DynamicTaskQuotaLease).where(
                DynamicTaskQuotaLease.tenant_id == tenant_id,
                DynamicTaskQuotaLease.holder_type == holder_type,
                DynamicTaskQuotaLease.holder_id == holder_id,
                DynamicTaskQuotaLease.scope_type == scope_type,
            )
        ).first()
        if existing is not None:
            if existing.scope_ref != scope_ref or existing.slot_number >= limit:
                raise DynamicTaskQuotaError("DYNAMIC_TASK_QUOTA_LEASE_CONFLICT")
            return
        for slot_number in range(limit):
            lease = DynamicTaskQuotaLease(
                tenant_id=tenant_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                slot_number=slot_number,
                holder_type=holder_type,
                holder_id=holder_id,
            )
            try:
                with self.db.begin_nested():
                    self.db.add(lease)
                    self.db.flush()
                return
            except IntegrityError:
                continue
        raise DynamicTaskQuotaError(f"DYNAMIC_TASK_{scope_type.upper()}_QUOTA_EXCEEDED")


def quota_limits_from_settings(settings: object) -> DynamicTaskQuotaLimits:
    """从真实 Settings 或测试替身安全读取四级上限，缺字段保持未配置。"""

    return DynamicTaskQuotaLimits(
        tenant=int(getattr(settings, "dynamic_task_max_active_per_tenant", 0)),
        agent=int(getattr(settings, "dynamic_task_max_active_per_agent", 0)),
        user=int(getattr(settings, "dynamic_task_max_active_per_user", 0)),
        tool=int(getattr(settings, "dynamic_task_max_active_per_tool", 0)),
    )
