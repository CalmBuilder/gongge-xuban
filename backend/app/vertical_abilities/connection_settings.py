"""
@Time       : 2026/09/03 15:10
@Author     : zhanglp8181
@File       : connection_settings.py
@CallChain  : Agent-Reach 平台审计 → 网站连接目录 → 连接 API/连接向导/垂直能力适配器
@Description: 声明全网情报员各网站的真实连接方式、凭据字段和后续能力绑定，不把不同平台强行抽象成用户名密码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ConnectionAuthMethod = Literal["api_token", "cookie", "browser_session", "oauth"]
ConnectionTransport = Literal["local_loopback", "browser_relay"]
ConnectionImplementationStatus = Literal["available", "adapter_pending"]


@dataclass(frozen=True, slots=True)
class ConnectionFieldDefinition:
    """声明一种连接方式所需的单个输入字段，不包含任何运行时值。"""

    key: str
    label: str
    placeholder: str
    description: str
    secret: bool = True
    required: bool = True


@dataclass(frozen=True, slots=True)
class VerticalAbilityConnectionDefinition:
    """声明一个网站连接卡片的认证方式、能力范围和实现状态。"""

    platform_id: str
    platform_name: str
    description: str
    capability_ids: tuple[str, ...]
    auth_methods: tuple[ConnectionAuthMethod, ...]
    fields: tuple[ConnectionFieldDefinition, ...] = ()
    setup_url: str | None = None
    setup_link_label: str | None = None
    implementation_status: ConnectionImplementationStatus = "adapter_pending"
    implementation_message: str = "连接保存后仍需对应平台适配器完成读取。"

    def fields_for(self, auth_method: ConnectionAuthMethod) -> tuple[ConnectionFieldDefinition, ...]:
        """返回指定连接方式的输入字段，字段定义按平台稳定键筛选。"""

        prefixes = {
            "api_token": ("access_token", "client_id", "client_secret"),
            "cookie": ("cookie", "auth_token", "ct0", "reddit_session"),
            "browser_session": ("session_reference",),
            "oauth": (),
        }
        allowed = prefixes[auth_method]
        return tuple(field for field in self.fields if field.key in allowed)


def _field(
    key: str,
    label: str,
    placeholder: str,
    description: str,
    *,
    required: bool = True,
) -> ConnectionFieldDefinition:
    """构造默认的敏感连接字段定义，统一前端掩码输入和后端加密存储语义。"""

    return ConnectionFieldDefinition(
        key=key,
        label=label,
        placeholder=placeholder,
        description=description,
        required=required,
    )


PLATFORM_CONNECTION_DEFINITIONS: tuple[VerticalAbilityConnectionDefinition, ...] = (
    VerticalAbilityConnectionDefinition(
        platform_id="github",
        platform_name="GitHub",
        description="连接个人 GitHub Token 后，可在授权范围内读取私有仓库、Issue 和搜索结果。",
        capability_ids=("intelligence.github_repo", "intelligence.github_search"),
        auth_methods=("api_token",),
        fields=(_field("access_token", "GitHub Token", "粘贴 GitHub fine-grained token", "只授予所需的只读仓库权限。"),),
        setup_url="https://github.com/settings/personal-access-tokens",
        setup_link_label="GitHub Token 设置",
        implementation_status="available",
        implementation_message="GitHub Token 连接可用于读取公开和授权范围内的仓库信息。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="twitter",
        platform_name="Twitter/X",
        description="连接 Twitter/X 登录态后，可读取推文、用户资料和时间线；搜索能力取决于当前后端可用性。",
        capability_ids=("intelligence.twitter_read", "intelligence.twitter_search"),
        auth_methods=("cookie", "browser_session"),
        fields=(
            _field("auth_token", "auth_token", "粘贴 Cookie-Editor 导出的 auth_token", "Twitter/X 显式登录 Cookie。", required=False),
            _field("ct0", "ct0", "粘贴 Cookie-Editor 导出的 ct0", "Twitter/X CSRF Cookie。", required=False),
            _field("cookie", "Cookie 字符串", "粘贴 Twitter/X 同域 Cookie", "仅接受用户主动导出的平台 Cookie。", required=False),
            _field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "不直接输入密码，引用用户明确控制的 Chrome 会话。"),
        ),
        setup_url="https://x.com/home",
        setup_link_label="打开 Twitter/X 登录页",
        implementation_status="available",
        implementation_message="已支持：浏览器会话走 OpenCLI；服务器模式可导入 auth_token 与 ct0 Cookie，凭据只在当前调用子进程中使用。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="reddit",
        platform_name="Reddit",
        description="Reddit 匿名接口容易被 403 拦截，需要用户自己的浏览器登录态或显式 Cookie。",
        capability_ids=("intelligence.reddit_search", "intelligence.reddit_read"),
        auth_methods=("browser_session", "cookie"),
        fields=(
            _field("reddit_session", "reddit_session", "粘贴 Cookie-Editor 导出的 reddit_session", "rdt-cli 使用的 Reddit 会话 Cookie。"),
            _field("cookie", "Cookie 字符串", "粘贴 Reddit 同域 Cookie", "仅接受用户主动导出的平台 Cookie。", required=False),
            _field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "复用用户已登录的 Chrome 会话。"),
        ),
        setup_url="https://www.reddit.com/login/",
        setup_link_label="打开 Reddit 登录页",
        implementation_status="available",
        implementation_message="已支持：浏览器会话走 OpenCLI；Cookie 连接走项目固定 Reddit JSON 请求。匿名请求被拦截时会明确返回登录连接提示。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="bilibili",
        platform_name="B站",
        description="公开搜索和详情无需登录；字幕读取需要用户已登录的浏览器会话。",
        capability_ids=("intelligence.bilibili_video", "intelligence.bilibili_subtitle"),
        auth_methods=("browser_session",),
        fields=(_field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "复用用户明确控制的 Chrome 会话。"),),
        setup_url="https://www.bilibili.com/",
        setup_link_label="打开 B站",
        implementation_status="available",
        implementation_message="已支持：公开搜索/详情无需连接，字幕通过已连接的 OpenCLI 浏览器会话读取。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="facebook",
        platform_name="Facebook",
        description="通过用户已登录浏览器会话读取主页、Feed 和群组等内容。",
        capability_ids=("intelligence.facebook_read",),
        auth_methods=("browser_session",),
        fields=(_field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "不直接输入密码，复用用户已登录的 Chrome 会话。"),),
        setup_url="https://www.facebook.com/",
        setup_link_label="打开 Facebook",
        implementation_status="available",
        implementation_message="已支持：固定 OpenCLI 操作复用当前用户已连接的浏览器会话。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="instagram",
        platform_name="Instagram",
        description="通过用户已登录浏览器会话读取用户资料、帖子和 Explore 内容。",
        capability_ids=("intelligence.instagram_read",),
        auth_methods=("browser_session",),
        fields=(_field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "不直接输入密码，复用用户已登录的 Chrome 会话。"),),
        setup_url="https://www.instagram.com/",
        setup_link_label="打开 Instagram",
        implementation_status="available",
        implementation_message="已支持：固定 OpenCLI 操作复用当前用户已连接的浏览器会话。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="xiaohongshu",
        platform_name="小红书",
        description="小红书登录必须由用户在自己的浏览器完成，平台只接收明确导入的会话或浏览器引用。",
        capability_ids=("intelligence.xiaohongshu_search", "intelligence.xiaohongshu_read"),
        auth_methods=("browser_session",),
        fields=(
            _field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "OpenCLI 复用用户已有 Chrome 会话。"),
        ),
        setup_url="https://www.xiaohongshu.com/",
        setup_link_label="打开小红书",
        implementation_status="available",
        implementation_message="已支持：通过 OpenCLI 复用用户明确连接的已登录浏览器；不由 Agent 代替用户登录，也不自动读取浏览器 Cookie。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="linkedin",
        platform_name="LinkedIn",
        description="公开页面可由 Jina Reader 读取；Profile、公司和职位搜索需要官方授权或浏览器会话。",
        capability_ids=("intelligence.linkedin_read", "intelligence.linkedin_search"),
        auth_methods=("browser_session",),
        fields=(_field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "复用用户已登录的 Chrome 会话。"),),
        setup_url="https://www.linkedin.com/login",
        setup_link_label="打开 LinkedIn 登录页",
        implementation_status="adapter_pending",
        implementation_message="公开页面读取已由 Jina/Exa 覆盖；上游私有 Profile、公司和职位搜索依赖 mcp-server-linkedin，项目将继续保持独立连接边界后接入。",
    ),
    VerticalAbilityConnectionDefinition(
        platform_id="xueqiu",
        platform_name="雪球",
        description="股票行情和部分公开内容可直接读取；受保护社区数据使用用户导入的雪球会话 Cookie。",
        capability_ids=("intelligence.xueqiu_read", "intelligence.xueqiu_search"),
        auth_methods=("cookie", "browser_session"),
        fields=(
            _field("cookie", "Cookie 字符串", "粘贴 Cookie-Editor 导出的雪球 Cookie", "仅接受用户主动导出的 xueqiu.com Cookie。"),
            _field("session_reference", "浏览器会话标识", "由桌面浏览器连接器生成", "复用用户已登录的 Chrome 会话。"),
        ),
        setup_url="https://xueqiu.com/",
        setup_link_label="打开雪球",
        implementation_status="available",
        implementation_message="已支持：公开接口直接读取，Cookie 连接按当前请求发送，浏览器会话走 OpenCLI。",
    ),
)


def list_connection_definitions() -> tuple[VerticalAbilityConnectionDefinition, ...]:
    """返回当前项目版本声明的网站连接目录，顺序即配置页展示顺序。"""

    return PLATFORM_CONNECTION_DEFINITIONS


def find_connection_definition(platform_id: str) -> VerticalAbilityConnectionDefinition | None:
    """按稳定平台 ID 查找连接定义，不接受客户端自定义网站地址或动态命令。"""

    normalized = str(platform_id or "").strip().casefold()
    return next(
        (item for item in PLATFORM_CONNECTION_DEFINITIONS if item.platform_id == normalized),
        None,
    )
