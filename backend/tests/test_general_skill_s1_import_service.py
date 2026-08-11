"""
@Time       : 2026/08/12 01:45
@Author     : zhanglp8181
@File       : test_general_skill_s1_import_service.py
@CallChain  : pytest → GeneralSkillImportService → SQLite/content-addressed object store
@Description: 验证暂存预览、幂等确认、私有所有权、全有全无与默认 pinned 绑定闭环。
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
from zipfile import ZipFile

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    GeneralSkillDependency,
    GeneralSkillImportJob,
    GeneralSkillImportQuota,
    GeneralSkillRevision,
    ManagementAuditLog,
    Tenant,
    User,
    utc_now,
)
from app.general_skills import import_service as import_service_module
from app.general_skills.import_schema import (
    GeneralSkillImportConfirm,
    GeneralSkillImportJobCreate,
    GeneralSkillUploadFile,
)
from app.general_skills.import_service import GeneralSkillImportError, GeneralSkillImportService
from app.general_skills.object_store import FileSystemSkillObjectStore, SkillObjectStoreError
from app.general_skills.remote_source import RemoteFetchResult


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


def _repository_package(*names: str) -> bytes:
    """构造带 GitHub 固定根目录和 skills 子树的仓库归档。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        for name in names:
            archive.writestr(
                f"repo-sha/skills/{name}/SKILL.md",
                "---\n"
                f"name: {name}\n"
                f"description: {name} 的生产指导。\n"
                "---\n"
                f"# {name}\n",
            )
    return payload.getvalue()


