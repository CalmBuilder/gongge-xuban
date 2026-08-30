"""
@Time       : 2026/08/04 02:25
@Author     : zhanglp8181
@File       : test_dynamic_task_planner.py
@CallChain  : pytest → DynamicTaskPlanner → JSON client/NormalizedPlan
@Description: 验证动态规划能力最小披露、任务契约冻结和预算收紧。
"""

from __future__ import annotations

import re

import pytest
import app.dynamic_tasks.planner_service as planner_service

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.dynamic_tasks.planner_service import (
    DynamicTaskPlanner,
    DynamicTaskPlannerError,
    _force_guidance_phase_gate,
    _guidance_source_contract,
    _goal_has_workspace_intent,
    _planner_guidance_candidate_catalog,
    _repair_covers_loaded_skills,
    _repair_guidance_phase_continuity,
    _repair_terminal_answer_steps,
    _repair_guidance_identity_fields,
    _strip_platform_owned_attachment_steps,
    _validate_clarification_semantics,
    _validate_guidance_step_alignment,
)
from app.dynamic_tasks.planning import (
    DynamicPlanDraft,
    NormalizedPlan,
    PlanStep,
    SuccessCriterion,
    guidance_principle_candidates,
    normalize_plan_draft,
)


class _Client:
    """记录模型可见 payload 并返回完整计划草案。"""

    def __init__(self) -> None:
        self.payload: dict | None = None

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """模拟 provider 完整 JSON 响应，并尝试改写目标供服务端纠正。"""

        self.payload = user_payload
        return {
            "goal": "模型擅自改写的目标",
            "success_criteria": [
                {"id": "model_changed", "type": "assertion", "spec": {"required": False}}
            ],
            "expected_artifacts": [
                {
                    "artifact_key": "验收报告",
                    "filename": "验收报告.md",
                    "mime_type": "text/markdown",
                    "content_source": "result.markdown",
                    "required": True,
                }
            ],
            "steps": [
                {
                    "draft_id": "contracts",
                    "title": "查询合同",
                    "kind": "tool.read",
                    "capability_refs": ["contract.query"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成简报",
                    "kind": "answer",
                    "required": False,
                    "depends_on": ["contracts"],
                },
            ],
        }


def _snapshot() -> CapabilitySnapshot:
    """返回包含模型视图与审计视图差异的只读能力。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "tool_contract",
        "tenant_id": "tenant_demo",
        "name": "contract.query",
        "contract": {"risk_class": "read"},
        "model_view": {"name": "contract.query", "input_schema": {"partner": "string"}},
        "user_view": {"name": "合同查询"},
        "audit_view": {"url": "https://internal.invalid", "authorization": "secret"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


def _guidance_snapshot(*, invocation_policy: str = "model_allowed") -> CapabilitySnapshot:
    """构造模型目录含描述但内部快照含敏感正文的固定指导能力。"""

    payload = {
        "capability_type": "general_skill",
        "capability_id": "genskill_diagnose",
        "tenant_id": "tenant_demo",
        "name": "diagnosing-bugs",
        "contract": {
            "usage_mode": "planning_guidance",
            "revision_id": "gsrev_diagnose_1",
            "invocation_policy": invocation_policy,
        },
        "model_view": {
            "id": "genskill_diagnose",
            "slug": "diagnosing-bugs",
            "name": "诊断缺陷",
            "description": "先复现，再形成可证伪假设。",
            "usage_mode": "planning_guidance",
            "revision_id": "gsrev_diagnose_1",
            "revision_number": 1,
            "skill_markdown": "# 不应在选择阶段披露的完整正文",
            "resources": [{"path": "references/debug.md"}],
        },
        "user_view": {"name": "诊断缺陷"},
        "audit_view": {"content_checksum": "secret-checksum"},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


class _GuidanceSelectionClient:
    """记录无正文选择 payload，并返回固定 Skill 或越权 user-only 名称。"""

    def __init__(self, selected_name: str) -> None:
        """保存模型将提出的名称。"""

        self.selected_name = selected_name
        self.payload: dict | None = None

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回单个选择并保留供应商实际可见内容。"""

        self.payload = user_payload
        return {
            "selected_skill_names": [self.selected_name],
            "reason": "该任务需要严格诊断纪律",
        }


class _AnswerSectionsClient:
    """模拟供应商把纯报告章节误拆为多个 answer 步骤。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回无能力调用的两个 answer，由服务端折叠为唯一终态。"""

        return {
            "goal": "生成报告",
            "success_criteria": user_payload["success_criteria"],
            "steps": [
                {
                    "draft_id": "outline",
                    "title": "形成报告结构",
                    "kind": "answer",
                    "required": True,
                },
                {
                    "draft_id": "final",
                    "title": "交付最终报告",
                    "kind": "answer",
                    "required": False,
                    "depends_on": ["outline"],
                },
            ],
        }


def test_guidance_selector_discloses_catalog_only_and_rejects_user_only() -> None:
    """动态选择阶段看不到正文/资源，且 user-only 即使模型点名也不进入自动选择。"""

    client = _GuidanceSelectionClient("diagnosing-bugs")
    criterion = SuccessCriterion(id="fixed", type="assertion", spec={"required": True})
    selection = DynamicTaskPlanner(client).select_guidance_skills(
        goal="诊断并修复问题",
        success_criteria=(criterion,),
        catalog=(_guidance_snapshot(),),
    )

    assert selection.selected_skill_names == ("diagnosing-bugs",)
    assert client.payload is not None
    assert "skill_markdown" not in str(client.payload)
    assert "resources" not in str(client.payload)
    assert "secret-checksum" not in str(client.payload)

    user_only_client = _GuidanceSelectionClient("diagnosing-bugs")
    user_only = DynamicTaskPlanner(user_only_client).select_guidance_skills(
        goal="诊断并修复问题",
        success_criteria=(criterion,),
        catalog=(_guidance_snapshot(invocation_policy="user_only"),),
    )
    assert user_only.selected_skill_names == ()
    assert user_only_client.payload is None


def test_planner_collapses_answer_only_sections_into_single_terminal_step() -> None:
    """纯文本章节不创造多个终态，含任何能力步骤的计划仍不适用此修复。"""

    criterion = SuccessCriterion(id="report_ready", type="assertion", spec={"required": True})
    plan = DynamicTaskPlanner(_AnswerSectionsClient()).create_plan(
        goal="生成报告",
        success_criteria=(criterion,),
        capabilities=(),
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].kind == "answer"
    assert plan.steps[0].required is True
    assert plan.steps[0].depends_on == ()


def test_repair_terminal_answer_steps_preserves_required_predecessors() -> None:
    """末次 repair 合并重复 answer 时保留前置步骤、依赖和指导引用。"""

    repaired = _repair_terminal_answer_steps(
        {
            "steps": [
                {
                    "draft_id": "read_input",
                    "title": "读取受管输入",
                    "kind": "tool.read",
                    "required": True,
                },
                {
                    "draft_id": "outline",
                    "title": "形成报告结构",
                    "kind": "answer",
                    "required": True,
                    "depends_on": ["read_input"],
                    "guidance_skill_refs": ["skill_use_a"],
                },
                {
                    "draft_id": "final",
                    "title": "交付最终报告",
                    "kind": "answer",
                    "required": False,
                    "depends_on": ["outline"],
                    "guidance_skill_refs": ["skill_use_b"],
                },
            ]
        }
    )

    steps = repaired["steps"]
    assert [step["draft_id"] for step in steps] == ["read_input", "final"]
    assert steps[-1]["required"] is True
    assert steps[-1]["depends_on"] == ["read_input"]
    assert steps[-1]["guidance_skill_refs"] == ["skill_use_b", "skill_use_a"]


def test_planner_only_discloses_model_view_and_freezes_server_contract() -> None:
    """验证审计/连接信息不进模型，目标、成功标准、预算和 step key 均由服务端裁决。"""

    client = _Client()
    criterion = SuccessCriterion(
        id="brief_ready",
        type="assertion",
        spec={"required": True},
    )
    plan = DynamicTaskPlanner(
        client,
        max_steps=4,
        max_tool_calls=2,
        max_model_calls=5,
    ).create_plan(
        goal="生成续约风险简报",
        success_criteria=(criterion,),
        capabilities=(_snapshot(),),
    )

    assert client.payload is not None
    assert client.payload["capabilities"] == [
        {
            "name": "contract.query",
            "input_schema": {"partner": "string"},
            "capability_type": "tool",
            "risk_class": "read",
            "allowed_step_kind": "tool.read",
        }
    ]
    assert set(client.payload["output_contract"]) == {
        "goal",
        "success_criteria",
        "constraints",
        "assumptions",
        "expected_artifacts",
        "guidance_requirements",
        "steps",
    }
    assert "draft_id" in client.payload["output_contract"]["steps"][0]
    assert "逐字复制" in client.payload["output_contract"]["steps"][0]["capability_refs"][0]
    assert "internal.invalid" not in str(client.payload)
    assert "authorization" not in str(client.payload)
    assert "audit_view" not in str(client.payload)
    assert plan.goal == "生成续约风险简报"
    assert plan.success_criteria == (criterion,)
    assert plan.expected_artifacts[0]["artifact_key"] == "artifact_01"
    assert plan.steps[-1].kind == "answer"
    assert plan.steps[-1].required is True
    assert plan.budget == {
        "max_steps": 4,
        "max_tool_calls": 2,
        "max_model_calls": 5,
        "max_input_tokens": 120_000,
        "max_output_tokens": 24_000,
        "max_total_tokens": 144_000,
        "max_runtime_seconds": 900,
    }
    assert plan.steps[0].step_key.startswith("step_01_")


class _UnavailableCapabilityClient:
    """模拟模型在无知识目录时仍虚构 knowledge 步骤。"""

    def __init__(self) -> None:
        """记录有限修复调用次数。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """确认动态 allowed kinds 已移除 knowledge，但仍返回越权草案供服务端拒绝。"""

        self.calls += 1
        assert "knowledge" not in user_payload["limits"]["allowed_step_kinds"]
        assert "knowledge" not in user_payload["output_contract"]["steps"][0]["kind"]
        if self.calls == 2:
            assert user_payload["repair"]["failure_code"] == "DYNAMIC_PLAN_SEMANTIC_INVALID"
        return {
            "goal": "生成简报",
            "success_criteria": [
                {"id": "brief_ready", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "search",
                    "title": "检索知识",
                    "kind": "knowledge",
                    "capability_refs": [],
                },
                {
                    "draft_id": "answer",
                    "title": "形成简报",
                    "kind": "answer",
                    "depends_on": ["search"],
                },
            ],
        }


class _ContractEchoRepairClient:
    """模拟 provider 两次回显 output_contract，第三次才返回完整计划。"""

    def __init__(self) -> None:
        """记录形状修复的有界调用次数。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """前两次返回宿主 schema 回显，第三次返回唯一 answer。"""

        self.calls += 1
        if self.calls < 3:
            if self.calls == 2:
                assert user_payload["repair"]["attempt"] == 1
            return {
                "goal": user_payload["goal"],
                "success_criteria": user_payload["success_criteria"],
                "output_contract": {"steps": [{"draft_id": "answer", "kind": "answer"}]},
            }
        assert user_payload["repair"]["attempt"] == 2
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": [
                {"draft_id": "answer", "title": "形成结果", "kind": "answer"},
            ],
        }


