"""
@Time       : 2026/08/10 18:05
@Author     : zhanglp8181
@File       : test_connection_profiles_api.py
@CallChain  : pytest/TestClient → connection profile API → ConnectionService/SQLite
@Description: 回归连接控制面认证、租户隔离、响应脱敏、CAS 重授权和受权读取。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from httpx import Response
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.connection_profiles import ConnectionProfileCreate, get_connection_service
from app.connectors.service import ConnectionService
from app.connectors.slack import SlackCallResult, SlackOAuthResult
from app.connectors.wecom import WeComCallResult
from app.config import Settings, get_settings
from app.db import get_session
from app.db.models import (
    AgentProfile,
    BusinessRole,
    ConnectionCommandReceipt,
    ConnectionOAuthState,
    ConnectorInboundEvent,
    EmployeeProfile,
    EmployeeRoleAssignment,
    ExecutionSignal,
    ManagementAuditLog,
    Tenant,
    User,
)
from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.main import app
from app.organization.governance import ensure_builtin_governance_catalog
from app.organization.permissions import sync_role_permissions
from app.security.auth import create_access_token
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


class ApiSlackStub:
    """为 API 测试返回可切换的 Slack 探测和读取结果。"""

    def __init__(self) -> None:
        """默认配置两个健康探测和一个频道读取成功响应。"""

        self.auth_results: deque[SlackCallResult] = deque()
        self.read_result = SlackCallResult(True, {"ok": True, "channel": {"id": "C123"}})
        self.oauth_result = SlackOAuthResult(False, error_code="SLACK_OAUTH_EXCHANGE_FAILED")
        self.oauth_calls = 0
        self.wecom = ApiWeComStub()

    def queue_auth(self, team_id: str = "T-A", scopes: set[str] | None = None) -> None:
        """追加一个携带实际 workspace 身份和 scope 的成功探测。"""

        self.auth_results.append(
            SlackCallResult(
                True,
                {"ok": True, "team_id": team_id},
                granted_scopes=frozenset({"channels:read"} if scopes is None else scopes),
            )
        )

    def auth_test(self, _token: str) -> SlackCallResult:
        """返回预设身份探测结果。"""

        return self.auth_results.popleft()

    def conversations_info(self, _token: str, *, channel_id: str) -> SlackCallResult:
        """返回预设频道读取结果。"""

        assert channel_id == "C123"
        return self.read_result

    def exchange_oauth_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> SlackOAuthResult:
        """记录 callback code exchange，确保测试不会访问真实 Slack。"""

        assert code == "oauth-code"
        assert client_id == "client-id"
        assert client_secret == "client-secret"
        assert redirect_uri == "https://app.test/api/enterprise/connection-profiles/slack/oauth/callback"
        self.oauth_calls += 1
        return self.oauth_result


class ApiWeComStub:
    """为 API 测试返回企业微信应用探测结果且不接触网络。"""

    def __init__(self) -> None:
        """初始化应用响应队列和缓存撤销计数。"""

        self.results: deque[WeComCallResult] = deque()
        self.invalidations = 0

    def queue_application(self, agent_id: str = "1000002") -> None:
        """追加一个启用状态的自建应用只读结果。"""

        self.results.append(
            WeComCallResult(
                True,
                {
                    "agent_id": agent_id,
                    "name": "企业微信测试应用",
                    "description": "API 回归",
                    "enabled": True,
                    "home_url": "",
                },
                granted_scopes=frozenset({"application:read"}),
            )
        )

    def application_info(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> WeComCallResult:
        """验证敏感字段进入 adapter 边界后返回预设结果。"""

        assert corp_id == "corp-a"
        assert corp_secret.startswith("secret-")
        assert agent_id == "1000002"
        return self.results.popleft()

    def invalidate_credentials(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> None:
        """记录旧凭据 token 缓存撤销。"""

        assert corp_id and corp_secret and agent_id
        self.invalidations += 1


@pytest.fixture
def api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Session, ApiSlackStub, dict[str, str]]]:
    """建立隔离 API 数据库、认证用户和可替换 Slack adapter。"""

    monkeypatch.setenv("APP_SECRET", "connection-profile-test-key-32-bytes-minimum")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    admin = User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="unused",
    )
    member = User(
        id="member_a",
        tenant_id="tenant_a",
        username="member",
        role="member",
        password_hash="unused",
    )
    connection_manager = User(
        id="connection_manager_a",
        tenant_id="tenant_a",
        username="connection-manager",
        role="member",
        password_hash="unused",
    )
    other_admin = User(
        id="admin_b",
        tenant_id="tenant_b",
        username="other-admin",
        role="admin",
        password_hash="unused",
    )
    db.add_all(
        [
            Tenant(id="tenant_a", name="Tenant A"),
            Tenant(id="tenant_b", name="Tenant B"),
            admin,
            member,
            connection_manager,
            other_admin,
            AgentProfile(id="agent_a", tenant_id="tenant_a", name="Agent A"),
            EmployeeProfile(
                id="employee_admin",
                tenant_id="tenant_a",
                user_id=admin.id,
                employee_id="E-ADMIN",
                employee_name="管理员",
            ),
            EmployeeProfile(
                id="employee_connection_manager",
                tenant_id="tenant_a",
                user_id=connection_manager.id,
                employee_id="E-CONNECTION",
                employee_name="连接管理员",
            ),
        ]
    )
    db.flush()
    ensure_builtin_governance_catalog(db, "tenant_a")
    reader_role = BusinessRole(
        id="role_external_reader",
        tenant_id="tenant_a",
        role_code="cross.external_reader",
        name="外部连接读取人",
        category="cross_functional",
    )
    db.add(reader_role)
    db.flush()
    sync_role_permissions(
        db,
        role=reader_role,
        permission_codes=["external_connection.read"],
    )
    db.add(
        EmployeeRoleAssignment(
            id="grant_admin_external_reader",
            tenant_id="tenant_a",
            employee_profile_id="employee_admin",
            business_role_id=reader_role.id,
            scope_type="tenant",
            scope_id="*",
            include_descendants=True,
            granted_by_user_id=admin.id,
        )
    )
    connection_role = db.exec(
        select(BusinessRole).where(
            BusinessRole.tenant_id == "tenant_a",
            BusinessRole.role_code == "governance_agent_admin",
        )
    ).one()
    db.add(
        EmployeeRoleAssignment(
            id="grant_connection_manager",
            tenant_id="tenant_a",
            employee_profile_id="employee_connection_manager",
            business_role_id=connection_role.id,
            scope_type="tenant",
            scope_id="*",
            include_descendants=True,
            granted_by_user_id=admin.id,
        )
    )
    db.commit()
    slack = ApiSlackStub()

    def override_session() -> Iterator[Session]:
        """向 API 提供测试会话。"""

        yield db

    def override_service() -> ConnectionService:
        """向 API 注入相同事务和 provider stub。"""

        return ConnectionService(db, slack=slack, wecom=slack.wecom)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_connection_service] = override_service
    tokens = {
        "admin": create_access_token(admin),
        "member": create_access_token(member),
        "manager": create_access_token(connection_manager),
        "other": create_access_token(other_admin),
    }
    try:
        yield TestClient(app), db, slack, tokens
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def _auth(token: str) -> dict[str, str]:
    """构造 bearer 请求头。"""

    return {"Authorization": f"Bearer {token}"}


def _create_profile(client: TestClient, slack: ApiSlackStub, token: str) -> dict[str, object]:
    """通过真实 API 创建一个健康 Slack 档案并返回响应。"""

    slack.queue_auth()
    response = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(token),
        json={
            "tenant_id": "tenant_a",
            "command_id": "create_profile_1",
            "provider": "slack",
            "display_name": "Workspace A",
            "token": "xoxb-super-secret",
            "required_scopes": ["channels:read"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _configure_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置固定 HTTPS OAuth callback，避免测试读取开发者本机环境。"""

    monkeypatch.setenv("SLACK_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("SLACK_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "SLACK_OAUTH_REDIRECT_URI",
        "https://app.test/api/enterprise/connection-profiles/slack/oauth/callback",
    )
    get_settings.cache_clear()


