"""
@Time       : 2026/08/14 23:55
@Author     : zhanglp8181
@File       : upload_quotas.py
@CallChain  : chat附件上传 → body前quota reservation → multipart读取 → finally settle
@Description: 以数据库唯一槽和日用量CAS提供跨进程tenant/user上传并发、TTL和字节配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Lock, Thread
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    AttachmentUploadDailyUsage,
    AttachmentUploadQuotaLease,
    AttachmentUploadQuotaReservation,
    utc_now,
)


class AttachmentUploadQuotaError(RuntimeError):
    """表示上传身份冲突、并发槽耗尽或日字节额度不足。"""

    def __init__(self, code: str) -> None:
        """保存可安全返回客户端的稳定错误码，不暴露其他用户用量。"""

        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AttachmentUploadQuotaPolicy:
    """冻结一次上传使用的用户/租户并发、日额度和reservation TTL。"""

    user_concurrency: int
    tenant_concurrency: int
    user_daily_bytes: int
    tenant_daily_bytes: int
    reservation_ttl_seconds: int
    reservation_bytes: int

    def validate(self) -> None:
        """拒绝零值或日额度小于单次预留量的不可执行部署配置。"""

        values = (
            self.user_concurrency,
            self.tenant_concurrency,
            self.user_daily_bytes,
            self.tenant_daily_bytes,
            self.reservation_ttl_seconds,
            self.reservation_bytes,
        )
        if any(value <= 0 for value in values):
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_QUOTA_NOT_CONFIGURED")
        if (
            self.user_daily_bytes < self.reservation_bytes
            or self.tenant_daily_bytes < self.reservation_bytes
        ):
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_DAILY_QUOTA_TOO_SMALL")


class AttachmentUploadQuotaService:
    """通过数据库唯一约束和条件UPDATE提供SQLite/MySQL一致的上传配额。"""

    def __init__(self, db: Session) -> None:
        """绑定调用方事务；acquire和settle都要求调用方随后提交。"""

        self.db = db

    def acquire(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        binding_id: str,
        policy: AttachmentUploadQuotaPolicy,
    ) -> AttachmentUploadQuotaReservation:
        """在读取body前原子取得tenant/user槽位及两级日字节预留。"""

        policy.validate()
        self.reap_expired(tenant_id=tenant_id)
        existing = self.db.exec(
            select(AttachmentUploadQuotaReservation).where(
                AttachmentUploadQuotaReservation.tenant_id == tenant_id,
                AttachmentUploadQuotaReservation.binding_id == binding_id,
            )
        ).first()
        if existing is not None:
            if (
                existing.owner_user_id != owner_user_id
                or existing.reserved_bytes != policy.reservation_bytes
                or existing.status != "active"
            ):
                raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_RESERVATION_CONFLICT")
            return existing

        now = utc_now()
        expires_at = now + timedelta(seconds=policy.reservation_ttl_seconds)
        reservation = AttachmentUploadQuotaReservation(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            binding_id=binding_id,
            reserved_bytes=policy.reservation_bytes,
            expires_at=expires_at,
        )
        with self.db.begin_nested():
            self.db.add(reservation)
            self.db.flush()
            self._acquire_scope(
                reservation=reservation,
                scope_type="tenant",
                scope_ref=tenant_id,
                concurrency=policy.tenant_concurrency,
                daily_limit=policy.tenant_daily_bytes,
            )
            self._acquire_scope(
                reservation=reservation,
                scope_type="user",
                scope_ref=owner_user_id,
                concurrency=policy.user_concurrency,
                daily_limit=policy.user_daily_bytes,
            )
        return reservation

    def settle(
        self,
        reservation: AttachmentUploadQuotaReservation,
        *,
        succeeded: bool,
        actual_bytes: int,
    ) -> None:
        """幂等释放并发槽和预留；仅成功请求计入UTC日用量。"""

        if actual_bytes < 0 or (succeeded and actual_bytes > reservation.reserved_bytes):
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_ACTUAL_BYTES_INVALID")
        target_status = "completed" if succeeded else "released"
        result = self.db.exec(
            update(AttachmentUploadQuotaReservation)
            .where(
                AttachmentUploadQuotaReservation.id == reservation.id,
                AttachmentUploadQuotaReservation.tenant_id == reservation.tenant_id,
                AttachmentUploadQuotaReservation.fencing_token == reservation.fencing_token,
                AttachmentUploadQuotaReservation.status == "active",
                AttachmentUploadQuotaReservation.expires_at > utc_now(),
            )
            .values(
                status=target_status,
                actual_bytes=actual_bytes if succeeded else 0,
                settled_at=utc_now(),
            )
        )
        if result.rowcount == 0:
            current = self.db.get(AttachmentUploadQuotaReservation, reservation.id)
            if current is not None and current.status in {"completed", "released"}:
                return
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_RESERVATION_FENCED")
        self._release_accounting(
            reservation,
            consumed_bytes=actual_bytes if succeeded else 0,
        )

    def reap_expired(self, *, tenant_id: str) -> int:
        """CAS回收已过期active reservation，重复worker不会重复扣减reserved字节。"""

        now = utc_now()
        expired = self.db.exec(
            select(AttachmentUploadQuotaReservation).where(
                AttachmentUploadQuotaReservation.tenant_id == tenant_id,
                AttachmentUploadQuotaReservation.status == "active",
                AttachmentUploadQuotaReservation.expires_at <= now,
            )
        ).all()
        reaped = 0
        for reservation in expired:
            result = self.db.exec(
                update(AttachmentUploadQuotaReservation)
                .where(
                    AttachmentUploadQuotaReservation.id == reservation.id,
                    AttachmentUploadQuotaReservation.fencing_token
                    == reservation.fencing_token,
                    AttachmentUploadQuotaReservation.status == "active",
                    AttachmentUploadQuotaReservation.expires_at <= now,
                )
                .values(status="expired", settled_at=now)
            )
            if result.rowcount == 1:
                self._release_accounting(reservation, consumed_bytes=0)
                reaped += 1
        return reaped

    def renew(
        self,
        *,
        tenant_id: str,
        reservation_id: str,
        fencing_token: str,
        ttl_seconds: int,
    ) -> None:
        """以token和未过期条件CAS续租reservation及其两级slot，过期后禁止复活。"""

        if ttl_seconds <= 0:
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_QUOTA_NOT_CONFIGURED")
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        result = self.db.exec(
            update(AttachmentUploadQuotaReservation)
            .where(
                AttachmentUploadQuotaReservation.id == reservation_id,
                AttachmentUploadQuotaReservation.tenant_id == tenant_id,
                AttachmentUploadQuotaReservation.fencing_token == fencing_token,
                AttachmentUploadQuotaReservation.status == "active",
                AttachmentUploadQuotaReservation.expires_at > now,
            )
            .values(expires_at=expires_at)
        )
        if result.rowcount != 1:
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_RESERVATION_FENCED")
        leases = self.db.exec(
            update(AttachmentUploadQuotaLease)
            .where(
                AttachmentUploadQuotaLease.tenant_id == tenant_id,
                AttachmentUploadQuotaLease.reservation_id == reservation_id,
            )
            .values(expires_at=expires_at)
        )
        if leases.rowcount != 2:
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_RESERVATION_FENCED")

    def _acquire_scope(
        self,
        *,
        reservation: AttachmentUploadQuotaReservation,
        scope_type: str,
        scope_ref: str,
        concurrency: int,
        daily_limit: int,
    ) -> None:
        """先竞争唯一并发slot，再以条件UPDATE预留当日字节，任一失败回滚本次acquire。"""

        acquired = False
        for slot_number in range(concurrency):
            lease = AttachmentUploadQuotaLease(
                tenant_id=reservation.tenant_id,
                reservation_id=reservation.id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                slot_number=slot_number,
                expires_at=reservation.expires_at,
            )
            try:
                with self.db.begin_nested():
                    self.db.add(lease)
                    self.db.flush()
                acquired = True
                break
            except IntegrityError:
                continue
        if not acquired:
            raise AttachmentUploadQuotaError(
                f"ATTACHMENT_UPLOAD_{scope_type.upper()}_CONCURRENCY_EXCEEDED"
            )

        day_key = reservation.created_at.date().isoformat()
        usage = self._ensure_daily_usage(
            tenant_id=reservation.tenant_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            day_key=day_key,
        )
        result = self.db.exec(
            update(AttachmentUploadDailyUsage)
            .where(
                AttachmentUploadDailyUsage.id == usage.id,
                AttachmentUploadDailyUsage.consumed_bytes
                + AttachmentUploadDailyUsage.reserved_bytes
                + reservation.reserved_bytes
                <= daily_limit,
            )
            .values(
                reserved_bytes=(
                    AttachmentUploadDailyUsage.reserved_bytes + reservation.reserved_bytes
                ),
                revision=AttachmentUploadDailyUsage.revision + 1,
                updated_at=utc_now(),
            )
        )
        if result.rowcount != 1:
            raise AttachmentUploadQuotaError(
                f"ATTACHMENT_UPLOAD_{scope_type.upper()}_DAILY_QUOTA_EXCEEDED"
            )

    def _ensure_daily_usage(
        self,
        *,
        tenant_id: str,
        scope_type: str,
        scope_ref: str,
        day_key: str,
    ) -> AttachmentUploadDailyUsage:
        """幂等建立每日bucket；并发插入由唯一约束收敛后重新读取。"""

        existing = self.db.exec(
            select(AttachmentUploadDailyUsage).where(
                AttachmentUploadDailyUsage.tenant_id == tenant_id,
                AttachmentUploadDailyUsage.scope_type == scope_type,
                AttachmentUploadDailyUsage.scope_ref == scope_ref,
                AttachmentUploadDailyUsage.day_key == day_key,
            )
        ).first()
        if existing is not None:
            return existing
        candidate = AttachmentUploadDailyUsage(
            tenant_id=tenant_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            day_key=day_key,
        )
        try:
            with self.db.begin_nested():
                self.db.add(candidate)
                self.db.flush()
            return candidate
        except IntegrityError:
            existing = self.db.exec(
                select(AttachmentUploadDailyUsage).where(
                    AttachmentUploadDailyUsage.tenant_id == tenant_id,
                    AttachmentUploadDailyUsage.scope_type == scope_type,
                    AttachmentUploadDailyUsage.scope_ref == scope_ref,
                    AttachmentUploadDailyUsage.day_key == day_key,
                )
            ).one()
            return existing

    def _release_accounting(
        self,
        reservation: AttachmentUploadQuotaReservation,
        *,
        consumed_bytes: int,
    ) -> None:
        """释放两级日预留与唯一并发slot，并按成功字节增加日累计。"""

        leases = self.db.exec(
            select(AttachmentUploadQuotaLease).where(
                AttachmentUploadQuotaLease.tenant_id == reservation.tenant_id,
                AttachmentUploadQuotaLease.reservation_id == reservation.id,
            )
        ).all()
        day_key = reservation.created_at.date().isoformat()
        for lease in leases:
            self.db.exec(
                update(AttachmentUploadDailyUsage)
                .where(
                    AttachmentUploadDailyUsage.tenant_id == reservation.tenant_id,
                    AttachmentUploadDailyUsage.scope_type == lease.scope_type,
                    AttachmentUploadDailyUsage.scope_ref == lease.scope_ref,
                    AttachmentUploadDailyUsage.day_key == day_key,
                    AttachmentUploadDailyUsage.reserved_bytes >= reservation.reserved_bytes,
                )
                .values(
                    reserved_bytes=(
                        AttachmentUploadDailyUsage.reserved_bytes - reservation.reserved_bytes
                    ),
                    consumed_bytes=(AttachmentUploadDailyUsage.consumed_bytes + consumed_bytes),
                    revision=AttachmentUploadDailyUsage.revision + 1,
                    updated_at=utc_now(),
                )
            )
        self.db.exec(
            delete(AttachmentUploadQuotaLease).where(
                AttachmentUploadQuotaLease.tenant_id == reservation.tenant_id,
                AttachmentUploadQuotaLease.reservation_id == reservation.id,
            )
        )


def quota_policy_from_settings(settings: object) -> AttachmentUploadQuotaPolicy:
    """从Settings或测试替身构造一次最大请求字节的可执行reservation策略。"""

    return AttachmentUploadQuotaPolicy(
        user_concurrency=int(getattr(settings, "attachment_upload_user_concurrency", 0)),
        tenant_concurrency=int(getattr(settings, "attachment_upload_tenant_concurrency", 0)),
        user_daily_bytes=int(getattr(settings, "attachment_upload_user_daily_bytes", 0)),
        tenant_daily_bytes=int(getattr(settings, "attachment_upload_tenant_daily_bytes", 0)),
        reservation_ttl_seconds=int(
            getattr(settings, "attachment_upload_reservation_ttl_seconds", 0)
        ),
        reservation_bytes=int(getattr(settings, "attachment_max_request_bytes", 0)),
    )


class AttachmentUploadQuotaHeartbeat:
    """在multipart接收和同步长解析期间使用独立Session周期续租上传reservation。"""

    def __init__(
        self,
        bind: Any,
        *,
        reservation: AttachmentUploadQuotaReservation,
        ttl_seconds: int,
    ) -> None:
        """保存数据库bind和不可变fencing身份；后台线程不复用请求Session。"""

        self._bind = bind
        self._tenant_id = reservation.tenant_id
        self._reservation_id = reservation.id
        self._fencing_token = reservation.fencing_token
        self._ttl_seconds = ttl_seconds
        self._interval_seconds = max(0.1, min(float(ttl_seconds) / 3, 10.0))
        self._stop_event = Event()
        self._error_lock = Lock()
        self._error: Exception | None = None
        self._thread = Thread(
            target=self._run,
            name=f"attachment-quota-heartbeat-{reservation.id}",
            daemon=True,
        )

    def start(self) -> None:
        """启动单个daemon续租线程。"""

        self._thread.start()

    def stop(self) -> None:
        """立即唤醒并等待续租线程退出，可重复调用。"""

        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._interval_seconds + 1.0))

    def ensure_healthy(self) -> None:
        """将后台数据库或fencing故障提升为请求失败，禁止带失效租约发布成功。"""

        with self._error_lock:
            error = self._error
        if error is not None:
            raise AttachmentUploadQuotaError("ATTACHMENT_UPLOAD_HEARTBEAT_FAILED") from error

    def _run(self) -> None:
        """按TTL三分之一周期用独立事务续租，首个错误后停止并保留原因。"""

        while not self._stop_event.wait(self._interval_seconds):
            try:
                with Session(self._bind) as db:
                    AttachmentUploadQuotaService(db).renew(
                        tenant_id=self._tenant_id,
                        reservation_id=self._reservation_id,
                        fencing_token=self._fencing_token,
                        ttl_seconds=self._ttl_seconds,
                    )
                    db.commit()
            except Exception as exc:
                with self._error_lock:
                    self._error = exc
                return