def test_planner_has_bounded_shape_repair_for_contract_echo() -> None:
    """契约回显只额外修复一次，成功后仍走完整计划语义校验。"""

    client = _ContractEchoRepairClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="整理分析结论",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
    )

    assert client.calls == 3
    assert [step.kind for step in plan.steps] == ["answer"]


def test_planner_rejects_knowledge_step_when_capability_is_not_frozen() -> None:
    """无 knowledge snapshot 时即使模型违约也不得持久化不可执行计划。"""

    criterion = SuccessCriterion(
        id="brief_ready",
        type="assertion",
        spec={"required": True},
    )
    client = _UnavailableCapabilityClient()
    with pytest.raises(DynamicTaskPlannerError, match="未冻结的知识能力") as rejected:
        DynamicTaskPlanner(client).create_plan(
            goal="生成简报",
            success_criteria=(criterion,),
            capabilities=(_snapshot(),),
        )
    assert rejected.value.code == "DYNAMIC_PLAN_SEMANTIC_INVALID"
    assert client.calls == 2


class _ManagedInputOnlyClient:
    """模拟只有受管附件、没有工具目录时生成最终回答。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """确认附件不会被错误投影为可规划的 tool.read 能力。"""

        assert "tool.read" not in user_payload["limits"]["allowed_step_kinds"]
        assert "自动读取" in user_payload["input_resource_contract"]
        return {
            "goal": "附件分析",
            "success_criteria": [
                {"id": "answer_ready", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "分析受管附件",
                    "kind": "answer",
                }
            ],
        }


def test_managed_input_does_not_expose_unfrozen_tool_read_to_planner() -> None:
    """附件由 Runtime 落 input.read Operation，规划器不得虚构同名工具能力。"""

    plan = DynamicTaskPlanner(_ManagedInputOnlyClient()).create_plan(
        goal="附件分析",
        success_criteria=(
            SuccessCriterion(id="answer_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
        input_resources=({"resource_id": "resource_demo", "filename": "demo.csv"},),
    )

    assert [step.kind for step in plan.steps] == ["answer"]


def test_platform_owned_attachment_read_step_is_removed_and_dependencies_rewired() -> None:
    """模型误规划附件业务读取时，宿主消解该步骤且保留最终 answer 汇聚。"""

    criterion = SuccessCriterion(
        id="answer_ready",
        type="assertion",
        spec={"required": True},
    )
    plan = NormalizedPlan(
        goal="附件分析",
        success_criteria=(criterion,),
        steps=(
            PlanStep(
                step_key="read_attachment",
                title="读取图片附件",
                kind="tool.read",
                capability_refs=("contract.query",),
            ),
            PlanStep(
                step_key="answer",
                title="回答",
                kind="answer",
                depends_on=("read_attachment",),
            ),
        ),
        budget={},
    )

    repaired = _strip_platform_owned_attachment_steps(plan, has_input_resources=True)

    assert [step.step_key for step in repaired.steps] == ["answer"]
    assert repaired.steps[0].depends_on == ()
    assert _strip_platform_owned_attachment_steps(plan, has_input_resources=False) is plan


class _ManagedFormulaCapabilityRepairClient:
    """首轮错误询问内建table.compute，修复轮改为由answer前Runtime自动复核。"""

    def __init__(self) -> None:
        """初始化调用次数以证明服务端语义门禁触发了规划修复。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """模拟真实模型把平台内建计算误认为需由用户提供的外部能力。"""

        self.calls += 1
        assert "table.compute" in user_payload["input_resource_contract"]
        steps = (
            [
                {
                    "draft_id": "ask_compute",
                    "title": "请用户确认允许使用table.compute",
                    "kind": "clarification",
                },
                {
                    "draft_id": "answer",
                    "title": "输出公式核验",
                    "kind": "answer",
                    "depends_on": ["ask_compute"],
                },
            ]
            if self.calls == 1
            else [
                {
                    "draft_id": "answer",
                    "title": "输出平台公式核验",
                    "kind": "answer",
                }
            ]
        )
        return {
            "goal": "核验公式",
            "success_criteria": [
                {"id": "answer_ready", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": steps,
        }


def test_managed_formula_compute_is_automatic_and_cannot_wait_for_user_capability() -> None:
    """规划器不得因平台内建table.compute不存在于外部能力目录而让Execution等待。"""

    client = _ManagedFormulaCapabilityRepairClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="核验公式",
        success_criteria=(
            SuccessCriterion(id="answer_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
        input_resources=({"resource_id": "resource_xlsx", "filename": "actuals.xlsx"},),
    )

    assert client.calls == 2
    assert [step.kind for step in plan.steps] == ["answer"]


class _ManagedAttachmentClarificationRepairClient:
    """模拟模型把平台自动读附件错拆成clarification后修复为唯一回答。"""

    def __init__(self) -> None:
        """记录两轮调用，确认错误计划在持久化前被拒绝。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """首轮用澄清伪装附件读取，修复轮由answer消费平台读回执。"""

        self.calls += 1
        steps = (
            [
                {
                    "draft_id": "read_attachment",
                    "title": "读取并解析评审材料",
                    "kind": "clarification",
                },
                {
                    "draft_id": "answer",
                    "title": "形成架构评审",
                    "kind": "answer",
                    "depends_on": ["read_attachment"],
                },
            ]
            if self.calls == 1
            else [
                {
                    "draft_id": "answer",
                    "title": "形成架构评审",
                    "kind": "answer",
                }
            ]
        )
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": steps,
        }


def test_managed_attachment_read_cannot_be_planned_as_clarification() -> None:
    """附件读取是Runtime内建Operation，不得变成用户等待或内部分析澄清。"""

    client = _ManagedAttachmentClarificationRepairClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="根据本轮附件形成架构评审",
        success_criteria=(
            SuccessCriterion(id="answer_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
        input_resources=({"resource_id": "resource_md", "filename": "design.md"},),
    )

    assert client.calls == 2
    assert [step.kind for step in plan.steps] == ["answer"]


class _AttachmentToolMisuseRepairClient:
    """首轮把附件读取错配到业务工具，修复轮才返回平台可执行计划。"""

    def __init__(self) -> None:
        """初始化调用计数，供测试确认语义修复确实发生。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """首轮模拟真实模型误选业务工具，第二轮遵从内建读取边界。"""

        self.calls += 1
        if self.calls == 1:
            return {
                "goal": "分析图片",
                "success_criteria": [
                    {"id": "answer_ready", "type": "assertion", "spec": {"required": True}}
                ],
                "steps": [
                    {
                        "draft_id": "wrong_read",
                        "title": "读取图片附件内容",
                        "kind": "tool.read",
                        "capability_refs": ["contract.query"],
                    },
                    {
                        "draft_id": "answer",
                        "title": "形成图片结论",
                        "kind": "answer",
                        "depends_on": ["wrong_read"],
                    },
                ],
            }
        assert "受管附件" in user_payload["repair"]["failure_message"]
        return {
            "goal": "分析图片",
            "success_criteria": [
                {"id": "answer_ready", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成图片结论",
                    "kind": "answer",
                }
            ],
        }


def test_planner_repairs_business_tool_misused_for_managed_attachment_read() -> None:
    """真实模型把附件分析映射为无关工具时，宿主编译器必须移除该旁路而非执行。"""

    client = _AttachmentToolMisuseRepairClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="分析图片",
        success_criteria=(
            SuccessCriterion(id="answer_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(_snapshot(),),
        input_resources=({"resource_id": "resource_image", "filename": "evidence.png"},),
    )

    # 平台拥有 input.read 的权威读取语义，首轮计划即可确定性剥离错误 tool.read，
    # 不再把同一错误反复交给模型修复，避免慢模型和修复提示之间形成循环。
    assert client.calls == 1
    assert [step.kind for step in plan.steps] == ["answer"]


class _LongDisplayTitleClient(_Client):
    """模拟模型把 Skill 方法说明错误塞入仅用于展示的步骤标题。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """返回语义合法但 title 超长的计划，其他字段不得被服务端代修。"""

        raw = super().generate_json(system_prompt, user_payload)
        raw["steps"][0]["title"] = "核对输入并形成可执行规范。" * 40
        return raw


def test_planner_bounds_model_controlled_display_title_without_weakening_plan_contract() -> None:
    """超长展示标题由服务端确定性收紧，能力引用和依赖仍按原严格契约验证。"""

    plan = DynamicTaskPlanner(_LongDisplayTitleClient()).create_plan(
        goal="生成续约风险简报",
        success_criteria=(
            SuccessCriterion(id="brief_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(_snapshot(),),
    )

    assert len(plan.steps[0].title) == 256
    assert plan.steps[0].title.endswith("…")
    assert plan.steps[0].capability_refs == ("contract.query",)


def test_planner_hides_side_effect_capability_without_explicit_user_intent() -> None:
    """写报告不授权创建 Skill；只有明确要求沉淀 Skill 时才向规划器披露提案能力。"""

    payload = {
        "capability_type": "tool",
        "capability_id": "platform.general_skill.propose",
        "tenant_id": "tenant_demo",
        "name": "platform.general_skill.propose",
        "contract": {
            "risk_class": "local_write",
            "requires_explicit_goal_intent": "skill_proposal",
        },
        "model_view": {"name": "platform.general_skill.propose", "input_schema": {}},
        "user_view": {},
        "audit_view": {},
    }
    capability = CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})

    ordinary_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(ordinary_client).create_plan(
            goal="写一份售后操作规范",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert ordinary_client.payload is not None
    assert ordinary_client.payload["capabilities"] == []

    selected_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(selected_client).create_plan(
            goal="使用 writing-for-agents Skill 创建一份售后操作规范",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert selected_client.payload is not None
    assert selected_client.payload["capabilities"] == []

    explicit_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(explicit_client).create_plan(
            goal="把售后方法沉淀为一个 Skill",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert explicit_client.payload is not None
    assert explicit_client.payload["capabilities"][0]["name"] == capability.name

    direct_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(direct_client).create_plan(
            goal="S5创建Skill：总结退款证据复核方法并提交我确认",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert direct_client.payload is not None
    assert direct_client.payload["capabilities"][0]["name"] == capability.name

    negated_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(negated_client).create_plan(
            goal="不要创建或安装任何 Skill，只输出操作规范",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert negated_client.payload is not None
    assert negated_client.payload["capabilities"] == []

    private_only_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(private_only_client).create_plan(
            goal="不要发布到组织广场，但请把方法沉淀为一个私有 Skill",
            success_criteria=(criterion,),
            capabilities=(capability,),
        )
    assert private_only_client.payload is not None
    assert private_only_client.payload["capabilities"][0]["name"] == capability.name


class _RepairingCapabilityClient:
    """首轮虚构工具，收到服务端修复契约后收敛为纯回答计划。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.payloads: list[dict] = []

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """首轮返回越权能力引用，第二轮遵守有限修复提示。"""

        self.payloads.append(user_payload)
        if len(self.payloads) == 1:
            return {
                "goal": user_payload["goal"],
                "success_criteria": user_payload["success_criteria"],
                "steps": [
                    {
                        "draft_id": "lookup",
                        "title": "查询未冻结系统",
                        "kind": "tool.read",
                        "capability_refs": ["invented.query"],
                    },
                    {
                        "draft_id": "answer",
                        "title": "形成结果",
                        "kind": "answer",
                        "depends_on": ["lookup"],
                    },
                ],
            }
        assert user_payload["repair"]["failure_code"] == "DYNAMIC_PLAN_SEMANTIC_INVALID"
        assert "guidance_requirements" in user_payload["repair"]["instruction"]
        assert "guidance_principle_candidates" in user_payload["repair"]["instruction"]
        assert "loaded_guidance 为空" in user_payload["repair"]["instruction"]
        assert "guidance_skill_refs 必须全部为空" in user_payload["repair"]["instruction"]
        assert "不要猜测或修补该 ID" in user_payload["repair"]["instruction"]
        assert "principle_candidate_id 设为 null" in user_payload["repair"]["instruction"]
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成结果",
                    "kind": "answer",
                }
            ],
        }


def test_planner_repairs_one_semantically_invalid_capability_plan() -> None:
    """规划器只允许一次受限语义修复，成功后仍由服务端完整校验。"""

    client = _RepairingCapabilityClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="生成操作规范",
        success_criteria=(
            SuccessCriterion(id="spec_ready", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
    )

    assert len(client.payloads) == 2
    assert [step.kind for step in plan.steps] == ["answer"]


class _NoGuidanceRepairClient:
    """首轮伪造不存在的 Skill，第二轮按宿主 repair 清空指导字段。"""

    def __init__(self) -> None:
        """记录两次规划请求，验证无 Skill 基线的有界恢复。"""

        self.payloads: list[dict] = []

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """先返回伪造指导引用，再返回无指导的合法最终 answer。"""

        self.payloads.append(user_payload)
        if len(self.payloads) == 1:
            return {
                "goal": user_payload["goal"],
                "success_criteria": user_payload["success_criteria"],
                "guidance_requirements": [],
                "steps": [
                    {
                        "draft_id": "answer",
                        "title": "形成结论",
                        "kind": "answer",
                        "guidance_skill_refs": ["invented-skill"],
                    }
                ],
            }
        assert user_payload["repair"]["failure_code"] == "DYNAMIC_PLAN_SEMANTIC_INVALID"
        assert "loaded_guidance 为空" in user_payload["repair"]["instruction"]
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "guidance_requirements": [],
            "steps": [{"draft_id": "answer", "title": "形成结论", "kind": "answer"}],
        }


def test_planner_repairs_hallucinated_skill_reference_when_no_skill_is_loaded() -> None:
    """无 Skill 的复杂问题不能因模型自造 guidance 引用而让整轮 Dynamic 失败。"""

    client = _NoGuidanceRepairClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="形成采购决策",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
    )

    assert len(client.payloads) == 2
    assert plan.guidance_requirements == ()
    assert plan.steps[0].guidance_skill_use_ids == ()


