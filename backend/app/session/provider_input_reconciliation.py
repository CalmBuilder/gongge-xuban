"""
@Time       : 2026/08/20 16:25
@Author     : zhanglp8181
@File       : provider_input_reconciliation.py
@CallChain  : ProviderInputDispatchGateway → 对账作业 → provider adapter → 删除作业
@Description: 以租户、receipt、lease和fencing约束第三方输入文件暴露对账与删除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.db.models import (
    ProviderInputDispatchGroup,
    ProviderInputDispatchReceipt,
    ProviderInputExposureReconciliationJob,
    utc_now,
)
from app.session.input_bindings import InputBindingError


ProviderExposureOutcomeKind = Literal[
    "found",
    "not_found",
    "unknown",
    "deleted",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class ProviderExposureOutcome:
    """供应商适配器返回的有限状态，不把未知结果伪装成删除成功。"""

    kind: ProviderExposureOutcomeKind
    provider_file_id: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


class ProviderExposureAdapter(Protocol):
    """供应商文件查询/删除的最小受控边界，禁止把HTTP细节带入账本。"""

    def reconcile_exposure(
        self,
        *,
        tenant_id: str,
        provider_request_id: str | None,
        dispatch_token: str,
    ) -> ProviderExposureOutcome:
        """按请求或dispatch token查询未知外发是否产生供应商文件。"""

    def delete_file(self, *, tenant_id: str, provider_file_id: str) -> ProviderExposureOutcome:
        """删除已确认的供应商文件并返回有限终态。"""


class ProviderInputReconciliationService:
    """持久化第三方暴露对账和删除作业，所有查询都带tenant边界。"""

    LEASE_SECONDS = 60
    MAX_ATTEMPTS = 3

    def __init__(self, db: Session) -> None:
        """绑定调用方数据库会话；网络适配器只在run_once中调用。"""

        self.db = db

    def schedule_unknown_group(
        self,
        group: ProviderInputDispatchGroup,
    ) -> list[ProviderInputExposureReconciliationJob]:
        """为unknown Receipt建立唯一对账作业，不创建重发attempt。"""

        receipts_by_id = {
            receipt.id: receipt
            for receipt in self.db.exec(
                select(ProviderInputDispatchReceipt).where(
                    ProviderInputDispatchReceipt.id.in_(group.ordered_receipt_ids_json),
                    ProviderInputDispatchReceipt.tenant_id == group.tenant_id,
                    ProviderInputDispatchReceipt.dispatch_group_id == group.id,
                    ProviderInputDispatchReceipt.status == "unknown",
                )
            ).all()
        }
        receipts = [receipts_by_id.get(receipt_id) for receipt_id in group.ordered_receipt_ids_json]
        if not receipts or any(receipt is None for receipt in receipts):
            raise InputBindingError("ATTACHMENT_PROVIDER_RECONCILIATION_INVALID")
        jobs: list[ProviderInputExposureReconciliationJob] = []
        for receipt in receipts:
            assert receipt is not None
            if (
                receipt.tenant_id != group.tenant_id
                or receipt.dispatch_group_id != group.id
                or receipt.status != "unknown"
            ):
                continue
            existing = self.db.exec(
                select(ProviderInputExposureReconciliationJob).where(
                    ProviderInputExposureReconciliationJob.tenant_id == group.tenant_id,
                    ProviderInputExposureReconciliationJob.dispatch_receipt_id == receipt.id,
                    ProviderInputExposureReconciliationJob.job_kind == "reconcile_exposure",
                )
            ).first()
            if existing is not None:
                jobs.append(existing)
                continue
            job = ProviderInputExposureReconciliationJob(
                tenant_id=group.tenant_id,
                dispatch_group_id=group.id,
                dispatch_receipt_id=receipt.id,
                job_kind="reconcile_exposure",
                provider_request_id=receipt.provider_request_id,
                dispatch_token=receipt.dispatch_token,
            )
            self.db.add(job)
            self.db.flush()
            jobs.append(job)
        return jobs

    def claim_next(
        self,
        *,
        worker_id: str,
        tenant_id: str | None = None,
    ) -> ProviderInputExposureReconciliationJob | None:
        """以租户可选过滤和lease/fencing CAS领取一个对账或删除作业。"""

        now = utc_now()
        base = select(ProviderInputExposureReconciliationJob).where(
            or_(
                ProviderInputExposureReconciliationJob.status.in_(("pending", "retry_wait")),
                (
                    (ProviderInputExposureReconciliationJob.status == "dispatching")
                    & ProviderInputExposureReconciliationJob.lease_expires_at.is_not(None)
                    & (ProviderInputExposureReconciliationJob.lease_expires_at <= now)
                ),
            )
        )
        if tenant_id is not None:
            base = base.where(ProviderInputExposureReconciliationJob.tenant_id == tenant_id)
        candidate = self.db.exec(
            base.order_by(
                ProviderInputExposureReconciliationJob.created_at,
                ProviderInputExposureReconciliationJob.id,
            )
        ).first()
        if candidate is None:
            return None
        lease_expires_at = now + timedelta(seconds=self.LEASE_SECONDS)
        result = self.db.exec(
            update(ProviderInputExposureReconciliationJob)
            .where(
                ProviderInputExposureReconciliationJob.id == candidate.id,
                ProviderInputExposureReconciliationJob.fencing_token == candidate.fencing_token,
                or_(
                    ProviderInputExposureReconciliationJob.status.in_(("pending", "retry_wait")),
                    (
                        (ProviderInputExposureReconciliationJob.status == "dispatching")
                        & ProviderInputExposureReconciliationJob.lease_expires_at.is_not(None)
                        & (ProviderInputExposureReconciliationJob.lease_expires_at <= now)
                    ),
                ),
            )
            .values(
                status="dispatching",
                attempt_no=ProviderInputExposureReconciliationJob.attempt_no + 1,
                fencing_token=ProviderInputExposureReconciliationJob.fencing_token + 1,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        self.db.flush()
        self.db.refresh(candidate)
        return candidate

    def run_once(
        self,
        adapter: ProviderExposureAdapter,
        *,
        worker_id: str,
        tenant_id: str | None = None,
    ) -> ProviderInputExposureReconciliationJob | None:
        """领取并执行一个作业，适配器不支持时稳定收敛Attention。"""

        job = self.claim_next(worker_id=worker_id, tenant_id=tenant_id)
        if job is None:
            return None
        try:
            if job.job_kind == "reconcile_exposure":
                outcome = adapter.reconcile_exposure(
                    tenant_id=job.tenant_id,
                    provider_request_id=job.provider_request_id,
                    dispatch_token=job.dispatch_token,
                )
                self._apply_reconcile_outcome(job, outcome, worker_id=worker_id)
            elif job.job_kind == "delete_file":
                if not job.provider_file_id:
                    self._finish(job, status="attention", worker_id=worker_id, error_code="PROVIDER_FILE_ID_MISSING")
                else:
                    outcome = adapter.delete_file(
                        tenant_id=job.tenant_id,
                        provider_file_id=job.provider_file_id,
                    )
                    self._apply_delete_outcome(job, outcome, worker_id=worker_id)
            else:
                self._finish(job, status="dead_letter", worker_id=worker_id, error_code="PROVIDER_JOB_KIND_INVALID")
        except Exception as exc:  # noqa: BLE001 - 网络/适配器异常只能进入可恢复重试。
            self._retry_or_dead_letter(job, worker_id=worker_id, detail=str(exc))
        self.db.commit()
        self.db.refresh(job)
        return job

    def _apply_reconcile_outcome(
        self,
        job: ProviderInputExposureReconciliationJob,
        outcome: ProviderExposureOutcome,
        *,
        worker_id: str,
    ) -> None:
        """将查询结果映射为reconciled/not_found/Attention并派生唯一删除作业。"""

        if outcome.kind == "unsupported":
            self._finish(job, status="attention", worker_id=worker_id, error_code="PROVIDER_RECONCILE_UNSUPPORTED", detail=outcome.detail)
            return
        if outcome.kind == "not_found":
            self._finish(job, status="not_found", worker_id=worker_id, detail=outcome.detail)
            return
        if outcome.kind != "found" or not outcome.provider_file_id:
            self._retry_or_dead_letter(job, worker_id=worker_id, detail=str(outcome.detail))
            return
        self._finish(
            job,
            status="reconciled",
            worker_id=worker_id,
            provider_file_id=outcome.provider_file_id,
            detail=outcome.detail,
        )
        self._schedule_delete_job(job, provider_file_id=outcome.provider_file_id)

    def _apply_delete_outcome(
        self,
        job: ProviderInputExposureReconciliationJob,
        outcome: ProviderExposureOutcome,
        *,
        worker_id: str,
    ) -> None:
        """将第三方删除结果收敛为deleted/not_found/Attention，拒绝空file-id删除。"""

        if outcome.kind == "unsupported":
            self._finish(job, status="attention", worker_id=worker_id, error_code="PROVIDER_DELETE_UNSUPPORTED", detail=outcome.detail)
        elif outcome.kind in {"deleted", "not_found"}:
            self._finish(job, status=outcome.kind, worker_id=worker_id, detail=outcome.detail)
        else:
            self._retry_or_dead_letter(job, worker_id=worker_id, detail=str(outcome.detail))

    def _schedule_delete_job(
        self,
        reconciliation_job: ProviderInputExposureReconciliationJob,
        *,
        provider_file_id: str,
    ) -> ProviderInputExposureReconciliationJob:
        """按tenant+provider file id幂等派生删除作业，不能跨租户合并文件。"""

        existing = self.db.exec(
            select(ProviderInputExposureReconciliationJob).where(
                ProviderInputExposureReconciliationJob.tenant_id == reconciliation_job.tenant_id,
                ProviderInputExposureReconciliationJob.provider_file_id == provider_file_id,
                ProviderInputExposureReconciliationJob.job_kind == "delete_file",
            )
        ).first()
        if existing is not None:
            return existing
        job = ProviderInputExposureReconciliationJob(
            tenant_id=reconciliation_job.tenant_id,
            dispatch_group_id=reconciliation_job.dispatch_group_id,
            dispatch_receipt_id=reconciliation_job.dispatch_receipt_id,
            job_kind="delete_file",
            provider_file_id=provider_file_id,
            provider_request_id=reconciliation_job.provider_request_id,
            dispatch_token=reconciliation_job.dispatch_token,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _finish(
        self,
        job: ProviderInputExposureReconciliationJob,
        *,
        status: str,
        worker_id: str,
        provider_file_id: str | None = None,
        error_code: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """以租户、owner、fencing和未过期lease CAS提交作业终态。"""

        now = utc_now()
        values: dict[str, object] = {
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "error_code": error_code,
            "updated_at": now,
            "finished_at": now if status not in {"retry_wait", "unknown"} else None,
        }
        if provider_file_id is not None:
            values["provider_file_id"] = provider_file_id
        if detail is not None:
            values["result_json"] = detail
        result = self.db.exec(
            update(ProviderInputExposureReconciliationJob)
            .where(
                ProviderInputExposureReconciliationJob.id == job.id,
                ProviderInputExposureReconciliationJob.tenant_id == job.tenant_id,
                ProviderInputExposureReconciliationJob.status == "dispatching",
                ProviderInputExposureReconciliationJob.lease_owner == worker_id,
                ProviderInputExposureReconciliationJob.fencing_token == job.fencing_token,
                ProviderInputExposureReconciliationJob.lease_expires_at > now,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise InputBindingError("ATTACHMENT_PROVIDER_RECONCILIATION_FENCED")

    def _retry_or_dead_letter(
        self,
        job: ProviderInputExposureReconciliationJob,
        *,
        worker_id: str,
        detail: str,
    ) -> None:
        """将不确定网络结果限制为有限重试，超过上限进入dead_letter而不伪造删除。"""

        status = "dead_letter" if job.attempt_no >= self.MAX_ATTEMPTS else "retry_wait"
        self._finish(
            job,
            status=status,
            worker_id=worker_id,
            error_code="PROVIDER_RECONCILIATION_UNKNOWN",
            detail={"detail": detail},
        )
