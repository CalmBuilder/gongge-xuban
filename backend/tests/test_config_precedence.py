"""
@Time       : 2026/07/27 18:10
@Author     : zhanglp8181
@File       : test_config_precedence.py
@CallChain  : pytest → Settings → 环境变量/.env/代码默认值
@Description: 验证应用配置优先级、必填约束及安全的本机开发默认值。
"""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_DOTENV_PATH, Settings


LAUNCHER_ENV_KEYS = (
    "APP_ENV",
    "SINGLE_PORT",
    "APP_HOST",
    "APP_PORT",
    "BACKEND_HOST",
    "BACKEND_PORT",
    "ENTERPRISE_HOST",
    "ENTERPRISE_PORT",
    "AUTO_RESTART",
    "DEV_STARTUP_TIMEOUT",
)


def clear_launcher_env(monkeypatch) -> None:
    for key in LAUNCHER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_launcher_defaults_come_from_settings(monkeypatch) -> None:
    clear_launcher_env(monkeypatch)

    settings = Settings(_env_file=None, public_mock_api_key="test-key")

    assert settings.app_env == "production"
    assert settings.single_port is True
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 5137


def test_demo_model_default_uses_loopback_address(monkeypatch) -> None:
    """未显式配置模型服务时只连接本机默认端口，不绑定外部部署地址。"""
    monkeypatch.delenv("DEMO_MODEL_BASE_URL", raising=False)

    settings = Settings(_env_file=None, public_mock_api_key="test-key")

    assert settings.demo_model_base_url == "http://127.0.0.1:52010/v1"


