"""
@Time       : 2026/07/22 14:00
@Author     : zhanglp8181
@File       : test_sop_scheduler.py
@CallChain  : pytest → 规范 SOP 编译 → 确定性调度计划
@Description: 验证输入等待、显式工具参数绑定、结构化回执分支和终态收口。
"""

from app.sop_runtime import compile_legacy_skill_card
from app.sop_runtime.scheduler import RuntimeAction, plan_next_action


def _quota_definition():
    """编译一个不依赖自然语言条件的报销额度查询代表定义。"""

    return compile_legacy_skill_card(
        {
            "skill_id": "skill_expense_quota_query",
            "name": "报销额度查询",
            "version": "2.0.0",
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "month": {"type": "string"},
                    },
                },
                "tool_result": {
                    "type": "object",
                    "properties": {
                        "expense_quota_query": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "collect_employee",
                    "type": "collect_info",
                    "name": "收集员工信息",
                    "expected_user_info": ["employee_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "node_id": "query_quota",
                    "type": "tool_call",
                    "name": "查询额度",
                    "allowed_actions": ["call_tool:expense.quota_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_id": "slots.employee_id",
                            "month": "slots.month",
                        },
                        "operation_result_key": "expense_quota_query",
                    },
                },
                {
                    "node_id": "reply_success",
                    "type": "response",
                    "name": "反馈查询结果",
                    "allowed_actions": ["answer_user"],
                },
                {
                    "node_id": "reply_failure",
                    "type": "response",
                    "name": "反馈查询失败",
                    "allowed_actions": ["answer_user"],
                },
            ],
            "edges": [
                {"source_node_id": "collect_employee", "next_node_id": "query_quota"},
                {
                    "source_node_id": "query_quota",
                    "next_node_id": "reply_success",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "tool_result.expense_quota_query.status"},
                        "right": {"value": "succeeded"},
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "query_quota",
                    "next_node_id": "reply_failure",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
            ],
            "start_node_id": "collect_employee",
            "terminal_node_ids": ["reply_success", "reply_failure"],
        }
    )


def _approval_definition():
    """编译一个只依赖结构化工作项结果的人工审批代表定义。"""

    return compile_legacy_skill_card(
        {
            "skill_id": "approval_test",
            "name": "审批验收",
            "version": "1.0.0",
            "condition_schemas": {
                "work_item": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "outcome": {"type": "string"},
                    },
                }
            },
            "nodes": [
                {
                    "node_id": "human_review",
                    "type": "human_task",
                    "name": "人工审批",
                    "metadata": {
                        "participant_policy": {
                            "candidate_role_codes": ["seal.approver"],
                            "completion_mode": "any",
                            "claim_required": True,
                            "allowed_outcomes": ["approved", "rejected"],
                        }
                    },
                },
                {
                    "node_id": "approved",
                    "type": "terminal",
                    "name": "审批通过",
                },
                {
                    "node_id": "rejected",
                    "type": "terminal",
                    "name": "审批拒绝",
                },
            ],
            "edges": [
                {
                    "source_node_id": "human_review",
                    "next_node_id": "approved",
                    "condition": {
                        "op": "eq",
                        "left": {"path": "work_item.outcome"},
                        "right": {"value": "approved"},
                    },
                    "priority": 100,
                },
                {
                    "source_node_id": "human_review",
                    "next_node_id": "rejected",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
            ],
            "start_node_id": "human_review",
            "terminal_node_ids": ["approved", "rejected"],
        }
    )


def _knowledge_definition():
    """编译一个只使用白名单槽位构造查询并按知识回执路由的代表定义。"""

    return compile_legacy_skill_card(
        {
            "skill_id": "knowledge_policy_test",
            "name": "制度核验",
            "version": "1.0.0",
            "condition_schemas": {
                "slots": {
                    "type": "object",
                    "properties": {
                        "employee_type": {"type": "string"},
                        "request_type": {"type": "string"},
                    },
                },
                "node_output": {
                    "type": "object",
                    "properties": {
                    "policy_evidence": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {"outcome": {"type": "string"}},
                            },
                        },
                        }
                    },
                },
            },
            "nodes": [
                {
                    "node_id": "verify_policy",
                    "type": "knowledge_query",
                    "name": "核验制度",
                    "instruction": "核验当前申请是否符合制度。",
                    "allowed_actions": ["knowledge_query"],
                    "metadata": {
                        "operation_input": {
                            "employee_type": "slots.employee_type",
                            "request_type": "slots.request_type",
                        },
                        "operation_result_key": "policy_evidence",
                        "knowledge_query": {
                            "query_type": "policy_check",
                            "desired_evidence": "返回适用条款和结论依据",
                            "max_chunks": 6,
                            "max_depth": 2,
                        },
                    },
                },
                {"node_id": "allowed", "type": "terminal", "name": "允许"},
                {"node_id": "manual_review", "type": "terminal", "name": "人工复核"},
            ],
            "edges": [
                {
                    "source_node_id": "verify_policy",
                    "next_node_id": "allowed",
                "condition": {
                    "op": "all",
                    "args": [
                        {
                            "op": "eq",
                            "left": {"path": "node_output.policy_evidence.status"},
                            "right": {"value": "succeeded"},
                        },
                        {
                            "op": "eq",
                            "left": {
                                "path": "node_output.policy_evidence.data.outcome"
                            },
                            "right": {"value": "evidence_found"},
                        },
                    ],
                },
                    "priority": 100,
                },
                {
                    "source_node_id": "verify_policy",
                    "next_node_id": "manual_review",
                    "condition": {"op": "always"},
                    "priority": 0,
                },
            ],
            "start_node_id": "verify_policy",
            "terminal_node_ids": ["allowed", "manual_review"],
        }
    )


