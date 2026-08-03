"""
@Time       : 2026/07/22 10:25
@Author     : zhanglp8181
@File       : condition_dsl.py
@CallChain  : SOP 发布编译 → 条件 DSL 编译 → Runtime 分支条件求值
@Description: 定义受限 JSON 条件 AST、发布期路径类型校验和确定性运行时求值。
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Mapping, TypeAlias

from pydantic import Field, TypeAdapter

from app.sop_runtime.contracts import RuntimeContract


CONDITION_PATH_PATTERN = re.compile(
    r"^(slots|node_output|tool_result|work_item|policy_result)"
    r"(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
)
NUMERIC_JSON_TYPES = frozenset({"integer", "number"})


class ConditionOperator(StrEnum):
    """受限 DSL 唯一允许的逻辑、存在性、比较和默认操作符。"""

    ALL = "all"
    ANY = "any"
    NOT = "not"
    EXISTS = "exists"
    MISSING = "missing"
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    ALWAYS = "always"


class ConditionErrorCode(StrEnum):
    """条件编译和求值向调用方返回的稳定错误码。"""

    INVALID_PATH = "CONDITION_INVALID_PATH"
    UNDECLARED_PATH = "CONDITION_UNDECLARED_PATH"
    UNSUPPORTED_SCHEMA = "CONDITION_UNSUPPORTED_SCHEMA"
    TYPE_MISMATCH = "CONDITION_TYPE_MISMATCH"
    DUPLICATE_PRIORITY = "CONDITION_DUPLICATE_PRIORITY"
    MULTIPLE_DEFAULTS = "CONDITION_MULTIPLE_DEFAULTS"
    MISSING_DEFAULT = "CONDITION_MISSING_DEFAULT"
    DEFAULT_PRIORITY_INVALID = "CONDITION_DEFAULT_PRIORITY_INVALID"
    VALUE_MISSING = "CONDITION_VALUE_MISSING"
    RUNTIME_TYPE_MISMATCH = "CONDITION_RUNTIME_TYPE_MISMATCH"


class ConditionReference(RuntimeContract):
    """引用发布期声明的数据根和字段路径，不允许对象属性或数组下标访问。"""

    path: str = Field(min_length=3, max_length=512, pattern=CONDITION_PATH_PATTERN.pattern)


class ConditionLiteral(RuntimeContract):
    """保存 JSON 标量或同类型标量数组，不接受对象和可执行值。"""

    value: bool | int | float | str | None | tuple[bool | int | float | str | None, ...]


ConditionOperand: TypeAlias = ConditionReference | ConditionLiteral


class AlwaysCondition(RuntimeContract):
    """显式表示无条件匹配的默认路径。"""

    op: Literal[ConditionOperator.ALWAYS] = ConditionOperator.ALWAYS


class ExistenceCondition(RuntimeContract):
    """判断声明路径是否存在或缺失。"""

    op: Literal[ConditionOperator.EXISTS, ConditionOperator.MISSING]
    path: str = Field(min_length=3, max_length=512, pattern=CONDITION_PATH_PATTERN.pattern)


class ComparisonCondition(RuntimeContract):
    """比较两个受限操作数，不执行字符串脚本或隐式类型转换。"""

    op: Literal[
        ConditionOperator.EQ,
        ConditionOperator.NE,
        ConditionOperator.GT,
        ConditionOperator.GTE,
        ConditionOperator.LT,
        ConditionOperator.LTE,
        ConditionOperator.IN,
    ]
    left: ConditionOperand
    right: ConditionOperand


class NotCondition(RuntimeContract):
    """对单个子条件执行逻辑取反。"""

    op: Literal[ConditionOperator.NOT] = ConditionOperator.NOT
    arg: ConditionDsl


class LogicalCondition(RuntimeContract):
    """按确定顺序对一个或多个子条件执行 all/any 逻辑。"""

    op: Literal[ConditionOperator.ALL, ConditionOperator.ANY]
    args: tuple[ConditionDsl, ...] = Field(min_length=1, max_length=100)


ConditionDsl: TypeAlias = Annotated[
    AlwaysCondition | ExistenceCondition | ComparisonCondition | NotCondition | LogicalCondition,
    Field(discriminator="op"),
]


class ConditionBranch(RuntimeContract):
    """绑定条件与显式优先级；数值越大越先求值。"""

    branch_id: str = Field(min_length=1, max_length=256)
    condition: ConditionDsl
    priority: int


class CompiledCondition(RuntimeContract):
    """保存发布期已校验 AST、引用路径和稳定摘要。"""

    ast: ConditionDsl
    referenced_paths: tuple[str, ...]
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompiledConditionBranch(RuntimeContract):
    """保存可直接按优先级选择的已编译分支。"""

    branch_id: str = Field(min_length=1, max_length=256)
    condition: CompiledCondition
    priority: int


class ConditionEvaluationResult(RuntimeContract):
    """返回确定匹配结果，或返回唯一稳定错误码而不抛出脚本异常。"""

    matched: bool | None = None
    error_code: ConditionErrorCode | None = None
    error_path: str | None = None


class ConditionBranchSelectionResult(RuntimeContract):
    """返回命中的分支标识，或返回求值阶段的稳定错误。"""

    selected_branch_id: str | None = None
    error_code: ConditionErrorCode | None = None
    error_path: str | None = None


class ConditionCompilationError(ValueError):
    """携带发布期条件错误码、字段路径和可修复说明。"""

    def __init__(
        self,
        code: ConditionErrorCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        """保存稳定错误信息，供发布 API 转换为结构化诊断。"""

        self.code = code
        self.path = path
        super().__init__(message)


_CONDITION_ADAPTER = TypeAdapter(ConditionDsl)
_MISSING = object()


def parse_condition_dsl(payload: Mapping[str, object]) -> ConditionDsl:
    """把 JSON 对象解析为封闭操作符集合的条件 AST。"""

    return _CONDITION_ADAPTER.validate_python(payload)


def compile_condition_dsl(
    payload: Mapping[str, object] | ConditionDsl,
    *,
    schemas: Mapping[str, Mapping[str, object]],
) -> CompiledCondition:
    """校验全部引用路径和比较类型，并生成可进入发布 checksum 的稳定结果。"""

    condition = (
        payload
        if isinstance(
            payload,
            (
                AlwaysCondition,
                ExistenceCondition,
                ComparisonCondition,
                NotCondition,
                LogicalCondition,
            ),
        )
        else parse_condition_dsl(payload)
    )
    referenced_paths: set[str] = set()
    _validate_condition(condition, schemas, referenced_paths)
    serialized = condition.model_dump(mode="json")
    checksum = hashlib.sha256(
        json.dumps(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return CompiledCondition(
        ast=condition,
        referenced_paths=tuple(sorted(referenced_paths)),
        checksum=checksum,
    )


def validate_condition_branches(branches: tuple[ConditionBranch, ...]) -> None:
    """确保分支优先级唯一，且唯一 always 位于最低数值优先级。"""

    if not branches:
        raise ConditionCompilationError(
            ConditionErrorCode.MISSING_DEFAULT,
            "分支集合不能为空，且必须声明 always 默认分支。",
        )

    priorities = [branch.priority for branch in branches]
    if len(priorities) != len(set(priorities)):
        raise ConditionCompilationError(
            ConditionErrorCode.DUPLICATE_PRIORITY,
            "同一分支集合的 priority 必须唯一。",
        )
    defaults = [branch for branch in branches if branch.condition.op is ConditionOperator.ALWAYS]
    if len(defaults) > 1:
        raise ConditionCompilationError(
            ConditionErrorCode.MULTIPLE_DEFAULTS,
            "同一分支集合最多只能声明一个 always 默认分支。",
        )
    if not defaults:
        raise ConditionCompilationError(
            ConditionErrorCode.MISSING_DEFAULT,
            "分支集合必须声明一个 always 默认分支。",
        )
    if defaults and defaults[0].priority != min(priorities):
        raise ConditionCompilationError(
            ConditionErrorCode.DEFAULT_PRIORITY_INVALID,
            "always 默认分支必须使用最低数值 priority。",
        )


def compile_condition_branches(
    branches: tuple[ConditionBranch, ...],
    *,
    schemas: Mapping[str, Mapping[str, object]],
) -> tuple[CompiledConditionBranch, ...]:
    """校验分支集合并按高优先级在前生成不可变编译结果。"""

    validate_condition_branches(branches)
    return tuple(
        CompiledConditionBranch(
            branch_id=branch.branch_id,
            condition=compile_condition_dsl(branch.condition, schemas=schemas),
            priority=branch.priority,
        )
        for branch in sorted(branches, key=lambda item: item.priority, reverse=True)
    )


def select_condition_branch(
    branches: tuple[CompiledConditionBranch, ...],
    data: Mapping[str, object],
) -> ConditionBranchSelectionResult:
    """按已冻结优先级选择首个匹配分支，遇到求值错误时停止并返回错误。"""

    for branch in branches:
        result = evaluate_condition(branch.condition, data)
        if result.error_code is not None:
            return ConditionBranchSelectionResult(
                error_code=result.error_code,
                error_path=result.error_path,
            )
        if result.matched:
            return ConditionBranchSelectionResult(selected_branch_id=branch.branch_id)
    return ConditionBranchSelectionResult(error_code=ConditionErrorCode.MISSING_DEFAULT)


def evaluate_condition(
    condition: CompiledCondition | ConditionDsl,
    data: Mapping[str, object],
) -> ConditionEvaluationResult:
    """在只读映射数据上确定性求值，不执行代码、属性访问或隐式类型转换。"""

    ast = condition.ast if isinstance(condition, CompiledCondition) else condition
    try:
        return ConditionEvaluationResult(matched=_evaluate(ast, data))
    except _ConditionRuntimeError as exc:
        return ConditionEvaluationResult(error_code=exc.code, error_path=exc.path)


class _ConditionRuntimeError(ValueError):
    """在求值器内部携带稳定错误码，并由公共接口转换为结果对象。"""

    def __init__(self, code: ConditionErrorCode, path: str | None = None) -> None:
        """记录失败类型和相关路径，不暴露底层 Python 异常。"""

        self.code = code
        self.path = path
        super().__init__(code.value)


def _validate_condition(
    condition: ConditionDsl,
    schemas: Mapping[str, Mapping[str, object]],
    referenced_paths: set[str],
) -> None:
    """递归校验条件引用和比较操作数类型。"""

    if isinstance(condition, AlwaysCondition):
        return
    if isinstance(condition, ExistenceCondition):
        _schema_type_for_path(condition.path, schemas)
        referenced_paths.add(condition.path)
        return
    if isinstance(condition, NotCondition):
        _validate_condition(condition.arg, schemas, referenced_paths)
        return
    if isinstance(condition, LogicalCondition):
        for argument in condition.args:
            _validate_condition(argument, schemas, referenced_paths)
        return
    left_type = _operand_type(condition.left, schemas, referenced_paths)
    right_type = _operand_type(condition.right, schemas, referenced_paths)
    _validate_comparison_types(condition.op, left_type, right_type)


def _operand_type(
    operand: ConditionOperand,
    schemas: Mapping[str, Mapping[str, object]],
    referenced_paths: set[str],
) -> str:
    """解析引用或字面量的稳定 JSON 类型。"""

    if isinstance(operand, ConditionReference):
        referenced_paths.add(operand.path)
        return _schema_type_for_path(operand.path, schemas)
    return _literal_json_type(operand.value)


def _schema_type_for_path(
    path: str,
    schemas: Mapping[str, Mapping[str, object]],
) -> str:
    """沿 JSON Schema properties 查找声明字段并返回单一基础类型。"""

    if not CONDITION_PATH_PATTERN.fullmatch(path):
        raise ConditionCompilationError(
            ConditionErrorCode.INVALID_PATH,
            "条件路径格式无效。",
            path=path,
        )
    root, *segments = path.split(".")
    schema: Mapping[str, object] | None = schemas.get(root)
    if schema is None:
        raise ConditionCompilationError(
            ConditionErrorCode.UNDECLARED_PATH,
            "条件数据根未声明 schema。",
            path=path,
        )
    for segment in segments:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            raise ConditionCompilationError(
                ConditionErrorCode.UNDECLARED_PATH,
                "条件路径没有被输入或前序输出 schema 声明。",
                path=path,
            )
        next_schema = properties[segment]
        if not isinstance(next_schema, Mapping):
            raise ConditionCompilationError(
                ConditionErrorCode.UNSUPPORTED_SCHEMA,
                "条件路径 schema 必须是 JSON 对象。",
                path=path,
            )
        schema = next_schema
    json_type = schema.get("type")
    if not isinstance(json_type, str) or json_type not in {
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
    }:
        raise ConditionCompilationError(
            ConditionErrorCode.UNSUPPORTED_SCHEMA,
            "条件字段必须声明唯一且受支持的 JSON type。",
            path=path,
        )
    if json_type == "array":
        items = schema.get("items")
        item_type = items.get("type") if isinstance(items, Mapping) else None
        if not isinstance(item_type, str) or item_type not in {
            "null",
            "boolean",
            "integer",
            "number",
            "string",
            "object",
        }:
            raise ConditionCompilationError(
                ConditionErrorCode.UNSUPPORTED_SCHEMA,
                "条件数组字段必须声明单一受支持的 items.type。",
                path=path,
            )
        return f"array:{item_type}"
    return json_type


def _literal_json_type(value: object) -> str:
    """按 JSON 而不是 Python 继承关系识别字面量类型。"""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, tuple):
        item_types = {_literal_json_type(item) for item in value}
        if len(item_types) != 1:
            raise ConditionCompilationError(
                ConditionErrorCode.TYPE_MISMATCH,
                "in 操作的字面量数组必须包含同一 JSON 类型。",
            )
        item_type = next(iter(item_types), "null")
        return f"array:{item_type}"
    raise ConditionCompilationError(
        ConditionErrorCode.TYPE_MISMATCH,
        "条件字面量不是受支持的 JSON 类型。",
    )


def _validate_comparison_types(
    operator: ConditionOperator,
    left_type: str,
    right_type: str,
) -> None:
    """拒绝发布期可确定的跨类型比较和非法 in 右值。"""

    if operator is ConditionOperator.IN:
        if not right_type.startswith("array:") or not _types_compatible(
            left_type, right_type.removeprefix("array:")
        ):
            raise ConditionCompilationError(
                ConditionErrorCode.TYPE_MISMATCH,
                "in 左值类型必须与右侧数组元素类型兼容。",
            )
        return
    if operator in {
        ConditionOperator.GT,
        ConditionOperator.GTE,
        ConditionOperator.LT,
        ConditionOperator.LTE,
    }:
        if left_type not in NUMERIC_JSON_TYPES or right_type not in NUMERIC_JSON_TYPES:
            raise ConditionCompilationError(
                ConditionErrorCode.TYPE_MISMATCH,
                "大小比较两侧必须是 integer 或 number。",
            )
        return
    if not _types_compatible(left_type, right_type):
        raise ConditionCompilationError(
            ConditionErrorCode.TYPE_MISMATCH,
            "eq/ne 两侧 JSON 类型不兼容。",
        )


def _types_compatible(left_type: str, right_type: str) -> bool:
    """仅允许完全同型或 integer/number 之间的数值兼容。"""

    return left_type == right_type or {left_type, right_type} <= NUMERIC_JSON_TYPES


def _evaluate(condition: ConditionDsl, data: Mapping[str, object]) -> bool:
    """递归执行已经编译的条件 AST。"""

    if isinstance(condition, AlwaysCondition):
        return True
    if isinstance(condition, ExistenceCondition):
        value = _resolve_path(condition.path, data)
        exists = value is not _MISSING and value is not None
        return exists if condition.op is ConditionOperator.EXISTS else not exists
    if isinstance(condition, NotCondition):
        return not _evaluate(condition.arg, data)
    if isinstance(condition, LogicalCondition):
        results = (_evaluate(argument, data) for argument in condition.args)
        return all(results) if condition.op is ConditionOperator.ALL else any(results)
    left = _resolve_operand(condition.left, data)
    right = _resolve_operand(condition.right, data)
    if left is _MISSING:
        path = condition.left.path if isinstance(condition.left, ConditionReference) else None
        raise _ConditionRuntimeError(ConditionErrorCode.VALUE_MISSING, path)
    if right is _MISSING:
        path = condition.right.path if isinstance(condition.right, ConditionReference) else None
        raise _ConditionRuntimeError(ConditionErrorCode.VALUE_MISSING, path)
    try:
        return _compare(condition.op, left, right)
    except (TypeError, ValueError):
        raise _ConditionRuntimeError(ConditionErrorCode.RUNTIME_TYPE_MISMATCH) from None


def _resolve_operand(operand: ConditionOperand, data: Mapping[str, object]) -> object:
    """从只读输入解析引用值，或返回不可变字面量。"""

    if isinstance(operand, ConditionReference):
        return _resolve_path(operand.path, data)
    return operand.value


def _resolve_path(path: str, data: Mapping[str, object]) -> object:
    """只通过 Mapping 键逐段解析路径，拒绝 getattr 和数组下标。"""

    current: object = data
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _compare(operator: ConditionOperator, left: object, right: object) -> bool:
    """在不做字符串数值转换的前提下执行单个比较操作。"""

    if operator is ConditionOperator.EQ:
        return type(left) is type(right) and left == right or (
            _is_number(left) and _is_number(right) and left == right
        )
    if operator is ConditionOperator.NE:
        return not _compare(ConditionOperator.EQ, left, right)
    if operator is ConditionOperator.IN:
        if not isinstance(right, (tuple, list)):
            raise TypeError("in right operand must be an array")
        return any(_compare(ConditionOperator.EQ, left, item) for item in right)
    if not _is_number(left) or not _is_number(right):
        raise TypeError("ordered comparison requires numbers")
    if operator is ConditionOperator.GT:
        return left > right
    if operator is ConditionOperator.GTE:
        return left >= right
    if operator is ConditionOperator.LT:
        return left < right
    if operator is ConditionOperator.LTE:
        return left <= right
    raise ValueError("unsupported comparison operator")


def _is_number(value: object) -> bool:
    """识别 JSON 数值并排除 Python 中属于 int 子类的 bool。"""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


NotCondition.model_rebuild()
LogicalCondition.model_rebuild()
