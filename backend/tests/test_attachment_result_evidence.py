"""
@Time       : 2026/08/13 23:55
@Author     : zhanglp8181
@File       : test_attachment_result_evidence.py
@CallChain  : pytest → DynamicTaskResult → attachment evidence verifier
@Description: 验证附件结论只能引用同Execution固定元素，错locator/checksum和值均拒绝。
"""

from app.dynamic_tasks.planning import NormalizedPlan, PlanStep, SuccessCriterion
from app.dynamic_tasks.result_verifier import DynamicTaskResult, verify_dynamic_result


def _plan() -> NormalizedPlan:
    """构造只有最终answer的最小动态计划。"""

    return NormalizedPlan(
        goal="核对续约通知",
        success_criteria=(SuccessCriterion(id="fact", type="assertion", spec={}),),
        steps=(PlanStep(step_key="answer", title="回答", kind="answer"),),
    )


def _result(*, value: int = 60, checksum: str = "a" * 64) -> DynamicTaskResult:
    """构造一条带页码证据的附件事实结论。"""

    return DynamicTaskResult.model_validate(
        {
            "markdown": "合同要求提前60天通知。",
            "criterion_evidence": {"fact": ["answer"]},
            "claims": [
                {
                    "claim_id": "renewal_days",
                    "text": "合同要求提前60天通知",
                    "claim_type": "fact",
                    "normalized_value": value,
                    "unit": "days",
                    "computation_receipt_id": None,
                    "semantic_review_status": "verified",
                    "evidence_refs": [
                        {
                            "snapshot_id": "snapshot_1",
                            "extraction_id": "extraction_1",
                            "read_operation_id": "operation_1",
                            "slice_checksum": "b" * 64,
                            "element_id": "element_1",
                            "element_checksum": checksum,
                            "locator": {"page": 1},
                        }
                    ],
                }
            ],
        }
    )


def test_attachment_claim_accepts_exact_frozen_element() -> None:
    """相同snapshot/extraction/element/checksum/locator和值才能通过。"""

    verification = verify_dynamic_result(
        _result(),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog={
            "element_1": {
                "snapshot_id": "snapshot_1",
                "extraction_id": "extraction_1",
                "read_operation_id": "operation_1",
                "slice_checksum": "b" * 64,
                "element_checksum": "a" * 64,
                "locator": {"page": 1},
                "text": "Renewal notice: 60 days",
            }
        },
        attachment_evidence_required=True,
    )
    assert verification["passed"] is True


def test_verified_claim_must_be_disclosed_in_user_markdown() -> None:
    """Result元数据中的真实Claim若未进入用户正文，必须修复而非静默发布。"""

    verification = verify_dynamic_result(
        _result().model_copy(update={"markdown": "合同材料已核对。"}),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog={
            "element_1": {
                "snapshot_id": "snapshot_1",
                "extraction_id": "extraction_1",
                "read_operation_id": "operation_1",
                "slice_checksum": "b" * 64,
                "element_checksum": "a" * 64,
                "locator": {"page": 1},
                "text": "Renewal notice: 60 days",
            }
        },
        attachment_evidence_required=True,
    )

    assert verification["passed"] is False
    assert verification["attachment_evidence_errors"] == [
        "renewal_days:not_disclosed_in_markdown"
    ]


def test_required_attachment_evidence_fails_closed_when_live_catalog_is_empty() -> None:
    """附件被撤权或证据目录失效后，required Claim不能因空目录而跳过验证。"""

    verification = verify_dynamic_result(
        _result(),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog={},
        attachment_evidence_required=True,
    )

    assert verification["passed"] is False
    assert verification["attachment_evidence_errors"] == ["attachments:evidence_unavailable"]


def test_revoked_snapshot_sentinel_fails_even_when_plan_did_not_require_claims() -> None:
    """提案后撤权必须阻止普通Markdown发布，不能依赖Planner是否声明附件Claim。"""

    result = DynamicTaskResult(
        markdown="已完成附件分析。",
        criterion_evidence={"fact": ("answer",)},
    )
    verification = verify_dynamic_result(
        result,
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog={
            "__unavailable__": {"snapshot_ids": ("snapshot-revoked",)},
        },
        attachment_evidence_required=False,
    )

    assert verification["passed"] is False
    assert verification["attachment_evidence_errors"] == ["attachments:evidence_unavailable"]


def test_attachment_claim_rejects_real_element_with_wrong_value_or_checksum() -> None:
    """引用真实元素但错报数值或校验和仍不得冒充已核验结论。"""

    catalog = {
        "element_1": {
            "snapshot_id": "snapshot_1",
            "extraction_id": "extraction_1",
            "read_operation_id": "operation_1",
            "slice_checksum": "b" * 64,
            "element_checksum": "a" * 64,
            "locator": {"page": 1},
            "text": "Renewal notice: 60 days",
        }
    }
    wrong_value = verify_dynamic_result(
        _result(value=30),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )
    wrong_checksum = verify_dynamic_result(
        _result(checksum="b" * 64),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )
    assert wrong_value["passed"] is False
    assert "renewal_days:element_1:unsupported_value" in wrong_value["attachment_evidence_errors"]
    assert wrong_checksum["passed"] is False
    assert "renewal_days:element_1:element_checksum" in wrong_checksum["attachment_evidence_errors"]


