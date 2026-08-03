"""Agency Agents 固定二级分类词表、模式与加载校验。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


AGENCY_AGENTS_SOURCE_COMMIT = "459dce837db3bdfdc4763d3fefd1fd854e73c8f1"
DEFAULT_TAXONOMY_PATH = (
    Path(__file__).resolve().parent / "data" / "agency_agents_taxonomy_v1.json"
)

AGENCY_AGENTS_TAXONOMY: dict[str, frozenset[str]] = {
    "学术研究": frozenset({"人文社会", "心理与行为", "统计与方法"}),
    "设计创意": frozenset({"品牌与视觉", "用户体验", "内容与叙事", "包容与无障碍"}),
    "工程研发": frozenset(
        {
            "AI 与智能体",
            "前端与客户端",
            "后端与平台",
            "数据与数据库",
            "云原生与可靠性",
            "集成与自动化",
            "安全与身份",
            "嵌入式与物联网",
            "开发效能",
            "行业应用工程",
        }
    ),
    "财务金融": frozenset({"财务运营", "财务分析与规划", "投资研究", "税务策略"}),
    "游戏开发": frozenset(
        {
            "游戏设计与内容",
            "技术美术与音频",
            "Unity",
            "Unreal Engine",
            "Godot",
            "Roblox",
            "Blender",
        }
    ),
    "地理信息": frozenset(
        {"空间分析", "空间数据", "地图与可视化", "三维与实景", "GeoAI", "GIS 工程与咨询"}
    ),
    "医疗健康": frozenset({"临床证据", "医疗创新", "医疗系统"}),
    "市场营销": frozenset(
        {"搜索与增长", "内容与社交", "品牌与传播", "电商与市场本地化", "生命周期营销", "营销技术与分析"}
    ),
    "付费媒体": frozenset({"投放策略", "广告创意", "渠道运营", "测量与优化"}),
    "产品管理": frozenset({"产品战略", "产品发现", "产品运营", "增长与实验"}),
    "项目管理": frozenset({"项目交付", "敏捷与流程", "风险与治理", "跨团队协调"}),
    "销售": frozenset({"销售策略", "售前与解决方案", "客户开发", "交易与谈判", "销售运营"}),
    "安全": frozenset({"应用安全", "云与基础设施安全", "威胁与事件响应", "治理风险与合规", "身份安全"}),
    "空间计算": frozenset({"XR 体验", "空间交互", "三维内容", "空间平台工程"}),
    "专业服务": frozenset(
        {
            "战略与管理咨询",
            "法律与合规",
            "供应链与运营",
            "政府与公共服务",
            "教育与培训",
            "研究与分析",
            "创作与编辑",
            "人才与组织",
            "行业解决方案",
            "智能体与自动化咨询",
        }
    ),
    "客户支持": frozenset({"客户服务", "技术支持", "客户成功", "支持运营", "合规支持"}),
    "测试质量": frozenset({"测试策略", "自动化测试", "性能与可靠性测试", "安全测试", "专项质量工程"}),
}


class ExpertTaxonomyError(ValueError):
    """分类文件不完整、不一致或不可安全使用。"""


class TaxonomyEntry(BaseModel):
    """一个上游专家的固定分类结果。"""

    model_config = ConfigDict(frozen=True)

    upstream_path: str
    category: str
    subcategory: str
    subcategory_original: str = ""
    basis: Literal["upstream_directory", "curated_role_mapping"]


class TaxonomyDocument(BaseModel):
    """一版完整的 Agency Agents 分类文档。"""

    model_config = ConfigDict(frozen=True)

    version: Literal[1]
    source_code: Literal["agency-agents"]
    source_commit: Literal[AGENCY_AGENTS_SOURCE_COMMIT]
    experts: list[TaxonomyEntry]


class TaxonomyItem(BaseModel):
    """一次检查或写入中单个来源路径的结果。"""

    model_config = ConfigDict(frozen=True)

    upstream_path: str
    status: Literal[
        "ready",
        "updated",
        "skipped_unchanged",
        "missing",
        "unmapped_agent",
        "category_mismatch",
        "failed",
    ]
    agent_id: str | None = None
    category: str | None = None
    subcategory: str | None = None
    message: str | None = None


class TaxonomyResult(BaseModel):
    """可审计的分类检查或写入报告。"""

    model_config = ConfigDict(frozen=True)

    operation: Literal["check", "apply"]
    tenant_id: str
    taxonomy_version: int
    source_commit: str
    started_at: str
    finished_at: str | None = None
    result_path: Path
    items: list[TaxonomyItem] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts


def _validate_entry(entry: TaxonomyEntry) -> None:
    allowed = AGENCY_AGENTS_TAXONOMY.get(entry.category, frozenset())
    if entry.subcategory not in allowed:
        raise ExpertTaxonomyError(
            f"Invalid category/subcategory pair: {entry.category}/{entry.subcategory}"
        )
    path = PurePosixPath(entry.upstream_path)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ExpertTaxonomyError(f"Invalid upstream path: {entry.upstream_path}")
    if entry.basis == "upstream_directory":
        if len(path.parts) < 3 or not entry.subcategory_original:
            raise ExpertTaxonomyError(
                f"Invalid upstream directory basis: {entry.upstream_path}"
            )
        if path.parts[1] != entry.subcategory_original:
            raise ExpertTaxonomyError(
                f"Invalid upstream directory basis: {entry.upstream_path}"
            )
    elif entry.subcategory_original:
        raise ExpertTaxonomyError(
            f"Curated mapping cannot claim an upstream subcategory: {entry.upstream_path}"
        )


def load_agency_agents_taxonomy(
    path: Path | None = None,
    *,
    expected_count: int | None = 263,
) -> TaxonomyDocument:
    """读取并完整校验版本化分类文档。"""

    source = path or DEFAULT_TAXONOMY_PATH
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        taxonomy = TaxonomyDocument.model_validate(value)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ExpertTaxonomyError(f"Invalid taxonomy document: {exc}") from exc
    if expected_count is not None and len(taxonomy.experts) != expected_count:
        raise ExpertTaxonomyError(
            f"Expected {expected_count} taxonomy entries, got {len(taxonomy.experts)}"
        )
    seen: set[str] = set()
    for entry in taxonomy.experts:
        if not entry.upstream_path or entry.upstream_path in seen:
            raise ExpertTaxonomyError(f"Duplicate upstream path: {entry.upstream_path}")
        seen.add(entry.upstream_path)
        _validate_entry(entry)
    return taxonomy
