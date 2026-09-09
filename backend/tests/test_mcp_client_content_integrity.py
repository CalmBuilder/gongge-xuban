"""
@Time       : 2026/09/05 18:31
@Author     : zhanglp8181
@File       : test_mcp_client_content_integrity.py
@CallChain  : pytest → MCP 工具调用 → 混合回执提取与既有返回形状
@Description: 验证混合回执的显式保真选项不改变其他 MCP 工具的返回契约。
"""

import pytest

from app.tools.mcp_client import MCPClientError, _extract_tool_result


@pytest.mark.parametrize("structured_key", ["structuredContent", "structured_content"])
def test_mcp_preserves_mixed_envelope_only_when_requested(structured_key: str) -> None:
    """显式保真时保留空结构化载荷旁的文本，默认调用仍返回原结构化对象。"""

    raw = {
        structured_key: {"results": []},
        "content": [{"type": "text", "text": "Title: Guide\nURL: https://example.org/guide"}],
    }
    assert _extract_tool_result(raw) == {"results": []}
    assert _extract_tool_result(raw, preserve_mixed_content=True) == raw


@pytest.mark.parametrize("structured", [{"count": 0}, {"enabled": False}, [], 0, False])
def test_mcp_default_keeps_scalar_and_structured_contracts(structured: object) -> None:
    """非搜索工具的零值、布尔值及集合保持原类型，不自动包装为协议对象。"""

    raw = {"structuredContent": structured, "content": [{"type": "text", "text": "Summary"}]}
    assert _extract_tool_result(raw) is structured


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"structuredContent": {"count": 0}}, {"count": 0}),
        ({"structuredContent": [], "content": [{"type": "text", "text": "  "}]}, []),
        ({"content": [{"type": "text", "text": '{"count": 0}'}]}, {"count": 0}),
        ({"content": [{"type": "text", "text": "plain text"}]}, "plain text"),
        (
            {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
            ["one", "two"],
        ),
        ({"content": [{"type": "image", "data": "fixture"}]}, {"type": "image", "data": "fixture"}),
        ({"structuredContent": [], "content": [{"type": "image", "data": "fixture"}]}, []),
    ],
)
def test_mcp_opt_in_does_not_wrap_unmixed_results(raw: object, expected: object) -> None:
    """保真选项仅影响有信息的混合文本回执，不改变纯文本、图片及空结果形状。"""

    assert _extract_tool_result(raw, preserve_mixed_content=True) == expected


@pytest.mark.parametrize("preserve_mixed_content", [False, True])
def test_mcp_tool_error_cannot_be_hidden_by_structured_results(
    preserve_mixed_content: bool,
) -> None:
    """无论是否保留协议对象，isError 都优先进入既有错误路径。"""

    with pytest.raises(MCPClientError, match="unavailable"):
        _extract_tool_result(
            {
                "isError": True,
                "structuredContent": {"results": [{"url": "https://example.org/"}]},
                "content": [{"type": "text", "text": "unavailable"}],
            },
            preserve_mixed_content=preserve_mixed_content,
        )
