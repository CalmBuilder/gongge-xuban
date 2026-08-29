"""
@Time       : 2026/07/27 14:45
@Author     : zhanglp8181
@File       : test_knowledge_citations.py
@CallChain  : pytest → knowledge.citations → AgentLoop 最终回复与消息引用
@Description: 验证知识引用提取、编号压缩和唯一原子值恢复。
"""

from app.knowledge.citations import (
    CITATION_EXCERPT_CHAR_LIMIT,
    compact_knowledge_citation_labels,
    knowledge_citation_identity,
    knowledge_citations_from_results,
    restore_truncated_atomic_references,
)


def test_compact_knowledge_citation_labels_renumbers_by_first_appearance() -> None:
    content, citations = compact_knowledge_citation_labels(
        "先参考手册。[4] 再确认规范。[1] 最后仍参考手册。[4]",
        [
            {"id": "kref_1", "label": "[1]", "title": "规范"},
            {"id": "kref_2", "label": "[2]", "title": "无关来源"},
            {"id": "kref_3", "label": "[3]", "title": "另一无关来源"},
            {"id": "kref_4", "label": "[4]", "title": "手册"},
        ],
    )

    assert content == "先参考手册。[1] 再确认规范。[2] 最后仍参考手册。[1]"
    assert [(item["label"], item["title"]) for item in citations] == [
        ("[1]", "手册"),
        ("[2]", "规范"),
    ]


def test_compact_knowledge_citation_labels_supports_historical_filtered_metadata() -> None:
    content, citations = compact_knowledge_citation_labels(
        "排查步骤来自手册。[1] 区域故障需要报修。[4]",
        [
            {"id": "kref_1", "label": "[1]", "title": "排查手册"},
            {"id": "kref_4", "label": "[4]", "title": "网络故障"},
        ],
    )

    assert content == "排查步骤来自手册。[1] 区域故障需要报修。[2]"
    assert [item["label"] for item in citations] == ["[1]", "[2]"]


def test_compact_knowledge_citation_labels_supports_ranges_and_removes_footer() -> None:
    """范围引用按顺序展开，来源尾巴不重复出现在正文中。"""

    content, citations = compact_knowledge_citation_labels(
        "请假制度依据员工手册。[1]-[4]\n\n参考来源：[1]-[4] [9]",
        [
            {"id": f"kref_{index}", "label": f"[{index}]", "title": f"来源 {index}"}
            for index in range(1, 6)
        ],
    )

    assert content == "请假制度依据员工手册。[1]-[4]"
    assert [item["label"] for item in citations] == ["[1]", "[2]", "[3]", "[4]"]


def test_compact_knowledge_citation_labels_keeps_authoritative_metadata_without_inline_labels() -> None:
    """模型漏写正文引用时仍保留服务端检索到的权威来源卡片。"""

    content, citations = compact_knowledge_citation_labels(
        "制度规定七天内可以申请退款。",
        [{"id": "kref_4", "label": "[4]", "title": "退款政策"}],
    )

    assert content == "制度规定七天内可以申请退款。"
    assert citations == [{"id": "kref_4", "label": "[1]", "title": "退款政策"}]


def test_knowledge_citations_keep_same_title_chunks_when_chunk_ids_differ() -> None:
    """同标题的不同知识切片不能因展示标题相同而错误合并。"""

    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "chunk-a",
                        "source_path": "policy.md",
                        "section_path": "退款政策",
                        "excerpt": "第一段",
                    },
                    {
                        "chunk_id": "chunk-b",
                        "source_path": "policy.md",
                        "section_path": "退款政策",
                        "excerpt": "第二段",
                    },
                ]
            }
        ]
    )

    assert [item["chunk_id"] for item in citations] == ["chunk-a", "chunk-b"]


def test_knowledge_citation_identity_keeps_same_title_chunks_in_final_agent_dedupe() -> None:
    """最终 AgentLoop 去重必须沿用切片身份，不能再次按展示标题截断证据。"""

    first = {"kind": "evidence", "title": "退款政策", "chunk_id": "chunk-a"}
    second = {"kind": "evidence", "title": "退款政策", "chunk_id": "chunk-b"}

    assert knowledge_citation_identity(first) != knowledge_citation_identity(second)


