"""
@Time       : 2026/08/10 12:05
@Author     : zhanglp8181
@File       : test_slack_live.py
@CallChain  : pytest slack_live → SlackAdapter → Slack Web API isolated workspace
@Description: 在显式隔离账号环境中验证真实 Slack 身份、scope 与频道只读调用。
"""

from __future__ import annotations

import os

import pytest

from app.connectors.slack import SlackAdapter


def _required_env(name: str) -> str:
    """读取 live 测试变量；缺失时跳过，且不在测试输出中回显值。"""

    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for isolated Slack live tests")
    return value


@pytest.mark.slack_live
def test_isolated_slack_workspace_identity_scope_and_channel_read() -> None:
    """验证 token 属于预期 workspace、具备最小 scope，并能读取指定测试频道。"""

    token = _required_env("SLACK_TEST_BOT_TOKEN")
    expected_team_id = _required_env("SLACK_TEST_TEAM_ID")
    channel_id = _required_env("SLACK_TEST_CHANNEL_ID")
    adapter = SlackAdapter(timeout_seconds=15.0)

    identity = adapter.auth_test(token)
    assert identity.success is True
    assert identity.data.get("team_id") == expected_team_id
    assert "channels:read" in identity.granted_scopes

    channel = adapter.conversations_info(token, channel_id=channel_id)
    assert channel.success is True
    assert channel.data.get("channel", {}).get("id") == channel_id