class _PersistentNoGuidanceHallucinationClient:
    """两次都伪造指导引用，验证末次有界收敛不会污染无Skill计划。"""

    def __init__(self) -> None:
        """记录 repair 次数，确保宿主不会无限重试。"""

        self.payloads: list[dict] = []

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """始终返回目录外 Skill，模拟真实模型忽略 repair 指令的情况。"""

        self.payloads.append(user_payload)
        if "requirements" in user_payload:
            return {"identities": []}
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "guidance_requirements": [
                {
                    "skill_ref": "invented-skill",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle": "不要执行未知指导",
                    "task_mapping": "伪造的指导映射",
                    "observable_acceptance": "伪造的指导验收",
                    "disposition": "apply",
                }
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成无Skill结论",
                    "kind": "answer",
                    "guidance_skill_refs": ["invented-skill"],
                }
            ],
        }


def test_planner_last_repair_strips_persistent_unloaded_skill_without_relaxing_capabilities() -> None:
    """模型持续伪造Skill时只清除指导字段，仍限制步骤和能力契约。"""

    client = _PersistentNoGuidanceHallucinationClient()
    plan = DynamicTaskPlanner(client).create_plan(
        goal="形成无外部能力的事故结论",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(),
    )

    assert len([payload for payload in client.payloads if "goal" in payload]) == 2
    assert plan.guidance_requirements == ()
    assert plan.steps[0].guidance_skill_use_ids == ()
    assert plan.steps[0].capability_refs == ()


