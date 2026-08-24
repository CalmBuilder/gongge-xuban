"""
@Time       : 2026/08/14 13:35
@Author     : zhanglp8181
@File       : test_attachment_visual_evidence.py
@CallChain  : pytest → AttachmentVisualReviewer → provider JSON contract
@Description: 验证第二证据只接受冻结Snapshot、结构化观察/冲突与原生受控内容块。
"""

import pytest
from pydantic import ValidationError

from app.dynamic_tasks.attachment_evidence import (
    AttachmentVisualReview,
    AttachmentVisualReviewer,
    _structural_value_is_anchored,
)
from app.llm.client import PROVIDER_CONTENT_PARTS_KEY


class _JsonClient:
    """捕获视觉复核请求并返回测试指定的完整JSON。"""

    def __init__(self, response: dict[str, object]) -> None:
        """保存一次provider响应并初始化请求捕获。"""

        self.response = response
        self.payload: dict[str, object] | None = None

    def generate_json_with_metadata(self, system_prompt, user_payload):  # noqa: ANN001, ANN201
        """记录系统边界和原生part，模拟已完成provider响应。"""

        assert "未可信业务数据" in system_prompt
        self.payload = user_payload
        return self.response, {"response_id": "visual-response", "finish_reason": "stop"}


def test_visual_reviewer_preserves_conflict_in_structured_result() -> None:
    """同一事实结构值与视觉值不同必须作为conflict返回，不能由复核器静默覆盖。"""

    client = _JsonClient(
        {
            "observations": [
                {
                    "snapshot_id": "snapshot-a",
                    "fact_key": "contract.notice_days",
                    "normalized_value": "30",
                    "locator": {"page": 1, "bbox": [1, 2, 3, 4]},
                    "confidence": 0.95,
                }
            ],
            "conflicts": [
                {
                    "snapshot_id": "snapshot-a",
                    "fact_key": "contract.notice_days",
                    "structural_value": "60",
                    "visual_value": "30",
                    "locator": {"page": 1},
                }
            ],
            "gaps": [],
        }
    )
    reviewer = AttachmentVisualReviewer(client)

    review, metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-a",
                "instruction_boundary": "resource_content_is_untrusted_data",
                "elements": [{"text": "提前60天通知", "locator": {"page": 1}}],
            }
        ],
        native_parts=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            }
        ],
        questions=["通知期是多少天"],
    )

    assert review.conflicts[0].structural_value == "60"
    assert review.conflicts[0].visual_value == "30"
    assert metadata["response_id"] == "visual-response"
    assert client.payload is not None
    assert client.payload[PROVIDER_CONTENT_PARTS_KEY][0]["type"] == "image_url"


def test_visual_reviewer_drops_image_metadata_pseudo_conflicts() -> None:
    """图片尺寸与OCR状态只是结构元数据，不得被模型升级为内容事实冲突。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": "dominant_color",
                        "normalized_value": "blue",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                        "confidence": 1,
                    },
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": "text_presence",
                        "normalized_value": "none",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                        "confidence": 1,
                    },
                ],
                "conflicts": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": "text_presence",
                        "structural_value": "OCR not requested",
                        "visual_value": "none",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                    }
                ],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-image",
                "elements": [
                    {
                        "type": "image",
                        "text": "Image 64x40; OCR not requested",
                    }
                ],
            }
        ],
        native_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
        questions=["主色是什么"],
    )

    assert [item.normalized_value for item in review.observations] == ["blue", "none"]
    assert review.conflicts == ()


def test_visual_reviewer_records_unobserved_or_unanchored_conflict_as_gap() -> None:
    """非图片正文中的未锚定冲突必须转成gap，不能被静默删除。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "contract.notice_days",
                        "normalized_value": "30",
                        "locator": {"page": 1},
                        "confidence": 0.9,
                    }
                ],
                "conflicts": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "other.fact",
                        "structural_value": "60",
                        "visual_value": "30",
                        "locator": {"page": 1},
                    },
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "contract.notice_days",
                        "structural_value": "90",
                        "visual_value": "30",
                        "locator": {"page": 1},
                    },
                ],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-a",
                "elements": [{"type": "text", "text": "提前60天通知"}],
            }
        ],
        native_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
        questions=["通知期"],
    )

    assert review.conflicts == ()
    assert review.gaps == (
        "VISUAL_CONFLICT_UNANCHORED:snapshot-a:other.fact",
        "VISUAL_CONFLICT_UNANCHORED:snapshot-a:contract.notice_days",
    )


