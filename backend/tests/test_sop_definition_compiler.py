"""
@Time       : 2026/07/22 05:16
@Author     : zhanglp8181
@File       : test_sop_definition_compiler.py
@CallChain  : pytest → 旧版 SkillCard 适配器 → 规范 SOP 定义/发布诊断
@Description: 验证节点归一、图校验、能力门禁、兼容警告和稳定 checksum。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.skills import (
    _compile_skill_for_publication,
    _validate_participant_references_for_publication,
)
from app.db.models import BusinessRole, OrganizationUnit, PermissionDefinition
from app.sop_runtime import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    CompatibilityCandidate,
    NodeType,
    SopCompilationError,
    build_compatibility_report,
    compile_legacy_skill_card,
    render_compatibility_markdown,
)


def _skill_card_content() -> dict[str, object]:
    """构造覆盖输入、工具、决策、人工接管和终态的旧版 SkillCard。"""

    return {
        "skill_id": "warehouse_exception_review",
        "name": "仓库盘点异常复核",
        "version": "1.0.0",
        "nodes": [
            {
                "node_id": "collect_difference",
                "type": "collect_info",
                "name": "收集差异",
                "expected_user_info": ["warehouse_id", "difference_quantity"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "load_inventory",
                "type": "tool_call",
                "name": "读取库存",
                "allowed_actions": ["call_tool:inventory.query", "continue_flow"],
            },
            {
                "node_id": "route_difference",
                "type": "decision",
                "name": "判断差异",
            },
            {
                "node_id": "manual_review",
                "type": "handoff",
                "name": "人工复核",
                "metadata": {"handoff_target": "warehouse_manager"},
            },
            {
                "node_id": "reply_result",
                "type": "response",
                "name": "反馈结果",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {
                "source_node_id": "collect_difference",
                "next_node_id": "load_inventory",
                "condition": "信息完整",
            },
            {
                "source_node_id": "load_inventory",
                "next_node_id": "route_difference",
                "condition": "tool_success",
            },
            {
                "source_node_id": "route_difference",
                "next_node_id": "manual_review",
                "condition": "difference_quantity > threshold",
                "priority": 1,
            },
            {
                "source_node_id": "route_difference",
                "next_node_id": "reply_result",
                "condition": "difference_quantity <= threshold",
                "priority": 2,
            },
        ],
        "start_node_id": "collect_difference",
        "terminal_node_ids": ["manual_review", "reply_result"],
    }


def _diagnostic_codes(error: SopCompilationError) -> set[str]:
    """提取编译错误码集合，避免测试依赖诊断文本顺序。"""

    return {diagnostic.code for diagnostic in error.diagnostics}


def test_compiler_normalizes_legacy_nodes_without_creating_approval_semantics() -> None:
    """验证旧节点统一映射为五类原语，handoff 仍是问答接管而非审批。"""

    compiled = compile_legacy_skill_card(_skill_card_content())
    nodes = {node.node_id: node for node in compiled.nodes}

    assert nodes["collect_difference"].type is NodeType.COLLECT_INPUT
    assert nodes["load_inventory"].type is NodeType.SERVICE_TASK
    assert nodes["route_difference"].type is NodeType.DECISION
    assert nodes["manual_review"].type is NodeType.HUMAN_TASK
    assert nodes["manual_review"].config.kind == "conversational_handoff"
    assert nodes["reply_result"].type is NodeType.TERMINAL
    assert compiled.source_format == "legacy_skill_card"
    assert compiled.source_schema_version == 2


def test_compiler_accepts_restricted_dsl_without_legacy_warning() -> None:
    """验证声明 schema 的受限条件进入规范定义且不再产生旧条件升级警告。"""

    content = _skill_card_content()
    content["condition_schemas"] = {
        "slots": {
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
        }
    }
    edges = content["edges"]
    assert isinstance(edges, list)
    edges[0]["condition"] = {
        "op": "eq",
        "left": {"path": "slots.ready"},
        "right": {"value": True},
    }
    edges[1]["condition"] = {"op": "always"}
    edges[2]["condition"] = {
        "op": "eq",
        "left": {"path": "slots.ready"},
        "right": {"value": True},
    }
    edges[2]["priority"] = 100
    edges[3]["condition"] = {"op": "always"}
    edges[3]["priority"] = 0

    compiled = compile_legacy_skill_card(content)

    assert all(edge.condition.language == "restricted_dsl" for edge in compiled.edges)
    assert "LEGACY_CONDITION_REQUIRES_UPGRADE" not in {
        diagnostic.code for diagnostic in compiled.diagnostics
    }
    decision = next(node for node in compiled.nodes if node.node_id == "route_difference")
    assert decision.config.capability == "decision.restricted_dsl"


def test_compiler_rejects_mixed_condition_languages_in_one_branch_set() -> None:
    """验证同一多出边节点不能只迁移部分条件而留下双重语义。"""

    content = _skill_card_content()
    content["condition_schemas"] = {"slots": {"type": "object", "properties": {}}}
    edges = content["edges"]
    assert isinstance(edges, list)
    edges[2]["condition"] = {"op": "always"}

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "CONDITION_MIXED_LANGUAGES" in _diagnostic_codes(caught.value)


def test_compiler_infers_missing_legacy_type_from_actions_and_position() -> None:
    """验证缺少 type 的历史节点可按工具动作和终态位置确定性归一。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[1].pop("type")
    nodes[4].pop("type")

    compiled = compile_legacy_skill_card(content)
    compiled_nodes = {node.node_id: node for node in compiled.nodes}

    assert compiled_nodes["load_inventory"].type is NodeType.SERVICE_TASK
    assert compiled_nodes["reply_result"].type is NodeType.TERMINAL


