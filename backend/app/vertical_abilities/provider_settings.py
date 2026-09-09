"""
@Time       : 2026/09/02 20:55
@Author     : zhanglp8181
@File       : provider_settings.py
@CallChain  : 能力包配置中心 API → 平台 Provider 配置目录 → 凭据状态卡片
@Description: 定义能力包依赖的可配置 Provider 元数据，供后端接口和前端卡片数据驱动渲染。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerticalAbilityProviderDefinition:
    """描述一个能力包 Provider 配置卡片，不包含任何运行时密钥。"""

    provider: str
    ability_ids: tuple[str, ...]
    ability_names: tuple[str, ...]
    provider_name: str
    description: str
    credential_label: str
    credential_placeholder: str
    credential_setup_text: str | None = None
    credential_setup_url: str | None = None
    credential_setup_link_label: str | None = None
    verification_adapter: str | None = None
    supports_verification: bool = True


PLATFORM_VERTICAL_ABILITY_PROVIDERS: tuple[VerticalAbilityProviderDefinition, ...] = (
    VerticalAbilityProviderDefinition(
        provider="jina",
        ability_ids=("web-intelligence",),
        ability_names=("全网情报员",),
        provider_name="Jina Reader",
        description="读取公开网页正文，为全网情报员提供可引用的原文内容。",
        credential_label="Jina API Key",
        credential_placeholder="粘贴 Jina API Key",
        credential_setup_text="打开 Jina 官方控制台，登录并创建 API Key：",
        credential_setup_url="https://jina.ai/api-dashboard",
        credential_setup_link_label="Jina API Dashboard",
        verification_adapter="jina.reader",
    ),
    VerticalAbilityProviderDefinition(
        provider="groq",
        ability_ids=("web-intelligence",),
        ability_names=("全网情报员",),
        provider_name="Groq Whisper",
        description="在 YouTube 公开视频没有可用字幕时，通过 Whisper 将视频音频转换为可引用文本。",
        credential_label="Groq API Key",
        credential_placeholder="粘贴 Groq API Key",
        credential_setup_text="打开 Groq 官方控制台，登录并创建 API Key：",
        credential_setup_url="https://console.groq.com/keys",
        credential_setup_link_label="Groq API Keys",
        verification_adapter="groq.whisper",
    ),
    VerticalAbilityProviderDefinition(
        provider="openai",
        ability_ids=("web-intelligence",),
        ability_names=("全网情报员",),
        provider_name="OpenAI Whisper",
        description="在 YouTube 公开视频没有可用字幕时，通过 OpenAI Whisper 将视频音频转换为可引用文本。",
        credential_label="OpenAI API Key",
        credential_placeholder="粘贴 OpenAI API Key",
        credential_setup_text="打开 OpenAI 官方控制台，登录并创建 API Key：",
        credential_setup_url="https://platform.openai.com/api-keys",
        credential_setup_link_label="OpenAI API Keys",
        verification_adapter="openai.whisper",
    ),
)


def find_vertical_ability_provider(provider: str) -> VerticalAbilityProviderDefinition | None:
    """按稳定 Provider 标识查找配置描述，未知标识返回空值。"""

    normalized = str(provider or "").strip().casefold()
    return next(
        (item for item in PLATFORM_VERTICAL_ABILITY_PROVIDERS if item.provider == normalized),
        None,
    )


def find_provider_for_vertical_ability(
    ability_id: str,
) -> VerticalAbilityProviderDefinition | None:
    """按能力包 ID 找到其配置 Provider，未声明外部凭据的能力返回空值。"""

    normalized = str(ability_id or "").strip()
    return next(
        (
            item
            for item in PLATFORM_VERTICAL_ABILITY_PROVIDERS
            if normalized in item.ability_ids
        ),
        None,
    )