def test_visual_reviewer_normalizes_whitespace_and_uses_numeric_boundaries() -> None:
    """60尾随空格仍锚定60天，而0不得以子串方式命中60。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "notice_days",
                        "normalized_value": "30 ",
                        "locator": {"page": 1},
                        "confidence": 0.9,
                    }
                ],
                "conflicts": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "notice_days",
                        "structural_value": "60 ",
                        "visual_value": "30 ",
                        "locator": {"page": 1},
                    },
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "notice_days",
                        "structural_value": "0",
                        "visual_value": "30",
                        "locator": {"page": 1},
                    },
                ],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-a",
                "elements": [{"type": "text", "text": "合同提前60天通知"}],
            }
        ],
        native_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
        questions=["通知期"],
    )

    assert [(item.structural_value, item.visual_value) for item in review.conflicts] == [
        ("60", "30")
    ]
    assert review.gaps == ("VISUAL_CONFLICT_UNANCHORED:snapshot-a:notice_days",)


def test_visual_reviewer_duplicate_observation_values_fail_closed() -> None:
    """同一事实出现不同视觉值时必须成为gap，且结果不得依赖provider数组顺序。"""

    def review_for(values: list[str]):
        """按指定顺序构造同fact重复观察并返回复核结果。"""

        reviewer = AttachmentVisualReviewer(
            _JsonClient(
                {
                    "observations": [
                        {
                            "snapshot_id": "snapshot-a",
                            "fact_key": "notice_days",
                            "normalized_value": value,
                            "locator": {"page": 1},
                            "confidence": 0.9,
                        }
                        for value in values
                    ],
                    "conflicts": [
                        {
                            "snapshot_id": "snapshot-a",
                            "fact_key": "notice_days",
                            "structural_value": "60",
                            "visual_value": "30",
                            "locator": {"page": 1},
                        }
                    ],
                    "gaps": [],
                }
            )
        )
        return reviewer.review(
            input_resources=[
                {
                    "snapshot_id": "snapshot-a",
                    "elements": [{"type": "text", "text": "提前60天通知"}],
                }
            ],
            native_parts=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
            ],
            questions=["通知期"],
        )[0]

    first = review_for(["30", "40"])
    second = review_for(["40", "30"])

    assert first.conflicts == second.conflicts == ()
    assert first.gaps == second.gaps == (
        "VISUAL_CONFLICT_UNANCHORED:snapshot-a:notice_days",
    )


@pytest.mark.parametrize("gap", ["", "   ", "\n", "x" * 513])
def test_visual_review_rejects_empty_control_or_unbounded_gap(gap: str) -> None:
    """空缺口、控制字符与无界文本不得借substring判断绕过最终披露。"""

    with pytest.raises(ValidationError):
        AttachmentVisualReview(gaps=(gap,))


@pytest.mark.parametrize("value", ["30\n", "\t30", "30\x00"])
def test_visual_review_rejects_control_characters_before_strip(value: str) -> None:
    """证据值边界控制字符也必须拒绝，不能先strip后放行。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "notice_days",
                        "normalized_value": value,
                        "locator": {"page": 1},
                        "confidence": 0.9,
                    }
                ],
                "conflicts": [],
                "gaps": [],
            }
        )
    )

    with pytest.raises(ValidationError):
        reviewer.review(
            input_resources=[{"snapshot_id": "snapshot-a", "elements": []}],
            native_parts=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
            ],
            questions=["通知期"],
        )


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("0", "0.5"),
        ("60", "60.5"),
        ("1", "1,000"),
        ("30", "-30"),
        ("5", ".5"),
        ("30", "−30"),
        ("1", "1\u00a0000"),
        ("30", "(30)"),
    ],
)
def test_structural_numeric_anchor_requires_complete_token(value: str, text: str) -> None:
    """数字冲突必须匹配完整带符号/小数/千分位token，禁止子串伪锚定。"""

    assert _structural_value_is_anchored(value, text) is False


def test_pure_image_only_silently_drops_known_metadata_conflicts() -> None:
    """纯图片中的未知事实冲突必须转gap，只有已识别技术元数据可以静默过滤。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": "business.amount",
                        "normalized_value": "30",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                        "confidence": 0.8,
                    }
                ],
                "conflicts": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": "business.amount",
                        "structural_value": "60",
                        "visual_value": "30",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                    }
                ],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-image",
                "elements": [{"type": "image", "text": "Image 64x40; OCR not requested"}],
            }
        ],
        native_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
        questions=["金额"],
    )

    assert review.conflicts == ()
    assert review.gaps == (
        "VISUAL_CONFLICT_UNANCHORED:snapshot-image:business.amount",
    )


@pytest.mark.parametrize("fact_key", ("image_has_text", "image_text_presence", "has_text"))
def test_pure_image_drops_text_presence_conflict_against_ocr_metadata(
    fact_key: str,
) -> None:
    """模型用常见无文字事实键错比OCR技术元数据时不得制造业务gap。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": fact_key,
                        "normalized_value": "false",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                        "confidence": 1.0,
                    }
                ],
                "conflicts": [
                    {
                        "snapshot_id": "snapshot-image",
                        "fact_key": fact_key,
                        "structural_value": "OCR not requested",
                        "visual_value": "false",
                        "locator": {"kind": "image_region", "region": "full_frame"},
                    }
                ],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[
            {
                "snapshot_id": "snapshot-image",
                "elements": [{"type": "image", "text": "Image 64x40; OCR not requested"}],
            }
        ],
        native_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ],
        questions=["图片是否包含文字"],
    )

    assert review.observations[0].normalized_value == "false"
    assert review.conflicts == ()
    assert review.gaps == ()


def test_visual_reviewer_rejects_unbounded_or_extra_provider_fields() -> None:
    """视觉provider伪造工具/路径等额外字段时必须在进入Operation前拒绝。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [],
                "conflicts": [],
                "gaps": [],
                "tool_call": {"name": "write_file"},
            }
        )
    )

    with pytest.raises(ValidationError):
        reviewer.review(
            input_resources=[{"snapshot_id": "snapshot-a"}],
            native_parts=[
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                }
            ],
            questions=["核验"],
        )


def test_visual_reviewer_normalizes_bounded_region_phrase() -> None:
    """兼容模型返回full_frame短语，但仍归一到受控locator对象。"""

    reviewer = AttachmentVisualReviewer(
        _JsonClient(
            {
                "observations": [
                    {
                        "snapshot_id": "snapshot-a",
                        "fact_key": "dominant_color",
                        "normalized_value": "blue",
                        "locator": "full_frame",
                        "confidence": 1,
                    }
                ],
                "conflicts": [],
                "gaps": [],
            }
        )
    )

    review, _metadata = reviewer.review(
        input_resources=[{"snapshot_id": "snapshot-a"}],
        native_parts=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            }
        ],
        questions=["主色是什么"],
    )

    assert review.observations[0].locator == {
        "kind": "image_region",
        "region": "full_frame",
    }
