"""
@Time       : 2026/08/13 20:29
@Author     : zhanglp8181
@File       : test_turn_input_runtime.py
@CallChain  : pytest → TurnInputRuntimeService → Snapshot/Element/ReadReceipt
@Description: 验证普通AgentLoop附件句柄的版本冻结、实时撤权、跨Turn隔离和读取审计。
"""

from __future__ import annotations

import hashlib
from decimal import getcontext

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.agent_loop import AgentLoop
from app.db.models import (
    InputDocumentElement,
    InputResourceExtraction,
    ManagedInputResource,
    MessageInputResourceLink,
    ResourceSessionBinding,
    SelectedResourceExtraction,
    TurnInputReadReceipt,
)
from app.session.input_bindings import InputBindingError
from app.session.input_runtime import (
    FORMULA_MAX_CHARS,
    TurnInputRuntimeService,
    _FormulaEvaluator,
    _canonical_table_scalar,
    formula_analysis_intent,
)
from app.session.session_schema import ChatAttachmentRef, ChatTurnRequest


def _session() -> Session:
    """创建覆盖附件领域表的隔离SQLite会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> tuple[ManagedInputResource, InputResourceExtraction]:
    """建立一份ready CSV、会话Link及不可变Extraction。"""

    checksum = hashlib.sha256(b"region,amount\nEast,100").hexdigest()
    resource = ManagedInputResource(
        id="input-sales",
        tenant_id="tenant-a",
        owner_user_id="user-a",
        agent_id="agent-a",
        version=checksum,
        filename="sales.csv",
        mime_type="text/csv",
        size_bytes=22,
        content_checksum=checksum,
        extraction_checksum="extract-v1",
        ingestion_status="ready",
        storage_locator="tenant-a/input-sales/blob",
        security_status="format_verified",
    )
    extraction = InputResourceExtraction(
        id="extract-v1",
        tenant_id="tenant-a",
        resource_id=resource.id,
        resource_version=resource.version,
        content_checksum=checksum,
        parser_name="builtin-csv",
        parser_version="1.0.0",
        parser_config_checksum="config-v1",
        extraction_checksum="extract-v1",
        element_manifest_checksum="manifest-v1",
        published_from_attempt_id="attempt-v1",
        element_count=1,
    )
    db.add(resource)
    db.add(extraction)
    db.add(
        SelectedResourceExtraction(
            tenant_id="tenant-a",
            resource_id=resource.id,
            resource_version=resource.version,
            profile_key="default",
            extraction_id=extraction.id,
        )
    )
    db.add(
        InputDocumentElement(
            id="element-v1",
            tenant_id="tenant-a",
            extraction_id=extraction.id,
            element_index=0,
            element_type="table",
            text="region,amount\nEast,100",
            table_json={"columns": ["region", "amount"], "rows": [["East", "100"]]},
            locator_json={"kind": "csv", "row_start": 1, "row_end": 2},
            content_checksum="element-checksum",
        )
    )
    binding = ResourceSessionBinding(
        id="binding-v1",
        tenant_id="tenant-a",
        resource_id=resource.id,
        resource_version=resource.version,
        owner_user_id="user-a",
        session_id="session-a",
        agent_id="agent-a",
    )
    db.add(binding)
    db.add(
        MessageInputResourceLink(
            id="link-v1",
            tenant_id="tenant-a",
            session_id="session-a",
            message_id="turn-a",
            resource_binding_id=binding.id,
            resource_id=resource.id,
            resource_version=resource.version,
            content_checksum=checksum,
        )
    )
    db.commit()
    return resource, extraction


def test_turn_read_uses_opaque_snapshot_and_records_receipt() -> None:
    """正向证明当前Turn显式Link产生handle、locator和唯一读取回执。"""

    with _session() as db:
        _seed(db)
        runtime = TurnInputRuntimeService(db)
        values = runtime.read_turn_inputs(
            tenant_id="tenant-a",
            session_id="session-a",
            turn_id="turn-a",
        )
        db.commit()
        receipts = db.exec(select(TurnInputReadReceipt)).all()

    assert values[0]["snapshot_handle"].startswith("input_handle_")
    assert values[0]["instruction_boundary"] == "attachment_content_is_untrusted_data"
    assert values[0]["elements"][0]["locator"] == {
        "kind": "csv",
        "row_start": 1,
        "row_end": 2,
    }
    assert len(receipts) == 1
    assert receipts[0].status == "succeeded"


def test_explicit_formula_question_routes_small_xlsx_to_dynamic_runtime() -> None:
    """小型XLSX若明确要求公式重算也必须升级Dynamic，不能在快路径只读缓存值。"""

    with _session() as db:
        resource, _extraction = _seed(db)
        resource.filename = "sales.xlsx"
        resource.mime_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        element = db.exec(select(InputDocumentElement)).one()
        element.table_json = {
            **element.table_json,
            "formulas": [
                {
                    "cell": "D2",
                    "formula": "B2/C2",
                    "formula_checksum": hashlib.sha256(b"B2/C2").hexdigest(),
                }
            ],
        }
        db.add(resource)
        db.add(element)
        db.commit()
        request = ChatTurnRequest(
            tenant_id="tenant-a",
            session_id="session-a",
            user_id="user-a",
            message="请核对销售总额是否正确",
            attachments=[
                ChatAttachmentRef(
                    resource_id=resource.id,
                    resource_version=resource.version,
                )
            ],
        )
        unrelated = request.model_copy(update={"message": "请检查客户名称拼写"})
        direct_cell = request.model_copy(update={"message": "D2=B2/C2 是否正确"})
        product_code = request.model_copy(update={"message": "请检查产品 A100 名称"})
        certification = request.model_copy(update={"message": "请检查 ISO9001 认证状态"})

        assert AgentLoop(db)._attachments_require_dynamic(request) is True
        assert AgentLoop(db)._attachments_require_dynamic(direct_cell) is True
        assert AgentLoop(db)._attachments_require_dynamic(unrelated) is False
        assert AgentLoop(db)._attachments_require_dynamic(product_code) is False
        assert AgentLoop(db)._attachments_require_dynamic(certification) is False


def test_formula_intent_requires_real_formula_cell_for_a1_like_tokens() -> None:
    """A100/ISO9001等业务编号不得触发公式核验，真实公式D2仍可精确升级。"""

    cells = {("Summary", "D2")}
    assert formula_analysis_intent("D2=B2/C2 是否正确", formula_cells=cells) is True
    assert formula_analysis_intent("Summary!D2 是否正确", formula_cells=cells) is True
    assert formula_analysis_intent("Sheet1!D2 是否正确", formula_cells=cells) is False
    assert formula_analysis_intent("请检查产品 A100 名称", formula_cells=cells) is False
    assert formula_analysis_intent("请检查 ISO9001 认证状态", formula_cells=cells) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (None, ""),
        (0, "0"),
        (False, "false"),
        (True, "true"),
        (1, "1"),
        (1.0, "1.0"),
    ],
)
def test_table_filter_scalar_canonicalization_distinguishes_zero_false_and_empty(
    value: object,
    expected: str,
) -> None:
    """发布允许的JSON标量在Runtime中必须有唯一文本语义，零和False不能折叠为空。"""

    assert _canonical_table_scalar(value) == expected


def test_table_compute_eq_filter_keeps_zero_false_and_empty_distinct() -> None:
    """真实table.compute过滤必须分别命中0、False和空值，禁止通过truthiness静默混淆。"""

    with _session() as db:
        _seed(db)
        element = db.exec(select(InputDocumentElement)).one()
        element.table_json = {
            "columns": ["value"],
            "rows": [[0], [False], [None], [""], [True], [1], [1.0]],
        }
        db.add(element)
        db.commit()
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]

        def count(value: object) -> int:
            """对同一冻结表格执行一个标量等值计数。"""

            result = runtime.table_compute(
                snapshot.opaque_handle,
                tenant_id="tenant-a",
                turn_id="turn-a",
                operation={"filter": {"op": "eq", "column": "value", "value": value}, "aggregate": "count"},
            )
            return int(result["result"])

        counts = {"zero": count(0), "false": count(False), "empty": count(None)}

    assert counts == {"zero": 1, "false": 1, "empty": 2}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_table_filter_rejects_non_finite_scalar(value: float) -> None:
    """过滤AST中的非有限浮点必须稳定拒绝，不能在摘要计算时泄漏裸ValueError。"""

    with pytest.raises(InputBindingError) as captured:
        _canonical_table_scalar(value)
    assert captured.value.code == "ATTACHMENT_COMPUTE_AST_INVALID"


@pytest.mark.parametrize(
    "value",
    ["NaN", "Infinity", "-Infinity", "1e309", 10**1_000],
    ids=["nan", "positive-infinity", "negative-infinity", "float-overflow", "integer-overflow"],
)
@pytest.mark.parametrize("aggregate", ["sum", "avg"])
def test_table_aggregate_rejects_non_finite_values(value: object, aggregate: str) -> None:
    """表格中的NaN、无穷与溢出数值不得进入sum/avg回执。"""

    with _session() as db:
        _seed(db)
        element = db.exec(select(InputDocumentElement)).one()
        element.table_json = {"columns": ["value"], "rows": [[value]]}
        db.add(element)
        db.commit()
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]

        with pytest.raises(InputBindingError) as captured:
            runtime.table_compute(
                snapshot.opaque_handle,
                tenant_id="tenant-a",
                turn_id="turn-a",
                operation={"aggregate": aggregate, "column": "value"},
            )
        assert captured.value.code == "ATTACHMENT_TABLE_TYPE_INVALID"


def test_bare_resource_id_and_cross_turn_handle_are_rejected() -> None:
    """反向证明input.read不能用裸resource ID，也不能把handle带到其他Turn。"""

    with _session() as db:
        _seed(db)
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        with pytest.raises(InputBindingError, match="附件不可用"):
            runtime.read("input-sales", tenant_id="tenant-a", turn_id="turn-a")
        with pytest.raises(InputBindingError, match="附件不可用"):
            runtime.read(snapshot.opaque_handle, tenant_id="tenant-a", turn_id="turn-other")


def test_parser_selection_change_does_not_drift_existing_turn() -> None:
    """同一Turn首次冻结后，parser升级只影响下一Turn而不漂移既有证据。"""

    with _session() as db:
        resource, _ = _seed(db)
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        newer = InputResourceExtraction(
            id="extract-v2",
            tenant_id="tenant-a",
            resource_id=resource.id,
            resource_version=resource.version,
            content_checksum=resource.content_checksum,
            parser_name="builtin-csv",
            parser_version="2.0.0",
            parser_config_checksum="config-v2",
            extraction_checksum="extract-v2",
            element_manifest_checksum="manifest-v2",
            published_from_attempt_id="attempt-v2",
        )
        db.add(newer)
        selected = db.exec(select(SelectedResourceExtraction)).one()
        selected.extraction_id = newer.id
        selected.revision += 1
        db.add(selected)
        db.commit()

        result = runtime.read(snapshot.opaque_handle, tenant_id="tenant-a", turn_id="turn-a")

    assert result["elements"][0]["text"] == "region,amount\nEast,100"


def test_resource_revoke_countermands_existing_turn_before_next_read() -> None:
    """附件撤权与Skill countermand分离：ACL变化后旧handle机械拒绝且不新增回执。"""

    with _session() as db:
        resource, _ = _seed(db)
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        resource.access_status = "revoked"
        resource.acl_revision += 1
        db.add(resource)
        db.commit()

        with pytest.raises(InputBindingError) as exc_info:
            runtime.read(snapshot.opaque_handle, tenant_id="tenant-a", turn_id="turn-a")
        receipts = db.exec(select(TurnInputReadReceipt)).all()

    assert exc_info.value.code == "ATTACHMENT_COUNTERMANDED"
    assert receipts == []


def test_table_profile_search_and_bounded_compute_share_frozen_slice() -> None:
    """正向验证搜索、表格画像与受控计算共享同一handle/slice且不依赖模型心算。"""

    with _session() as db:
        _seed(db)
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        search = runtime.search(
            snapshot.opaque_handle,
            tenant_id="tenant-a",
            turn_id="turn-a",
            query="east",
        )
        profile = runtime.table_profile(
            snapshot.opaque_handle,
            tenant_id="tenant-a",
            turn_id="turn-a",
        )
        computed = runtime.table_compute(
            snapshot.opaque_handle,
            tenant_id="tenant-a",
            turn_id="turn-a",
            operation={
                "filter": {"op": "eq", "column": "region", "value": "East"},
                "aggregate": "sum",
                "column": "amount",
            },
        )

    assert len(search["matches"]) == 1
    assert profile["table_profiles"] == [
        {
            "element_id": "element-v1",
            "columns": ["region", "amount"],
            "row_count": 1,
            "column_count": 2,
            "locator": {"kind": "csv", "row_start": 1, "row_end": 2},
            "content_checksum": "element-checksum",
        }
    ]
    assert computed["result"] == 100.0
    assert computed["matched_rows"] == 1
    assert len(computed["operation_checksum"]) == 64


@pytest.mark.parametrize(
    "operation",
    [
        {"aggregate": "eval", "column": "amount"},
        {"aggregate": "sum", "column": "missing"},
        {"filter": {"op": "regex", "column": "region", "value": ".*"}, "aggregate": "count"},
    ],
)
def test_table_compute_rejects_freeform_or_missing_columns(operation: dict[str, object]) -> None:
    """反向验证自由表达式、未知列和非白名单filter都在计算前fail closed。"""

    with _session() as db:
        _seed(db)
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        with pytest.raises(InputBindingError):
            runtime.table_compute(
                snapshot.opaque_handle,
                tenant_id="tenant-a",
                turn_id="turn-a",
                operation=operation,
            )


@pytest.mark.parametrize(
    ("cached_value", "expected_status"),
    [("0.8", "match"), ("0.9", "conflict"), (None, "gap")],
)
def test_formula_verify_recomputes_decimal_and_exposes_cache_status(
    cached_value: str | None,
    expected_status: str,
) -> None:
    """公式回执必须把缓存值与Decimal重算值并列，绝不让模型或视觉代算。"""

    with _session() as db:
        _seed(db)
        element = db.exec(select(InputDocumentElement)).one()
        formula = "B2/C2"
        formula_checksum = hashlib.sha256(formula.encode()).hexdigest()
        element.table_json = {
            **element.table_json,
            "cells": [
                {"cell": "B2", "raw_value": "80", "formula": None},
                {"cell": "C2", "raw_value": "100", "formula": None},
                {
                    "cell": "D2",
                    "raw_value": cached_value or "=B2/C2",
                    "cached_value": cached_value,
                    "formula": formula,
                    "formula_checksum": formula_checksum,
                    "formula_type": "normal",
                },
            ],
        }
        db.add(element)
        db.commit()
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        result = runtime.table_compute(
            snapshot.opaque_handle,
            tenant_id="tenant-a",
            turn_id="turn-a",
            operation={
                "op": "verify_formula",
                "element_id": element.id,
                "cell": "D2",
                "formula_checksum": formula_checksum,
            },
        )

    assert result["computed_value"] == "0.8"
    assert result["cached_value"] == cached_value
    assert result["status"] == expected_status
    assert result["dependencies"] == ["B2", "C2"]
    assert len(result["operation_checksum"]) == 64


@pytest.mark.parametrize(
    ("formula", "gap_code"),
    [
        ("[remote.xlsx]Sheet1!A1", "ATTACHMENT_FORMULA_EXTERNAL_REFERENCE"),
        ("B2/0", "ATTACHMENT_FORMULA_DIVISION_BY_ZERO"),
        ("D2+1", "ATTACHMENT_FORMULA_CYCLE"),
        ("NOW()", "ATTACHMENT_FORMULA_UNSUPPORTED"),
    ],
)
def test_formula_verify_failures_become_explicit_gap_not_zero(
    formula: str,
    gap_code: str,
) -> None:
    """外链、除零、循环和易变函数必须形成gap，不能返回0或缓存值冒充重算。"""

    with _session() as db:
        _seed(db)
        element = db.exec(select(InputDocumentElement)).one()
        checksum = hashlib.sha256(formula.encode()).hexdigest()
        element.table_json = {
            **element.table_json,
            "cells": [
                {"cell": "B2", "raw_value": "80", "formula": None},
                {
                    "cell": "D2",
                    "raw_value": "1",
                    "cached_value": "1",
                    "formula": formula,
                    "formula_checksum": checksum,
                    "formula_type": "normal",
                },
            ],
        }
        db.add(element)
        db.commit()
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        result = runtime.table_compute(
            snapshot.opaque_handle,
            tenant_id="tenant-a",
            turn_id="turn-a",
            operation={
                "op": "verify_formula",
                "element_id": element.id,
                "cell": "D2",
                "formula_checksum": checksum,
            },
        )

    assert result["status"] == "gap"
    assert result["gap_code"] == gap_code
    assert result["computed_value"] is None


def test_shared_formula_and_oversized_literal_become_explicit_gaps() -> None:
    """共享公式与超长单token必须逐项收敛为gap，不能抛裸异常或执行缓存值。"""

    with _session() as db:
        _seed(db)
        element = db.exec(select(InputDocumentElement)).one()
        formulas = (("D2", "B2/C2", "shared"), ("D3", "1" * (FORMULA_MAX_CHARS + 1), "normal"))
        element.table_json = {
            **element.table_json,
            "cells": [
                {"cell": "B2", "raw_value": "80", "formula": None},
                {"cell": "C2", "raw_value": "100", "formula": None},
                *[
                    {
                        "cell": cell,
                        "raw_value": "0.8",
                        "cached_value": "0.8",
                        "formula": formula,
                        "formula_checksum": hashlib.sha256(formula.encode()).hexdigest(),
                        "formula_type": formula_type,
                    }
                    for cell, formula, formula_type in formulas
                ],
            ],
        }
        db.add(element)
        db.commit()
        runtime = TurnInputRuntimeService(db)
        snapshot = runtime.snapshot_message_inputs(
            tenant_id="tenant-a", session_id="session-a", turn_id="turn-a"
        )[0]
        results = [
            runtime.table_compute(
                snapshot.opaque_handle,
                tenant_id="tenant-a",
                turn_id="turn-a",
                operation={
                    "op": "verify_formula",
                    "element_id": element.id,
                    "cell": cell,
                    "formula_checksum": hashlib.sha256(formula.encode()).hexdigest(),
                },
            )
            for cell, formula, _formula_type in formulas
        ]

    assert [item["status"] for item in results] == ["gap", "gap"]
    assert [item["gap_code"] for item in results] == [
        "ATTACHMENT_FORMULA_UNSUPPORTED",
        "ATTACHMENT_FORMULA_BUDGET_EXCEEDED",
    ]


def test_formula_evaluator_is_independent_from_process_decimal_context() -> None:
    """外部库修改全局Decimal精度时，同一公式仍必须得到完全相同的规范值。"""

    cells = {
        "A1": {"cell": "A1", "raw_value": "1", "formula": None},
        "A2": {"cell": "A2", "raw_value": "3", "formula": None},
        "A3": {"cell": "A3", "raw_value": "", "formula": "A1/A2"},
    }
    original_precision = getcontext().prec
    try:
        getcontext().prec = 6
        first, _ = _FormulaEvaluator(cells).evaluate_cell("A3")
        getcontext().prec = 28
        second, _ = _FormulaEvaluator(cells).evaluate_cell("A3")
    finally:
        getcontext().prec = original_precision

    assert first == second
    assert str(first) == "0.3333333333333333333333333333333333"


def test_formula_evaluator_rejects_non_finite_and_deep_dependency_graph() -> None:
    """NaN与超深依赖图必须返回稳定业务错误，不能泄漏Decimal或RecursionError。"""

    non_finite = {
        "A1": {"cell": "A1", "raw_value": "NaN", "formula": None},
        "A2": {"cell": "A2", "raw_value": "", "formula": "A1+1"},
    }
    with pytest.raises(InputBindingError) as numeric:
        _FormulaEvaluator(non_finite).evaluate_cell("A2")
    assert numeric.value.code == "ATTACHMENT_FORMULA_NUMERIC_INVALID"

    deep = {
        f"A{index}": {
            "cell": f"A{index}",
            "raw_value": "1" if index == 70 else "",
            "formula": None if index == 70 else f"A{index + 1}+1",
        }
        for index in range(1, 71)
    }
    with pytest.raises(InputBindingError) as budget:
        _FormulaEvaluator(deep).evaluate_cell("A1")
    assert budget.value.code == "ATTACHMENT_FORMULA_BUDGET_EXCEEDED"
