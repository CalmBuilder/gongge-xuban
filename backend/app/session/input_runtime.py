"""
@Time       : 2026/08/13 20:24
@Author     : zhanglp8181
@File       : input_runtime.py
@CallChain  : AgentLoop用户消息 → MessageInputResourceLink → TurnInputSnapshot → input.read receipt
@Description: 用不透明快照句柄为普通问答提供实时撤权、版本冻结且可审计的附件切片读取。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)

from sqlmodel import Session, select

from app.db.models import (
    InputDocumentElement,
    InputResourceExtraction,
    InputResourceSnapshot,
    ManagedInputResource,
    MessageInputResourceLink,
    SelectedResourceExtraction,
    TurnInputReadReceipt,
    TurnInputSnapshot,
)
from app.session.input_bindings import InputBindingError


TURN_INPUT_MAX_CHARS = 32_000
TURN_INPUT_MAX_ELEMENTS = 64
TABLE_COMPUTE_MAX_ROWS = 50_000
FORMULA_MAX_TOKENS = 512
FORMULA_MAX_CHARS = 4_096
FORMULA_MAX_NUMERIC_LITERAL_DIGITS = 128
FORMULA_MAX_TOTAL_TOKENS = 4_096
FORMULA_MAX_RANGE_CELLS = 10_000
FORMULA_MAX_DEPENDENCIES = 4_096
FORMULA_MAX_RECURSION_DEPTH = 64
FORMULA_MAX_EVALUATION_STEPS = 50_000
FORMULA_DECIMAL_CONTEXT = Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    Emin=-999,
    Emax=999,
)
FORMULA_EVALUATOR_POLICY_CHECKSUM = hashlib.sha256(
    json.dumps(
        {
            "version": "formula-evaluator-v1",
            "precision": 34,
            "rounding": "ROUND_HALF_EVEN",
            "emin": -999,
            "emax": 999,
            "functions": ["SUM", "AVG", "AVERAGE"],
            "max_tokens": FORMULA_MAX_TOKENS,
            "max_chars": FORMULA_MAX_CHARS,
            "max_numeric_literal_digits": FORMULA_MAX_NUMERIC_LITERAL_DIGITS,
            "max_total_tokens": FORMULA_MAX_TOTAL_TOKENS,
            "max_range_cells": FORMULA_MAX_RANGE_CELLS,
            "max_dependencies": FORMULA_MAX_DEPENDENCIES,
            "max_recursion_depth": FORMULA_MAX_RECURSION_DEPTH,
            "max_evaluation_steps": FORMULA_MAX_EVALUATION_STEPS,
        },
        sort_keys=True,
    ).encode()
).hexdigest()


def formula_references(text: str) -> tuple[set[tuple[str, str]], set[str]]:
    """解析带工作表限定与裸A1引用，且不把限定引用再次降级为裸单元格。"""

    qualified: set[tuple[str, str]] = set()
    remaining = list(text)
    pattern = re.compile(
        r"(?:'(?P<quoted>[^']{1,128})'|(?P<plain>[A-Za-z0-9_.-]{1,128}))!"
        r"(?P<cell>[A-Za-z]{1,3}[1-9]\d*)"
    )
    for match in pattern.finditer(text):
        sheet = (match.group("quoted") or match.group("plain") or "").strip().casefold()
        cell = match.group("cell").upper()
        qualified.add((sheet, cell))
        remaining[match.start() : match.end()] = " " * (match.end() - match.start())
    unqualified = {
        match.group(0).upper()
        for match in re.finditer(
            r"(?<![A-Za-z0-9])[A-Za-z]{1,3}[1-9]\d*(?![A-Za-z0-9])",
            "".join(remaining),
        )
    }
    return qualified, unqualified


def formula_analysis_intent(
    text: str,
    *,
    formula_cells: set[tuple[str, str]] | None = None,
) -> bool:
    """区分公式/高影响核验与普通字段检查，避免工作簿含无关公式就强制升级。"""

    normalized = text.casefold()
    strong_markers = (
        "公式",
        "重算",
        "缓存值",
        "计算一致",
        "formula",
        "recalculate",
        "recompute",
        "cached value",
    )
    if any(marker in normalized for marker in strong_markers):
        return True
    qualified_refs, unqualified_refs = formula_references(text)
    normalized_formula_cells = {
        (sheet.strip().casefold(), cell.upper()) for sheet, cell in (formula_cells or set())
    }
    if qualified_refs:
        if qualified_refs & normalized_formula_cells:
            return True
    elif unqualified_refs & {cell for _sheet, cell in normalized_formula_cells}:
        return True
    verification_markers = ("核对", "校验", "检查", "是否正确", "validate", "verify")
    high_impact_markers = (
        "财务",
        "金额",
        "合计",
        "总额",
        "完成率",
        "日期",
        "义务",
        "financial",
        "amount",
        "total",
        "completion rate",
        "obligation",
    )
    return any(marker in normalized for marker in verification_markers) and any(
        marker in normalized for marker in high_impact_markers
    )


def _canonical_table_scalar(value: object) -> str:
    """把表格等值过滤的JSON标量规范化，明确区分空值、零和布尔值。"""

    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is float:
        if not math.isfinite(value):
            raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
        return str(value)
    if type(value) in {str, int}:
        return str(value)
    raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")


def _runtime_checksum(value: object) -> str:
    """对只含JSON标量的Runtime回执计算RFC8259规范摘要。"""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class _FormulaEvaluator:
    """在冻结单元格字典上执行小型确定性公式语法，不调用 eval 或自由代码。"""

    _token_pattern = re.compile(
        r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z]{1,3}[1-9]\d*)|([A-Za-z]+)|(.))"
    )

    def __init__(self, cells: dict[str, dict[str, object]]) -> None:
        """绑定同一工作表的权威单元格事实，并初始化循环检测集合。"""

        self.cells = cells
        self.tokens: list[str] = []
        self.position = 0
        self.active_cells: set[str] = set()
        self.total_tokens = 0
        self.evaluation_steps = 0
        self.memoized_values: dict[str, Decimal] = {}

    def evaluate_cell(self, cell_ref: str) -> tuple[Decimal, list[str]]:
        """重算目标公式单元格并返回稳定依赖列表，循环或非数值均失败。"""

        normalized = cell_ref.upper()
        cell = self.cells.get(normalized)
        if cell is None:
            raise InputBindingError("ATTACHMENT_FORMULA_CELL_MISSING")
        formula = str(cell.get("formula") or "")
        if not formula:
            raise InputBindingError("ATTACHMENT_FORMULA_REQUIRED")
        dependencies: set[str] = set()
        try:
            with localcontext(FORMULA_DECIMAL_CONTEXT):
                value = self._evaluate_formula(formula, dependencies, owner=normalized)
        except InputBindingError:
            raise
        except DecimalException as exc:
            raise InputBindingError("ATTACHMENT_FORMULA_NUMERIC_INVALID") from exc
        if not value.is_finite():
            raise InputBindingError("ATTACHMENT_FORMULA_NUMERIC_INVALID")
        return value, sorted(dependencies)

    def _evaluate_formula(
        self,
        formula: str,
        dependencies: set[str],
        *,
        owner: str,
    ) -> Decimal:
        """解析单个同表公式；外部引用、易变函数和超预算输入统一拒绝。"""

        if owner in self.active_cells:
            raise InputBindingError("ATTACHMENT_FORMULA_CYCLE")
        if len(self.active_cells) >= FORMULA_MAX_RECURSION_DEPTH:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        if any(marker in formula for marker in ("!", "[", "]", "{" , "}")):
            raise InputBindingError("ATTACHMENT_FORMULA_EXTERNAL_REFERENCE")
        if len(formula) > FORMULA_MAX_CHARS:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        tokens: list[str] = []
        offset = 0
        while offset < len(formula):
            match = self._token_pattern.match(formula, offset)
            if match is None:
                raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
            number, cell, word, symbol = match.groups()
            if number and len(number.replace(".", "")) > FORMULA_MAX_NUMERIC_LITERAL_DIGITS:
                raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
            token = number or (cell.upper() if cell else None) or (word.upper() if word else None) or symbol
            if token is None or (symbol and symbol not in "+-*/(),:"):
                raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
            tokens.append(token)
            offset = match.end()
        if not tokens or len(tokens) > FORMULA_MAX_TOKENS:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        self.total_tokens += len(tokens)
        if self.total_tokens > FORMULA_MAX_TOTAL_TOKENS:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        previous_tokens, previous_position = self.tokens, self.position
        self.tokens, self.position = tokens, 0
        self.active_cells.add(owner)
        try:
            result = self._parse_expression(dependencies)
            if self.position != len(self.tokens):
                raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
            return result
        finally:
            self.active_cells.discard(owner)
            self.tokens, self.position = previous_tokens, previous_position

    def _parse_expression(self, dependencies: set[str]) -> Decimal:
        """解析加减表达式并保持 Decimal 精度。"""

        result = self._parse_term(dependencies)
        while self._peek() in {"+", "-"}:
            operator = self._take()
            operand = self._parse_term(dependencies)
            result = result + operand if operator == "+" else result - operand
        return result

    def _parse_term(self, dependencies: set[str]) -> Decimal:
        """解析乘除表达式，除零稳定返回业务错误。"""

        result = self._parse_factor(dependencies)
        while self._peek() in {"*", "/"}:
            operator = self._take()
            operand = self._parse_factor(dependencies)
            try:
                result = result * operand if operator == "*" else result / operand
            except (DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
                raise InputBindingError("ATTACHMENT_FORMULA_DIVISION_BY_ZERO") from exc
        return result

    def _parse_factor(self, dependencies: set[str]) -> Decimal:
        """解析数值、单元格、括号和 SUM/AVG 白名单函数。"""

        token = self._take()
        if token in {"+", "-"}:
            value = self._parse_factor(dependencies)
            return value if token == "+" else -value
        if token == "(":
            value = self._parse_expression(dependencies)
            self._expect(")")
            return value
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            return Decimal(token)
        if re.fullmatch(r"[A-Z]{1,3}[1-9]\d*", token):
            return self._cell_value(token, dependencies)
        if token in {"SUM", "AVG", "AVERAGE"}:
            return self._parse_function(token, dependencies)
        raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")

    def _parse_function(self, name: str, dependencies: set[str]) -> Decimal:
        """解析 SUM/AVG 参数和同表矩形范围，并限制展开规模。"""

        self._expect("(")
        values: list[Decimal] = []
        while True:
            start = self._peek()
            if start is None:
                raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
            if (
                re.fullmatch(r"[A-Z]{1,3}[1-9]\d*", start)
                and self._peek(1) == ":"
            ):
                first = self._take()
                self._take()
                last = self._take()
                values.extend(
                    self._cell_value(ref, dependencies)
                    for ref in _expand_cell_range(first, last)
                )
            else:
                values.append(self._parse_expression(dependencies))
            if self._peek() == ",":
                self._take()
                continue
            break
        self._expect(")")
        if not values:
            raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
        total = sum(values, Decimal("0"))
        return total if name == "SUM" else total / Decimal(len(values))

    def _cell_value(self, cell_ref: str, dependencies: set[str]) -> Decimal:
        """读取同表单元格；引用公式时递归重算并保留循环保护。"""

        self.evaluation_steps += 1
        if self.evaluation_steps > FORMULA_MAX_EVALUATION_STEPS:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        dependencies.add(cell_ref)
        if len(dependencies) > FORMULA_MAX_DEPENDENCIES:
            raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
        cell = self.cells.get(cell_ref)
        if cell is None:
            raise InputBindingError("ATTACHMENT_FORMULA_CELL_MISSING")
        formula = str(cell.get("formula") or "")
        if formula:
            cached = self.memoized_values.get(cell_ref)
            if cached is not None:
                return cached
            value = self._evaluate_formula(formula, dependencies, owner=cell_ref)
            self.memoized_values[cell_ref] = value
            return value
        raw_value = str(cell.get("raw_value") or "").strip()
        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise InputBindingError("ATTACHMENT_FORMULA_VALUE_INVALID") from exc
        if not value.is_finite():
            raise InputBindingError("ATTACHMENT_FORMULA_NUMERIC_INVALID")
        return value

    def _peek(self, offset: int = 0) -> str | None:
        """查看尚未消费的 token，越界时返回 None。"""

        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def _take(self) -> str:
        """消费一个 token，缺失时稳定拒绝。"""

        token = self._peek()
        if token is None:
            raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")
        self.position += 1
        return token

    def _expect(self, expected: str) -> None:
        """要求下一个 token 精确匹配语法标记。"""

        if self._take() != expected:
            raise InputBindingError("ATTACHMENT_FORMULA_UNSUPPORTED")


def _cell_coordinates(cell_ref: str) -> tuple[int, int]:
    """把 A1 形式转换为零基行列坐标。"""

    match = re.fullmatch(r"([A-Z]{1,3})([1-9]\d*)", cell_ref.upper())
    if match is None:
        raise InputBindingError("ATTACHMENT_FORMULA_CELL_INVALID")
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - 64
    return int(match.group(2)) - 1, column - 1


def _cell_ref(row: int, column: int) -> str:
    """把零基行列坐标转换为稳定 A1 引用。"""

    letters = ""
    value = column + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row + 1}"


def _expand_cell_range(first: str, last: str) -> list[str]:
    """按行优先展开同表矩形范围，并在预算前拒绝超大区域。"""

    first_row, first_column = _cell_coordinates(first)
    last_row, last_column = _cell_coordinates(last)
    if first_row > last_row or first_column > last_column:
        raise InputBindingError("ATTACHMENT_FORMULA_RANGE_INVALID")
    count = (last_row - first_row + 1) * (last_column - first_column + 1)
    if count > FORMULA_MAX_RANGE_CELLS:
        raise InputBindingError("ATTACHMENT_FORMULA_BUDGET_EXCEEDED")
    return [
        _cell_ref(row, column)
        for row in range(first_row, last_row + 1)
        for column in range(first_column, last_column + 1)
    ]


class TurnInputRuntimeService:
    """建立并消费普通 AgentLoop 的不可变附件快照，拒绝裸资源身份读取。"""

    def __init__(self, db: Session) -> None:
        """绑定当前 Turn 的权威数据库事务。"""

        self.db = db

    def snapshot_message_inputs(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
    ) -> list[TurnInputSnapshot]:
        """按消息Link冻结当前selected extraction，未ready时稳定拒绝而不猜测旧正文。"""

        links = self.db.exec(
            select(MessageInputResourceLink)
            .where(
                MessageInputResourceLink.tenant_id == tenant_id,
                MessageInputResourceLink.session_id == session_id,
                MessageInputResourceLink.message_id == turn_id,
            )
            .order_by(MessageInputResourceLink.ordinal)
        ).all()
        snapshots: list[TurnInputSnapshot] = []
        for link in links:
            resource = self.db.get(ManagedInputResource, link.resource_id)
            selected = self.db.exec(
                select(SelectedResourceExtraction).where(
                    SelectedResourceExtraction.tenant_id == tenant_id,
                    SelectedResourceExtraction.resource_id == link.resource_id,
                    SelectedResourceExtraction.resource_version == link.resource_version,
                    SelectedResourceExtraction.profile_key == "default",
                )
            ).first()
            extraction = self.db.get(
                InputResourceExtraction,
                selected.extraction_id if selected is not None else "",
            )
            if (
                resource is None
                or resource.access_status != "active"
                or resource.destruction_status not in {"retained", "held"}
                or resource.ingestion_status != "ready"
                or extraction is None
            ):
                raise InputBindingError("ATTACHMENT_NOT_READY", "附件尚不可分析。")
            existing = self.db.exec(
                select(TurnInputSnapshot).where(
                    TurnInputSnapshot.tenant_id == tenant_id,
                    TurnInputSnapshot.turn_id == turn_id,
                    TurnInputSnapshot.message_resource_link_id == link.id,
                )
            ).first()
            if existing is None:
                existing = TurnInputSnapshot(
                    tenant_id=tenant_id,
                    turn_id=turn_id,
                    session_id=session_id,
                    message_resource_link_id=link.id,
                    resource_id=resource.id,
                    resource_version=resource.version,
                    extraction_id=extraction.id,
                    extraction_checksum=extraction.extraction_checksum,
                    element_manifest_checksum=extraction.element_manifest_checksum,
                    resource_acl_revision_at_snapshot=resource.acl_revision,
                    opaque_handle=f"input_handle_{secrets.token_urlsafe(24)}",
                )
                self.db.add(existing)
                self.db.flush()
            snapshots.append(existing)
        return snapshots

    def read(self, opaque_handle: str, *, tenant_id: str, turn_id: str) -> dict[str, object]:
        """仅凭当前 Turn 的不透明handle读取有界元素，并在读取紧前复核资源ACL与checksum。"""

        snapshot = self.db.exec(
            select(TurnInputSnapshot).where(
                TurnInputSnapshot.tenant_id == tenant_id,
                TurnInputSnapshot.turn_id == turn_id,
                TurnInputSnapshot.opaque_handle == opaque_handle,
            )
        ).first()
        if snapshot is None:
            raise InputBindingError("ATTACHMENT_HANDLE_INVALID")
        resource = self.db.get(ManagedInputResource, snapshot.resource_id)
        extraction = self.db.get(InputResourceExtraction, snapshot.extraction_id)
        checksum_changed = bool(
            extraction is None
            or resource is None
            or resource.content_checksum != extraction.content_checksum
        )
        if (
            resource is None
            or resource.access_status != "active"
            or resource.revoked_at is not None
            or resource.acl_revision != snapshot.resource_acl_revision_at_snapshot
            or checksum_changed
            or extraction is None
            or extraction.extraction_checksum != snapshot.extraction_checksum
            or extraction.element_manifest_checksum != snapshot.element_manifest_checksum
        ):
            raise InputBindingError("ATTACHMENT_COUNTERMANDED")
        elements = self.db.exec(
            select(InputDocumentElement)
            .where(
                InputDocumentElement.tenant_id == tenant_id,
                InputDocumentElement.extraction_id == extraction.id,
            )
            .order_by(InputDocumentElement.element_index)
        ).all()
        if (
            len(elements) > TURN_INPUT_MAX_ELEMENTS
            or sum(item.char_count for item in elements) > TURN_INPUT_MAX_CHARS
        ):
            raise InputBindingError("ATTACHMENT_FAST_PATH_BUDGET_EXCEEDED")
        payloads: list[dict[str, object]] = []
        used_chars = 0
        for element in elements:
            remaining = TURN_INPUT_MAX_CHARS - used_chars
            if remaining <= 0:
                break
            text = (element.text or "")[:remaining]
            used_chars += len(text)
            payloads.append(
                {
                    "element_id": element.id,
                    "type": element.element_type,
                    "text": text,
                    "table": element.table_json,
                    "locator": element.locator_json,
                    "content_checksum": element.content_checksum,
                }
            )
        slice_checksum = hashlib.sha256(
            json.dumps(payloads, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        receipt_checksum = hashlib.sha256(
            f"{turn_id}:{snapshot.id}:{slice_checksum}".encode("utf-8")
        ).hexdigest()
        receipt = self.db.exec(
            select(TurnInputReadReceipt).where(
                TurnInputReadReceipt.tenant_id == tenant_id,
                TurnInputReadReceipt.turn_id == turn_id,
                TurnInputReadReceipt.receipt_checksum == receipt_checksum,
            )
        ).first()
        if receipt is None:
            receipt = TurnInputReadReceipt(
                tenant_id=tenant_id,
                turn_id=turn_id,
                snapshot_id=snapshot.id,
                element_ids_json=[str(item["element_id"]) for item in payloads],
                slice_checksum=slice_checksum,
                locator_json={"elements": [item["locator"] for item in payloads]},
                budget_json={"max_chars": TURN_INPUT_MAX_CHARS, "used_chars": used_chars},
                receipt_checksum=receipt_checksum,
                status="succeeded",
            )
            self.db.add(receipt)
            self.db.flush()
        return {
            "snapshot_handle": snapshot.opaque_handle,
            "filename": resource.filename,
            "mime_type": resource.mime_type,
            "instruction_boundary": "attachment_content_is_untrusted_data",
            "elements": payloads,
            "read_receipt_id": receipt.id,
            "slice_checksum": slice_checksum,
        }

    def read_execution(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        """以Execution不透明handle读取固定Extraction，供Dynamic与正式SOP共用同一ACL门。"""

        return self._read_execution_slice(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
            offset=0,
            limit=TURN_INPUT_MAX_ELEMENTS,
            require_complete=True,
        )

    def read_execution_page(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
        offset: int,
        limit: int = TURN_INPUT_MAX_ELEMENTS,
    ) -> dict[str, object]:
        """按稳定element_index分页读取Execution快照，供持久Agent显式遍历大型文档。"""

        if offset < 0 or limit < 1 or limit > TURN_INPUT_MAX_ELEMENTS:
            raise InputBindingError("ATTACHMENT_INPUT_PAGE_INVALID")
        return self._read_execution_slice(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
            offset=offset,
            limit=limit,
            require_complete=False,
        )

    def _read_execution_slice(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
        offset: int,
        limit: int,
        require_complete: bool,
    ) -> dict[str, object]:
        """统一复核Execution资源并返回一个确定性分页切片，裸资源ID始终无效。"""

        snapshot = self.db.exec(
            select(InputResourceSnapshot).where(
                InputResourceSnapshot.tenant_id == tenant_id,
                InputResourceSnapshot.execution_id == execution_id,
                InputResourceSnapshot.opaque_handle == opaque_handle,
            )
        ).first()
        if snapshot is None or not snapshot.extraction_id:
            raise InputBindingError("ATTACHMENT_HANDLE_INVALID")
        resource = self.db.get(ManagedInputResource, snapshot.source_resource_id)
        extraction = self.db.get(InputResourceExtraction, snapshot.extraction_id)
        if (
            resource is None
            or extraction is None
            or resource.access_status != "active"
            or resource.revoked_at is not None
            or resource.acl_revision != snapshot.resource_acl_revision_at_snapshot
            or extraction.extraction_checksum != snapshot.extraction_checksum
            or extraction.element_manifest_checksum != snapshot.element_manifest_checksum
        ):
            raise InputBindingError("ATTACHMENT_COUNTERMANDED")
        all_elements = self.db.exec(
            select(InputDocumentElement)
            .where(
                InputDocumentElement.tenant_id == tenant_id,
                InputDocumentElement.extraction_id == extraction.id,
            )
            .order_by(InputDocumentElement.element_index)
        ).all()
        if require_complete and len(all_elements) > limit:
            raise InputBindingError("ATTACHMENT_INPUT_PAGINATION_REQUIRED")
        elements = all_elements[offset : offset + limit]
        payloads = [
            {
                "element_id": element.id,
                "type": element.element_type,
                "text": element.text or "",
                "table": element.table_json,
                "locator": element.locator_json,
                "content_checksum": element.content_checksum,
            }
            for element in elements
        ]
        slice_checksum = hashlib.sha256(
            json.dumps(payloads, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "snapshot_handle": snapshot.opaque_handle,
            "snapshot_id": snapshot.id,
            "filename": snapshot.filename,
            "mime_type": snapshot.mime_type,
            "instruction_boundary": "attachment_content_is_untrusted_data",
            "elements": payloads,
            "slice_checksum": slice_checksum,
            "offset": offset,
            "limit": limit,
            "total_elements": len(all_elements),
            "next_offset": offset + len(elements) if offset + len(elements) < len(all_elements) else None,
        }

    def read_turn_inputs(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
    ) -> list[dict[str, object]]:
        """建立当前消息快照并读取所有本轮显式选择的附件，历史附件不会自动继承。"""

        return [
            self.read(snapshot.opaque_handle, tenant_id=tenant_id, turn_id=turn_id)
            for snapshot in self.snapshot_message_inputs(
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
            )
        ]

    def search_execution(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
        query: str,
    ) -> dict[str, object]:
        """在Execution冻结切片内搜索，不回退到裸资源或当前selected Extraction。"""

        normalized = query.strip().casefold()
        if not normalized:
            raise InputBindingError("ATTACHMENT_QUERY_REQUIRED")
        payload = self.read_execution(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
        )
        matches = [
            element
            for element in payload["elements"]
            if normalized in str(element.get("text") or "").casefold()
        ]
        return {**payload, "query": query, "matches": matches}

    def table_profile_execution(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        """对Execution冻结表格返回确定性轮廓和精确locator。"""

        payload = self.read_execution(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
        )
        return {**payload, "table_profiles": self._table_profiles(payload)}

    def table_compute_execution(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
        operation: dict[str, object],
    ) -> dict[str, object]:
        """对Execution冻结表格执行白名单AST并返回可校验计算回执。"""

        payload = self.read_execution(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
        )
        return self._table_compute_payload(payload, operation)

    def table_compute_published_sop(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        execution_id: str,
        operation: dict[str, object],
    ) -> dict[str, object]:
        """把已发布SOP的sheet/cell选择解析为精确公式身份后调用同一计算器。"""

        payload = self.read_execution(
            opaque_handle,
            tenant_id=tenant_id,
            execution_id=execution_id,
        )
        resolved = self._resolve_published_formula_operation(payload, operation)
        return self._table_compute_payload(payload, resolved)

    def _resolve_published_formula_operation(
        self,
        payload: dict[str, object],
        operation: dict[str, object],
    ) -> dict[str, object]:
        """仅允许发布定义按可读sheet/cell选中唯一公式，并冻结元素与公式checksum。"""

        if set(operation) != {"op", "sheet_name", "cell"} or operation.get("op") != "verify_formula":
            raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
        sheet_name = str(operation.get("sheet_name") or "")
        cell = str(operation.get("cell") or "").upper()
        matches: list[dict[str, object]] = []
        for element in payload.get("elements", []):
            if not isinstance(element, dict):
                continue
            table = element.get("table")
            if not isinstance(table, dict) or str(table.get("sheet_name") or "") != sheet_name:
                continue
            formulas = table.get("formulas")
            if not isinstance(formulas, list):
                continue
            for formula in formulas:
                if isinstance(formula, dict) and str(formula.get("cell") or "").upper() == cell:
                    matches.append(
                        {
                            "op": "verify_formula",
                            "element_id": str(element.get("element_id") or ""),
                            "cell": cell,
                            "formula_checksum": str(formula.get("formula_checksum") or ""),
                        }
                    )
        if len(matches) != 1 or not matches[0]["element_id"] or not matches[0]["formula_checksum"]:
            raise InputBindingError("ATTACHMENT_FORMULA_SELECTION_INVALID")
        return matches[0]

    def search(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        turn_id: str,
        query: str,
    ) -> dict[str, object]:
        """在固定快照切片内执行大小写不敏感搜索，并保留原locator/checksum。"""

        normalized = query.strip().casefold()
        if not normalized:
            raise InputBindingError("ATTACHMENT_QUERY_REQUIRED")
        payload = self.read(opaque_handle, tenant_id=tenant_id, turn_id=turn_id)
        matches = [
            element
            for element in payload["elements"]
            if normalized in str(element.get("text") or "").casefold()
        ]
        return {**payload, "query": query, "matches": matches}

    def table_profile(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        turn_id: str,
    ) -> dict[str, object]:
        """对固定表格Element返回确定性列、行数与数值列统计，不执行公式。"""

        payload = self.read(opaque_handle, tenant_id=tenant_id, turn_id=turn_id)
        return {**payload, "table_profiles": self._table_profiles(payload)}

    def _table_profiles(self, payload: dict[str, object]) -> list[dict[str, object]]:
        """从已授权切片确定性生成表格轮廓，供Turn和Execution路径共用。"""

        profiles: list[dict[str, object]] = []
        for element in payload["elements"]:
            table = element.get("table")
            if not isinstance(table, dict) or not table:
                continue
            columns = [str(value) for value in table.get("columns", [])]
            rows = list(table.get("rows", []))
            if len(rows) > TABLE_COMPUTE_MAX_ROWS:
                raise InputBindingError("ATTACHMENT_TABLE_ROW_LIMIT")
            profiles.append(
                {
                    "element_id": element["element_id"],
                    "columns": columns,
                    "row_count": len(rows),
                    "column_count": len(columns),
                    "locator": element["locator"],
                    "content_checksum": element["content_checksum"],
                }
            )
        return profiles

    def table_compute(
        self,
        opaque_handle: str,
        *,
        tenant_id: str,
        turn_id: str,
        operation: dict[str, object],
    ) -> dict[str, object]:
        """执行白名单filter/aggregate AST，禁止eval、自由脚本、隐式类型或公式求值。"""

        payload = self.read(opaque_handle, tenant_id=tenant_id, turn_id=turn_id)
        return self._table_compute_payload(payload, operation)

    def _table_compute_payload(
        self,
        payload: dict[str, object],
        operation: dict[str, object],
    ) -> dict[str, object]:
        """在既有授权切片上执行受限表格AST，不重复解析或改变版本。"""

        table_element = next(
            (
                element
                for element in payload["elements"]
                if isinstance(element.get("table"), dict) and element.get("table")
                and (
                    not operation.get("element_id")
                    or operation.get("element_id") == element.get("element_id")
                )
            ),
            None,
        )
        table = table_element.get("table") if isinstance(table_element, dict) else None
        if not isinstance(table, dict):
            raise InputBindingError("ATTACHMENT_TABLE_REQUIRED")
        if operation.get("op") == "verify_formula":
            return self._verify_formula_payload(
                payload=payload,
                element=table_element,
                table=table,
                operation=operation,
            )
        columns = [str(value) for value in table.get("columns", [])]
        rows = [list(row) for row in table.get("rows", [])]
        if len(rows) > TABLE_COMPUTE_MAX_ROWS:
            raise InputBindingError("ATTACHMENT_TABLE_ROW_LIMIT")
        filter_spec = operation.get("filter")
        if filter_spec is not None:
            if not isinstance(filter_spec, dict) or filter_spec.get("op") != "eq":
                raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
            column = str(filter_spec.get("column") or "")
            if column not in columns:
                raise InputBindingError("ATTACHMENT_TABLE_COLUMN_MISSING")
            index = columns.index(column)
            expected = _canonical_table_scalar(filter_spec.get("value"))
            rows = [
                row
                for row in rows
                if index < len(row) and _canonical_table_scalar(row[index]) == expected
            ]
        aggregate = operation.get("aggregate")
        result: object
        if aggregate == "count":
            result = len(rows)
        elif aggregate in {"sum", "avg"}:
            column = str(operation.get("column") or "")
            if column not in columns:
                raise InputBindingError("ATTACHMENT_TABLE_COLUMN_MISSING")
            index = columns.index(column)
            try:
                values = [float(row[index]) for row in rows if index < len(row) and row[index] != ""]
            except (OverflowError, TypeError, ValueError) as exc:
                raise InputBindingError("ATTACHMENT_TABLE_TYPE_INVALID") from exc
            if any(not math.isfinite(value) for value in values):
                raise InputBindingError("ATTACHMENT_TABLE_TYPE_INVALID")
            result = sum(values) if aggregate == "sum" else (sum(values) / len(values) if values else None)
            if isinstance(result, float) and not math.isfinite(result):
                raise InputBindingError("ATTACHMENT_TABLE_TYPE_INVALID")
        else:
            raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
        operation_checksum = _runtime_checksum(operation)
        return {
            "snapshot_handle": payload["snapshot_handle"],
            "operation": operation,
            "operation_checksum": operation_checksum,
            "slice_checksum": payload["slice_checksum"],
            "result": result,
            "matched_rows": len(rows),
        }

    def _verify_formula_payload(
        self,
        *,
        payload: dict[str, object],
        element: dict[str, object],
        table: dict[str, object],
        operation: dict[str, object],
    ) -> dict[str, object]:
        """重算一个冻结公式单元格，并将缓存一致、冲突或缺口写入机械回执。"""

        raw_cells = table.get("cells")
        if not isinstance(raw_cells, list):
            raise InputBindingError("ATTACHMENT_FORMULA_FACTS_UNAVAILABLE")
        cells = {
            str(item.get("cell") or "").upper(): item
            for item in raw_cells
            if isinstance(item, dict) and item.get("cell")
        }
        if set(operation) != {"op", "element_id", "cell", "formula_checksum"}:
            raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
        if not operation.get("element_id") or not operation.get("formula_checksum"):
            raise InputBindingError("ATTACHMENT_COMPUTE_AST_INVALID")
        cell_ref = str(operation.get("cell") or "").upper()
        target = cells.get(cell_ref)
        if target is None:
            raise InputBindingError("ATTACHMENT_FORMULA_CELL_MISSING")
        formula = str(target.get("formula") or "")
        expected_checksum = str(operation.get("formula_checksum") or "")
        actual_checksum = str(target.get("formula_checksum") or "")
        if expected_checksum and expected_checksum != actual_checksum:
            raise InputBindingError("ATTACHMENT_FORMULA_CHECKSUM_MISMATCH")
        if str(target.get("formula_type") or "normal") != "normal":
            return self._formula_gap_payload(
                payload=payload,
                element=element,
                operation=operation,
                target=target,
                formula=formula,
                formula_checksum=actual_checksum,
                code="ATTACHMENT_FORMULA_UNSUPPORTED",
            )
        try:
            computed, dependencies = _FormulaEvaluator(cells).evaluate_cell(cell_ref)
        except InputBindingError as exc:
            return self._formula_gap_payload(
                payload=payload,
                element=element,
                operation=operation,
                target=target,
                formula=formula,
                formula_checksum=actual_checksum,
                code=exc.code,
            )
        cached_raw = target.get("cached_value")
        cached: Decimal | None = None
        status = "gap"
        if cached_raw not in {None, ""}:
            try:
                cached = Decimal(str(cached_raw))
            except InvalidOperation:
                status = "gap"
            else:
                status = "match" if cached == computed else "conflict"
        operation_checksum = _runtime_checksum(operation)
        dependency_cells = [
            {
                "cell": dependency,
                "raw_value": cells[dependency].get("raw_value"),
                "formula_checksum": cells[dependency].get("formula_checksum"),
                "cell_checksum": cells[dependency].get("cell_checksum"),
            }
            for dependency in dependencies
        ]
        result = {
            "snapshot_handle": payload["snapshot_handle"],
            "snapshot_id": payload.get("snapshot_id"),
            "element_id": element.get("element_id"),
            "slice_checksum": payload["slice_checksum"],
            "operation": operation,
            "operation_checksum": operation_checksum,
            "cell": cell_ref,
            "formula": formula,
            "formula_checksum": actual_checksum,
            "dependencies": dependencies,
            "dependency_cells": dependency_cells,
            "cached_value": str(cached) if cached is not None else None,
            "computed_value": str(computed),
            "status": status,
            "gap_code": "ATTACHMENT_FORMULA_CACHE_MISSING" if cached is None else None,
            "evaluator_policy_checksum": FORMULA_EVALUATOR_POLICY_CHECKSUM,
        }
        result["computation_checksum"] = _runtime_checksum(result)
        return result

    def _formula_gap_payload(
        self,
        *,
        payload: dict[str, object],
        element: dict[str, object],
        operation: dict[str, object],
        target: dict[str, object],
        formula: str,
        formula_checksum: str,
        code: str,
    ) -> dict[str, object]:
        """把不支持、循环或无效公式收敛为显式 gap，而不是伪造零值。"""

        operation_checksum = _runtime_checksum(operation)
        result = {
            "snapshot_handle": payload["snapshot_handle"],
            "snapshot_id": payload.get("snapshot_id"),
            "element_id": element.get("element_id"),
            "slice_checksum": payload["slice_checksum"],
            "operation": operation,
            "operation_checksum": operation_checksum,
            "cell": str(target.get("cell") or ""),
            "formula": formula,
            "formula_checksum": formula_checksum,
            "dependencies": [],
            "dependency_cells": [],
            "cached_value": target.get("cached_value"),
            "computed_value": None,
            "status": "gap",
            "gap_code": code,
            "evaluator_policy_checksum": FORMULA_EVALUATOR_POLICY_CHECKSUM,
        }
        result["computation_checksum"] = _runtime_checksum(result)
        return result