def _oauth_state_from_response(response: Response) -> str:
    """从 OAuth start 的 TestClient 响应中提取未持久化的原始 state。"""

    authorize_url = response.json()["authorize_url"]
    return parse_qs(urlparse(authorize_url).query)["state"][0]


def test_profile_api_never_returns_secret_or_internal_reference(api_context) -> None:
    """验证响应不泄密，并在管理审计中冻结连接动作白名单。"""

    client, db, slack, tokens = api_context
    created = _create_profile(client, slack, tokens["admin"])

    listed = client.get(
        "/api/enterprise/connection-profiles?tenant_id=tenant_a",
        headers=_auth(tokens["admin"]),
    )

    assert listed.status_code == 200
    serialized = f"{created}{listed.json()}"
    assert "xoxb-super-secret" not in serialized
    assert "secret_ref" not in serialized
    assert created["has_secret"] is True
    audit = db.exec(
        select(ManagementAuditLog).where(
            ManagementAuditLog.action == "connection_profile.create"
        )
    ).one()
    assert audit.after_json["required_scopes"] == ["channels:read"]
    assert audit.after_json["granted_scopes"] == ["channels:read"]
    assert audit.after_json["tool_allowlist"] == ["slack.channel_info"]
    assert "xoxb-super-secret" not in str(audit.after_json)


