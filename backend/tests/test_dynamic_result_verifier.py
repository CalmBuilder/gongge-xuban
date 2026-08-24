"""
@Time       : 2026/08/04 03:26
@Author     : zhanglp8181
@File       : test_dynamic_result_verifier.py
@CallChain  : pytest → DynamicResultVerifier → verification evidence
@Description: 验证结果不能靠模型文字自证，必须逐项引用已完成步骤。
"""

from app.dynamic_tasks.planning import (
    GuidanceDisposition,
    GuidanceRequirement,
    GuidanceSourceKind,
    NormalizedPlan,
    PlanStep,
    SuccessCriterion,
)
from app.dynamic_tasks.result_verifier import (
    DynamicTaskResult,
    GuidanceApplication,
    GuidanceApplicationItem,
    verify_dynamic_result,
)


def _plan() -> NormalizedPlan:
    """返回带一条可验证成功标准的两步计划。"""

    return NormalizedPlan(
        goal="生成简报",
        success_criteria=(
            SuccessCriterion(id="brief_ready", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(step_key="query_contract", title="查询合同", kind="tool.read"),
            PlanStep(
                step_key="answer",
                title="形成简报",
                kind="answer",
                depends_on=("query_contract",),
            ),
        ),
    )


def test_result_requires_completed_step_evidence_for_every_criterion() -> None:
    """验证不存在或未完成的引用都会使 verification 明确失败。"""

    invalid = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 风险简报",
            criterion_evidence={"brief_ready": ("invented_step",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
    )
    valid = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 风险简报",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
    )

    assert invalid["passed"] is False
    assert invalid["invalid_step_refs"] == ["invented_step"]
    assert valid["passed"] is True


def test_result_requires_declared_operation_values_in_markdown() -> None:
    """引用步骤但只写占位语不能通过，真实值及显式空配置必须进入交付正文。"""

    required = {
        "query_contract": {
            "name": "共格·序伴连接器测试",
            "enabled": True,
            "home_url": "",
        }
    }
    placeholder = verify_dynamic_result(
        DynamicTaskResult(
            markdown="应用名称为步骤返回值，状态与主页地址同上。",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
        required_evidence_by_step=required,
    )
    factual = verify_dynamic_result(
        DynamicTaskResult(
            markdown="应用名称：共格·序伴连接器测试；状态：已启用；主页地址：未配置。",
            criterion_evidence={"brief_ready": ("query_contract",)},
        ),
        plan=_plan(),
        completed_step_keys={"query_contract"},
        required_evidence_by_step=required,
    )

    assert placeholder["passed"] is False
    assert placeholder["missing_result_evidence"] == [
        "query_contract:enabled",
        "query_contract:home_url",
        "query_contract:name",
    ]
    assert factual["passed"] is True
    assert factual["missing_result_evidence"] == []


def test_answer_only_plan_may_cite_its_delivery_step_without_faking_external_evidence() -> None:
    """纯生成任务可由交付步骤证明正文，但未知步骤仍不能冒充工具或知识回执。"""

    plan = NormalizedPlan(
        goal="形成操作规范",
        success_criteria=(
            SuccessCriterion(id="document_ready", type="assertion", spec={"required": True}),
        ),
        steps=(PlanStep(step_key="write_playbook", title="形成规范", kind="answer"),),
    )
    accepted = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 售后升级处理\n\n## 输入\n订单号\n\n## 步骤\n核验订单。",
            criterion_evidence={"document_ready": ("write_playbook",)},
        ),
        plan=plan,
        completed_step_keys={"write_playbook"},
    )
    rejected = verify_dynamic_result(
        DynamicTaskResult(
            markdown="# 售后升级处理",
            criterion_evidence={"document_ready": ("invented_tool_receipt",)},
        ),
        plan=plan,
        completed_step_keys={"write_playbook"},
    )

    assert accepted["passed"] is True
    assert rejected["invalid_step_refs"] == ["invented_tool_receipt"]


