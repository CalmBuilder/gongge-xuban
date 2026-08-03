"""使用平台模型生成结构化中文专家内容并执行保真校验。"""

from __future__ import annotations

import json
import re
from typing import Protocol

from pydantic import ValidationError

from app.experts.capability import ALLOWED_ANALYSIS_CAPABILITIES
from app.experts.parser import ParsedExpert
from app.experts.schema import ExpertTranslation


TRANSLATION_PROMPT = """把专家档案翻译为简体中文。保持 Markdown 层级、规则强度、模板、成功指标和示例边界。代码、命令、API、库名、变量、路径和 URL 必须逐字保留。不得增加工具权限、联网能力或原文没有的承诺。分析核心任务还需要哪些平台能力；每个判断都必须附一段可在英文原文精确匹配的 evidence。只有原文明示调用其他专家、跨专家交接或自动重试时才设置 orchestration_required=true。只返回 JSON：name_zh、description_zh、category_zh、tags_zh、markdown_zh、capability_analysis。"""

HIGH_RISK_KEYWORDS = ("法律", "法务", "财务", "金融", "医疗", "健康", "安全", "风控")
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
URL_RE = re.compile(r"https?://[^\s)>\]}`]+")
CATEGORY_LABELS = {
    "academic": "学术研究",
    "design": "设计创意",
    "engineering": "工程研发",
    "finance": "财务金融",
    "game-development": "游戏开发",
    "gis": "地理信息",
    "healthcare": "医疗健康",
    "marketing": "市场营销",
    "paid-media": "付费媒体",
    "product": "产品管理",
    "project-management": "项目管理",
    "sales": "销售",
    "security": "安全",
    "spatial-computing": "空间计算",
    "specialized": "专业服务",
    "support": "客户支持",
    "testing": "测试质量",
}
ORCHESTRATION_RE = re.compile(
    r"\b(?:spawn\b.{0,100}\bagent|"
    r"coordinate\b.{0,100}\b(?:AI|specialist|sub-?)agents?|"
    r"delegate\b.{0,120}\bto\b.{0,80}\b(?:AI|specialist|sub-?)agent|"
    r"automatically\s+retry)\b",
    re.IGNORECASE,
)


class TextGenerator(Protocol):
    def generate_text(self, system_prompt: str, user_payload: object) -> str: ...


class ExpertTranslationError(ValueError):
    """模型输出不完整、不保真或包含无证据能力判断。"""


def is_high_risk(category: str, tags: list[str]) -> bool:
    corpus = " ".join([category, *tags])
    return any(keyword in corpus for keyword in HIGH_RISK_KEYWORDS)


def preserve_original_translation(parsed: ParsedExpert) -> ExpertTranslation:
    """离线保留上游提示词，仅生成确定性展示字段和有原文证据的能力分析。"""
    orchestration_evidence = next(
        (
            line.strip()
            for line in parsed.source_markdown.splitlines()
            if line.strip() and ORCHESTRATION_RE.search(line)
        ),
        "",
    )
    orchestration_required = bool(orchestration_evidence)
    evidence = [parsed.description]
    if orchestration_evidence and orchestration_evidence not in evidence:
        evidence.append(orchestration_evidence)
    required_capabilities = ["prompt_reasoning"]
    if orchestration_required:
        required_capabilities.append("expert_orchestration")
    tags: list[str] = []
    for value in [parsed.category_original, *parsed.tools, *(item.name for item in parsed.services)]:
        normalized = value.strip()
        if normalized and normalized not in tags:
            tags.append(normalized)
    category = CATEGORY_LABELS.get(parsed.category_original, parsed.category_original)
    return ExpertTranslation(
        name_zh=parsed.name,
        description_zh=parsed.description,
        category_zh=category,
        tags_zh=tags[:12],
        markdown_zh=parsed.source_markdown,
        high_risk=is_high_risk(category, tags),
        capability_analysis={
            "required_capabilities": required_capabilities,
            "orchestration_required": orchestration_required,
            "core_execution_requires_external_capability": bool(
                parsed.tools or parsed.services or orchestration_required
            ),
            "evidence": evidence,
        },
    )


def _parse_json_response(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExpertTranslationError(f"Translation response is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ExpertTranslationError("Translation response must be a JSON object")
    return value


def _evidence_corpus(parsed: ParsedExpert) -> str:
    lines = [parsed.name, parsed.description, parsed.source_markdown]
    lines.extend(item for item in (parsed.vibe, parsed.author) if item)
    if parsed.tools:
        lines.append(f"tools: {', '.join(parsed.tools)}")
    for service in parsed.services:
        lines.extend([service.name, service.url, service.tier or ""])
    return "\n".join(lines)


def _validate_translation(parsed: ParsedExpert, translation: ExpertTranslation) -> None:
    preserved = [*CODE_BLOCK_RE.findall(parsed.source_markdown), *URL_RE.findall(parsed.source_markdown)]
    missing = [token for token in preserved if token not in translation.markdown_zh]
    if missing:
        raise ExpertTranslationError("Translation must preserve code blocks, commands and URLs")
    corpus = _evidence_corpus(parsed)
    if not translation.capability_analysis.evidence:
        raise ExpertTranslationError("capability evidence is required")
    if any(evidence not in corpus for evidence in translation.capability_analysis.evidence):
        raise ExpertTranslationError("capability evidence must exactly match source text")
    unknown = set(translation.capability_analysis.required_capabilities) - ALLOWED_ANALYSIS_CAPABILITIES
    if unknown:
        raise ExpertTranslationError(f"Unsupported capability names: {', '.join(sorted(unknown))}")


class ExpertTranslator:
    def __init__(self, llm: TextGenerator) -> None:
        self.llm = llm

    def translate(self, parsed: ParsedExpert) -> ExpertTranslation:
        payload = {
            "name": parsed.name,
            "description": parsed.description,
            "category": parsed.category_original,
            "vibe": parsed.vibe,
            "author": parsed.author,
            "tools": parsed.tools,
            "services": [service.model_dump(mode="json") for service in parsed.services],
            "markdown": parsed.source_markdown,
        }
        data = _parse_json_response(self.llm.generate_text(TRANSLATION_PROMPT, payload))
        data["high_risk"] = is_high_risk(
            str(data.get("category_zh") or ""),
            [str(item) for item in data.get("tags_zh", [])]
            if isinstance(data.get("tags_zh"), list)
            else [],
        )
        try:
            translation = ExpertTranslation.model_validate(data)
        except ValidationError as exc:
            raise ExpertTranslationError(f"Translation response schema is invalid: {exc}") from exc
        tags = []
        for tag in translation.tags_zh:
            normalized = tag.strip()
            if normalized and normalized not in tags:
                tags.append(normalized)
        if len(tags) > 12 or any(len(tag) > 32 for tag in tags):
            raise ExpertTranslationError("Translation tags exceed limits")
        normalized = translation.model_copy(update={"tags_zh": tags})
        _validate_translation(parsed, normalized)
        return normalized
