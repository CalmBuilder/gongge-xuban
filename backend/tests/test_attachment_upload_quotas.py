"""
@Time       : 2026/08/15 00:05
@Author     : zhanglp8181
@File       : test_attachment_upload_quotas.py
@CallChain  : pytest → AttachmentUploadQuotaService → SQLite唯一slot/日额度CAS
@Description: 验证跨Session并发拒绝、双scope原子回滚、TTL回收、日额度和finally式释放。
"""

from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread
import asyncio
import time

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.formparsers import MultiPartException
from starlette.requests import Request

from app.db.models import (
    AttachmentUploadDailyUsage,
    AttachmentUploadQuotaLease,
    AttachmentUploadQuotaReservation,
    utc_now,
)
from app.session.upload_quotas import (
    AttachmentUploadQuotaError,
    AttachmentUploadQuotaHeartbeat,
    AttachmentUploadQuotaPolicy,
    AttachmentUploadQuotaService,
)


def _engine(tmp_path: Path):
    """创建真实文件SQLite，使两个Session模拟两个API进程共享同一数据库。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'upload-quota.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _policy(
    *,
    user_concurrency: int = 1,
    tenant_concurrency: int = 2,
    reservation_bytes: int = 100,
    user_daily_bytes: int = 1_000,
    tenant_daily_bytes: int = 2_000,
    reservation_ttl_seconds: int = 60,
) -> AttachmentUploadQuotaPolicy:
    """构造边界较小、便于机械触发并发和日额度的测试策略。"""

    return AttachmentUploadQuotaPolicy(
        user_concurrency=user_concurrency,
        tenant_concurrency=tenant_concurrency,
        user_daily_bytes=user_daily_bytes,
        tenant_daily_bytes=tenant_daily_bytes,
        reservation_ttl_seconds=reservation_ttl_seconds,
        reservation_bytes=reservation_bytes,
    )


def _request(content_type: str, body: bytes) -> Request:
    """构造只发送一个ASGI body帧的真实Starlette Request，验证multipart parser硬边界。"""

    sent = False

    async def receive() -> dict[str, object]:
        """只返回一次完整测试body，之后稳定返回空终帧。"""

        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/attachments",
            "headers": [(b"content-type", content_type.encode("ascii"))],
        },
        receive,
    )


async def _parse_form(request: Request):
    """等待Starlette的context-manager兼容form包装器并返回解析结果。"""

    return await request.form(max_files=8, max_fields=4, max_part_size=64 * 1024)


def test_cross_session_user_concurrency_is_database_enforced(tmp_path: Path) -> None:
    """一个Session提交占槽后，另一个Session不能为同用户取得相同唯一slot。"""

    engine = _engine(tmp_path)
    with Session(engine) as first_db:
        first = AttachmentUploadQuotaService(first_db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-1",
            policy=_policy(),
        )
        first_db.commit()
        with Session(engine) as second_db:
            with pytest.raises(AttachmentUploadQuotaError) as exc_info:
                AttachmentUploadQuotaService(second_db).acquire(
                    tenant_id="tenant-a",
                    owner_user_id="user-a",
                    binding_id="binding-2",
                    policy=_policy(),
                )
            second_db.rollback()
        assert exc_info.value.code == "ATTACHMENT_UPLOAD_USER_CONCURRENCY_EXCEEDED"
        AttachmentUploadQuotaService(first_db).settle(
            first,
            succeeded=False,
            actual_bytes=0,
        )
        first_db.commit()

    with Session(engine) as verify_db:
        assert verify_db.exec(select(AttachmentUploadQuotaLease)).all() == []
        usages = verify_db.exec(select(AttachmentUploadDailyUsage)).all()
        assert all(item.reserved_bytes == 0 for item in usages)
        assert all(item.consumed_bytes == 0 for item in usages)


def test_failed_second_scope_rolls_back_tenant_slot_and_daily_reservation(tmp_path: Path) -> None:
    """user scope失败时，同次事务已取得的tenant slot和日预留不得残留。"""

    engine = _engine(tmp_path)
    with Session(engine) as db:
        held = AttachmentUploadQuotaService(db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-held",
            policy=_policy(user_concurrency=1, tenant_concurrency=3),
        )
        db.commit()
        with pytest.raises(AttachmentUploadQuotaError):
            AttachmentUploadQuotaService(db).acquire(
                tenant_id="tenant-a",
                owner_user_id="user-a",
                binding_id="binding-rejected",
                policy=_policy(user_concurrency=1, tenant_concurrency=3),
            )
        db.rollback()
        reservations = db.exec(select(AttachmentUploadQuotaReservation)).all()
        leases = db.exec(select(AttachmentUploadQuotaLease)).all()
        assert [item.id for item in reservations] == [held.id]
        assert len(leases) == 2


def test_expired_reservation_is_reaped_before_new_body_reservation(tmp_path: Path) -> None:
    """崩溃遗留active reservation到期后由下一请求CAS回收并复用并发槽。"""

    engine = _engine(tmp_path)
    with Session(engine) as db:
        stale = AttachmentUploadQuotaService(db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-stale",
            policy=_policy(),
        )
        db.commit()
        stale.expires_at = utc_now() - timedelta(seconds=1)
        db.add(stale)
        db.commit()
        stale_id = stale.id

    with Session(engine) as db:
        current = AttachmentUploadQuotaService(db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-current",
            policy=_policy(),
        )
        db.commit()
        old = db.get(AttachmentUploadQuotaReservation, stale_id)
        assert old is not None and old.status == "expired"
        assert current.status == "active"
        assert len(db.exec(select(AttachmentUploadQuotaLease)).all()) == 2


def test_heartbeat_prevents_short_ttl_oversell_and_late_settle_is_fenced(
    tmp_path: Path,
) -> None:
    """活跃心跳跨过原TTL仍占槽；停止后新请求回收，旧请求迟到settle必须被fence。"""

    engine = _engine(tmp_path)
    policy = _policy(
        user_concurrency=1,
        tenant_concurrency=2,
        reservation_ttl_seconds=1,
    )
    with Session(engine) as first_db:
        first = AttachmentUploadQuotaService(first_db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-live",
            policy=policy,
        )
        first_db.commit()
        first_id = first.id
        heartbeat = AttachmentUploadQuotaHeartbeat(
            engine,
            reservation=first,
            ttl_seconds=policy.reservation_ttl_seconds,
        )
    heartbeat.start()
    time.sleep(1.25)
    heartbeat.ensure_healthy()

    barrier = Barrier(2)
    competing_error: list[str] = []

    def compete_after_original_ttl() -> None:
        """在原始TTL之后从第二Session竞争同一用户slot并记录稳定错误码。"""

        barrier.wait()
        with Session(engine) as second_db:
            try:
                AttachmentUploadQuotaService(second_db).acquire(
                    tenant_id="tenant-a",
                    owner_user_id="user-a",
                    binding_id="binding-competing",
                    policy=policy,
                )
            except AttachmentUploadQuotaError as exc:
                second_db.rollback()
                competing_error.append(exc.code)

    competitor = Thread(target=compete_after_original_ttl)
    competitor.start()
    barrier.wait()
    competitor.join(timeout=5)
    assert not competitor.is_alive()
    assert competing_error == ["ATTACHMENT_UPLOAD_USER_CONCURRENCY_EXCEEDED"]

    heartbeat.stop()
    heartbeat.ensure_healthy()
    time.sleep(1.1)
    with Session(engine) as third_db:
        replacement = AttachmentUploadQuotaService(third_db).acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-replacement",
            policy=policy,
        )
        third_db.commit()
        replacement_id = replacement.id
        old = third_db.get(AttachmentUploadQuotaReservation, first_id)
        assert old is not None and old.status == "expired"

    with Session(engine) as late_db:
        stale = late_db.get(AttachmentUploadQuotaReservation, first_id)
        assert stale is not None
        with pytest.raises(
            AttachmentUploadQuotaError,
            match="ATTACHMENT_UPLOAD_RESERVATION_FENCED",
        ):
            AttachmentUploadQuotaService(late_db).settle(
                stale,
                succeeded=True,
                actual_bytes=10,
            )
        late_db.rollback()

    with Session(engine) as cleanup_db:
        current = cleanup_db.get(AttachmentUploadQuotaReservation, replacement_id)
        assert current is not None
        AttachmentUploadQuotaService(cleanup_db).settle(
            current,
            succeeded=False,
            actual_bytes=0,
        )
        cleanup_db.commit()


def test_success_consumes_daily_bytes_failure_refunds_and_limit_is_atomic(tmp_path: Path) -> None:
    """成功量进入日累计，失败只退预留；累计加新预留超限时拒绝且无部分bucket。"""

    engine = _engine(tmp_path)
    with Session(engine) as db:
        service = AttachmentUploadQuotaService(db)
        first = service.acquire(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            binding_id="binding-first",
            policy=_policy(
                reservation_bytes=60,
                user_daily_bytes=100,
                tenant_daily_bytes=100,
            ),
        )
        db.commit()
        service.settle(first, succeeded=True, actual_bytes=60)
        db.commit()

        with pytest.raises(AttachmentUploadQuotaError) as exc_info:
            service.acquire(
                tenant_id="tenant-a",
                owner_user_id="user-a",
                binding_id="binding-over-limit",
                policy=_policy(
                    reservation_bytes=50,
                    user_daily_bytes=100,
                    tenant_daily_bytes=100,
                ),
            )
        db.rollback()
        assert exc_info.value.code == "ATTACHMENT_UPLOAD_TENANT_DAILY_QUOTA_EXCEEDED"
        usages = db.exec(select(AttachmentUploadDailyUsage)).all()
        assert all(item.reserved_bytes == 0 for item in usages)
        assert all(item.consumed_bytes == 60 for item in usages)

        failed = service.acquire(
            tenant_id="tenant-b",
            owner_user_id="user-b",
            binding_id="binding-failed",
            policy=_policy(reservation_bytes=40),
        )
        db.commit()
        service.settle(failed, succeeded=False, actual_bytes=140)
        service.settle(failed, succeeded=False, actual_bytes=140)
        db.commit()
        failed_usage = db.exec(
            select(AttachmentUploadDailyUsage).where(
                AttachmentUploadDailyUsage.tenant_id == "tenant-b"
            )
        ).all()
        assert all(item.reserved_bytes == 0 for item in failed_usage)
        assert all(item.consumed_bytes == 0 for item in failed_usage)


def test_multipart_missing_boundary_and_field_bloat_are_rejected_before_business_loop() -> None:
    """路由采用的parser参数必须拒绝缺boundary和超过4个非文件字段，避免业务计数前膨胀。"""

    malformed = _request("multipart/form-data", b"ignored")
    with pytest.raises(MultiPartException, match="boundary"):
        asyncio.run(
            _parse_form(malformed)
        )

    boundary = "quota-boundary"
    body = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="field{index}"\r\n\r\n'
            f"value{index}\r\n"
        ).encode("ascii")
        for index in range(5)
    ) + f"--{boundary}--\r\n".encode("ascii")
    bloated = _request(f"multipart/form-data; boundary={boundary}", body)
    with pytest.raises(MultiPartException, match="fields"):
        asyncio.run(
            _parse_form(bloated)
        )

    oversized_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="oversized"\r\n\r\n'
    ).encode("ascii") + b"x" * (64 * 1024 + 1) + f"\r\n--{boundary}--\r\n".encode("ascii")
    oversized = _request(f"multipart/form-data; boundary={boundary}", oversized_body)
    with pytest.raises(MultiPartException, match="(?i)part exceeded"):
        asyncio.run(_parse_form(oversized))


def test_multipart_more_than_eight_files_is_rejected_by_parser() -> None:
    """九个文件part在形成业务files列表前由max_files硬拒绝。"""

    boundary = "quota-files"
    body = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{index}.txt"\r\n'
            "Content-Type: text/plain\r\n\r\nx\r\n"
        ).encode("ascii")
        for index in range(9)
    ) + f"--{boundary}--\r\n".encode("ascii")
    request = _request(f"multipart/form-data; boundary={boundary}", body)
    with pytest.raises(MultiPartException, match="files"):
        asyncio.run(_parse_form(request))
