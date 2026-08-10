"""
@Time       : 2026/08/10 17:00
@Author     : zhanglp8181
@File       : test_connection_service.py
@CallChain  : pytest → ConnectionService → SQLite/provider adapter stub
@Description: 回归多账号绑定、密钥隔离、scope 再授权、健康降级和受控外部读取。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.connectors.service import ConnectionError, ConnectionService
from app.connectors.slack import SlackCallResult
from app.connectors.wecom import WeComCallResult
from app.db.models import (
    AgentProfile,
    BusinessRole,
    ConnectionSecret,
    ConnectorThreadBinding,
    EmployeeProfile,
    EmployeeRoleAssignment,
    Tenant,
    User,
)
from app.dynamic_tasks.capability_catalog import DynamicCapabilityCatalog
from app.organization.governance import ensure_builtin_governance_catalog
from app.organization.permissions import sync_role_permissions
from app.security.encryption import encrypt_secret


class SlackStub:
    """按队列返回身份探测结果，并记录不含 token 的只读调用定位信息。"""

    def __init__(self, *auth_results: SlackCallResult) -> None:
        """保存测试预设的 probe 队列。"""

        self.auth_results = deque(auth_results)
        self.read_result = SlackCallResult(True, {"ok": True, "channel": {"id": "C1"}})
        self.read_calls: list[str] = []
        self.read_hook: Callable[[], None] | None = None

    def auth_test(self, _token: str) -> SlackCallResult:
        """返回下一个身份探测响应。"""

        return self.auth_results.popleft()

    def conversations_info(self, _token: str, *, channel_id: str) -> SlackCallResult:
        """记录频道标识并返回预设读取响应。"""

        self.read_calls.append(channel_id)
        result = self.read_result
        if self.read_hook is not None:
            self.read_hook()
        return result


class WeComStub:
    """按队列返回企业微信应用详情，并记录读取与缓存撤销行为。"""

    def __init__(self, *results: WeComCallResult) -> None:
        """保存测试预设响应并初始化非敏感调用计数。"""

        self.results = deque(results)
        self.calls = 0
        self.invalidations = 0
        self.send_result = WeComCallResult(
            True,
            {"message_id": "message-1", "invalid_user_count": 0},
        )
        self.sent: list[tuple[str, str]] = []

    def application_info(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> WeComCallResult:
        """校验测试凭据仅抵达 adapter，并返回下一个应用响应。"""

        assert corp_id.startswith("corp-")
        assert corp_secret.startswith("secret-")
        assert agent_id.isdigit()
        self.calls += 1
        return self.results.popleft()

    def invalidate_credentials(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
    ) -> None:
        """记录旧凭据对应的进程 token 缓存已被撤销。"""

        assert corp_id and corp_secret and agent_id
        self.invalidations += 1

    def send_text(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
        recipient_ref: str,
        content: str,
    ) -> WeComCallResult:
        """记录固定收件人与正文，验证凭据只进入 adapter 边界。"""

        assert corp_id and corp_secret and agent_id
        self.sent.append((recipient_ref, content))
        return self.send_result


@pytest.fixture
def db() -> Session:
    """创建包含两个租户和两个 Agent 的隔离内存数据库。"""

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id="tenant_a", name="Tenant A"),
                Tenant(id="tenant_b", name="Tenant B"),
                AgentProfile(id="agent_a", tenant_id="tenant_a", name="Agent A"),
                AgentProfile(id="agent_b", tenant_id="tenant_b", name="Agent B"),
                User(
                    id="reader_a",
                    tenant_id="tenant_a",
                    username="reader-a",
                    role="member",
                    password_hash="unused",
                ),
                EmployeeProfile(
                    id="employee_reader_a",
                    tenant_id="tenant_a",
                    user_id="reader_a",
                    employee_id="E-READER",
                    employee_name="外部连接读取人",
                ),
            ]
        )
        session.flush()
        ensure_builtin_governance_catalog(session, "tenant_a")
        reader_role = BusinessRole(
            id="role_external_reader",
            tenant_id="tenant_a",
            role_code="cross.external_reader",
            name="外部连接读取人",
            category="cross_functional",
        )
        session.add(reader_role)
        session.flush()
        sync_role_permissions(
            session,
            role=reader_role,
            permission_codes=["external_connection.read", "external_connection.write"],
        )
        session.add(
            EmployeeRoleAssignment(
                id="grant_external_reader",
                tenant_id="tenant_a",
                employee_profile_id="employee_reader_a",
                business_role_id=reader_role.id,
                scope_type="tenant",
                scope_id="*",
                include_descendants=True,
                granted_by_user_id="reader_a",
            )
        )
        session.commit()
        yield session


def _healthy(team_id: str, scopes: set[str] | None = None) -> SlackCallResult:
    """构造携带稳定 workspace 身份和授权 scope 的成功探测。"""

    return SlackCallResult(
        True,
        {"ok": True, "team_id": team_id, "team": team_id},
        granted_scopes=frozenset({"channels:read"} if scopes is None else scopes),
    )


def _healthy_wecom(agent_id: str = "1000002") -> WeComCallResult:
    """构造启用状态的企业微信自建应用只读响应。"""

    return WeComCallResult(
        True,
        {
            "agent_id": agent_id,
            "name": "企业微信测试应用",
            "description": "连接器回归",
            "enabled": True,
            "home_url": "",
        },
        granted_scopes=frozenset({"application:read"}),
    )


def test_profile_keeps_secret_out_of_business_projection_and_supports_two_accounts(db: Session) -> None:
    """验证同 provider 可建两个账号，档案字段和密文均不泄漏原 token。"""

    slack = SlackStub(_healthy("T-A"), _healthy("T-B"))
    service = ConnectionService(db, slack=slack)

    first = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="Workspace A",
        token="xoxb-first-secret",
        required_scopes={"channels:read"},
        actor_user_id="user_admin",
    )
    second = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="Workspace B",
        token="xoxb-second-secret",
        required_scopes={"channels:read"},
        actor_user_id="user_admin",
    )
    db.commit()

    assert {first.account_id, second.account_id} == {"T-A", "T-B"}
    assert "xoxb" not in str(first.model_dump())
    secrets = db.exec(select(ConnectionSecret)).all()
    assert len(secrets) == 2
    assert all("xoxb" not in row.encrypted_payload for row in secrets)


def test_wecom_profile_encrypts_credentials_and_freezes_application_identity(db: Session) -> None:
    """企业微信建档只保存加密三元凭据，并冻结企业/应用身份和最小只读能力。"""

    wecom = WeComStub(_healthy_wecom())
    service = ConnectionService(db, wecom=wecom)

    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信测试",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    db.commit()

    assert profile.provider == "wecom"
    assert profile.account_id.startswith("wecom_app_")
    assert "corp-a" not in profile.account_id
    assert "1000002" not in profile.account_id
    assert profile.required_scopes_json == ["application:read"]
    assert profile.granted_scopes_json == ["application:read"]
    assert profile.tool_allowlist_json == ["wecom.application_info"]
    assert "secret-a" not in str(profile.model_dump())
    stored = db.exec(
        select(ConnectionSecret).where(ConnectionSecret.provider == "wecom")
    ).one()
    assert "secret-a" not in stored.encrypted_payload
    assert "corp-a" not in stored.encrypted_payload


def test_wecom_profile_can_wait_for_trusted_ip_with_encrypted_callback_config(
    db: Session,
) -> None:
    """60020 初始化仍可建降级档案，回调配置只存在密文并能按档案安全解析。"""

    wecom = WeComStub(WeComCallResult(False, {}, error_code="WECOM_60020"))
    service = ConnectionService(db, wecom=wecom)

    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="等待可信 IP",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        callback_token="callback-token",
        callback_encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        actor_user_id="admin",
    )
    db.commit()

    assert profile.status == "active"
    assert profile.health_status == "degraded"
    assert profile.health_error_code == "CONNECTION_TRUSTED_IP_REQUIRED"
    assert profile.granted_scopes_json == []
    stored = db.exec(select(ConnectionSecret).where(ConnectionSecret.provider == "wecom")).one()
    assert "callback-token" not in stored.encrypted_payload
    callback = service.wecom_callback_config(profile.id)
    assert callback.tenant_id == "tenant_a"
    assert callback.profile_id == profile.id
    assert "callback-token" not in repr(callback)


def test_wecom_callback_config_rejects_missing_or_unknown_profile(db: Session) -> None:
    """公开回调不得解析不存在的档案，也不接受未配置回调密钥的旧档案。"""

    wecom = WeComStub(_healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="无回调配置",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    db.commit()

    with pytest.raises(ConnectionError, match="WECOM_CALLBACK_NOT_CONFIGURED"):
        service.wecom_callback_config(profile.id)
    with pytest.raises(ConnectionError, match="WECOM_CALLBACK_NOT_FOUND"):
        service.wecom_callback_config("missing-profile")


def test_wecom_secret_rotation_preserves_encrypted_callback_contract(db: Session) -> None:
    """轮换 CorpSecret 时必须保留同一档案回调密钥，避免已验证 URL 静默失效。"""

    wecom = WeComStub(_healthy_wecom(), _healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="回调轮换测试",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-old",
        callback_token="callback-token",
        callback_encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        actor_user_id="admin",
    )
    db.commit()

    rotated = service.rotate_wecom_secret(
        tenant_id="tenant_a",
        profile_id=profile.id,
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-new",
        expected_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()

    assert rotated.secret_revision == 2
    assert rotated.callback_configured is True
    assert service.wecom_callback_config(profile.id).profile_id == profile.id
    assert wecom.invalidations == 1


def test_wecom_callback_rotation_preserves_api_credentials_and_supersedes_secret(db: Session) -> None:
    """独立轮换回调密钥时保留 CorpSecret，并以新修订取代已暴露的旧回调配置。"""

    wecom = WeComStub(_healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="回调密钥轮换",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        callback_token="old-callback-token",
        callback_encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        actor_user_id="admin",
    )
    db.commit()

    rotated = service.rotate_wecom_callback(
        tenant_id="tenant_a",
        profile_id=profile.id,
        callback_token="new-callback-token",
        callback_encoding_aes_key="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg",
        expected_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()

    callback = service.wecom_callback_config(profile.id)
    assert rotated.secret_revision == 2
    assert callback.token == "new-callback-token"
    secrets = db.exec(
        select(ConnectionSecret).where(ConnectionSecret.reference_id == profile.secret_ref_id)
    ).all()
    assert {row.status for row in secrets} == {"active", "superseded"}


def test_wecom_trusted_ip_failure_is_degraded_without_forcing_secret_rotation(db: Session) -> None:
    """可信 IP 未配置属于可运维降级，不把有效 Secret 误标为需要重新授权。"""

    wecom = WeComStub(
        _healthy_wecom(),
        WeComCallResult(False, {}, error_code="WECOM_60020"),
    )
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信测试",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    db.commit()

    checked = service.check_health(tenant_id="tenant_a", profile_id=profile.id)
    db.commit()

    assert checked.status == "active"
    assert checked.health_status == "degraded"
    assert checked.health_error_code == "CONNECTION_TRUSTED_IP_REQUIRED"


def test_wecom_read_requires_binding_scope_allowlist_and_never_returns_credentials(
    db: Session,
) -> None:
    """企业微信应用读取复用统一三层授权，并只返回 adapter 的安全应用投影。"""

    wecom = WeComStub(_healthy_wecom(), _healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信测试",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"application:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()

    snapshots = DynamicCapabilityCatalog(db).list_connector_reads(
        "tenant_a", "agent_a", "reader_a"
    )
    assert len(snapshots) == 1
    assert snapshots[0].name == f"wecom.application_info@{profile.id}"
    assert snapshots[0].contract["provider"] == "wecom"
    assert snapshots[0].model_view["input_schema"]["properties"] == {}
    assert "corp-a" not in snapshots[0].model_dump_json()
    assert "1000002" not in snapshots[0].model_dump_json()
    assert "secret-a" not in snapshots[0].model_dump_json()

    data = service.read_wecom_application(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
    )

    assert data["agent_id"] == "1000002"
    assert "secret" not in str(data).lower()
    assert "access_token" not in str(data).lower()
    profile.tool_allowlist_json = []
    db.add(profile)
    db.commit()
    with pytest.raises(ConnectionError, match="CONNECTION_ACTION_DENIED"):
        service.read_wecom_application(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
        )


def test_wecom_write_requires_explicit_action_and_dispatches_fixed_thread_once(
    db: Session,
) -> None:
    """写动作需档案/绑定双白名单、独立权限和冻结线程，并只传精确正文给 adapter。"""

    wecom = WeComStub(_healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信受控写",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    binding = service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"application:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    profile, binding = service.set_binding_actions(
        tenant_id="tenant_a",
        profile_id=profile.id,
        binding_id=binding.id,
        allowed_actions={"wecom.message_send"},
        expected_profile_revision=profile.revision,
        expected_binding_revision=binding.revision,
        actor_user_id="admin",
    )
    thread = ConnectorThreadBinding(
        id="thread-write-a",
        tenant_id="tenant_a",
        provider="wecom",
        profile_id=profile.id,
        sender_ref_hash="a" * 64,
        encrypted_recipient_ref=encrypt_secret("external-user-a"),
        user_id="reader_a",
        agent_id="agent_a",
        session_id="session-write-a",
    )
    db.add(thread)
    db.commit()

    evidence = service.validate_wecom_message_dispatch(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        thread_binding_id=thread.id,
        expected_profile_revision=profile.revision,
        expected_secret_revision=profile.secret_revision,
        expected_binding_revision=binding.revision,
    )
    result = service.send_wecom_approved_message(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        thread_binding_id=thread.id,
        content="已批准的精确正文",
        expected_profile_revision=profile.revision,
        expected_secret_revision=profile.secret_revision,
        expected_binding_revision=binding.revision,
    )

    assert evidence["action"] == "wecom.message_send"
    assert "external-user-a" not in str(evidence)
    assert result.success is True
    assert wecom.sent == [("external-user-a", "已批准的精确正文")]


def test_wecom_write_fails_closed_on_revision_revoke_target_and_partial_delivery(
    db: Session,
) -> None:
    """修订漂移、动作撤销、目标失活均须零外呼，部分送达必须进入未知效果语义。"""

    wecom = WeComStub(_healthy_wecom())
    service = ConnectionService(db, wecom=wecom)
    profile = service.create_wecom_profile(
        tenant_id="tenant_a",
        display_name="企业微信写撤权",
        corp_id="corp-a",
        agent_id="1000002",
        corp_secret="secret-a",
        actor_user_id="admin",
    )
    binding = service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"application:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    profile, binding = service.set_binding_actions(
        tenant_id="tenant_a",
        profile_id=profile.id,
        binding_id=binding.id,
        allowed_actions={"wecom.message_send"},
        expected_profile_revision=profile.revision,
        expected_binding_revision=binding.revision,
        actor_user_id="admin",
    )
    thread = ConnectorThreadBinding(
        id="thread-write-revoke",
        tenant_id="tenant_a",
        provider="wecom",
        profile_id=profile.id,
        sender_ref_hash="b" * 64,
        encrypted_recipient_ref=encrypt_secret("external-user-b"),
        user_id="reader_a",
        agent_id="agent_a",
        session_id="session-write-revoke",
    )
    db.add(thread)
    db.commit()

    with pytest.raises(ConnectionError, match="CONNECTION_APPROVAL_REVISION_CHANGED"):
        service.validate_wecom_message_dispatch(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
            thread_binding_id=thread.id,
            expected_profile_revision=profile.revision - 1,
            expected_secret_revision=profile.secret_revision,
            expected_binding_revision=binding.revision,
        )
    assert wecom.sent == []

    thread.status = "disabled"
    db.add(thread)
    db.commit()
    with pytest.raises(ConnectionError, match="CONNECTION_APPROVAL_TARGET_CHANGED"):
        service.validate_wecom_message_dispatch(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
            thread_binding_id=thread.id,
            expected_profile_revision=profile.revision,
            expected_secret_revision=profile.secret_revision,
            expected_binding_revision=binding.revision,
        )
    thread.status = "active"
    db.add(thread)
    db.commit()
    profile, binding = service.set_binding_actions(
        tenant_id="tenant_a",
        profile_id=profile.id,
        binding_id=binding.id,
        allowed_actions=set(),
        expected_profile_revision=profile.revision,
        expected_binding_revision=binding.revision,
        actor_user_id="admin",
    )
    with pytest.raises(ConnectionError, match="CONNECTION_ACTION_DENIED"):
        service.current_wecom_message_dispatch_evidence(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
            thread_binding_id=thread.id,
        )
    assert wecom.sent == []
    profile, binding = service.set_binding_actions(
        tenant_id="tenant_a",
        profile_id=profile.id,
        binding_id=binding.id,
        allowed_actions={"wecom.message_send"},
        expected_profile_revision=profile.revision,
        expected_binding_revision=binding.revision,
        actor_user_id="admin",
    )
    wecom.send_result = WeComCallResult(
        True,
        {"message_id": "partial", "invalid_user_count": 1},
    )
    partial = service.send_wecom_approved_message(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        thread_binding_id=thread.id,
        content="部分送达测试",
        expected_profile_revision=profile.revision,
        expected_secret_revision=profile.secret_revision,
        expected_binding_revision=binding.revision,
    )
    assert partial.success is False
    assert partial.error_code == "WECOM_PARTIAL_DELIVERY"
    assert len(wecom.sent) == 1


def test_binding_selects_explicit_account_and_rejects_cross_tenant(db: Session) -> None:
    """验证 Agent 只能使用明确绑定的同租户账号，不存在默认账号旁路。"""

    slack = SlackStub(_healthy("T-A"), _healthy("T-B"))
    service = ConnectionService(db, slack=slack)
    first = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    second = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="B",
        token="token-b",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=second.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=second.revision,
        actor_user_id="admin",
    )
    db.commit()

    resolved = service.resolve(
        tenant_id="tenant_a",
        profile_id=second.id,
        agent_id="agent_a",
        required_scope="channels:read",
        required_action="slack.channel_info",
        actor_user_id="reader_a",
    )
    assert resolved.token == "token-b"
    assert "token-b" not in repr(resolved)

    with pytest.raises(ConnectionError, match="CONNECTION_BINDING_NOT_FOUND"):
        service.resolve(
            tenant_id="tenant_a",
            profile_id=first.id,
            agent_id="agent_a",
            required_scope="channels:read",
            required_action="slack.channel_info",
            actor_user_id="reader_a",
        )
    with pytest.raises(ConnectionError, match="CONNECTION_PROFILE_NOT_FOUND"):
        service.resolve(
            tenant_id="tenant_b",
            profile_id=second.id,
            agent_id="agent_b",
            required_scope="channels:read",
            required_action="slack.channel_info",
            actor_user_id="reader_a",
        )


def test_dynamic_catalog_exposes_only_explicit_active_account_without_secret(db: Session) -> None:
    """验证规划目录只展示绑定账号的安全身份视图，停用后立即消失。"""

    slack = SlackStub(_healthy("T-A"), _healthy("T-B"))
    service = ConnectionService(db, slack=slack)
    service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="Unbound",
        token="token-unbound",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    bound = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="Bound",
        token="token-bound-secret",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=bound.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=bound.revision,
        actor_user_id="admin",
    )
    db.commit()

    snapshots = DynamicCapabilityCatalog(db).list_connector_reads(
        "tenant_a", "agent_a", "reader_a"
    )

    assert len(snapshots) == 1
    assert snapshots[0].capability_id == bound.id
    serialized = snapshots[0].model_dump_json()
    assert "token-bound-secret" not in serialized
    assert "secret_ref" not in serialized
    bound.status = "disabled"
    db.add(bound)
    db.commit()
    assert DynamicCapabilityCatalog(db).list_connector_reads(
        "tenant_a", "agent_a", "reader_a"
    ) == []


def test_scope_is_narrowed_by_profile_and_binding(db: Session) -> None:
    """验证读取同时受实际授权和 Agent 绑定白名单约束。"""

    service = ConnectionService(db, slack=SlackStub(_healthy("T-A", {"channels:read", "users:read"})))
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()

    with pytest.raises(ConnectionError, match="CONNECTION_SCOPE_DENIED"):
        service.resolve(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            required_scope="users:read",
            required_action="slack.channel_info",
            actor_user_id="reader_a",
        )


@pytest.mark.parametrize(
    ("upstream_error", "expected_code"),
    [
        ("token_expired", "CONNECTION_TOKEN_EXPIRED"),
        ("token_revoked", "CONNECTION_TOKEN_REVOKED"),
        ("invalid_auth", "CONNECTION_INVALID_AUTH"),
    ],
)
def test_health_marks_invalid_credentials_as_reauth_required(
    db: Session,
    upstream_error: str,
    expected_code: str,
) -> None:
    """验证 token 失效不会伪装成普通网络错误，而是进入明确 reauth 状态。"""

    slack = SlackStub(_healthy("T-A"), SlackCallResult(False, {}, error_code=upstream_error))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )

    checked = service.check_health(tenant_id="tenant_a", profile_id=profile.id)

    assert checked.status == "reauth_required"
    assert checked.health_status == "unhealthy"
    assert checked.health_error_code == expected_code


def test_health_preserves_active_profile_during_rate_limit(db: Session) -> None:
    """验证 429 只降级健康并保留 reauth 语义边界。"""

    slack = SlackStub(
        _healthy("T-A"),
        SlackCallResult(False, {}, error_code="SLACK_RATE_LIMITED"),
    )
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )

    checked = service.check_health(tenant_id="tenant_a", profile_id=profile.id)

    assert checked.status == "active"
    assert checked.health_status == "degraded"
    assert checked.health_error_code == "SLACK_RATE_LIMITED"


def test_authorized_read_uses_bound_account_and_records_runtime_revocation(db: Session) -> None:
    """验证受权读取成功，并在后续 token 撤销时立即封锁档案。"""

    slack = SlackStub(_healthy("T-A"))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()

    result = service.read_slack_channel(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        channel_id="C1",
    )
    assert result["channel"] == {"id": "C1"}
    assert slack.read_calls == ["C1"]

    slack.read_result = SlackCallResult(False, {}, error_code="token_revoked")
    with pytest.raises(ConnectionError, match="CONNECTION_TOKEN_REVOKED"):
        service.read_slack_channel(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
            channel_id="C1",
        )
    assert profile.status == "reauth_required"


def test_late_old_token_failure_cannot_invalidate_concurrent_reauthorization(
    db: Session,
) -> None:
    """验证旧 token 迟到失败不覆盖新凭据，并用新修订有界重试。"""

    slack = SlackStub(_healthy("T-A"), _healthy("T-A"))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    db.commit()
    revision_before_read = profile.revision

    def rotate_during_read() -> None:
        """模拟旧请求在途时管理员已完成凭据轮换。"""

        slack.read_hook = None
        service.rotate_slack_secret(
            tenant_id="tenant_a",
            profile_id=profile.id,
            token="token-b",
            expected_revision=revision_before_read,
            actor_user_id="admin",
        )
        slack.read_result = SlackCallResult(True, {"ok": True, "channel": {"id": "C1"}})

    slack.read_hook = rotate_during_read
    slack.read_result = SlackCallResult(False, {}, error_code="token_revoked")

    result = service.read_slack_channel(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        channel_id="C1",
    )

    assert result["channel"] == {"id": "C1"}
    assert slack.read_calls == ["C1", "C1"]
    assert profile.secret_revision == 2
    assert profile.revision == revision_before_read + 1
    assert profile.status == "active"
    assert profile.health_status == "healthy"
    assert profile.health_error_code is None


def test_late_success_cannot_reactivate_profile_disabled_during_read(db: Session) -> None:
    """验证外呼途中停用 profile 后，旧请求的迟到成功不能撤销 kill switch。"""

    slack = SlackStub(_healthy("T-A"))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    profile.health_status = "degraded"
    profile.health_error_code = "SLACK_RATE_LIMITED"
    profile.revision += 1
    db.add(profile)
    db.commit()
    revision_before_read = profile.revision

    def disable_during_read() -> None:
        """模拟管理员在旧请求返回前执行 profile kill switch。"""

        service.disable_profile(
            tenant_id="tenant_a",
            profile_id=profile.id,
            expected_revision=revision_before_read,
            actor_user_id="admin",
        )

    slack.read_hook = disable_during_read

    result = service.read_slack_channel(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        channel_id="C1",
    )

    assert result["channel"] == {"id": "C1"}
    assert profile.revision == revision_before_read + 1
    assert profile.status == "disabled"
    assert profile.health_status == "unhealthy"
    assert profile.health_error_code == "CONNECTION_DISABLED"


def test_successful_retry_clears_runtime_rate_limit_without_churning_healthy_revision(
    db: Session,
) -> None:
    """验证限流后的真实成功读取恢复健康，持续健康读取不制造无意义 profile 修订。"""

    slack = SlackStub(_healthy("T-A"))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    profile.health_status = "degraded"
    profile.health_error_code = "SLACK_RATE_LIMITED"
    profile.revision += 1
    degraded_revision = profile.revision
    db.add(profile)
    db.commit()

    service.read_slack_channel(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        channel_id="C1",
    )
    recovered_revision = profile.revision
    service.read_slack_channel(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        actor_user_id="reader_a",
        channel_id="C1",
    )

    assert profile.health_status == "healthy"
    assert profile.health_error_code is None
    assert recovered_revision == degraded_revision + 1
    assert profile.revision == recovered_revision


def test_tool_allowlist_revocation_blocks_before_slack_adapter_call(db: Session) -> None:
    """验证 scope 与 Agent 绑定均有效时，档案动作白名单仍可在外呼前独立撤权。"""

    slack = SlackStub(_healthy("T-A"))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    service.bind_agent(
        tenant_id="tenant_a",
        profile_id=profile.id,
        agent_id="agent_a",
        allowed_scopes={"channels:read"},
        expected_profile_revision=profile.revision,
        actor_user_id="admin",
    )
    profile.tool_allowlist_json = []
    profile.revision += 1
    db.add(profile)
    db.commit()

    with pytest.raises(ConnectionError, match="CONNECTION_ACTION_DENIED"):
        service.read_slack_channel(
            tenant_id="tenant_a",
            profile_id=profile.id,
            agent_id="agent_a",
            actor_user_id="reader_a",
            channel_id="C1",
        )

    assert slack.read_calls == []


def test_duplicate_provider_account_is_rejected(db: Session) -> None:
    """验证相同租户和 provider 的同一稳定账号不能重复建档。"""

    service = ConnectionService(db, slack=SlackStub(_healthy("T-A"), _healthy("T-A")))
    service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A1",
        token="token-a1",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )
    with pytest.raises(ConnectionError, match="CONNECTION_ACCOUNT_ALREADY_EXISTS"):
        service.create_slack_profile(
            tenant_id="tenant_a",
            display_name="A2",
            token="token-a2",
            required_scopes={"channels:read"},
            actor_user_id="admin",
        )


def test_profile_scope_narrowing_is_detected_by_health_probe(db: Session) -> None:
    """验证已授予 scope 被收窄时进入 reauth，而不是继续使用过期授权快照。"""

    slack = SlackStub(_healthy("T-A"), _healthy("T-A", set()))
    service = ConnectionService(db, slack=slack)
    profile = service.create_slack_profile(
        tenant_id="tenant_a",
        display_name="A",
        token="token-a",
        required_scopes={"channels:read"},
        actor_user_id="admin",
    )

    checked = service.check_health(tenant_id="tenant_a", profile_id=profile.id)

    assert checked.status == "reauth_required"
    assert checked.health_error_code == "CONNECTION_SCOPE_MISSING"
    assert checked.granted_scopes_json == []
