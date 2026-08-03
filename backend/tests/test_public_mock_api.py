"""
@Time       : 2026/07/22 13:28
@Author     : zhanglp8181
@File       : test_public_mock_api.py
@CallChain  : pytest → public-mock API/service → ToolExecutor
@Description: 验证受控公共 mock 的认证、严格契约、工单生命周期回执和工具执行。
"""

from __future__ import annotations

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import Tool
from app.db.seed import seed_demo_data
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


SAMPLE_PAYLOADS = {
    "admin.room_book": {
        "employee_id": "E001",
        "date": "2026-07-20",
        "start_time": "09:00",
        "end_time": "10:00",
    },
    "admin.supply_request": {"employee_id": "E001", "items": [{"name": "A4纸", "quantity": 2}]},
    "contract.archive_query": {"query": "软件采购合同", "top_k": 2},
    "contract.risk_assess": {
        "contract_type": "software_procurement",
        "contract_content": "供应商因任何违约造成的全部损失承担无限责任，采购方可单方任意解除合同。",
    },
    "partner.due_diligence_query": {
        "company_name": "共格演示科技有限公司",
        "unified_social_credit_code": "91370000MA3D3M001X",
    },
    "expense.submit": {"employee_id": "E001", "category": "travel", "amount": 128.5},
    "expense.travel_policy_assess": {
        "employee_id": "E002",
        "destination_city": "杭州",
        "trip_start_date": "2026-07-20",
        "trip_end_date": "2026-07-22",
        "expense_category": "lodging",
        "claimed_amount": 700,
        "trip_scope": "domestic",
        "trip_approval_status": "approved",
        "trip_approval_number": "TRIP-DEMO-APPROVED-001",
    },
    "expense.quota_query": {"employee_id": "E001", "month": "2026-07"},
    "hr.balance_query": {"employee_id": "E001", "month": "2026-07"},
    "hr.cert_issue": {"employee_id": "E001", "cert_type": "employment"},
    "hr.leave_apply": {
        "employee_id": "E001",
        "leave_type": "annual",
        "start_date": "2026-07-20",
        "end_date": "2026-07-21",
    },
    "invoice.verify": {"invoice_code": "044001", "invoice_number": "12345678"},
    "it.grant_permission": {"employee_id": "E001", "system": "CRM", "permission": "只读"},
    "telecom.circuit.verify.training": {
        "customer_code": "CUST-DEMO-1001",
        "circuit_no": "CU-DEMO-3701",
    },
    "telecom.enterprise_fault.create.training": {
        "customer_code": "CUST-DEMO-1001",
        "circuit_no": "CU-DEMO-3701",
        "contact_name": "李经理",
        "contact_phone": "13800000000",
        "fault_started_at": "2026-07-28T09:05:00+08:00",
        "symptom_type": "total_outage",
        "affected_scope": "济南、青岛两个园区的收银和订单系统",
        "business_impact": "critical",
        "fault_description": "两个园区专线完全中断，重启接入设备后仍未恢复",
        "idempotency_key": "demo-fault-CUST-DEMO-1001-CU-DEMO-3701-20260728T0905",
    },
    "it.ticket_create": {"employee_id": "E001", "title": "VPN 无法连接"},
    "it.ticket_close": {"ticket_id": "TICKET-DEMO", "requester_employee_id": "E001"},
    "it.ticket_reopen": {"ticket_id": "TICKET-DEMO", "requester_employee_id": "E001"},
}


def _public_mock_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("PUBLIC_MOCK_API_KEY", "expected-key")
    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "false")
    from app.config import get_settings
    from app.public_mock.router import router

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_public_mock_api_key_dependency_rejects_missing_and_wrong_keys(monkeypatch) -> None:
    from app.public_mock.router import require_public_mock_api_key

    monkeypatch.setenv("PUBLIC_MOCK_API_KEY", "expected-key")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        for supplied in (None, "wrong-key"):
            with pytest.raises(HTTPException) as exc_info:
                require_public_mock_api_key(supplied)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Public mock authentication required"
    finally:
        get_settings.cache_clear()


def test_public_mock_api_key_dependency_accepts_configured_key(monkeypatch) -> None:
    from app.public_mock.router import require_public_mock_api_key

    monkeypatch.setenv("PUBLIC_MOCK_API_KEY", "expected-key")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert require_public_mock_api_key("expected-key") is None
    finally:
        get_settings.cache_clear()