def test_natural_fact_without_scalar_requires_exact_source_backed_claim_text() -> None:
    """normalized_value为空时仍要求Claim正文逐字来自权威元素，拒绝任意释义洗白。"""

    catalog = {
        "element_1": {
            "snapshot_id": "snapshot_1",
            "extraction_id": "extraction_1",
            "read_operation_id": "operation_1",
            "slice_checksum": "b" * 64,
            "element_checksum": "a" * 64,
            "locator": {"page": 1},
            "text": "合同要求提前60天通知",
        }
    }
    exact = _result().model_copy(
        update={
            "claims": (
                _result().claims[0].model_copy(update={"normalized_value": None}),
            )
        }
    )
    invented = exact.model_copy(
        update={
            "claims": (
                exact.claims[0].model_copy(update={"text": "合同允许不通知直接解约"}),
            )
        }
    )

    accepted = verify_dynamic_result(
        exact,
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )
    rejected = verify_dynamic_result(
        invented.model_copy(update={"markdown": "合同允许不通知直接解约。"}),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )

    assert accepted["passed"] is True
    assert "renewal_days:unsupported_text" in rejected["attachment_evidence_errors"]


def test_attachment_claim_accepts_matching_visual_fact_but_rejects_wrong_value() -> None:
    """视觉观察只支持同Snapshot元素上同claim_id的规范值，不能泛化为任意结论。"""

    catalog = {
        "element_1": {
            "snapshot_id": "snapshot_1",
            "extraction_id": "extraction_1",
            "read_operation_id": "operation_1",
            "slice_checksum": "b" * 64,
            "element_checksum": "a" * 64,
            "locator": {"page": 1},
            "text": "Image 64x40; OCR not requested",
            "visual_fact_values": {"renewal_days": ["60"]},
        }
    }
    accepted = verify_dynamic_result(
        _result(),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )
    rejected = verify_dynamic_result(
        _result(value=30),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=catalog,
        attachment_evidence_required=True,
    )

    assert accepted["passed"] is True
    assert "renewal_days:element_1:unsupported_value" in rejected["attachment_evidence_errors"]


def test_computed_claim_requires_exact_successful_formula_receipt() -> None:
    """随机、跨Execution或值冲突的计算回执不能让computed Claim通过。"""

    raw = _result().model_dump(mode="json")
    raw["claims"][0].update(
        {
            "claim_id": "formula_D2",
            "claim_type": "computed",
            "normalized_value": "0.8",
            "computation_receipt_id": "operation-formula",
        }
    )
    result = DynamicTaskResult.model_validate(raw)
    attachment_catalog = {
        "element_1": {
            "snapshot_id": "snapshot_1",
            "extraction_id": "extraction_1",
            "read_operation_id": "operation_1",
            "slice_checksum": "b" * 64,
            "element_checksum": "a" * 64,
            "locator": {"page": 1},
            "text": "formula B2/C2",
        }
    }
    check = {
        "fact_key": "formula_D2",
        "snapshot_id": "snapshot_1",
        "element_id": "element_1",
        "slice_checksum": "b" * 64,
        "formula_checksum": "c" * 64,
        "operation_checksum": "d" * 64,
        "computation_checksum": "e" * 64,
        "evaluator_policy_checksum": "f" * 64,
        "computed_value": "0.8",
        "status": "match",
    }
    accepted = verify_dynamic_result(
        result,
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=attachment_catalog,
        computation_evidence_catalog={"operation-formula": (check,)},
        attachment_evidence_required=True,
    )
    random_receipt_raw = result.model_dump(mode="json")
    random_receipt_raw["claims"][0]["computation_receipt_id"] = "foreign-or-random"
    rejected_receipt = verify_dynamic_result(
        DynamicTaskResult.model_validate(random_receipt_raw),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=attachment_catalog,
        computation_evidence_catalog={"operation-formula": (check,)},
        attachment_evidence_required=True,
    )
    wrong_value_raw = result.model_dump(mode="json")
    wrong_value_raw["claims"][0]["normalized_value"] = "0.9"
    rejected_value = verify_dynamic_result(
        DynamicTaskResult.model_validate(wrong_value_raw),
        plan=_plan(),
        completed_step_keys={"answer"},
        attachment_evidence_catalog=attachment_catalog,
        computation_evidence_catalog={"operation-formula": (check,)},
        attachment_evidence_required=True,
    )

    assert accepted["passed"] is True
    assert "formula_D2:computation_receipt_invalid" in rejected_receipt[
        "attachment_evidence_errors"
    ]
    assert "formula_D2:computation_receipt_invalid" in rejected_value[
        "attachment_evidence_errors"
    ]