def test_compiler_checksum_is_stable_for_the_same_canonical_definition() -> None:
    """验证同一旧定义重复编译得到稳定 checksum，供版本不可变校验使用。"""

    first = compile_legacy_skill_card(_skill_card_content())
    second = compile_legacy_skill_card(_skill_card_content())

    assert first.checksum == second.checksum
    assert len(first.checksum) == 64


def test_compiler_freezes_input_value_aliases_and_rejects_undeclared_slot() -> None:
    """验证输入别名进入版本契约，且不能绑定节点未声明的任意槽位。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["metadata"] = {
        "value_aliases": {
            "difference_quantity": {"十": "10", "TEN": "10", "10": "10"},
        }
    }

    compiled = compile_legacy_skill_card(content)
    collect_node = next(node for node in compiled.nodes if node.node_id == "collect_difference")

    assert collect_node.config.value_aliases == {
        "difference_quantity": {"十": "10", "ten": "10", "10": "10"}
    }

    nodes[0]["metadata"] = {"value_aliases": {"unknown_slot": {"a": "b"}}}
    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "INPUT_VALUE_ALIAS_SLOT_UNDECLARED" in _diagnostic_codes(caught.value)


def test_compiler_rejects_unknown_node_type() -> None:
    """验证无法映射到统一原语的旧节点类型阻断编译。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["type"] = "approval_magic"

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "UNKNOWN_NODE_TYPE" in _diagnostic_codes(caught.value)


def test_compiler_rejects_definition_without_terminal() -> None:
    """验证缺少终态声明时返回稳定领域错误码而不是底层校验文本。"""

    content = _skill_card_content()
    content["terminal_node_ids"] = []

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert _diagnostic_codes(caught.value) == {"MISSING_TERMINAL"}


def test_compiler_rejects_unreachable_node() -> None:
    """验证无法从起点到达的孤立节点不能进入发布定义。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    terminals = content["terminal_node_ids"]
    assert isinstance(nodes, list)
    assert isinstance(terminals, list)
    nodes.append(
        {
            "node_id": "orphan_reply",
            "type": "response",
            "name": "孤立响应",
            "allowed_actions": ["answer_user"],
        }
    )
    terminals.append("orphan_reply")

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "UNREACHABLE_NODE" in _diagnostic_codes(caught.value)


def test_compiler_rejects_reachable_cycle_without_terminal_path() -> None:
    """验证可达但永远无法进入终态的循环分支阻断发布。"""

    content = _skill_card_content()
    edges = content["edges"]
    assert isinstance(edges, list)
    edges[1]["next_node_id"] = "collect_difference"

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "NO_PATH_TO_TERMINAL" in _diagnostic_codes(caught.value)


def test_compiler_rejects_terminal_with_outgoing_edge() -> None:
    """验证声明为终态的节点不能继续连接下游节点。"""

    content = _skill_card_content()
    edges = content["edges"]
    assert isinstance(edges, list)
    edges.append(
        {
            "source_node_id": "reply_result",
            "next_node_id": "collect_difference",
            "condition": "restart",
        }
    )

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "TERMINAL_HAS_OUTGOING" in _diagnostic_codes(caught.value)


def test_compiler_rejects_invalid_legacy_condition() -> None:
    """验证超长或包含空字符的旧条件不能穿过兼容边界。"""

    content = _skill_card_content()
    edges = content["edges"]
    assert isinstance(edges, list)
    edges[0]["condition"] = "bad\x00condition"

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "INVALID_LEGACY_CONDITION" in _diagnostic_codes(caught.value)


def test_compiler_rejects_unregistered_capability() -> None:
    """验证 Runtime 未注册节点所需能力时发布编译失败。"""

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(_skill_card_content(), registry=CapabilityRegistry(()))

    assert "MISSING_CAPABILITY" in _diagnostic_codes(caught.value)


def test_capability_registry_only_marks_fully_connected_runtime_handlers_executable() -> None:
    """验证工具、知识、人工任务等已接通的 Runtime 能力才标记为可执行。"""

    assert DEFAULT_CAPABILITY_REGISTRY.supports_definition("service.tool", NodeType.SERVICE_TASK)
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution("service.tool", NodeType.SERVICE_TASK)
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution(
        "service.knowledge", NodeType.SERVICE_TASK
    )
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution("input.collect", NodeType.COLLECT_INPUT)
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution(
        "decision.restricted_dsl", NodeType.DECISION
    )
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution("terminal.response", NodeType.TERMINAL)
    assert DEFAULT_CAPABILITY_REGISTRY.supports_execution(
        "human.structured_work_item", NodeType.HUMAN_TASK
    )


def test_capability_registry_rejects_legacy_knowledge_node_without_k1_contract() -> None:
    """验证旧知识节点保留 checksum 兼容，但缺少查询和结果契约时不能进入新 Runtime。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    knowledge_node = nodes[1]
    assert isinstance(knowledge_node, dict)
    knowledge_node["type"] = "knowledge_query"
    knowledge_node["allowed_actions"] = ["knowledge_query"]

    definition = compile_legacy_skill_card(content)

    assert (
        "load_inventory",
        "service.knowledge",
    ) in DEFAULT_CAPABILITY_REGISTRY.non_executable_nodes(definition)