def test_last_guidance_repair_selects_one_authoritative_phase_gate() -> None:
    """多个同义 Phase 1 gate 时，末次 repair 只注入一个来源内候选。"""

    raw = {
        "guidance_requirements": [
            {
                "skill_ref": "diagnosing-bugs",
                "source_kind": "instructions",
                "source_ref": "instructions",
                "principle_candidate_id": "guidcand_side",
                "principle": None,
                "task_mapping": "列出尝试",
                "observable_acceptance": "正文可见",
                "disposition": "apply",
            },
            {
                "skill_ref": "diagnosing-bugs",
                "source_kind": "instructions",
                "source_ref": "instructions",
                "principle_candidate_id": "guidcand_phase3",
                "principle": None,
                "task_mapping": "形成假设",
                "observable_acceptance": "列出假设",
                "disposition": "apply",
            }
        ],
        "steps": [{"draft_id": "answer", "kind": "answer"}],
    }
    catalog = [
        {
            "skill_ref": "diagnosing-bugs",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [
                        {
                            "section_path": "Phase 1",
                            "candidates": [
                                {
                                    "principle_candidate_id": "guidcand_gate_a",
                                    "source_order": 2,
                                    "principle": "Do not proceed to hypothesise without a loop.",
                                },
                                {
                                    "principle_candidate_id": "guidcand_gate_b",
                                    "source_order": 3,
                                    "principle": "No red-capable command, no Phase 2.",
                                },
                            ],
                        },
                        {
                            "section_path": "Phase 3 — Hypothesise",
                            "candidates": [
                                {
                                    "principle_candidate_id": "guidcand_phase3",
                                    "source_order": 8,
                                    "principle": "Generate ranked hypotheses.",
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ]

    repaired = _force_guidance_phase_gate(raw, candidate_catalog=catalog)
    requirement = repaired["guidance_requirements"][0]
    assert requirement["principle_candidate_id"] == "guidcand_gate_a"
    assert requirement.get("principle") is None
    assert "停止进入假设阶段" in requirement["task_mapping"]
    assert len(repaired["guidance_requirements"]) == 1

    unbound = _force_guidance_phase_gate(
        {
            "guidance_requirements": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "task_mapping": "",
                    "observable_acceptance": "",
                    "disposition": "apply",
                }
            ],
            "steps": [{"draft_id": "answer", "kind": "answer"}],
        },
        candidate_catalog=catalog,
    )
    assert unbound["guidance_requirements"][0]["skill_ref"] == "diagnosing-bugs"
    assert unbound["guidance_requirements"][0]["principle_candidate_id"] == "guidcand_gate_a"

    synthesized = _force_guidance_phase_gate(
        {"steps": [{"draft_id": "answer", "kind": "answer"}]},
        candidate_catalog=catalog,
    )
    assert synthesized["guidance_requirements"][0]["skill_ref"] == "diagnosing-bugs"
    assert synthesized["guidance_requirements"][0]["principle_candidate_id"] == "guidcand_gate_a"


class _NonConvergentClient:
    """返回缺少结果汇聚步骤的模型计划。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """构造只有能力调用、没有最终 answer 的违约草案。"""

        return {
            "goal": "查询合同",
            "success_criteria": [
                {"id": "query_done", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "contracts",
                    "title": "查询合同",
                    "kind": "tool.read",
                    "capability_refs": ["contract.query"],
                }
            ],
        }


def test_planner_rejects_plan_without_terminal_answer() -> None:
    """模型省略结果步骤时必须在持久化前拒绝，不能生成无唤醒 running 孤儿。"""

    criterion = SuccessCriterion(
        id="query_done",
        type="assertion",
        spec={"required": True},
    )
    with pytest.raises(ValueError, match="required answer"):
        DynamicTaskPlanner(_NonConvergentClient()).create_plan(
            goal="查询合同",
            success_criteria=(criterion,),
            capabilities=(_snapshot(),),
        )


def test_normalization_carries_loaded_guidance_into_final_answer() -> None:
    """即使模型只在前置读取步骤引用 Skill，最终交付步骤也必须继续消费全部固定指导。"""

    draft = DynamicPlanDraft.model_validate(
        {
            "goal": "查询并总结合同",
            "success_criteria": [
                {"id": "done", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "read",
                    "title": "查询合同",
                    "kind": "tool.read",
                    "capability_refs": ["contract.query"],
                    "guidance_skill_refs": ["contract-review"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成结论",
                    "kind": "answer",
                    "depends_on": ["read"],
                },
            ],
        }
    )
    plan = normalize_plan_draft(
        draft,
        max_steps=10,
        max_tool_calls=5,
        max_model_calls=10,
        guidance_use_ids_by_name={"contract-review": ("gsuse_contract",)},
    )

    assert [step.guidance_skill_use_ids for step in plan.steps] == [
        ("gsuse_contract",),
        ("gsuse_contract",),
    ]


def test_normalization_makes_required_answer_the_terminal_convergence_point() -> None:
    """模型把澄清反向依赖 answer 时，编译器应固定为澄清完成后再形成唯一结果。"""

    draft = DynamicPlanDraft.model_validate(
        {
            "goal": "在缺少诊断证据时给出受限结论",
            "success_criteria": [
                {"id": "bounded", "type": "assertion", "spec": {"required": True}}
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成受限结论",
                    "kind": "answer",
                },
                {
                    "draft_id": "clarify",
                    "title": "请求脱敏诊断证据",
                    "kind": "clarification",
                    "depends_on": ["answer"],
                },
            ],
        }
    )

    plan = normalize_plan_draft(
        draft,
        max_steps=4,
        max_tool_calls=0,
        max_model_calls=4,
    )

    answer = next(step for step in plan.steps if step.kind == "answer")
    clarification = next(step for step in plan.steps if step.kind == "clarification")
    assert clarification.depends_on == ()
    assert answer.depends_on == (clarification.step_key,)


def test_clarification_rejects_internal_structure_or_data_source_wait() -> None:
    """已给齐事实时，模型不得等待用户确认报告结构或内部数据来源。"""

    plan = NormalizedPlan(
        goal="生成验收报告",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="clarify",
                title="确认发布验收报告的结构与数据来源",
                kind="clarification",
            ),
            PlanStep(
                step_key="answer",
                title="生成报告",
                kind="answer",
                depends_on=("clarify",),
            ),
        ),
    )

    with pytest.raises(ValueError, match="内部工作"):
        _validate_clarification_semantics(plan)


def test_clarification_rejects_generic_delivery_requirements_when_goal_is_complete() -> None:
    """目标已给齐事实与输出要求时，不能把确认交付要求伪装成用户输入缺口。"""

    plan = NormalizedPlan(
        goal="完成采购决策备忘录并按给定硬约束比较方案",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="clarify",
                title="确认采购决策备忘录的交付要求",
                kind="clarification",
            ),
            PlanStep(
                step_key="answer",
                title="生成采购决策备忘录",
                kind="answer",
                depends_on=("clarify",),
            ),
        ),
    )

    with pytest.raises(ValueError, match="内部工作"):
        _validate_clarification_semantics(plan)


def _guidance_sources() -> dict[str, tuple[dict[str, str], ...]]:
    """返回 instructions 与固定 reviewed resource 两类权威规划来源。"""

    return {
        "codebase-design": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "a" * 64,
                "content": "Keep policy separate from mechanism. Prefer explicit seams.",
            },
            {
                "source_kind": "reviewed_resource",
                "source_ref": "DESIGN-IT-TWICE.md",
                "source_checksum": "b" * 64,
                "content": "Design it twice before committing to a boundary.",
            },
        )
    }


def _guided_draft(*, disposition: str = "apply", principle: str | None = None) -> DynamicPlanDraft:
    """构造包含一条模型 GuidanceRequirement 声明的最小草案。"""

    return DynamicPlanDraft.model_validate(
        {
            "goal": "评审模块边界",
            "success_criteria": [
                {"id": "done", "type": "assertion", "spec": {"required": True}}
            ],
            "guidance_requirements": [
                {
                    "skill_ref": "codebase-design",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle": principle or "Keep policy separate from mechanism.",
                    "task_mapping": "把支付策略移出 API 层",
                    "observable_acceptance": "提案明确展示策略端口和适配器边界",
                    "disposition": disposition,
                }
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成架构提案",
                    "kind": "answer",
                    "guidance_skill_refs": ["codebase-design"],
                }
            ],
        }
    )


def test_guidance_requirements_are_source_backed_and_receive_stable_server_ids() -> None:
    """模型只能引用固定来源原文，requirement_id 由服务端语义稳定派生。"""

    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"codebase-design": ("gsuse_design",)},
        "guidance_sources_by_name": _guidance_sources(),
        "guidance_selection_modes_by_name": {"codebase-design": "auto"},
    }
    first = normalize_plan_draft(_guided_draft(), **kwargs)
    second = normalize_plan_draft(_guided_draft(), **kwargs)

    assert first.guidance_requirements == second.guidance_requirements
    requirement = first.guidance_requirements[0]
    assert requirement.requirement_id.startswith("guidreq_")
    assert requirement.skill_use_id == "gsuse_design"
    assert requirement.source_kind == "instructions"
    assert requirement.source_ref == "instructions"
    assert requirement.principle == "Keep policy separate from mechanism."

    with pytest.raises(ValueError, match="权威来源"):
        normalize_plan_draft(_guided_draft(principle="Invented architecture rule."), **kwargs)

    invalid_sources = _guidance_sources()
    invalid_sources["codebase-design"][1]["source_checksum"] = "not-a-checksum"
    with pytest.raises(ValueError, match="checksum"):
        normalize_plan_draft(
            _guided_draft(),
            **{**kwargs, "guidance_sources_by_name": invalid_sources},
        )


def test_identical_guidance_requirement_declarations_are_idempotently_deduplicated() -> None:
    """模型重复返回完全相同的原则时只保留一条，改动字段的声明仍不被吞掉。"""

    raw = _guided_draft().model_dump(mode="json")
    raw["guidance_requirements"].append(dict(raw["guidance_requirements"][0]))
    draft = DynamicPlanDraft.model_validate(raw)
    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"codebase-design": ("gsuse_design",)},
        "guidance_sources_by_name": _guidance_sources(),
        "guidance_selection_modes_by_name": {"codebase-design": "auto"},
    }
    plan = normalize_plan_draft(draft, **kwargs)

    assert len(plan.guidance_requirements) == 1
    changed = draft.model_dump(mode="json")
    changed["guidance_requirements"][1]["task_mapping"] += "，并列出回退边界"
    changed_plan = normalize_plan_draft(DynamicPlanDraft.model_validate(changed), **kwargs)
    assert len(changed_plan.guidance_requirements) == 2


def test_guidance_requirements_cap_three_to_keep_planner_output_bounded() -> None:
    """每个 Skill 最多冻结三条核心要求，避免长原则目录挤爆规划 JSON。"""

    raw = _guided_draft().model_dump(mode="json")
    raw["guidance_requirements"] = [
        {
            **raw["guidance_requirements"][0],
            "task_mapping": f"把支付策略移出 API 层，要求 {index}",
        }
        for index in range(4)
    ]
    draft = DynamicPlanDraft.model_validate(raw)
    with pytest.raises(ValueError, match=r"1\.\.3"):
        normalize_plan_draft(
            draft,
            max_steps=3,
            max_tool_calls=0,
            max_model_calls=3,
            guidance_use_ids_by_name={"codebase-design": ("gsuse_design",)},
            guidance_sources_by_name=_guidance_sources(),
            guidance_selection_modes_by_name={"codebase-design": "auto"},
        )


def test_guidance_candidate_id_is_authoritative_over_model_source_metadata() -> None:
    """服务端候选ID决定唯一来源，模型误抄 source_ref 时由宿主规范化而非换源。"""

    sources = _guidance_sources()
    candidate = next(
        item
        for item in guidance_principle_candidates(sources["codebase-design"])
        if item["principle"] == "Keep policy separate from mechanism. Prefer explicit seams."
    )
    raw = _guided_draft().model_dump(mode="json")
    raw_requirement = raw["guidance_requirements"][0]
    raw_requirement["principle"] = None
    raw_requirement["principle_candidate_id"] = candidate["principle_candidate_id"]
    draft = DynamicPlanDraft.model_validate(raw)
    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"codebase-design": ("gsuse_design",)},
        "guidance_sources_by_name": sources,
        "guidance_selection_modes_by_name": {"codebase-design": "auto"},
    }

    plan = normalize_plan_draft(draft, **kwargs)
    assert plan.guidance_requirements[0].principle == candidate["principle"]

    relabelled = draft.model_dump(mode="json")
    relabelled["guidance_requirements"][0]["source_kind"] = "reviewed_resource"
    relabelled["guidance_requirements"][0]["source_ref"] = "DESIGN-IT-TWICE.md"
    canonical = normalize_plan_draft(DynamicPlanDraft.model_validate(relabelled), **kwargs)
    assert canonical.guidance_requirements[0].source_kind == "instructions"
    assert canonical.guidance_requirements[0].source_ref == "instructions"


def test_invalid_guidance_candidate_id_uses_bounded_identity_repair() -> None:
    """模型幻造候选ID时只允许一次候选索引修复，不把未知ID放入正式计划。"""

    candidate_id = "guidcand_" + "a" * 64
    raw = {
        "guidance_requirements": [
            {
                "skill_ref": "codebase-design",
                "source_kind": "instructions",
                "source_ref": "instructions",
                "principle_candidate_id": "guidcand_" + "f" * 64,
                "principle": None,
                "task_mapping": "形成边界建议",
                "observable_acceptance": "正文包含边界与测试",
                "disposition": "apply",
            }
        ]
    }
    candidate_catalog = [
        {
            "skill_ref": "codebase-design",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [
                        {
                            "candidates": [
                                {
                                    "principle_candidate_id": candidate_id,
                                    "principle": "Keep policy separate from mechanism.",
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    ]

    class _IdentityClient:
        """返回唯一候选索引，模拟只读身份修复模型。"""

        def generate_json(self, _system_prompt: str, _payload: dict) -> dict:
            """只返回候选数组下标，不接收或执行任何工具请求。"""

            return {"identities": [{"index": 0, "candidate_index": 0}]}

    repaired = _repair_guidance_identity_fields(
        _IdentityClient(),
        raw,
        candidate_catalog=candidate_catalog,
    )
    requirement = repaired["guidance_requirements"][0]
    assert requirement["principle_candidate_id"] == candidate_id


def test_guidance_candidate_id_hydrates_missing_source_identity_before_schema_validation() -> None:
    """候选 ID 已可信但模型漏填来源字段时，由宿主补齐而不是提前放弃计划。"""

    candidate_id = "guidcand_" + "a" * 64
    candidate_catalog = [
        {
            "skill_ref": "diagnosing-bugs",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [
                        {
                            "section_path": "Phase 1",
                            "candidates": [
                                {
                                    "principle_candidate_id": candidate_id,
                                    "principle_candidate_id_short": "a" * 16,
                                    "source_order": 1,
                                    "principle": "先建立可复现反馈回路。",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]

    class _NoRepairClient:
        """候选身份已唯一时不应触发第二次模型修复。"""

        def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
            """若调用则说明宿主没有使用权威候选完成补齐。"""

            raise AssertionError("唯一候选身份不应触发模型 repair")

    repaired = _repair_guidance_identity_fields(
        _NoRepairClient(),
        {
            "guidance_requirements": [
                {
                    "principle_candidate_id": candidate_id,
                    "task_mapping": "用于诊断任务",
                    "observable_acceptance": "正文说明反馈回路",
                    "disposition": "apply",
                }
            ]
        },
        candidate_catalog=candidate_catalog,
    )

    requirement = repaired["guidance_requirements"][0]
    assert requirement["skill_ref"] == "diagnosing-bugs"
    assert requirement["source_kind"] == "instructions"
    assert requirement["source_ref"] == "instructions"
    assert requirement.get("principle") is None


def test_guidance_duplicate_principle_does_not_guess_source_identity() -> None:
    """相同原则出现在多个来源时不得按候选顺序猜测 Skill 身份。"""

    candidate_catalog = [
        {
            "skill_ref": "skill-a",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [{"candidates": [{
                        "principle_candidate_id": "guidcand_" + "a" * 64,
                        "principle": "保持边界清晰。",
                    }]}],
                },
            ],
        },
        {
            "skill_ref": "skill-b",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [{"candidates": [{
                        "principle_candidate_id": "guidcand_" + "b" * 64,
                        "principle": "保持边界清晰。",
                    }]}],
                },
            ],
        },
    ]

    class _RepairClient:
        """记录重复原则触发受限修复，而不是接收宿主猜测。"""

        called = False

        def generate_json(self, _system_prompt: str, _payload: dict) -> dict:
            """不选择任一候选，模拟身份无法安全消歧。"""

            self.called = True
            return {"identities": []}

    client = _RepairClient()
    raw = {
        "guidance_requirements": [{
            "principle": "保持边界清晰。",
            "task_mapping": "映射边界职责",
            "observable_acceptance": "报告说明边界",
            "disposition": "apply",
        }],
    }
    repaired = _repair_guidance_identity_fields(client, raw, candidate_catalog=candidate_catalog)

    assert client.called is True
    assert repaired["guidance_requirements"][0].get("skill_ref") in (None, "")
    assert repaired["guidance_requirements"][0].get("source_ref") in (None, "")


def test_guidance_candidate_id_may_repeat_matching_principle_but_not_rewrite_it() -> None:
    """候选ID附带原文时只接受与权威原则完全一致的冗余回显。"""

    sources = _guidance_sources()
    candidate = next(
        item
        for item in guidance_principle_candidates(sources["codebase-design"])
        if item["principle"] == "Keep policy separate from mechanism. Prefer explicit seams."
    )
    raw = _guided_draft().model_dump(mode="json")
    raw_requirement = raw["guidance_requirements"][0]
    raw_requirement["principle_candidate_id"] = candidate["principle_candidate_id"]
    raw_requirement["principle"] = candidate["principle"]
    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"codebase-design": ("gsuse_design",)},
        "guidance_sources_by_name": sources,
        "guidance_selection_modes_by_name": {"codebase-design": "auto"},
    }
    plan = normalize_plan_draft(DynamicPlanDraft.model_validate(raw), **kwargs)
    assert plan.guidance_requirements[0].principle == candidate["principle"]

    raw["guidance_requirements"][0]["principle_candidate_id"] = "guidcand_" + "f" * 64
    fallback_plan = normalize_plan_draft(DynamicPlanDraft.model_validate(raw), **kwargs)
    assert fallback_plan.guidance_requirements[0].principle == candidate["principle"]

    raw["guidance_requirements"][0]["principle_candidate_id"] = candidate["principle_candidate_id"]
    raw["guidance_requirements"][0]["principle"] = "未经授权的改写"
    with pytest.raises(ValueError, match="候选ID与原则原文不一致"):
        normalize_plan_draft(DynamicPlanDraft.model_validate(raw), **kwargs)


def test_guidance_full_candidate_id_is_authoritative_over_redundant_short_id() -> None:
    """完整ID是唯一权威身份，冗余短ID不会覆盖或改变它。"""

    raw = _guided_draft().model_dump(mode="json")
    raw_requirement = raw["guidance_requirements"][0]
    raw_requirement["principle_candidate_id"] = "guidcand_" + "a" * 64
    raw_requirement["principle_candidate_id_short"] = "a" * 16
    draft = DynamicPlanDraft.model_validate(raw)
    assert draft.guidance_requirements[0].principle_candidate_id_short == "a" * 16

    raw_requirement["principle_candidate_id_short"] = "b" * 16
    assert DynamicPlanDraft.model_validate(raw).guidance_requirements[0].principle_candidate_id == (
        "guidcand_" + "a" * 64
    )


def test_truncated_guidance_candidate_id_resolves_only_unique_authoritative_prefix() -> None:
    """长目录中的截断候选 ID 仅在同一权威来源唯一命中时恢复。"""

    sources = _guidance_sources()
    candidate = next(
        item for item in guidance_principle_candidates(sources["codebase-design"])
        if item["principle"] == "Keep policy separate from mechanism. Prefer explicit seams."
    )
    raw = _guided_draft().model_dump(mode="json")
    raw_requirement = raw["guidance_requirements"][0]
    raw_requirement["principle"] = None
    raw_requirement["principle_candidate_id"] = candidate["principle_candidate_id"][:-2]
    draft = DynamicPlanDraft.model_validate(raw)
    plan = normalize_plan_draft(
        draft,
        max_steps=3,
        max_tool_calls=0,
        max_model_calls=3,
        guidance_use_ids_by_name={"codebase-design": ("gsuse_design",)},
        guidance_sources_by_name=sources,
        guidance_selection_modes_by_name={"codebase-design": "auto"},
    )
    assert plan.guidance_requirements[0].principle == candidate["principle"]

    raw_requirement["principle_candidate_id"] = candidate["principle_candidate_id"].removeprefix(
        "guidcand_"
    )
    no_prefix_plan = normalize_plan_draft(
        DynamicPlanDraft.model_validate(raw),
        max_steps=3,
        max_tool_calls=0,
        max_model_calls=3,
        guidance_use_ids_by_name={"codebase-design": ("gsuse_design",)},
        guidance_sources_by_name=sources,
        guidance_selection_modes_by_name={"codebase-design": "auto"},
    )
    assert no_prefix_plan.guidance_requirements[0].principle == candidate["principle"]


def test_short_guidance_candidate_id_field_maps_to_authoritative_candidate() -> None:
    """模型输出契约允许提示中的短候选ID，并仍只映射到唯一权威原则。"""

    sources = _guidance_sources()
    candidate = next(
        item
        for item in guidance_principle_candidates(sources["codebase-design"])
        if item["principle"] == "Keep policy separate from mechanism. Prefer explicit seams."
    )
    raw = _guided_draft().model_dump(mode="json")
    raw_requirement = raw["guidance_requirements"][0]
    raw_requirement["principle"] = None
    raw_requirement["principle_candidate_id"] = None
    raw_requirement["principle_candidate_id_short"] = candidate["principle_candidate_id"].removeprefix(
        "guidcand_"
    )[:16]
    draft = DynamicPlanDraft.model_validate(raw)
    plan = normalize_plan_draft(
        draft,
        max_steps=3,
        max_tool_calls=0,
        max_model_calls=3,
        guidance_use_ids_by_name={"codebase-design": ("gsuse_design",)},
        guidance_sources_by_name=sources,
        guidance_selection_modes_by_name={"codebase-design": "auto"},
    )
    assert plan.guidance_requirements[0].principle == candidate["principle"]


def test_guidance_candidates_skip_headings_and_split_long_source_into_exact_sentences() -> None:
    """候选排除标题、切分长段，同时保留方法章节和原文顺序。"""

    first = "Hunt no-ops sentence by sentence: delete instructions that do not change behavior."
    second = "Keep one authoritative source so changing behavior remains a one-place edit."
    content = f"# Editing guidance\n## Phase 1 — Gate\n- {first} {second} " + (
        "extra context " * 20
    )
    sources = (
        {
            "source_kind": "instructions",
            "source_ref": "instructions",
            "source_checksum": "d" * 64,
            "content": content,
        },
    )

    candidates = guidance_principle_candidates(sources)
    principles = {item["principle"] for item in candidates}

    assert "Editing guidance" not in principles
    assert first in principles
    assert second in principles
    selected = next(item for item in candidates if item["principle"] == first)
    assert selected["section_path"] == "Editing guidance > Phase 1 — Gate"
    assert selected["source_order"] == 1


def test_planner_candidate_catalog_skips_generic_skill_introduction() -> None:
    """候选目录优先呈现操作性规则，Skill 过短时仍保留全部来源。"""

    sources = {
        "writing-for-agents": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "e" * 64,
                "content": (
                    "Reference for any agent document.\n"
                    "The packaging differs; the writing does not.\n"
                    "A context pointer names material and the branch that loads it.\n"
                ),
            },
        )
    }
    catalog = _planner_guidance_candidate_catalog(sources)
    principles = [
        candidate["principle"]
        for source in catalog[0]["sources"]
        for section in source["sections"]
        for candidate in section["candidates"]
    ]
    assert principles == ["A context pointer names material and the branch that loads it."]


def test_planner_candidate_catalog_skips_skill_frontmatter_metadata() -> None:
    """Planner候选目录不得把Skill frontmatter的身份字段当成可应用原则。"""

    sources = {
        "writing-for-agents": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "f" * 64,
                "content": (
                    "---\n"
                    "name: writing-for-agents\n"
                    "description: Writing documents for agents.\n"
                    "---\n"
                    "# Method\n"
                    "Every step ends on a checkable completion criterion.\n"
                ),
            },
        )
    }

    catalog = _planner_guidance_candidate_catalog(sources)
    principles = [
        candidate["principle"]
        for source in catalog[0]["sources"]
        for section in source["sections"]
        for candidate in section["candidates"]
    ]

    assert principles == ["Every step ends on a checkable completion criterion."]


def test_guidance_phase_candidates_require_contiguous_prerequisite_closure() -> None:
    """分阶段 Skill 不能只冻结后续方法，必须同时冻结连续前置阶段。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "e" * 64,
                "content": (
                    "# Diagnosing Bugs\n"
                    "## Phase 1 — Build a feedback loop\n"
                    "Name a red-capable signal before diagnosing.\n"
                    "## Phase 2 — Reproduce and minimise\n"
                    "Minimise the reproduction one variable at a time.\n"
                    "## Phase 3 — Hypothesise\n"
                    "Generate three ranked falsifiable hypotheses.\n"
                ),
            },
        )
    }
    candidates = guidance_principle_candidates(sources["diagnosing-bugs"])
    by_phase = {
        int(re.search(r"Phase (\d+)", str(item["section_path"])).group(1)): item
        for item in candidates
    }

    def draft_for(phases: tuple[int, ...]) -> DynamicPlanDraft:
        """按指定阶段构造使用服务端候选ID的诊断计划草案。"""

        return DynamicPlanDraft.model_validate(
            {
                "goal": "规划事故诊断",
                "success_criteria": [
                    {"id": "done", "type": "assertion", "spec": {"required": True}}
                ],
                "guidance_requirements": [
                    {
                        "skill_ref": "diagnosing-bugs",
                        "source_kind": "instructions",
                        "source_ref": "instructions",
                        "principle_candidate_id": by_phase[phase]["principle_candidate_id"],
                        "task_mapping": f"按顺序应用诊断阶段 {phase}",
                        "observable_acceptance": f"交付物展示阶段 {phase} 的结果或门禁",
                        "disposition": "apply",
                    }
                    for phase in phases
                ],
                "steps": [
                    {
                        "draft_id": "answer",
                        "title": "形成诊断计划",
                        "kind": "answer",
                        "guidance_skill_refs": ["diagnosing-bugs"],
                    }
                ],
            }
        )

    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"diagnosing-bugs": ("gsuse_diagnose",)},
        "guidance_sources_by_name": sources,
        "guidance_selection_modes_by_name": {"diagnosing-bugs": "forced"},
    }
    with pytest.raises(ValueError, match="缺少连续前置阶段: 1, 2"):
        normalize_plan_draft(draft_for((3,)), **kwargs)
    with pytest.raises(ValueError, match="缺少连续前置阶段: 2"):
        normalize_plan_draft(draft_for((1, 3)), **kwargs)

    plan = normalize_plan_draft(draft_for((1, 2, 3)), **kwargs)
    assert len(plan.guidance_requirements) == 3
    reversed_plan = normalize_plan_draft(draft_for((3, 2, 1)), **kwargs)
    assert [item.principle for item in reversed_plan.guidance_requirements] == [
        by_phase[phase]["principle"] for phase in (1, 2, 3)
    ]