def _dependency_package(*, cycle: bool = False) -> bytes:
    """构造父 Skill 引用 user-only 子 Skill 的包，可选形成反向环。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "parent/SKILL.md",
            "---\nname: parent\ndescription: parent guidance\n---\n"
            "Use `/child` before completing the task.\n",
        )
        child_body = "Use `/parent`.\n" if cycle else "Perform the focused child procedure.\n"
        archive.writestr(
            "child/SKILL.md",
            "---\n"
            "name: child\n"
            "description: child guidance\n"
            "disable-model-invocation: true\n"
            'argument-hint: "child input"\n'
            "---\n"
            f"{child_body}",
        )
    return payload.getvalue()


def _request(payload: bytes) -> GeneralSkillImportJobCreate:
    """把 ZIP 转为上传 API 使用的严格 base64 请求。"""

    return GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        filename="skills.zip",
        content_base64=base64.b64encode(payload).decode(),
    )


def _folder_request(files: list[tuple[str, bytes]]) -> GeneralSkillImportJobCreate:
    """构造浏览器文件夹上传使用的相对路径清单。"""

    return GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        filename="folder-upload",
        files=[
            GeneralSkillUploadFile(
                path=path,
                content_base64=base64.b64encode(content).decode(),
            )
            for path, content in files
        ],
    )


class _RemoteFetcherStub:
    """返回固定 ZIP 并记录服务传入的固定归档 URL 与 host allowlist。"""

    def __init__(self, payload: bytes) -> None:
        """保存本次远程来源正文。"""

        self.payload = payload
        self.calls: list[tuple[str, frozenset[str] | None]] = []

    def fetch(
        self,
        source_url: str,
        *,
        allowed_hosts: frozenset[str] | None = None,
    ) -> RemoteFetchResult:
        """记录请求并返回已去敏的固定结果。"""

        self.calls.append((source_url, allowed_hosts))
        return RemoteFetchResult(source_url, self.payload, 0)


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
    assert preview.candidates[0].license_hint is None
    assert preview.candidates[0].risk_findings == [
        "license_not_declared",
        "requests_tools",
    ]
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
    referenced = {
        str(item["content_checksum"]) for item in revision.resource_manifest_json
    }
    assert service.object_store.sweep_unreferenced_objects(
        referenced,
        older_than=utc_now() + timedelta(seconds=1),
    ) == []
    assert not (tmp_path / "staging" / preview.id).exists()
    audit_rows = db.exec(
        select(ManagementAuditLog)
        .where(ManagementAuditLog.resource_id == preview.id)
        .order_by(ManagementAuditLog.created_at, ManagementAuditLog.id)
    ).all()
    assert {row.action for row in audit_rows} == {
        "general_skill_import_created",
        "general_skill_import_fetched",
        "general_skill_import_awaiting_approval",
        "general_skill_import_installed",
    }
    installed_audit = next(
        row for row in audit_rows if row.action == "general_skill_import_installed"
    )
    assert installed_audit.detail_json["revision_ids"] == [revision.id]
    assert installed_audit.correlation_id == preview.id


def test_single_skill_markdown_uses_the_same_secure_preview_pipeline(tmp_path) -> None:
    """验证直接上传 SKILL.md 会封装后进入 ZIP 规范化器，而非走宽松旁路。"""

    db, service, owner, _ = _context(tmp_path)
    manifest = b"---\nname: direct-skill\ndescription: Direct import\n---\nDo the work safely.\n"
    result = service.create_upload_job(
        GeneralSkillImportJobCreate(
            tenant_id="tenant_a",
            target_agent_id="agent_a",
            source_kind="upload",
            filename="SKILL.md",
            content_base64=base64.b64encode(manifest).decode(),
        ),
        idempotency_key="single-skill-md-001",
        current_user=owner,
    )

    assert result.status == "awaiting_approval"
    assert [candidate.name for candidate in result.candidates] == ["direct-skill"]
    assert result.source_reference_redacted == "SKILL.md"


def test_preview_exposes_declared_license_and_non_executing_script_risk(tmp_path) -> None:
    """验证确认页能看到许可证提示与脚本资源风险，且不把脚本标记为已获执行权。"""

    _, service, owner, _ = _context(tmp_path)
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "review/SKILL.md",
            "---\nname: reviewed\ndescription: Reviewed package\nlicense: MIT\n---\nReview.\n",
        )
        archive.writestr("review/scripts/check.py", "print('review only')\n")
    result = service.create_upload_job(
        _request(payload.getvalue()),
        idempotency_key="preview-risk-license-001",
        current_user=owner,
    )

    candidate = result.candidates[0]
    assert candidate.license_hint == "MIT"
    assert candidate.risk_findings == ["contains_executable_content"]


def test_single_skill_markdown_replay_is_content_idempotent(tmp_path) -> None:
    """验证服务端封装结果可复现，相同单文件与幂等键不会因 ZIP 时间戳产生冲突。"""

    _, service, owner, _ = _context(tmp_path)
    request = GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        source_kind="upload",
        filename="SKILL.md",
        content_base64=base64.b64encode(
            b"---\nname: deterministic\ndescription: Stable package\n---\nStable.\n"
        ).decode(),
    )
    first = service.create_upload_job(
        request,
        idempotency_key="single-skill-replay-001",
        current_user=owner,
    )
    replay = service.create_upload_job(
        request,
        idempotency_key="single-skill-replay-001",
        current_user=owner,
    )

    assert replay.id == first.id
    assert replay.raw_checksum == first.raw_checksum


def test_confirm_replay_requires_the_same_semantic_request(tmp_path) -> None:
    """验证同一确认可幂等重放，而改选候选不能借 installed 终态伪装成功。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_package("alpha", "beta")),
        idempotency_key="upload-confirm-replay-001",
        current_user=owner,
    )
    first_candidate, second_candidate = preview.candidates
    request = GeneralSkillImportConfirm(
        preview_checksum=preview.preview_checksum or "",
        candidate_ids=[first_candidate.candidate_id],
        expected_row_version=preview.row_version,
    )
    installed = service.confirm_job(preview.id, request, current_user=owner)
    replay = service.confirm_job(preview.id, request, current_user=owner)
    assert replay.installed_revision_ids == installed.installed_revision_ids
    with pytest.raises(GeneralSkillImportError) as captured:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[second_candidate.candidate_id],
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_STATE_CONFLICT"
    assert len(db.exec(select(GeneralSkill)).all()) == 1