def test_public_mock_registry_contains_all_supported_capabilities() -> None:
    """验证能力目录包含历史、通信专线和工单生命周期工具且路径唯一。"""
    from app.public_mock.service import PUBLIC_MOCK_CAPABILITIES

    assert {item.name for item in PUBLIC_MOCK_CAPABILITIES} == set(SAMPLE_PAYLOADS)
    assert len({item.path for item in PUBLIC_MOCK_CAPABILITIES}) == 18
    assert all(item.input_schema.get("type") == "object" for item in PUBLIC_MOCK_CAPABILITIES)
    assert all(item.output_schema.get("type") == "object" for item in PUBLIC_MOCK_CAPABILITIES)


@pytest.mark.parametrize(("tool_name", "payload"), SAMPLE_PAYLOADS.items())
def test_public_mock_service_validates_and_executes_every_capability(tool_name, payload) -> None:
    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(tool_name, payload)

    assert result.model_dump()


def test_public_mock_request_models_are_strict() -> None:
    from app.public_mock.schemas import ExpenseQuotaQueryRequest

    with pytest.raises(ValidationError):
        ExpenseQuotaQueryRequest.model_validate({})
    with pytest.raises(ValidationError):
        ExpenseQuotaQueryRequest.model_validate({"employee_id": "E001", "unexpected": True})


def test_public_mock_query_results_are_deterministic() -> None:
    from app.public_mock.service import execute_public_mock

    first = execute_public_mock("expense.quota_query", SAMPLE_PAYLOADS["expense.quota_query"])
    second = execute_public_mock("expense.quota_query", SAMPLE_PAYLOADS["expense.quota_query"])

    assert first == second
    assert first.total_quota == 20_000
    assert first.remaining == first.total_quota - first.used


def test_contract_reference_query_returns_only_keyword_related_demo_records() -> None:
    """验证无限责任条款只命中责任限制演示资料，不再返回固定无关合同。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(
        "contract.archive_query",
        {"query": "供应商对任何违约承担无限责任", "top_k": 5},
    )

    assert result.total == 2
    assert all("责任" in item.title for item in result.results)
    assert all(item.citation.startswith("DEMO-") for item in result.results)
    assert "演示资料" in result.message


def test_contract_reference_query_returns_explicit_empty_result_for_unknown_clause() -> None:
    """验证无关键词匹配时返回零结果，不用默认案例伪装相关依据。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(
        "contract.archive_query",
        {"query": "完全未知的特殊条款主题"},
    )

    assert result.total == 0
    assert result.results == []
    assert result.message == "演示资料库没有找到匹配记录。"


def test_contract_risk_assessment_separates_low_and_high_risk_signals() -> None:
    """验证演示规则以结构化风险点区分普通保密条款和明显高风险表述。"""

    from app.public_mock.service import execute_public_mock

    low = execute_public_mock(
        "contract.risk_assess",
        {
            "contract_type": "software_procurement",
            "contract_content": (
                "双方对履约过程中知悉的商业秘密承担保密义务，未经书面同意不得披露，"
                "但法律法规要求披露的除外，保密义务持续三年。"
            ),
        },
    )
    high = execute_public_mock(
        "contract.risk_assess",
        {
            "contract_type": "software_procurement",
            "contract_content": (
                "供应商对任何违约承担无限责任，采购方可以无需理由单方任意解除合同，"
                "供应商不得提出异议。"
            ),
        },
    )

    assert low.status == "assessed"
    assert low.risk_level == "low"
    assert low.risk_points == []
    assert low.requires_human_review is False
    assert high.status == "assessed"
    assert high.risk_level == "high"
    assert high.requires_human_review is True
    assert {point.code for point in high.risk_points} == {
        "UNLIMITED_LIABILITY",
        "UNILATERAL_TERMINATION",
    }
    assert high.reference_codes == [
        "DEMO-CLAUSE-LIABILITY-001",
        "DEMO-REVIEW-TERMINATION-001",
    ]


def test_contract_risk_assessment_requires_enough_contract_text() -> None:
    """验证字段合法但信息量不足时返回 insufficient，而不是猜测风险等级。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(
        "contract.risk_assess",
        {
            "contract_type": "service",
            "contract_content": "这是一段长度足够进入接口但缺乏完整权利义务的合同描述文本。",
        },
    )

    assert result.status == "insufficient"
    assert result.risk_level == "unknown"
    assert result.risk_points == []


def test_partner_due_diligence_rejects_name_and_credit_code_mismatch() -> None:
    """验证已知信用代码与错误企业名称组合不会形成演示通过建议。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(
        "partner.due_diligence_query",
        {
            "company_name": "错误的演示企业名称有限公司",
            "unified_social_credit_code": "91370000MA3D3M001X",
        },
    )

    assert result.status == "identity_mismatch"
    assert result.credit_code_match is False
    assert result.risk_level == "unknown"
    assert result.recommendation == "insufficient"


