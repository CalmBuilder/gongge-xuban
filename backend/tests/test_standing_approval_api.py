"""
@Time       : 2026/08/11 23:55
@Author     : zhanglp8181
@File       : test_standing_approval_api.py
@CallChain  : pytest/TestClient → Standing Approval API → 治理权限/SQLModel/审计
@Description: 回归长期批准管理 API 的真实权限组合、租户隔离、幂等创建和 CAS 撤销。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.db import get_session
from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    BusinessRole,
    ConnectionProfile,
    ConnectorThreadBinding,
    EmployeeProfile,
    EmployeeRoleAssignment,
    ScheduledTask,
    StandingApprovalRule,
    Tenant,
    User,
    utc_now,
)
from app.main import app
from app.organization.governance import ensure_builtin_governance_catalog
from app.organization.permissions import sync_role_permissions
from app.security.auth import create_access_token


@pytest.fixture
def standing_api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Session, dict[str, str]]]:
    """建立具备长期批准+连接治理+外部写权限的真实组合授权环境。"""

    monkeypatch.setenv("APP_SECRET", "standing-approval-api-test-key-32-bytes")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    manager = User(
        id="standing-manager",
        tenant_id="tenant-standing",
        username="standing-manager",
        password_hash="unused",
    )
    member = User(
        id="standing-member",
        tenant_id="tenant-standing",
        username="standing-member",
        password_hash="unused",
    )
    outsider = User(
        id="standing-outsider",
        tenant_id="tenant-other",
        username="standing-outsider",
        password_hash="unused",
    )
    agent = AgentProfile(
        id="agent-standing",
        tenant_id="tenant-standing",
        name="长期任务员工",
        owner_user_id=manager.id,
    )
    task = ScheduledTask(
        id="schedule-standing",
        tenant_id="tenant-standing",
        agent_id=agent.id,
        created_by_user_id=manager.id,
        title="固定日报",
        prompt="发送固定日报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
    )
    profile = ConnectionProfile(
        id="profile-standing",
        tenant_id="tenant-standing",
        provider="wecom",
        account_id="corp:agent",
        display_name="长期批准企业微信",
        secret_ref_id="secret-standing",
        granted_scopes_json=["wecom.application:read"],
        tool_allowlist_json=["wecom.message_send"],
        created_by_user_id=manager.id,
        updated_by_user_id=manager.id,
    )
    binding = AgentConnectionBinding(
        id="binding-standing",
        tenant_id="tenant-standing",
        agent_id=agent.id,
        profile_id=profile.id,
        allowed_scopes_json=["wecom.application:read"],
        allowed_actions_json=["wecom.message_send"],
        created_by_user_id=manager.id,
        updated_by_user_id=manager.id,
    )
    thread = ConnectorThreadBinding(
        id="thread-standing",
        tenant_id="tenant-standing",
        provider="wecom",
        profile_id=profile.id,
        sender_ref_hash="sender-standing",
        encrypted_recipient_ref="encrypted-standing",
        user_id=manager.id,
        agent_id=agent.id,
        session_id="session-standing",
    )
    db.add_all(
        [
            Tenant(id="tenant-standing", name="Standing tenant"),
            Tenant(id="tenant-other", name="Other tenant"),
            manager,
            member,
            outsider,
            agent,
            task,
            profile,
            binding,
            thread,
            EmployeeProfile(
                id="employee-standing-manager",
                tenant_id="tenant-standing",
                user_id=manager.id,
                employee_id="E-STANDING-MANAGER",
                employee_name="长期批准管理员",
            ),
            EmployeeProfile(
                id="employee-standing-member",
                tenant_id="tenant-standing",
                user_id=member.id,
                employee_id="E-STANDING-MEMBER",
                employee_name="普通成员",
            ),
        ]
    )
    db.flush()
    ensure_builtin_governance_catalog(db, "tenant-standing")
    external_role = BusinessRole(
        id="role-standing-manager",
        tenant_id="tenant-standing",
        role_code="cross.standing_manager",
        name="长期批准管理员",
        category="cross_functional",
    )
    db.add(external_role)
    db.flush()
    sync_role_permissions(
        db,
        role=external_role,
        permission_codes=["external_connection.write"],
    )
    governance_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant-standing",
            BusinessRole.role_code == "governance_agent_admin",
        )
    ).one()
    db.add_all(
        [
            EmployeeRoleAssignment(
            id="grant-standing-manager",
            tenant_id="tenant-standing",
            employee_profile_id="employee-standing-manager",
            business_role_id=external_role.id,
            scope_type="tenant",
            scope_id="*",
            include_descendants=True,
            granted_by_user_id=manager.id,
            ),
            EmployeeRoleAssignment(
                id="grant-standing-governance",
                tenant_id="tenant-standing",
                employee_profile_id="employee-standing-manager",
                business_role_id=governance_role.id,
                scope_type="tenant",
                scope_id="*",
                include_descendants=True,
                granted_by_user_id=manager.id,
            ),
        ]
    )
    db.commit()

    def override_session() -> Iterator[Session]:
        """向 API 注入隔离事务。"""

        yield db

    app.dependency_overrides[get_session] = override_session
    tokens = {
        "manager": create_access_token(manager),
        "member": create_access_token(member),
        "outsider": create_access_token(outsider),
    }
    try:
        yield TestClient(app), db, tokens
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def test_standing_rule_api_create_replay_list_and_revoke(standing_api_context) -> None:
    """管理人可以创建、幂等重放、脱敏查询并以 revision 撤销精确规则。"""

    client, db, tokens = standing_api_context
    now = utc_now()
    candidates = client.get(
        "/api/standing-approval-rules/candidates"
        "?tenant_id=tenant-standing&source_schedule_id=schedule-standing",
        headers=_auth(tokens["manager"]),
    )
    assert candidates.status_code == 200
    assert candidates.json()[0]["thread_binding_id"] == "thread-standing"
    assert "encrypted-standing" not in str(candidates.json())
    payload = {
        "tenant_id": "tenant-standing",
        "command_id": "create-standing-api",
        "agent_id": "agent-standing",
        "source_schedule_id": "schedule-standing",
        "profile_id": "profile-standing",
        "thread_binding_id": candidates.json()[0]["thread_binding_id"],
        "tool_action": "wecom.message_send",
        "argument_constraints": {"content": {"equals": "固定日报"}},
        "valid_from": now.isoformat(),
        "valid_to": (now + timedelta(days=7)).isoformat(),
    }
    created = client.post(
        "/api/standing-approval-rules",
        headers=_auth(tokens["manager"]),
        json=payload,
    )
    replay = client.post(
        "/api/standing-approval-rules",
        headers=_auth(tokens["manager"]),
        json=payload,
    )
    listed = client.get(
        "/api/standing-approval-rules?tenant_id=tenant-standing",
        headers=_auth(tokens["manager"]),
    )

    assert created.status_code == replay.status_code == listed.status_code == 200
    assert created.json()["id"] == replay.json()["id"] == listed.json()[0]["id"]
    assert "encrypted-standing" not in str(listed.json())
    assert "固定日报" in str(listed.json()[0]["argument_constraints"])
    assert len(db.exec(select(StandingApprovalRule)).all()) == 1

    revoked = client.post(
        f"/api/standing-approval-rules/{created.json()['id']}/revoke",
        headers=_auth(tokens["manager"]),
        json={
            "tenant_id": "tenant-standing",
            "command_id": "revoke-standing-api",
            "expected_revision": 1,
        },
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revision"] == 2


def test_standing_rule_api_denies_missing_permission_and_cross_tenant(
    standing_api_context,
) -> None:
    """普通成员及其他租户用户均无法枚举或创建当前租户规则。"""

    client, _db, tokens = standing_api_context
    denied_list = client.get(
        "/api/standing-approval-rules?tenant_id=tenant-standing",
        headers=_auth(tokens["member"]),
    )
    cross_tenant = client.get(
        "/api/standing-approval-rules?tenant_id=tenant-standing",
        headers=_auth(tokens["outsider"]),
    )

    assert denied_list.status_code == 403
    assert cross_tenant.status_code == 403


def _auth(token: str) -> dict[str, str]:
    """构造 bearer 请求头。"""

    return {"Authorization": f"Bearer {token}"}
