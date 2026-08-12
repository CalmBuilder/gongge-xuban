"""
@Time       : 2026/08/03 23:35
@Author     : zhanglp8181
@File       : test_dynamic_capability_catalog.py
@CallChain  : pytest → Dynamic capability catalog → Tool/GeneralSkill/ModelConfig
@Description: 固化动态任务能力发布、分视图、快照和执行前再授权契约。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.api.general_skills import import_general_skill, publish_general_skill
from app.db.models import AgentProfile, AgentResourceBinding, ModelConfig, Tenant, Tool, User
from app.dynamic_tasks.capability_catalog import (
    CapabilityAccessDenied,
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    capability_checksum,
    project_tool_capability,
)
from app.general_skills.schema import GeneralSkillImportRequest


def _contract(**overrides: object) -> ToolReliabilityContract:
    """构造一个可进入首期动态目录的纯读契约。"""

    payload: dict[str, object] = {
        "risk_class": "read",
        "side_effect": "none",
        "confirmation_policy": "none",
        "idempotency": {"mode": "none", "argument": None, "remote_scope": None},
        "reconcile": {
            "supported": False,
            "tool_name": None,
            "reference_source": None,
            "terminal_status_mapping": {},
        },
        "model_visibility": {
            "allowed_paths": ["input.city", "output.temperature"],
            "user_display_paths": ["output.temperature"],
            "audit_only_paths": ["output.provider_trace"],
        },
        "timeout_policy": "failed",
        "dynamic_task_enabled": True,
    }
    payload.update(overrides)
    return ToolReliabilityContract.model_validate(payload)


def _admin() -> User:
    """构造动态能力发布测试所需的租户管理员。"""

    return User(
        id="admin_a",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="test",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "risk_class": "external_write",
            "side_effect": "external",
            "confirmation_policy": "once",
            "timeout_policy": "failed",
        },
        {
            "risk_class": "destructive",
            "side_effect": "external",
            "confirmation_policy": "always",
            "timeout_policy": "unknown",
        },
        {
            "reconcile": {
                "supported": True,
                "tool_name": None,
                "reference_source": "result.id",
                "terminal_status_mapping": {"done": "complete"},
            }
        },
        {
            "model_visibility": {
                "allowed_paths": ["input.api_key"],
                "user_display_paths": [],
                "audit_only_paths": [],
            }
        },
    ],
)
def test_contract_rejects_unsafe_or_incomplete_combinations(
    overrides: dict[str, object],
) -> None:
    """验证发布阶段拒绝错误超时语义、破坏性放行、假对账和凭据暴露。"""

    with pytest.raises(ValidationError):
        _contract(**overrides)


def test_contract_rejects_overlapping_visibility_paths() -> None:
    """验证模型、用户与审计专用字段不得交叉泄露。"""

    with pytest.raises(ValidationError):
        _contract(
            model_visibility={
                "allowed_paths": ["output.trace"],
                "user_display_paths": ["output.trace"],
                "audit_only_paths": ["output.trace"],
            }
        )


def test_parallel_contract_requires_explicit_safe_read_and_concurrency_key() -> None:
    """验证并行默认关闭，只有确定失败的纯读契约可声明稳定并发边界。"""

    contract = _contract(
        parallel_safe=True,
        concurrency_key="crm-read",
        max_in_flight=3,
    )
    assert contract.parallel_safe is True
    assert contract.max_in_flight == 3

    with pytest.raises(ValidationError):
        _contract(parallel_safe=True)
    with pytest.raises(ValidationError):
        _contract(concurrency_key="crm-read")
    with pytest.raises(ValidationError):
        _contract(parallel_safe=False, max_in_flight=2)


def test_capability_snapshot_is_canonical_and_views_do_not_leak_credentials() -> None:
    """验证键顺序不影响 checksum，且模型/用户视图不包含凭据或审计专用数据。"""

    tool = Tool(
        id="tool_weather",
        tenant_id="tenant_a",
        name="weather.lookup",
        display_name="天气查询",
        description="查询城市温度",
        method="GET",
        url="https://weather.invalid/api",
        headers_json={"Authorization": "Bearer secret"},
        auth_json={"api_key": "secret"},
        input_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "default": "credential-like-default"},
                "profile": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "private_note": {"type": "string"},
                    },
                },
                "api_key": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "temperature": {"type": "number"},
                "provider_trace": {"type": "string"},
            },
        },
    )
    contract = _contract(
        model_visibility={
            "allowed_paths": ["input.city", "input.profile.name", "output.temperature"],
            "user_display_paths": ["output.temperature"],
            "audit_only_paths": ["output.provider_trace"],
        }
    )
    views = project_tool_capability(tool, contract)

    assert views.model["input_schema"]["properties"]["city"] == {"type": "string"}
    assert views.user["output_schema"]["properties"] == {
        "temperature": {"type": "number"}
    }
    assert views.model["input_schema"]["properties"]["profile"]["properties"] == {
        "name": {"type": "string"}
    }
    assert "provider_trace" not in str(views.model)
    assert "provider_trace" not in str(views.user)
    assert "secret" not in str(views.model)
    assert "secret" not in str(views.user)
    assert "credential-like-default" not in str(views.model)
    first = capability_checksum({"contract": contract.model_dump(mode="json"), "name": tool.name})
    second = capability_checksum({"name": tool.name, "contract": contract.model_dump(mode="json")})
    assert first == second


def test_catalog_defaults_legacy_tools_out_and_rechecks_live_access() -> None:
    """验证无契约存量工具不进目录，且停用后不能凭旧快照继续执行。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        db.add(AgentProfile(id="agent_a", tenant_id="tenant_a", name="A", is_overall=False))
        legacy = Tool(
            id="tool_legacy",
            tenant_id="tenant_a",
            name="legacy.lookup",
            method="GET",
            url="https://example.invalid",
        )
        published = Tool(
            id="tool_published",
            tenant_id="tenant_a",
            name="weather.lookup",
            method="GET",
            url="https://example.invalid",
            reliability_contract_json=_contract().model_dump(mode="json"),
        )
        db.add_all([legacy, published])
        db.commit()

        catalog = DynamicCapabilityCatalog(db)
        assert catalog.list_tools("tenant_a", "agent_a") == []

        from app.agents.branching import ensure_private_resource_binding

        ensure_private_resource_binding(
            db, "tenant_a", "agent_a", "tool", published.id, "active"
        )
        db.commit()
        snapshot = catalog.resolve_tool("tenant_a", "agent_a", published.name)
        assert snapshot.capability_id == published.id

        published.enabled = False
        db.add(published)
        db.commit()
        with pytest.raises(CapabilityAccessDenied, match="DISABLED"):
            catalog.reauthorize_tool(snapshot, actor_user_id=None, organization_unit_id=None)