def test_wecom_profile_binding_and_probe_form_a_real_read_contract(api_context) -> None:
    """企业微信 API 建档、绑定和只读探测共享同一账号、scope 与 allowlist 契约。"""

    client, db, adapters, tokens = api_context
    adapters.wecom.queue_application()
    adapters.wecom.queue_application()
    created_response = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "create_wecom_1",
            "provider": "wecom",
            "display_name": "企业微信测试",
            "corp_id": "corp-a",
            "agent_id": "1000002",
            "corp_secret": "secret-a",
            "callback_token": "callback-token",
            "callback_encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "required_scopes": ["application:read"],
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    binding = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_wecom_1",
            "expected_revision": created["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["application:read"],
        },
    )
    probe = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/probe-read",
        headers=_auth(tokens["admin"]),
        json={"tenant_id": "tenant_a", "agent_id": "agent_a"},
    )

    assert binding.status_code == 201, binding.text
    assert probe.status_code == 200, probe.text
    assert created["provider"] == "wecom"
    assert created["required_scopes"] == ["application:read"]
    assert created["tool_allowlist"] == ["wecom.application_info"]
    serialized = str({"created": created, "probe": probe.json()})
    assert "secret-a" not in serialized
    assert "access_token" not in serialized
    receipt = db.exec(
        select(ConnectionCommandReceipt).where(
            ConnectionCommandReceipt.command_id == "create_wecom_1"
        )
    ).one()
    assert "secret-a" not in str(receipt.model_dump())


def test_wecom_binding_write_action_uses_dual_cas_receipt_and_audit(api_context) -> None:
    """管理端显式授权审批后发送，并以档案/绑定双修订防止覆盖并发撤权。"""

    client, db, adapters, tokens = api_context
    adapters.wecom.queue_application()
    created_response = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "create_wecom_write_action",
            "provider": "wecom",
            "display_name": "企业微信写动作",
            "corp_id": "corp-a",
            "agent_id": "1000002",
            "corp_secret": "secret-a",
            "callback_token": "callback-token",
            "callback_encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "required_scopes": ["application:read"],
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    binding = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_wecom_write_action",
            "expected_revision": created["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["application:read"],
        },
    ).json()
    payload = {
        "tenant_id": "tenant_a",
        "command_id": "grant_wecom_write_action",
        "expected_profile_revision": created["revision"],
        "expected_binding_revision": binding["revision"],
        "allowed_actions": ["wecom.message_send"],
    }

    granted = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}"
        f"/bindings/{binding['id']}/actions",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    replay = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}"
        f"/bindings/{binding['id']}/actions",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    stale = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}"
        f"/bindings/{binding['id']}/actions",
        headers=_auth(tokens["admin"]),
        json={**payload, "command_id": "stale_wecom_write_action"},
    )
    listed = client.get(
        f"/api/enterprise/connection-profiles/{created['id']}/bindings"
        "?tenant_id=tenant_a",
        headers=_auth(tokens["admin"]),
    )

    assert granted.status_code == replay.status_code == 200
    assert replay.json() == granted.json()
    assert granted.json()["allowed_actions"] == ["wecom.message_send"]
    assert granted.json()["revision"] == binding["revision"] + 1
    assert stale.status_code == 409
    assert listed.json()[0]["allowed_actions"] == ["wecom.message_send"]
    audit = db.exec(
        select(ManagementAuditLog).where(
            ManagementAuditLog.action == "connection_binding.actions_change"
        )
    ).one()
    assert audit.after_json["allowed_actions"] == ["wecom.message_send"]


