"""
@Time       : 2026/07/22 10:40
@Author     : zhanglp8181
@File       : test_condition_dsl.py
@CallChain  : pytest → 条件 DSL 编译/求值 → 稳定分支结果或错误码
@Description: 验证受限操作符、路径类型门禁、优先级、确定性和运行时失败边界。
"""

import pytest
from pydantic import ValidationError

from app.sop_runtime.condition_dsl import (
    AlwaysCondition,
    ConditionBranch,
    ConditionCompilationError,
    ConditionErrorCode,
    compile_condition_branches,
    compile_condition_dsl,
    evaluate_condition,
    parse_condition_dsl,
    select_condition_branch,
    validate_condition_branches,
)
from app.sop_runtime.capabilities import DEFAULT_CAPABILITY_REGISTRY
from app.sop_runtime.definition import NodeType


SCHEMAS: dict[str, dict[str, object]] = {
    "slots": {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "amount": {"type": "number"},
            "confirmation": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    },
    "policy_result": {
        "type": "object",
        "properties": {
            "limit": {"type": "number"},
            "outcome": {"type": "string"},
        },
    },
    "tool_result": {
        "type": "object",
        "properties": {"status": {"type": "string"}},
    },
}


def _expense_condition() -> dict[str, object]:
    """构造同时检查必填槽位和报销额度的代表性条件。"""

    return {
        "op": "all",
        "args": [
            {"op": "exists", "path": "slots.employee_id"},
            {
                "op": "gte",
                "left": {"path": "slots.amount"},
                "right": {"path": "policy_result.limit"},
            },
        ],
    }


def test_compiler_accepts_declared_paths_and_produces_stable_checksum() -> None:
    """验证声明路径可编译，引用集合和摘要不受字典顺序影响。"""

    first = compile_condition_dsl(_expense_condition(), schemas=SCHEMAS)
    second = compile_condition_dsl(_expense_condition(), schemas=dict(reversed(SCHEMAS.items())))

    assert first.referenced_paths == (
        "policy_result.limit",
        "slots.amount",
        "slots.employee_id",
    )
    assert first.checksum == second.checksum
    assert len(first.checksum) == 64


def test_evaluator_is_deterministic_for_true_false_and_missing_values() -> None:
    """验证相同输入重复求值一致，并区分不匹配与缺少比较字段。"""

    compiled = compile_condition_dsl(_expense_condition(), schemas=SCHEMAS)
    matching_data = {
        "slots": {"employee_id": "E001", "amount": 1200.0},
        "policy_result": {"limit": 1000.0},
    }
    false_data = {
        "slots": {"employee_id": "E001", "amount": 800.0},
        "policy_result": {"limit": 1000.0},
    }
    missing_data = {
        "slots": {"employee_id": "E001"},
        "policy_result": {"limit": 1000.0},
    }

    assert evaluate_condition(compiled, matching_data).matched is True
    assert evaluate_condition(compiled, matching_data).matched is True
    assert evaluate_condition(compiled, false_data).matched is False
    missing = evaluate_condition(compiled, missing_data)
    assert missing.matched is None
    assert missing.error_code is ConditionErrorCode.VALUE_MISSING
    assert missing.error_path == "slots.amount"


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("eq", True),
        ("ne", False),
        ("gt", False),
        ("gte", True),
        ("lt", False),
        ("lte", True),
    ],
)
def test_scalar_comparison_operators(operator: str, expected: bool) -> None:
    """验证比较操作符不做字符串与数值之间的隐式转换。"""

    compiled = compile_condition_dsl(
        {
            "op": operator,
            "left": {"path": "slots.amount"},
            "right": {"value": 1000.0},
        },
        schemas=SCHEMAS,
    )

    assert evaluate_condition(compiled, {"slots": {"amount": 1000.0}}).matched is expected


def test_in_not_any_and_missing_operators() -> None:
    """验证集合、取反、任一匹配和缺失判断均使用封闭 AST。"""

    compiled = compile_condition_dsl(
        {
            "op": "any",
            "args": [
                {
                    "op": "in",
                    "left": {"path": "slots.confirmation"},
                    "right": {"value": ["confirm", "approve"]},
                },
                {
                    "op": "not",
                    "arg": {"op": "missing", "path": "tool_result.status"},
                },
            ],
        },
        schemas=SCHEMAS,
    )

    assert (
        evaluate_condition(
            compiled,
            {"slots": {"confirmation": "approve"}, "tool_result": {}},
        ).matched
        is True
    )
    assert (
        evaluate_condition(
            compiled,
            {"slots": {"confirmation": "cancel"}, "tool_result": {"status": "success"}},
        ).matched
        is True
    )


def test_compiler_rejects_undeclared_path() -> None:
    """验证发布期拒绝未由输入或前序输出 schema 声明的字段路径。"""

    with pytest.raises(ConditionCompilationError) as exc_info:
        compile_condition_dsl(
            {"op": "exists", "path": "slots.unknown"},
            schemas=SCHEMAS,
        )

    assert exc_info.value.code is ConditionErrorCode.UNDECLARED_PATH
    assert exc_info.value.path == "slots.unknown"