def test_dynamic_model_requires_successful_capability_preflight() -> None:
    """验证未验证或缺少 tool/structured-output 能力的模型不能创建动态执行。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        model = ModelConfig(
            id="model_a",
            tenant_id="tenant_a",
            name="Model A",
            api_key_encrypted="encrypted",
            model="example-model",
        )
        db.add(model)
        db.commit()
        catalog = DynamicCapabilityCatalog(db)

        with pytest.raises(CapabilityAccessDenied, match="MODEL_PREFLIGHT_REQUIRED"):
            catalog.require_dynamic_model("tenant_a", model.id)

        model.preflight_status = "ready"
        model.capability_snapshot_json = {
            "tool_calling": True,
            "structured_output": False,
            "sdk_available": True,
            "credentials_verified": True,
        }
        db.add(model)
        db.commit()
        with pytest.raises(CapabilityAccessDenied, match="MODEL_CAPABILITY_MISSING"):
            catalog.require_dynamic_model("tenant_a", model.id)


def test_planning_guidance_requires_explicit_publish_and_binding_reauthorization() -> None:
    """验证规划指南导入后先保持 draft，发布后可冻结，撤销绑定立即失效。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_a", name="Overall", is_overall=True)
        )
        db.add(AgentProfile(id="agent_a", tenant_id="tenant_a", name="A", is_overall=False))
        db.commit()
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_a",
                slug="incident-guide",
                name="故障处置指南",
                markdown="# 故障处置\n先查询监控，不得直接执行写操作。",
                usage_mode="planning_guidance",
                status="published",
            ),
            db,
            _admin(),
        )
        assert imported.status == "draft"
        assert imported.capability_checksum is None

        published = publish_general_skill(
            "incident-guide",
            tenant_id="tenant_a",
            db=db,
            agent_id="agent_overall",
            current_user=_admin(),
        )
        assert published.status == "published"
        assert published.capability_checksum is not None
        ensure_private_resource_binding(
            db, "tenant_a", "agent_a", "general_skill", published.id, "active"
        )
        db.commit()

        catalog = DynamicCapabilityCatalog(db)
        snapshot = catalog.resolve_general_skill("tenant_a", "agent_a", "incident-guide")
        assert snapshot.model_view["skill_markdown"].startswith("# 故障处置")
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_a",
                AgentResourceBinding.agent_id == "agent_a",
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == published.id,
            )
        ).one()
        binding.status = "inactive"
        db.add(binding)
        db.commit()
        with pytest.raises(CapabilityAccessDenied, match="BINDING_REVOKED"):
            catalog.reauthorize_general_skill(snapshot)
