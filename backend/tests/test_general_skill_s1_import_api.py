"""
@Time       : 2026/08/12 02:05
@Author     : zhanglp8181
@File       : test_general_skill_s1_import_api.py
@CallChain  : TestClient → general-skill-import-jobs API → service/SQLite/object store
@Description: 验证新导入 HTTP 契约、开关、认证隔离、刷新恢复和 checksum 确认。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.api.general_skill_imports import get_general_skill_remote_fetcher
from app.db import get_session
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ConnectionSecret,
    GeneralSkillUse,
    GeneralSkillRevision,
    GeneralSkillSourceCredential,
    Tenant,
    User,
)
from app.main import app
from app.general_skills.remote_source import RemoteFetchResult
from app.security.auth import create_access_token


@pytest.fixture
def import_api_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Session, dict[str, str]]]:
    """建立启用 V2 的隔离 API、两个认证用户和本人 Agent。"""

    monkeypatch.setenv("APP_SECRET", "general-skill-import-test-key-32-bytes")
    monkeypatch.setenv("GENERAL_SKILL_IMPORT_V2_ENABLED", "true")
    monkeypatch.setenv("GENERAL_SKILL_IMPORT_ASYNC_ENABLED", "false")
    monkeypatch.setenv("GENERAL_SKILL_OBJECT_STORE_PATH", str(tmp_path / "objects"))
    get_settings.cache_clear()
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
    db.add(Tenant(id="tenant_b", name="Tenant B"))
    db.add(owner)
    db.add(other)
    db.add(
        AgentProfile(
            id="agent_owner",
            tenant_id="tenant_a",
            name="购物售后助手",
            owner_user_id=owner.id,
        )
    )
    db.add(
        ChatSession(
            id="session_owner_skill_runtime",
            tenant_id="tenant_a",
            user_id=owner.id,
            agent_id="agent_owner",
        )
    )
    db.add(
        AgentProfile(
            id="agent_other",
            tenant_id="tenant_a",
            name="Other Agent",
            owner_user_id=other.id,
        )
    )
    db.commit()

    def override_session() -> Iterator[Session]:
        """向 API 提供同一个 SQLite 测试事务会话。"""

        yield db

    class RemoteFetcherStub:
        """在 API 契约测试中返回固定 ZIP，不访问真实供应商。"""

        def fetch(
            self,
            source_url: str,
            *,
            allowed_hosts: frozenset[str] | None = None,
            authorization: str | None = None,
            authorization_hosts: frozenset[str] | None = None,
        ) -> RemoteFetchResult:
            """验证 GitHub 固定归档 URL 后返回上传用例的同一 ZIP。"""

            assert source_url.startswith("https://github.com/mattpocock/skills/archive/")
            assert allowed_hosts and "codeload.github.com" in allowed_hosts
            payload = BytesIO()
            with ZipFile(payload, "w") as archive:
                archive.writestr(
                    "repo-sha/skills/refund/SKILL.md",
                    "---\nname: refund\ndescription: 固定 GitHub 候选。\n---\n# Refund\n",
                )
            return RemoteFetchResult(source_url, payload.getvalue(), 0)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_general_skill_remote_fetcher] = RemoteFetcherStub
    tokens = {"owner": create_access_token(owner), "other": create_access_token(other)}
    try:
        yield TestClient(app), db, tokens
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def _upload_json(*, name: str = "售后退款指南", version: str = "v1") -> dict[str, str]:
    """构造 API 使用的单候选 ZIP base64 请求。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "refund/SKILL.md",
            "---\n"
            f"name: {name}\n"
            "description: 指导数字员工核验订单并解释退款规则。\n"
            "allowed-tools:\n"
            "  - crm.order.read\n"
            "---\n"
            f"# 售后退款指南 {version}\n",
        )
    return {
        "tenant_id": "tenant_a",
        "target_agent_id": "agent_owner",
        "source_kind": "upload",
        "filename": "refund.zip",
        "content_base64": base64.b64encode(payload.getvalue()).decode(),
    }