def test_guidance_phase_repair_inserts_missing_authoritative_phase() -> None:
    """模型跳过中间阶段时，末次 repair 只补来源中的缺失阶段而不改写原则。"""

    catalog = [
        {
            "skill_ref": "diagnosing-bugs",
            "sources": [
                {
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "sections": [
                        {
                            "section_path": "Phase 1",
                            "candidates": [
                                {
                                    "principle_candidate_id": "guidcand_phase1",
                                    "source_order": 1,
                                    "principle": "Build the feedback loop.",
                                }
                            ],
                        },
                        {
                            "section_path": "Phase 2",
                            "candidates": [
                                {
                                    "principle_candidate_id": "guidcand_phase2",
                                    "source_order": 2,
                                    "principle": "Minimise the reproduction.",
                                }
                            ],
                        },
                        {
                            "section_path": "Phase 3",
                            "candidates": [
                                {
                                    "principle_candidate_id": "guidcand_phase3",
                                    "source_order": 3,
                                    "principle": "Generate falsifiable hypotheses.",
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    ]
    repaired = _repair_guidance_phase_continuity(
        {
            "guidance_requirements": [
                {
                    "skill_ref": "diagnosing-bugs",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": "guidcand_phase1",
                    "disposition": "apply",
                },
                {
                    "skill_ref": "diagnosing-bugs",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": "guidcand_phase3",
                    "disposition": "apply",
                },
            ],
            "steps": [
                {"draft_id": "check", "kind": "tool.execute"},
                {"draft_id": "answer", "kind": "answer"},
            ],
        },
        candidate_catalog=catalog,
    )

    assert [
        item["principle_candidate_id"]
        for item in repaired["guidance_requirements"]
    ] == ["guidcand_phase1", "guidcand_phase3", "guidcand_phase2"]
    inserted = repaired["guidance_requirements"][-1]
    assert inserted["principle"] is None
    assert inserted["task_mapping"] == "按固定来源顺序补齐 Phase 2 前置阶段。"


def test_guidance_phase_gate_blocks_later_phase_without_runtime_evidence() -> None:
    """来源明确禁止越过前置门时，初始计划不能靠同时抄录门禁进入后续阶段。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "f" * 64,
                "content": (
                    "# Diagnosing Bugs\n"
                    "## Phase 1 — Build a feedback loop\n"
                    "Do not proceed to hypothesise without a red-capable loop.\n"
                    "## Phase 2 — Reproduce\n"
                    "Run the loop and minimise the reproducer.\n"
                    "## Phase 3 — Hypothesise\n"
                    "Generate three ranked hypotheses.\n"
                ),
            },
        )
    }
    candidates = guidance_principle_candidates(sources["diagnosing-bugs"])
    selected = []
    for phase in (1, 2, 3):
        selected.append(
            next(item for item in candidates if f"Phase {phase}" in item["section_path"])
        )
    draft = DynamicPlanDraft.model_validate(
        {
            "goal": "在没有运行证据时诊断事故",
            "success_criteria": [
                {"id": "done", "type": "assertion", "spec": {"required": True}}
            ],
            "guidance_requirements": [
                {
                    "skill_ref": "diagnosing-bugs",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": item["principle_candidate_id"],
                    "task_mapping": (
                        "缺少 red 证据时停止并请求前置证据"
                        if item is selected[0]
                        else "应用固定诊断阶段"
                    ),
                    "observable_acceptance": (
                        "交付物在前置证据缺失时保持阻塞"
                        if item is selected[0]
                        else "交付物展示该阶段的受控结论"
                    ),
                    "disposition": "apply",
                }
                for item in selected
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成诊断结论",
                    "kind": "answer",
                    "guidance_skill_refs": ["diagnosing-bugs"],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="存在未满足前置门禁: 1"):
        normalize_plan_draft(
            draft,
            max_steps=3,
            max_tool_calls=0,
            max_model_calls=3,
            guidance_use_ids_by_name={"diagnosing-bugs": ("gsuse_diagnose",)},
            guidance_sources_by_name=sources,
            guidance_selection_modes_by_name={"diagnosing-bugs": "forced"},
        )


def test_guidance_phase_gate_must_be_frozen_before_side_principles_without_runtime() -> None:
    """无运行回路时不能只选“停止/列出尝试”旁支而遗漏权威否决门。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "1" * 64,
                "content": (
                    "# Diagnosing Bugs\n"
                    "## Phase 1 — Build a feedback loop\n"
                    "Do not proceed to hypothesise without a red-capable loop.\n"
                    "### When you genuinely cannot build a loop\n"
                    "Stop and say so explicitly. List what you tried.\n"
                ),
            },
        )
    }
    candidates = guidance_principle_candidates(sources["diagnosing-bugs"])
    gate = next(item for item in candidates if "Do not proceed" in item["principle"])
    stop = next(item for item in candidates if item["principle"].startswith("Stop and"))

    def draft_for(candidate: dict[str, object]) -> DynamicPlanDraft:
        """构造只选择一个原则、且没有显式运行步骤的 answer 计划。"""

        return DynamicPlanDraft.model_validate(
            {
                "goal": "在没有运行权限时初步评估故障",
                "success_criteria": [
                    {"id": "done", "type": "assertion", "spec": {"required": True}}
                ],
                "guidance_requirements": [
                    {
                        "skill_ref": "diagnosing-bugs",
                        "source_kind": "instructions",
                        "source_ref": "instructions",
                        "principle_candidate_id": candidate["principle_candidate_id"],
                        "task_mapping": "当前没有反馈回路时停止并说明边界",
                        "observable_acceptance": "正文明确披露反馈回路缺失，不进入假设阶段",
                        "disposition": "apply",
                    }
                ],
                "steps": [
                    {
                        "draft_id": "answer",
                        "title": "形成受限结论",
                        "kind": "answer",
                        "guidance_skill_refs": ["diagnosing-bugs"],
                    }
                ],
            }
        )

    kwargs = {
        "max_steps": 2,
        "max_tool_calls": 0,
        "max_model_calls": 2,
        "guidance_use_ids_by_name": {"diagnosing-bugs": ("gsuse_diagnose",)},
        "guidance_sources_by_name": sources,
        "guidance_selection_modes_by_name": {"diagnosing-bugs": "forced"},
    }
    with pytest.raises(ValueError, match="缺少当前无运行回路的前置否决门"):
        normalize_plan_draft(draft_for(stop), **kwargs)
    accepted = normalize_plan_draft(draft_for(gate), **kwargs)
    assert accepted.guidance_requirements[0].principle == gate["principle"]


def test_guidance_phase_gate_normalizes_mapping_that_continues_with_hypotheses() -> None:
    """模型把停止原则解释成待验证原因时，宿主应收敛成零假设的安全要求。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "9" * 64,
                "content": (
                    "# Diagnosing Bugs\n"
                    "## Phase 1 — Build a feedback loop\n"
                    "Do not proceed to hypothesise without a red-capable loop.\n"
                ),
            },
        )
    }
    candidate = guidance_principle_candidates(sources["diagnosing-bugs"])[0]

    def draft_for(mapping: str, acceptance: str) -> DynamicPlanDraft:
        """以相同固定门禁候选构造一致或矛盾的任务映射。"""

        return DynamicPlanDraft.model_validate(
            {
                "goal": "在没有 red 证据时给出受限诊断",
                "success_criteria": [
                    {"id": "done", "type": "assertion", "spec": {"required": True}}
                ],
                "guidance_requirements": [
                    {
                        "skill_ref": "diagnosing-bugs",
                        "source_kind": "instructions",
                        "source_ref": "instructions",
                        "principle_candidate_id": candidate["principle_candidate_id"],
                        "task_mapping": mapping,
                        "observable_acceptance": acceptance,
                        "disposition": "apply",
                    }
                ],
                "steps": [
                    {
                        "draft_id": "answer",
                        "title": "形成受限结论",
                        "kind": "answer",
                        "guidance_skill_refs": ["diagnosing-bugs"],
                    }
                ],
            }
        )

    kwargs = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"diagnosing-bugs": ("gsuse_diagnose",)},
        "guidance_sources_by_name": sources,
        "guidance_selection_modes_by_name": {"diagnosing-bugs": "forced"},
    }
    normalized = normalize_plan_draft(
        draft_for(
            "不进行无根据假设，但仍给出最可能原因方向并标为待验证假设",
            "所有原因均标注为待验证",
        ),
        **kwargs,
    )
    requirement = normalized.guidance_requirements[0]
    assert "停止进入假设阶段" in requirement.task_mapping
    assert "不得输出原因排序" in requirement.observable_acceptance

    plan = normalize_plan_draft(
        draft_for(
            "缺少反馈回路时停止并请求脱敏 trace 或环境访问权限",
            "交付物明确阻塞且不进入假设阶段",
        ),
        **kwargs,
    )
    assert plan.guidance_requirements[0].task_mapping.startswith("缺少反馈回路")


def test_guidance_file_read_requirement_needs_matching_frozen_read_step() -> None:
    """冻结原则要求读取 CONTEXT.md 时，普通源码读取步骤不能冒充该方法已执行。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "8" * 64,
                "content": "When exploring code, read `CONTEXT.md` before diagnosing.",
            },
        )
    }
    candidate = guidance_principle_candidates(sources["diagnosing-bugs"])[0]

    def plan_for(title: str):
        """以不同读取标题构造同一固定指导要求。"""

        draft = DynamicPlanDraft.model_validate(
            {
                "goal": "诊断记忆路由",
                "success_criteria": [
                    {"id": "done", "type": "assertion", "spec": {"required": True}}
                ],
                "guidance_requirements": [
                    {
                        "skill_ref": "diagnosing-bugs",
                        "source_kind": "instructions",
                        "source_ref": "instructions",
                        "principle_candidate_id": candidate["principle_candidate_id"],
                        "task_mapping": "读取 CONTEXT.md 后再诊断模块",
                        "observable_acceptance": "报告引用已读取的上下文",
                        "disposition": "apply",
                    }
                ],
                "steps": [
                    {
                        "draft_id": "read",
                        "title": title,
                        "kind": "tool.read",
                        "capability_refs": ["workspace.memory.read"],
                        "guidance_skill_refs": ["diagnosing-bugs"],
                    },
                    {
                        "draft_id": "answer",
                        "title": "形成结论",
                        "kind": "answer",
                        "depends_on": ["read"],
                        "guidance_skill_refs": ["diagnosing-bugs"],
                    },
                ],
            }
        )
        return normalize_plan_draft(
            draft,
            max_steps=3,
            max_tool_calls=1,
            max_model_calls=3,
            guidance_use_ids_by_name={"diagnosing-bugs": ("gsuse_diagnose",)},
            guidance_sources_by_name=sources,
            guidance_selection_modes_by_name={"diagnosing-bugs": "forced"},
        )

    with pytest.raises(ValueError, match="缺少对应的冻结 read 步骤"):
        _validate_guidance_step_alignment(plan_for("读取 app/memory_route.py"))
    _validate_guidance_step_alignment(plan_for("读取 CONTEXT.md"))


def test_guidance_pointer_reference_does_not_require_opening_target_document() -> None:
    """待交付 AGENTS 文档中的按需读取指针不应伪造本轮文件读取前置。"""

    sources = {
        "writing-for-agents": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "9" * 64,
                "content": "Branching is the cleanest disclosure test.",
            },
        )
    }
    candidate = guidance_principle_candidates(sources["writing-for-agents"])[0]
    draft = DynamicPlanDraft.model_validate(
        {
            "goal": "改写根目录 AGENTS.md",
            "success_criteria": [
                {"id": "done", "type": "assertion", "spec": {"required": True}}
            ],
            "guidance_requirements": [
                {
                    "skill_ref": "writing-for-agents",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": candidate["principle_candidate_id"],
                    "task_mapping": (
                        "为每个需要按需读取的文档设计上下文指针，"
                        "例如修改 retry 时引用 docs/payment-retries.md"
                    ),
                    "observable_acceptance": "最终文档包含明确触发条件。",
                    "disposition": "apply",
                }
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "生成最终 AGENTS.md",
                    "kind": "answer",
                    "guidance_skill_refs": ["writing-for-agents"],
                }
            ],
        }
    )
    plan = normalize_plan_draft(
        draft,
        max_steps=2,
        max_tool_calls=0,
        max_model_calls=2,
        guidance_use_ids_by_name={"writing-for-agents": ("gsuse_writing",)},
        guidance_sources_by_name=sources,
        guidance_selection_modes_by_name={"writing-for-agents": "forced"},
    )

    _validate_guidance_step_alignment(plan)


