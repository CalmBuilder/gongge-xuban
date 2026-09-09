"""
@Time       : 2026/09/04
@Author     : zhanglp8181
@File       : test_vertical_ability_expert_depth.py
@CallChain  : pytest → 内置专家包/专家深度矩阵 → 全网情报员 Guidance/公共互联网验收
@Description: 验证全网情报员使用同一个 DynamicTaskAgent 覆盖全部专家分类和深度研究协议。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.experts.builtin import load_builtin_expert_package
from app.vertical_abilities import VerticalAbilityCatalog


MATRIX_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agent_reach_expert_depth_matrix.json"
EXPECTED_CATEGORIES = {
    "专业服务",
    "产品管理",
    "付费媒体",
    "医疗健康",
    "地理信息",
    "学术研究",
    "安全",
    "客户支持",
    "工程研发",
    "市场营销",
    "测试质量",
    "游戏开发",
    "空间计算",
    "设计创意",
    "财务金融",
    "销售",
    "项目管理",
}


def _load_matrix() -> list[dict[str, Any]]:
    """读取不含凭据和专家正文的公共互联网深度任务矩阵。"""

    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert all(isinstance(item, dict) for item in payload)
    return payload


def test_expert_depth_matrix_covers_current_builtin_categories_and_experts() -> None:
    """验证每个当前一级专家分类都有两个可追溯的真实专家候选。"""

    cases = _load_matrix()
    package = load_builtin_expert_package()
    by_path = {
        record.expert.parsed.upstream_path: record
        for record in package.records
    }

    assert len(cases) == len(EXPECTED_CATEGORIES) * 2
    assert {str(item["category"]) for item in cases} == EXPECTED_CATEGORIES
    assert len({str(item["case_id"]) for item in cases}) == len(cases)
    assert len({str(item["upstream_path"]) for item in cases}) == len(cases)

    counts = {category: 0 for category in EXPECTED_CATEGORIES}
    for case in cases:
        category = str(case["category"])
        path = str(case["upstream_path"])
        record = by_path.get(path)
        assert record is not None, path
        assert record.expert.translation.category_zh == category
        assert record.expert.translation.name_zh == str(case["expert_name"])
        assert str(case["research_question"]).strip()
        assert isinstance(case["focus_dimensions"], list)
        assert case["focus_dimensions"]
        counts[category] += 1

    assert counts == {category: 2 for category in EXPECTED_CATEGORIES}


def test_web_intelligence_guidance_publishes_task_scoped_agent_reach_depth_protocol() -> None:
    """验证深度协议是同一能力包的 Guidance，而不是第二套 Runtime 或用户限制。"""

    definition = VerticalAbilityCatalog().resolve("web-intelligence")
    protocol = definition.output_contract["research_depth_protocol"]

    assert protocol == {
        "protocol_id": "agent-reach-depth.v1",
        "stages": [
            "discover",
            "primary_read",
            "related_context",
            "cross_check",
            "report",
        ],
        "applicability": "task_scoped",
        "single_source_allowed": True,
        "read_depth_policy": {
            "multi_source_task_minimum_independent_reads": 2,
            "single_object_task_minimum_independent_reads": 1,
            "same_source_alias_or_reprint_not_independent": True,
        },
        "empty_result_policy": "有界改写或切换适用公共入口后仍为空时，保留 no_results、原因和已完成步骤",
        "public_source_policy": {
            "login_dependent_sources_optional": True,
            "substitute_with_public_sources": True,
            "depth_anchor": [
                "intelligence.web_search",
                "intelligence.web_read",
            ],
            "substitution_disclosure": "登录态来源不可用时，使用适用公共互联网来源并披露未验证平台原生口径",
        },
        "trace_authority": "Execution/Operation 回执",
    }
    assert "DynamicTaskAgent" not in definition.name
    assert "发现 → 原文/详情 → 关联信息 → 交叉核验 → 报告" in definition.instructions
    assert "至少读取两个彼此独立的实际来源" in definition.instructions
    assert "同一来源的别名、重定向或改写转载不能凑数" in definition.instructions
    assert "专家身份只是需求视角" in definition.instructions
    assert "登录态来源只是平台原生内容的增强来源" in definition.instructions
    assert "公共互联网替代，未验证平台原生口径" in definition.instructions
    assert "对每个最终未成功的垂直 Operation" in definition.instructions
    assert "不要重复相同失败请求" in definition.instructions
    assert "改用 intelligence.web_read" in definition.instructions