def test_wecom_inbound_route_and_principal_binding_never_accept_raw_sender(api_context) -> None:
    """管理员只能用已验签事件绑定用户，并显式选择已有连接绑定的 Agent。"""

    client, db, adapters, tokens = api_context
    adapters.wecom.queue_application()
    created_response = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "create_wecom_inbound",
            "provider": "wecom",
            "display_name": "企业微信入站",
            "corp_id": "corp-a",
            "agent_id": "1000002",
            "corp_secret": "secret-a",
            "callback_token": "callback-token",
            "callback_encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "required_scopes": ["application:read"],
        },
    )
    created = created_response.json()
    binding = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_wecom_inbound_agent",
            "expected_revision": created["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["application:read"],
        },
    )
    assert binding.status_code == 201
    event = ConnectorInboundEvent(
        id="inbound_api_event",
        tenant_id="tenant_a",
        provider="wecom",
        profile_id=created["id"],
        external_event_id="external-event",
        payload_checksum="a" * 64,
        encrypted_payload="encrypted-test-payload",
        event_type="text",
        sender_ref_hash="b" * 64,
    )
    db.add(event)
    db.commit()

    route = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/inbound-route",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "set_wecom_inbound_route",
            "expected_revision": created["revision"],
            "agent_id": "agent_a",
        },
    )
    principal = client.post(
        "/api/enterprise/connection-profiles/inbound/principal-bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_wecom_inbound_principal",
            "event_id": event.id,
            "user_id": "member_a",
        },
    )

    assert route.status_code == 200, route.text
    assert principal.status_code == 201, principal.text
    assert route.json()["agent_id"] == "agent_a"
    assert principal.json()["user_id"] == "member_a"
    serialized = f"{route.json()}{principal.json()}"
    assert "sender_ref_hash" not in serialized
    assert "b" * 64 not in serialized


def test_wecom_callback_rotation_uses_cas_receipt_and_never_returns_keys(api_context) -> None:
    """回调密钥轮换经管理员、CAS 和幂等回执，响应与审计均不回显新旧密钥。"""

    client, db, adapters, tokens = api_context
    adapters.wecom.queue_application()
    created = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "create_wecom_callback_rotate",
            "provider": "wecom",
            "display_name": "企业微信回调轮换",
            "corp_id": "corp-a",
            "agent_id": "1000002",
            "corp_secret": "secret-a",
            "callback_token": "old-callback-token",
            "callback_encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            "required_scopes": ["application:read"],
        },
    ).json()
    payload = {
        "tenant_id": "tenant_a",
        "command_id": "rotate_wecom_callback_1",
        "expected_revision": created["revision"],
        "callback_token": "new-callback-token",
        "callback_encoding_aes_key": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg",
    }

    rotated = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/wecom-callback",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    replay = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/wecom-callback",
        headers=_auth(tokens["admin"]),
        json=payload,
    )

    assert rotated.status_code == replay.status_code == 200
    assert rotated.json()["secret_revision"] == 2
    assert rotated.json()["revision"] == created["revision"] + 1
    serialized = rotated.text + replay.text
    assert "new-callback-token" not in serialized
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg" not in serialized
    receipt = db.exec(
        select(ConnectionCommandReceipt).where(
            ConnectionCommandReceipt.command_id == "rotate_wecom_callback_1"
        )
    ).one()
    assert "new-callback-token" not in str(receipt.model_dump())