def test_nonblocking_guidance_checkpoint_cannot_become_required_clarification() -> None:
    """Skill明确要求AFK时继续，规划器不得强制用户确认假设排序。"""

    sources = {
        "diagnosing-bugs": (
            {
                "source_kind": "instructions",
                "source_ref": "instructions",
                "source_checksum": "a" * 64,
                "content": (
                    "# Diagnosing Bugs\n"
                    "## Phase 3 — Hypothesise\n"
                    "Generate three ranked hypotheses.\n"
                    "Show the ranked list to the user. Don't block on it — proceed if AFK.\n"
                ),
            },
        )
    }
    candidate = next(
        item
        for item in guidance_principle_candidates(sources["diagnosing-bugs"])
        if item["principle"] == "Generate three ranked hypotheses."
    )
    draft = DynamicPlanDraft.model_validate(
        {
            "goal": "根据已有red回执给出诊断",
            "success_criteria": [
                {"id": "done", "type": "assertion", "spec": {"required": True}}
            ],
            "guidance_requirements": [
                {
                    "skill_ref": "diagnosing-bugs",
                    "source_kind": "instructions",
                    "source_ref": "instructions",
                    "principle_candidate_id": candidate["principle_candidate_id"],
                    "task_mapping": "生成排序假设",
                    "observable_acceptance": "正文展示假设及预测",
                    "disposition": "apply",
                }
            ],
            "steps": [
                {
                    "draft_id": "confirm",
                    "title": "请用户确认假设优先级",
                    "kind": "clarification",
                    "guidance_skill_refs": ["diagnosing-bugs"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成诊断",
                    "kind": "answer",
                    "depends_on": ["confirm"],
                    "guidance_skill_refs": ["diagnosing-bugs"],
                },
            ],
        }
    )

    with pytest.raises(ValueError, match="非阻塞的假设检查点"):
        normalize_plan_draft(
            draft,
            max_steps=3,
            max_tool_calls=0,
            max_model_calls=3,
            guidance_use_ids_by_name={"diagnosing-bugs": ("gsuse_diagnose",)},
            guidance_sources_by_name=sources,
            guidance_selection_modes_by_name={"diagnosing-bugs": "forced"},
        )


def test_guidance_not_applicable_is_unique_and_forced_only() -> None:
    """每个自动选择都必须实际应用；forced 可用唯一 not_applicable 解释不适用。"""

    common = {
        "max_steps": 3,
        "max_tool_calls": 0,
        "max_model_calls": 3,
        "guidance_use_ids_by_name": {"codebase-design": ("gsuse_design",)},
        "guidance_sources_by_name": _guidance_sources(),
    }
    draft = _guided_draft(disposition="not_applicable")
    with pytest.raises(ValueError, match="每个自动选择"):
        normalize_plan_draft(
            draft,
            **common,
            guidance_selection_modes_by_name={"codebase-design": "auto"},
        )

    forced = normalize_plan_draft(
        draft,
        **common,
        guidance_selection_modes_by_name={"codebase-design": "forced"},
    )
    assert forced.guidance_requirements[0].disposition == "not_applicable"


def test_guidance_source_contract_preserves_dependency_selection_mode() -> None:
    """多 Skill 组合中的依赖 Skill 应保留 dependency 身份并进入同一规划契约。"""

    sources, modes = _guidance_source_contract(
        [
            {
                "name": "setup-matt-pocock-skills",
                "selection_mode": "forced",
                "skills": [{"instructions": "先确认交付范围。", "reviewed_resources": []}],
            },
            {
                "name": "tdd",
                "selection_mode": "dependency",
                "skills": [{"instructions": "先写测试。", "reviewed_resources": []}],
            },
        ]
    )

    assert set(sources) == {"setup-matt-pocock-skills", "tdd"}
    assert modes == {"setup-matt-pocock-skills": "forced", "tdd": "dependency"}


def test_guidance_repair_does_not_accept_mixed_not_applicable_and_apply() -> None:
    """Guidance 修复不能把同一 Skill 的不适用和适用要求混在一起提前放行。"""

    contract = [{"skill_ref": "diagnosing-bugs", "selection_mode": "forced"}]
    mixed = [
        {"skill_ref": "diagnosing-bugs", "disposition": "not_applicable"},
        {"skill_ref": "diagnosing-bugs", "disposition": "apply"},
    ]

    assert not _repair_covers_loaded_skills(mixed, contract)
    assert _repair_covers_loaded_skills(
        [{"skill_ref": "diagnosing-bugs", "disposition": "not_applicable"}],
        contract,
    )
    assert not _repair_covers_loaded_skills(
        [{"skill_ref": "diagnosing-bugs", "disposition": "not_applicable"}],
        [{"skill_ref": "diagnosing-bugs", "selection_mode": "auto"}],
    )


def test_each_auto_guidance_must_have_an_applicable_requirement() -> None:
    """多个自动 Skill 中任一个声明不适用都应拒绝，避免保留无效 Use。"""

    raw = _guided_draft().model_dump(mode="json")
    raw["guidance_requirements"].append(
        {
            "skill_ref": "writing-for-agents",
            "source_kind": "instructions",
            "source_ref": "instructions",
            "principle": "Write for the agent that will consume the instruction.",
            "task_mapping": "当前任务不需要写作指导",
            "observable_acceptance": "明确记录不适用",
            "disposition": "not_applicable",
        }
    )
    raw["steps"][0]["guidance_skill_refs"].append("writing-for-agents")
    sources = _guidance_sources()
    sources["writing-for-agents"] = (
        {
            "source_kind": "instructions",
            "source_ref": "instructions",
            "source_checksum": "c" * 64,
            "content": "Write for the agent that will consume the instruction.",
        },
    )

    with pytest.raises(ValueError, match="每个自动选择"):
        normalize_plan_draft(
            DynamicPlanDraft.model_validate(raw),
            max_steps=3,
            max_tool_calls=0,
            max_model_calls=3,
            guidance_use_ids_by_name={
                "codebase-design": ("gsuse_design",),
                "writing-for-agents": ("gsuse_writing",),
            },
            guidance_sources_by_name=sources,
            guidance_selection_modes_by_name={
                "codebase-design": "auto",
                "writing-for-agents": "auto",
            },
        )


class _GuidanceRequirementClient:
    """断言 Planner 披露两类固定来源并返回可校验的应用声明。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """从 output contract 返回 instructions 与 reviewed resource 应用。"""

        assert "guidance_principle_candidates" in system_prompt
        assert "覆盖互补的工作面" in system_prompt
        assert "真实变化接缝与适配器" in system_prompt
        guidance_catalog = user_payload["loaded_guidance"][0]
        assert "skills" not in guidance_catalog
        assert {item["source_ref"] for item in guidance_catalog["sources"]} == {
            "instructions",
            "DESIGN-IT-TWICE.md",
            "scripts/review.sh",
        }
        assert all("content" not in item for item in guidance_catalog["sources"])
        sources = user_payload["guidance_principle_candidates"][0]["sources"]
        candidates = [
            candidate
            for source in sources
            for section in source["sections"]
            for candidate in section["candidates"]
        ]
        assert {source["source_ref"] for source in sources} == {
            "instructions",
            "DESIGN-IT-TWICE.md",
        }
        selected = next(
            item
            for item in candidates
            if item["principle"] == "Design it twice before committing to a boundary."
        )
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "guidance_requirements": [
                {
                    "skill_ref": "codebase-design",
                    "source_kind": "reviewed_resource",
                    "source_ref": "DESIGN-IT-TWICE.md",
                    "principle_candidate_id": selected["principle_candidate_id"],
                    "task_mapping": "先比较集中式和端口适配器两种支付边界",
                    "observable_acceptance": "提案列出两种设计的取舍并明确推荐",
                    "disposition": "apply",
                }
            ],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成架构提案",
                    "kind": "answer",
                    "guidance_skill_refs": ["codebase-design"],
                }
            ],
        }


def test_planner_extracts_reviewed_guidance_sources_before_normalization() -> None:
    """Planner 从固定 prompt block 建立来源目录，不接受模型自报 checksum 或 requirement id。"""

    resource_checksum = "c" * 64
    loaded = (
        {
            "name": "codebase-design",
            "skill_use_ids": ["gsuse_design"],
            "selection_mode": "forced",
            "skills": [
                {
                    "instructions": "Keep policy separate from mechanism.",
                    "reviewed_resources": [
                        {
                            "path": "DESIGN-IT-TWICE.md",
                            "content_checksum": resource_checksum,
                            "content": "Design it twice before committing to a boundary.",
                        },
                        {
                            "path": "scripts/review.sh",
                            "media_type": "text/x-shellscript",
                            "content_checksum": "d" * 64,
                            "content": "#!/bin/sh\nrun-untrusted-review-command",
                            "authority": "reviewed_reference_only; never execute as code implicitly",
                        }
                    ],
                }
            ],
        },
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})
    plan = DynamicTaskPlanner(_GuidanceRequirementClient()).create_plan(
        goal="评审支付架构边界",
        success_criteria=(criterion,),
        capabilities=(),
        loaded_guidance=loaded,
    )

    requirement = plan.guidance_requirements[0]
    assert requirement.skill_use_id == "gsuse_design"
    assert requirement.source_ref == "DESIGN-IT-TWICE.md"
    assert requirement.requirement_id.startswith("guidreq_")


class _MissingGuidanceRequirementRepairClient:
    """模拟完整计划两次遗漏Guidance后由受限修复器补回要求。"""

    def __init__(self) -> None:
        """记录规划与Guidance专用修复调用。"""

        self.calls = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """仅在专用修复契约中返回候选绑定，其余两轮故意遗漏要求。"""

        self.calls += 1
        if "Guidance 要求修复器" in system_prompt:
            candidate = user_payload["candidate_options"][0]
            return {
                "guidance_requirements": [
                    {
                        "skill_ref": candidate["skill_ref"],
                        "source_kind": candidate["source_kind"],
                        "source_ref": candidate["source_ref"],
                        "principle_candidate_id": candidate["principle_candidate_id"],
                        "task_mapping": "把候选原则映射到本次架构评审",
                        "observable_acceptance": "交付物明确列出该原则的验收证据",
                        "disposition": "apply",
                    }
                ]
            }
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": [
                {
                    "draft_id": "answer",
                    "title": "形成评审结论",
                    "kind": "answer",
                    "guidance_skill_refs": ["codebase-design"],
                }
            ],
        }


def test_planner_repairs_missing_guidance_requirements_without_changing_plan() -> None:
    """完整计划遗漏Guidance时只修复要求字段，其他步骤仍受原契约校验。"""

    loaded = (
        {
            "name": "codebase-design",
            "skill_use_ids": ["gsuse_design"],
            "selection_mode": "forced",
            "skills": [
                {
                    "instructions": "Use a checkable acceptance criterion.",
                    "reviewed_resources": [],
                }
            ],
        },
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})
    client = _MissingGuidanceRequirementRepairClient()

    plan = DynamicTaskPlanner(client).create_plan(
        goal="评审架构边界",
        success_criteria=(criterion,),
        capabilities=(),
        loaded_guidance=loaded,
    )

    assert client.calls == 3
    assert plan.steps[-1].title == "形成评审结论"
    assert plan.guidance_requirements[0].skill_ref == "codebase-design"
    assert plan.guidance_requirements[0].principle == "Use a checkable acceptance criterion."


class _FlakyMissingGuidanceRequirementRepairClient(_MissingGuidanceRequirementRepairClient):
    """模拟第一次专用修复仍为空、第二次有界重试补齐Guidance。"""

    def __init__(self) -> None:
        """初始化专用修复调用计数。"""

        super().__init__()
        self.requirement_repairs = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """让第一次专用修复返回非法空数组，验证第二次修复不会静默放行。"""

        if "Guidance 要求修复器" in system_prompt:
            self.requirement_repairs += 1
            if self.requirement_repairs == 1:
                self.calls += 1
                return {"guidance_requirements": []}
        return super().generate_json(system_prompt, user_payload)


def test_planner_retries_invalid_guidance_requirement_repair_fail_closed() -> None:
    """专用修复返回空数组时必须再做一次有界修复，不能把缺失Skill要求当成功。"""

    loaded = (
        {
            "name": "codebase-design",
            "skill_use_ids": ["gsuse_design"],
            "selection_mode": "forced",
            "skills": [
                {
                    "instructions": "Use a checkable acceptance criterion.",
                    "reviewed_resources": [],
                }
            ],
        },
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})
    client = _FlakyMissingGuidanceRequirementRepairClient()

    plan = DynamicTaskPlanner(client).create_plan(
        goal="评审架构边界",
        success_criteria=(criterion,),
        capabilities=(),
        loaded_guidance=loaded,
    )

    assert client.calls == 4
    assert client.requirement_repairs == 2
    assert plan.guidance_requirements[0].skill_ref == "codebase-design"


class _AlwaysEmptyGuidanceRequirementRepairClient(_MissingGuidanceRequirementRepairClient):
    """模拟两次专用修复都为空，验证宿主按权威首项做有界兜底。"""

    def __init__(self) -> None:
        """初始化兜底测试计数。"""

        super().__init__()
        self.requirement_repairs = 0

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """让两次专用修复都返回空数组，其余完整计划保持与基类一致。"""

        if "Guidance 要求修复器" in system_prompt:
            self.calls += 1
            self.requirement_repairs += 1
            return {"guidance_requirements": []}
        return super().generate_json(system_prompt, user_payload)


def test_planner_uses_authoritative_guidance_fallback_after_two_empty_repairs() -> None:
    """模型两次不返回要求时，宿主只能从权威候选补一条apply，不能放行空字段。"""

    loaded = (
        {
            "name": "codebase-design",
            "skill_use_ids": ["gsuse_design"],
            "selection_mode": "forced",
            "skills": [
                {
                    "instructions": "Use a checkable acceptance criterion.",
                    "reviewed_resources": [],
                }
            ],
        },
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})
    client = _AlwaysEmptyGuidanceRequirementRepairClient()

    plan = DynamicTaskPlanner(client).create_plan(
        goal="评审架构边界",
        success_criteria=(criterion,),
        capabilities=(),
        loaded_guidance=loaded,
    )

    assert client.calls == 4
    assert client.requirement_repairs == 2
    assert plan.guidance_requirements[0].principle == "Use a checkable acceptance criterion."


def test_planner_restores_guidance_after_phase_repair_postprocessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阶段连续性后处理丢失要求时，normalize 前仍按权威候选恢复契约。"""

    loaded = (
        {
            "name": "codebase-design",
            "skill_use_ids": ["gsuse_design"],
            "selection_mode": "forced",
            "skills": [
                {
                    "instructions": "Use a checkable acceptance criterion.",
                    "reviewed_resources": [],
                }
            ],
        },
    )
    criterion = SuccessCriterion(id="done", type="assertion", spec={"required": True})

    def drop_requirements(raw: dict, *, candidate_catalog: object) -> dict:
        """模拟阶段收敛器意外丢弃字段，验证末次宿主兜底仍生效。"""

        repaired = dict(raw)
        repaired["guidance_requirements"] = []
        return repaired

    monkeypatch.setattr(planner_service, "_repair_guidance_phase_continuity", drop_requirements)
    plan = DynamicTaskPlanner(_MissingGuidanceRequirementRepairClient()).create_plan(
        goal="评审架构边界",
        success_criteria=(criterion,),
        capabilities=(),
        loaded_guidance=loaded,
    )

    assert plan.guidance_requirements[0].principle == "Use a checkable acceptance criterion."


class _ManagedWorkspacePlanClient:
    """返回受管代码写入、隔离检查和最终结果的收敛计划。"""

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """断言模型只能看到发布视图，并使用风险类别对应的步骤语义。"""

        assert "tool.write" in user_payload["limits"]["allowed_step_kinds"]
        assert "tool.execute" in user_payload["limits"]["allowed_step_kinds"]
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": [
                {
                    "draft_id": "patch",
                    "title": "写入补丁",
                    "kind": "tool.write",
                    "capability_refs": ["workspace.apply"],
                },
                {
                    "draft_id": "test",
                    "title": "运行检查",
                    "kind": "tool.execute",
                    "depends_on": ["patch"],
                    "capability_refs": ["workspace.test"],
                },
                {
                    "draft_id": "answer",
                    "title": "交付结果",
                    "kind": "answer",
                    "depends_on": ["test"],
                },
            ],
        }


