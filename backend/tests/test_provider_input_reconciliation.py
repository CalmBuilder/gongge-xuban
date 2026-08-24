"""
@Time       : 2026/08/20 16:35
@Author     : zhanglp8181
@File       : test_provider_input_reconciliation.py
@CallChain  : pytest → ProviderInputReconciliationService → 对账/删除作业账本
@Description: 验证unknown对账、file-id删除、租户隔离和fencing收敛契约。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.db.models import (
    ProviderInputExposureReconciliationJob,
    ProviderInputDispatchReceipt,
)
from app.session.input_bindings import InputBindingError
from app.session.provider_input_reconciliation import (
    ProviderExposureOutcome,
    ProviderInputReconciliationService,
)
from tests.test_provider_input_dispatch import _seed, _session
from app.session.provider_input_dispatch import ProviderInputDispatchGateway


class _Adapter:
    """测试用有限状态provider，模拟查找和删除而不连接外网。"""

    def __init__(self, reconcile: ProviderExposureOutcome, delete: ProviderExposureOutcome) -> None:
        """保存本次测试的查询和删除结果。"""

        self.reconcile_outcome = reconcile
        self.delete_outcome = delete
        self.reconcile_calls = 0
        self.delete_calls = 0

    def reconcile_exposure(
        self,
        *,
        tenant_id: str,
        provider_request_id: str | None,
        dispatch_token: str,
    ) -> ProviderExposureOutcome:
        """记录请求/token查询并返回预置结果。"""

        assert tenant_id in {"tenant-a", "tenant-b"}
        assert provider_request_id is None or provider_request_id.startswith("req-")
        assert dispatch_token
        self.reconcile_calls += 1
        return self.reconcile_outcome

    def delete_file(self, *, tenant_id: str, provider_file_id: str) -> ProviderExposureOutcome:
        """记录带file-id的删除调用并返回预置结果。"""

        assert tenant_id in {"tenant-a", "tenant-b"}
        assert provider_file_id
        self.delete_calls += 1
        return self.delete_outcome


def _unknown_group(db: Session):
    """建立并收敛一个tenant-a的unknown外发Group。"""

    _, read = _seed(db)
    gateway = ProviderInputDispatchGateway(db)
    group = gateway.prepare_turn_group(
        tenant_id="tenant-a",
        turn_id="turn-1",
        read_receipt_ids=[read.id],
        egress_policy_checksum="inline-model-default-v1",
    )
    assert group is not None
    gateway.authorize(group, worker_id="worker-a")
    db.commit()
    gateway.mark_unknown(group)
    db.commit()
    return group


def test_unknown_group_creates_one_tenant_scoped_reconcile_job() -> None:
    """unknown只登记对账作业，不自动重发或伪造第三方删除。"""

    with _session() as db:
        group = _unknown_group(db)
        jobs = db.exec(select(ProviderInputExposureReconciliationJob)).all()
        again = ProviderInputReconciliationService(db).schedule_unknown_group(group)
        job_values = [(job.tenant_id, job.job_kind, job.status, job.id) for job in jobs]
        again_id = again[0].id
        db.commit()

    assert job_values == [("tenant-a", "reconcile_exposure", "pending", job_values[0][3])]
    assert len(again) == 1
    assert again_id == job_values[0][3]


def test_reconcile_found_derives_delete_and_delete_is_idempotent() -> None:
    """先按request/token找到file-id，再派生唯一delete_file并收敛deleted。"""

    with _session() as db:
        _unknown_group(db)
        receipt = db.exec(select(ProviderInputDispatchReceipt)).one()
        receipt.provider_request_id = "req-provider-a"
        db.add(receipt)
        db.commit()
        service = ProviderInputReconciliationService(db)
        adapter = _Adapter(
            ProviderExposureOutcome(kind="found", provider_file_id="file-a"),
            ProviderExposureOutcome(kind="deleted"),
        )

        reconcile_job = service.run_once(adapter, worker_id="reconcile-worker")
        delete_job = service.run_once(adapter, worker_id="delete-worker")
        db.commit()
        jobs = db.exec(
            select(ProviderInputExposureReconciliationJob).order_by(
                ProviderInputExposureReconciliationJob.job_kind
            )
        ).all()

    assert reconcile_job is not None and reconcile_job.status == "reconciled"
    assert delete_job is not None and delete_job.status == "deleted"
    assert [(job.job_kind, job.status) for job in jobs] == [
        ("delete_file", "deleted"),
        ("reconcile_exposure", "reconciled"),
    ]
    assert adapter.reconcile_calls == 1
    assert adapter.delete_calls == 1


def test_unsupported_provider_enters_attention_without_fake_delete() -> None:
    """供应商不支持查询时进入Attention，绝不把unknown显示为已删除。"""

    with _session() as db:
        _unknown_group(db)
        service = ProviderInputReconciliationService(db)
        adapter = _Adapter(
            ProviderExposureOutcome(kind="unsupported"),
            ProviderExposureOutcome(kind="deleted"),
        )
        job = service.run_once(adapter, worker_id="attention-worker")
        jobs = db.exec(select(ProviderInputExposureReconciliationJob)).all()

    assert job is not None and job.status == "attention"
    assert job.error_code == "PROVIDER_RECONCILE_UNSUPPORTED"
    assert len(jobs) == 1
    assert adapter.delete_calls == 0


def test_reconcile_not_found_is_terminal_without_delete_job() -> None:
    """供应商按request/token明确无文件时记录not_found，不派生删除作业。"""

    with _session() as db:
        _unknown_group(db)
        service = ProviderInputReconciliationService(db)
        adapter = _Adapter(
            ProviderExposureOutcome(kind="not_found"),
            ProviderExposureOutcome(kind="deleted"),
        )
        job = service.run_once(adapter, worker_id="not-found-worker")
        jobs = db.exec(select(ProviderInputExposureReconciliationJob)).all()

    assert job is not None and job.status == "not_found"
    assert len(jobs) == 1
    assert adapter.delete_calls == 0


def test_same_provider_file_id_is_tenant_scoped() -> None:
    """相同第三方file-id在不同tenant必须分别保留删除账本，不能跨租户合并。"""

    with _session() as db:
        jobs = [
            ProviderInputExposureReconciliationJob(
                tenant_id=tenant,
                dispatch_group_id=f"group-{tenant}",
                dispatch_receipt_id=f"receipt-{tenant}",
                job_kind="delete_file",
                provider_file_id="shared-file-id",
                dispatch_token=f"token-{tenant}",
            )
            for tenant in ("tenant-a", "tenant-b")
        ]
        db.add_all(jobs)
        db.commit()
        persisted = db.exec(
            select(ProviderInputExposureReconciliationJob).where(
                ProviderInputExposureReconciliationJob.provider_file_id == "shared-file-id"
            )
        ).all()

    assert {job.tenant_id for job in persisted} == {"tenant-a", "tenant-b"}


def test_stale_reconciliation_worker_cannot_finish_after_fencing() -> None:
    """旧worker租约被接管后不能把作业写成deleted或reconciled。"""

    with _session() as db:
        _unknown_group(db)
        service = ProviderInputReconciliationService(db)
        stale = service.claim_next(worker_id="old-worker")
        assert stale is not None
        stale.fencing_token += 1
        stale.lease_owner = "new-worker"
        db.add(stale)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            service._finish(stale, status="reconciled", worker_id="old-worker")
        db.rollback()
        current = db.get(ProviderInputExposureReconciliationJob, stale.id)

    assert exc_info.value.code == "ATTACHMENT_PROVIDER_RECONCILIATION_FENCED"
    assert current is not None
    assert current.status == "dispatching"
    assert current.lease_owner == "new-worker"