def test_profile_creation_fails_closed_when_master_key_is_placeholder(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证生产管理入口不会用可预测的开发主密钥保存 Slack token。"""

    client, _db, slack, tokens = api_context
    monkeypatch.setattr(
        "app.api.connection_profiles.get_settings",
        lambda: Settings(
            _env_file=None,
            public_mock_api_key="test-key",
            app_secret="change-me-in-development",
        ),
    )

    response = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "weak_secret_backend_1",
            "provider": "slack",
            "display_name": "Workspace A",
            "token": "xoxb-must-not-be-encrypted",
            "required_scopes": ["channels:read"],
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "CONNECTION_SECRET_BACKEND_NOT_CONFIGURED"}
    assert not slack.auth_results


def test_create_command_replay_returns_frozen_result_and_rejects_changed_semantics(
    api_context,
) -> None:
    """验证命令可安全重放，不同凭据仍被摘要区分且不进入请求 repr。"""

    client, db, slack, tokens = api_context
    slack.queue_auth()
    payload = {
        "tenant_id": "tenant_a",
        "command_id": "create_replay_1",
        "provider": "slack",
        "display_name": "Workspace A",
        "token": "xoxb-command-secret",
        "required_scopes": ["channels:read"],
    }

    first = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    replay = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    changed = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={**payload, "display_name": "Changed"},
    )
    changed_token = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["admin"]),
        json={**payload, "token": "xoxb-different-secret"},
    )

    receipt = db.exec(select(ConnectionCommandReceipt)).one()
    request_model = ConnectionProfileCreate.model_validate(payload)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert changed.status_code == 409
    assert changed.json() == {"detail": "CONNECTION_COMMAND_ID_REUSED"}
    assert changed_token.status_code == 409
    assert changed_token.json() == {"detail": "CONNECTION_COMMAND_ID_REUSED"}
    assert not slack.auth_results
    assert "xoxb-command-secret" not in str(receipt.result_json)
    assert "xoxb-command-secret" not in receipt.payload_checksum
    assert "xoxb-command-secret" not in repr(request_model)


def test_oauth_create_uses_one_time_state_and_persists_no_token_in_callback_response(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证授权 URL、一次性 callback、真实建档事务和 OAuth token 脱敏形成闭环。"""

    client, db, slack, tokens = api_context
    _configure_oauth(monkeypatch)
    slack.oauth_result = SlackOAuthResult(
        True,
        account_id="T-OAUTH",
        account_name="OAuth Workspace",
        granted_scopes=frozenset({"channels:read"}),
        token="xoxb-oauth-secret",
    )
    slack.queue_auth(team_id="T-OAUTH")

    started = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "oauth_create_1",
            "flow_type": "create",
            "display_name": "OAuth Workspace",
            "expected_profile_revision": 0,
        },
    )
    assert started.status_code == 200, started.text
    authorize_url = started.json()["authorize_url"]
    parsed = urlparse(authorize_url)
    state = parse_qs(parsed.query)["state"][0]
    assert parsed.scheme == "https" and parsed.netloc == "slack.com"
    assert parse_qs(parsed.query)["scope"] == ["channels:read"]

    callback = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": state, "code": "oauth-code"},
        follow_redirects=False,
    )
    replay = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": state, "code": "oauth-code"},
        follow_redirects=False,
    )

    oauth_state = db.exec(select(ConnectionOAuthState)).one()
    receipt = db.exec(
        select(ConnectionCommandReceipt).where(
            ConnectionCommandReceipt.command_id == "oauth_create_1"
        )
    ).one()
    assert callback.status_code == replay.status_code == 303
    assert callback.headers["location"].endswith("slack_oauth=success")
    assert "SLACK_OAUTH_STATE_NOT_PENDING" in replay.headers["location"]
    assert oauth_state.status == "consumed"
    assert slack.oauth_calls == 1
    assert "xoxb-oauth-secret" not in callback.text
    assert "xoxb-oauth-secret" not in str(receipt.result_json)