def test_planner_maps_local_write_and_execute_to_distinct_governed_steps() -> None:
    """本地修改与执行不得伪装成只读或外部写，二者均进入独立计划语义。"""

    capabilities: list[CapabilitySnapshot] = []
    for name, risk in (("workspace.apply", "local_write"), ("workspace.test", "execute")):
        payload = {
            "capability_type": "tool",
            "capability_id": f"tool-{risk}",
            "tenant_id": "tenant_demo",
            "name": name,
            "contract": {"risk_class": risk},
            "model_view": {"name": name, "input_schema": {"type": "object"}},
            "user_view": {"name": name},
            "audit_view": {"managed_workspace": {"workspace_id": "demo"}},
        }
        capabilities.append(
            CapabilitySnapshot(
                **payload,
                agent_id="agent_demo",
                checksum=capability_checksum(payload),
            )
        )
    ordinary_client = _Client()
    with pytest.raises(DynamicTaskPlannerError):
        DynamicTaskPlanner(ordinary_client).create_plan(
            goal="形成事故复盘草案和采购决策备忘录",
            success_criteria=(
                SuccessCriterion(id="done", type="assertion", spec={"required": True}),
            ),
            capabilities=capabilities,
        )
    assert ordinary_client.payload is not None
    assert ordinary_client.payload["capabilities"] == []

    plan = DynamicTaskPlanner(_ManagedWorkspacePlanClient()).create_plan(
        goal="修改并验证退款能力",
        success_criteria=(
            SuccessCriterion(id="verified", type="assertion", spec={"required": True}),
        ),
        capabilities=capabilities,
    )
    assert [step.kind for step in plan.steps] == ["tool.write", "tool.execute", "answer"]


