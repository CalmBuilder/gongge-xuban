"""
@Time       : 2026/08/15 10:30
@Author     : zhanglp8181
@File       : budget_policy.py
@CallChain  : DynamicTaskAgent.start_task → select_dynamic_budget → DynamicTaskPlanner/Execution
@Description: 以服务端可重放规则选择动态任务分级预算，并生成可冻结的阶段限额。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum


class DynamicBudgetTier(StrEnum):
    """声明可对用户解释、可在 Execution 中冻结的四档预算。"""

    INTERACTIVE = "interactive"
    STANDARD = "standard"
    EXTENDED = "extended"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class DynamicBudgetProfile:
    """保存单个预算档位的总量上限和阶段超时上限。"""

    tier: DynamicBudgetTier
    max_steps: int
    max_tool_calls: int
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_runtime_seconds: int
    max_model_call_seconds: int
    max_parallel_read_seconds: int
    max_visual_review_seconds: int
    max_renderer_seconds: int

    def planner_kwargs(self) -> dict[str, int]:
        """只投影 DynamicTaskPlanner 识别的总量预算字段。"""

        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_model_calls": self.max_model_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_runtime_seconds": self.max_runtime_seconds,
        }

    def snapshot(self) -> dict[str, int | str]:
        """生成持久化预算快照，后续运行不再重新推断档位。"""

        payload = asdict(self)
        payload["tier"] = self.tier.value
        payload["policy_version"] = "dynamic-budget-v1"
        return payload


_PROFILES: dict[DynamicBudgetTier, DynamicBudgetProfile] = {
    DynamicBudgetTier.INTERACTIVE: DynamicBudgetProfile(
        tier=DynamicBudgetTier.INTERACTIVE,
        max_steps=6,
        max_tool_calls=4,
        max_model_calls=6,
        max_input_tokens=60_000,
        max_output_tokens=16_000,
        max_total_tokens=76_000,
        max_runtime_seconds=300,
        max_model_call_seconds=180,
        max_parallel_read_seconds=60,
        max_visual_review_seconds=120,
        max_renderer_seconds=120,
    ),
    DynamicBudgetTier.STANDARD: DynamicBudgetProfile(
        tier=DynamicBudgetTier.STANDARD,
        max_steps=10,
        max_tool_calls=9,
        max_model_calls=12,
        max_input_tokens=120_000,
        max_output_tokens=24_000,
        max_total_tokens=144_000,
        max_runtime_seconds=900,
        max_model_call_seconds=600,
        max_parallel_read_seconds=120,
        max_visual_review_seconds=300,
        max_renderer_seconds=180,
    ),
    DynamicBudgetTier.EXTENDED: DynamicBudgetProfile(
        tier=DynamicBudgetTier.EXTENDED,
        max_steps=20,
        max_tool_calls=24,
        max_model_calls=20,
        max_input_tokens=240_000,
        max_output_tokens=48_000,
        max_total_tokens=288_000,
        max_runtime_seconds=1_800,
        max_model_call_seconds=600,
        max_parallel_read_seconds=240,
        max_visual_review_seconds=600,
        max_renderer_seconds=300,
    ),
    DynamicBudgetTier.BACKGROUND: DynamicBudgetProfile(
        tier=DynamicBudgetTier.BACKGROUND,
        max_steps=30,
        max_tool_calls=36,
        max_model_calls=30,
        max_input_tokens=480_000,
        max_output_tokens=64_000,
        max_total_tokens=544_000,
        max_runtime_seconds=3_600,
        max_model_call_seconds=600,
        max_parallel_read_seconds=300,
        max_visual_review_seconds=900,
        max_renderer_seconds=600,
    ),
}

_BACKGROUND_MARKERS = ("后台", "长时", "全量", "批量处理", "逐文件", "所有文件")
_EXTENDED_MARKERS = (
    "ocr",
    "扫描件",
    "视觉核验",
    "交叉比对",
    "冲突核验",
    "逐项核验",
    "生成报告",
    "可恢复",
    "故障恢复",
    "持久计划",
    "结果校验",
    "结果验证",
    "完整闭环",
    "多阶段",
    "多步",
)
_VISUAL_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def dynamic_budget_profile(tier: DynamicBudgetTier | str) -> DynamicBudgetProfile:
    """按稳定档位返回不可变预算，未知值拒绝而不默认放大。"""

    return _PROFILES[DynamicBudgetTier(str(tier))]


def select_dynamic_budget(
    *,
    goal: str,
    resources: Sequence[Mapping[str, object]],
    guidance_count: int,
) -> DynamicBudgetProfile:
    """依据服务端文件事实和用户目标选档，不接受模型自报复杂度。"""

    normalized_goal = " ".join(goal.casefold().split())
    resource_count = len(resources)
    total_bytes = sum(
        max(0, int(item.get("size_bytes") or 0))
        for item in resources
        if isinstance(item, Mapping)
    )
    has_visual = any(
        str(item.get("mime_type") or "").casefold() in _VISUAL_MIME_TYPES
        for item in resources
        if isinstance(item, Mapping)
    )
    total_elements = sum(int(item.get("element_count") or 0) for item in resources)
    total_pages = sum(int(item.get("page_count") or 0) for item in resources)
    total_sheets = sum(int(item.get("sheet_count") or 0) for item in resources)
    total_slides = sum(int(item.get("slide_count") or 0) for item in resources)
    explicitly_background = any(marker in normalized_goal for marker in _BACKGROUND_MARKERS)
    high_volume = (
        resource_count >= 8
        or total_bytes >= 24 * 1024 * 1024
        or total_elements >= 500
        or total_pages >= 30
        or total_sheets >= 8
        or total_slides >= 30
    )
    if explicitly_background and high_volume:
        return _PROFILES[DynamicBudgetTier.BACKGROUND]
    extended_goal = any(marker in normalized_goal for marker in _EXTENDED_MARKERS)
    if (
        has_visual
        or resource_count >= 3
        or total_bytes >= 8 * 1024 * 1024
        or total_elements >= 100
        or total_pages >= 10
        or total_sheets >= 3
        or total_slides >= 10
        or (resource_count > 0 and guidance_count > 0)
        or (resource_count > 0 and extended_goal)
        # 纯对话也可能是需要持久计划、恢复和结果核验的复杂任务。不能因为
        # 没有附件就把它压回 300 秒交互预算；用户目标本身是服务端可重放的
        # 选档事实，而不是模型自报复杂度。后台批量任务仍需同时满足上面的
        # 规模门槛，避免仅凭文案把小任务放大到 60 分钟。
        or extended_goal
    ):
        return _PROFILES[DynamicBudgetTier.EXTENDED]
    if resource_count > 0 or guidance_count > 0:
        return _PROFILES[DynamicBudgetTier.STANDARD]
    return _PROFILES[DynamicBudgetTier.INTERACTIVE]
