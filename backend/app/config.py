"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : config.py
@CallChain  : Environment/.env → Settings/get_settings → app/main/db/api
@Description: 管理从环境变量和 `.env` 加载的应用配置。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.brand import desktop_env_value


DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    app_env: str = "production"
    single_port: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 5137
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    enterprise_host: str = "0.0.0.0"
    enterprise_port: int = 5137
    auto_restart: bool = True
    dev_startup_timeout: float = 180.0
    api_base_url: str = ""
    vite_api_base_url: str = ""
    app_name: str = "共格·序伴"
    database_url: str = "sqlite:///./gongge_xuban.db"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 30
    database_pool_recycle_seconds: int = 1800
    app_secret: str = "change-me-in-development"
    demo_model_base_url: str = "http://127.0.0.1:52010/v1"
    demo_model_name: str = "qwen3.6-27b"
    demo_model_api_key: str = ""
    model_api_timeout_seconds: float = 600.0
    model_thinking_mode: str = ""
    model_thinking_models: str = ""
    tool_timeout_seconds: float = 8.0
    tool_base_url: str = "http://localhost:5137"
    public_mock_api_key: str
    public_mock_llm_enabled: bool = False
    cors_origins: str = "http://localhost:5137,http://127.0.0.1:5137"
    general_skill_runtime_python: str = ""
    general_skill_runtime_venv: str = ""
    general_skill_runtime_packages: str = "requests,httpx"
    general_skill_runtime_auto_install: bool = True
    general_skill_pip_index_url: str = ""
    general_skill_pip_timeout_seconds: int = 180
    general_skill_network_install: bool = False

    model_config = SettingsConfigDict(
        env_file=desktop_env_value("DOTENV", str(DEFAULT_DOTENV_PATH)),
        env_file_encoding="utf-8", extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """拆分并清理跨域来源配置中的非空地址。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def normalized_tool_base_url(self) -> str:
        """移除工具服务基础地址末尾的斜杠，便于拼接请求路径。"""
        return self.tool_base_url.rstrip("/")

    @field_validator("public_mock_api_key")
    @classmethod
    def validate_public_mock_api_key(cls, value: str) -> str:
        """拒绝空的公网 mock 密钥，避免服务意外匿名开放。"""
        value = value.strip()
        if not value:
            raise ValueError("PUBLIC_MOCK_API_KEY must be configured")
        return value

    @property
    def general_skill_runtime_package_list(self) -> list[str]:
        """拆分并清理通用技能运行时配置中的非空包名。"""
        return [item.strip() for item in self.general_skill_runtime_packages.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """创建并缓存从环境变量和配置文件加载的应用设置。"""
    return Settings()
