"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_vertical_ability_media_browser_integrity.py
@CallChain  : pytest → 字幕 Runtime/浏览器适配器 → 有界正文、时间轴与关联材料
@Description: 用离线负例保护字幕时间轴、浏览器公开关联字段、凭据隔离、有效指标和截断披露。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.dynamic_tasks.execution_context import project_result_for_model
from app.vertical_abilities import adapters, youtube_runtime
from app.vertical_abilities.catalog import (
    _SUBTITLE_OUTPUT_SCHEMA,
    _YOUTUBE_VIDEO_OUTPUT_SCHEMA,
)


def _settings() -> SimpleNamespace:
    """提供不依赖真实配置、数据库或网络的最小适配器设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def _install_youtube_vtt(monkeypatch: pytest.MonkeyPatch, vtt: str) -> None:
    """让真实字幕读取流程处理替身下载器生成的 VTT，不触达 YouTube。"""

    class FakeDownloader:
        """仅实现 Runtime 所需的下载协议，并在其临时目录写入测试字幕。"""

        def __init__(self, options: dict[str, object]) -> None:
            """保存 Runtime 提供的输出目录以验证实际文件解析路径。"""

            self.options = options

        def __enter__(self) -> FakeDownloader:
            """返回当前下载器以兼容上下文管理协议。"""

            return self

        def __exit__(self, *_args: object) -> None:
            """保持退出无副作用，清理由实际 Runtime 负责。"""

        def close(self) -> None:
            """提供实际取消管理器可调用的无副作用关闭方法。"""

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            """生成中英文字幕夹具并返回最小视频元数据。"""

            assert download is True
            directory = Path(self.options["outtmpl"]["default"]).parent
            (directory / "sample.zh-Hans.vtt").write_text(vtt, encoding="utf-8")
            return {"id": "sample", "title": "数字教程", "webpage_url": url}

    monkeypatch.setattr(youtube_runtime, "_load_ytdlp", lambda: (FakeDownloader, RuntimeError))
    monkeypatch.setattr(youtube_runtime, "_js_runtime_options", lambda: {})
    monkeypatch.setattr(adapters, "_validate_youtube_url", lambda *_args: None)


@pytest.mark.parametrize("body", ["2026", "0", "123\n456", "NOTE 是一句台词"])
def test_vtt_distinguishes_cue_identifiers_from_numeric_or_note_body(body: str) -> None:
    """仅跳过时间行前的 cue 编号，不丢弃正文中的数字和 NOTE 单词。"""

    vtt = f"WEBVTT\n\nNOTE 元数据\n不属于台词\n\n17\n00:01.000 --> 00:03.000\n{body}\n"

    assert youtube_runtime._clean_vtt(vtt) == body


def test_youtube_preserves_timed_numeric_cues_through_file_runtime_and_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证去重展示文本不删除不同时间的重复 cue，时间单位为来源中的秒。"""

    _install_youtube_vtt(
        monkeypatch,
        "WEBVTT\n\n1\n00:01.250 --> 00:03.000 align:start\n2026\n\n"
        "2\n00:03.000 --> 00:04.500\n2026\n\n"
        "cue-name\n01:00:00.000 --> 01:00:02.000\n<c>你好 &amp; 世界</c>\n",
    )
    result = adapters.execute_vertical_adapter(
        "youtube.ytdlp_video",
        {"url": "https://www.youtube.com/watch?v=sample"},
        settings=_settings(),
    )

    assert result["subtitle_result_state"] == "success_with_data"
    assert result["subtitle"]["content"] == "2026\n你好 & 世界"
    assert result["subtitle"]["timeline"] == [
        {"start": 1.25, "end": 3.0, "text": "2026"},
        {"start": 3.0, "end": 4.5, "text": "2026"},
        {"start": 3600.0, "end": 3602.0, "text": "你好 & 世界"},
    ]
    assert result["subtitle"]["content_truncated"] is False
    assert result["subtitle"]["timeline_truncated"] is False


