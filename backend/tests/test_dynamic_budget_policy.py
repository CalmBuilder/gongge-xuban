"""
@Time       : 2026/08/15 10:45
@Author     : zhanglp8181
@File       : test_dynamic_budget_policy.py
@CallChain  : pytest → dynamic_tasks.budget_policy → planner/Execution 预算快照
@Description: 验证动态任务分级选择、阶段限额和防模型放大契约。
"""

from app.dynamic_tasks.budget_policy import (
    DynamicBudgetTier,
    dynamic_budget_profile,
    select_dynamic_budget,
)


def _resource(*, mime_type: str, size_bytes: int) -> dict[str, object]:
    """构造只含服务端选档所需事实的资源投影。"""

    return {"mime_type": mime_type, "size_bytes": size_bytes}


def test_budget_tiers_follow_server_owned_complexity_facts() -> None:
    """确认纯对话、单附件、视觉/Skill附件和显式大批量分别选档。"""

    assert select_dynamic_budget(goal="制定发布计划", resources=(), guidance_count=0).tier == (
        DynamicBudgetTier.INTERACTIVE
    )
    assert select_dynamic_budget(
        goal="分析文档", resources=(_resource(mime_type="text/csv", size_bytes=1024),), guidance_count=0
    ).tier == DynamicBudgetTier.STANDARD
    assert select_dynamic_budget(
        goal="分析图片",
        resources=(_resource(mime_type="image/png", size_bytes=1024),),
        guidance_count=0,
    ).tier == DynamicBudgetTier.EXTENDED
    assert select_dynamic_budget(
        goal="用 Skill 辅助分析附件",
        resources=(_resource(mime_type="text/markdown", size_bytes=1024),),
        guidance_count=1,
    ).tier == DynamicBudgetTier.EXTENDED
    assert select_dynamic_budget(
        goal="请后台全量批量处理所有文件",
        resources=tuple(
            _resource(mime_type="application/pdf", size_bytes=4 * 1024 * 1024)
            for _ in range(8)
        ),
        guidance_count=0,
    ).tier == DynamicBudgetTier.BACKGROUND


def test_background_wording_alone_cannot_expand_execution_budget() -> None:
    """拒绝仅凭用户或附件中的“后台”文字把小任务放大到60分钟。"""

    selected = select_dynamic_budget(
        goal="请后台处理这一个小文件",
        resources=(_resource(mime_type="text/plain", size_bytes=128),),
        guidance_count=0,
    )

    assert selected.tier == DynamicBudgetTier.STANDARD
    assert selected.max_runtime_seconds == 900


def test_complex_goal_without_resources_gets_long_runtime_budget() -> None:
    """确认无附件的持久复杂题不会误用交互级300秒上限。"""

    selected = select_dynamic_budget(
        goal="请通过持久、可恢复、可校验的 DynamicTaskAgent 完成事故复盘并生成报告",
        resources=(),
        guidance_count=0,
    )

    assert selected.tier == DynamicBudgetTier.EXTENDED
    assert selected.max_runtime_seconds == 1_800
    assert selected.max_model_call_seconds == 600

    recovery_wording = select_dynamic_budget(
        goal="请制定持久计划，完成故障恢复和结果校验后再交付",
        resources=(),
        guidance_count=0,
    )
    assert recovery_wording.tier == DynamicBudgetTier.EXTENDED


def test_multi_skill_goal_gets_runtime_budget_for_guidance_replay() -> None:
    """确认多 Skill 组合只提升运行时容量，不改变普通动态能力的开放语义。"""

    selected = select_dynamic_budget(
        goal="按多个 Skill 完成可审计交付",
        resources=(),
        guidance_count=3,
    )

    assert selected.tier == DynamicBudgetTier.EXTENDED
    assert selected.max_model_calls == 20


def test_simple_plain_goal_keeps_interactive_budget() -> None:
    """确认复杂题放宽等待不会把普通短问答一并升级。"""

    selected = select_dynamic_budget(
        goal="今天星期几？",
        resources=(),
        guidance_count=0,
    )

    assert selected.tier == DynamicBudgetTier.INTERACTIVE
    assert selected.max_runtime_seconds == 300


def test_profile_snapshot_freezes_total_and_stage_limits() -> None:
    """确认 Execution 快照同时携带墙钟、调用、token、读取、视觉和渲染限额。"""

    profile = dynamic_budget_profile(DynamicBudgetTier.EXTENDED)
    snapshot = profile.snapshot()

    assert snapshot == {
        "tier": "extended",
        "max_steps": 20,
        "max_tool_calls": 24,
        "max_model_calls": 20,
        "max_input_tokens": 240_000,
        "max_output_tokens": 48_000,
        "max_total_tokens": 288_000,
        "max_runtime_seconds": 1_800,
        "max_model_call_seconds": 600,
        "max_parallel_read_seconds": 240,
        "max_visual_review_seconds": 600,
        "max_renderer_seconds": 300,
        "policy_version": "dynamic-budget-v1",
    }