def test_scheduler_waits_until_required_input_is_present() -> None:
    """验证缺少员工号时只返回结构化等待计划，不越过输入节点。"""

    plan = plan_next_action(
        _quota_definition(), current_node_id="collect_employee", slots={}
    )

    assert plan.action is RuntimeAction.WAIT_INPUT
    assert plan.expected_inputs == ("employee_id",)


def test_scheduler_builds_tool_arguments_only_from_declared_bindings() -> None:
    """验证工具命令只读取显式绑定字段，并忽略缺失的可选月份。"""

    definition = _quota_definition()
    advance = plan_next_action(
        definition,
        current_node_id="collect_employee",
        slots={"employee_id": "E001"},
    )
    plan = plan_next_action(
        definition,
        current_node_id=advance.next_node_id or "",
        slots={"employee_id": "E001", "untrusted": "ignored"},
    )

    assert advance.action is RuntimeAction.ADVANCE
    assert plan.action is RuntimeAction.CALL_TOOL
    assert plan.operation_name == "expense.quota_query"
    assert plan.operation_arguments == {"employee_id": "E001"}
    assert plan.result_key == "expense_quota_query"


def test_scheduler_builds_required_knowledge_query_from_declared_bindings() -> None:
    """验证知识节点不能被跳过，且查询载荷只包含定义声明的槽位。"""

    plan = plan_next_action(
        _knowledge_definition(),
        current_node_id="verify_policy",
        slots={
            "employee_type": "正式员工",
            "request_type": "年假",
            "untrusted": "不得进入查询",
        },
    )

    assert plan.action is RuntimeAction.QUERY_KNOWLEDGE
    assert plan.operation_name == "knowledge.search"
    assert plan.operation_arguments == {
        "query": "核验制度\n核验当前申请是否符合制度。\nemployee_type: 正式员工\nrequest_type: 年假",
        "query_type": "policy_check",
        "desired_evidence": "返回适用条款和结论依据",
        "max_chunks": 6,
        "max_depth": 2,
    }
    assert plan.result_key == "policy_evidence"


def test_scheduler_routes_knowledge_node_only_after_persisted_receipt() -> None:
    """验证知识节点必须等到 node_output 出现结构化回执后才能推进。"""

    definition = _knowledge_definition()
    pending = plan_next_action(
        definition,
        current_node_id="verify_policy",
        slots={"employee_type": "正式员工", "request_type": "年假"},
    )
    routed = plan_next_action(
        definition,
        current_node_id="verify_policy",
        slots={"employee_type": "正式员工", "request_type": "年假"},
        node_outputs={
            "policy_evidence": {
                "status": "succeeded",
                "data": {
                    "outcome": "evidence_found",
                    "evidence_pack": [{"content": "正式员工享有年假。"}],
                },
            }
        },
    )

    assert pending.action is RuntimeAction.QUERY_KNOWLEDGE
    assert routed.action is RuntimeAction.ADVANCE
    assert routed.next_node_id == "allowed"


def test_scheduler_routes_by_structured_receipt_and_completes_terminal() -> None:
    """验证成功回执由受限 DSL 选择成功终态，终态再显式完成实例。"""

    definition = _quota_definition()
    routed = plan_next_action(
        definition,
        current_node_id="query_quota",
        slots={"employee_id": "E001"},
        tool_results={
            "expense_quota_query": {
                "status": "succeeded",
                "data": {"remaining": 20000.0},
                "error": None,
            }
        },
    )
    completed = plan_next_action(
        definition,
        current_node_id=routed.next_node_id or "",
        slots={"employee_id": "E001"},
    )

    assert routed.action is RuntimeAction.ADVANCE
    assert routed.next_node_id == "reply_success"
    assert completed.action is RuntimeAction.COMPLETE
    assert completed.outcome == "completed"


def test_scheduler_uses_default_branch_for_failed_receipt() -> None:
    """验证失败回执稳定进入最低优先级默认分支。"""

    plan = plan_next_action(
        _quota_definition(),
        current_node_id="query_quota",
        slots={"employee_id": "E001"},
        tool_results={
            "expense_quota_query": {
                "status": "failed",
                "data": {},
                "error": {"code": "UPSTREAM_TIMEOUT"},
            }
        },
    )

    assert plan.action is RuntimeAction.ADVANCE
    assert plan.next_node_id == "reply_failure"


def test_scheduler_waits_for_structured_work_item_and_routes_approved_outcome() -> None:
    """验证人工节点在工作项完成前等待，完成后仅按结构化 outcome 选择分支。"""

    definition = _approval_definition()
    waiting = plan_next_action(
        definition,
        current_node_id="human_review",
        slots={},
    )
    approved = plan_next_action(
        definition,
        current_node_id="human_review",
        slots={},
        work_items={"status": "completed", "outcome": "approved"},
    )

    assert waiting.action is RuntimeAction.WAIT_WORK_ITEM
    assert approved.action is RuntimeAction.ADVANCE
    assert approved.next_node_id == "approved"


def test_scheduler_rejects_unlisted_work_item_outcome() -> None:
    """验证工作项回执包含定义未允许的结果时不会继续流程。"""

    plan = plan_next_action(
        _approval_definition(),
        current_node_id="human_review",
        slots={},
        work_items={"status": "completed", "outcome": "returned"},
    )

    assert plan.action is RuntimeAction.FAIL
    assert plan.error_code == "RUNTIME_WORK_ITEM_OUTCOME_INVALID"