def test_dependency_candidate_requires_explicit_decision_and_selected_child(tmp_path) -> None:
    """验证正文引用既不能静默授权，也不能指向本次未安装的依赖。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_dependency_package()),
        idempotency_key="upload-dependency-review-001",
        current_user=owner,
    )
    parent = next(item for item in preview.candidates if item.name == "parent")
    child = next(item for item in preview.candidates if item.name == "child")
    edge_id = str(parent.dependency_candidates[0]["dependency_candidate_id"])
    with pytest.raises(GeneralSkillImportError) as missing:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[parent.candidate_id, child.candidate_id],
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert missing.value.error_code == "GENERAL_SKILL_DEPENDENCY_INVALID"
    with pytest.raises(GeneralSkillImportError) as unselected:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[parent.candidate_id],
                dependency_decisions=[
                    {"dependency_candidate_id": edge_id, "dependency_kind": "required"}
                ],
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert unselected.value.error_code == "GENERAL_SKILL_DEPENDENCY_INVALID"
    assert db.exec(select(GeneralSkill)).all() == []


def test_confirm_persists_human_dependency_and_user_only_binding_policy(tmp_path) -> None:
    """验证人工确认边固定到父子修订，且 child 的 user-only 策略进入绑定和修订。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_dependency_package()),
        idempotency_key="upload-dependency-install-001",
        current_user=owner,
    )
    parent = next(item for item in preview.candidates if item.name == "parent")
    child = next(item for item in preview.candidates if item.name == "child")
    edge_id = str(parent.dependency_candidates[0]["dependency_candidate_id"])
    confirmed = service.confirm_job(
        preview.id,
        GeneralSkillImportConfirm(
            preview_checksum=preview.preview_checksum or "",
            candidate_ids=[parent.candidate_id, child.candidate_id],
            dependency_decisions=[
                {"dependency_candidate_id": edge_id, "dependency_kind": "required"}
            ],
            expected_row_version=preview.row_version,
        ),
        current_user=owner,
    )
    assert confirmed.status == "installed"
    dependency = db.exec(select(GeneralSkillDependency)).one()
    assert dependency.dependency_kind == "required"
    assert dependency.source == "human_confirmed"
    assert dependency.allow_user_only is True
    child_skill = db.exec(select(GeneralSkill).where(GeneralSkill.name == "child")).one()
    child_revision = db.get(GeneralSkillRevision, child_skill.current_published_revision_id)
    child_binding = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.resource_id == child_skill.id)
    ).one()
    assert child_revision is not None
    assert child_revision.requested_capabilities_json["invocation_policy"] == "user_only"
    assert child_revision.requested_capabilities_json["argument_hint"] == "child input"
    assert child_binding.metadata_json["invocation_policy"] == "user_only"


