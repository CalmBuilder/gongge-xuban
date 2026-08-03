"""
@Time       : 2026/07/22 13:28
@Author     : zhanglp8181
@File       : schemas.py
@CallChain  : ToolExecutor → public-mock API → 请求/响应契约
@Description: 定义受控公共 mock 的严格业务工具输入输出，包括通信专线与 IT 工单生命周期。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoomBookRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    date: str
    start_time: str
    end_time: str
    attendees: int | None = Field(default=None, ge=1)
    equipment: list[str] = Field(default_factory=list)
    room_preference: str | None = None
    topic: str | None = None


class RoomAlternative(StrictModel):
    room_name: str
    capacity: int
    location: str


class RoomBookResponse(StrictModel):
    booking_id: str
    status: Literal["booked", "waitlist", "unavailable"]
    room_name: str
    capacity: int
    location: str
    date: str
    time_slot: str
    alternatives: list[RoomAlternative] = Field(default_factory=list)
    message: str


class SupplyItem(StrictModel):
    name: str
    quantity: int = Field(ge=1)
    unit: str | None = None


class SupplyRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    department: str | None = None
    items: list[SupplyItem] = Field(min_length=1)
    reason: str | None = None
    needed_by: str | None = None


class ApprovedSupplyItem(StrictModel):
    name: str
    requested: int
    approved: int
    note: str = ""


class SupplyResponse(StrictModel):
    request_id: str
    status: Literal["approved", "pending", "partial", "rejected"]
    approved_items: list[ApprovedSupplyItem]
    pickup_location: str
    message: str
    submitted_at: str


class ContractArchiveQueryRequest(StrictModel):
    query: str = Field(min_length=1)
    doc_type: Literal["contract", "case", "all"] = "all"
    keywords: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class ContractResult(StrictModel):
    doc_id: str
    doc_type: Literal["contract", "case"]
    title: str
    date: str
    parties: list[str]
    summary: str
    relevance: float
    citation: str


class ContractArchiveQueryResponse(StrictModel):
    query: str
    total: int
    results: list[ContractResult]
    message: str


class ContractRiskAssessRequest(StrictModel):
    contract_type: Literal["software_procurement", "service", "sales", "other"]
    contract_content: str = Field(min_length=20)
    review_scope: Literal["key_clauses", "full_text"] = "key_clauses"


class ContractRiskPoint(StrictModel):
    code: str
    title: str
    severity: Literal["low", "medium", "high"]
    evidence: str
    suggestion: str


class ContractRiskAssessResponse(StrictModel):
    assessment_id: str
    status: Literal["assessed", "insufficient"]
    risk_level: Literal["low", "medium", "high", "unknown"]
    risk_points: list[ContractRiskPoint]
    reference_codes: list[str]
    requires_human_review: bool
    message: str
    assessed_at: str


class PartnerDueDiligenceRequest(StrictModel):
    """约束合作方演示尽调所需的企业全称和有效格式信用代码。"""

    company_name: str = Field(min_length=2, max_length=200)
    unified_social_credit_code: str = Field(
        min_length=18,
        max_length=18,
        pattern=r"^[0-9A-HJ-NP-RT-UWXY]{18}$",
    )


class PartnerRiskFlag(StrictModel):
    """描述演示尽调命中的单项结构化风险事实。"""

    code: str
    title: str
    severity: Literal["medium", "high"]
    evidence: str


class PartnerDueDiligenceResponse(StrictModel):
    """返回主体匹配、风险事实、证据时间和保守建议。"""

    check_id: str
    status: Literal["assessed", "not_found", "identity_mismatch"]
    subject_name: str
    unified_social_credit_code: str
    subject_status: Literal["active", "abnormal", "unknown"]
    credit_code_match: bool
    litigation_count: int = Field(ge=0)
    enforcement_count: int = Field(ge=0)
    blacklisted: bool
    risk_level: Literal["low", "high", "unknown"]
    risk_flags: list[PartnerRiskFlag]
    recommendation: Literal["pass", "human_review", "insufficient"]
    requires_human_review: bool
    evidence_as_of: str
    evidence_sources: list[str]
    message: str


class TravelPolicyAssessRequest(StrictModel):
    """定义境内普通员工住宿费的结构化差旅标准评估输入。"""

    employee_id: str
    destination_city: str = Field(min_length=2)
    trip_start_date: str
    trip_end_date: str
    expense_category: Literal["lodging"]
    claimed_amount: float = Field(gt=0)
    trip_scope: Literal["domestic", "overseas"] = "domestic"
    trip_approval_status: Literal["approved", "not_approved"]
    trip_approval_number: str = Field(min_length=8)


class TravelPolicyAssessResponse(StrictModel):
    """返回可直接驱动确定性分支的住宿标准与超标金额。"""

    status: Literal[
        "within_limit",
        "over_limit",
        "unsupported",
        "unsupported_employee",
        "approval_unverified",
        "late_submission",
        "invalid_date",
    ]
    policy_code: str
    employee_level: Literal["staff"] | None = None
    city_tier: Literal["tier_1", "tier_2", "tier_3"] | None = None
    lodging_nights: int | None = Field(default=None, ge=1)
    nightly_limit: float | None = Field(default=None, gt=0)
    allowance_limit: float | None = Field(default=None, gt=0)
    claimed_amount: float
    over_limit_amount: float = Field(ge=0)
    approval_verified: bool
    submission_deadline: str | None = None
    days_since_trip_end: int | None = None
    message: str


class ExpenseSubmitRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    category: str
    amount: float = Field(gt=0)
    currency: str = "CNY"
    invoice_no: str | None = None
    expense_date: str | None = None
    description: str | None = None


class ExpenseSubmitResponse(StrictModel):
    expense_id: str
    status: Literal["accepted", "pending", "rejected"]
    message: str
    submitted_at: str
    submitted_at: str


class ExpenseQuotaQueryRequest(StrictModel):
    employee_id: str
    month: str | None = None


class ExpenseQuotaQueryResponse(StrictModel):
    employee_id: str
    month: str
    total_quota: float
    used: float
    remaining: float
    currency: str
    message: str


class HrBalanceQueryRequest(StrictModel):
    employee_id: str
    month: str | None = None
    include_attendance: bool = True
    leave_type: Literal[
        "annual", "personal", "sick", "compensatory", "marriage", "maternity", "other"
    ] | None = None
    start_date: str | None = None
    end_date: str | None = None
    overtime_date: str | None = None
    overtime_duration_hours: float | None = Field(default=None, gt=0)
    overtime_day_type: Literal["workday", "rest_day", "statutory_holiday"] | None = None
    is_pre_approved: bool | None = None
    pre_approval_status: Literal["approved", "not_approved"] | None = None


class LeaveBalance(StrictModel):
    annual: float
    compensatory: float
    sick: float
    personal: float


class Attendance(StrictModel):
    work_days: int
    actual_days: int
    late_count: int
    early_leave_count: int
    absent_days: float
    overtime_hours: float


class LeaveRequestAssessment(StrictModel):
    """描述指定日期范围对应的自然日数和余额充分性。"""

    status: Literal["sufficient", "insufficient", "manual_review", "invalid_date"]
    leave_type: str
    requested_days: float | None = None
    available_days: float | None = None


class OvertimePolicyAssessment(StrictModel):
    """描述加班事实是否满足当前结构化调休政策，不臆造小时到工作日换算。"""

    status: Literal[
        "eligible",
        "preapproval_missing",
        "workday_minimum_not_met",
        "statutory_holiday",
        "invalid_date",
        "manual_review",
    ]
    conversion_ratio: Literal["1:1"] = "1:1"
    credit_unit: Literal["hour"] = "hour"
    credited_hours: float | None = None


class OvertimeCreditAssessment(StrictModel):
    """用统一小时口径描述本次加班额度是否足以覆盖计划调休。"""

    status: Literal["sufficient", "insufficient", "manual_review", "invalid_date"]
    standard_hours_per_day: float = Field(default=8, gt=0)
    requested_days: float | None = None
    requested_hours: float | None = None
    credited_hours: float | None = None
    available_hours: float | None = None


class HrBalanceQueryResponse(StrictModel):
    employee_id: str
    month: str
    leave_balance: LeaveBalance
    attendance: Attendance | None = None
    request_assessment: LeaveRequestAssessment | None = None
    overtime_policy_assessment: OvertimePolicyAssessment | None = None
    overtime_credit_assessment: OvertimeCreditAssessment | None = None
    message: str


class HrCertificateIssueRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    cert_type: Literal["employment", "income", "employment_income"]
    purpose: str | None = None
    language: Literal["zh", "en"] = "zh"
    include_income: bool = False


class HrCertificateIssueResponse(StrictModel):
    cert_id: str
    status: Literal["issued", "pending", "rejected"]
    cert_type: str
    content: str
    download_url: str
    message: str
    issued_at: str


class HrLeaveApplyRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    leave_type: Literal[
        "annual", "personal", "sick", "compensatory", "marriage", "maternity", "other"
    ]
    start_date: str
    end_date: str
    days: float | None = Field(default=None, gt=0)
    reason: str | None = None


class HrLeaveApplyResponse(StrictModel):
    application_id: str
    status: Literal["approved", "pending", "rejected"]
    approver: str
    message: str
    submitted_at: str


class InvoiceVerifyRequest(StrictModel):
    invoice_code: str
    invoice_number: str
    invoice_date: str | None = None
    amount: float | None = Field(default=None, ge=0)
    expected_amount: float | None = Field(default=None, ge=0)
    check_code: str | None = None
    seller: str | None = None
    buyer: str | None = None


class InvoiceVerifyResponse(StrictModel):
    authentic: bool
    fields_complete: bool
    amount_matches: bool | None = None
    missing_fields: list[str]
    risk_level: Literal["low", "medium", "high"]
    message: str


class PermissionGrantRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    system: str
    permission: str
    access_level: Literal["read", "write", "admin"] = "read"
    reason: str | None = None
    duration: str | None = None


class PermissionGrantResponse(StrictModel):
    grant_id: str
    status: Literal["granted", "pending", "rejected"]
    system: str
    permission: str
    approver: str
    effective_at: str
    message: str


class TelecomCircuitVerifyRequest(StrictModel):
    """约束演示政企客户编码和专线编号的核验输入。"""

    customer_code: str = Field(min_length=1, max_length=64)
    circuit_no: str = Field(min_length=1, max_length=64)


class TelecomCircuitVerifyResponse(StrictModel):
    """返回演示客户线路匹配状态和脱敏后的线路摘要。"""

    status: Literal["matched", "not_found", "mismatch", "inactive"]
    customer_code: str
    customer_name_masked: str | None = None
    circuit_no: str
    service_status: Literal["active", "inactive", "unknown"]
    service_type: str | None = None
    message: str


class TelecomFaultCreateRequest(StrictModel):
    """定义政企专线故障演示工单的完整结构化输入。"""

    customer_code: str = Field(min_length=1, max_length=64)
    circuit_no: str = Field(min_length=1, max_length=64)
    contact_name: str = Field(min_length=1, max_length=100)
    contact_phone: str = Field(min_length=6, max_length=32)
    fault_started_at: str = Field(min_length=1, max_length=64)
    symptom_type: Literal[
        "total_outage",
        "intermittent",
        "slow",
        "high_latency",
        "packet_loss",
        "other",
    ]
    affected_scope: str = Field(min_length=1, max_length=500)
    business_impact: Literal["critical", "high", "medium", "low"]
    fault_description: str = Field(min_length=1, max_length=2000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class TelecomFaultCreateResponse(StrictModel):
    """返回演示故障工单的受理、去重或业务拒绝回执。"""

    ticket_id: str | None = None
    status: Literal["accepted", "duplicate", "rejected"]
    severity: Literal["P1", "P2", "P3"]
    accepted_at: str | None = None
    expected_first_response_minutes: int | None = Field(default=None, ge=1)
    contact_channel: str | None = None
    message: str


class TicketCreateRequest(StrictModel):
    employee_id: str
    employee_name: str | None = None
    category: Literal["hardware", "software", "network", "account", "other"] = "other"
    title: str
    description: str | None = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    contact: str | None = None


class TicketCreateResponse(StrictModel):
    ticket_id: str
    status: Literal["created", "assigned", "pending"]
    priority: Literal["low", "medium", "high", "urgent"]
    category: str
    assignee: str
    sla: str
    message: str
    created_at: str


class TicketCloseRequest(StrictModel):
    ticket_id: str
    requester_employee_id: str


class TicketCloseResponse(StrictModel):
    ticket_id: str
    status: Literal["closed"]
    closed_by_employee_id: str
    message: str
    closed_at: str


class TicketReopenRequest(StrictModel):
    ticket_id: str
    requester_employee_id: str


class TicketReopenResponse(StrictModel):
    ticket_id: str
    status: Literal["reopened"]
    queue: str
    message: str
    reopened_at: str


class PublicMockCapability(BaseModel):
    name: str
    display_name: str
    description: str
    method: Literal["POST"] = "POST"
    path: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class PublicMockCatalog(BaseModel):
    tools: list[PublicMockCapability]
