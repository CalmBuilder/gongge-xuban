"""
@Time       : 2026/08/28 16:00
@Author     : zhanglp8181
@File       : test_mcp_client_pagination.py
@CallChain  : pytest → MCP 会话 → tools/list 分页与 cursor 安全校验
@Description: 验证 MCP 工具发现完整读取分页、去重并在异常 cursor 下失败关闭。
"""

import pytest

from app.tools.mcp_client import MCPClientError, _MCPSession


class _PagedSession(_MCPSession):
    """用预置 JSON-RPC 结果模拟 MCP 分页会话。"""

    def __init__(self, pages: dict[str | None, dict[str, object]]) -> None:
        super().__init__({}, 1)
        self.pages = pages
        self.requests: list[tuple[str, dict[str, object]]] = []

    def _request(self, method: str, params: dict[str, object]) -> object:
        """记录请求并返回对应的初始化或分页结果。"""

        self.requests.append((method, params))
        if method == "initialize":
            return {}
        if method == "tools/list":
            return self.pages.get(params.get("cursor"))
        raise AssertionError(f"unexpected method: {method}")

    def _notify(self, method: str, params: dict[str, object]) -> None:
        """忽略初始化通知。"""


def test_mcp_tools_list_reads_all_pages_and_deduplicates_names() -> None:
    """分页发现应带 cursor 继续请求，并保留工具名首次出现的定义。"""

    session = _PagedSession(
        {
            None: {"tools": [{"name": "one"}, {"name": "duplicate"}], "nextCursor": "page-2"},
            "page-2": {"tools": [{"name": "duplicate"}, {"name": "two"}]},
        }
    )

    assert [item["name"] for item in session.list_tools()] == ["one", "duplicate", "two"]
    assert session.requests[-1] == ("tools/list", {"cursor": "page-2"})


def test_mcp_tools_list_rejects_repeated_cursor() -> None:
    """重复 cursor 必须失败关闭，避免恶意服务造成无限发现。"""

    session = _PagedSession(
        {
            None: {"tools": [], "next_cursor": "same"},
            "same": {"tools": [], "nextCursor": "same"},
        }
    )

    with pytest.raises(MCPClientError, match="重复 cursor"):
        session.list_tools()