def test_oauth_start_replays_same_command_but_rejects_changed_context(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 start 本身幂等，且 command_id 不能被另一显示语义或 actor 复用。"""

    client, db, _slack, tokens = api_context
    _configure_oauth(monkeypatch)
    payload = {
        "tenant_id": "tenant_a",
        "command_id": "oauth_start_replay_1",
        "flow_type": "create",
        "display_name": "Workspace A",
        "expected_profile_revision": 0,
    }

    first = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    replay = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json=payload,
    )
    changed = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json={**payload, "display_name": "Workspace B"},
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert changed.status_code == 409
    assert changed.json() == {"detail": "CONNECTION_COMMAND_ID_REUSED"}
    assert len(db.exec(select(ConnectionOAuthState)).all()) == 1


@pytest.mark.parametrize(
    ("callback_params", "oauth_result", "expected_code", "expected_calls"),
    [
        ({"error": "access_denied"}, None, "SLACK_OAUTH_DENIED", 0),
        (
            {"code": "oauth-code"},
            SlackOAuthResult(
                True,
                account_id="T-OAUTH",
                account_name="OAuth Workspace",
                granted_scopes=frozenset(),
                token="xoxb-no-scope",
            ),
            "CONNECTION_SCOPE_MISSING",
            1,
        ),
    ],
)
def test_oauth_callback_consumes_denial_and_missing_scope_without_profile(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
    callback_params: dict[str, str],
    oauth_result: SlackOAuthResult | None,
    expected_code: str,
    expected_calls: int,
) -> None:
    """验证拒绝授权和缺 scope 均终结 state，且不会留下连接或可重放 token。"""

    client, db, slack, tokens = api_context
    _configure_oauth(monkeypatch)
    if oauth_result is not None:
        slack.oauth_result = oauth_result
    started = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": f"oauth_failure_{expected_code}",
            "flow_type": "create",
            "display_name": "OAuth Workspace",
            "expected_profile_revision": 0,
        },
    )
    state = _oauth_state_from_response(started)

    callback = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": state, **callback_params},
        follow_redirects=False,
    )
    replay = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": state, **callback_params},
        follow_redirects=False,
    )

    oauth_state = db.exec(select(ConnectionOAuthState)).one()
    assert callback.status_code == replay.status_code == 303
    assert expected_code in callback.headers["location"]
    assert "SLACK_OAUTH_STATE_NOT_PENDING" in replay.headers["location"]
    assert oauth_state.status == "failed"
    assert oauth_state.error_code == expected_code
    assert slack.oauth_calls == expected_calls


def test_oauth_reauthorization_rejects_workspace_drift_without_rotating_secret(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 OAuth 重授权仍绑定原稳定 workspace identity，不能借 callback 静默换号。"""

    client, db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    _configure_oauth(monkeypatch)
    slack.oauth_result = SlackOAuthResult(
        True,
        account_id="T-B",
        account_name="Another Workspace",
        granted_scopes=frozenset({"channels:read"}),
        token="xoxb-another-workspace",
    )
    slack.queue_auth(team_id="T-B")
    started = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "oauth_drift_1",
            "flow_type": "reauthorize",
            "profile_id": profile["id"],
            "expected_profile_revision": profile["revision"],
        },
    )

    callback = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": _oauth_state_from_response(started), "code": "oauth-code"},
        follow_redirects=False,
    )

    oauth_state = db.exec(select(ConnectionOAuthState)).one()
    listed = client.get(
        "/api/enterprise/connection-profiles?tenant_id=tenant_a",
        headers=_auth(tokens["admin"]),
    ).json()[0]
    assert "CONNECTION_ACCOUNT_CHANGED" in callback.headers["location"]
    assert oauth_state.status == "failed"
    assert listed["account_id"] == "T-A"
    assert listed["secret_revision"] == profile["secret_revision"]
    assert "xoxb-another-workspace" not in str(oauth_state.model_dump())


def test_member_and_cross_tenant_admin_cannot_manage_profile(api_context) -> None:
    """验证普通成员和其他租户管理员都不能操作当前租户连接。"""

    client, _db, slack, tokens = api_context
    slack.queue_auth()
    payload = {
        "tenant_id": "tenant_a",
        "command_id": "unauthorized_create_1",
        "provider": "slack",
        "display_name": "Workspace A",
        "token": "secret",
        "required_scopes": ["channels:read"],
    }

    member = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["member"]),
        json=payload,
    )
    other = client.post(
        "/api/enterprise/connection-profiles",
        headers=_auth(tokens["other"]),
        json=payload,
    )

    assert member.status_code == 403
    assert other.status_code == 403


def test_delegated_connection_manager_can_manage_without_platform_admin_role(api_context) -> None:
    """验证普通成员可受托管理连接，但不会借此获得外部业务读取权。"""

    client, _db, slack, tokens = api_context
    created = _create_profile(client, slack, tokens["manager"])
    bound = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/bindings",
        headers=_auth(tokens["manager"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "manager_bind_1",
            "expected_revision": created["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["channels:read"],
        },
    )
    denied_read = client.post(
        f"/api/enterprise/connection-profiles/{created['id']}/probe-read",
        headers=_auth(tokens["manager"]),
        json={"tenant_id": "tenant_a", "agent_id": "agent_a", "channel_id": "C123"},
    )

    assert created["account_id"] == "T-A"
    assert bound.status_code == 201
    assert denied_read.status_code == 403
    assert denied_read.json() == {"detail": "CONNECTION_ACTOR_PERMISSION_REQUIRED"}


def test_binding_and_probe_read_require_explicit_agent_account_binding(api_context) -> None:
    """验证 probe read 在无绑定时失败，绑定后才调用指定账号。"""

    client, _db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    probe_payload = {"tenant_id": "tenant_a", "agent_id": "agent_a", "channel_id": "C123"}

    denied = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/probe-read",
        headers=_auth(tokens["admin"]),
        json=probe_payload,
    )
    binding = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_agent_1",
            "expected_revision": profile["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["channels:read"],
        },
    )
    allowed = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/probe-read",
        headers=_auth(tokens["admin"]),
        json=probe_payload,
    )

    assert denied.status_code == 404
    assert binding.status_code == 201
    assert allowed.status_code == 200
    assert allowed.json()["account_id"] == "T-A"


