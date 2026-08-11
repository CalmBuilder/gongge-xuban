"""
@Time       : 2026/08/12 01:45
@Author     : zhanglp8181
@File       : test_general_skill_s1_import_service.py
@CallChain  : pytest → GeneralSkillImportService → SQLite/content-addressed object store
@Description: 验证暂存预览、幂等确认、私有所有权、全有全无与默认 pinned 绑定闭环。
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillImportJob,
    GeneralSkillRevision,
    Tenant,
    User,
)
from app.general_skills.import_schema import GeneralSkillImportConfirm, GeneralSkillImportJobCreate
from app.general_skills.import_service import GeneralSkillImportError, GeneralSkillImportService
from app.general_skills.object_store import FileSystemSkillObjectStore, SkillObjectStoreError


def _package(*names: str) -> bytes:
    """构造含一个或多个独立指导型 Skill 的上传归档。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        for name in names:
            archive.writestr(
                f"{name}/SKILL.md",
                "---\n"
                f"name: {name}\n"
                f"description: {name} 的生产指导。\n"
                "allowed-tools:\n"
                "  - crm.order.read\n"
                "---\n"
                f"# {name}\n",
            )
            archive.writestr(f"{name}/reference.md", f"# {name} 参考\n")
    return payload.getvalue()


def _request(payload: bytes) -> GeneralSkillImportJobCreate:
    """把 ZIP 转为上传 API 使用的严格 base64 请求。"""

    return GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        filename="skills.zip",
        content_base64=base64.b64encode(payload).decode(),
    )


def _context(tmp_path: Path) -> tuple[Session, GeneralSkillImportService, User, User]:
    """建立隔离租户、两个用户、本人 Agent 和临时对象存储。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    owner = User(
        id="user_owner",
        tenant_id="tenant_a",
        username="owner",
        role="member",
        password_hash="unused",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_a",
        username="other",
        role="member",
        password_hash="unused",
    )
    db.add(Tenant(id="tenant_a", name="Tenant A"))
    db.add(owner)
    db.add(other)
    db.add(
        AgentProfile(
            id="agent_a",
            tenant_id="tenant_a",
            name="售后助手",
            owner_user_id=owner.id,
        )
    )
    db.commit()
    return db, GeneralSkillImportService(db, FileSystemSkillObjectStore(tmp_path)), owner, other


def test_confirm_creates_published_revision_and_default_pinned_private_binding(tmp_path) -> None:
    """证明预览前不安装，确认后根、修订、对象和本人 Agent pinned 绑定同时可见。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_package("refund-helper")),
        idempotency_key="upload-refund-001",
        current_user=owner,
    )
    assert preview.status == "awaiting_approval"
    assert preview.preview_checksum
    assert len(preview.candidates) == 1
    assert db.exec(select(GeneralSkill)).all() == []
    confirmed = service.confirm_job(
        preview.id,
        GeneralSkillImportConfirm(
            preview_checksum=preview.preview_checksum,
            candidate_ids=[preview.candidates[0].candidate_id],
            expected_row_version=preview.row_version,
        ),
        current_user=owner,
    )
    assert confirmed.status == "installed"
    assert confirmed.quota_bytes == 0
    revision = db.exec(select(GeneralSkillRevision)).one()
    skill = db.get(GeneralSkill, revision.skill_id)
    binding = db.exec(select(AgentResourceBinding)).one()
    assert revision.status == "published"
    assert revision.published_at is not None
    assert skill is not None
    assert skill.current_published_revision_id == revision.id
    assert skill.owner_user_id == owner.id
    assert skill.visibility_scope == "agent_private"
    assert binding.agent_id == "agent_a"
    assert binding.metadata_json == {
        "schema_version": 1,
        "revision_policy": "pinned",
        "pinned_revision_id": revision.id,
        "invocation_policy": "model_allowed",
        "atomic_execution_allowed": False,
        "created_by_user_id": owner.id,
    }
    assert all(
        str(item["object_key"]).startswith("sha256:")
        for item in revision.resource_manifest_json
    )
    assert not (tmp_path / "staging" / preview.id).exists()