def test_skill_guidance_requires_distinct_source_backed_visible_applications() -> None:
    """Skill不能只留下Use记录，至少三条原则必须来自正文并在最终交付中可见。"""

    source = (
        "A deep module has a small interface. Interface is the test surface. "
        "Accept dependencies, return results."
    )
    plan = NormalizedPlan(
        goal="评审模块设计",
        success_criteria=(
            SuccessCriterion(id="review_ready", type="assertion", spec={"required": True}),
        ),
        steps=(
            PlanStep(
                step_key="answer",
                title="形成评审",
                kind="answer",
                guidance_skill_use_ids=("use_design",),
            ),
        ),
    )
    markdown = (
        "采用 deep module 收拢事务语义。Interface is the test surface，围绕公开协议测试。"
        "实现 Accept dependencies, return results，避免隐藏全局状态。"
    )
    valid = DynamicTaskResult(
        markdown=markdown,
        criterion_evidence={"review_ready": ("answer",)},
        guidance_applications=(
            GuidanceApplication(
                skill_use_id="use_design",
                items=(
                    GuidanceApplicationItem(
                        principle="deep module",
                        application="收拢事务语义",
                        evidence_excerpt="采用 deep module 收拢事务语义",
                    ),
                    GuidanceApplicationItem(
                        principle="Interface is the test surface",
                        application="按公开协议组织测试",
                        evidence_excerpt="Interface is the test surface，围绕公开协议测试",
                    ),
                    GuidanceApplicationItem(
                        principle="Accept dependencies, return results",
                        application="显式依赖和返回值",
                        evidence_excerpt="Accept dependencies, return results，避免隐藏全局状态",
                    ),
                ),
            ),
        ),
    )
    accepted = verify_dynamic_result(
        valid,
        plan=plan,
        completed_step_keys={"answer"},
        guidance_source_catalog={"use_design": source},
    )
    missing = verify_dynamic_result(
        valid.model_copy(update={"guidance_applications": ()}),
        plan=plan,
        completed_step_keys={"answer"},
        guidance_source_catalog={"use_design": source},
    )
    invented = verify_dynamic_result(
        valid.model_copy(
            update={
                "guidance_applications": (
                    valid.guidance_applications[0].model_copy(
                        update={
                            "items": (
                                valid.guidance_applications[0].items[0].model_copy(
                                    update={"principle": "invented architecture law"}
                                ),
                                *valid.guidance_applications[0].items[1:],
                            )
                        }
                    ),
                )
            }
        ),
        plan=plan,
        completed_step_keys={"answer"},
        guidance_source_catalog={"use_design": source},
    )

    assert accepted["passed"] is True
    assert missing["guidance_application_errors"] == [
        "use_design:guidance_application_required"
    ]
    assert invented["guidance_application_errors"] == [
        "use_design:item_0:principle_not_in_skill"
    ]


