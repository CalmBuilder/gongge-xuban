"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_dynamic_result_projection_integrity.py
@CallChain  : pytest → 已发布结果 schema → 模型上下文有界投影
@Description: 验证模型侧二次截断显式披露且不污染原始回执、不放宽字段与凭据边界。
"""

from copy import deepcopy

import pytest

from app.dynamic_tasks.execution_context import project_result_for_model


@pytest.mark.parametrize("field", ["content", "body", "text"])
def test_long_open_content_discloses_model_clipping_without_mutating_receipt(field: str) -> None:
    """README/RSS/转写长正文在模型侧被截断时更新已有标记并保留原始回执。"""

    raw = {"repository": {field: "公开正文" * 4000, f"{field}_truncated": False}}
    original = deepcopy(raw)
    schema = {"type": "object", "properties": {"repository": {"type": "object"}}}
    visible = project_result_for_model(raw, schema)
    assert len(visible["repository"][field]) <= 12_000
    assert "模型上下文已截断" in visible["repository"][field]
    assert visible["repository"][f"{field}_truncated"] is True
    assert raw == original


def test_closed_schema_content_discloses_clipping_without_extra_fields() -> None:
    """封闭合同没有截断字段时在文本内说明，不擅自增加模型可见字段或泄露侧带。"""

    schema = {"type": "object", "properties": {"content": {"type": "string"}}}
    raw = {"content": "a" * 14_936, "api_key": "private", "_audit": "private"}
    visible = project_result_for_model(raw, schema)
    assert set(visible) == {"content"}
    assert len(visible["content"]) <= 12_000
    assert "模型上下文已截断" in visible["content"]
    assert "14936" in visible["content"]
    assert "private" not in str(visible)


def test_projection_preserves_short_text_and_provider_truncation() -> None:
    """未再裁剪的正文完全不变，适配器已经披露的截断状态不能被模型投影清除。"""

    raw = {"content": "短正文", "content_truncated": True}
    assert project_result_for_model(raw, {"type": "object"}) == raw


def test_nested_list_clipping_is_disclosed_in_each_actual_content() -> None:
    """RSS 列表元素的长正文也要逐项披露，不能仅在根对象笼统说可能截断。"""

    raw = {"entries": [{"content": "x" * 20_000, "content_truncated": False}]}
    visible = project_result_for_model(raw, {
        "type": "object", "properties": {"entries": {"type": "array"}},
    })
    entry = visible["entries"][0]
    assert entry["content_truncated"] is True
    assert "模型上下文已截断" in entry["content"]


def test_media_generic_truncation_flag_cannot_remain_false_after_projection() -> None:
    """转写合同使用通用 truncated/partial 标记时也应反映模型实际可见范围。"""

    raw = {"text": "字幕内容" * 4000, "truncated": False, "partial": False}
    visible = project_result_for_model(raw, {"type": "object"})
    assert visible["truncated"] is True
    assert visible["partial"] is True
    assert raw["truncated"] is False and raw["partial"] is False
