from app import brand
from app.brand import (
    DESKTOP_APP_ID,
    MCP_CLIENT_NAME,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    health_payload,
)


def test_product_identity_is_canonical() -> None:
    assert PRODUCT_NAME == "共格·序伴"
    assert PRODUCT_SLUG == "gongge-xuban"
    assert DESKTOP_APP_ID == "cn.gongge.xuban.desktop"
    assert MCP_CLIENT_NAME == "Gongge-Xuban"


def test_health_payload_uses_stable_product_id() -> None:
    assert health_payload() == {
        "status": "ok",
        "product_id": "gongge-xuban",
        "app": "共格·序伴",
    }


def test_current_environment_value_is_used(monkeypatch) -> None:
    monkeypatch.setenv("GONGGE_XUBAN_SITE_CHAT_UPSTREAM", "http://current.example")

    assert brand.site_chat_upstream() == "http://current.example"


def test_unrecognized_environment_value_is_ignored(monkeypatch) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_SITE_CHAT_UPSTREAM", raising=False)
    foreign_prefix = "".join(("STAFF", "DECK"))
    monkeypatch.setenv(f"{foreign_prefix}_SITE_CHAT_UPSTREAM", "http://foreign.example")

    assert brand.site_chat_upstream() == "http://127.0.0.1:10187"


def test_unrecognized_desktop_port_variable_is_ignored(monkeypatch) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_PORT", raising=False)
    foreign_prefix = "".join(("ULTRA", "RAG"))
    monkeypatch.setenv(f"{foreign_prefix}_PORT", "6000")

    assert brand.desktop_env_value("PORT", "5173") == "5173"