def test_restore_truncated_email_from_unique_cited_evidence() -> None:
    """证据只有一个匹配邮箱时恢复模型截断的完整值。"""

    reply = "请将材料发送至 ops@example... [1]"
    citations = [
        {
            "label": "[1]",
            "source_path": "employee-guide.md",
            "excerpt": "材料准备完成后发送至 ops@example.test。",
        }
    ]

    assert restore_truncated_atomic_references(reply, citations) == (
        "请将材料发送至 ops@example.test [1]"
    )


def test_restore_truncated_email_keeps_ambiguous_prefix_unchanged() -> None:
    """同一前缀对应多个证据邮箱时不得猜测补全。"""

    reply = "联系 ops@example... [1]"
    citations = [
        {
            "label": "[1]",
            "excerpt": "可联系 ops@example.test 或 ops@example.team。",
        }
    ]

    assert restore_truncated_atomic_references(reply, citations) == reply


def test_knowledge_citations_prefer_wiki_concepts_over_evidence_pack() -> None:
    citations = knowledge_citations_from_results(
        [
            {
                "selected_concepts": [
                    {
                        "concept_id": "sources/vue3-coding-standards",
                        "type": "Source Document",
                        "title": "前端编码规范",
                        "description": "Vue 3、Vite、TypeScript、组件编写和命名规范。",
                        "source_refs": [{"source_path": "vue3-coding-standards.md"}],
                    }
                ],
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_citation_demo",
                        "document_id": "kdoc_citation_demo",
                        "bucket_id": "kbucket_citation_demo",
                        "source_path": "citation-demo.md",
                        "section_path": "知识引用测试说明 / 引用规则",
                        "summary": "回答基于业务资料时必须展示可点击知识引用。",
                        "excerpt": "共格·序伴引用测试规则。",
                    }
                ],
            }
        ]
    )

    assert citations[0]["kind"] == "concept"
    assert citations[0]["title"] == "前端编码规范"
    assert citations[0]["source_path"] == "vue3-coding-standards.md"


def test_knowledge_citations_use_concept_content_instead_of_summary() -> None:
    content = "完整 Content 段落。" * 120
    citations = knowledge_citations_from_results(
        [
            {
                "selected_concepts": [
                    {
                        "concept_id": "sources/chatgpt-memory/sections/sec-4",
                        "type": "Source Section",
                        "title": "段落组 1",
                        "description": "段落组 1 摘要，不完整。",
                        "content": content,
                        "source_refs": [{"source_path": "memory.md"}],
                    }
                ],
            }
        ]
    )

    assert citations[0]["content"] == content
    assert citations[0]["excerpt"] == content
    assert citations[0]["summary"] == "段落组 1 摘要，不完整。"


def test_knowledge_citations_keep_long_evidence_excerpt_until_display_limit() -> None:
    excerpt = "引用片段" * 900
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_long_excerpt",
                        "document_id": "kdoc_long_excerpt",
                        "bucket_id": "kbucket_long_excerpt",
                        "source_path": "long-citation.md",
                        "section_path": "长引用测试",
                        "summary": "长引用摘要",
                        "excerpt": excerpt,
                    }
                ],
            }
        ]
    )

    assert citations[0]["excerpt"] == excerpt


def test_knowledge_citations_cap_evidence_excerpt_at_display_limit() -> None:
    excerpt = "x" * (CITATION_EXCERPT_CHAR_LIMIT + 16)
    citations = knowledge_citations_from_results(
        [
            {
                "evidence_pack": [
                    {
                        "chunk_id": "kchunk_capped_excerpt",
                        "document_id": "kdoc_capped_excerpt",
                        "bucket_id": "kbucket_capped_excerpt",
                        "source_path": "capped-citation.md",
                        "section_path": "引用上限测试",
                        "summary": "引用上限摘要",
                        "excerpt": excerpt,
                    }
                ],
            }
        ]
    )

    assert citations[0]["excerpt"] == excerpt[:CITATION_EXCERPT_CHAR_LIMIT]