def test_confirm_rejects_human_confirmed_dependency_cycle_before_writes(tmp_path) -> None:
    """验证双向引用只有经人工标为依赖时才组成环，并在任何安装写入前拒绝。"""

    db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _request(_dependency_package(cycle=True)),
        idempotency_key="upload-dependency-cycle-001",
        current_user=owner,
    )
    decisions = [
        {
            "dependency_candidate_id": str(edge["dependency_candidate_id"]),
            "dependency_kind": "required",
        }
        for candidate in preview.candidates
        for edge in candidate.dependency_candidates
    ]
    with pytest.raises(GeneralSkillImportError) as captured:
        service.confirm_job(
            preview.id,
            GeneralSkillImportConfirm(
                preview_checksum=preview.preview_checksum or "",
                candidate_ids=[candidate.candidate_id for candidate in preview.candidates],
                dependency_decisions=decisions,
                expected_row_version=preview.row_version,
            ),
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_DEPENDENCY_INVALID"
    assert db.exec(select(GeneralSkill)).all() == []


def test_raw_checkpoint_recovers_after_normalizer_process_crash(tmp_path, monkeypatch) -> None:
    """验证 raw 与 fetched 已提交后，即使规范化进程崩溃也能由新 service 重放。"""

    db, service, owner, _ = _context(tmp_path)
    real_normalize = import_service_module.normalize_zip_package

    def crash_normalizer(*_args, **_kwargs):
        """模拟无法被业务异常捕获的进程级中断。"""

        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(import_service_module, "normalize_zip_package", crash_normalizer)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.create_upload_job(
            _request(_package("recoverable")),
            idempotency_key="upload-recovery-001",
            current_user=owner,
        )
    db.rollback()
    interrupted = db.exec(select(GeneralSkillImportJob)).one()
    assert interrupted.status == "fetched"
    assert interrupted.raw_checksum
    assert service.object_store.read_staged(interrupted.id, interrupted.raw_checksum)
    monkeypatch.setattr(import_service_module, "normalize_zip_package", real_normalize)

    recovered = service.recover_stale_jobs(stale_before=utc_now() + timedelta(seconds=1))
    assert [item.status for item in recovered] == ["awaiting_approval"]
    assert recovered[0].preview_checksum
    assert recovered[0].quota_bytes > len(_package("recoverable"))


def test_uncheckpointed_stale_job_fails_closed_and_expired_preview_releases_quota(tmp_path) -> None:
    """验证无 raw 的崩溃态不会猜测恢复，已预览到期后全部暂存和配额均回收。"""

    db, service, owner, _ = _context(tmp_path)
    stale = GeneralSkillImportJob(
        tenant_id="tenant_a",
        owner_user_id=owner.id,
        target_agent_id="agent_a",
        source_kind="upload",
        source_reference_redacted="lost.zip",
        idempotency_key="upload-lost-001",
        expires_at=utc_now() + timedelta(hours=1),
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
    )
    service._reserve_import_quota("tenant_a", owner.id)
    db.add(stale)
    db.commit()
    failed = service.recover_stale_jobs(stale_before=utc_now() - timedelta(minutes=5))
    assert [item.status for item in failed] == ["failed"]
    assert failed[0].error_code == "GENERAL_SKILL_RECOVERY_REQUIRED"

    preview = service.create_upload_job(
        _request(_package("expires")),
        idempotency_key="upload-expires-001",
        current_user=owner,
    )
    preview_row = db.get(GeneralSkillImportJob, preview.id)
    assert preview_row is not None
    preview_row.expires_at = utc_now() - timedelta(seconds=1)
    db.add(preview_row)
    db.commit()
    expired = service.expire_jobs(now=utc_now())
    assert [item.status for item in expired] == ["expired"]
    assert expired[0].quota_bytes == 0
    assert not (tmp_path / "staging" / preview.id).exists()


def test_folder_upload_reuses_normalizer_and_preserves_empty_resources(tmp_path) -> None:
    """验证完整文件夹无需客户端打 ZIP，仍复用候选发现与严格资源规范化。"""

    _db, service, owner, _ = _context(tmp_path)
    preview = service.create_upload_job(
        _folder_request(
            [
                (
                    "folder-skill/SKILL.md",
                    b"---\nname: folder-skill\ndescription: imported folder\n---\n# Folder\n",
                ),
                ("folder-skill/references/empty.md", b""),
            ]
        ),
        idempotency_key="upload-folder-001",
        current_user=owner,
    )
    assert preview.status == "awaiting_approval"
    assert preview.candidates[0].name == "folder-skill"
    assert [item["relative_path"] for item in preview.candidates[0].resources] == [
        "SKILL.md",
        "references/empty.md",
    ]


def test_folder_upload_unsafe_relative_path_fails_entire_job(tmp_path) -> None:
    """验证文件夹来源不能借相对路径绕过 ZIP 路径穿越门禁。"""

    db, service, owner, _ = _context(tmp_path)
    result = service.create_upload_job(
        _folder_request(
            [
                ("safe/SKILL.md", b"---\nname: safe\ndescription: safe\n---\n"),
                ("../outside.txt", b"unsafe"),
            ]
        ),
        idempotency_key="upload-folder-unsafe-001",
        current_user=owner,
    )
    assert result.status == "failed"
    assert result.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert db.exec(select(GeneralSkill)).all() == []


def test_user_concurrent_import_quota_is_atomic_and_released_on_cancel(tmp_path) -> None:
    """验证同一用户第三个活跃导入被 429 拒绝，取消后名额和字节立即可复用。"""

    db, service, owner, _ = _context(tmp_path)
    first = service.create_upload_job(
        _request(_package("quota-one")),
        idempotency_key="upload-quota-001",
        current_user=owner,
    )
    second = service.create_upload_job(
        _request(_package("quota-two")),
        idempotency_key="upload-quota-002",
        current_user=owner,
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_upload_job(
            _request(_package("quota-three")),
            idempotency_key="upload-quota-003",
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_QUOTA_EXCEEDED"
    assert captured.value.status_code == 429
    user_quota = db.exec(
        select(GeneralSkillImportQuota).where(
            GeneralSkillImportQuota.scope_kind == "user",
            GeneralSkillImportQuota.scope_id == owner.id,
        )
    ).one()
    assert user_quota.active_jobs == 2
    assert user_quota.staged_bytes == first.quota_bytes + second.quota_bytes

    service.cancel_job(
        first.id,
        expected_row_version=first.row_version,
        current_user=owner,
    )
    third = service.create_upload_job(
        _request(_package("quota-three")),
        idempotency_key="upload-quota-003",
        current_user=owner,
    )
    assert third.status == "awaiting_approval"
    db.refresh(user_quota)
    assert user_quota.active_jobs == 2
    assert user_quota.staged_bytes == second.quota_bytes + third.quota_bytes


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


def test_concurrent_confirm_has_one_physical_install_and_idempotent_replay(tmp_path) -> None:
    """验证两个独立 SQLite Session 同时确认时只写一组 revision/binding，另一请求可安全重放。"""

    database_path = tmp_path / "confirm-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as setup_db:
        owner = User(
            id="concurrent_owner",
            tenant_id="tenant_concurrent",
            username="concurrent-owner",
            role="member",
            password_hash="unused",
        )
        setup_db.add(Tenant(id="tenant_concurrent", name="Concurrent Tenant"))
        setup_db.add(owner)
        setup_db.add(
            AgentProfile(
                id="agent_concurrent",
                tenant_id="tenant_concurrent",
                name="并发验收员工",
                owner_user_id=owner.id,
            )
        )
        setup_db.commit()
        preview = GeneralSkillImportService(
            setup_db,
            FileSystemSkillObjectStore(tmp_path / "objects"),
        ).create_upload_job(
            GeneralSkillImportJobCreate(
                tenant_id="tenant_concurrent",
                target_agent_id="agent_concurrent",
                source_kind="upload",
                filename="concurrent.zip",
                content_base64=base64.b64encode(_package("concurrent-skill")).decode(),
            ),
            idempotency_key="concurrent-confirm-001",
            current_user=owner,
        )
    request = GeneralSkillImportConfirm(
        preview_checksum=preview.preview_checksum or "",
        candidate_ids=[preview.candidates[0].candidate_id],
        expected_row_version=preview.row_version,
    )
    barrier = Barrier(2)

    def confirm_from_independent_session() -> tuple[str, str]:
        """等待两个 worker 同时起跑，并返回成功状态或稳定领域错误码。"""

        with Session(engine) as worker_db:
            worker_owner = worker_db.get(User, "concurrent_owner")
            assert worker_owner is not None
            service = GeneralSkillImportService(
                worker_db,
                FileSystemSkillObjectStore(tmp_path / "objects"),
            )
            barrier.wait(timeout=5)
            try:
                result = service.confirm_job(
                    preview.id,
                    request,
                    current_user=worker_owner,
                )
                return "success", result.status
            except GeneralSkillImportError as exc:
                return "error", exc.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: confirm_from_independent_session(), range(2)))

    assert all(outcome in {("success", "installed"), ("error", "GENERAL_SKILL_STATE_CONFLICT")} for outcome in outcomes)
    assert any(outcome == ("success", "installed") for outcome in outcomes)
    with Session(engine) as verify_db:
        assert len(verify_db.exec(select(GeneralSkillRevision)).all()) == 1
        assert len(verify_db.exec(select(AgentResourceBinding)).all()) == 1
        installed_job = verify_db.get(GeneralSkillImportJob, preview.id)
        assert installed_job is not None
        assert installed_job.status == "installed"
    engine.dispose()


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
    promoted = list((tmp_path / "objects").glob("*/*"))
    assert len(promoted) == 1
    removed = service.object_store.sweep_unreferenced_objects(
        set(),
        older_than=utc_now() + timedelta(seconds=1),
    )
    assert removed == [promoted[0].name]
    assert not promoted[0].exists()


def test_failed_import_retry_creates_one_linear_attempt_with_corrected_content(tmp_path) -> None:
    """验证失败包可用新正文创建 attempt+1，且同一父作业不能分叉成多个重试。"""

    db, service, owner, _ = _context(tmp_path)
    unsafe_payload = BytesIO()
    with ZipFile(unsafe_payload, "w") as archive:
        archive.writestr("../SKILL.md", b"unsafe")
    failed = service.create_upload_job(
        _request(unsafe_payload.getvalue()),
        idempotency_key="retry-parent-001",
        current_user=owner,
    )
    assert failed.status == "failed"
    retry_request = _request(_package("corrected-skill")).model_copy(
        update={"retry_parent_job_id": failed.id}
    )
    retried = service.create_upload_job(
        retry_request,
        idempotency_key="retry-child-001",
        current_user=owner,
    )

    retry_row = db.get(GeneralSkillImportJob, retried.id)
    assert retry_row is not None
    assert retried.status == "awaiting_approval"
    assert retry_row.parent_job_id == failed.id
    assert retry_row.attempt == 2
    replay = service.create_upload_job(
        retry_request,
        idempotency_key="retry-child-001",
        current_user=owner,
    )
    assert replay.id == retried.id
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_upload_job(
            retry_request,
            idempotency_key="retry-child-other-001",
            current_user=owner,
        )
    assert captured.value.error_code == "GENERAL_SKILL_STATE_CONFLICT"


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


def test_github_source_uses_full_revision_and_shared_preview_pipeline(tmp_path) -> None:
    """验证 GitHub repo 被转换为固定 commit 归档并产生与上传一致的候选预览。"""

    db, service, owner, _ = _context(tmp_path)
    revision = "84fdeffd12f2ee307994d1eb6feb48173b6e0502"
    fetcher = _RemoteFetcherStub(_repository_package("tdd"))
    request = GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        source_kind="github",
        source_url="https://github.com/mattpocock/skills?token=must-not-persist",
        revision=revision,
        source_subpath="skills",
    )
    result = service.create_job(
        request,
        idempotency_key="github-matt-skills-001",
        current_user=owner,
        fetcher=fetcher,
    )
    assert result.status == "awaiting_approval"
    assert [candidate.name for candidate in result.candidates] == ["tdd"]
    assert result.source_reference_redacted == (
        f"https://github.com/mattpocock/skills@{revision}#skills"
    )
    assert fetcher.calls[0][0] == (
        f"https://github.com/mattpocock/skills/archive/{revision}.zip"
    )
    assert "must-not-persist" not in str(result.model_dump(mode="json"))
    assert "must-not-persist" not in str(db.exec(select(ManagementAuditLog)).all())