def test_planned_guidance_requires_exact_requirement_identity_and_visible_evidence() -> None:
    """执行前冻结的Guidance要求必须逐项回证，不能在结果阶段换原则或伪装不适用。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "a" * 24,
        skill_use_id="use_design",
        skill_ref="codebase-design",
        source_kind=GuidanceSourceKind.REVIEWED_RESOURCE,
        source_ref="DESIGN-IT-TWICE.md",
        principle="Design it twice",
        task_mapping="比较两种支付模块边界",
        observable_acceptance="正文列出两条路线、权衡和推荐",
        disposition=GuidanceDisposition.APPLY,
    )
    plan = NormalizedPlan(
        goal="评审支付模块",
        success_criteria=(
            SuccessCriterion(id="review_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(
                step_key="answer",
                title="形成评审",
                kind="answer",
                guidance_skill_use_ids=("use_design",),
            ),
        ),
    )
    application = GuidanceApplication(
        skill_use_id="use_design",
        items=(
            GuidanceApplicationItem(
                requirement_id=requirement.requirement_id,
                principle=requirement.principle,
                application="比较适配器收敛与领域拆分",
                evidence_excerpt="路线A保留单体适配器；路线B按领域拆分",
            ),
        ),
    )
    result = DynamicTaskResult(
        markdown="路线A保留单体适配器；路线B按领域拆分。推荐先采用路线A。",
        criterion_evidence={"review_ready": ("answer",)},
        guidance_applications=(application,),
    )

    accepted = verify_dynamic_result(result, plan=plan, completed_step_keys={"answer"})
    missing = verify_dynamic_result(
        result.model_copy(update={"guidance_applications": ()}),
        plan=plan,
        completed_step_keys={"answer"},
    )
    relabelled = verify_dynamic_result(
        result.model_copy(
            update={
                "guidance_applications": (
                    application.model_copy(
                        update={
                            "items": (
                                application.items[0].model_copy(
                                    update={"principle": "Invented principle"}
                                ),
                            )
                        }
                    ),
                )
            }
        ),
        plan=plan,
        completed_step_keys={"answer"},
    )

    assert accepted["passed"] is True
    assert missing["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_requirement_required"
    ]
    assert relabelled["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_principle_mismatch"
    ]


def test_planned_guidance_phase_gate_rejects_hypotheses_without_runtime_loop() -> None:
    """无工具或知识回执时，Skill明确否决门不得被“待验证假设”绕过。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "b" * 24,
        skill_use_id="use_diagnosis",
        skill_ref="diagnosing-bugs",
        source_kind=GuidanceSourceKind.INSTRUCTIONS,
        source_ref="instructions",
        principle="Do not proceed to hypothesise without a loop.",
        task_mapping="无red-capable loop时停止并请求证据",
        observable_acceptance="只说明已知事实、缺口和取得证据的下一步",
        disposition=GuidanceDisposition.APPLY,
    )
    plan = NormalizedPlan(
        goal="在无运行权限时初步评估故障",
        success_criteria=(
            SuccessCriterion(id="assessment_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(
                step_key="answer",
                title="说明证据缺口",
                kind="answer",
                guidance_skill_use_ids=("use_diagnosis",),
            ),
        ),
    )
    application = GuidanceApplication(
        skill_use_id="use_diagnosis",
        items=(
            GuidanceApplicationItem(
                requirement_id=requirement.requirement_id,
                principle=requirement.principle,
                application="说明阻塞原因",
                evidence_excerpt="当前没有可运行的red-capable loop",
            ),
        ),
    )
    bypass = DynamicTaskResult(
        markdown=(
            "当前没有可运行的red-capable loop。\n"
            "H1 待验证：可能是缓存竞态。"
        ),
        criterion_evidence={"assessment_ready": ("answer",)},
        guidance_applications=(application,),
    )
    blocked = bypass.model_copy(
        update={"markdown": "当前没有可运行的red-capable loop；请提供脱敏trace。"}
    )

    rejected = verify_dynamic_result(bypass, plan=plan, completed_step_keys={"answer"})
    accepted = verify_dynamic_result(blocked, plan=plan, completed_step_keys={"answer"})

    assert rejected["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_phase_gate_bypassed"
    ]
    assert accepted["passed"] is True

    heading_bypass = bypass.model_copy(
        update={"markdown": "当前没有可运行的red-capable loop。\n## 最可能原因（待验证）\nv2.4 引入了慢查询。"}
    )
    heading_rejected = verify_dynamic_result(
        heading_bypass,
        plan=plan,
        completed_step_keys={"answer"},
    )
    assert heading_rejected["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_phase_gate_bypassed"
    ]