def test_reauthorize_uses_cas_and_rejects_workspace_drift(api_context) -> None:
    """验证凭据轮换要求当前修订且不能把档案静默切到另一 workspace。"""

    client, _db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    slack.queue_auth(team_id="T-B")
    drift = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/reauthorize",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "reauthorize_drift_1",
            "expected_revision": profile["revision"],
            "token": "new-secret-other-account",
        },
    )
    slack.queue_auth(team_id="T-A")
    rotated = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/reauthorize",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "reauthorize_success_1",
            "expected_revision": profile["revision"],
            "token": "new-secret-same-account",
        },
    )
    slack.queue_auth(team_id="T-A")
    stale = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/reauthorize",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "reauthorize_stale_1",
            "expected_revision": profile["revision"],
            "token": "stale-secret",
        },
    )

    assert drift.status_code == 409
    assert rotated.status_code == 200
    assert rotated.json()["secret_revision"] == 2
    assert stale.status_code == 409


def test_runtime_revocation_persists_reauth_state_without_leaking_error_body(api_context) -> None:
    """验证读取期 token 撤销被持久化为 reauth，API 仅返回稳定错误码。"""

    client, _db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    binding = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_agent_1",
            "expected_revision": profile["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["channels:read"],
        },
    )
    assert binding.status_code == 201
    slack.read_result = SlackCallResult(False, {}, error_code="token_revoked")

    response = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/probe-read",
        headers=_auth(tokens["admin"]),
        json={"tenant_id": "tenant_a", "agent_id": "agent_a", "channel_id": "C123"},
    )
    listed = client.get(
        "/api/enterprise/connection-profiles?tenant_id=tenant_a",
        headers=_auth(tokens["admin"]),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "CONNECTION_TOKEN_REVOKED"}
    assert listed.json()[0]["status"] == "reauth_required"


def test_binding_state_endpoint_uses_revision_and_stops_runtime_resolution(api_context) -> None:
    """验证单个 Agent 绑定可 CAS 停用，停用后 probe read 在外呼前失败。"""

    client, _db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    created = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/bindings",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "bind_agent_1",
            "expected_revision": profile["revision"],
            "agent_id": "agent_a",
            "allowed_scopes": ["channels:read"],
        },
    )
    binding = created.json()

    disabled = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}"
        f"/bindings/{binding['id']}/state",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "disable_binding_1",
            "expected_revision": binding["revision"],
            "enabled": False,
        },
    )
    probe = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}/probe-read",
        headers=_auth(tokens["admin"]),
        json={"tenant_id": "tenant_a", "agent_id": "agent_a", "channel_id": "C123"},
    )

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["revision"] == binding["revision"] + 1
    assert probe.status_code == 404


def test_attention_reauthorization_atomically_rotates_secret_and_emits_resume_signal(
    api_context,
) -> None:
    """验证待办专用命令在一个事务中轮换密钥、决定 Attention 并生成持久 signal。"""

    client, db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    plan = NormalizedPlan(
        goal="读取 Slack 频道",
        success_criteria=(
            SuccessCriterion(id="channel", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="read_channel",
                title="读取频道",
                kind="tool.read",
                capability_refs=(f"slack.channel_info@{profile['id']}",),
            ),
        ),
        budget={"max_steps": 2},
    )
    store = SopExecutionStore(db)
    instance = store.start_dynamic_instance(
        tenant_id="tenant_a",
        session_id="session_reauth_api",
        agent_id="agent_a",
        initiator_user_id="admin_a",
        plan=plan,
        capability_snapshot={
            "tools": [],
            "connectors": [
                {
                    "name": f"slack.channel_info@{profile['id']}",
                    "capability_id": profile["id"],
                }
            ],
        },
    )[0]
    with store.owned(instance, worker_id="prepare_reauth"):
        node = store.enter_node(
            instance,
            "read_channel",
            step_key="read_channel",
            plan_revision_id=instance.current_plan_revision_id,
            step_kind="tool.read",
            title="读取频道",
        )
        attention, _ = ExecutionControlService(db, store).offer_attention(
            instance,
            attention_kind="reauth",
            attention_key="read_channel:reauth:1",
            title="重新授权 Slack",
            payload={
                "profile_id": profile["id"],
                "profile_revision": profile["revision"],
                "secret_revision": profile["secret_revision"],
            },
            allowed_commands=["reauthorize"],
            candidate_user_ids=["admin_a"],
            node_execution=node,
        )
        store.wait_for_work_item(instance, node, work_item_id=attention.id)
    db.commit()
    slack.queue_auth(team_id="T-A")

    response = client.post(
        f"/api/enterprise/connection-profiles/{profile['id']}"
        f"/reauthorize-attention/{attention.id}",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "expected_revision": profile["revision"],
            "attention_expected_revision": attention.revision,
            "command_id": "reauth_api_1",
            "token": "fresh-secret",
        },
    )

    db.refresh(attention)
    signals = db.exec(
        select(ExecutionSignal).where(ExecutionSignal.execution_id == instance.id)
    ).all()
    assert response.status_code == 200, response.text
    assert response.json()["secret_revision"] == profile["secret_revision"] + 1
    assert attention.status == "completed"
    assert attention.resolution_json["command"] == "reauthorize"
    assert len(signals) == 1 and signals[0].signal_type == "attention_decided"


