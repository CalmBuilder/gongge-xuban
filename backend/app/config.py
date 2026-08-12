"""
@Time       : 2026/07/18 09:22
@Author     : zhanglp8181
@File       : config.py
@CallChain  : Environment/.env → Settings/get_settings → app/main/db/api
@Description: 管理从环境变量和 `.env` 加载的应用配置。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
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
    dynamic_task_router_shadow_enabled: bool = False
    dynamic_task_execution_enabled: bool = False
    dynamic_task_steering_enabled: bool = False
    dynamic_task_skill_loading_enabled: bool = False
    dynamic_task_external_write_enabled: bool = False
    dynamic_task_standing_approval_enabled: bool = False
    dynamic_task_explore_enabled: bool = False
    dynamic_task_managed_workspace_enabled: bool = False
    dynamic_task_managed_workspace_root: str = "./data/managed-code-workspaces"
    general_skill_agent_proposal_enabled: bool = False
    general_skill_agent_proposal_approval_ttl_seconds: int = Field(
        default=900, ge=30, le=86_400
    )
    dynamic_task_tenant_allowlist: str = ""
    dynamic_task_agent_allowlist: str = ""
    dynamic_task_signal_dispatch_workers: int = Field(default=4, ge=1, le=64)
    dynamic_task_signal_dispatch_capacity: int = Field(default=16, ge=1, le=4096)
    dynamic_task_alert_signal_backlog_threshold: int = Field(default=0, ge=0)
    dynamic_task_alert_dead_letter_threshold: int = Field(default=0, ge=0)
    dynamic_task_alert_unknown_operation_threshold: int = Field(default=0, ge=0)
    dynamic_task_alert_publication_backlog_threshold: int = Field(default=0, ge=0)
    dynamic_task_alert_waiting_age_seconds: int = Field(default=0, ge=0)
    dynamic_task_max_active_per_tenant: int = Field(default=0, ge=0, le=4096)
    dynamic_task_max_active_per_agent: int = Field(default=0, ge=0, le=1024)
    dynamic_task_max_active_per_user: int = Field(default=0, ge=0, le=256)
    dynamic_task_max_active_per_tool: int = Field(default=0, ge=0, le=1024)
    slack_oauth_client_id: str = ""
    slack_oauth_client_secret: str = ""
    slack_oauth_redirect_uri: str = ""
    dynamic_task_router_shadow_timeout_seconds: float = Field(default=2.0, ge=0.1, le=30.0)
    dynamic_task_router_shadow_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
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
    general_skill_import_v2_enabled: bool = False
    general_skill_object_store_path: str = "./data/general-skill-objects"
    general_skill_https_allowed_hosts: str = ""
    general_skill_dns_resolver: str = Field(default="system", pattern="^(system|cloudflare_doh)$")
    general_skill_import_tenant_active_limit: int = Field(default=4, ge=1, le=8)
    general_skill_import_user_active_limit: int = Field(default=2, ge=1, le=4)
    general_skill_import_tenant_staged_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1,
        le=2 * 1024 * 1024 * 1024,
    )
    general_skill_import_user_staged_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        le=500 * 1024 * 1024,
    )
    general_skill_import_async_enabled: bool = True
    general_skill_resolver_v2_shadow: bool = False
    general_skill_resolver_v2_enabled: bool = False
    general_skill_dynamic_guidance_enabled: bool = False
    general_skill_catalog_top_k: int = Field(default=12, ge=1, le=24)
    general_skill_instruction_char_limit: int = Field(default=48_000, ge=1_000, le=96_000)
    general_skill_total_instruction_char_limit: int = Field(
        default=64_000, ge=1_000, le=192_000
    )
    general_skill_dependency_max_depth: int = Field(default=4, ge=1, le=8)
    general_skill_max_loaded_per_turn: int = Field(default=12, ge=1, le=16)
    general_skill_resource_read_bytes: int = Field(default=64 * 1024, ge=1_024, le=256 * 1024)
    general_skill_import_worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    general_skill_import_worker_lease_seconds: int = Field(default=300, ge=180, le=1800)

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

    @staticmethod
    def _identifier_allowlist(value: str) -> frozenset[str]:
        """把逗号分隔灰度标识规范化为不可变集合，保留星号作为显式全量选择。"""

        return frozenset(item.strip() for item in value.split(",") if item.strip())

    def dynamic_task_rollout_allows(self, tenant_id: str, agent_id: str) -> bool:
        """要求总开关、双灰度名单和生产告警阈值同时就绪，任一缺失均默认拒绝。"""

        if (
            not self.dynamic_task_execution_enabled
            or not self.dynamic_task_alert_thresholds_configured
            or not self.dynamic_task_quota_limits_configured
        ):
            return False
        tenants = self._identifier_allowlist(self.dynamic_task_tenant_allowlist)
        agents = self._identifier_allowlist(self.dynamic_task_agent_allowlist)
        return ("*" in tenants or tenant_id in tenants) and (
            "*" in agents or agent_id in agents
        )

    @property
    def dynamic_task_alert_thresholds_configured(self) -> bool:
        """要求五项运行停止阈值均由部署方填写正数，零保持显式未就绪语义。"""

        return all(
            value > 0
            for value in (
                self.dynamic_task_alert_signal_backlog_threshold,
                self.dynamic_task_alert_dead_letter_threshold,
                self.dynamic_task_alert_unknown_operation_threshold,
                self.dynamic_task_alert_publication_backlog_threshold,
                self.dynamic_task_alert_waiting_age_seconds,
            )
        )

    @property
    def dynamic_task_quota_limits_configured(self) -> bool:
        """要求 tenant、Agent、用户和工具四级上限均为正数，零表示发布门禁未就绪。"""

        return all(
            value > 0
            for value in (
                self.dynamic_task_max_active_per_tenant,
                self.dynamic_task_max_active_per_agent,
                self.dynamic_task_max_active_per_user,
                self.dynamic_task_max_active_per_tool,
            )
        )

    @model_validator(mode="after")
    def validate_dynamic_task_dispatch_capacity(self) -> "Settings":
        """拒绝小于 worker 数的队列容量，避免配置后部分 worker 永远无法入队。"""

        if self.dynamic_task_signal_dispatch_capacity < self.dynamic_task_signal_dispatch_workers:
            raise ValueError(
                "DYNAMIC_TASK_SIGNAL_DISPATCH_CAPACITY must be greater than or equal to "
                "DYNAMIC_TASK_SIGNAL_DISPATCH_WORKERS"
            )
        return self

    @field_validator("public_mock_api_key")
    @classmethod
    def validate_public_mock_api_key(cls, value: str) -> str:
        """拒绝空的公网 mock 密钥，避免服务意外匿名开放。"""
        value = value.strip()
        if not value:
            raise ValueError("PUBLIC_MOCK_API_KEY must be configured")
        return value

    @field_validator("dynamic_task_managed_workspace_root")
    @classmethod
    def validate_managed_workspace_root(cls, value: str) -> str:
        """拒绝空值、文件系统根和用户主目录作为受管代码工作区边界。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT must be configured")
        resolved = Path(normalized).expanduser().resolve()
        if resolved in {Path("/").resolve(), Path.home().resolve()}:
            raise ValueError("DYNAMIC_TASK_MANAGED_WORKSPACE_ROOT is too broad")
        return normalized

    @property
    def general_skill_runtime_package_list(self) -> list[str]:
        """拆分并清理通用技能运行时配置中的非空包名。"""
        return [item.strip() for item in self.general_skill_runtime_packages.split(",") if item.strip()]

    @property
    def general_skill_https_allowed_host_set(self) -> frozenset[str]:
        """规范化管理员配置的公开 HTTPS Skill 包主机白名单。"""

        return frozenset(
            item.strip().lower().rstrip(".")
            for item in self.general_skill_https_allowed_hosts.split(",")
            if item.strip()
        )

    @property
    def slack_oauth_configured(self) -> bool:
        """仅在三项服务端 OAuth 配置齐全且回调使用 HTTPS 时启用 Slack 安装入口。"""

        return bool(
            self.slack_oauth_client_id.strip()
            and self.slack_oauth_client_secret.strip()
            and self.slack_oauth_redirect_uri.strip().startswith("https://")
        )

    @property
    def connection_secret_backend_configured(self) -> bool:
        """要求连接凭据使用非占位且长度足够的应用主密钥派生加密键。"""

        value = self.app_secret.strip()
        return len(value) >= 32 and value != "change-me-in-development"


@lru_cache
def get_settings() -> Settings:
    """创建并缓存从环境变量和配置文件加载的应用设置。"""
    return Settings()