def test_guidance_delivery_requires_diagnostic_loop_to_reach_the_answer() -> None:
    """有真实运行前置时，诊断 Guidance 的假设、探针和退出条件必须进入正文。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "c" * 24,
        skill_use_id="use_diagnosis",
        skill_ref="diagnosing-bugs",
        source_kind=GuidanceSourceKind.INSTRUCTIONS,
        source_ref="instructions",
        principle="A tight red-capable feedback loop consumes hypothesis testing.",
        task_mapping="先运行检查，再按假设组织探针",
        observable_acceptance="正文列出假设、探针、退出条件和完成标准",
        disposition=GuidanceDisposition.APPLY,
    )
    plan = NormalizedPlan(
        goal="诊断故障",
        success_criteria=(
            SuccessCriterion(id="diagnosis_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(step_key="read", title="读取代码", kind="tool.read"),
            PlanStep(
                step_key="answer",
                title="形成诊断",
                kind="answer",
                depends_on=("read",),
                guidance_skill_use_ids=("use_diagnosis",),
            ),
        ),
    )
    application = GuidanceApplication(
        skill_use_id="use_diagnosis",
        items=(
            GuidanceApplicationItem(
                requirement_id=requirement.requirement_id,
                principle=requirement.principle,
                application="用三条假设和单变量探针验证故障",
                evidence_excerpt="H1：若代码为空返回，则修复后检查变绿。",
            ),
        ),
    )
    incomplete = DynamicTaskResult(
        markdown="H1：若代码为空返回，则修复后检查变绿。",
        criterion_evidence={"diagnosis_ready": ("read",)},
        guidance_applications=(application,),
    )
    complete = incomplete.model_copy(
        update={
            "markdown": (
                "H1：若代码为空返回，则修复后检查变绿。\n"
                "H2：若调用方未传参，则日志显示 preferences 为空。\n"
                "H3：若存储恢复丢失，则快照为空。\n"
                "每次只改变一个变量，使用单一变量探针；退出条件是检查从 red 变 green。"
                "完成标准：修复后检查通过且退出码为 0。"
            )
        }
    )

    rejected = verify_dynamic_result(incomplete, plan=plan, completed_step_keys={"read", "answer"})
    accepted = verify_dynamic_result(complete, plan=plan, completed_step_keys={"read", "answer"})

    assert rejected["passed"] is False
    assert rejected["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_completion_criteria_required",
        f"{requirement.requirement_id}:guidance_exit_criteria_required",
        f"{requirement.requirement_id}:guidance_hypotheses_required",
        f"{requirement.requirement_id}:guidance_probe_required",
    ]
    assert accepted["passed"] is True


def test_guidance_delivery_requires_coverage_for_document_change_behaviors() -> None:
    """文档类完成标准必须逐项声明改动行为的测试覆盖，不得只写总命令。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "d" * 24,
        skill_use_id="use_writing",
        skill_ref="writing-for-agents",
        source_kind=GuidanceSourceKind.INSTRUCTIONS,
        source_ref="instructions",
        principle="Keep each meaning in a single source of truth.",
        task_mapping="为 AGENTS.md 的付款改动定义明确的完成标准",
        observable_acceptance="正文逐项说明改动行为的测试覆盖",
        disposition=GuidanceDisposition.APPLY,
    )
    plan = NormalizedPlan(
        goal="整理 AGENTS.md",
        success_criteria=(
            SuccessCriterion(id="document_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(
                step_key="answer",
                title="交付文档",
                kind="answer",
                guidance_skill_use_ids=("use_writing",),
            ),
        ),
    )
    application = GuidanceApplication(
        skill_use_id="use_writing",
        items=(
            GuidanceApplicationItem(
                requirement_id=requirement.requirement_id,
                principle=requirement.principle,
                application="为每个文档改动列出对应检查",
                evidence_excerpt="完成标准",
            ),
        ),
    )
    incomplete = DynamicTaskResult(
        markdown="完成标准：pytest 与 ruff 命令通过且退出码为 0。",
        criterion_evidence={"document_ready": ("answer",)},
        guidance_applications=(application,),
    )
    complete = incomplete.model_copy(
        update={
            "markdown": (
                "完成标准：pytest 与 ruff 命令通过且退出码为 0。\n"
                "所有改动行为均有测试覆盖，并逐项记录对应检查。"
            )
        }
    )

    rejected = verify_dynamic_result(incomplete, plan=plan, completed_step_keys={"answer"})
    accepted = verify_dynamic_result(complete, plan=plan, completed_step_keys={"answer"})

    assert rejected["guidance_application_errors"] == [
        f"{requirement.requirement_id}:guidance_changed_behavior_test_coverage_required"
    ]
    assert accepted["passed"] is True


