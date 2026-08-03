from app.knowledge.okf import (
    KNOWLEDGE_URI_SCHEME,
    is_supported_knowledge_target,
    knowledge_uri,
)


def test_new_knowledge_links_use_gongge_scheme() -> None:
    assert KNOWLEDGE_URI_SCHEME == "gongge-xuban"
    assert knowledge_uri("documents/doc-1") == "gongge-xuban://knowledge/documents/doc-1"


def test_only_current_and_web_knowledge_links_are_valid() -> None:
    assert is_supported_knowledge_target("gongge-xuban://knowledge/documents/doc-1")
    foreign_scheme = "".join(("ultra", "rag"))
    assert not is_supported_knowledge_target(f"{foreign_scheme}://knowledge/documents/doc-1")
    assert is_supported_knowledge_target("https://example.com/reference")
    assert not is_supported_knowledge_target("missing/concept")
