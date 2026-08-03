"""
@Time       : 2026/07/22 13:28
@Author     : zhanglp8181
@File       : router.py
@CallChain  : HTTP ToolExecutor → API Key 校验 → public-mock service
@Description: 暴露受控公共 Mock 工具接口，包括政企专线、工单生命周期和演示证明下载。
"""

from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from app.config import get_settings
from app.public_mock import schemas
from app.public_mock.copywriter import TextGenerator, build_copywriter
from app.public_mock.service import PUBLIC_MOCK_CAPABILITIES, execute_public_mock


PUBLIC_MOCK_API_KEY_HEADER = "X-API-Key"

router = APIRouter(tags=["public-mock"])


def require_public_mock_api_key(
    api_key: str | None = Header(default=None, alias=PUBLIC_MOCK_API_KEY_HEADER),
) -> None:
    """要求公共 Mock 的发现和调用接口携带独立 API Key。"""
    expected = get_settings().public_mock_api_key
    if api_key is None or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Public mock authentication required")


_AUTH = [Depends(require_public_mock_api_key)]


@router.get("/health")
def public_mock_health() -> dict[str, int | str]:
    """返回公共 Mock 服务健康状态和能力数量。"""

    return {"status": "ok", "tools": len(PUBLIC_MOCK_CAPABILITIES)}


@router.get("/api/tools", response_model=schemas.PublicMockCatalog, dependencies=_AUTH)
def list_public_mock_tools() -> schemas.PublicMockCatalog:
    """返回已注册公共 Mock 工具的结构化能力目录。"""

    return schemas.PublicMockCatalog(tools=list(PUBLIC_MOCK_CAPABILITIES))


@router.post(
    "/api/mock/admin/room_book", response_model=schemas.RoomBookResponse, dependencies=_AUTH
)
def room_book(
    request: schemas.RoomBookRequest, copywriter: TextGenerator | None = Depends(build_copywriter)
) -> schemas.RoomBookResponse:
    """根据结构化时间和人数返回演示会议室预订回执。"""

    return execute_public_mock("admin.room_book", request.model_dump(), copywriter)


@router.post(
    "/api/mock/admin/supply_request", response_model=schemas.SupplyResponse, dependencies=_AUTH
)
def supply_request(
    request: schemas.SupplyRequest, copywriter: TextGenerator | None = Depends(build_copywriter)
) -> schemas.SupplyResponse:
    """根据结构化用品清单返回演示申领回执。"""

    return execute_public_mock("admin.supply_request", request.model_dump(), copywriter)