def test_compiler_freezes_structured_work_item_participant_policy() -> None:
    """验证候选角色、多角色完成规则和防自批策略进入不可变规范定义。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager", "inventory.auditor"],
            "completion_mode": "any",
            "claim_required": True,
            "participant_scope_resolver": "explicit_org",
            "participant_scope_org_unit_id": "org_warehouse",
            "exclude_initiator": True,
            "allowed_outcomes": ["approved", "rejected"],
            "timeout_seconds": 86400,
            "timeout_action": "fail",
        }
    }

    compiled = compile_legacy_skill_card(content)
    human_task = next(node for node in compiled.nodes if node.node_id == "manual_review")

    assert human_task.config.capability == "human.structured_work_item"
    assert human_task.config.candidate_role_codes == (
        "warehouse.manager",
        "inventory.auditor",
    )
    assert human_task.config.completion_policy.mode == "any"
    assert human_task.config.completion_policy.claim_required is True
    assert human_task.config.participant_scope_resolver == "explicit_org"
    assert human_task.config.participant_scope_org_unit_id == "org_warehouse"
    assert human_task.config.exclude_initiator is True
    assert human_task.config.timeout_policy.timeout_seconds == 86400


def test_compiler_defaults_old_participant_policy_to_tenant_scope() -> None:
    """验证旧 participant_policy 未声明组织范围时仍编译为全租户。"""

    content = _skill_card_content()
    manual_review = content["nodes"][3]
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "any",
        }
    }

    compiled = compile_legacy_skill_card(content)
    human_task = next(node for node in compiled.nodes if node.node_id == "manual_review")

    assert human_task.config.participant_scope_resolver == "tenant"
    assert human_task.config.participant_scope_org_unit_id is None


def test_compiler_freezes_non_approval_outcome_options_and_waiting_message() -> None:
    """验证人工办理结果、意见要求和通知文案进入统一不可变工作项契约。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["it_support_engineer"],
            "completion_mode": "any",
            "claim_required": True,
            "waiting_message": "工单等待工程师处理。",
            "outcome_options": [
                {
                    "value": "resolved",
                    "label": "标记已解决",
                    "tone": "success",
                    "comment_required": True,
                    "completion_message": "工程师处理说明：{comment}",
                }
            ],
        }
    }

    compiled = compile_legacy_skill_card(content)
    human_task = next(node for node in compiled.nodes if node.node_id == "manual_review")

    assert human_task.config.allowed_outcomes == ("resolved",)
    assert human_task.config.waiting_message == "工单等待工程师处理。"
    assert human_task.config.outcome_options[0].comment_required is True
    assert human_task.config.outcome_options[0].completion_message == "工程师处理说明：{comment}"


