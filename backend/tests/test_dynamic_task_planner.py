"""
@Time       : 2026/08/04 02:25
@Author     : zhanglp8181
@File       : test_dynamic_task_planner.py
@CallChain  : pytest → DynamicTaskPlanner → JSON client/NormalizedPlan
@Description: 验证动态规划能力最小披露、任务契约冻结和预算收紧。
"""

from __future__ import annotations

import pytest

from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.dynamic_tasks.planner_service import DynamicTaskPlanner, DynamicTaskPlannerError
from app.dynamic_tasks.planning import DynamicPlanDraft, SuccessCriterion, normalize_plan_draft


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
        "steps",
    }
    assert "draft_id" in client.payload["output_contract"]["steps"][0]
    assert "internal.invalid" not in str(client.payload)
    assert "authorization" not in str(client.payload)
    assert "audit_view" not in str(client.payload)
    assert plan.goal == "生成续约风险简报"
    assert plan.success_criteria == (criterion,)
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
    plan = DynamicTaskPlanner(_ManagedWorkspacePlanClient()).create_plan(
        goal="修改并验证退款能力",
        success_criteria=(
            SuccessCriterion(id="verified", type="assertion", spec={"required": True}),
        ),
        capabilities=capabilities,
    )
    assert [step.kind for step in plan.steps] == ["tool.write", "tool.execute", "answer"]