def test_youtube_subtitle_bounds_are_disclosed_in_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Runtime 的正文与时间轴上限不会在适配器透传时丢失截断声明。"""

    monkeypatch.setattr(youtube_runtime, "_MAX_SUBTITLE_CHARS", 12)
    _install_youtube_vtt(
        monkeypatch,
        "WEBVTT\n\n00:00.000 --> 00:01.000\nabcdefghij\n\n"
        "00:01.000 --> 00:02.000\nklmnopqrst\n",
    )
    result = adapters.execute_vertical_adapter(
        "youtube.ytdlp_video",
        {"url": "https://www.youtube.com/watch?v=sample"},
        settings=_settings(),
    )

    subtitle = result["subtitle"]
    assert subtitle["content"] == "abcdefghij\nk"
    assert subtitle["content_truncated"] is True
    assert subtitle["timeline_truncated"] is True
    assert sum(len(cue["text"]) for cue in subtitle["timeline"]) <= 12


@pytest.mark.parametrize(
    "raw",
    [
        {"segments": [{"start": 1.25, "end": 3, "text": "2026"}]},
        {"data": {"body": [{"from": 1.25, "to": 3, "content": "2026"}]}},
        [{"start": "00:01.250", "end": "00:03.000", "text": "2026"}],
        [{"index": 1, "from": "1.25s", "to": "3.00s", "content": "2026"}],
    ],
)
def test_bilibili_keeps_available_segment_timeline_and_numeric_body(
    monkeypatch: pytest.MonkeyPatch, raw: object,
) -> None:
    """B站已有分段结果必须同时保留原文与可验证时间，不依赖新增工具。"""

    monkeypatch.setattr(adapters, "run_opencli", lambda *_args, **_kwargs: raw)
    result = adapters.execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
    )

    assert result["subtitle_result_state"] == "success_with_data"
    assert result["subtitle"]["content"] == "2026"
    assert result["subtitle"]["timeline"] == [{"start": 1.25, "end": 3.0, "text": "2026"}]


@pytest.mark.parametrize(
    "raw",
    ["2026", {"segments": [{"text": "2026"}]}, {"text": "Whisper 只有文本"}],
)
def test_subtitle_text_without_timing_never_invents_a_timeline(
    monkeypatch: pytest.MonkeyPatch, raw: object,
) -> None:
    """纯文本或无时间字段的分段不得被包装为虚构的零秒时间轴。"""

    monkeypatch.setattr(adapters, "run_opencli", lambda *_args, **_kwargs: raw)
    result = adapters.execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
    )

    assert result["subtitle"]["content"]
    assert not result["subtitle"].get("timeline")


@pytest.mark.parametrize("start,end", [(3, 1), (-1, 2), ("NaN", 2), (0, "inf")])
def test_bilibili_rejects_invalid_timing_but_keeps_untimed_text(
    monkeypatch: pytest.MonkeyPatch, start: object, end: object,
) -> None:
    """倒序、负数及非有限时间必须披露无效分段，而不影响已有正文保真。"""

    raw = {"segments": [{"start": start, "end": end, "text": "2026"}]}
    monkeypatch.setattr(adapters, "run_opencli", lambda *_args, **_kwargs: raw)
    result = adapters.execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
    )

    assert result["subtitle"]["content"] == "2026"
    assert not result["subtitle"].get("timeline")
    assert result["subtitle"]["timeline_invalid_count"] == 1


def test_browser_note_preserves_identity_metrics_author_and_related_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按上游 note_card 形状保留公开字段，关联评论不丢 ID、作者和互动零值。"""

    raw = {
        "id": "outer-note",
        "note_card": {
            "note_id": "note-1", "display_title": "工具体验", "desc": "命令设计很好用",
            "user": {"nickname": "开发者", "user_id": "author-1", "cookie": "not-public"},
            "interact_info": {"liked_count": 0, "comment_count": 2, "share_count": None},
            "comments": [{
                "id": "comment-1", "parent_id": "note-1", "content": "补全是否支持？",
                "user_info": {"nickname": "读者", "user_id": "reader-1"},
                "like_count": 0, "sub_comment_count": 1,
                "sub_comments": [{
                    "id": "reply-1", "parent_id": "comment-1", "content": "支持补全",
                    "user": {"nickname": "维护者", "user_id": "maintainer-1"},
                }],
            }],
        },
    }
    monkeypatch.setattr(adapters, "run_opencli", lambda *_args, **_kwargs: raw)
    result = adapters.execute_vertical_adapter(
        "xiaohongshu.opencli",
        {"operation": "note", "value": "https://www.xiaohongshu.com/explore/note-1"},
        settings=_settings(),
        platform_connections={"xiaohongshu": {"session_reference": "relay:fixture"}},
    )

    item = result["items"][0]
    assert item["id"] == "outer-note"
    assert item["note_id"] == "note-1"
    assert item["title"] == "工具体验"
    assert item["summary"] == "命令设计很好用"
    assert item["user"] == {"nickname": "开发者", "user_id": "author-1"}
    assert item["author"] == "开发者"
    assert item["liked_count"] == 0
    assert item["share_count"] is None
    assert "collected_count" not in item
    comment = item["comments"][0]
    assert comment["id"] == "comment-1"
    assert comment["parent_id"] == "note-1"
    assert comment["content"] == "补全是否支持？"
    assert comment["author"] == "读者"
    assert comment["user_info"]["user_id"] == "reader-1"
    assert comment["like_count"] == 0
    assert comment["sub_comments"][0]["parent_id"] == "comment-1"
    assert comment["sub_comments"][0]["content"] == "支持补全"
    assert "not-public" not in json.dumps(result, ensure_ascii=False)