@router.post(
    "/api/mock/contract/archive_query",
    response_model=schemas.ContractArchiveQueryResponse,
    dependencies=_AUTH,
)
def contract_archive_query(
    request: schemas.ContractArchiveQueryRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.ContractArchiveQueryResponse:
    """按查询条件返回演示合同档案检索结果。"""

    return execute_public_mock("contract.archive_query", request.model_dump(), copywriter)


@router.post(
    "/api/mock/contract/risk_assess",
    response_model=schemas.ContractRiskAssessResponse,
    dependencies=_AUTH,
)
def contract_risk_assess(
    request: schemas.ContractRiskAssessRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.ContractRiskAssessResponse:
    """按固定演示规则返回结构化合同风险初筛回执。"""

    return execute_public_mock("contract.risk_assess", request.model_dump(), copywriter)


@router.post(
    "/api/mock/partner/due_diligence_query",
    response_model=schemas.PartnerDueDiligenceResponse,
    dependencies=_AUTH,
)
def partner_due_diligence_query(
    request: schemas.PartnerDueDiligenceRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.PartnerDueDiligenceResponse:
    """按固定虚构主体返回结构化合作方尽调回执。"""

    return execute_public_mock(
        "partner.due_diligence_query",
        request.model_dump(),
        copywriter,
    )


@router.post(
    "/api/mock/expense/submit", response_model=schemas.ExpenseSubmitResponse, dependencies=_AUTH
)
def expense_submit(
    request: schemas.ExpenseSubmitRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.ExpenseSubmitResponse:
    """校验报销字段并返回演示报销受理回执。"""

    return execute_public_mock("expense.submit", request.model_dump(), copywriter)


@router.post(
    "/api/mock/expense/travel_policy_assess",
    response_model=schemas.TravelPolicyAssessResponse,
    dependencies=_AUTH,
)
def expense_travel_policy_assess(
    request: schemas.TravelPolicyAssessRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TravelPolicyAssessResponse:
    """返回境内普通员工住宿费的结构化标准评估。"""

    return execute_public_mock(
        "expense.travel_policy_assess",
        request.model_dump(),
        copywriter,
    )


@router.post(
    "/api/mock/expense/quota_query",
    response_model=schemas.ExpenseQuotaQueryResponse,
    dependencies=_AUTH,
)
def expense_quota_query(
    request: schemas.ExpenseQuotaQueryRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.ExpenseQuotaQueryResponse:
    """返回指定员工和月份的演示报销额度。"""

    return execute_public_mock("expense.quota_query", request.model_dump(), copywriter)


@router.post(
    "/api/mock/hr/balance_query", response_model=schemas.HrBalanceQueryResponse, dependencies=_AUTH
)
def hr_balance_query(
    request: schemas.HrBalanceQueryRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.HrBalanceQueryResponse:
    """返回指定员工的演示假期余额和可选考勤数据。"""

    return execute_public_mock("hr.balance_query", request.model_dump(), copywriter)


@router.post(
    "/api/mock/hr/cert_issue", response_model=schemas.HrCertificateIssueResponse, dependencies=_AUTH
)
def hr_cert_issue(
    request: schemas.HrCertificateIssueRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.HrCertificateIssueResponse:
    """根据受控参数生成不具备正式效力的演示证明回执。"""

    return execute_public_mock("hr.cert_issue", request.model_dump(), copywriter)


@router.get("/api/mock/files/{cert_id}.pdf")
def download_demo_certificate(cert_id: str) -> Response:
    """返回不含个人信息的最小演示 PDF，并拒绝伪造格式的证明编号。"""

    if re.fullmatch(r"CERT-[A-F0-9]{12}", cert_id) is None:
        raise HTTPException(status_code=404, detail="Demo certificate not found")
    pdf_content = _build_demo_certificate_pdf(cert_id)
    return Response(
        content=pdf_content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{cert_id}.pdf"'},
    )


def _build_demo_certificate_pdf(cert_id: str) -> bytes:
    """构造仅包含编号和免责声明的有效单页 PDF，避免在下载链路暴露员工信息。"""

    text = f"DEMO CERTIFICATE {cert_id} - NOT VALID FOR OFFICIAL USE"
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


@router.post(
    "/api/mock/hr/leave_apply", response_model=schemas.HrLeaveApplyResponse, dependencies=_AUTH
)
def hr_leave_apply(
    request: schemas.HrLeaveApplyRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.HrLeaveApplyResponse:
    """校验请假字段并返回演示申请受理回执。"""

    return execute_public_mock("hr.leave_apply", request.model_dump(), copywriter)


@router.post(
    "/api/mock/invoice/verify", response_model=schemas.InvoiceVerifyResponse, dependencies=_AUTH
)
def invoice_verify(
    request: schemas.InvoiceVerifyRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.InvoiceVerifyResponse:
    """根据结构化发票字段返回演示查验结果。"""

    return execute_public_mock("invoice.verify", request.model_dump(), copywriter)


@router.post(
    "/api/mock/it/grant_permission",
    response_model=schemas.PermissionGrantResponse,
    dependencies=_AUTH,
)
def grant_permission(
    request: schemas.PermissionGrantRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.PermissionGrantResponse:
    """根据受控权限参数返回演示授权系统回执。"""

    return execute_public_mock("it.grant_permission", request.model_dump(), copywriter)


@router.post(
    "/api/mock/telecom/circuit_verify",
    response_model=schemas.TelecomCircuitVerifyResponse,
    dependencies=_AUTH,
)
def telecom_circuit_verify(
    request: schemas.TelecomCircuitVerifyRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TelecomCircuitVerifyResponse:
    """核验虚构政企客户与演示专线的归属和启用状态。"""

    return execute_public_mock(
        "telecom.circuit.verify.training",
        request.model_dump(),
        copywriter,
    )


@router.post(
    "/api/mock/telecom/enterprise_fault_create",
    response_model=schemas.TelecomFaultCreateResponse,
    dependencies=_AUTH,
)
def telecom_enterprise_fault_create(
    request: schemas.TelecomFaultCreateRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TelecomFaultCreateResponse:
    """按演示线路、分级和幂等键返回政企故障受理回执。"""

    return execute_public_mock(
        "telecom.enterprise_fault.create.training",
        request.model_dump(),
        copywriter,
    )


@router.post(
    "/api/mock/it/ticket_create", response_model=schemas.TicketCreateResponse, dependencies=_AUTH
)
def ticket_create(
    request: schemas.TicketCreateRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TicketCreateResponse:
    """根据故障描述创建演示工单并返回受理状态。"""

    return execute_public_mock("it.ticket_create", request.model_dump(), copywriter)


@router.post(
    "/api/mock/it/ticket_close", response_model=schemas.TicketCloseResponse, dependencies=_AUTH
)
def ticket_close(
    request: schemas.TicketCloseRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TicketCloseResponse:
    """校验报修人关闭请求并返回不可编造的 closed 回执。"""

    return execute_public_mock("it.ticket_close", request.model_dump(), copywriter)


@router.post(
    "/api/mock/it/ticket_reopen", response_model=schemas.TicketReopenResponse, dependencies=_AUTH
)
def ticket_reopen(
    request: schemas.TicketReopenRequest,
    copywriter: TextGenerator | None = Depends(build_copywriter),
) -> schemas.TicketReopenResponse:
    """校验报修人重开请求并返回重新排队回执。"""

    return execute_public_mock("it.ticket_reopen", request.model_dump(), copywriter)
