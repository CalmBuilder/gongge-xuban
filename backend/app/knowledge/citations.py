"""
@Time       : 2026/07/27 14:45
@Author     : zhanglp8181
@File       : citations.py
@CallChain  : KnowledgeService/AgentLoop → 引用提取与回复规范化 → Message metadata
@Description: 生成、去重和重编号知识引用，并修复可唯一确认的原子引用截断。
"""

from __future__ import annotations

import re
from typing import Any

CITATION_EXCERPT_CHAR_LIMIT = 6000
CITATION_SUMMARY_CHAR_LIMIT = 800
CONCEPT_EXCERPT_CHAR_LIMIT = 2400
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+(?![\w.-])",
    re.IGNORECASE,
)
TRUNCATED_EMAIL_PATTERN = re.compile(
    r"(?P<prefix>[A-Z0-9._%+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)*)"
    r"(?P<ellipsis>\.{3}|…)",
    re.IGNORECASE,
)


def compact_knowledge_citation_labels(
    content: str,
    citations: object,
) -> tuple[str, list[dict[str, Any]]]:
    """Keep cited sources and renumber them by first appearance in the reply."""
    if not isinstance(citations, list) or not citations:
        return content, []

    citations_by_label: dict[int, dict[str, Any]] = {}
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        label_match = re.fullmatch(r"\[(\d+)\]", str(citation.get("label") or "").strip())
        label = int(label_match.group(1)) if label_match else index
        citations_by_label.setdefault(label, citation)

    ordered_labels: list[int] = []
    for match in re.finditer(r"\[(\d+)\]", content):
        label = int(match.group(1))
        if label in citations_by_label and label not in ordered_labels:
            ordered_labels.append(label)

    if not ordered_labels:
        return content, []

    label_mapping = {old_label: index for index, old_label in enumerate(ordered_labels, start=1)}

    def replace_label(match: re.Match[str]) -> str:
        old_label = int(match.group(1))
        new_label = label_mapping.get(old_label)
        return f"[{new_label}]" if new_label is not None else match.group(0)

    compacted_content = re.sub(r"\[(\d+)\]", replace_label, content)
    compacted_citations = [
        {**citations_by_label[old_label], "label": f"[{label_mapping[old_label]}]"}
        for old_label in ordered_labels
    ]
    return compacted_content, compacted_citations


def restore_truncated_atomic_references(content: str, citations: object) -> str:
    """仅在引用证据能唯一确认时，把模型省略号截断的邮箱恢复为完整原子值。"""

    if not content or not isinstance(citations, list):
        return content
    evidence_text = "\n".join(
        str(citation.get(field) or "")
        for citation in citations
        if isinstance(citation, dict)
        for field in ("content", "excerpt", "summary", "source_path")
    )
    evidence_emails = {
        match.group(0)
        for match in EMAIL_PATTERN.finditer(evidence_text)
    }
    if not evidence_emails:
        return content

    def replace(match: re.Match[str]) -> str:
        """按截断前缀寻找唯一证据邮箱，存在歧义时保持原文。"""

        prefix = match.group("prefix")
        candidates = {
            email
            for email in evidence_emails
            if email.lower().startswith(prefix.lower())
        }
        return next(iter(candidates)) if len(candidates) == 1 else match.group(0)

    return TRUNCATED_EMAIL_PATTERN.sub(replace, content)


def _compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _normalize_identity(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _semantic_identity(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _display_title(value: str) -> str:
    return _compact(value, 72)


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    def add(kind: str, identity: str, payload: dict[str, Any]) -> None:
        if len(citations) >= limit:
            return
        normalized = _normalize_identity(identity)
        title_identity = _normalize_identity(str(payload.get("title") or ""))
        if not normalized or normalized in seen_identities or (title_identity and title_identity in seen_identities):
            return
        seen_identities.add(normalized)
        if title_identity:
            seen_identities.add(title_identity)
        citations.append(
            {
                "id": f"kref_{len(citations) + 1}",
                "label": f"[{len(citations) + 1}]",
                "kind": kind,
                **payload,
            }
        )

    for result in knowledge_results:
        for concept in result.get("selected_concepts") or []:
            if not isinstance(concept, dict):
                continue
            concept_id = str(concept.get("concept_id") or concept.get("id") or "").strip()
            title = _display_title(str(concept.get("title") or concept_id or "Wiki 概念"))
            description = str(concept.get("description") or "").strip()
            content = str(concept.get("content") or concept.get("content_excerpt") or "").strip()
            excerpt = content or description
            source_refs = concept.get("source_refs") if isinstance(concept.get("source_refs"), list) else []
            source_path = ""
            if source_refs and isinstance(source_refs[0], dict):
                source_path = str(source_refs[0].get("source_path") or source_refs[0].get("document_id") or "")
            add(
                "concept",
                concept_id or title,
                {
                    "title": title,
                    "source_path": source_path,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": description[:CITATION_SUMMARY_CHAR_LIMIT],
                    "concept_id": concept_id,
                    "concept_type": concept.get("type"),
                },
            )

        for item in result.get("evidence_pack") or []:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("content") or item.get("excerpt") or "").strip()
            summary = str(item.get("summary") or "").strip()
            section_path = str(item.get("section_path") or "").strip()
            source_path = str(item.get("source_path") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = _display_title(section_path or source_path or summary or "知识片段")
            identity = _semantic_identity(section_path or summary or f"{source_path}:{excerpt[:120]}" or chunk_id)
            add(
                "evidence",
                identity,
                {
                    "title": title,
                    "source_path": source_path,
                    "section_path": section_path,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": summary[:CITATION_SUMMARY_CHAR_LIMIT],
                    "confidence_reason": str(item.get("confidence_reason") or ""),
                    "document_id": item.get("document_id"),
                    "bucket_id": item.get("bucket_id"),
                    "chunk_id": chunk_id,
                },
            )

        if not citations:
            for item in result.get("okf_citations") or []:
                if not isinstance(item, dict):
                    continue
                concept_id = str(item.get("concept_id") or "").strip()
                target = str(item.get("target") or "").strip()
                label = str(item.get("label") or "").strip()
                title = _display_title(str(item.get("title") or concept_id or "OKF 引用"))
                add(
                    "okf",
                    f"{concept_id}:{target or label}",
                    {
                        "title": title,
                        "source_path": target,
                        "excerpt": label,
                        "concept_id": concept_id,
                    },
                )
    return citations