def _scoped_read_snapshot(
    *,
    name: str,
    applicability: dict[str, object] | None,
) -> CapabilitySnapshot:
    """构造带可选结构化目标范围的只读工作区能力。"""

    contract: dict[str, object] = {"risk_class": "read"}
    if applicability is not None:
        contract["applicability"] = applicability
    payload = {
        "capability_type": "tool",
        "capability_id": f"tool_{name.replace('.', '_')}",
        "tenant_id": "tenant_demo",
        "name": name,
        "contract": contract,
        "model_view": {"name": name, "input_schema": {"path": "string"}},
        "user_view": {"name": name},
        "audit_view": {"managed_workspace": {"workspace_id": "demo"}},
    }
    return CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )


class _ScopedReadClient:
    """记录相关性过滤后的能力目录，并按预期能力生成最小计划。"""

    def __init__(self, expected_capability: str | None) -> None:
        """保存本轮应保留的唯一能力名称；None表示纯回答。"""

        self.expected_capability = expected_capability
        self.payload: dict | None = None

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """断言模型只看到相关能力，再返回能力读取或直接回答计划。"""

        self.payload = user_payload
        names = [item["name"] for item in user_payload["capabilities"]]
        assert names == ([self.expected_capability] if self.expected_capability else [])
        steps: list[dict[str, object]] = []
        if self.expected_capability:
            steps.append(
                {
                    "draft_id": "read",
                    "title": "读取相关工作区代码",
                    "kind": "tool.read",
                    "capability_refs": [self.expected_capability],
                }
            )
        steps.append(
            {
                "draft_id": "answer",
                "title": "形成结论",
                "kind": "answer",
                "depends_on": ["read"] if self.expected_capability else [],
            }
        )
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": steps,
        }


def test_planner_excludes_unrelated_goal_scoped_refund_workspace() -> None:
    """已给齐事故事实时不得向Planner披露无关退款工作区读取能力。"""

    refund = _scoped_read_snapshot(
        name="workspace.refund.read",
        applicability={
            "mode": "goal_scoped",
            "domains": ["refund"],
            "aliases": ["退款"],
        },
    )
    client = _ScopedReadClient(None)

    plan = DynamicTaskPlanner(client).create_plan(
        goal=(
            "已知INC-742发布时间、错误率、P95、连接池和回滚恢复事实，"
            "请形成事故分析与复盘草案。"
        ),
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(refund,),
    )

    assert [step.kind for step in plan.steps] == ["answer"]


def test_planner_retains_goal_scoped_refund_workspace_for_refund_goal() -> None:
    """退款目标应继续获得绑定的退款工作区读取能力。"""

    refund = _scoped_read_snapshot(
        name="workspace.refund.read",
        applicability={
            "mode": "goal_scoped",
            "domains": ["refund"],
            "aliases": ["退款"],
        },
    )
    client = _ScopedReadClient("workspace.refund.read")

    plan = DynamicTaskPlanner(client).create_plan(
        goal="检查退款模块代码并分析高额退款审批缺陷",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(refund,),
    )

    assert [step.kind for step in plan.steps] == ["tool.read", "answer"]


def test_planner_retains_generic_agent_workspace_for_code_change_without_path() -> None:
    """通用代码工作区按意图放行，不能要求用户预先知道具体文件路径。"""

    workspace = _scoped_read_snapshot(
        name="workspace.code.read",
        applicability={
            "mode": "agent_workspace",
            "intents": ["code_inspect", "code_change"],
        },
    )
    client = _ScopedReadClient("workspace.code.read")

    plan = DynamicTaskPlanner(client).create_plan(
        goal="修复登录模块偶发重复创建会话的问题并说明原因",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(workspace,),
    )

    assert [step.kind for step in plan.steps] == ["tool.read", "answer"]


def test_workspace_execute_intent_accepts_completion_of_real_tests() -> None:
    """用户要求完成真实测试时，规划器应投影已授权的工作区执行能力。"""

    assert _goal_has_workspace_intent("完成真实测试和提交", "code_execute")


class _RequiredKnowledgeRepairClient:
    """首轮漏掉必选知识步骤，第二轮按服务端错误补齐前置依赖。"""

    def __init__(self) -> None:
        """记录两次规划输入以验证修复发生在持久化之前。"""

        self.payloads: list[dict] = []

    def generate_json(self, system_prompt: str, user_payload: dict) -> dict:
        """先返回纯回答，收到 required knowledge 错误后返回知识前置。"""

        self.payloads.append(user_payload)
        steps = [
            {
                "draft_id": "answer",
                "title": "形成结论",
                "kind": "answer",
            }
        ]
        if len(self.payloads) > 1:
            assert "required knowledge" in user_payload["repair"]["failure_message"]
            steps = [
                {
                    "draft_id": "knowledge",
                    "title": "查询必选企业知识",
                    "kind": "knowledge",
                    "capability_refs": ["knowledge.search"],
                },
                {
                    "draft_id": "answer",
                    "title": "形成结论",
                    "kind": "answer",
                    "depends_on": ["knowledge"],
                },
            ]
        return {
            "goal": user_payload["goal"],
            "success_criteria": user_payload["success_criteria"],
            "steps": steps,
        }


def test_planner_repairs_missing_required_knowledge_before_execution_creation() -> None:
    """required knowledge 必须进入一次有界规划修复，不能在 start_task 尾部突然失败。"""

    payload = {
        "capability_type": "knowledge",
        "capability_id": "knowledge.search",
        "tenant_id": "tenant_demo",
        "name": "knowledge.search",
        "contract": {
            "risk_class": "read",
            "side_effect": "none",
            "required_for_answer": True,
        },
        "model_view": {"name": "knowledge.search", "knowledge_bases": []},
        "user_view": {"name": "企业知识检索"},
        "audit_view": {"knowledge_bases": []},
    }
    capability = CapabilitySnapshot(
        **payload,
        agent_id="agent_demo",
        checksum=capability_checksum(payload),
    )
    client = _RequiredKnowledgeRepairClient()

    plan = DynamicTaskPlanner(client).create_plan(
        goal="依据企业制度形成采购建议",
        success_criteria=(
            SuccessCriterion(id="done", type="assertion", spec={"required": True}),
        ),
        capabilities=(capability,),
    )

    assert len(client.payloads) == 2
    assert [step.kind for step in plan.steps] == ["knowledge", "answer"]