def test_browser_preserves_generic_post_ids_metrics_and_comment_body() -> None:
    """通用帖子已有 ID、互动与关联评论不可在来源卡片规范化时丢弃。"""

    item = adapters._normalize_platform_items([{
        "id": "post-1", "url": "https://example.org/post", "content": "x" * 4001,
        "likes": 42, "comments": [{"id": "c1", "body": "2026", "author": "user-1"}],
    }], 10)[0]

    assert item["id"] == "post-1"
    assert item["likes"] == 42
    assert item["summary"] == "x" * 4000
    assert item["summary_truncated"] is True
    assert item["content_truncated"] is True
    assert item["comments"][0]["id"] == "c1"
    assert item["comments"][0]["summary"] == "2026"
    assert item["comments"][0]["author"] == "user-1"


@pytest.mark.parametrize("length", [3999, 4000, 4001])
def test_browser_marks_exact_text_and_title_truncation_boundary(length: int) -> None:
    """四千字符上限包含标题和纯文本摘要，恰好等于上限不误报截断。"""

    text = "字" * length
    text_item = adapters._normalize_platform_items([text], 1)[0]
    post_item = adapters._normalize_platform_items([{"title": text, "content": text}], 1)[0]

    assert len(text_item["summary"]) == min(length, 4000)
    assert text_item.get("summary_truncated", False) is (length > 4000)
    assert post_item.get("summary_truncated", False) is (length > 4000)
    assert post_item.get("title_truncated", False) is (length > 4000)
    assert post_item.get("content_truncated", False) is (length > 4000)


def test_browser_bounds_related_comments_and_discloses_source_has_more() -> None:
    """关联评论读取量有限且来源已有分页提示时，不暗示取回了全部评论。"""

    raw = {
        "id": "p1", "content": "正文",
        "comments": [{"id": f"c{index}", "content": "评论"} for index in range(51)],
        "has_more": True,
    }
    item = adapters._normalize_platform_items([raw], 1)[0]

    assert len(item["comments"]) == 50
    assert item["comments_truncated"] is True
    assert item["has_more"] is True
    assert item["comments"][0]["id"] == "c0"