def test_skillhub_slug_uses_vendor_allowlist_and_shared_preview_pipeline(tmp_path) -> None:
    """验证 SkillHub slug 经固定供应商适配器下载并进入相同预览、确认和绑定链。"""

    _, service, owner, _ = _context(tmp_path)
    fetcher = _RemoteFetcherStub(_package("skillhub-helper"))
    result = service.create_job(
        GeneralSkillImportJobCreate(
            tenant_id="tenant_a",
            target_agent_id="agent_a",
            source_kind="skillhub",
            source_url="skillhub-helper",
        ),
        idempotency_key="skillhub-helper-001",
        current_user=owner,
        fetcher=fetcher,
    )

    assert result.status == "awaiting_approval"
    assert result.source_reference_redacted == "skillhub:skillhub-helper"
    assert [candidate.name for candidate in result.candidates] == ["skillhub-helper"]
    assert fetcher.calls == [
        (
            "https://wry-manatee-359.convex.site/api/v1/download?slug=skillhub-helper",
            frozenset({"wry-manatee-359.convex.site"}),
        )
    ]


def test_remote_idempotency_key_cannot_be_reused_for_another_source(tmp_path) -> None:
    """验证远程作业完成预览后仍保留请求指纹，阻止换 URL 重放同一幂等键。"""

    _, service, owner, _ = _context(tmp_path)
    fetcher = _RemoteFetcherStub(_package("remote"))
    first = GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        source_kind="https",
        source_url="https://packages.example.com/first.zip",
    )
    service.create_job(
        first,
        idempotency_key="https-source-001",
        current_user=owner,
        fetcher=fetcher,
    )
    changed = first.model_copy(update={"source_url": "https://packages.example.com/second.zip"})
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_job(
            changed,
            idempotency_key="https-source-001",
            current_user=owner,
            fetcher=fetcher,
        )
    assert captured.value.error_code == "GENERAL_SKILL_STATE_CONFLICT"


