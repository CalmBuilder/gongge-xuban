from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.mock import (
    MockBenefitReconcileRequest,
    MockFulfillmentReroutePlanRequest,
    MockOrderAddRequest,
    MockOrderQueryRequest,
    MockProductPriceQueryRequest,
    MockProductPurchaseRequest,
    mock_fulfillment_reroute_plan,
    mock_member_benefit_reconcile,
    mock_product_price_query,
    mock_order_add,
    mock_order_archive_query,
    mock_order_query,
    mock_product_purchase,
)
from app.db.seed import DEMO_TOOLS


def test_primary_order_query_returns_configured_order() -> None:
    result = mock_order_query(MockOrderQueryRequest(order_id="ORDER-1001"))

    assert result["found"] is True
    assert result["source"] == "primary_order_center"
    assert result["refundable"] is True


def test_primary_order_query_returns_miss_for_unknown_primary_order() -> None:
    result = mock_order_query(MockOrderQueryRequest(order_id="ARCHIVE-1001"))

    assert result["found"] is False
    assert result["miss_reason"] == "source_miss"


def test_archive_order_query_returns_refundable_history_order() -> None:
    result = mock_order_archive_query(MockOrderQueryRequest(order_id="ARCHIVE-1001"))

    assert result["found"] is True
    assert result["source"] == "archive_order_center"
    assert result["refundable"] is True


def test_product_purchase_persists_queryable_order() -> None:
    with _test_session() as db:
        purchase = mock_product_purchase(
            MockProductPurchaseRequest(user_id="user_demo", product_id="A1", quantity=2),
            db,
        )

        result = mock_order_query(MockOrderQueryRequest(order_id=purchase["order_id"]), db)

    assert result["found"] is True
    assert result["source"] == "primary_order_center"
    assert result["order_id"] == purchase["order_id"]
    assert result["product_id"] == "A1"
    assert result["quantity"] == 2
    assert result["payment_status"] == "paid"
    assert result["refundable"] is True
    assert result["total_amount"] == 258.0


def test_product_purchase_uses_name_catalog_price_for_a3() -> None:
    with _test_session() as db:
        purchase = mock_product_purchase(
            MockProductPurchaseRequest(user_id="user_demo", product_id="a3", quantity=1),
            db,
        )

        result = mock_order_query(MockOrderQueryRequest(order_id=purchase["order_id"]), db)

    assert purchase["found"] is True
    assert purchase["product_id"] == "A3"
    assert purchase["display_name"] == "A3 高阶商品"
    assert purchase["unit_price"] == 239.0
    assert purchase["total_amount"] == 239.0
    assert result["found"] is True
    assert result["product_id"] == "A3"
    assert result["total_amount"] == 239.0


def test_product_purchase_returns_miss_for_unknown_product() -> None:
    with _test_session() as db:
        purchase = mock_product_purchase(
            MockProductPurchaseRequest(user_id="user_demo", product_id="UNKNOWN", quantity=1),
            db,
        )

    assert purchase["found"] is False
    assert purchase["miss_reason"] == "product_not_found"
    assert "order_id" not in purchase


def test_order_add_persists_queryable_order() -> None:
    with _test_session() as db:
        added = mock_order_add(
            MockOrderAddRequest(user_id="user_demo", product_id="A3", quantity=1, status="created"),
            db,
        )

        result = mock_order_query(MockOrderQueryRequest(order_id=added["order_id"]), db)

    assert result["found"] is True
    assert result["order_id"] == added["order_id"]
    assert result["product_id"] == "A3"
    assert result["status"] == "created"
    assert result["total_amount"] == 239.0


def test_product_price_query_returns_price_by_product_name() -> None:
    result = mock_product_price_query(MockProductPriceQueryRequest(product_name="iPhone 15"))

    assert result["found"] is True
    assert result["source"] == "mock_product_price_catalog"
    assert result["display_name"] == "iPhone 15"
    assert result["price"] == 4599.0


def test_product_price_query_accepts_display_name_alias() -> None:
    result = mock_product_price_query(MockProductPriceQueryRequest(product_name="A3 高阶商品"))

    assert result["found"] is True
    assert result["product_id"] == "A3"
    assert result["price"] == 239.0


def test_product_price_query_is_seeded_for_price_compare_skill() -> None:
    price_tool = next(tool for tool in DEMO_TOOLS if tool["name"] == "product.price_query")

    assert "skill_price_compare_001" in price_tool["allowed_skills_json"]
    assert "skill_graph_visual_demo" in price_tool["allowed_skills_json"]
    assert price_tool["url"].endswith("/api/mock/product/price-query")


def test_member_benefit_reconcile_returns_missing_benefits() -> None:
    result = mock_member_benefit_reconcile(
        MockBenefitReconcileRequest(
            user_id="user_demo",
            order_id="A12345",
            member_level="black",
            benefit_type="coupon",
            benefit_campaign_id="vip_2026_midyear",
        )
    )

    assert result["found"] is True
    assert result["eligible"] is True
    assert result["missing_benefits"]
    assert result["recommended_action"] == "auto_reissue"


def test_fulfillment_reroute_plan_returns_candidate_plans() -> None:
    result = mock_fulfillment_reroute_plan(
        MockFulfillmentReroutePlanRequest(
            order_id="A12345",
            user_id="user_demo",
            target_address="上海市浦东新区示例路 88 号",
            expected_delivery_time="2026-06-04T20:00:00+08:00",
            allow_split_package=True,
            member_level="black",
        )
    )

    assert result["found"] is True
    assert result["reroutable"] is True
    assert result["recommended_plan_id"] == "same_city_priority"
    assert result["plans"]


def test_discovery_mock_apis_are_not_seeded_as_configured_tools() -> None:
    tool_names = {tool["name"] for tool in DEMO_TOOLS}

    assert "member.benefit_reconcile" not in tool_names
    assert "fulfillment.reroute_plan" not in tool_names


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