def test_demo_certificate_download_is_real_pdf_without_employee_information(monkeypatch) -> None:
    """验证证明回执地址可下载 PDF，且文件不承载员工个人信息。"""

    client = _public_mock_client(monkeypatch)
    issue_response = client.post(
        "/api/mock/hr/cert_issue",
        headers={"X-API-Key": "expected-key"},
        json={
            "employee_id": "E002",
            "employee_name": "演示员工",
            "cert_type": "employment",
        },
    )
    assert issue_response.status_code == 200
    issued = issue_response.json()

    download_response = client.get(issued["download_url"])

    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/pdf"
    assert download_response.content.startswith(b"%PDF-1.4")
    assert b"E002" not in download_response.content
    assert "演示员工" not in download_response.content.decode("latin-1")


def test_demo_certificate_download_rejects_invalid_identifier(monkeypatch) -> None:
    """验证下载端点只接受系统生成的证明编号格式。"""

    response = _public_mock_client(monkeypatch).get("/api/mock/files/not-a-cert.pdf")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("tool_name", "field", "prefix"),
    [
        ("expense.submit", "expense_id", "EXP-"),
        ("hr.leave_apply", "application_id", "LEAVE-"),
        ("it.ticket_create", "ticket_id", "TICKET-"),
    ],
)
def test_public_mock_submit_ids_use_business_prefixes(tool_name, field, prefix) -> None:
    """验证写操作返回的业务凭证使用稳定类型前缀。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(tool_name, SAMPLE_PAYLOADS[tool_name])

    assert getattr(result, field).startswith(prefix)


def test_public_mock_copywriter_uses_fallback_when_disabled(monkeypatch) -> None:
    from app.config import get_settings
    from app.public_mock.copywriter import polish_text

    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert (
            polish_text("expense.quota_query", "固定文案", {}, lambda *_: "润色文案") == "固定文案"
        )
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("generated", [None, "", "   "])
def test_public_mock_copywriter_falls_back_for_invalid_output(monkeypatch, generated) -> None:
    from app.config import get_settings
    from app.public_mock.copywriter import polish_text

    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert (
            polish_text("expense.quota_query", "固定文案", {}, lambda *_: generated) == "固定文案"
        )
    finally:
        get_settings.cache_clear()


def test_public_mock_copywriter_falls_back_on_generator_error(monkeypatch) -> None:
    from app.config import get_settings
    from app.public_mock.copywriter import polish_text

    def fail(*_args):
        raise RuntimeError("model unavailable")

    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert polish_text("expense.quota_query", "固定文案", {}, fail) == "固定文案"
    finally:
        get_settings.cache_clear()


def test_public_mock_copywriter_changes_only_text_fields(monkeypatch) -> None:
    from app.config import get_settings
    from app.public_mock.service import execute_public_mock

    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "true")
    get_settings.cache_clear()
    try:
        baseline = execute_public_mock(
            "expense.quota_query", SAMPLE_PAYLOADS["expense.quota_query"]
        )
        polished = execute_public_mock(
            "expense.quota_query",
            SAMPLE_PAYLOADS["expense.quota_query"],
            text_generator=lambda *_: "这是润色后的说明。",
        )
        assert polished.message == "这是润色后的说明。"
        assert polished.model_copy(update={"message": baseline.message}) == baseline
    finally:
        get_settings.cache_clear()


def test_public_mock_health_is_anonymous_and_reports_capability_count(monkeypatch) -> None:
    """验证匿名健康检查返回当前已注册公共 Mock 能力数量。"""

    client = _public_mock_client(monkeypatch)

    assert client.get("/health").json() == {"status": "ok", "tools": 18}


@pytest.mark.parametrize(
    ("customer_code", "circuit_no", "expected_status"),
    [
        ("CUST-DEMO-1001", "CU-DEMO-3701", "matched"),
        ("CUST-DEMO-9999", "CU-DEMO-3701", "mismatch"),
        ("CUST-DEMO-1001", "CU-DEMO-3702", "inactive"),
        ("CUST-DEMO-1001", "CU-DEMO-9999", "not_found"),
    ],
)
def test_telecom_circuit_verify_returns_conservative_structured_status(
    customer_code: str,
    circuit_no: str,
    expected_status: str,
) -> None:
    """验证线路核验不会为未知、不匹配或停用线路生成匹配结论。"""

    from app.public_mock.service import execute_public_mock

    result = execute_public_mock(
        "telecom.circuit.verify.training",
        {"customer_code": customer_code, "circuit_no": circuit_no},
    )

    assert result.status == expected_status


def test_telecom_fault_create_is_idempotent_and_rejects_unverified_circuit() -> None:
    """验证显式或派生幂等键稳定返回同一工单，未核验线路不产生业务编号。"""

    from app.public_mock.service import execute_public_mock

    payload = SAMPLE_PAYLOADS["telecom.enterprise_fault.create.training"]
    first = execute_public_mock("telecom.enterprise_fault.create.training", payload)
    repeated = execute_public_mock("telecom.enterprise_fault.create.training", payload)
    rejected = execute_public_mock(
        "telecom.enterprise_fault.create.training",
        {**payload, "customer_code": "CUST-DEMO-9999"},
    )

    assert first.status == "accepted"
    assert first.ticket_id == repeated.ticket_id
    assert first.ticket_id and first.ticket_id.startswith("TEL-")
    assert first.severity == "P1"
    assert first.expected_first_response_minutes == 15
    assert rejected.status == "rejected"
    assert rejected.ticket_id is None

    derived_payload = {key: value for key, value in payload.items() if key != "idempotency_key"}
    derived_first = execute_public_mock(
        "telecom.enterprise_fault.create.training", derived_payload
    )
    derived_repeated = execute_public_mock(
        "telecom.enterprise_fault.create.training", derived_payload
    )

    assert derived_first.status == "accepted"
    assert derived_first.ticket_id == derived_repeated.ticket_id


def test_public_mock_catalog_requires_api_key_and_lists_only_mock_capabilities(monkeypatch) -> None:
    client = _public_mock_client(monkeypatch)

    assert client.get("/api/tools").status_code == 401
    assert client.get("/api/tools", headers={"X-API-Key": "wrong"}).status_code == 401
    response = client.get("/api/tools", headers={"X-API-Key": "expected-key"})

    assert response.status_code == 200
    assert {item["name"] for item in response.json()["tools"]} == set(SAMPLE_PAYLOADS)


@pytest.mark.parametrize(("tool_name", "payload"), SAMPLE_PAYLOADS.items())
def test_public_mock_http_routes_are_authenticated_and_schema_validated(
    monkeypatch, tool_name, payload
) -> None:
    from app.public_mock.service import PUBLIC_MOCK_CAPABILITIES

    path = next(item.path for item in PUBLIC_MOCK_CAPABILITIES if item.name == tool_name)
    client = _public_mock_client(monkeypatch)

    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers={"X-API-Key": "expected-key"}, json={}).status_code == 422
    response = client.post(path, headers={"X-API-Key": "expected-key"}, json=payload)

    assert response.status_code == 200
    assert response.json()


def test_seeded_employee_tool_executes_through_public_mock_http_route(monkeypatch) -> None:
    client = _public_mock_client(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    class RoutedClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, headers=None, json=None, params=None):
            path = httpx.URL(url).raw_path.decode()
            response = client.request(method, path, headers=headers, json=json, params=params)
            return httpx.Response(
                response.status_code,
                json=response.json(),
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "Client", RoutedClient)
    with Session(engine) as db:
        seed_demo_data(db)
        db.commit()
        tool = db.exec(select(Tool).where(Tool.name == "expense.quota_query")).one()

        result = ToolExecutor(db).execute(
            "tenant_demo",
            ToolCall(name=tool.name, arguments={"employee_id": "E001", "month": "2026-07"}),
        )

        assert result.success is True
        assert result.data["employee_id"] == "E001"
        assert result.data["total_quota"] == 20_000
        from app.config import get_settings

        assert tool.url == f"{get_settings().normalized_tool_base_url}/api/mock/expense/quota_query"
        assert tool.headers_json == {"X-API-Key": "${secret.PUBLIC_MOCK_API_KEY}"}


def test_public_mock_http_route_uses_optional_copywriter_dependency(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_MOCK_API_KEY", "expected-key")
    monkeypatch.setenv("PUBLIC_MOCK_LLM_ENABLED", "true")
    from app.config import get_settings
    from app.public_mock.copywriter import build_copywriter
    from app.public_mock.router import router

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[build_copywriter] = lambda: lambda *_: "路由已启用润色。"
    try:
        response = TestClient(app).post(
            "/api/mock/expense/quota_query",
            headers={"X-API-Key": "expected-key"},
            json=SAMPLE_PAYLOADS["expense.quota_query"],
        )
        assert response.status_code == 200
        assert response.json()["message"] == "路由已启用润色。"
        assert response.json()["total_quota"] == 20_000
    finally:
        get_settings.cache_clear()