def test_guidance_delivery_does_not_treat_teaching_document_as_code_change() -> None:
    """教学讲义虽是文档交付物，但没有代码改动要求时不应强制测试覆盖口号。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "e" * 24,
        skill_use_id="use_teach",
        skill_ref="teach",
        source_kind=GuidanceSourceKind.INSTRUCTIONS,
        source_ref="instructions",
        principle="Retrieval practice builds storage strength.",
        task_mapping="通过检查题练习租户隔离、幂等和事务边界",
        observable_acceptance="教学文档包含概念解释、检查题和掌握标准",
        disposition=GuidanceDisposition.APPLY,
    )
    result = DynamicTaskResult(
        markdown=(
            "概念解释：租户隔离限制数据边界。\n"
            "检查题：为什么幂等键不能跨租户复用？\n"
            "掌握标准：能用一个例子说明事务边界。"
        ),
        criterion_evidence={"document_ready": ("answer",)},
        guidance_applications=(
            GuidanceApplication(
                skill_use_id="use_teach",
                items=(
                    GuidanceApplicationItem(
                        requirement_id=requirement.requirement_id,
                        principle=requirement.principle,
                        application="通过检查题要求回忆并应用边界概念",
                        evidence_excerpt="检查题：为什么幂等键不能跨租户复用？",
                    ),
                ),
            ),
        ),
    )
    plan = NormalizedPlan(
        goal="交付教学文档",
        success_criteria=(
            SuccessCriterion(id="document_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(
                step_key="answer",
                title="形成教学文档",
                kind="answer",
                guidance_skill_use_ids=("use_teach",),
            ),
        ),
    )

    verification = verify_dynamic_result(
        result,
        plan=plan,
        completed_step_keys={"answer"},
    )

    assert verification["passed"] is True
    assert verification["guidance_application_errors"] == []


def test_guidance_delivery_does_not_treat_business_change_as_code_change() -> None:
    """业务领域的“退款变更”不应凭空要求代码改动测试覆盖。"""

    requirement = GuidanceRequirement(
        requirement_id="guidreq_" + "f" * 24,
        skill_use_id="use_ask",
        skill_ref="ask-matt",
        source_kind=GuidanceSourceKind.INSTRUCTIONS,
        source_ref="instructions",
        principle="You don't remember every skill, so ask.",
        task_mapping="针对退款变更列出待确认的领域细节",
        observable_acceptance="交付物包含待确认问题或假设章节",
        disposition=GuidanceDisposition.APPLY,
    )
    plan = NormalizedPlan(
        goal="形成退款变更分析",
        success_criteria=(
            SuccessCriterion(id="analysis_ready", type="assertion", spec={"required": True}),
        ),
        guidance_requirements=(requirement,),
        steps=(
            PlanStep(
                step_key="answer",
                title="形成分析",
                kind="answer",
                guidance_skill_use_ids=("use_ask",),
            ),
        ),
    )
    result = DynamicTaskResult(
        markdown="待确认问题：退款状态的可回滚边界；假设：材料未给出数据库约束。",
        criterion_evidence={"analysis_ready": ("answer",)},
        guidance_applications=(
            GuidanceApplication(
                skill_use_id="use_ask",
                items=(
                    GuidanceApplicationItem(
                        requirement_id=requirement.requirement_id,
                        principle=requirement.principle,
                        application="列出待确认问题和明确假设",
                        evidence_excerpt="待确认问题：退款状态的可回滚边界",
                    ),
                ),
            ),
        ),
    )

    verification = verify_dynamic_result(
        result,
        plan=plan,
        completed_step_keys={"answer"},
    )

    assert verification["passed"] is True
    assert verification["guidance_application_errors"] == []
