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
from app.db import get_session
from app.db.models import AgentProfile, AgentResourceBinding, GeneralSkillRevision, Tenant, User
from app.main import app
from app.security.auth import create_access_token


@pytest.fixture
def import_api_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Session, dict[str, str]]]:
    """建立启用 V2 的隔离 API、两个认证用户和本人 Agent。"""

    monkeypatch.setenv("APP_SECRET", "general-skill-import-test-key-32-bytes")
    monkeypatch.setenv("GENERAL_SKILL_IMPORT_V2_ENABLED", "true")
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
    db.commit()

    def override_session() -> Iterator[Session]:
        """向 API 提供同一个 SQLite 测试事务会话。"""

        yield db

    app.dependency_overrides[get_session] = override_session
    tokens = {"owner": create_access_token(owner), "other": create_access_token(other)}
    try:
        yield TestClient(app), db, tokens
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def _upload_json() -> dict[str, str]:
    """构造 API 使用的单候选 ZIP base64 请求。"""

    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "refund/SKILL.md",
            "---\n"
            "name: 售后退款指南\n"
            "description: 指导数字员工核验订单并解释退款规则。\n"
            "allowed-tools:\n"
            "  - crm.order.read\n"
            "---\n"
            "# 售后退款指南\n",
        )
    return {
        "tenant_id": "tenant_a",
        "target_agent_id": "agent_owner",
        "source_kind": "upload",
        "filename": "refund.zip",
        "content_base64": base64.b64encode(payload.getvalue()).decode(),
    }


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