def test_create_is_idempotent_and_rejects_same_key_for_different_content(tmp_path) -> None:
    """验证同内容重放返回原作业，而不同正文不能复用幂等键。"""

    db, service, owner, _ = _context(tmp_path)
    first = service.create_upload_job(
        _request(_package("first")),
        idempotency_key="upload-idempotent-001",
        current_user=owner,
    )
    replay = service.create_upload_job(
        _request(_package("first")),
        idempotency_key="upload-idempotent-001",
        current_user=owner,
    )
    assert replay.id == first.id
    assert len(db.exec(select(GeneralSkillImportJob)).all()) == 1
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_upload_job(
            _request(_package("second")),
            idempotency_key="upload-idempotent-001",
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_STATE_CONFLICT"


def test_preview_checksum_mismatch_keeps_job_uninstalled(tmp_path) -> None:
    """验证未经本次预览审核的 checksum 不能安装任何修订或绑定。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_package("reviewed")),
        idempotency_key="upload-preview-001",
        current_user=owner,
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum="0" * 64,
                candidate_ids=[preview.candidates[0].candidate_id],
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_PREVIEW_MISMATCH"
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.get(GeneralSkillImportJob, preview.id).status == "awaiting_approval"


def test_other_user_cannot_read_or_confirm_owner_job(tmp_path) -> None:
    """验证同租户其他用户无法凭 opaque job ID 枚举或安装 Skill。"""

    db, service, owner, other = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_package("private")),
        idempotency_key="upload-private-001",
        current_user=owner,
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.get_job(preview.id, current_user=other)
    assert captured.value.error_code == "GENERAL_SKILL_NOT_AVAILABLE"
    with pytest.raises(GeneralSkillImportError):
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[preview.candidates[0].candidate_id],
                expected_row_version=preview.row_version,
            ),
            current_user=other,
        )
    assert db.exec(select(GeneralSkill)).all() == []


def test_malicious_package_persists_failed_terminal_without_skill(tmp_path) -> None:
    """验证危险路径整包失败、错误可恢复查看且不会残留可运行 Skill。"""

    db, service, owner, _ = _context(tmp_path)
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("../SKILL.md", b"unsafe")
    result = service.create_upload_job(
        _request(payload.getvalue()),
        idempotency_key="upload-malicious-001",
        current_user=owner,
    )
    assert result.status == "failed"
    assert result.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert result.quota_bytes == 0
    assert db.exec(select(GeneralSkill)).all() == []


class _FailSecondPromotionStore(FileSystemSkillObjectStore):
    """在第二次内容提升时模拟对象存储中断。"""

    def __init__(self, root: Path) -> None:
        """初始化提升次数计数。"""

        super().__init__(root)
        self.promotions = 0

    def promote(self, job_id: str, checksum: str) -> str:
        """第一次正常提升，第二次抛出稳定存储错误。"""

        self.promotions += 1
        if self.promotions == 2:
            raise SkillObjectStoreError("simulated storage outage")
        return super().promote(job_id, checksum)


def test_multi_candidate_confirm_rolls_back_all_database_rows_on_storage_failure(tmp_path) -> None:
    """验证多候选任一对象失败时不产生部分 Skill、修订或绑定。"""

    db, _, owner, _ = _context(tmp_path)
    service = GeneralSkillImportService(db, _FailSecondPromotionStore(tmp_path))
    preview = service.create_upload_job(
        _request(_package("alpha", "beta")),
        idempotency_key="upload-atomic-001",
        current_user=owner,
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[item.candidate_id for item in preview.candidates],
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_STORAGE_UNAVAILABLE"
    assert db.exec(select(GeneralSkill)).all() == []
    assert db.exec(select(GeneralSkillRevision)).all() == []
    assert db.exec(select(AgentResourceBinding)).all() == []
    assert db.get(GeneralSkillImportJob, preview.id).status == "awaiting_approval"


def test_cancel_releases_staging_and_is_idempotent(tmp_path) -> None:
    """验证取消回收暂存配额，重复取消不改变终态或行版本。"""

    _, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_package("cancel-me")),
        idempotency_key="upload-cancel-001",
        current_user=owner,
    )
    cancelled = service.cancel_job(
        preview.id,
        expected_row_version=preview.row_version,
        current_user=owner,
    )
    replay = service.cancel_job(
        preview.id,
        expected_row_version=preview.row_version,
        current_user=owner,
    )
    assert cancelled.status == replay.status == "cancelled"
    assert cancelled.row_version == replay.row_version
    assert cancelled.quota_bytes == replay.quota_bytes == 0
    assert not (tmp_path / "staging" / preview.id).exists()
