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
    assert settings.dynamic_task_router_shadow_timeout_seconds == 2.0
    assert settings.dynamic_task_router_shadow_min_confidence == 0.7


def test_active_production_tool_base_url_is_reachable_from_the_host() -> None:
    settings = Settings()
    hostname = urlsplit(settings.normalized_tool_base_url).hostname

    assert settings.app_env == "production"
    assert hostname not in {None, "localhost", "127.0.0.1", "0.0.0.0", "::1"}