def test_s2_api_upgrade_publishes_new_revision_under_the_same_skill(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证公开导入 API 携带 target_skill_id 后仍经预览确认并生成同根 v2。"""

    client, db, tokens = import_api_context
    first = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-upgrade-v1-001"),
        json=_upload_json(version="v1"),
    ).json()
    client.post(
        f"/api/enterprise/general-skill-import-jobs/{first['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": first["preview_checksum"],
            "candidate_ids": [first["candidates"][0]["candidate_id"]],
            "expected_row_version": first["row_version"],
        },
    )
    first_revision = db.exec(select(GeneralSkillRevision)).one()
    request = _upload_json(version="v2")
    request["target_skill_id"] = first_revision.skill_id
    second = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-upgrade-v2-001"),
        json=request,
    )
    assert second.status_code == 202, second.text
    preview = second.json()
    assert preview["target_skill_id"] == first_revision.skill_id
    confirmed = client.post(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": preview["preview_checksum"],
            "candidate_ids": [preview["candidates"][0]["candidate_id"]],
            "expected_row_version": preview["row_version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    revisions = db.exec(
        select(GeneralSkillRevision)
        .where(GeneralSkillRevision.skill_id == first_revision.skill_id)
        .order_by(GeneralSkillRevision.revision_number)
    ).all()
    assert [(row.revision_number, row.status) for row in revisions] == [
        (1, "superseded"),
        (2, "published"),
    ]


def _auth(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    """构造 bearer 头并按需附加幂等键。"""

    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_api_preview_refresh_and_confirm_are_real_persisted_operations(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证创建 202、GET 刷新和 confirm 后不可变修订与绑定均真实落库。"""

    client, db, tokens = import_api_context
    created = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-upload-001"),
        json=_upload_json(),
    )
    assert created.status_code == 202, created.text
    preview = created.json()
    assert preview["status"] == "awaiting_approval"
    refreshed = client.get(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}",
        headers=_auth(tokens["owner"]),
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["preview_checksum"] == preview["preview_checksum"]
    confirmed = client.post(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": preview["preview_checksum"],
            "candidate_ids": [preview["candidates"][0]["candidate_id"]],
            "expected_row_version": preview["row_version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "installed"
    revision = db.exec(select(GeneralSkillRevision)).one()
    binding = db.exec(select(AgentResourceBinding)).one()
    assert binding.metadata_json["pinned_revision_id"] == revision.id


def test_s2_governance_api_lists_revisions_and_updates_binding_atomically(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证用户经公开 API 查询修订并原子切换为停用的 follow-latest 绑定。"""

    client, db, tokens = import_api_context
    preview = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-governance-001"),
        json=_upload_json(),
    ).json()
    confirmed = client.post(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": preview["preview_checksum"],
            "candidate_ids": [preview["candidates"][0]["candidate_id"]],
            "expected_row_version": preview["row_version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    revision = db.exec(select(GeneralSkillRevision)).one()
    binding = db.exec(select(AgentResourceBinding)).one()

    revisions = client.get(
        f"/api/enterprise/general-skill-governance/skills/{revision.skill_id}/revisions",
        params={"tenant_id": "tenant_a"},
        headers=_auth(tokens["owner"]),
    )
    assert revisions.status_code == 200, revisions.text
    assert revisions.json()[0]["id"] == revision.id
    catalog_before = client.get(
        "/api/enterprise/general-skill-governance/agents/agent_owner/catalog",
        headers=_auth(tokens["owner"]),
    )
    assert catalog_before.status_code == 200, catalog_before.text
    assert catalog_before.json()["items"][0]["revision_id"] == revision.id
    updated = client.patch(
        f"/api/enterprise/general-skill-governance/bindings/{binding.id}",
        headers=_auth(tokens["owner"]),
        json={
            "agent_id": "agent_owner",
            "status": "inactive",
            "revision_policy": "follow_latest",
            "pinned_revision_id": None,
            "invocation_policy": "user_only",
            "expected_row_version": binding.row_version,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "inactive"
    assert updated.json()["revision_policy"] == "follow_latest"
    assert updated.json()["invocation_policy"] == "user_only"
    catalog_after = client.get(
        "/api/enterprise/general-skill-governance/agents/agent_owner/catalog",
        headers=_auth(tokens["owner"]),
    )
    assert catalog_after.status_code == 200
    assert catalog_after.json()["items"] == []
    assert (
        catalog_after.json()["authorization_revision"]
        > catalog_before.json()["authorization_revision"]
    )


def test_s3_session_catalog_load_and_mute_are_one_authenticated_scope(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """公开会话 API 必须以同一用户/Agent/session 完成目录、加载账本和 mute。"""

    client, db, tokens = import_api_context
    preview = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-runtime-001"),
        json=_upload_json(),
    ).json()
    confirmed = client.post(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": preview["preview_checksum"],
            "candidate_ids": [preview["candidates"][0]["candidate_id"]],
            "expected_row_version": preview["row_version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    revision = db.exec(select(GeneralSkillRevision)).one()

    catalog = client.get(
        "/api/chat/sessions/session_owner_skill_runtime/general-skills",
        params={"agent_id": "agent_owner"},
        headers=_auth(tokens["owner"]),
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["items"][0]["revision_id"] == revision.id
    loaded = client.post(
        "/api/chat/sessions/session_owner_skill_runtime/general-skill-loads",
        headers=_auth(tokens["owner"]),
        json={
            "agent_id": "agent_owner",
            "turn_id": "turn_api_runtime_1",
            "skill_id": revision.skill_id,
            "selection_mode": "forced",
        },
    )
    assert loaded.status_code == 200, loaded.text
    replay = client.post(
        "/api/chat/sessions/session_owner_skill_runtime/general-skill-loads",
        headers=_auth(tokens["owner"]),
        json={
            "agent_id": "agent_owner",
            "turn_id": "turn_api_runtime_1",
            "skill_id": revision.skill_id,
            "selection_mode": "forced",
        },
    )
    assert replay.json()["use_id"] == loaded.json()["use_id"]
    assert len(db.exec(select(GeneralSkillUse)).all()) == 1

    muted = client.put(
        f"/api/chat/sessions/session_owner_skill_runtime/general-skills/{revision.skill_id}",
        headers=_auth(tokens["owner"]),
        json={"agent_id": "agent_owner", "enabled": False},
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["enabled"] is False
    muted_catalog = client.get(
        "/api/chat/sessions/session_owner_skill_runtime/general-skills",
        params={"agent_id": "agent_owner"},
        headers=_auth(tokens["owner"]),
    ).json()["items"]
    assert muted_catalog[0]["enabled"] is False
    assert muted_catalog[0]["override_row_version"] == muted.json()["row_version"]
    denied = client.post(
        "/api/chat/sessions/session_owner_skill_runtime/general-skill-loads",
        headers=_auth(tokens["owner"]),
        json={
            "agent_id": "agent_owner",
            "turn_id": "turn_api_runtime_2",
            "skill_id": revision.skill_id,
            "selection_mode": "forced",
        },
    )
    assert denied.status_code == 404
    assert denied.json()["detail"]["error_code"] == "GENERAL_SKILL_NOT_AVAILABLE"


def test_s2_governance_api_hides_owner_revision_from_other_user(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证同租户其他普通用户不能借稳定 Skill ID 枚举私有修订。"""

    client, db, tokens = import_api_context
    preview = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-governance-private-001"),
        json=_upload_json(),
    ).json()
    client.post(
        f"/api/enterprise/general-skill-import-jobs/{preview['id']}/confirm",
        headers=_auth(tokens["owner"]),
        json={
            "preview_checksum": preview["preview_checksum"],
            "candidate_ids": [preview["candidates"][0]["candidate_id"]],
            "expected_row_version": preview["row_version"],
        },
    )
    revision = db.exec(select(GeneralSkillRevision)).one()

    hidden = client.get(
        f"/api/enterprise/general-skill-governance/skills/{revision.skill_id}/revisions",
        params={"tenant_id": "tenant_a"},
        headers=_auth(tokens["other"]),
    )
    assert hidden.status_code == 403


def test_api_hides_owner_job_from_another_user(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证同租户其他用户读取作业统一得到不可用而不泄漏 owner。"""

    client, _, tokens = import_api_context
    created = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-upload-private-001"),
        json=_upload_json(),
    ).json()
    hidden = client.get(
        f"/api/enterprise/general-skill-import-jobs/{created['id']}",
        headers=_auth(tokens["other"]),
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["error_code"] == "GENERAL_SKILL_NOT_AVAILABLE"


@pytest.mark.parametrize(
    ("tenant_id", "agent_id"),
    [("tenant_b", "agent_owner"), ("tenant_a", "agent_other")],
)
def test_api_rejects_cross_tenant_or_unmanaged_agent_import(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
    tenant_id: str,
    agent_id: str,
) -> None:
    """验证请求正文不能把当前用户提升为跨租户或其他用户 Agent 的安装者。"""

    client, _, tokens = import_api_context
    payload = _upload_json()
    payload.update({"tenant_id": tenant_id, "target_agent_id": agent_id})
    response = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key=f"cross-boundary-{agent_id}"),
        json=payload,
    )

    assert response.status_code in {403, 404}


def test_api_feature_flag_defaults_closed(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证关闭导入 V2 后路由不暴露旧导入回退。"""

    client, _, tokens = import_api_context
    monkeypatch.setenv("GENERAL_SKILL_IMPORT_V2_ENABLED", "false")
    get_settings.cache_clear()
    response = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-upload-disabled-001"),
        json=_upload_json(),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "FEATURE_NOT_AVAILABLE"


def test_api_capabilities_hide_https_until_admin_configures_allowlist(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证能力发现不会把尚未配置主机白名单的 HTTPS 来源错误呈现为可用。"""

    client, _, tokens = import_api_context
    monkeypatch.delenv("GENERAL_SKILL_HTTPS_ALLOWED_HOSTS", raising=False)
    get_settings.cache_clear()
    response = client.get(
        "/api/enterprise/general-skill-import-jobs/capabilities",
        headers=_auth(tokens["owner"]),
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "source_kinds": ["upload", "github", "skillhub"],
    }


def test_api_github_source_requires_and_persists_fixed_revision(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证真实 API 经供应商边界形成 GitHub 固定 revision 预览且不保存 query。"""

    client, _, tokens = import_api_context
    revision = "84fdeffd12f2ee307994d1eb6feb48173b6e0502"
    response = client.post(
        "/api/enterprise/general-skill-import-jobs",
        headers=_auth(tokens["owner"], idempotency_key="api-github-fixed-001"),
        json={
            "tenant_id": "tenant_a",
            "target_agent_id": "agent_owner",
            "source_kind": "github",
            "source_url": "https://github.com/mattpocock/skills?secret=removed",
            "revision": revision,
            "source_subpath": "skills",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["source_reference_redacted"] == (
        f"https://github.com/mattpocock/skills@{revision}#skills"
    )
    assert "secret" not in response.text


def test_api_source_credential_lifecycle_is_private_versioned_and_redacted(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
) -> None:
    """验证本人凭据创建、轮换、撤销、用户隔离及响应/审计去敏形成闭环。"""

    client, db, tokens = import_api_context
    created_response = client.post(
        "/api/enterprise/general-skill-import-jobs/credentials",
        headers=_auth(tokens["owner"]),
        json={
            "tenant_id": "tenant_a",
            "display_name": "私人 GitHub",
            "source_kind": "github",
            "token": "github-private-token-v1-never-return",
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["secret_revision"] == 1
    assert "token" not in created_response.text.lower()
    assert "secret_reference" not in created_response.text

    owner_list = client.get(
        "/api/enterprise/general-skill-import-jobs/credentials",
        headers=_auth(tokens["owner"]),
    )
    other_list = client.get(
        "/api/enterprise/general-skill-import-jobs/credentials",
        headers=_auth(tokens["other"]),
    )
    assert [row["id"] for row in owner_list.json()] == [created["id"]]
    assert other_list.json() == []

    rotated_response = client.post(
        f"/api/enterprise/general-skill-import-jobs/credentials/{created['id']}/rotate",
        headers=_auth(tokens["owner"]),
        json={
            "token": "github-private-token-v2-never-return",
            "expected_row_version": created["row_version"],
        },
    )
    assert rotated_response.status_code == 200, rotated_response.text
    rotated = rotated_response.json()
    assert rotated["secret_revision"] == 2
    stale_rotate = client.post(
        f"/api/enterprise/general-skill-import-jobs/credentials/{created['id']}/rotate",
        headers=_auth(tokens["owner"]),
        json={
            "token": "stale-token-must-not-win",
            "expected_row_version": created["row_version"],
        },
    )
    assert stale_rotate.status_code == 409
    forbidden = client.post(
        f"/api/enterprise/general-skill-import-jobs/credentials/{created['id']}/revoke",
        headers=_auth(tokens["other"]),
        json={"expected_row_version": rotated["row_version"]},
    )
    assert forbidden.status_code == 404

    revoked_response = client.post(
        f"/api/enterprise/general-skill-import-jobs/credentials/{created['id']}/revoke",
        headers=_auth(tokens["owner"]),
        json={"expected_row_version": rotated["row_version"]},
    )
    assert revoked_response.status_code == 200
    assert revoked_response.json()["status"] == "revoked"
    secrets = db.exec(select(ConnectionSecret).order_by(ConnectionSecret.revision)).all()
    assert [row.status for row in secrets] == ["superseded", "revoked"]
    profile = db.exec(select(GeneralSkillSourceCredential)).one()
    assert profile.status == "revoked"
    serialized = created_response.text + rotated_response.text + revoked_response.text
    assert "github-private-token-v1-never-return" not in serialized
    assert "github-private-token-v2-never-return" not in serialized


def test_api_refuses_source_credential_when_secret_backend_is_not_configured(
    import_api_context: tuple[TestClient, Session, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证开发占位 APP_SECRET 下不能把私有仓库 token 写入看似加密的生产档案。"""

    client, db, _tokens = import_api_context
    monkeypatch.setenv("APP_SECRET", "change-me-in-development")
    get_settings.cache_clear()
    owner = db.get(User, "user_owner")
    assert owner is not None
    placeholder_secret_token = create_access_token(owner)
    response = client.post(
        "/api/enterprise/general-skill-import-jobs/credentials",
        headers=_auth(placeholder_secret_token),
        json={
            "tenant_id": "tenant_a",
            "display_name": "不能保存",
            "source_kind": "github",
            "token": "must-never-be-persisted",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == (
        "GENERAL_SKILL_CREDENTIAL_BACKEND_NOT_CONFIGURED"
    )
    assert db.exec(select(ConnectionSecret)).all() == []
