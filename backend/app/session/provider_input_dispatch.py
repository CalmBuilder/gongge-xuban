"""
@Time       : 2026/08/13 20:34
@Author     : zhanglp8181
@File       : provider_input_dispatch.py
@CallChain  : AgentLoop/Dynamic/SOP模型动作 → ProviderInputDispatchGateway → LLM provider
@Description: 统一附件切片外发授权线性化、网络unknown、迟到结果丢弃与审计归并。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import exists, update
from sqlmodel import Session, select

from app.db.models import (
    InputResourceExtraction,
    InputResourceSnapshot,
    ManagedInputResource,
    ModelConfig,
    ProviderInputDispatchGroup,
    ProviderInputDispatchReceipt,
    ScannerEvidence,
    SopInstance,
    SopOperation,
    TurnInputReadReceipt,
    TurnInputSnapshot,
    utc_now,
)
from app.session.input_bindings import InputBindingError
from app.session.input_extraction import InputExtractionError, sanitize_image_bytes_for_provider
from app.session.managed_resources import InputResourceAccessDenied, ManagedInputResourceService
from app.session.provider_input_reconciliation import ProviderInputReconciliationService


TURN_EGRESS_POLICY_CHECKSUM = "inline-model-default-v1"


class ProviderInputDispatchGateway:
    """将一次模型动作的附件披露事实与本地读取事实分开持久化。"""

    def __init__(
        self,
        db: Session,
        *,
        resource_service: ManagedInputResourceService | None = None,
    ) -> None:
        """绑定调用方事务，网络调用由上层在authorize后执行。"""

        self.db = db
        self.resource_service = resource_service or ManagedInputResourceService(db)

    def prepare_turn_group(
        self,
        *,
        tenant_id: str,
        turn_id: str,
        read_receipt_ids: list[str],
        egress_policy_checksum: str,
    ) -> ProviderInputDispatchGroup | None:
        """按有序读取回执幂等建立一请求一Group及逐资源Receipt。"""

        if not read_receipt_ids:
            return None
        causation_id = f"turn:{turn_id}"
        existing = self.db.exec(
            select(ProviderInputDispatchGroup).where(
                ProviderInputDispatchGroup.tenant_id == tenant_id,
                ProviderInputDispatchGroup.causation_id == causation_id,
                ProviderInputDispatchGroup.attempt_no == 1,
            )
        ).first()
        if existing is not None:
            return existing
        resolved_reads: list[tuple[TurnInputReadReceipt, TurnInputSnapshot]] = []
        for read_id in read_receipt_ids:
            read = self.db.get(TurnInputReadReceipt, read_id)
            snapshot = self.db.get(TurnInputSnapshot, read.snapshot_id if read else "")
            if (
                read is None
                or snapshot is None
                or read.tenant_id != tenant_id
                or snapshot.tenant_id != tenant_id
                or read.turn_id != turn_id
                or snapshot.turn_id != turn_id
                or read.snapshot_id != snapshot.id
                or read.status != "succeeded"
            ):
                raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
            resolved_reads.append((read, snapshot))
        group = ProviderInputDispatchGroup(
            tenant_id=tenant_id,
            consumer_kind="turn",
            causation_id=causation_id,
        )
        self.db.add(group)
        self.db.flush()
        receipt_ids: list[str] = []
        deadline = utc_now() + timedelta(minutes=15)
        for read, snapshot in resolved_reads:
            receipt = ProviderInputDispatchReceipt(
                tenant_id=tenant_id,
                dispatch_group_id=group.id,
                consumer_kind="turn",
                turn_id=turn_id,
                resource_id=snapshot.resource_id,
                extraction_id=snapshot.extraction_id,
                slice_checksum=read.slice_checksum,
                expected_acl_revision=snapshot.resource_acl_revision_at_snapshot,
                egress_policy_checksum=egress_policy_checksum,
                dispatch_token=hashlib.sha256(
                    f"{group.id}:{snapshot.id}:{secrets.token_hex(16)}".encode()
                ).hexdigest(),
                deadline_at=deadline,
                causation_id=read.id,
            )
            self.db.add(receipt)
            self.db.flush()
            read.provider_dispatch_group_id = group.id
            self.db.add(read)
            receipt_ids.append(receipt.id)
        group.ordered_receipt_ids_json = receipt_ids
        self.db.add(group)
        self.db.flush()
        return group

    def prepare_execution_group(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        causation_id: str,
        slices: list[tuple[str, str]],
        egress_policy_checksum: str,
    ) -> ProviderInputDispatchGroup | None:
        """按Execution快照和切片摘要建立Dynamic模型动作的逐资源披露事实。"""

        if not slices:
            return None
        stable_causation = f"execution:{execution_id}:{causation_id}"
        existing = self.db.exec(
            select(ProviderInputDispatchGroup).where(
                ProviderInputDispatchGroup.tenant_id == tenant_id,
                ProviderInputDispatchGroup.causation_id == stable_causation,
                ProviderInputDispatchGroup.attempt_no == 1,
            )
        ).first()
        if existing is not None:
            return existing
        group = ProviderInputDispatchGroup(
            tenant_id=tenant_id,
            consumer_kind="dynamic_task",
            causation_id=stable_causation,
        )
        self.db.add(group)
        self.db.flush()
        deadline = utc_now() + timedelta(minutes=15)
        receipt_ids: list[str] = []
        for snapshot_id, slice_checksum in slices:
            snapshot = self.db.get(InputResourceSnapshot, snapshot_id)
            if (
                snapshot is None
                or snapshot.tenant_id != tenant_id
                or snapshot.execution_id != execution_id
                or not snapshot.extraction_id
            ):
                raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
            receipt = ProviderInputDispatchReceipt(
                tenant_id=tenant_id,
                dispatch_group_id=group.id,
                consumer_kind="dynamic_task",
                execution_id=execution_id,
                resource_id=snapshot.source_resource_id,
                extraction_id=snapshot.extraction_id,
                slice_checksum=slice_checksum,
                expected_acl_revision=snapshot.resource_acl_revision_at_snapshot,
                egress_policy_checksum=egress_policy_checksum,
                dispatch_token=hashlib.sha256(
                    f"{group.id}:{snapshot.id}:{secrets.token_hex(16)}".encode()
                ).hexdigest(),
                deadline_at=deadline,
                causation_id=causation_id,
            )
            self.db.add(receipt)
            self.db.flush()
            receipt_ids.append(receipt.id)
        group.ordered_receipt_ids_json = receipt_ids
        self.db.add(group)
        self.db.flush()
        return group

    def authorize(self, group: ProviderInputDispatchGroup, *, worker_id: str) -> None:
        """模型调用紧前以资源ACL revision与Receipt状态CAS形成外发授权线性化点。"""

        if group.status == "settled":
            return
        if group.status != "prepared":
            raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
        receipts = [
            self.db.get(ProviderInputDispatchReceipt, item)
            for item in group.ordered_receipt_ids_json
        ]
        if not receipts or any(item is None for item in receipts):
            raise InputBindingError("ATTACHMENT_DISPATCH_INVALID")
        resources: list[ManagedInputResource] = []
        for receipt in receipts:
            resource = self.db.get(ManagedInputResource, receipt.resource_id)
            if (
                receipt.tenant_id != group.tenant_id
                or receipt.dispatch_group_id != group.id
                or resource is None
                or resource.tenant_id != group.tenant_id
                or resource.access_status != "active"
                or resource.revoked_at is not None
                or resource.acl_revision != receipt.expected_acl_revision
                or resource.destruction_status not in {"retained", "held"}
            ):
                raise InputBindingError("ATTACHMENT_COUNTERMANDED")
            self._assert_egress_policy(receipt)
            self._assert_receipt_evidence(receipt, resource)
            resources.append(resource)
        with self.db.begin_nested():
            for receipt, resource in zip(receipts, resources, strict=True):
                result = self.db.exec(
                    update(ProviderInputDispatchReceipt)
                    .where(
                        ProviderInputDispatchReceipt.id == receipt.id,
                        ProviderInputDispatchReceipt.tenant_id == group.tenant_id,
                        ProviderInputDispatchReceipt.dispatch_group_id == group.id,
                        ProviderInputDispatchReceipt.status == "prepared",
                        ProviderInputDispatchReceipt.expected_acl_revision
                        == resource.acl_revision,
                        exists(
                            select(ManagedInputResource.id).where(
                                ManagedInputResource.id == receipt.resource_id,
                                ManagedInputResource.tenant_id == group.tenant_id,
                                ManagedInputResource.access_status == "active",
                                ManagedInputResource.revoked_at.is_(None),
                                ManagedInputResource.destruction_status.in_(
                                    ("retained", "held")
                                ),
                                ManagedInputResource.acl_revision
                                == receipt.expected_acl_revision,
                            )
                        ),
                    )
                    .values(
                        status="dispatching",
                        lease_owner=worker_id,
                        fencing_token=ProviderInputDispatchReceipt.fencing_token + 1,
                        dispatch_started_at=utc_now(),
                    )
                )
                if result.rowcount != 1:
                    raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
            group.status = "dispatching"
            self.db.add(group)
        self.db.flush()

    def expire_dispatching_to_unknown(self) -> int:
        """把越过授权线性化点但超过deadline的外发统一收敛为可能披露。"""

        now = utc_now()
        groups = self.db.exec(
            select(ProviderInputDispatchGroup).where(
                ProviderInputDispatchGroup.status == "dispatching",
                exists(
                    select(ProviderInputDispatchReceipt.id).where(
                        ProviderInputDispatchReceipt.dispatch_group_id
                        == ProviderInputDispatchGroup.id,
                        ProviderInputDispatchReceipt.tenant_id
                        == ProviderInputDispatchGroup.tenant_id,
                        ProviderInputDispatchReceipt.status == "dispatching",
                        ProviderInputDispatchReceipt.deadline_at <= now,
                    )
                ),
            )
        ).all()
        for group in groups:
            self.mark_unknown(group)
        return len(groups)

    def _assert_receipt_evidence(
        self,
        receipt: ProviderInputDispatchReceipt,
        resource: ManagedInputResource,
    ) -> None:
        """在网络授权前重算Extraction、切片因果和扫描保证，拒绝仅凭Receipt自报。"""

        extraction = self.db.get(InputResourceExtraction, receipt.extraction_id)
        if (
            extraction is None
            or extraction.tenant_id != receipt.tenant_id
            or extraction.resource_id != resource.id
            or extraction.resource_version != resource.version
            or extraction.content_checksum != resource.content_checksum
        ):
            raise InputBindingError("ATTACHMENT_EXTRACTION_DRIFT")
        if receipt.consumer_kind == "turn":
            read = self.db.get(TurnInputReadReceipt, receipt.causation_id)
            snapshot = self.db.get(TurnInputSnapshot, read.snapshot_id if read else "")
            if (
                read is None
                or snapshot is None
                or read.tenant_id != receipt.tenant_id
                or snapshot.tenant_id != receipt.tenant_id
                or read.slice_checksum != receipt.slice_checksum
                or snapshot.extraction_id != receipt.extraction_id
                or snapshot.resource_id != resource.id
            ):
                raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
        elif receipt.consumer_kind == "dynamic_task":
            snapshot = self.db.exec(
                select(InputResourceSnapshot).where(
                    InputResourceSnapshot.tenant_id == receipt.tenant_id,
                    InputResourceSnapshot.execution_id == receipt.execution_id,
                    InputResourceSnapshot.source_resource_id == resource.id,
                    InputResourceSnapshot.extraction_id == receipt.extraction_id,
                )
            ).first()
            if snapshot is None:
                raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
            sanitized_image_checksum: str | None = None
            if snapshot.mime_type in {"image/jpeg", "image/png", "image/webp"}:
                instance = self.db.get(SopInstance, receipt.execution_id)
                if instance is None:
                    raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
                try:
                    _, image_data = self.resource_service.resolve_snapshot(
                        snapshot,
                        instance=instance,
                    )
                    sanitized_image_checksum = hashlib.sha256(
                        sanitize_image_bytes_for_provider(image_data)
                    ).hexdigest()
                except (InputExtractionError, InputResourceAccessDenied) as exc:
                    raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID") from exc
            operations = self.db.exec(
                select(SopOperation).where(
                    SopOperation.tenant_id == receipt.tenant_id,
                    SopOperation.instance_id == receipt.execution_id,
                    SopOperation.operation_name == "input.read",
                    SopOperation.effect_kind == "read",
                    SopOperation.status == "succeeded",
                )
            ).all()
            from app.dynamic_tasks.capability_catalog import capability_checksum

            persisted_read = False
            for operation in operations:
                data = (operation.result_json or {}).get("data")
                if not isinstance(data, dict) or data.get("snapshot_id") != snapshot.id:
                    continue
                elements = data.get("elements")
                if not isinstance(elements, list) or any(
                    not isinstance(item, dict) for item in elements
                ):
                    continue
                persisted_checksum = str(data.get("slice_checksum") or "")
                disclosure_base = {
                    "snapshot_id": snapshot.id,
                    "element_ids": [item.get("element_id") for item in elements],
                    "element_checksums": [item.get("content_checksum") for item in elements],
                }
                disclosure_checksums = {
                    capability_checksum(
                        {**disclosure_base, "native_content_checksum": None}
                    )
                }
                if snapshot.mime_type in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                }:
                    disclosure_checksums.add(
                        capability_checksum(
                            {
                                **disclosure_base,
                                "native_content_checksum": sanitized_image_checksum,
                            }
                        )
                    )
                elif snapshot.mime_type == "application/pdf":
                    disclosure_checksums.add(
                        capability_checksum(
                            {
                                **disclosure_base,
                                "native_content_checksum": resource.content_checksum,
                            }
                        )
                    )
                if (
                    capability_checksum(elements) == persisted_checksum
                    and receipt.slice_checksum in disclosure_checksums
                ):
                    persisted_read = True
                    break
            if not persisted_read:
                raise InputBindingError("ATTACHMENT_DISPATCH_CAUSATION_INVALID")
        scan = self.db.exec(
            select(ScannerEvidence)
            .where(
                ScannerEvidence.tenant_id == receipt.tenant_id,
                ScannerEvidence.resource_id == resource.id,
                ScannerEvidence.resource_version == resource.version,
                ScannerEvidence.verdict == "accepted",
            )
            .order_by(ScannerEvidence.scanned_at.desc())
        ).first()
        if (
            scan is None
            or scan.assurance_level not in {"format_verified", "malware_scanned"}
            or resource.security_status != scan.assurance_level
        ):
            raise InputBindingError("ATTACHMENT_SCAN_REQUIRED")
        definition_age = (scan.scanned_at - scan.definition_published_at).total_seconds()
        if definition_age < 0 or definition_age > scan.max_age_at_scan_seconds:
            raise InputBindingError("ATTACHMENT_SCAN_STALE")

    def _assert_egress_policy(self, receipt: ProviderInputDispatchReceipt) -> None:
        """把Receipt策略摘要绑定到内置Turn策略或Execution冻结模型，不接受任意SHA外观。"""

        if (
            receipt.consumer_kind == "turn"
            and receipt.egress_policy_checksum == TURN_EGRESS_POLICY_CHECKSUM
        ):
            return
        if receipt.consumer_kind != "dynamic_task" or not receipt.execution_id:
            raise InputBindingError("ATTACHMENT_EGRESS_POLICY_DENIED")
        instance = self.db.get(SopInstance, receipt.execution_id)
        frozen_model = (instance.capability_snapshot_json or {}).get("model") if instance else None
        model_config_id = (
            str(frozen_model.get("model_config_id") or "")
            if isinstance(frozen_model, dict)
            else ""
        )
        model = self.db.get(ModelConfig, model_config_id)
        if (
            instance is None
            or instance.tenant_id != receipt.tenant_id
            or model is None
            or model.tenant_id != receipt.tenant_id
            or not model.enabled
            or model.preflight_status != "ready"
        ):
            raise InputBindingError("ATTACHMENT_EGRESS_POLICY_DENIED")
        from app.dynamic_tasks.capability_catalog import capability_checksum

        expected = capability_checksum(
            {
                "provider": model.provider,
                "model": model.model,
                "mode": "reviewed_elements",
            }
        )
        if receipt.egress_policy_checksum != expected:
            raise InputBindingError("ATTACHMENT_EGRESS_POLICY_DENIED")

    def settle_delivered(self, group: ProviderInputDispatchGroup) -> None:
        """以Receipt和资源ACL条件CAS接纳结果，拒绝超时或撤权后的迟到提交。"""

        if group.status != "dispatching":
            raise InputBindingError("ATTACHMENT_DISPATCH_STATE_INVALID")
        receipts = [
            self.db.get(ProviderInputDispatchReceipt, receipt_id)
            for receipt_id in group.ordered_receipt_ids_json
        ]
        if not receipts or any(
            receipt is None
            or receipt.tenant_id != group.tenant_id
            or receipt.dispatch_group_id != group.id
            for receipt in receipts
        ):
            raise InputBindingError("ATTACHMENT_DISPATCH_STATE_INVALID")
        settled_at = utc_now()
        with self.db.begin_nested():
            for receipt in receipts:
                result = self.db.exec(
                    update(ProviderInputDispatchReceipt)
                    .where(
                        ProviderInputDispatchReceipt.id == receipt.id,
                        ProviderInputDispatchReceipt.tenant_id == group.tenant_id,
                        ProviderInputDispatchReceipt.dispatch_group_id == group.id,
                        ProviderInputDispatchReceipt.status == "dispatching",
                        ProviderInputDispatchReceipt.deadline_at > settled_at,
                        exists(
                            select(ManagedInputResource.id).where(
                                ManagedInputResource.id == receipt.resource_id,
                                ManagedInputResource.tenant_id == group.tenant_id,
                                ManagedInputResource.access_status == "active",
                                ManagedInputResource.revoked_at.is_(None),
                                ManagedInputResource.destruction_status.in_(
                                    ("retained", "held")
                                ),
                                ManagedInputResource.acl_revision
                                == receipt.expected_acl_revision,
                            )
                        ),
                    )
                    .values(
                        status="settled",
                        settled_at=settled_at,
                        lease_owner=None,
                        fencing_token=ProviderInputDispatchReceipt.fencing_token + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
            group_result = self.db.exec(
                update(ProviderInputDispatchGroup)
                .where(
                    ProviderInputDispatchGroup.id == group.id,
                    ProviderInputDispatchGroup.tenant_id == group.tenant_id,
                    ProviderInputDispatchGroup.status == "dispatching",
                )
                .values(status="settled", settled_at=settled_at)
                .execution_options(synchronize_session=False)
            )
            if group_result.rowcount != 1:
                raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
        self.db.flush()
        self.db.refresh(group)

    def mark_unknown(self, group: ProviderInputDispatchGroup) -> None:
        """以条件CAS记录可能披露，禁止与成功结算互相覆盖。"""

        if group.status != "dispatching":
            raise InputBindingError("ATTACHMENT_DISPATCH_STATE_INVALID")
        receipts = [
            self.db.get(ProviderInputDispatchReceipt, receipt_id)
            for receipt_id in group.ordered_receipt_ids_json
        ]
        if not receipts or any(
            receipt is None
            or receipt.tenant_id != group.tenant_id
            or receipt.dispatch_group_id != group.id
            for receipt in receipts
        ):
            raise InputBindingError("ATTACHMENT_DISPATCH_STATE_INVALID")
        settled_at = utc_now()
        with self.db.begin_nested():
            for receipt in receipts:
                result = self.db.exec(
                    update(ProviderInputDispatchReceipt)
                    .where(
                        ProviderInputDispatchReceipt.id == receipt.id,
                        ProviderInputDispatchReceipt.tenant_id == group.tenant_id,
                        ProviderInputDispatchReceipt.dispatch_group_id == group.id,
                        ProviderInputDispatchReceipt.status == "dispatching",
                    )
                    .values(
                        status="unknown",
                        settled_at=settled_at,
                        lease_owner=None,
                        fencing_token=ProviderInputDispatchReceipt.fencing_token + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
            group_result = self.db.exec(
                update(ProviderInputDispatchGroup)
                .where(
                    ProviderInputDispatchGroup.id == group.id,
                    ProviderInputDispatchGroup.tenant_id == group.tenant_id,
                    ProviderInputDispatchGroup.status == "dispatching",
                )
                .values(status="unknown", settled_at=settled_at)
                .execution_options(synchronize_session=False)
            )
            if group_result.rowcount != 1:
                raise InputBindingError("ATTACHMENT_DISPATCH_FENCED")
            self.db.expire_all()
            ProviderInputReconciliationService(self.db).schedule_unknown_group(group)
        self.db.flush()
        self.db.refresh(group)