def test_backend_dotenv_overrides_code_defaults(tmp_path: Path, monkeypatch) -> None:
    clear_launcher_env(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        'APP_HOST="127.0.0.1"\nAPP_PORT="6200"\nPUBLIC_MOCK_API_KEY="test-key"\n',
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 6200


def test_os_environment_overrides_backend_dotenv(tmp_path: Path, monkeypatch) -> None:
    clear_launcher_env(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text('APP_PORT="6200"\nPUBLIC_MOCK_API_KEY="test-key"\n', encoding="utf-8")
    monkeypatch.setenv("APP_PORT", "6300")

    assert Settings(_env_file=dotenv).app_port == 6300


def test_default_dotenv_path_is_backend_env() -> None:
    assert DEFAULT_DOTENV_PATH.is_absolute()
    assert DEFAULT_DOTENV_PATH == Path(__file__).resolve().parents[1] / ".env"


def test_public_mock_settings_require_an_explicit_api_key() -> None:
    settings = Settings(_env_file=None, public_mock_api_key="test-public-mock-key")

    assert settings.public_mock_api_key == "test-public-mock-key"
    assert settings.public_mock_llm_enabled is False


def test_public_mock_settings_reject_a_blank_api_key() -> None:
    with pytest.raises(ValidationError, match="PUBLIC_MOCK_API_KEY must be configured"):
        Settings(_env_file=None, public_mock_api_key="   ")


def test_dynamic_task_router_shadow_is_safe_by_default(monkeypatch) -> None:
    """未显式配置时关闭动态任务 shadow，并保持有界超时与置信度门禁。"""

    monkeypatch.delenv("DYNAMIC_TASK_ROUTER_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("DYNAMIC_TASK_ROUTER_SHADOW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DYNAMIC_TASK_ROUTER_SHADOW_MIN_CONFIDENCE", raising=False)

    settings = Settings(_env_file=None, public_mock_api_key="test-key")

    assert settings.dynamic_task_router_shadow_enabled is False
    assert settings.dynamic_task_execution_enabled is False
    assert settings.dynamic_task_steering_enabled is False
    assert settings.dynamic_task_external_write_enabled is False
    assert settings.dynamic_task_tenant_allowlist == ""
    assert settings.dynamic_task_agent_allowlist == ""
    assert settings.dynamic_task_rollout_allows("tenant_demo", "agent_demo") is False
    assert settings.dynamic_task_signal_dispatch_workers == 4
    assert settings.dynamic_task_signal_dispatch_capacity == 16
    assert settings.dynamic_task_alert_signal_backlog_threshold == 0
    assert settings.dynamic_task_alert_dead_letter_threshold == 0
    assert settings.dynamic_task_alert_unknown_operation_threshold == 0
    assert settings.dynamic_task_alert_publication_backlog_threshold == 0
    assert settings.dynamic_task_alert_waiting_age_seconds == 0
    assert settings.dynamic_task_alert_thresholds_configured is False
    assert settings.dynamic_task_max_active_per_tenant == 0
    assert settings.dynamic_task_max_active_per_agent == 0
    assert settings.dynamic_task_max_active_per_user == 0
    assert settings.dynamic_task_max_active_per_tool == 0
    assert settings.dynamic_task_quota_limits_configured is False
    assert settings.dynamic_task_router_shadow_timeout_seconds == 2.0
    assert settings.dynamic_task_router_shadow_min_confidence == 0.7


@pytest.mark.parametrize(
    ("tenant_allowlist", "agent_allowlist", "tenant_id", "agent_id", "allowed"),
    [
        ("tenant_a", "agent_a", "tenant_a", "agent_a", True),
        ("tenant_a", "agent_a", "tenant_b", "agent_a", False),
        ("tenant_a", "agent_a", "tenant_a", "agent_b", False),
        ("*", "agent_a", "tenant_b", "agent_a", True),
        ("tenant_a", "*", "tenant_a", "agent_b", True),
        ("", "", "tenant_a", "agent_a", False),
    ],
)
def test_dynamic_task_rollout_requires_tenant_and_agent_allowlists(
    tenant_allowlist: str,
    agent_allowlist: str,
    tenant_id: str,
    agent_id: str,
    allowed: bool,
) -> None:
    """验证打开总开关后仍须同时命中 tenant 与 Agent 灰度边界。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        dynamic_task_execution_enabled=True,
        dynamic_task_tenant_allowlist=tenant_allowlist,
        dynamic_task_agent_allowlist=agent_allowlist,
        dynamic_task_alert_signal_backlog_threshold=10,
        dynamic_task_alert_dead_letter_threshold=1,
        dynamic_task_alert_unknown_operation_threshold=1,
        dynamic_task_alert_publication_backlog_threshold=5,
        dynamic_task_alert_waiting_age_seconds=3600,
        dynamic_task_max_active_per_tenant=16,
        dynamic_task_max_active_per_agent=8,
        dynamic_task_max_active_per_user=4,
        dynamic_task_max_active_per_tool=4,
    )

    assert settings.dynamic_task_rollout_allows(tenant_id, agent_id) is allowed


def test_dynamic_task_rollout_global_kill_switch_overrides_wildcards() -> None:
    """验证显式全量灰度也不能绕过关闭的全局执行开关。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        dynamic_task_execution_enabled=False,
        dynamic_task_tenant_allowlist="*",
        dynamic_task_agent_allowlist="*",
    )

    assert settings.dynamic_task_rollout_allows("tenant_a", "agent_a") is False


def test_dynamic_task_rollout_rejects_allowlists_without_alert_thresholds() -> None:
    """验证 tenant/Agent 即使命中，也不能在停止阈值未配置时开放生产执行。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        dynamic_task_execution_enabled=True,
        dynamic_task_tenant_allowlist="tenant_a",
        dynamic_task_agent_allowlist="agent_a",
    )

    assert settings.dynamic_task_alert_thresholds_configured is False
    assert settings.dynamic_task_rollout_allows("tenant_a", "agent_a") is False


def test_dynamic_task_rollout_rejects_allowlists_without_quota_limits() -> None:
    """验证停止阈值已配置但四级配额为空时仍不能开放动态执行。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        dynamic_task_execution_enabled=True,
        dynamic_task_tenant_allowlist="tenant_a",
        dynamic_task_agent_allowlist="agent_a",
        dynamic_task_alert_signal_backlog_threshold=10,
        dynamic_task_alert_dead_letter_threshold=1,
        dynamic_task_alert_unknown_operation_threshold=1,
        dynamic_task_alert_publication_backlog_threshold=5,
        dynamic_task_alert_waiting_age_seconds=3600,
    )

    assert settings.dynamic_task_alert_thresholds_configured is True
    assert settings.dynamic_task_quota_limits_configured is False
    assert settings.dynamic_task_rollout_allows("tenant_a", "agent_a") is False


def test_dynamic_task_signal_capacity_must_cover_all_workers() -> None:
    """验证 Signal 队列容量配置不能小于派发 worker 数。"""

    with pytest.raises(ValidationError, match="DISPATCH_CAPACITY"):
        Settings(
            _env_file=None,
            public_mock_api_key="test-key",
            dynamic_task_signal_dispatch_workers=8,
            dynamic_task_signal_dispatch_capacity=4,
        )


def test_dynamic_task_alert_thresholds_reject_negative_values() -> None:
    """验证告警阈值只能是未配置的零或正数，拒绝含义不明的负值。"""

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings(
            _env_file=None,
            public_mock_api_key="test-key",
            dynamic_task_alert_waiting_age_seconds=-1,
        )


@pytest.mark.parametrize(
    ("redirect_uri", "configured"),
    [
        ("https://app.example.com/api/slack/callback", True),
        ("http://app.example.com/api/slack/callback", False),
        ("", False),
    ],
)
def test_slack_oauth_requires_complete_https_server_configuration(
    redirect_uri: str,
    configured: bool,
) -> None:
    """验证生产 OAuth 只有 client 两项和 HTTPS callback 同时存在时才启用。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        slack_oauth_client_id="client-id",
        slack_oauth_client_secret="client-secret",
        slack_oauth_redirect_uri=redirect_uri,
    )

    assert settings.slack_oauth_configured is configured


@pytest.mark.parametrize(
    ("app_secret", "configured"),
    [
        ("connection-profile-production-key-32-bytes", True),
        ("change-me-in-development", False),
        ("short", False),
    ],
)
def test_connection_secret_backend_rejects_placeholder_or_short_master_key(
    app_secret: str,
    configured: bool,
) -> None:
    """验证连接凭据写入门禁不会接受可预测或低强度主密钥。"""

    settings = Settings(
        _env_file=None,
        public_mock_api_key="test-key",
        app_secret=app_secret,
    )

    assert settings.connection_secret_backend_configured is configured


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("general_skill_import_tenant_active_limit", 9),
        ("general_skill_import_user_active_limit", 5),
        ("general_skill_import_tenant_staged_bytes", 2 * 1024 * 1024 * 1024 + 1),
        ("general_skill_import_user_staged_bytes", 500 * 1024 * 1024 + 1),
    ],
)
def test_general_skill_import_quota_cannot_exceed_platform_hard_limit(
    field: str,
    value: int,
) -> None:
    """验证部署只能收窄导入预算，不能用环境变量越过平台安全上限。"""

    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_mock_api_key="test-key", **{field: value})


def test_active_production_tool_base_url_is_reachable_from_the_host() -> None:
    settings = Settings()
    hostname = urlsplit(settings.normalized_tool_base_url).hostname

    assert settings.app_env == "production"
    assert hostname not in {None, "localhost", "127.0.0.1", "0.0.0.0", "::1"}
