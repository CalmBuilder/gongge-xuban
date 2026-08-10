"""
@Time       : 2026/08/10 16:35
@Author     : zhanglp8181
@File       : service.py
@CallChain  : Connector API/DynamicTaskAgent → ConnectionService → secret store/provider adapter
@Description: 管理多账号连接档案、Agent 绑定、scope 再授权、健康状态和受控只读调用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.connectors.slack import SlackAdapter, SlackCallResult
from app.connectors.wecom import (
    WECOM_APPLICATION_INFO_ACTION,
    WECOM_APPLICATION_READ_SCOPE,
    WECOM_MESSAGE_SEND_ACTION,
    WeComAdapter,
    WeComCallResult,
    wecom_account_id,
)
from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    ConnectionProfile,
    ConnectionSecret,
    ConnectorThreadBinding,
    EmployeeProfile,
    MemberOrgAssignment,
    new_id,
    utc_now,
)
from app.organization.permissions import user_permission_codes
from app.organization.query import current_assignment_predicates
from app.security.encryption import decrypt_secret, encrypt_secret


_REAUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "token_expired",
        "token_revoked",
        "account_inactive",
        "WECOM_40001",
        "WECOM_40013",
    }
)
CONNECTION_READ_PERMISSION_CODE = "external_connection.read"
CONNECTION_WRITE_PERMISSION_CODE = "external_connection.write"
_DEFAULT_WECOM_ADAPTER = WeComAdapter()


class ConnectionError(RuntimeError):
    """以稳定代码表达连接档案、授权或上游协议拒绝。"""

    def __init__(self, code: str) -> None:
        """保留可供 API/Runtime 映射的机器代码，不拼接上游敏感正文。"""

        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedConnection:
    """保存一次实时再授权后的账号、凭据和绑定快照。"""

    profile: ConnectionProfile
    binding: AgentConnectionBinding
    token: str = field(repr=False)
    profile_revision: int
    secret_revision: int
    credential_payload: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ValidatedSlackRotation:
    """保存短生命周期的已验证轮换输入；token 禁止进入 repr、日志或持久业务 JSON。"""

    tenant_id: str
    profile_id: str
    expected_revision: int
    account_id: str
    granted_scopes: frozenset[str]
    token: str = field(repr=False)


@dataclass(frozen=True)
class ValidatedWeComRotation:
    """保存已验证的企业微信凭据轮换输入，所有凭据字段均禁止进入 repr。"""

    tenant_id: str
    profile_id: str
    expected_revision: int
    account_id: str
    granted_scopes: frozenset[str]
    corp_id: str = field(repr=False)
    agent_id: str = field(repr=False)
    corp_secret: str = field(repr=False)


@dataclass(frozen=True)
class WeComCallbackConfig:
    """承载指定租户档案解密后的回调配置，所有秘密字段禁止进入 repr。"""

    tenant_id: str
    profile_id: str
    corp_id: str = field(repr=False)
    agent_id: str = field(repr=False)
    token: str = field(repr=False)
    encoding_aes_key: str = field(repr=False)


class ConnectionService:
    """提供不依赖通用 Tool 配置的服务端连接账号控制面。"""

    def __init__(
        self,
        db: Session,
        *,
        slack: SlackAdapter | None = None,
        wecom: WeComAdapter | None = None,
    ) -> None:
        """绑定事务和 provider adapter；调用方决定最终提交边界。"""

        self.db = db
        self.slack = slack or SlackAdapter()
        self.wecom = wecom or _DEFAULT_WECOM_ADAPTER

    def create_slack_profile(
        self,
        *,
        tenant_id: str,
        display_name: str,
        token: str,
        required_scopes: set[str],
        actor_user_id: str,
    ) -> ConnectionProfile:
        """先验证真实 workspace 身份与 scope，再原子保存密钥修订和唯一档案。"""

        normalized_token = token.strip()
        if not normalized_token:
            raise ConnectionError("CONNECTION_SECRET_REQUIRED")
        probe = self.slack.auth_test(normalized_token)
        if not probe.success:
            raise ConnectionError(_normalize_error(probe.error_code))
        account_id = str(probe.data.get("team_id") or "").strip()
        if not account_id:
            raise ConnectionError("SLACK_ACCOUNT_ID_MISSING")
        granted = set(probe.granted_scopes)
        missing = set(required_scopes) - granted
        if missing:
            raise ConnectionError("CONNECTION_SCOPE_MISSING")
        reference_id = new_id("secretref")
        now = utc_now()
        secret = ConnectionSecret(
            tenant_id=tenant_id,
            provider="slack",
            reference_id=reference_id,
            encrypted_payload=encrypt_secret(json.dumps({"token": normalized_token})),
            revision=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        profile = ConnectionProfile(
            tenant_id=tenant_id,
            provider="slack",
            account_id=account_id,
            display_name=display_name.strip(),
            secret_ref_id=reference_id,
            secret_revision=1,
            required_scopes_json=sorted(required_scopes),
            granted_scopes_json=sorted(granted),
            tool_allowlist_json=["slack.channel_info"],
            status="active",
            health_status="healthy",
            last_checked_at=now,
            last_healthy_at=now,
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        self.db.add(secret)
        self.db.add(profile)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_ACCOUNT_ALREADY_EXISTS") from exc
        return profile

    def create_wecom_profile(
        self,
        *,
        tenant_id: str,
        display_name: str,
        corp_id: str,
        agent_id: str,
        corp_secret: str,
        callback_token: str | None = None,
        callback_encoding_aes_key: str | None = None,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """验证企业微信身份；可信 IP 待配置时保存不可执行的受控初始化档案。"""

        credentials = _normalize_wecom_credentials(corp_id, agent_id, corp_secret)
        callback = _normalize_wecom_callback(callback_token, callback_encoding_aes_key)
        probe = self.wecom.application_info(**credentials)
        waiting_for_trusted_ip = probe.error_code == "WECOM_60020"
        if not probe.success and not waiting_for_trusted_ip:
            raise ConnectionError(_normalize_error(probe.error_code))
        returned_agent_id = (
            credentials["agent_id"]
            if waiting_for_trusted_ip
            else str(probe.data.get("agent_id") or "").strip()
        )
        if returned_agent_id != credentials["agent_id"]:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        if not waiting_for_trusted_ip and probe.data.get("enabled") is not True:
            raise ConnectionError("WECOM_APPLICATION_DISABLED")
        account_id = wecom_account_id(credentials["corp_id"], returned_agent_id)
        reference_id = new_id("secretref")
        now = utc_now()
        secret = ConnectionSecret(
            tenant_id=tenant_id,
            provider="wecom",
            reference_id=reference_id,
            encrypted_payload=encrypt_secret(json.dumps({**credentials, **callback})),
            revision=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        profile = ConnectionProfile(
            tenant_id=tenant_id,
            provider="wecom",
            account_id=account_id,
            display_name=display_name.strip(),
            secret_ref_id=reference_id,
            secret_revision=1,
            callback_configured=bool(callback),
            required_scopes_json=[WECOM_APPLICATION_READ_SCOPE],
            granted_scopes_json=[] if waiting_for_trusted_ip else sorted(probe.granted_scopes),
            tool_allowlist_json=[WECOM_APPLICATION_INFO_ACTION],
            status="active",
            health_status="degraded" if waiting_for_trusted_ip else "healthy",
            health_error_code=("CONNECTION_TRUSTED_IP_REQUIRED" if waiting_for_trusted_ip else None),
            last_checked_at=now,
            last_healthy_at=None if waiting_for_trusted_ip else now,
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        self.db.add(secret)
        self.db.add(profile)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_ACCOUNT_ALREADY_EXISTS") from exc
        return profile

    def wecom_callback_config(self, profile_id: str) -> WeComCallbackConfig:
        """按公开回调路径中的档案 ID 解析租户隔离且已加密保存的回调配置。"""

        profile = self.db.get(ConnectionProfile, profile_id)
        if profile is None or profile.provider != "wecom" or profile.status == "disabled":
            raise ConnectionError("WECOM_CALLBACK_NOT_FOUND")
        payload = self._decoded_secret_payload(self._active_secret(profile))
        credentials = _wecom_credentials(payload)
        callback = _wecom_callback(payload)
        return WeComCallbackConfig(
            tenant_id=profile.tenant_id,
            profile_id=profile.id,
            corp_id=credentials["corp_id"],
            agent_id=credentials["agent_id"],
            token=callback["callback_token"],
            encoding_aes_key=callback["callback_encoding_aes_key"],
        )

    def send_wecom_reply(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        recipient_ref: str,
        content: str,
    ) -> WeComCallResult:
        """使用回调线程预先验证的目标回发结果，不把发信暴露为模型可选工具。"""

        profile = self._profile(tenant_id, profile_id)
        if profile.provider != "wecom" or profile.status != "active":
            raise ConnectionError("CONNECTION_PROFILE_NOT_ACTIVE")
        payload = self._decoded_secret_payload(self._active_secret(profile))
        credentials = _wecom_credentials(payload)
        return self.wecom.send_text(
            **credentials,
            recipient_ref=recipient_ref,
            content=content,
        )

    def rotate_wecom_callback(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        callback_token: str,
        callback_encoding_aes_key: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """以 CAS 新增密钥修订并轮换回调配置，同时原样保留企业微信 API 凭据。"""

        profile = self._profile_for_update(tenant_id, profile_id)
        if profile.provider != "wecom":
            raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
        if profile.revision != expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        old_secret = self._active_secret(profile)
        old_payload = self._decoded_secret_payload(old_secret)
        credentials = _wecom_credentials(old_payload)
        callback = _normalize_wecom_callback(callback_token, callback_encoding_aes_key)
        if not callback:
            raise ConnectionError("WECOM_CALLBACK_CONFIG_INVALID")
        now = utc_now()
        next_revision = profile.secret_revision + 1
        new_secret = ConnectionSecret(
            tenant_id=tenant_id,
            provider="wecom",
            reference_id=profile.secret_ref_id,
            encrypted_payload=encrypt_secret(json.dumps({**credentials, **callback})),
            revision=next_revision,
            status="active",
            created_at=now,
            updated_at=now,
        )
        old_secret.status = "superseded"
        old_secret.updated_at = now
        profile.secret_revision = next_revision
        profile.callback_configured = True
        profile.revision += 1
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = now
        self.db.add(old_secret)
        self.db.add(new_secret)
        self.db.add(profile)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT") from exc
        return profile

    def bind_agent(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        allowed_scopes: set[str],
        expected_profile_revision: int,
        actor_user_id: str,
    ) -> AgentConnectionBinding:
        """校验三方同租户并确保绑定 scope 不超过档案已授权集合。"""

        profile = self._profile(tenant_id, profile_id)
        if profile.revision != expected_profile_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        if profile.status != "active":
            raise ConnectionError("CONNECTION_PROFILE_NOT_ACTIVE")
        agent = self.db.get(AgentProfile, agent_id)
        if agent is None or agent.tenant_id != tenant_id or agent.status != "active":
            raise ConnectionError("CONNECTION_AGENT_NOT_FOUND")
        if not allowed_scopes <= set(profile.granted_scopes_json or []):
            raise ConnectionError("CONNECTION_SCOPE_MISSING")
        row = AgentConnectionBinding(
            tenant_id=tenant_id,
            profile_id=profile.id,
            agent_id=agent.id,
            allowed_scopes_json=sorted(allowed_scopes),
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
        )
        self.db.add(row)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_BINDING_ALREADY_EXISTS") from exc
        return row

    def rotate_slack_secret(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        token: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """以 CAS 更新凭据，要求新 token 仍属于原 workspace 且满足冻结 scope。"""

        validated = self.validate_slack_reauthorization(
            tenant_id=tenant_id,
            profile_id=profile_id,
            token=token,
            expected_revision=expected_revision,
        )
        return self.apply_slack_reauthorization(validated, actor_user_id=actor_user_id)

    def validate_slack_reauthorization(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        token: str,
        expected_revision: int,
    ) -> ValidatedSlackRotation:
        """在不持有数据库写锁时验证新 token 的账号身份和冻结 scope。"""

        profile = self._profile(tenant_id, profile_id)
        if profile.revision != expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        normalized_token = token.strip()
        if not normalized_token:
            raise ConnectionError("CONNECTION_SECRET_REQUIRED")
        probe = self.slack.auth_test(normalized_token)
        if not probe.success:
            raise ConnectionError(_normalize_error(probe.error_code))
        account_id = str(probe.data.get("team_id") or "")
        if account_id != profile.account_id:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        if set(profile.required_scopes_json or []) - set(probe.granted_scopes):
            raise ConnectionError("CONNECTION_SCOPE_MISSING")
        return ValidatedSlackRotation(
            tenant_id=tenant_id,
            profile_id=profile_id,
            expected_revision=expected_revision,
            account_id=account_id,
            granted_scopes=probe.granted_scopes,
            token=normalized_token,
        )

    def apply_slack_reauthorization(
        self,
        validated: ValidatedSlackRotation,
        *,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """在调用方事务内锁定档案并应用已验证轮换，供管理或 Attention 原子命令复用。"""

        profile = self._profile_for_update(validated.tenant_id, validated.profile_id)
        if profile.revision != validated.expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        if profile.account_id != validated.account_id:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        now = utc_now()
        old_secret = self.db.exec(
            select(ConnectionSecret).where(
                ConnectionSecret.tenant_id == validated.tenant_id,
                ConnectionSecret.provider == profile.provider,
                ConnectionSecret.reference_id == profile.secret_ref_id,
                ConnectionSecret.revision == profile.secret_revision,
                ConnectionSecret.status == "active",
            )
        ).first()
        if old_secret is None:
            raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE")
        next_revision = profile.secret_revision + 1
        new_secret = ConnectionSecret(
            tenant_id=validated.tenant_id,
            provider=profile.provider,
            reference_id=profile.secret_ref_id,
            encrypted_payload=encrypt_secret(json.dumps({"token": validated.token})),
            revision=next_revision,
            status="active",
            created_at=now,
            updated_at=now,
        )
        old_secret.status = "superseded"
        old_secret.updated_at = now
        profile.secret_revision = next_revision
        profile.granted_scopes_json = sorted(validated.granted_scopes)
        profile.status = "active"
        profile.health_status = "healthy"
        profile.health_error_code = None
        profile.rate_limited_until = None
        profile.last_checked_at = now
        profile.last_healthy_at = now
        profile.revision += 1
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = now
        self.db.add(old_secret)
        self.db.add(new_secret)
        self.db.add(profile)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT") from exc
        return profile

    def rotate_wecom_secret(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        corp_id: str,
        agent_id: str,
        corp_secret: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """验证同一企业微信应用后，以新密钥修订替换旧凭据。"""

        validated = self.validate_wecom_reauthorization(
            tenant_id=tenant_id,
            profile_id=profile_id,
            corp_id=corp_id,
            agent_id=agent_id,
            corp_secret=corp_secret,
            expected_revision=expected_revision,
        )
        return self.apply_wecom_reauthorization(validated, actor_user_id=actor_user_id)

    def validate_wecom_reauthorization(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        corp_id: str,
        agent_id: str,
        corp_secret: str,
        expected_revision: int,
    ) -> ValidatedWeComRotation:
        """无锁验证新凭据仍属于原企业和应用，并冻结验证所得能力集合。"""

        profile = self._profile(tenant_id, profile_id)
        if profile.provider != "wecom":
            raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
        if profile.revision != expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        credentials = _normalize_wecom_credentials(corp_id, agent_id, corp_secret)
        probe = self.wecom.application_info(**credentials)
        if not probe.success:
            raise ConnectionError(_normalize_error(probe.error_code))
        returned_agent_id = str(probe.data.get("agent_id") or "").strip()
        account_id = wecom_account_id(credentials["corp_id"], returned_agent_id)
        if account_id != profile.account_id:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        if set(profile.required_scopes_json or []) - set(probe.granted_scopes):
            raise ConnectionError("CONNECTION_SCOPE_MISSING")
        return ValidatedWeComRotation(
            tenant_id=tenant_id,
            profile_id=profile_id,
            expected_revision=expected_revision,
            account_id=account_id,
            granted_scopes=probe.granted_scopes,
            **credentials,
        )

    def apply_wecom_reauthorization(
        self,
        validated: ValidatedWeComRotation,
        *,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """在行锁内轮换企业微信密钥，保留旧修订供审计但禁止运行时回退。"""

        profile = self._profile_for_update(validated.tenant_id, validated.profile_id)
        if profile.provider != "wecom":
            raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
        if profile.revision != validated.expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        if profile.account_id != validated.account_id:
            raise ConnectionError("CONNECTION_ACCOUNT_CHANGED")
        old_secret = self._active_secret(profile)
        old_payload = self._decoded_secret_payload(old_secret)
        now = utc_now()
        next_revision = profile.secret_revision + 1
        new_payload = {
            "corp_id": validated.corp_id,
            "agent_id": validated.agent_id,
            "corp_secret": validated.corp_secret,
        }
        if profile.callback_configured:
            new_payload.update(_wecom_callback(old_payload))
        new_secret = ConnectionSecret(
            tenant_id=validated.tenant_id,
            provider="wecom",
            reference_id=profile.secret_ref_id,
            encrypted_payload=encrypt_secret(json.dumps(new_payload)),
            revision=next_revision,
            status="active",
            created_at=now,
            updated_at=now,
        )
        old_secret.status = "superseded"
        old_secret.updated_at = now
        profile.secret_revision = next_revision
        profile.granted_scopes_json = sorted(validated.granted_scopes)
        profile.status = "active"
        profile.health_status = "healthy"
        profile.health_error_code = None
        profile.rate_limited_until = None
        profile.last_checked_at = now
        profile.last_healthy_at = now
        profile.revision += 1
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = now
        self.db.add(old_secret)
        self.db.add(new_secret)
        self.db.add(profile)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT") from exc
        try:
            self.wecom.invalidate_credentials(**_wecom_credentials(old_payload))
        except ConnectionError:
            pass
        return profile

    def set_binding_enabled(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        binding_id: str,
        enabled: bool,
        expected_revision: int,
        actor_user_id: str,
    ) -> AgentConnectionBinding:
        """以 CAS 启停单个 Agent 账号绑定，使运行时解析立即应用撤权。"""

        row = self.db.exec(
            select(AgentConnectionBinding)
            .where(
                AgentConnectionBinding.id == binding_id,
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.profile_id == profile_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            raise ConnectionError("CONNECTION_BINDING_NOT_FOUND")
        if row.revision != expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        profile = self._profile(tenant_id, profile_id)
        if enabled and profile.status != "active":
            raise ConnectionError("CONNECTION_PROFILE_NOT_ACTIVE")
        row.enabled = enabled
        row.revision += 1
        row.updated_by_user_id = actor_user_id
        row.updated_at = utc_now()
        self.db.add(row)
        return row

    def set_binding_actions(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        binding_id: str,
        allowed_actions: set[str],
        expected_profile_revision: int,
        expected_binding_revision: int,
        actor_user_id: str,
    ) -> tuple[ConnectionProfile, AgentConnectionBinding]:
        """以双 CAS 管理 Agent 动作白名单，并同步档案级 provider 动作开关。"""

        if allowed_actions - {WECOM_MESSAGE_SEND_ACTION}:
            raise ConnectionError("CONNECTION_ACTION_UNSUPPORTED")
        profile = self._profile_for_update(tenant_id, profile_id)
        if profile.revision != expected_profile_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        if profile.provider != "wecom":
            raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
        if profile.status != "active" or profile.health_status != "healthy":
            raise ConnectionError("CONNECTION_PROFILE_NOT_ACTIVE")
        binding = self.db.exec(
            select(AgentConnectionBinding)
            .where(
                AgentConnectionBinding.id == binding_id,
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.profile_id == profile_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if binding is None:
            raise ConnectionError("CONNECTION_BINDING_NOT_FOUND")
        if binding.revision != expected_binding_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        binding.allowed_actions_json = sorted(allowed_actions)
        binding.revision += 1
        binding.updated_by_user_id = actor_user_id
        binding.updated_at = utc_now()
        other_actions = {
            action
            for row in self.db.exec(
                select(AgentConnectionBinding).where(
                    AgentConnectionBinding.tenant_id == tenant_id,
                    AgentConnectionBinding.profile_id == profile_id,
                    AgentConnectionBinding.id != binding.id,
                    AgentConnectionBinding.enabled.is_(True),
                )
            ).all()
            for action in (row.allowed_actions_json or [])
        }
        profile.tool_allowlist_json = sorted(
            {WECOM_APPLICATION_INFO_ACTION} | other_actions | allowed_actions
        )
        profile.revision += 1
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = utc_now()
        self.db.add(binding)
        self.db.add(profile)
        self.db.flush()
        return profile, binding

    def disable_profile(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> ConnectionProfile:
        """以 CAS 停用档案但保留密钥和历史绑定，确保执行期立即快速失败。"""

        profile = self._profile_for_update(tenant_id, profile_id)
        if profile.revision != expected_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        profile.status = "disabled"
        profile.health_status = "unhealthy"
        profile.health_error_code = "CONNECTION_DISABLED"
        profile.revision += 1
        profile.updated_by_user_id = actor_user_id
        profile.updated_at = utc_now()
        self.db.add(profile)
        return profile

    def _record_runtime_success(self, resolved: ResolvedConnection) -> None:
        """仅用发起外呼时的修订恢复健康，跳过重授权或停用后的迟到回执。"""

        profile = resolved.profile
        if (
            profile.health_status == "healthy"
            and profile.health_error_code is None
            and profile.rate_limited_until is None
        ):
            return
        profile = self._profile_for_update(profile.tenant_id, profile.id)
        if (
            profile.revision != resolved.profile_revision
            or profile.secret_revision != resolved.secret_revision
        ):
            return
        now = utc_now()
        profile.status = "active"
        profile.health_status = "healthy"
        profile.health_error_code = None
        profile.rate_limited_until = None
        profile.last_checked_at = now
        profile.last_healthy_at = now
        profile.revision += 1
        profile.updated_at = now
        self.db.add(profile)

    def resolve(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        required_scope: str,
        required_action: str,
        actor_user_id: str,
        required_permission_code: str = CONNECTION_READ_PERMISSION_CODE,
    ) -> ResolvedConnection:
        """每次调用前重查档案、绑定、状态、scope 和精确密钥修订。"""

        profile = self._profile(tenant_id, profile_id)
        if profile.status == "disabled":
            raise ConnectionError("CONNECTION_DISABLED")
        if profile.status == "reauth_required":
            raise ConnectionError("CONNECTION_REAUTH_REQUIRED")
        agent = self.db.get(AgentProfile, agent_id)
        if agent is None or agent.tenant_id != tenant_id or agent.status != "active":
            raise ConnectionError("CONNECTION_AGENT_NOT_FOUND")
        binding = self.db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.agent_id == agent_id,
                AgentConnectionBinding.profile_id == profile.id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).first()
        if binding is None:
            raise ConnectionError("CONNECTION_BINDING_NOT_FOUND")
        if required_scope not in set(binding.allowed_scopes_json or []):
            raise ConnectionError("CONNECTION_SCOPE_DENIED")
        if required_scope not in set(profile.granted_scopes_json or []):
            raise ConnectionError("CONNECTION_REAUTH_REQUIRED")
        if required_action not in set(profile.tool_allowlist_json or []):
            raise ConnectionError("CONNECTION_ACTION_DENIED")
        _authorize_connection_actor(
            self.db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            permission_code=required_permission_code,
        )
        payload = self._decoded_secret_payload(self._active_secret(profile))
        token = str(payload.get("token") or "")
        if profile.provider == "slack" and not token:
            raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE")
        return ResolvedConnection(
            profile=profile,
            binding=binding,
            token=token,
            profile_revision=profile.revision,
            secret_revision=profile.secret_revision,
            credential_payload={str(key): str(value) for key, value in payload.items()},
        )

    def check_health(self, *, tenant_id: str, profile_id: str) -> ConnectionProfile:
        """探测实际账号并持久化健康、限流或 reauth 状态，不凭空推断成功。"""

        profile = self._profile(tenant_id, profile_id)
        now = utc_now()
        if profile.status == "disabled":
            profile = self._profile_for_update(tenant_id, profile_id)
            profile.health_status = "unhealthy"
            profile.health_error_code = "CONNECTION_DISABLED"
            profile.last_checked_at = now
            profile.revision += 1
            profile.updated_at = now
            self.db.add(profile)
            return profile
        probed_secret_revision = profile.secret_revision
        result, account_id = self._probe_profile(profile)
        profile = self._profile_for_update(tenant_id, profile_id)
        if profile.secret_revision != probed_secret_revision:
            raise ConnectionError("CONNECTION_REVISION_CONFLICT")
        profile.last_checked_at = now
        profile.rate_limited_until = result.rate_limited_until
        if result.success:
            missing = set(profile.required_scopes_json or []) - set(result.granted_scopes)
            if account_id != profile.account_id:
                _mark_reauth(profile, "CONNECTION_ACCOUNT_CHANGED")
            elif missing:
                profile.granted_scopes_json = sorted(result.granted_scopes)
                _mark_reauth(profile, "CONNECTION_SCOPE_MISSING")
            else:
                profile.status = "active"
                profile.health_status = "healthy"
                profile.health_error_code = None
                profile.granted_scopes_json = sorted(result.granted_scopes)
                profile.last_healthy_at = now
        elif result.error_code in {"SLACK_RATE_LIMITED", "WECOM_RATE_LIMITED"}:
            profile.health_status = "degraded"
            profile.health_error_code = _normalize_error(result.error_code)
        elif result.error_code in _REAUTH_ERRORS:
            _mark_reauth(profile, _normalize_error(result.error_code))
        else:
            profile.health_status = "degraded"
            profile.health_error_code = _normalize_error(result.error_code)
        profile.updated_at = now
        profile.revision += 1
        self.db.add(profile)
        return profile

    def _probe_profile(
        self,
        profile: ConnectionProfile,
    ) -> tuple[SlackCallResult | WeComCallResult, str]:
        """按档案 provider 探测真实身份，并返回可与冻结 account_id 比较的值。"""

        payload = self._decoded_secret_payload(self._active_secret(profile))
        if profile.provider == "slack":
            token = str(payload.get("token") or "")
            if not token:
                raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE")
            result = self.slack.auth_test(token)
            return result, str(result.data.get("team_id") or "")
        if profile.provider == "wecom":
            credentials = _wecom_credentials(payload)
            result = self.wecom.application_info(**credentials)
            returned_agent_id = str(result.data.get("agent_id") or "")
            account_id = (
                wecom_account_id(credentials["corp_id"], returned_agent_id)
                if returned_agent_id
                else ""
            )
            return result, account_id
        raise ConnectionError("CONNECTION_PROVIDER_UNSUPPORTED")

    def read_slack_channel(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
        channel_id: str,
    ) -> dict[str, object]:
        """在绑定与 scope 再授权后读取频道，档案并发演进时最多用新凭据重试一次。"""

        for attempt in range(2):
            resolved = self.resolve(
                tenant_id=tenant_id,
                profile_id=profile_id,
                agent_id=agent_id,
                required_scope="channels:read",
                required_action="slack.channel_info",
                actor_user_id=actor_user_id,
            )
            if resolved.profile.provider != "slack":
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            result = self.slack.conversations_info(resolved.token, channel_id=channel_id)
            if result.success:
                self._record_runtime_success(resolved)
                return dict(result.data)
            feedback_applied = self._record_runtime_failure(resolved, result)
            if not feedback_applied and attempt == 0:
                continue
            if not feedback_applied:
                raise ConnectionError("CONNECTION_PROFILE_CHANGED_DURING_CALL")
            raise ConnectionError(_normalize_error(result.error_code))
        raise ConnectionError("CONNECTION_PROFILE_CHANGED_DURING_CALL")

    def read_wecom_application(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, object]:
        """经绑定、scope 和 allowlist 校验后读取企业微信应用详情，修订漂移时最多重试一次。"""

        for attempt in range(2):
            resolved = self.resolve(
                tenant_id=tenant_id,
                profile_id=profile_id,
                agent_id=agent_id,
                required_scope=WECOM_APPLICATION_READ_SCOPE,
                required_action=WECOM_APPLICATION_INFO_ACTION,
                actor_user_id=actor_user_id,
            )
            if resolved.profile.provider != "wecom":
                raise ConnectionError("CONNECTION_PROVIDER_MISMATCH")
            credentials = _wecom_credentials(resolved.credential_payload)
            result = self.wecom.application_info(**credentials)
            if result.success:
                returned_account = wecom_account_id(
                    credentials["corp_id"], str(result.data.get("agent_id") or "")
                )
                if returned_account != resolved.profile.account_id:
                    result = WeComCallResult(False, {}, error_code="CONNECTION_ACCOUNT_CHANGED")
                else:
                    self._record_runtime_success(resolved)
                    return dict(result.data)
            feedback_applied = self._record_runtime_failure(resolved, result)
            if not feedback_applied and attempt == 0:
                continue
            if not feedback_applied:
                raise ConnectionError("CONNECTION_PROFILE_CHANGED_DURING_CALL")
            raise ConnectionError(_normalize_error(result.error_code))
        raise ConnectionError("CONNECTION_PROFILE_CHANGED_DURING_CALL")

    def send_wecom_approved_message(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
        thread_binding_id: str,
        content: str,
        expected_profile_revision: int,
        expected_secret_revision: int,
        expected_binding_revision: int,
    ) -> WeComCallResult:
        """派发已批准的固定线程消息；修订或权限漂移均在外呼前拒绝。"""

        resolved, thread = self._resolve_wecom_message_dispatch(
            tenant_id=tenant_id,
            profile_id=profile_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            thread_binding_id=thread_binding_id,
            expected_profile_revision=expected_profile_revision,
            expected_secret_revision=expected_secret_revision,
            expected_binding_revision=expected_binding_revision,
        )
        try:
            recipient_ref = decrypt_secret(thread.encrypted_recipient_ref)
        except (TypeError, ValueError) as exc:
            raise ConnectionError("CONNECTION_RECIPIENT_UNAVAILABLE") from exc
        credentials = _wecom_credentials(resolved.credential_payload)
        result = self.wecom.send_text(
            **credentials,
            recipient_ref=recipient_ref,
            content=content,
        )
        if result.success and int(result.data.get("invalid_user_count") or 0) == 0:
            self._record_runtime_success(resolved)
            return result
        if result.success:
            result = WeComCallResult(
                False,
                dict(result.data),
                error_code="WECOM_PARTIAL_DELIVERY",
            )
        self._record_runtime_failure(resolved, result)
        return result

    def validate_wecom_message_dispatch(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
        thread_binding_id: str,
        expected_profile_revision: int,
        expected_secret_revision: int,
        expected_binding_revision: int,
    ) -> dict[str, object]:
        """在派发事务中重新验证批准对象，返回不含凭据和外部用户标识的证据。"""

        resolved, thread = self._resolve_wecom_message_dispatch(
            tenant_id=tenant_id,
            profile_id=profile_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            thread_binding_id=thread_binding_id,
            expected_profile_revision=expected_profile_revision,
            expected_secret_revision=expected_secret_revision,
            expected_binding_revision=expected_binding_revision,
        )
        return {
            "permission_code": CONNECTION_WRITE_PERMISSION_CODE,
            "profile_id": resolved.profile.id,
            "profile_revision": resolved.profile_revision,
            "secret_revision": resolved.secret_revision,
            "binding_id": resolved.binding.id,
            "binding_revision": resolved.binding.revision,
            "thread_binding_id": thread.id,
            "action": WECOM_MESSAGE_SEND_ACTION,
        }

    def current_wecom_message_dispatch_evidence(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
        thread_binding_id: str,
    ) -> dict[str, object]:
        """重查当前写授权事实，供纯修订漂移后生成新的显式审批而不触发外呼。"""

        resolved, thread = self._resolve_wecom_message_dispatch(
            tenant_id=tenant_id,
            profile_id=profile_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            thread_binding_id=thread_binding_id,
            expected_profile_revision=None,
            expected_secret_revision=None,
            expected_binding_revision=None,
        )
        return {
            "permission_code": CONNECTION_WRITE_PERMISSION_CODE,
            "profile_id": resolved.profile.id,
            "profile_revision": resolved.profile_revision,
            "secret_revision": resolved.secret_revision,
            "binding_id": resolved.binding.id,
            "binding_revision": resolved.binding.revision,
            "thread_binding_id": thread.id,
            "action": WECOM_MESSAGE_SEND_ACTION,
        }

    def _resolve_wecom_message_dispatch(
        self,
        *,
        tenant_id: str,
        profile_id: str,
        agent_id: str,
        actor_user_id: str,
        thread_binding_id: str,
        expected_profile_revision: int | None,
        expected_secret_revision: int | None,
        expected_binding_revision: int | None,
    ) -> tuple[ResolvedConnection, ConnectorThreadBinding]:
        """解析并核对企业微信派发主体、动作、修订和固定线程目标。"""

        resolved = self.resolve(
            tenant_id=tenant_id,
            profile_id=profile_id,
            agent_id=agent_id,
            required_scope=WECOM_APPLICATION_READ_SCOPE,
            required_action=WECOM_MESSAGE_SEND_ACTION,
            actor_user_id=actor_user_id,
            required_permission_code=CONNECTION_WRITE_PERMISSION_CODE,
        )
        if WECOM_MESSAGE_SEND_ACTION not in set(resolved.binding.allowed_actions_json or []):
            raise ConnectionError("CONNECTION_ACTION_DENIED")
        if any(
            expected is not None and actual != expected
            for actual, expected in (
                (resolved.profile_revision, expected_profile_revision),
                (resolved.secret_revision, expected_secret_revision),
                (resolved.binding.revision, expected_binding_revision),
            )
        ):
            raise ConnectionError("CONNECTION_APPROVAL_REVISION_CHANGED")
        thread = self.db.get(ConnectorThreadBinding, thread_binding_id)
        if (
            thread is None
            or thread.tenant_id != tenant_id
            or thread.profile_id != profile_id
            or thread.agent_id != agent_id
            or thread.status != "active"
        ):
            raise ConnectionError("CONNECTION_APPROVAL_TARGET_CHANGED")
        return resolved, thread

    def _profile(self, tenant_id: str, profile_id: str) -> ConnectionProfile:
        """以 tenant 和主键双重约束读取档案，跨租户统一表现为不存在。"""

        row = self.db.get(ConnectionProfile, profile_id)
        if row is None or row.tenant_id != tenant_id:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        return row

    def _profile_for_update(self, tenant_id: str, profile_id: str) -> ConnectionProfile:
        """以数据库行锁和最新值读取待变更档案，避免并发 CAS 丢失更新。"""

        row = self.db.exec(
            select(ConnectionProfile)
            .where(
                ConnectionProfile.id == profile_id,
                ConnectionProfile.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            raise ConnectionError("CONNECTION_PROFILE_NOT_FOUND")
        return row

    def _active_secret(self, profile: ConnectionProfile) -> ConnectionSecret:
        """读取档案当前精确密钥修订，不允许隐式回退旧凭据。"""

        secret = self.db.exec(
            select(ConnectionSecret).where(
                ConnectionSecret.tenant_id == profile.tenant_id,
                ConnectionSecret.provider == profile.provider,
                ConnectionSecret.reference_id == profile.secret_ref_id,
                ConnectionSecret.revision == profile.secret_revision,
                ConnectionSecret.status == "active",
            )
        ).first()
        if secret is None:
            raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE")
        return secret

    @staticmethod
    def _decoded_secret_payload(secret: ConnectionSecret) -> dict[str, object]:
        """解密并验证凭据 JSON 对象，不把原始密文或解析异常向上游传播。"""

        try:
            payload = json.loads(decrypt_secret(secret.encrypted_payload))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE") from exc
        if not isinstance(payload, dict):
            raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE")
        return dict(payload)

    def _record_runtime_failure(
        self,
        resolved: ResolvedConnection,
        result: SlackCallResult | WeComCallResult,
    ) -> bool:
        """仅在档案仍是外呼时修订时回写失效或限流，避免迟到回执覆盖新凭据。"""

        profile = self._profile_for_update(resolved.profile.tenant_id, resolved.profile.id)
        if (
            profile.revision != resolved.profile_revision
            or profile.secret_revision != resolved.secret_revision
        ):
            return False
        profile.last_checked_at = utc_now()
        profile.rate_limited_until = result.rate_limited_until
        if result.error_code in _REAUTH_ERRORS or result.error_code == "missing_scope":
            _mark_reauth(profile, _normalize_error(result.error_code))
        else:
            profile.health_status = "degraded"
            profile.health_error_code = _normalize_error(result.error_code)
        profile.revision += 1
        profile.updated_at = utc_now()
        self.db.add(profile)
        return True


def authorize_connection_read_actor(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
) -> None:
    """按活动员工当前组织归属校验外部读取权，平台角色和连接管理权均不旁路。"""

    _authorize_connection_actor(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        permission_code=CONNECTION_READ_PERMISSION_CODE,
    )


def authorize_connection_write_actor(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
) -> None:
    """校验办理人当前仍具有外部写批准权，不允许管理角色隐式旁路。"""

    _authorize_connection_actor(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        permission_code=CONNECTION_WRITE_PERMISSION_CODE,
    )


def _authorize_connection_actor(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    permission_code: str,
) -> None:
    """按活动员工及当前组织任职解析指定外部连接业务权限。"""

    employee = db.exec(
        select(EmployeeProfile).where(
            EmployeeProfile.tenant_id == tenant_id,
            EmployeeProfile.user_id == actor_user_id,
            EmployeeProfile.status == "active",
        )
    ).first()
    if employee is None:
        raise ConnectionError("CONNECTION_ACTOR_PERMISSION_REQUIRED")
    org_ids = set(
        db.exec(
            select(MemberOrgAssignment.org_unit_id).where(
                MemberOrgAssignment.tenant_id == tenant_id,
                MemberOrgAssignment.employee_profile_id == employee.id,
                *current_assignment_predicates(),
            )
        ).all()
    )
    permissions = user_permission_codes(
        db,
        tenant_id=tenant_id,
        user_id=actor_user_id,
        organization_unit_ids=org_ids,
    )
    if permission_code not in permissions:
        raise ConnectionError("CONNECTION_ACTOR_PERMISSION_REQUIRED")


def _mark_reauth(profile: ConnectionProfile, error_code: str) -> None:
    """统一设置需要重新授权的不可执行状态。"""

    profile.status = "reauth_required"
    profile.health_status = "unhealthy"
    profile.health_error_code = error_code


def _normalize_error(error_code: str | None) -> str:
    """把 provider 错误归一为稳定平台代码，同时保留可诊断类别。"""

    mapping = {
        "invalid_auth": "CONNECTION_INVALID_AUTH",
        "token_expired": "CONNECTION_TOKEN_EXPIRED",
        "token_revoked": "CONNECTION_TOKEN_REVOKED",
        "account_inactive": "CONNECTION_ACCOUNT_INACTIVE",
        "missing_scope": "CONNECTION_SCOPE_MISSING",
        "WECOM_40001": "CONNECTION_INVALID_AUTH",
        "WECOM_40013": "CONNECTION_ACCOUNT_INVALID",
        "WECOM_60020": "CONNECTION_TRUSTED_IP_REQUIRED",
        "WECOM_60021": "CONNECTION_SCOPE_MISSING",
        "WECOM_48002": "CONNECTION_SCOPE_MISSING",
        "WECOM_RATE_LIMITED": "CONNECTION_RATE_LIMITED",
        "WECOM_UNAVAILABLE": "CONNECTION_UPSTREAM_UNAVAILABLE",
        "WECOM_INVALID_RESPONSE": "CONNECTION_UPSTREAM_INVALID_RESPONSE",
    }
    return mapping.get(str(error_code or ""), str(error_code or "CONNECTION_UPSTREAM_ERROR"))


def _normalize_wecom_credentials(
    corp_id: str,
    agent_id: str,
    corp_secret: str,
) -> dict[str, str]:
    """清理并校验企业微信三元凭据，拒绝空值和异常长度。"""

    credentials = {
        "corp_id": corp_id.strip(),
        "agent_id": agent_id.strip(),
        "corp_secret": corp_secret.strip(),
    }
    if not all(credentials.values()):
        raise ConnectionError("CONNECTION_SECRET_REQUIRED")
    if len(credentials["corp_id"]) > 128 or len(credentials["agent_id"]) > 64:
        raise ConnectionError("CONNECTION_CREDENTIAL_INVALID")
    if len(credentials["corp_secret"]) > 4096:
        raise ConnectionError("CONNECTION_CREDENTIAL_INVALID")
    return credentials


def _wecom_credentials(payload: dict[str, object]) -> dict[str, str]:
    """从解密对象提取企业微信凭据，缺字段时统一按凭据不可用拒绝。"""

    try:
        if any(payload.get(name) is None for name in ("corp_id", "agent_id", "corp_secret")):
            raise KeyError("missing wecom credential")
        return _normalize_wecom_credentials(
            str(payload["corp_id"]),
            str(payload["agent_id"]),
            str(payload["corp_secret"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectionError("CONNECTION_SECRET_UNAVAILABLE") from exc


def _normalize_wecom_callback(token: str | None, encoding_aes_key: str | None) -> dict[str, str]:
    """要求企业微信回调 Token 与 43 字符 EncodingAESKey 成对出现。"""

    normalized_token = str(token or "").strip()
    normalized_key = str(encoding_aes_key or "").strip()
    if not normalized_token and not normalized_key:
        return {}
    if not normalized_token or len(normalized_token) > 128 or len(normalized_key) != 43:
        raise ConnectionError("WECOM_CALLBACK_CONFIG_INVALID")
    return {
        "callback_token": normalized_token,
        "callback_encoding_aes_key": normalized_key,
    }


def _wecom_callback(payload: dict[str, object]) -> dict[str, str]:
    """从已解密档案中读取完整回调配置，缺失时稳定拒绝公开回调。"""

    callback = _normalize_wecom_callback(
        str(payload.get("callback_token") or ""),
        str(payload.get("callback_encoding_aes_key") or ""),
    )
    if not callback:
        raise ConnectionError("WECOM_CALLBACK_NOT_CONFIGURED")
    return callback
