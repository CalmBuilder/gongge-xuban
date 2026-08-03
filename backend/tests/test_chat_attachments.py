from app.api.chat import _user_message_metadata
from app.session.attachments import image_payloads_from_attachments, message_content_with_attachment_context, parse_chat_attachment
from app.session.session_schema import ChatTurnRequest


def test_text_attachment_extracts_preview_and_python_summary() -> None:
    attachment = parse_chat_attachment(
        "notes.txt",
        "text/plain",
        "第一行\n第二行".encode("utf-8"),
    )

    assert attachment.kind == "text"
    assert attachment.filename == "notes.txt"
    assert "第一行" in (attachment.text or "")
    assert attachment.preview
    assert "解析得到" in (attachment.python_summary or "")


def test_user_message_metadata_keeps_attachments() -> None:
    attachment = parse_chat_attachment("readme.md", "text/markdown", b"# Title")
    metadata = _user_message_metadata(
        ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            message="请看附件",
            attachments=[attachment],
        )
    )

    assert metadata["attachments"][0]["filename"] == "readme.md"
    assert metadata["attachments"][0]["kind"] == "text"


def test_image_attachment_uses_supported_extension_and_builds_image_payload() -> None:
    attachment = parse_chat_attachment("screen.PNG", "application/octet-stream", b"image-bytes")

    assert attachment.kind == "image"
    assert attachment.content_type == "image/png"
    assert attachment.data_url is not None
    assert image_payloads_from_attachments([attachment]) == [
        {
            "type": "image_url",
            "image_url": {
                "url": attachment.data_url,
                "detail": "auto",
            },
        }
    ]


def test_message_context_appends_attachment_text() -> None:
    attachment = parse_chat_attachment("readme.md", "text/markdown", b"# Title\ncontent")
    context = message_content_with_attachment_context(
        "总结一下",
        {"attachments": [attachment.model_dump(mode="json")]},
    )

    assert "总结一下" in context
    assert "上传附件上下文" in context
    assert "# Title" in context
