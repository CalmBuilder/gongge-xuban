"""
@Time       : 2026/08/10 23:35
@Author     : zhanglp8181
@File       : operations.py
@CallChain  : 运维 API → DynamicTaskOperationsService → Execution/Signal/Operation 权威表
@Description: 聚合动态任务脱敏运行指标，并按显式配置阈值计算不改变业务状态的告警快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, select

from app.db.models import (
    ExecutionPublication,
    ExecutionSignal,
    DynamicTaskQuotaLease,
    SopInstance,
    SopOperation,
    SopWorkItem,
)
from app.sop_runtime.execution_store import SopExecutionStore
from app.dynamic_tasks.quotas import DynamicTaskQuotaLimits


@dataclass(frozen=True, slots=True)
class DynamicTaskAlertThresholds:
    """保存由部署方显式给出的告警阈值；零表示尚未配置而不是无限容量。"""

    signal_backlog: int = 0
    dead_letters: int = 0
    unknown_operations: int = 0
    publication_backlog: int = 0
    waiting_age_seconds: int = 0

    @property
    def configured(self) -> bool:
        """只有全部关键阈值均为正数时才允许报告生产告警配置就绪。"""

        return all(
            value > 0
            for value in (
                self.signal_backlog,
                self.dead_letters,
                self.unknown_operations,
                self.publication_backlog,
                self.waiting_age_seconds,
            )
        )


class DynamicTaskOperationsService:
    """从统一 Runtime 权威表生成单租户、无业务正文的运行快照。"""

    def __init__(self, db: Session) -> None:
        """绑定只读数据库会话，不维护进程内累计指标。"""

        self.db = db

    def snapshot(
        self,
        *,
        tenant_id: str,
        thresholds: DynamicTaskAlertThresholds,
        quota_limits: DynamicTaskQuotaLimits,
    ) -> dict[str, object]:
        """聚合状态计数、最老等待时长与阈值告警，严格限定动态任务和当前租户。"""

        now = SopExecutionStore(self.db).database_now()
        execution_statuses = self._execution_statuses(tenant_id)
        signal_statuses = self._signal_statuses(tenant_id)
        operation_statuses = self._operation_statuses(tenant_id)
        publication_statuses = self._publication_statuses(tenant_id)
        attention_statuses = self._attention_statuses(tenant_id)
        quota_leases = self._quota_leases(tenant_id)
        oldest_waiting = self.db.exec(
            select(func.min(SopInstance.updated_at)).where(
                SopInstance.tenant_id == tenant_id,
                SopInstance.kind == "dynamic_task",
                SopInstance.status == "waiting",
            )
        ).one()
        waiting_age_seconds = _age_seconds(now, oldest_waiting)
        signal_backlog = sum(
            signal_statuses.get(status, 0) for status in ("pending", "claimed")
        )
        dead_letters = signal_statuses.get("dead_letter", 0) + publication_statuses.get(
            "dead_letter", 0
        )
        unknown_operations = operation_statuses.get("unknown", 0)
        publication_backlog = sum(
            publication_statuses.get(status, 0)
            for status in ("pending", "delivering", "unknown")
        )
        alerts = [
            _alert("signal_backlog", signal_backlog, thresholds.signal_backlog, "warning"),
            _alert("dead_letters", dead_letters, thresholds.dead_letters, "critical"),
            _alert(
                "unknown_operations",
                unknown_operations,
                thresholds.unknown_operations,
                "critical",
            ),
            _alert(
                "publication_backlog",
                publication_backlog,
                thresholds.publication_backlog,
                "critical",
            ),
            _alert(
                "waiting_age_seconds",
                waiting_age_seconds,
                thresholds.waiting_age_seconds,
                "warning",
            ),
        ]
        return {
            "tenant_id": tenant_id,
            "observed_at": now,
            "thresholds_configured": thresholds.configured,
            "quota_limits_configured": quota_limits.configured,
            "quota_limits": {
                "tenant": quota_limits.tenant,
                "agent": quota_limits.agent,
                "user": quota_limits.user,
                "tool": quota_limits.tool,
            },
            "quota_leases": quota_leases,
            "executions": execution_statuses,
            "signals": signal_statuses,
            "operations": operation_statuses,
            "publications": publication_statuses,
            "attentions": attention_statuses,
            "oldest_waiting_age_seconds": waiting_age_seconds,
            "alerts": alerts,
        }

    def _execution_statuses(self, tenant_id: str) -> dict[str, int]:
        """按终态/活动态统计当前租户动态 Execution。"""

        rows = self.db.exec(
            select(SopInstance.status, func.count(SopInstance.id))
            .where(SopInstance.tenant_id == tenant_id, SopInstance.kind == "dynamic_task")
            .group_by(SopInstance.status)
        ).all()
        return _count_map(rows)

    def _signal_statuses(self, tenant_id: str) -> dict[str, int]:
        """仅统计属于动态 Execution 的持久信号，排除同租户正式 SOP。"""

        rows = self.db.exec(
            select(ExecutionSignal.status, func.count(ExecutionSignal.id))
            .join(SopInstance, ExecutionSignal.execution_id == SopInstance.id)
            .where(
                ExecutionSignal.tenant_id == tenant_id,
                SopInstance.tenant_id == tenant_id,
                SopInstance.kind == "dynamic_task",
            )
            .group_by(ExecutionSignal.status)
        ).all()
        return _count_map(rows)

    def _operation_statuses(self, tenant_id: str) -> dict[str, int]:
        """仅统计动态 Execution 的逻辑 Operation 状态，不返回参数或结果正文。"""

        rows = self.db.exec(
            select(SopOperation.status, func.count(SopOperation.id))
            .join(SopInstance, SopOperation.instance_id == SopInstance.id)
            .where(
                SopOperation.tenant_id == tenant_id,
                SopInstance.tenant_id == tenant_id,
                SopInstance.kind == "dynamic_task",
            )
            .group_by(SopOperation.status)
        ).all()
        return _count_map(rows)

    def _publication_statuses(self, tenant_id: str) -> dict[str, int]:
        """统计动态结果应用内/外部发布状态，用于识别结果投递积压。"""

        rows = self.db.exec(
            select(ExecutionPublication.status, func.count(ExecutionPublication.id))
            .join(SopInstance, ExecutionPublication.execution_id == SopInstance.id)
            .where(
                ExecutionPublication.tenant_id == tenant_id,
                SopInstance.tenant_id == tenant_id,
                SopInstance.kind == "dynamic_task",
            )
            .group_by(ExecutionPublication.status)
        ).all()
        return _count_map(rows)

    def _attention_statuses(self, tenant_id: str) -> dict[str, int]:
        """统计动态任务统一待处理状态，不暴露候选人、评论或业务载荷。"""

        rows = self.db.exec(
            select(SopWorkItem.status, func.count(SopWorkItem.id))
            .join(SopInstance, SopWorkItem.instance_id == SopInstance.id)
            .where(
                SopWorkItem.tenant_id == tenant_id,
                SopInstance.tenant_id == tenant_id,
                SopInstance.kind == "dynamic_task",
            )
            .group_by(SopWorkItem.status)
        ).all()
        return _count_map(rows)

    def _quota_leases(self, tenant_id: str) -> dict[str, int]:
        """按 scope 统计活动并发槽位，不返回 Agent、用户或工具标识。"""

        rows = self.db.exec(
            select(DynamicTaskQuotaLease.scope_type, func.count(DynamicTaskQuotaLease.id))
            .where(DynamicTaskQuotaLease.tenant_id == tenant_id)
            .group_by(DynamicTaskQuotaLease.scope_type)
        ).all()
        return _count_map(rows)


def _count_map(rows: list[tuple[object, object]]) -> dict[str, int]:
    """把 SQLite/MySQL 均可返回的 group-by 行规范化为稳定状态计数字典。"""

    return {str(status): int(count) for status, count in rows}


def _age_seconds(now: datetime, value: datetime | None) -> int:
    """以数据库 UTC 时间计算非负年龄，并兼容方言返回的 naive datetime。"""

    if value is None:
        return 0
    normalized_now = now.replace(tzinfo=None)
    normalized_value = value.replace(tzinfo=None)
    return max(0, int((normalized_now - normalized_value).total_seconds()))


def _alert(code: str, current: int, threshold: int, severity: str) -> dict[str, object]:
    """生成不泄露业务字段的阈值结果；未配置阈值时绝不假报正常。"""

    enabled = threshold > 0
    return {
        "code": code,
        "severity": severity,
        "current": current,
        "threshold": threshold if enabled else None,
        "enabled": enabled,
        "triggered": enabled and current >= threshold,
    }