def test_attention_oauth_callback_atomically_rotates_and_resumes_original_execution(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 OAuth callback 与手工 token 路径共享 Attention 原子恢复语义。"""

    client, db, slack, tokens = api_context
    profile = _create_profile(client, slack, tokens["admin"])
    plan = NormalizedPlan(
        goal="读取 Slack 频道",
        success_criteria=(
            SuccessCriterion(id="channel", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="read_channel",
                title="读取频道",
                kind="tool.read",
                capability_refs=(f"slack.channel_info@{profile['id']}",),
            ),
        ),
        budget={"max_steps": 2},
    )
    store = SopExecutionStore(db)
    instance = store.start_dynamic_instance(
        tenant_id="tenant_a",
        session_id="session_reauth_oauth_api",
        agent_id="agent_a",
        initiator_user_id="admin_a",
        plan=plan,
        capability_snapshot={
            "tools": [],
            "connectors": [
                {
                    "name": f"slack.channel_info@{profile['id']}",
                    "capability_id": profile["id"],
                }
            ],
        },
    )[0]
    with store.owned(instance, worker_id="prepare_oauth_reauth"):
        node = store.enter_node(
            instance,
            "read_channel",
            step_key="read_channel",
            plan_revision_id=instance.current_plan_revision_id,
            step_kind="tool.read",
            title="读取频道",
        )
        attention, _ = ExecutionControlService(db, store).offer_attention(
            instance,
            attention_kind="reauth",
            attention_key="read_channel:reauth:oauth",
            title="重新授权 Slack",
            payload={
                "profile_id": profile["id"],
                "profile_revision": profile["revision"],
                "secret_revision": profile["secret_revision"],
            },
            allowed_commands=["reauthorize"],
            candidate_user_ids=["admin_a"],
            node_execution=node,
        )
        store.wait_for_work_item(instance, node, work_item_id=attention.id)
    db.commit()
    _configure_oauth(monkeypatch)
    slack.oauth_result = SlackOAuthResult(
        True,
        account_id="T-A",
        account_name="Workspace A",
        granted_scopes=frozenset({"channels:read"}),
        token="xoxb-oauth-recovery",
    )
    slack.queue_auth(team_id="T-A")

    started = client.post(
        "/api/enterprise/connection-profiles/slack/oauth/start",
        headers=_auth(tokens["admin"]),
        json={
            "tenant_id": "tenant_a",
            "command_id": "oauth_attention_1",
            "flow_type": "reauthorize_attention",
            "profile_id": profile["id"],
            "attention_id": attention.id,
            "expected_profile_revision": profile["revision"],
            "expected_attention_revision": attention.revision,
        },
    )
    callback = client.get(
        "/api/enterprise/connection-profiles/slack/oauth/callback",
        params={"state": _oauth_state_from_response(started), "code": "oauth-code"},
        follow_redirects=False,
    )

    db.refresh(attention)
    signals = db.exec(
        select(ExecutionSignal).where(ExecutionSignal.execution_id == instance.id)
    ).all()
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/enterprise/work-items?slack_oauth=success")
    assert attention.status == "completed"
    assert attention.resolution_json["command"] == "reauthorize"
    assert len(signals) == 1 and signals[0].signal_type == "attention_decided"