def test_compiler_rejects_unimplemented_work_item_timeout_action() -> None:
    """验证发布期拒绝尚无执行器的升级超时动作，避免配置成功但运行时静默失效。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "any",
            "timeout_seconds": 60,
            "timeout_action": "escalate",
        }
    }

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "INVALID_PARTICIPANT_POLICY" in _diagnostic_codes(caught.value)


def test_publication_rejects_unknown_business_role_and_accepts_active_role() -> None:
    """验证发布边界阻止悬空角色引用，并在角色建立后允许同一定义发布。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "any",
        }
    }
    compiled = compile_legacy_skill_card(content)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        with pytest.raises(HTTPException) as caught:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)
        assert caught.value.status_code == 422
        assert caught.value.detail["code"] == "PARTICIPANT_ROLE_NOT_FOUND"
        assert caught.value.detail["role_codes"] == ["warehouse.manager"]

        db.add(
            BusinessRole(
                tenant_id="tenant_demo",
                role_code="warehouse.manager",
                name="仓库负责人",
            )
        )
        db.commit()
        _validate_participant_references_for_publication(db, "tenant_demo", compiled)


def test_publication_rejects_governance_role_as_work_item_candidate() -> None:
    """验证治理授权不能伪装成流程业务参与者，保持两类角色语义分离。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["tenant_administrator"],
            "completion_mode": "any",
        }
    }
    compiled = compile_legacy_skill_card(content)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            BusinessRole(
                tenant_id="tenant_demo",
                role_code="tenant_administrator",
                name="租户管理员",
                role_kind="governance",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as caught:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)

        assert caught.value.detail["code"] == "PARTICIPANT_ROLE_NOT_FOUND"
        assert caught.value.detail["role_codes"] == ["tenant_administrator"]


def test_publication_validates_explicit_participant_organization() -> None:
    """验证显式参与组织必须是同租户活动节点，拒绝悬空、停用和跨租户引用。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_user_ids": ["user_reviewer"],
            "completion_mode": "single",
            "participant_scope_resolver": "explicit_org",
            "participant_scope_org_unit_id": "org_review",
        }
    }
    compiled = compile_legacy_skill_card(content)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        with pytest.raises(HTTPException) as missing:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)
        assert missing.value.detail["code"] == "PARTICIPANT_ORG_NOT_FOUND"

        db.add(
            OrganizationUnit(
                id="org_review",
                tenant_id="tenant_other",
                code="review",
                name="跨租户复核组",
                unit_type_code="department",
                tree_path="/org_review/",
                status="active",
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as foreign:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)
        assert foreign.value.detail["org_unit_ids"] == ["org_review"]

        foreign_org = db.get(OrganizationUnit, "org_review")
        assert foreign_org is not None
        db.delete(foreign_org)
        db.commit()
        db.add(
            OrganizationUnit(
                id="org_review",
                tenant_id="tenant_demo",
                code="review",
                name="复核组",
                unit_type_code="department",
                tree_path="/org_review/",
                status="inactive",
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as inactive:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)
        assert inactive.value.detail["org_unit_ids"] == ["org_review"]

        active_org = db.get(OrganizationUnit, "org_review")
        assert active_org is not None
        active_org.status = "active"
        db.add(active_org)
        db.commit()
        _validate_participant_references_for_publication(db, "tenant_demo", compiled)


def test_publication_rejects_unknown_work_item_action_permission() -> None:
    """验证动作权限必须在发布时来自同租户有效目录，避免运行期才发现悬空契约。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "any",
            "action_permissions": {"outcome:approved": "warehouse.review.approve"},
        }
    }
    compiled = compile_legacy_skill_card(content)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            BusinessRole(
                tenant_id="tenant_demo",
                role_code="warehouse.manager",
                name="仓库负责人",
            )
        )
        db.commit()
        with pytest.raises(HTTPException) as caught:
            _validate_participant_references_for_publication(db, "tenant_demo", compiled)
        assert caught.value.detail["code"] == "ACTION_PERMISSION_NOT_FOUND"
        assert caught.value.detail["permission_codes"] == ["warehouse.review.approve"]

        db.add(
            PermissionDefinition(
                tenant_id="tenant_demo",
                permission_code="warehouse.review.approve",
                name="通过仓库复核",
                category="cross_functional",
                resource="warehouse.review",
                action="approve",
            )
        )
        db.commit()
        _validate_participant_references_for_publication(db, "tenant_demo", compiled)


def test_compiler_rejects_structured_work_item_without_candidate_source() -> None:
    """验证结构化人工任务不能在没有候选角色或用户时发布。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {"participant_policy": {"completion_mode": "any"}}

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "INVALID_PARTICIPANT_POLICY" in _diagnostic_codes(caught.value)


def test_compiler_rejects_single_mode_role_pool_as_ambiguous_assignment() -> None:
    """验证 single 只能指向一个直接用户，角色候选池必须显式使用 any。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "single",
        }
    }

    with pytest.raises(SopCompilationError) as caught:
        compile_legacy_skill_card(content)

    assert "INVALID_PARTICIPANT_POLICY" in _diagnostic_codes(caught.value)


def test_compiler_warns_when_legacy_tool_operation_is_undeclared() -> None:
    """验证缺少具体操作名的旧工具节点进入兼容报告而不是被静默伪造。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[1]["allowed_actions"] = ["continue_flow"]

    compiled = compile_legacy_skill_card(content)

    assert "LEGACY_TOOL_OPERATION_UNDECLARED" in {
        diagnostic.code for diagnostic in compiled.diagnostics
    }


def test_compiler_warns_when_legacy_node_collects_input_and_calls_tool() -> None:
    """验证混合输入收集与工具副作用的旧节点被标记为需要拆分。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["allowed_actions"] = ["ask_user", "call_tool:inventory.query"]

    compiled = compile_legacy_skill_card(content)

    assert "LEGACY_MIXED_NODE_REQUIRES_SPLIT" in {
        diagnostic.code for diagnostic in compiled.diagnostics
    }


def test_publication_boundary_returns_structured_compilation_error() -> None:
    """验证发布边界把编译错误转换为包含稳定诊断码的 422 响应。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["type"] = "approval_magic"

    with pytest.raises(HTTPException) as caught:
        _compile_skill_for_publication(content)

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "SOP_DEFINITION_COMPILATION_FAILED"
    assert caught.value.detail["diagnostics"][0]["code"] == "UNKNOWN_NODE_TYPE"


def test_deterministic_publication_rejects_schema_only_capabilities() -> None:
    """验证确定性发布不能包含只有 schema、尚无 Runtime 执行器的节点能力。"""

    content = _skill_card_content()
    content["execution_mode"] = "deterministic"

    with pytest.raises(HTTPException) as caught:
        _compile_skill_for_publication(content)

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "CAPABILITY_NOT_EXECUTABLE"
    unsupported = {item["capability"] for item in caught.value.detail["nodes"]}
    assert "human.conversational_handoff" in unsupported
    assert "decision.legacy_expression" in unsupported


def test_compatibility_report_keeps_successes_and_blocked_definitions() -> None:
    """验证批量兼容扫描不会因单个坏定义中断，并可稳定输出 Markdown。"""

    invalid_content = _skill_card_content()
    invalid_nodes = invalid_content["nodes"]
    assert isinstance(invalid_nodes, list)
    invalid_nodes[0]["type"] = "approval_magic"
    report = build_compatibility_report(
        (
            CompatibilityCandidate(
                skill_id="compatible_skill",
                version="1.0.0",
                content=_skill_card_content(),
            ),
            CompatibilityCandidate(
                skill_id="blocked_skill",
                version="1.0.0",
                content=invalid_content,
            ),
        )
    )

    assert report.total == 2
    assert report.compilable == 1
    assert report.compiles_with_warnings == 1
    assert report.blocked == 1
    compatible_entry = next(
        entry for entry in report.entries if entry.skill_id == "compatible_skill"
    )
    assert compatible_entry.participant_scope_counts == {}
    assert compatible_entry.candidate_source_counts == {}
    markdown = render_compatibility_markdown(report)
    assert "`compatible_skill`" in markdown
    assert "UNKNOWN_NODE_TYPE" in markdown


def test_compatibility_report_inventories_structured_participant_scope() -> None:
    """验证只读盘点能识别组织范围解析器和直接用户、业务角色候选来源。"""

    content = _skill_card_content()
    nodes = content["nodes"]
    assert isinstance(nodes, list)
    manual_review = nodes[3]
    assert isinstance(manual_review, dict)
    manual_review["metadata"] = {
        "participant_policy": {
            "candidate_user_ids": ["user_reviewer"],
            "candidate_role_codes": ["warehouse.manager"],
            "completion_mode": "any",
            "participant_scope_resolver": "initiator_primary_org_subtree",
        }
    }

    report = build_compatibility_report(
        (
            CompatibilityCandidate(
                skill_id="scoped_review",
                version="1.0.0",
                content=content,
            ),
        )
    )

    entry = report.entries[0]
    assert entry.participant_scope_counts == {"initiator_primary_org_subtree": 1}
    assert entry.candidate_source_counts == {"business_role": 1, "direct_user": 1}
    markdown = render_compatibility_markdown(report)
    assert "initiator_primary_org_subtree=1" in markdown
    assert "business_role=1" in markdown
