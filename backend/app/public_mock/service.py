"""
@Time       : 2026/07/22 13:28
@Author     : zhanglp8181
@File       : service.py
@CallChain  : public-mock router → execute_public_mock → 严格模型/确定性处理器
@Description: 执行受控公共 Mock 工具并返回可审计业务回执，包括政企专线与工单能力。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.public_mock.copywriter import TextGenerator, polish_text
from app.public_mock import schemas


def _now() -> str:
    """返回公共 mock 回执使用的当前 UTC 时间。"""

    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    """生成带业务前缀的演示凭证编号。"""

    return f"{prefix}-{uuid4().hex[:12].upper()}"


def _month(value: str | None) -> str:
    """返回显式月份，未提供时使用当前 UTC 月份。"""

    return value or datetime.now(UTC).strftime("%Y-%m")


def _room_book(req: schemas.RoomBookRequest) -> schemas.RoomBookResponse:
    """返回确定性的会议室预订成功回执。"""

    return schemas.RoomBookResponse(
        booking_id=_id("ROOM"),
        status="booked",
        room_name=req.room_preference or "序伴会议室 A",
        capacity=max(req.attendees or 6, 6),
        location="3F-A区",
        date=req.date,
        time_slot=f"{req.start_time}-{req.end_time}",
        alternatives=[],
        message="会议室已预订。",
    )


def _supply_request(req: schemas.SupplyRequest) -> schemas.SupplyResponse:
    """按申请清单返回办公用品登记和批准数量。"""

    approved = [
        schemas.ApprovedSupplyItem(name=x.name, requested=x.quantity, approved=x.quantity)
        for x in req.items
    ]
    return schemas.SupplyResponse(
        request_id=_id("SUP"),
        status="approved",
        approved_items=approved,
        pickup_location="行政服务台",
        message="申领已登记。",
        submitted_at=_now(),
    )


_CONTRACT_REFERENCE_FIXTURES = (
    (
        ("无限责任", "责任上限", "赔偿", "违约责任"),
        schemas.ContractResult(
            doc_id="DEMO-CONTRACT-LIABILITY-001",
            doc_type="contract",
            title="软件采购责任限制条款示例（演示资料）",
            date="2025-03-18",
            parties=["演示采购方", "演示供应方"],
            summary=(
                "普通违约累计责任设置明确上限，并将故意或重大过失、保密、知识产权、"
                "数据安全及法律不得限制的责任列为例外。"
            ),
            relevance=0.98,
            citation="DEMO-CLAUSE-LIABILITY-001",
        ),
    ),
    (
        ("无限责任", "责任上限", "赔偿", "违约责任"),
        schemas.ContractResult(
            doc_id="DEMO-CASE-LIABILITY-002",
            doc_type="case",
            title="责任上限谈判复盘（演示资料）",
            date="2025-01-09",
            parties=["演示甲方", "演示乙方"],
            summary=(
                "内部复盘建议明确上限计算基数、累计口径、适用期间和除外责任，"
                "避免仅写“合理上限”造成解释分歧。"
            ),
            relevance=0.94,
            citation="DEMO-REVIEW-LIABILITY-002",
        ),
    ),
    (
        ("交付", "验收", "延期"),
        schemas.ContractResult(
            doc_id="DEMO-CONTRACT-DELIVERY-001",
            doc_type="contract",
            title="软件交付与验收条款示例（演示资料）",
            date="2025-02-12",
            parties=["演示采购方", "演示供应方"],
            summary="明确交付物、验收标准、异议期限、整改次数和延期责任的衔接方式。",
            relevance=0.93,
            citation="DEMO-CLAUSE-DELIVERY-001",
        ),
    ),
    (
        ("软件采购合同", "软件采购"),
        schemas.ContractResult(
            doc_id="DEMO-CONTRACT-SOFTWARE-001",
            doc_type="contract",
            title="企业级软件采购合同（演示资料）",
            date="2025-01-16",
            parties=["演示采购方", "演示供应方"],
            summary="覆盖软件交付、服务等级、知识产权、数据安全和违约责任。",
            relevance=0.90,
            citation="DEMO-CONTRACT-SOFTWARE-001",
        ),
    ),
)


def _contract_query(
    req: schemas.ContractArchiveQueryRequest,
) -> schemas.ContractArchiveQueryResponse:
    """按关键词、文档类型、日期和数量限制检索相关演示合同资料。"""

    query_text = " ".join([req.query, *req.keywords]).casefold()
    matched_rows = [
        result
        for keywords, result in _CONTRACT_REFERENCE_FIXTURES
        if any(keyword.casefold() in query_text for keyword in keywords)
        and (req.doc_type == "all" or result.doc_type == req.doc_type)
        and (req.date_from is None or result.date >= req.date_from)
        and (req.date_to is None or result.date <= req.date_to)
    ][: req.top_k]
    message = (
        f"演示资料检索完成，返回 {len(matched_rows)} 条相关记录。"
        if matched_rows
        else "演示资料库没有找到匹配记录。"
    )
    return schemas.ContractArchiveQueryResponse(
        query=req.query,
        total=len(matched_rows),
        results=matched_rows,
        message=message,
    )


def _contract_risk_assess(
    req: schemas.ContractRiskAssessRequest,
) -> schemas.ContractRiskAssessResponse:
    """按显式演示规则返回结构化风险初筛，不冒充正式法律审查。"""

    contract_text = req.contract_content.strip()
    if len(contract_text) < 40:
        return schemas.ContractRiskAssessResponse(
            assessment_id=_id("RISK"),
            status="insufficient",
            risk_level="unknown",
            risk_points=[],
            reference_codes=[],
            requires_human_review=False,
            message="合同文本不足以完成演示风险初筛，请补充完整条款。",
            assessed_at=_now(),
        )

    risk_points: list[schemas.ContractRiskPoint] = []
    reference_codes: list[str] = []
    if "无限责任" in contract_text:
        risk_points.append(
            schemas.ContractRiskPoint(
                code="UNLIMITED_LIABILITY",
                title="责任范围未设上限",
                severity="high",
                evidence="条款包含“无限责任”表述。",
                suggestion="明确累计责任上限、计算基数、适用期间及不得限制的除外责任。",
            )
        )
        reference_codes.append("DEMO-CLAUSE-LIABILITY-001")
    if "单方任意解除" in contract_text or "无需理由解除" in contract_text:
        risk_points.append(
            schemas.ContractRiskPoint(
                code="UNILATERAL_TERMINATION",
                title="单方任意解除权",
                severity="high",
                evidence="条款允许一方无需明确事由单方解除。",
                suggestion="限定解除事由、通知期限、补救期和解除后的费用及数据处理责任。",
            )
        )
        reference_codes.append("DEMO-REVIEW-TERMINATION-001")
    if not risk_points and any(keyword in contract_text for keyword in ("交付", "验收", "延期")):
        risk_points.append(
            schemas.ContractRiskPoint(
                code="DELIVERY_ACCEPTANCE_DETAIL",
                title="交付验收机制需细化",
                severity="medium",
                evidence="条款涉及交付或验收，但仍需核对标准、期限和整改机制。",
                suggestion="明确交付物、客观验收标准、异议期限、整改次数和延期责任。",
            )
        )
        reference_codes.append("DEMO-CLAUSE-DELIVERY-001")

    risk_level = (
        "high"
        if any(point.severity == "high" for point in risk_points)
        else "medium"
        if risk_points
        else "low"
    )
    return schemas.ContractRiskAssessResponse(
        assessment_id=_id("RISK"),
        status="assessed",
        risk_level=risk_level,
        risk_points=risk_points,
        reference_codes=list(dict.fromkeys(reference_codes)),
        requires_human_review=risk_level == "high",
        message=(
            "演示初筛命中高风险信号，需由法务复核。"
            if risk_level == "high"
            else "演示初筛已完成；未命中高风险信号不等于正式法律审查通过。"
        ),
        assessed_at=_now(),
    )


_PARTNER_DUE_DILIGENCE_FIXTURES = {
    "91370000MA3D3M001X": {
        "company_name": "共格演示科技有限公司",
        "subject_status": "active",
        "litigation_count": 0,
        "enforcement_count": 0,
        "blacklisted": False,
        "risk_flags": [],
        "evidence_sources": [
            "DEMO-CORP-REGISTRY-001",
            "DEMO-LITIGATION-SCREEN-001",
            "DEMO-BLACKLIST-SCREEN-001",
        ],
    },
    "91370000MA3R15K01X": {
        "company_name": "共格演示风险供应商有限公司",
        "subject_status": "abnormal",
        "litigation_count": 3,
        "enforcement_count": 1,
        "blacklisted": True,
        "risk_flags": [
            schemas.PartnerRiskFlag(
                code="ENFORCEMENT_RECORD",
                title="存在演示执行记录",
                severity="high",
                evidence="演示数据集中记录 1 条执行信息。",
            ),
            schemas.PartnerRiskFlag(
                code="DEMO_BLACKLIST_MATCH",
                title="命中演示黑名单",
                severity="high",
                evidence="主体命中本地演示供应商黑名单。",
            ),
        ],
        "evidence_sources": [
            "DEMO-CORP-REGISTRY-002",
            "DEMO-LITIGATION-SCREEN-002",
            "DEMO-ENFORCEMENT-SCREEN-001",
            "DEMO-BLACKLIST-SCREEN-002",
        ],
    },
}


def _partner_due_diligence(
    req: schemas.PartnerDueDiligenceRequest,
) -> schemas.PartnerDueDiligenceResponse:
    """按固定虚构主体返回尽调事实，未知或名称不符时不生成通过结论。"""

    fixture = _PARTNER_DUE_DILIGENCE_FIXTURES.get(req.unified_social_credit_code)
    if fixture is None:
        return schemas.PartnerDueDiligenceResponse(
            check_id=_id("DD"),
            status="not_found",
            subject_name=req.company_name,
            unified_social_credit_code=req.unified_social_credit_code,
            subject_status="unknown",
            credit_code_match=False,
            litigation_count=0,
            enforcement_count=0,
            blacklisted=False,
            risk_level="unknown",
            risk_flags=[],
            recommendation="insufficient",
            requires_human_review=False,
            evidence_as_of="2026-07-27",
            evidence_sources=[],
            message="本地演示尽调数据集中未找到该主体，不能形成入库建议。",
        )

    expected_name = str(fixture["company_name"])
    if req.company_name != expected_name:
        return schemas.PartnerDueDiligenceResponse(
            check_id=_id("DD"),
            status="identity_mismatch",
            subject_name=expected_name,
            unified_social_credit_code=req.unified_social_credit_code,
            subject_status=str(fixture["subject_status"]),
            credit_code_match=False,
            litigation_count=int(fixture["litigation_count"]),
            enforcement_count=int(fixture["enforcement_count"]),
            blacklisted=bool(fixture["blacklisted"]),
            risk_level="unknown",
            risk_flags=[],
            recommendation="insufficient",
            requires_human_review=False,
            evidence_as_of="2026-07-27",
            evidence_sources=list(fixture["evidence_sources"]),
            message="企业名称与统一社会信用代码对应的演示主体不一致，请核对主体信息。",
        )

    risk_flags = list(fixture["risk_flags"])
    has_risk = bool(risk_flags)
    return schemas.PartnerDueDiligenceResponse(
        check_id=_id("DD"),
        status="assessed",
        subject_name=expected_name,
        unified_social_credit_code=req.unified_social_credit_code,
        subject_status=str(fixture["subject_status"]),
        credit_code_match=True,
        litigation_count=int(fixture["litigation_count"]),
        enforcement_count=int(fixture["enforcement_count"]),
        blacklisted=bool(fixture["blacklisted"]),
        risk_level="high" if has_risk else "low",
        risk_flags=risk_flags,
        recommendation="human_review" if has_risk else "pass",
        requires_human_review=has_risk,
        evidence_as_of="2026-07-27",
        evidence_sources=list(fixture["evidence_sources"]),
        message=(
            "演示尽调命中风险信号，需由法务真人复核。"
            if has_risk
            else "演示尽调未命中风险信号；结果不代表真实外部数据库核验或正式准入批准。"
        ),
    )


def _expense_submit(req: schemas.ExpenseSubmitRequest) -> schemas.ExpenseSubmitResponse:
    """返回报销单已受理的确定性业务凭证。"""

    return schemas.ExpenseSubmitResponse(
        expense_id=_id("EXP"),
        status="accepted",
        message=f"{req.amount:.2f} {req.currency} 报销单已受理。",
        submitted_at=_now(),
    )


def _travel_policy_assess(
    req: schemas.TravelPolicyAssessRequest,
) -> schemas.TravelPolicyAssessResponse:
    """按冻结城市档位和住宿晚数评估境内普通员工住宿费是否超标。"""

    policy_code = "TRAVEL-LODGING-STAFF-2026"
    approval_verified = (
        req.trip_approval_status == "approved"
        and req.trip_approval_number.startswith("TRIP-DEMO-APPROVED-")
    )
    if req.employee_id != "E002":
        return schemas.TravelPolicyAssessResponse(
            status="unsupported_employee",
            policy_code=policy_code,
            employee_level=None,
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=approval_verified,
            message="当前演示员工没有可信普通员工职级回执。",
        )
    if req.trip_scope != "domestic":
        return schemas.TravelPolicyAssessResponse(
            status="unsupported",
            policy_code=policy_code,
            employee_level="staff",
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=approval_verified,
            message="境外差旅标准不在当前自动评估范围内。",
        )
    if not approval_verified:
        return schemas.TravelPolicyAssessResponse(
            status="approval_unverified",
            policy_code=policy_code,
            employee_level="staff",
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=False,
            message="事前出差申请未通过受控演示申请号核验。",
        )
    try:
        start_date = date.fromisoformat(req.trip_start_date)
        end_date = date.fromisoformat(req.trip_end_date)
    except ValueError:
        return schemas.TravelPolicyAssessResponse(
            status="invalid_date",
            policy_code=policy_code,
            employee_level="staff",
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=True,
            message="行程日期格式无效。",
        )
    lodging_nights = (end_date - start_date).days
    if lodging_nights < 1:
        return schemas.TravelPolicyAssessResponse(
            status="invalid_date",
            policy_code=policy_code,
            employee_level="staff",
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=True,
            message="住宿行程结束日期必须晚于开始日期。",
        )
    today = datetime.now(UTC).date()
    days_since_trip_end = (today - end_date).days
    submission_deadline = end_date + timedelta(days=14)
    if days_since_trip_end < 0 or days_since_trip_end > 14:
        return schemas.TravelPolicyAssessResponse(
            status="late_submission",
            policy_code=policy_code,
            employee_level="staff",
            claimed_amount=req.claimed_amount,
            over_limit_amount=0,
            approval_verified=True,
            submission_deadline=submission_deadline.isoformat(),
            days_since_trip_end=days_since_trip_end,
            message="差旅报销已超出行程结束后 14 天的自动受理时限。",
        )

    tier_1_cities = {"北京", "上海", "广州", "深圳"}
    tier_2_cities = {"杭州", "南京", "成都", "武汉"}
    if req.destination_city in tier_1_cities:
        city_tier, nightly_limit = "tier_1", 500.0
    elif req.destination_city in tier_2_cities:
        city_tier, nightly_limit = "tier_2", 400.0
    else:
        city_tier, nightly_limit = "tier_3", 320.0
    allowance_limit = nightly_limit * lodging_nights
    over_limit_amount = max(0.0, req.claimed_amount - allowance_limit)
    status = "over_limit" if over_limit_amount > 0 else "within_limit"
    return schemas.TravelPolicyAssessResponse(
        status=status,
        policy_code=policy_code,
        employee_level="staff",
        city_tier=city_tier,
        lodging_nights=lodging_nights,
        nightly_limit=nightly_limit,
        allowance_limit=allowance_limit,
        claimed_amount=req.claimed_amount,
        over_limit_amount=over_limit_amount,
        approval_verified=True,
        submission_deadline=submission_deadline.isoformat(),
        days_since_trip_end=days_since_trip_end,
        message="差旅住宿标准评估完成。",
    )


def _expense_quota(req: schemas.ExpenseQuotaQueryRequest) -> schemas.ExpenseQuotaQueryResponse:
    """返回指定员工和月份的演示报销额度。"""

    total, used = 20_000.0, 0.0
    return schemas.ExpenseQuotaQueryResponse(
        employee_id=req.employee_id,
        month=_month(req.month),
        total_quota=total,
        used=used,
        remaining=total - used,
        currency="CNY",
        message="报销额度查询成功。",
    )


def _hr_balance(req: schemas.HrBalanceQueryRequest) -> schemas.HrBalanceQueryResponse:
    """返回演示假期余额，并计算请假天数及加班调休政策资格。"""

    leave_balance = schemas.LeaveBalance(annual=5, compensatory=2, sick=10, personal=3)
    attendance = (
        schemas.Attendance(
            work_days=23,
            actual_days=22,
            late_count=1,
            early_leave_count=0,
            absent_days=1,
            overtime_hours=4.5,
        )
        if req.include_attendance
        else None
    )
    request_assessment = _assess_leave_request(req, leave_balance)
    overtime_policy_assessment = _assess_overtime_policy(req)
    overtime_credit_assessment = _assess_overtime_credit(
        req,
        leave_balance,
        overtime_policy_assessment,
        request_assessment,
    )
    return schemas.HrBalanceQueryResponse(
        employee_id=req.employee_id,
        month=_month(req.month),
        leave_balance=leave_balance,
        attendance=attendance,
        request_assessment=request_assessment,
        overtime_policy_assessment=overtime_policy_assessment,
        overtime_credit_assessment=overtime_credit_assessment,
        message="假期与考勤查询成功。",
    )


def _assess_leave_request(
    req: schemas.HrBalanceQueryRequest,
    leave_balance: schemas.LeaveBalance,
) -> schemas.LeaveRequestAssessment | None:
    """仅在请求字段完整时校验 ISO 日期，并按余额型假种返回稳定业务枚举。"""

    if req.leave_type is None or req.start_date is None or req.end_date is None:
        return None
    try:
        start_date = date.fromisoformat(req.start_date)
        end_date = date.fromisoformat(req.end_date)
    except ValueError:
        return schemas.LeaveRequestAssessment(
            status="invalid_date",
            leave_type=req.leave_type,
        )
    if end_date < start_date:
        return schemas.LeaveRequestAssessment(
            status="invalid_date",
            leave_type=req.leave_type,
        )
    requested_days = float((end_date - start_date).days + 1)
    balance_by_type = {
        "annual": leave_balance.annual,
        "personal": leave_balance.personal,
        "sick": leave_balance.sick,
        "compensatory": leave_balance.compensatory,
    }
    available_days = balance_by_type.get(req.leave_type)
    if available_days is None:
        return schemas.LeaveRequestAssessment(
            status="manual_review",
            leave_type=req.leave_type,
            requested_days=requested_days,
        )
    return schemas.LeaveRequestAssessment(
        status="sufficient" if available_days >= requested_days else "insufficient",
        leave_type=req.leave_type,
        requested_days=requested_days,
        available_days=available_days,
    )


def _assess_overtime_policy(
    req: schemas.HrBalanceQueryRequest,
) -> schemas.OvertimePolicyAssessment | None:
    """按已知制度校验审批和日期类型，1:1 仅按小时记账而不猜测日工时。"""

    overtime_fields = (
        req.overtime_date,
        req.overtime_duration_hours,
        req.overtime_day_type,
    )
    approval_status = req.pre_approval_status
    if approval_status is None and req.is_pre_approved is not None:
        approval_status = "approved" if req.is_pre_approved else "not_approved"
    if all(value is None for value in overtime_fields) and approval_status is None:
        return None
    if any(value is None for value in overtime_fields) or approval_status is None:
        return schemas.OvertimePolicyAssessment(status="manual_review")
    try:
        date.fromisoformat(req.overtime_date or "")
    except ValueError:
        return schemas.OvertimePolicyAssessment(status="invalid_date")
    if approval_status != "approved":
        return schemas.OvertimePolicyAssessment(status="preapproval_missing")
    if req.overtime_day_type == "statutory_holiday":
        return schemas.OvertimePolicyAssessment(status="statutory_holiday")
    duration_hours = float(req.overtime_duration_hours or 0)
    if req.overtime_day_type == "workday" and duration_hours < 2:
        return schemas.OvertimePolicyAssessment(status="workday_minimum_not_met")
    return schemas.OvertimePolicyAssessment(
        status="eligible",
        credited_hours=duration_hours,
    )


def _assess_overtime_credit(
    req: schemas.HrBalanceQueryRequest,
    leave_balance: schemas.LeaveBalance,
    policy: schemas.OvertimePolicyAssessment | None,
    request: schemas.LeaveRequestAssessment | None,
) -> schemas.OvertimeCreditAssessment | None:
    """按演示 HR 的八小时标准日统一比较加班小时、调休小时和已入账余额。"""

    if policy is None and req.leave_type != "compensatory":
        return None
    if policy is None or request is None:
        return schemas.OvertimeCreditAssessment(status="manual_review")
    if policy.status == "invalid_date" or request.status == "invalid_date":
        return schemas.OvertimeCreditAssessment(status="invalid_date")
    if policy.status != "eligible" or request.requested_days is None:
        return schemas.OvertimeCreditAssessment(status="manual_review")
    standard_hours_per_day = 8.0
    requested_hours = request.requested_days * standard_hours_per_day
    credited_hours = float(policy.credited_hours or 0)
    available_hours = leave_balance.compensatory * standard_hours_per_day
    sufficient = credited_hours >= requested_hours and available_hours >= requested_hours
    return schemas.OvertimeCreditAssessment(
        status="sufficient" if sufficient else "insufficient",
        standard_hours_per_day=standard_hours_per_day,
        requested_days=request.requested_days,
        requested_hours=requested_hours,
        credited_hours=credited_hours,
        available_hours=available_hours,
    )


def _certificate(req: schemas.HrCertificateIssueRequest) -> schemas.HrCertificateIssueResponse:
    """生成演示在职证明编号、正文和下载地址。"""

    cert_id = _id("CERT")
    employee_display_name = req.employee_name or req.employee_id
    certificate_labels = {
        "employment": "在职证明",
        "income": "收入证明",
        "employment_income": "在职及收入证明",
    }
    certificate_label = certificate_labels[req.cert_type]
    if req.language == "en":
        content = (
            f"Demo {certificate_label}: employee {employee_display_name}; "
            "this mock document is not valid for official use."
        )
    else:
        content = (
            f"演示{certificate_label}：员工 {employee_display_name}；"
            "该 Mock 文件仅用于流程演示，不具备正式证明效力。"
        )
    return schemas.HrCertificateIssueResponse(
        cert_id=cert_id,
        status="issued",
        cert_type=req.cert_type,
        content=content,
        download_url=f"/api/mock/files/{cert_id}.pdf",
        message="演示证明已开具。",
        issued_at=_now(),
    )


def _leave(req: schemas.HrLeaveApplyRequest) -> schemas.HrLeaveApplyResponse:
    """返回请假申请已提交且等待审批的业务状态。"""

    return schemas.HrLeaveApplyResponse(
        application_id=_id("LEAVE"),
        status="pending",
        approver="直属主管",
        message="请假申请已提交。",
        submitted_at=_now(),
    )


def _invoice(req: schemas.InvoiceVerifyRequest) -> schemas.InvoiceVerifyResponse:
    """按必要字段完整性返回演示发票查验结果。"""

    missing = [name for name in ("invoice_date", "amount") if getattr(req, name) is None]
    authentic = req.invoice_number != "00000000"
    amount_matches = (
        abs(req.amount - req.expected_amount) < 0.01
        if req.amount is not None and req.expected_amount is not None
        else None
    )
    return schemas.InvoiceVerifyResponse(
        authentic=authentic,
        fields_complete=not missing,
        amount_matches=amount_matches,
        missing_fields=missing,
        risk_level="high" if not authentic else ("low" if not missing else "medium"),
        message="发票查验完成。" if authentic else "演示发票号码命中无效票据规则。",
    )


def _permission(req: schemas.PermissionGrantRequest) -> schemas.PermissionGrantResponse:
    """返回由 SOP 前置确认或高权限审批授权后的确定性开通回执。"""

    return schemas.PermissionGrantResponse(
        grant_id=_id("GRANT"),
        status="granted",
        system=req.system,
        permission=req.permission,
        approver="系统管理员",
        effective_at=_now(),
        message="权限申请已处理。",
    )


_TELECOM_CIRCUIT_FIXTURES = {
    "CU-DEMO-3701": {
        "customer_code": "CUST-DEMO-1001",
        "customer_name_masked": "山东演示制造有限公司",
        "service_status": "active",
        "service_type": "政企互联网专线（演示）",
    },
    "CU-DEMO-3702": {
        "customer_code": "CUST-DEMO-1001",
        "customer_name_masked": "山东演示制造有限公司",
        "service_status": "inactive",
        "service_type": "政企互联网专线（演示）",
    },
}


def _telecom_circuit_verify(
    req: schemas.TelecomCircuitVerifyRequest,
) -> schemas.TelecomCircuitVerifyResponse:
    """按固定虚构线路返回匹配、不匹配、停用或未找到状态。"""

    fixture = _TELECOM_CIRCUIT_FIXTURES.get(req.circuit_no)
    if fixture is None:
        return schemas.TelecomCircuitVerifyResponse(
            status="not_found",
            customer_code=req.customer_code,
            circuit_no=req.circuit_no,
            service_status="unknown",
            message="演示线路数据集中未找到该线路，不能继续自动受理。",
        )
    if req.customer_code != fixture["customer_code"]:
        return schemas.TelecomCircuitVerifyResponse(
            status="mismatch",
            customer_code=req.customer_code,
            customer_name_masked=None,
            circuit_no=req.circuit_no,
            service_status="unknown",
            service_type=str(fixture["service_type"]),
            message="客户编码与演示线路归属不一致，请转人工核对。",
        )
    service_status = str(fixture["service_status"])
    return schemas.TelecomCircuitVerifyResponse(
        status="matched" if service_status == "active" else "inactive",
        customer_code=req.customer_code,
        customer_name_masked=str(fixture["customer_name_masked"]),
        circuit_no=req.circuit_no,
        service_status="active" if service_status == "active" else "inactive",
        service_type=str(fixture["service_type"]),
        message=(
            "演示客户与线路核验通过。"
            if service_status == "active"
            else "演示线路已停用，不能继续普通故障申告。"
        ),
    )


def _telecom_fault_create(
    req: schemas.TelecomFaultCreateRequest,
) -> schemas.TelecomFaultCreateResponse:
    """核验演示线路并按结构化事实定级，再以幂等键生成稳定工单编号。"""

    circuit = _telecom_circuit_verify(
        schemas.TelecomCircuitVerifyRequest(
            customer_code=req.customer_code,
            circuit_no=req.circuit_no,
        )
    )
    severity = (
        "P1"
        if req.symptom_type == "total_outage" and req.business_impact == "critical"
        else "P2"
        if req.symptom_type in {"intermittent", "high_latency", "packet_loss"}
        or req.business_impact == "high"
        else "P3"
    )
    if circuit.status != "matched":
        return schemas.TelecomFaultCreateResponse(
            status="rejected",
            severity=severity,
            message="客户与线路未通过演示核验，故障工单未受理。",
        )
    stable_key = req.idempotency_key or "|".join(
        (
            req.customer_code,
            req.circuit_no,
            req.fault_started_at,
            req.symptom_type,
            req.affected_scope,
        )
    )
    digest = sha256(stable_key.encode("utf-8")).hexdigest()[:12].upper()
    response_minutes = {"P1": 15, "P2": 30, "P3": 120}[severity]
    return schemas.TelecomFaultCreateResponse(
        ticket_id=f"TEL-{digest}",
        status="accepted",
        severity=severity,
        accepted_at=_now(),
        expected_first_response_minutes=response_minutes,
        contact_channel="政企服务演示专席",
        message="政企专线故障演示工单已受理；响应时限仅为演示值。",
    )


def _ticket(req: schemas.TicketCreateRequest) -> schemas.TicketCreateResponse:
    """创建演示 IT 工单并按优先级返回服务时限。"""

    sla = {"low": "2个工作日", "medium": "8小时", "high": "4小时", "urgent": "1小时"}[req.priority]
    return schemas.TicketCreateResponse(
        ticket_id=_id("TICKET"),
        status="created",
        priority=req.priority,
        category=req.category,
        assignee="IT 服务台",
        sla=sla,
        message="IT 工单已登记。",
        created_at=_now(),
    )


def _ticket_close(req: schemas.TicketCloseRequest) -> schemas.TicketCloseResponse:
    """在报修人确认恢复后返回确定性的工单关闭回执。"""

    return schemas.TicketCloseResponse(
        ticket_id=req.ticket_id,
        status="closed",
        closed_by_employee_id=req.requester_employee_id,
        message="报修人已确认恢复，IT 工单已关闭。",
        closed_at=_now(),
    )


def _ticket_reopen(req: schemas.TicketReopenRequest) -> schemas.TicketReopenResponse:
    """在报修人确认未恢复后返回重新进入服务台队列的回执。"""

    return schemas.TicketReopenResponse(
        ticket_id=req.ticket_id,
        status="reopened",
        queue="IT 服务台",
        message="报修人反馈仍未解决，工单已重新打开。",
        reopened_at=_now(),
    )


Handler = Callable[[Any], BaseModel]


class _Definition:
    def __init__(
        self,
        name: str,
        display_name: str,
        description: str,
        path: str,
        request_model: type[BaseModel],
        response_model: type[BaseModel],
        handler: Handler,
    ):
        """冻结一个公共 mock 的名称、模型、处理器和能力描述。"""

        self.name, self.request_model, self.response_model, self.handler = (
            name,
            request_model,
            response_model,
            handler,
        )
        self.capability = schemas.PublicMockCapability(
            name=name,
            display_name=display_name,
            description=description,
            path=path,
            input_schema=request_model.model_json_schema(),
            output_schema=response_model.model_json_schema(),
        )


_DEFINITIONS = (
    _Definition(
        "admin.room_book",
        "会议室预订",
        "查询并预订会议室。",
        "/api/mock/admin/room_book",
        schemas.RoomBookRequest,
        schemas.RoomBookResponse,
        _room_book,
    ),
    _Definition(
        "admin.supply_request",
        "办公用品申领",
        "办公用品申领登记。",
        "/api/mock/admin/supply_request",
        schemas.SupplyRequest,
        schemas.SupplyResponse,
        _supply_request,
    ),
    _Definition(
        "contract.archive_query",
        "合同参考资料检索",
        "按关键词检索演示合同、条款和复盘资料。",
        "/api/mock/contract/archive_query",
        schemas.ContractArchiveQueryRequest,
        schemas.ContractArchiveQueryResponse,
        _contract_query,
    ),
    _Definition(
        "contract.risk_assess",
        "合同风险初筛",
        "按显式演示规则返回结构化合同风险信号。",
        "/api/mock/contract/risk_assess",
        schemas.ContractRiskAssessRequest,
        schemas.ContractRiskAssessResponse,
        _contract_risk_assess,
    ),
    _Definition(
        "partner.due_diligence_query",
        "合作方入库尽调",
        "按固定虚构主体返回工商、涉诉、执行和演示黑名单结构化回执。",
        "/api/mock/partner/due_diligence_query",
        schemas.PartnerDueDiligenceRequest,
        schemas.PartnerDueDiligenceResponse,
        _partner_due_diligence,
    ),
    _Definition(
        "expense.submit",
        "报销单提交",
        "提交报销单。",
        "/api/mock/expense/submit",
        schemas.ExpenseSubmitRequest,
        schemas.ExpenseSubmitResponse,
        _expense_submit,
    ),
    _Definition(
        "expense.travel_policy_assess",
        "差旅住宿标准评估",
        "按境内普通员工住宿标准评估报销金额。",
        "/api/mock/expense/travel_policy_assess",
        schemas.TravelPolicyAssessRequest,
        schemas.TravelPolicyAssessResponse,
        _travel_policy_assess,
    ),
    _Definition(
        "expense.quota_query",
        "报销额度查询",
        "查询员工报销额度。",
        "/api/mock/expense/quota_query",
        schemas.ExpenseQuotaQueryRequest,
        schemas.ExpenseQuotaQueryResponse,
        _expense_quota,
    ),
    _Definition(
        "hr.balance_query",
        "假期考勤查询",
        "查询假期余额与考勤。",
        "/api/mock/hr/balance_query",
        schemas.HrBalanceQueryRequest,
        schemas.HrBalanceQueryResponse,
        _hr_balance,
    ),
    _Definition(
        "hr.cert_issue",
        "在职收入证明开具",
        "开具在职或收入证明。",
        "/api/mock/hr/cert_issue",
        schemas.HrCertificateIssueRequest,
        schemas.HrCertificateIssueResponse,
        _certificate,
    ),
    _Definition(
        "hr.leave_apply",
        "请假调休申请",
        "提交请假或调休申请。",
        "/api/mock/hr/leave_apply",
        schemas.HrLeaveApplyRequest,
        schemas.HrLeaveApplyResponse,
        _leave,
    ),
    _Definition(
        "invoice.verify",
        "发票查验",
        "校验发票真伪与完整性。",
        "/api/mock/invoice/verify",
        schemas.InvoiceVerifyRequest,
        schemas.InvoiceVerifyResponse,
        _invoice,
    ),
    _Definition(
        "it.grant_permission",
        "系统权限开通",
        "申请系统权限。",
        "/api/mock/it/grant_permission",
        schemas.PermissionGrantRequest,
        schemas.PermissionGrantResponse,
        _permission,
    ),
    _Definition(
        "telecom.circuit.verify.training",
        "政企专线核验（演示）",
        "核验虚构政企客户编码和演示线路归属。",
        "/api/mock/telecom/circuit_verify",
        schemas.TelecomCircuitVerifyRequest,
        schemas.TelecomCircuitVerifyResponse,
        _telecom_circuit_verify,
    ),
    _Definition(
        "telecom.enterprise_fault.create.training",
        "政企专线故障申告（演示）",
        "按结构化故障事实和幂等键创建演示受理工单。",
        "/api/mock/telecom/enterprise_fault_create",
        schemas.TelecomFaultCreateRequest,
        schemas.TelecomFaultCreateResponse,
        _telecom_fault_create,
    ),
    _Definition(
        "it.ticket_create",
        "IT工单登记",
        "登记 IT 工单。",
        "/api/mock/it/ticket_create",
        schemas.TicketCreateRequest,
        schemas.TicketCreateResponse,
        _ticket,
    ),
    _Definition(
        "it.ticket_close",
        "IT工单关闭",
        "由原报修人确认恢复并关闭工单。",
        "/api/mock/it/ticket_close",
        schemas.TicketCloseRequest,
        schemas.TicketCloseResponse,
        _ticket_close,
    ),
    _Definition(
        "it.ticket_reopen",
        "IT工单重开",
        "由原报修人反馈未恢复并重新打开工单。",
        "/api/mock/it/ticket_reopen",
        schemas.TicketReopenRequest,
        schemas.TicketReopenResponse,
        _ticket_reopen,
    ),
)

_BY_NAME = {item.name: item for item in _DEFINITIONS}
PUBLIC_MOCK_CAPABILITIES = tuple(item.capability for item in _DEFINITIONS)


def execute_public_mock(
    tool_name: str,
    payload: dict[str, Any],
    text_generator: TextGenerator | None = None,
) -> BaseModel:
    """校验并执行一个确定性公共 mock，按需润色非控制型文案。"""
    definition = _BY_NAME[tool_name]
    request = definition.request_model.model_validate(payload)
    result = definition.response_model.model_validate(definition.handler(request))
    if text_generator is None:
        return result
    data = result.model_dump()
    if isinstance(data.get("message"), str):
        data["message"] = polish_text(
            tool_name,
            data["message"],
            {"field": "message", "request": request.model_dump(), "response": data},
            text_generator,
        )
    for item in data.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            item["summary"] = polish_text(
                tool_name,
                item["summary"],
                {"field": "summary", "request": request.model_dump(), "result": item},
                text_generator,
            )
    return definition.response_model.model_validate(data)