def test_browser_related_comment_recursion_is_bounded_and_marked() -> None:
    """超深评论树必须停在明确边界并标记截断，不能无限递归或静默丢失。"""

    raw: dict[str, object] = {"id": "leaf", "content": "最内层"}
    for index in range(8):
        raw = {"id": f"c{index}", "content": "评论", "replies": [raw]}
    item = adapters._normalize_platform_items([raw], 1)[0]
    visited = 0
    while item.get("replies"):
        visited += 1
        assert visited <= 4
        item = item["replies"][0]
    assert item["replies_truncated"] is True


def test_browser_keeps_original_simple_card_contract() -> None:
    """没有关联数据的旧来源卡片不增添虚构 ID、互动零值或空评论。"""

    assert adapters._normalize_platform_items([{
        "title": "兼容卡片", "url": "https://example.org/p", "summary": "简介",
    }], 1) == [{
        "title": "兼容卡片", "url": "https://example.org/p", "summary": "简介",
        "published_at": "",
    }]


def test_browser_comment_listing_unwraps_without_turning_post_comments_into_posts() -> None:
    """评论操作的集合应展开，而正文对象的 comments 必须仍挂在所属帖子下。"""

    comments = [{"id": "c1", "content": "评论1"}, {"id": "c2", "content": "评论2"}]
    listing = adapters._normalize_platform_items({"data": {"comments": comments}}, 10)
    post = adapters._normalize_platform_items({"id": "p1", "comments": comments}, 10)

    assert [item["id"] for item in listing] == ["c1", "c2"]
    assert len(post) == 1
    assert post[0]["id"] == "p1"
    assert [item["id"] for item in post[0]["comments"]] == ["c1", "c2"]


def test_bilibili_relay_json_text_keeps_opencli_seconds_and_numeric_caption() -> None:
    """实际 Relay 文本回执仍使用固定 OpenCLI 的 from/to 秒格式，并走真实 Runtime 解析。"""

    def relay(*_args: object, **_kwargs: object) -> bytes:
        """返回 OpenCLI 1.8.7 subtitle 的原字段形状而不连接真实设备。"""

        return b'[{"index":1,"from":"1.25s","to":"3.00s","content":"2026"}]'

    result = adapters.execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
        relay_executor=relay,
    )

    assert result["subtitle"]["content"] == "2026"
    assert result["subtitle"]["timeline"] == [{"start": 1.25, "end": 3.0, "text": "2026"}]


def test_bilibili_relay_default_yaml_preserves_timeline_and_numeric_zero() -> None:
    """OpenCLI 1.8.7 非 TTY 默认输出 YAML，必须解析行字段而非把整段 YAML 当正文。"""

    def relay(*_args: object, **_kwargs: object) -> bytes:
        """模拟固定发行版非交互模式的字幕输出，保留原始 from/to 和文本内容。"""

        return b"- index: 1\n  from: 0.00s\n  to: 1.50s\n  content: '0'\n"

    result = adapters.execute_vertical_adapter(
        "bilibili.opencli_subtitle",
        {"video": "BV1Q541167Qg"},
        settings=_settings(),
        platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
        relay_executor=relay,
    )

    assert result["subtitle"]["content"] == "0"
    assert result["subtitle"]["timeline"] == [{"start": 0.0, "end": 1.5, "text": "0"}]


def test_subtitle_timeline_count_is_bounded_without_dropping_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """时间轴达到条数上限时仍保留范围内正文，并明确披露时间轴不是完整分段。"""

    monkeypatch.setattr(youtube_runtime, "_MAX_SUBTITLE_SEGMENTS", 2)
    result = youtube_runtime.normalize_subtitle_segments([
        {"start": i, "end": i + 1, "text": str(i)} for i in range(3)
    ])

    assert result["content"] == "0\n1\n2"
    assert result["content_truncated"] is False
    assert len(result["timeline"]) == 2
    assert result["timeline_truncated"] is True