def test_compiler_rejects_cross_type_and_non_numeric_ordering() -> None:
    """验证字符串数值比较和字符串大小比较在发布期失败。"""

    invalid_conditions = (
        {
            "op": "eq",
            "left": {"path": "slots.amount"},
            "right": {"value": "1000"},
        },
        {
            "op": "gt",
            "left": {"path": "slots.employee_id"},
            "right": {"value": "E001"},
        },
    )
    for condition in invalid_conditions:
        with pytest.raises(ConditionCompilationError) as exc_info:
            compile_condition_dsl(condition, schemas=SCHEMAS)
        assert exc_info.value.code is ConditionErrorCode.TYPE_MISMATCH


def test_parser_rejects_unknown_operator_and_attribute_style_path() -> None:
    """验证未知操作符、对象属性语法和数组下标无法进入 AST。"""

    with pytest.raises(ValidationError):
        parse_condition_dsl({"op": "execute", "code": "__import__('os')"})
    with pytest.raises(ValidationError):
        parse_condition_dsl({"op": "exists", "path": "slots.user.__class__"})
    with pytest.raises(ValidationError):
        parse_condition_dsl({"op": "exists", "path": "slots.items[0]"})


def test_branch_priorities_are_unique_and_default_is_last() -> None:
    """验证高数值优先、低数值默认的分支顺序契约。"""

    conditional = parse_condition_dsl({"op": "exists", "path": "slots.employee_id"})
    default = AlwaysCondition()
    validate_condition_branches(
        (
            ConditionBranch(branch_id="conditional", condition=conditional, priority=100),
            ConditionBranch(branch_id="default", condition=default, priority=0),
        )
    )

    with pytest.raises(ConditionCompilationError) as duplicate:
        validate_condition_branches(
            (
                ConditionBranch(branch_id="conditional", condition=conditional, priority=10),
                ConditionBranch(branch_id="default", condition=default, priority=10),
            )
        )
    assert duplicate.value.code is ConditionErrorCode.DUPLICATE_PRIORITY

    with pytest.raises(ConditionCompilationError) as misplaced:
        validate_condition_branches(
            (
                ConditionBranch(branch_id="conditional", condition=conditional, priority=0),
                ConditionBranch(branch_id="default", condition=default, priority=100),
            )
        )
    assert misplaced.value.code is ConditionErrorCode.DEFAULT_PRIORITY_INVALID


def test_branch_selection_uses_explicit_priority_when_multiple_conditions_match() -> None:
    """验证多个条件同时命中时选择数值优先级最高的分支。"""

    branches = (
        ConditionBranch(
            branch_id="high_amount",
            condition=parse_condition_dsl(
                {
                    "op": "gte",
                    "left": {"path": "slots.amount"},
                    "right": {"value": 1000.0},
                }
            ),
            priority=200,
        ),
        ConditionBranch(
            branch_id="has_amount",
            condition=parse_condition_dsl({"op": "exists", "path": "slots.amount"}),
            priority=100,
        ),
        ConditionBranch(branch_id="default", condition=AlwaysCondition(), priority=0),
    )
    compiled = compile_condition_branches(branches, schemas=SCHEMAS)

    selected = select_condition_branch(compiled, {"slots": {"amount": 1200.0}})

    assert selected.selected_branch_id == "high_amount"
    assert selected.error_code is None


def test_branch_set_requires_explicit_default() -> None:
    """验证没有 always 的不完备分支集合在发布期失败。"""

    conditional = parse_condition_dsl({"op": "exists", "path": "slots.employee_id"})
    with pytest.raises(ConditionCompilationError) as exc_info:
        validate_condition_branches(
            (ConditionBranch(branch_id="conditional", condition=conditional, priority=10),)
        )

    assert exc_info.value.code is ConditionErrorCode.MISSING_DEFAULT


def test_exists_accepts_object_array_schema_without_enabling_object_comparison() -> None:
    """验证对象数组可用于存在性和参数绑定校验，但仍不能进入对象值比较。"""

    schemas = {
        "slots": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                    },
                }
            },
        }
    }

    compiled = compile_condition_dsl(
        {"op": "exists", "path": "slots.items"},
        schemas=schemas,
    )

    assert compiled.referenced_paths == ("slots.items",)
    with pytest.raises(ConditionCompilationError) as caught:
        compile_condition_dsl(
            {
                "op": "eq",
                "left": {"path": "slots.items"},
                "right": {"value": ["A4纸"]},
            },
            schemas=schemas,
        )
    assert caught.value.code is ConditionErrorCode.TYPE_MISMATCH


def test_restricted_decision_capability_is_registered_and_runtime_wired() -> None:
    """验证 Stage B1 完成后受限 DSL 已由确定性调度器真实执行。"""

    assert DEFAULT_CAPABILITY_REGISTRY.supports_definition(
        "decision.restricted_dsl", NodeType.DECISION
    )
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution(
        "decision.restricted_dsl", NodeType.DECISION
    )