def test_https_source_rejects_embedded_credentials_before_job_persistence(tmp_path) -> None:
    """验证 URL userinfo 在建作业前被拒绝，密码不会进入数据库或 API DTO。"""

    db, service, owner, _ = _context(tmp_path)
    request = GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        source_kind="https",
        source_url="https://import-user:must-not-persist@packages.example.com/skill.zip",
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_job(
            request,
            idempotency_key="https-credential-001",
            current_user=owner,
            fetcher=_RemoteFetcherStub(_package("credential")),
        )

    assert captured.value.error_code == "GENERAL_SKILL_PACKAGE_INVALID"
    assert db.exec(select(GeneralSkillImportJob)).all() == []


def test_https_source_requires_deployment_allowlist_when_policy_is_supplied(tmp_path) -> None:
    """验证生产 API 注入空白名单时关闭任意 HTTPS 来源，而非退化成全网可抓取。"""

    db, _, owner, _ = _context(tmp_path)
    service = GeneralSkillImportService(
        db,
        FileSystemSkillObjectStore(tmp_path),
        https_allowed_hosts=frozenset(),
    )
    request = GeneralSkillImportJobCreate(
        tenant_id="tenant_a",
        target_agent_id="agent_a",
        source_kind="https",
        source_url="https://packages.example.com/skill.zip",
    )
    with pytest.raises(GeneralSkillImportError) as captured:
        service.create_job(
            request,
            idempotency_key="https-no-allowlist-001",
            current_user=owner,
            fetcher=_RemoteFetcherStub(_package("allowlist")),
        )

    assert captured.value.error_code == "GENERAL_SKILL_SOURCE_NOT_CONFIGURED"
    assert db.exec(select(GeneralSkillImportJob)).all() == []