def test_browser_related_comment_budget_is_shared_across_all_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多帖与多级评论共用同一预算，不能每层重新分配而放大输出大小。"""

    monkeypatch.setattr(adapters, "_MAX_PLATFORM_RELATED_TOTAL", 2)
    raw = [
        {"id": f"p{i}", "content": "正文", "comments": [{"id": f"c{i}", "content": "评论"}]}
        for i in range(3)
    ]
    items = adapters._normalize_platform_items(raw, 3)

    assert sum(len(item["comments"]) for item in items) == 2
    assert items[2]["comments"] == []
    assert items[2]["comments_truncated"] is True


@pytest.mark.parametrize("platform", ["youtube", "bilibili"])
@pytest.mark.parametrize("count", [100, 101, 2001])
def test_subtitle_model_projection_keeps_timeline_truncation_truthful(
    monkeypatch: pytest.MonkeyPatch, platform: str, count: int,
) -> None:
    """通过真实字幕适配器与公开 schema 验证百条模型裁剪，原始回执不得被投影修改。"""

    if platform == "youtube":
        vtt = "WEBVTT\n\n" + "\n\n".join(
            f"{i // 60:02d}:{i % 60:02d}.000 --> "
            f"{(i + 1) // 60:02d}:{(i + 1) % 60:02d}.000\n{i}"
            for i in range(count)
        )
        _install_youtube_vtt(monkeypatch, vtt)
        result = adapters.execute_vertical_adapter(
            "youtube.ytdlp_video", {"url": "https://www.youtube.com/watch?v=sample"},
            settings=_settings(),
        )
        schema = _YOUTUBE_VIDEO_OUTPUT_SCHEMA
    else:
        raw = [{"from": i, "to": i + 1, "content": str(i)} for i in range(count)]
        monkeypatch.setattr(adapters, "run_opencli", lambda *_args, **_kwargs: raw)
        result = adapters.execute_vertical_adapter(
            "bilibili.opencli_subtitle", {"video": "BV1Q541167Qg"}, settings=_settings(),
            platform_connections={"bilibili": {"session_reference": "relay:fixture"}},
        )
        schema = _SUBTITLE_OUTPUT_SCHEMA
    original = json.dumps(result, ensure_ascii=False, sort_keys=True)

    projected = project_result_for_model(result, schema)

    assert len(result["subtitle"]["timeline"]) == min(count, 2000)
    assert result["subtitle"]["timeline_truncated"] is (count > 2000)
    assert len(projected["subtitle"]["timeline"]) == min(count, 100)
    assert projected["subtitle"]["timeline_truncated"] is (count > 100)
    assert projected["subtitle"]["timeline"][0] == {"start": 0.0, "end": 1.0, "text": "0"}
    assert json.dumps(result, ensure_ascii=False, sort_keys=True) == original


@pytest.mark.parametrize("raw", [
    {"secret": "fixture-private"},
    {"token": "fixture-private", "_private": {"unknown": "fixture-private"}},
    {"unexpected": {"password": "fixture-private"}},
    {"has_more": False, "credentials": {"api_key": "fixture-private"}},
    {"id": None, "user": {"cookie": "fixture-private"}},
    {"user": {"nickname": ""}, "_private": "fixture-private"},
])
def test_browser_unknown_or_credential_only_objects_do_not_become_results(raw: object) -> None:
    """未知与凭据对象不得通过 JSON 兜底变成正文或虚构一条有效来源。"""

    assert adapters._normalize_platform_items([raw], 10) == []


@pytest.mark.parametrize("field", ["title", "url", "published_at", "summary", "content"])
def test_browser_named_text_fields_never_stringify_unknown_objects(field: str) -> None:
    """即使嵌套未知对象放在已知文本字段内，也不得经 str 或 repr 暴露内部字段。"""

    item = adapters._normalize_platform_items([{
        "id": "public-1", field: {"secret": "fixture-private", "token": "fixture-private"},
    }], 1)[0]

    assert item["id"] == "public-1"
    assert "fixture-private" not in json.dumps(item, ensure_ascii=False, allow_nan=False)
    assert item["summary"] == ""


@pytest.mark.parametrize("raw", [
    {"error": {"message": "fixture-private"}},
    {"isError": True, "content": "fixture-private"},
    {"success": False, "description": "fixture-private"},
    {"error": "unavailable", "data": [{"id": "not-success", "content": "fixture-private"}]},
])
def test_browser_error_objects_are_structured_failures_not_source_cards(raw: object) -> None:
    """显式 Provider 错误对象不得算作结果或合法无结果，错误消息不回显不可信载荷。"""

    with pytest.raises(adapters.VerticalAbilityAdapterError) as error:
        adapters._normalize_platform_items(raw, 10)

    assert error.value.code == "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID"
    assert "fixture-private" not in error.value.message


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -float("inf")])
def test_browser_metadata_filters_boolean_counts_and_nonfinite_numbers(value: object) -> None:
    """顶层与分组指标均拒绝布尔伪计数和非有限浮点，合法记录仍可严格 JSON 序列化。"""

    item = adapters._normalize_platform_items([{
        "id": "public-1", "content": "公开正文", "likes": value, "post_id": value,
        "interact_info": {"like_count": value, "share_count": None, "comment_count": 0},
    }], 1)[0]

    assert "likes" not in item
    assert "post_id" not in item
    assert "like_count" not in item
    assert "like_count" not in item["interact_info"]
    assert item["comment_count"] == 0
    assert item["share_count"] is None
    json.dumps(item, ensure_ascii=False, allow_nan=False)


def test_browser_public_identity_author_and_zero_metrics_remain_available() -> None:
    """只含公开 ID、公开作者或正文的记录仍有效，合法零值、未知值与计数字符串不丢失。"""

    items = adapters._normalize_platform_items([
        {"id": 0}, {"user": {"user_id": "author-1"}}, {"author": "作者"},
        {"content": "2026", "likes": 0, "score": -2.5, "view_count": "1.2万", "shares": None},
    ], 10)

    assert len(items) == 4
    assert items[0]["id"] == 0
    assert items[1]["user"]["user_id"] == "author-1"
    assert items[2]["author"] == "作者"
    assert items[3]["content"] == "2026"
    assert items[3]["likes"] == 0
    assert items[3]["score"] == -2.5
    assert items[3]["view_count"] == "1.2万"
    assert items[3]["shares"] is None


def test_browser_default_yaml_nonfinite_metrics_cannot_break_json_output() -> None:
    """真实 Relay YAML 解析出的 .nan/.inf/布尔值不得进入指标回执或破坏 API JSON。"""

    def relay(*_args: object, **_kwargs: object) -> bytes:
        """提供只含夹具内容的默认 YAML 回执，不接触网站或真实登录态。"""

        return (
            b"- id: public-1\n  content: '2026'\n  likes: .nan\n"
            b"  public_metrics:\n    view_count: .inf\n    like_count: true\n    share_count: 0\n"
        )

    result = adapters.execute_vertical_adapter(
        "xiaohongshu.opencli", {"operation": "search", "query": "CLI"}, settings=_settings(),
        platform_connections={"xiaohongshu": {"session_reference": "relay:fixture"}},
        relay_executor=relay,
    )

    item = result["items"][0]
    assert result["count"] == 1
    assert item["content"] == "2026"
    assert "likes" not in item
    assert item["public_metrics"] == {"share_count": 0}
    json.dumps(result, ensure_ascii=False, allow_nan=False)
