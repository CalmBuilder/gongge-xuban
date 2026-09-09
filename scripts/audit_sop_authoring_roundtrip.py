"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : audit_sop_authoring_roundtrip.py
@CallChain  : 手工审核命令 → 真实 SOP API 函数/编译器/Coordinator → 内存 SQLite
@Description: 用虚构候选复现生成消费边界；不调用模型、HTTP 工具或业务数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

# 使仓库根目录调用使用同一份后端代码，不依赖安装的其他应用版本。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.agents.branching import visible_published_skills  # noqa: E402
from app.api.skills import create_skill, publish_skill, update_skill  # noqa: E402
from app.db.demo_sop_versions import _leave_balance_deterministic_content  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentProfile,
    ChatSession,
    SkillVersion,
    SopInstance,
    SopOperation,
    Tenant,
    User,
)
from app.session.session_schema import StepAgentResult  # noqa: E402
from app.skills.skill_distiller import SkillDistiller  # noqa: E402
from app.skills.skill_schema import (  # noqa: E402
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillUpdateRequest,
)
from app.sop_runtime.coordinator import DeterministicSopCoordinator  # noqa: E402
from app.sop_runtime.legacy_skill_card_adapter import compile_legacy_skill_card  # noqa: E402
from app.sop_runtime.scheduler import plan_next_action  # noqa: E402
from app.sop_runtime.versioning import skill_content_checksum  # noqa: E402
from app.tools.tool_schema import ToolResult  # noqa: E402

TENANT_ID = "tenant_sop_audit_synthetic"
AGENT_ID = "agent_sop_audit_synthetic"
SKILL_ID = "skill_sop_audit_synthetic"
Payload = dict[str, Any]
Probe = Callable[[], tuple[bool, Payload]]


@contextmanager
def memory_session() -> Iterator[tuple[Session, User]]:
    """仅创建内存测试表及虚构主体，结束时销毁连接，不读取应用数据库。"""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(
                id="user_sop_audit_synthetic",
                tenant_id=TENANT_ID,
                username="sop_audit_synthetic",
                role="admin",
                password_hash="synthetic-test-only-not-a-credential",
            )
            db.add(Tenant(id=TENANT_ID, name="虚构审核租户"))
            db.add(user)
            db.add(
                AgentProfile(
                    id=AGENT_ID,
                    tenant_id=TENANT_ID,
                    name="虚构审核员工",
                    owner_user_id=user.id,
                    metadata_json={"owner_user_id": user.id},
                )
            )
            db.commit()
            yield db, user
    finally:
        engine.dispose()


def candidate() -> Payload:
    """构造收集员工号、查询、回复的确定性候选，不伪装成模型真实生成结果。"""

    return {
        "skill_id": SKILL_ID,
        "name": "虚构查询流程",
        "version": "1.0.0",
        "execution_mode": "deterministic",
        "condition_schemas": {
            "slots": {
                "type": "object",
                "properties": {"employee_id": {"type": "string"}},
            },
            "tool_result": {
                "type": "object",
                "properties": {"lookup_receipt": {"type": "object"}},
            },
        },
        "nodes": [
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集员工号",
                "expected_user_info": ["employee_id"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "lookup",
                "type": "tool_call",
                "name": "查询",
                "allowed_actions": ["call_tool:sample.lookup"],
                "metadata": {
                    "operation_input": {"employee_id": "slots.employee_id"},
                    "operation_result_key": "lookup_receipt",
                },
            },
            {
                "node_id": "done",
                "type": "response",
                "name": "反馈结果",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [
            {"source_node_id": "collect", "next_node_id": "lookup"},
            {"source_node_id": "lookup", "next_node_id": "done"},
        ],
        "start_node_id": "collect",
        "terminal_node_ids": ["done"],
    }


def create_via_api(db: Session, user: User, status: str) -> None:
    """通过生产创建函数建立分支和版本，不直接预置 SkillVersion。"""

    request = SkillCreateRequest.model_validate(
        {"tenant_id": TENANT_ID, "content": candidate(), "status": status}
    )
    create_skill(request=request, agent_id=AGENT_ID, db=db, current_user=user)


def chat(db: Session, user: User, *, with_input: bool = True) -> ChatSession:
    """建立虚构聊天会话，供真实 Coordinator 消费刚创建的发布对象。"""

    row = ChatSession(
        id=f"session_sop_audit_{uuid4().hex}",
        tenant_id=TENANT_ID,
        user_id=user.id,
        agent_id=AGENT_ID,
        active_skill_id=SKILL_ID,
        active_step_id="collect",
        slots_json={"employee_id": "SYNTHETIC-001"} if with_input else {},
    )
    db.add(row)
    db.commit()
    return row


def direct_publication_consumption() -> tuple[bool, Payload]:
    """正向验证创建即发布后的等待、工具实参、注入回执、持久终态及版本一致性。"""

    with memory_session() as (db, user):
        create_via_api(db, user, "published")
        skill = visible_published_skills(db, TENANT_ID, AGENT_ID)[0]
        session = chat(db, user, with_input=False)
        coordinator = DeterministicSopCoordinator(db)
        waiting = coordinator.prepare_step(session, skill, StepAgentResult())
        session.slots_json = {"employee_id": "SYNTHETIC-001"}
        calling = coordinator.prepare_step(session, skill, StepAgentResult())
        assert calling.tool_call is not None
        result = coordinator.record_tool_result(
            session,
            calling.tool_call,
            ToolResult(tool_name="sample.lookup", success=True, data={"remaining": 10}),
        )
        db.commit()
        instance = db.exec(select(SopInstance)).one()
        operation = db.exec(select(SopOperation)).one()
        version = db.get(SkillVersion, instance.skill_version_id)
        assert version is not None
        checksum_matches = (
            skill_content_checksum(skill.content_json)
            == skill_content_checksum(version.content_json)
            == version.content_checksum
        )
        details = {
            "wait_action": waiting.action,
            "tool_call": calling.tool_call.model_dump(),
            "final_action": result.action if result else None,
            "instance_status": instance.status,
            "operation_status": operation.status,
            "published_content_matches": checksum_matches,
            "runtime_checksum_matches": (
                instance.definition_checksum == version.compiled_definition_checksum
            ),
        }
        passed = (
            waiting.action == "ask_user"
            and calling.tool_call.arguments == {"employee_id": "SYNTHETIC-001"}
            and result is not None
            and result.action == "complete"
            and instance.status == operation.status == "succeeded"
            and checksum_matches
            and details["runtime_checksum_matches"]
        )
        return bool(passed), details


def draft_then_publish() -> tuple[bool, Payload]:
    """检验作者计划依赖的员工草稿启用路径是否产生 Runtime 可读取的发布快照。"""

    with memory_session() as (db, user):
        create_via_api(db, user, "draft")
        published = publish_skill(
            skill_id=SKILL_ID,
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            db=db,
            current_user=user,
        )
        skill = visible_published_skills(db, TENANT_ID, AGENT_ID)[0]
        statuses = [v.status for v in db.exec(select(SkillVersion)).all()]
        try:
            result = DeterministicSopCoordinator(db).prepare_step(
                chat(db, user), skill, StepAgentResult()
            )
        except ValueError as exc:
            return False, {
                "api_status": published.status,
                "visible_to_runtime": True,
                "snapshot_statuses": statuses,
                "consumer_error": str(exc),
            }
        return result.tool_call is not None, {"action": result.action}


def update_employee_branch() -> tuple[bool, Payload]:
    """核对分支修订后的新实例与原活动实例，显式区分两种消费行为。"""

    with memory_session() as (db, user):
        create_via_api(db, user, "published")
        coordinator = DeterministicSopCoordinator(db)
        original = visible_published_skills(db, TENANT_ID, AGENT_ID)[0]
        old_chat = chat(db, user, with_input=False)
        coordinator.prepare_step(old_chat, original, StepAgentResult())
        db.commit()
        old_instance = db.exec(select(SopInstance)).one()
        frozen_version_id = old_instance.skill_version_id
        content = candidate()
        content["version"] = "1.1.0"
        content["nodes"][1]["allowed_actions"] = ["call_tool:sample.branch_lookup"]
        update_skill(
            SKILL_ID,
            SkillUpdateRequest(tenant_id=TENANT_ID, content=SkillCard(**content)),
            agent_id=AGENT_ID,
            db=db,
            current_user=user,
        )
        revised = visible_published_skills(db, TENANT_ID, AGENT_ID)[0]
        old_chat.slots_json = {"employee_id": "SYNTHETIC-001"}
        old_call = coordinator.prepare_step(old_chat, revised, StepAgentResult())
        details = {
            "branch_version": revised.version,
            "snapshot_versions": [v.version for v in db.exec(select(SkillVersion)).all()],
            "old_instance_still_frozen": old_instance.skill_version_id == frozen_version_id,
            "old_instance_operation": old_call.tool_call.name if old_call.tool_call else None,
        }
        try:
            new_call = coordinator.prepare_step(chat(db, user), revised, StepAgentResult())
        except ValueError as exc:
            return False, {**details, "new_instance_error": str(exc)}
        return (
            new_call.tool_call is not None and new_call.tool_call.name == "sample.branch_lookup",
            details,
        )


def branch_global_version_collision() -> tuple[bool, Payload]:
    """复现员工分支与整体同版本号不同内容时，实际实例是否执行错误定义。"""

    with memory_session() as (db, user):
        create_via_api(db, user, "published")
        branch_content = candidate()
        branch_content["version"] = "1.1.0"
        branch_content["nodes"][1]["allowed_actions"] = ["call_tool:sample.branch_lookup"]
        update_skill(
            SKILL_ID,
            SkillUpdateRequest(tenant_id=TENANT_ID, content=SkillCard(**branch_content)),
            agent_id=AGENT_ID,
            db=db,
            current_user=user,
        )
        global_content = deepcopy(branch_content)
        global_content["nodes"][1]["allowed_actions"] = ["call_tool:sample.global_lookup"]
        update_skill(
            SKILL_ID,
            SkillUpdateRequest(tenant_id=TENANT_ID, content=SkillCard(**global_content)),
            agent_id=None,
            db=db,
            current_user=user,
        )
        visible = visible_published_skills(db, TENANT_ID, AGENT_ID)[0]
        result = DeterministicSopCoordinator(db).prepare_step(
            chat(db, user), visible, StepAgentResult()
        )
        instance = db.exec(select(SopInstance)).one()
        version = db.get(SkillVersion, instance.skill_version_id)
        assert version is not None
        matches = skill_content_checksum(visible.content_json) == skill_content_checksum(
            version.content_json
        )
        details = {
            "projected_version": visible.version,
            "expected_tool": "sample.branch_lookup",
            "actual_tool": result.tool_call.name if result.tool_call else None,
            "projected_checksum": skill_content_checksum(visible.content_json),
            "runtime_source_checksum": skill_content_checksum(version.content_json),
            "content_matches": matches,
        }
        return matches and details["actual_tool"] == details["expected_tool"], details


def generation_normalization_fidelity() -> tuple[bool, Payload]:
    """将真实种子定义送入实际归一化函数，检测是否仍保留可执行契约。"""

    source = _leave_balance_deterministic_content({})
    before = compile_legacy_skill_card(source)
    response = SkillDistiller()._normalize_response(
        {"draft_skill": source},
        SkillDistillRequest(
            tenant_id=TENANT_ID,
            title="虚构审核",
            raw_content="虚构余额查询需求",
            available_tools=[{"name": "hr.balance_query"}],
        ),
    )
    normalized = response.draft_skill.model_dump()
    details = {
        "source_diagnostics": len(before.diagnostics),
        "source_execution_mode": source["execution_mode"],
        "normalized_execution_mode": normalized["execution_mode"],
        "condition_schemas_preserved": source["condition_schemas"]
        == normalized["condition_schemas"],
    }
    try:
        after = compile_legacy_skill_card(normalized)
    except ValueError as exc:
        return False, {**details, "compile_error": str(exc)}
    return before.checksum == after.checksum and not after.diagnostics, details


def repeated_tool_receipt_keys() -> tuple[bool, Payload]:
    """检验同一工具调用两次时，第一次回执是否错误替代了第二次业务动作。"""

    content = candidate()
    first = content["nodes"][1]
    first["metadata"].pop("operation_result_key")
    second = deepcopy(first)
    second["node_id"] = "lookup_again"
    content["nodes"].insert(2, second)
    content["edges"][1]["next_node_id"] = "lookup_again"
    content["edges"].append({"source_node_id": "lookup_again", "next_node_id": "done"})
    definition = compile_legacy_skill_card(content)
    first_plan = plan_next_action(
        definition, current_node_id="lookup", slots={"employee_id": "SYNTHETIC-001"}
    )
    assert first_plan.result_key is not None
    second_plan = plan_next_action(
        definition,
        current_node_id="lookup_again",
        slots={"employee_id": "SYNTHETIC-001"},
        tool_results={first_plan.result_key: {"status": "succeeded", "data": {"remaining": 10}}},
    )
    return second_plan.action == "call_tool", {
        "compile_diagnostics": len(definition.diagnostics),
        "first_result_key": first_plan.result_key,
        "first_action": first_plan.action,
        "second_action": second_plan.action,
        "second_next_node": second_plan.next_node_id,
    }


def fabricated_future_input() -> tuple[bool, Payload]:
    """检测候选自造但没有生产者的数据路径，是否仍被编译并发出空工具参数。"""

    content = candidate()
    content["condition_schemas"]["tool_result"]["properties"]["future"] = {
        "type": "object",
        "properties": {
            "data": {"type": "object", "properties": {"employee_id": {"type": "string"}}}
        },
    }
    content["nodes"][1]["metadata"]["operation_input"]["employee_id"] = (
        "tool_result.future.data.employee_id"
    )
    try:
        definition = compile_legacy_skill_card(content)
    except ValueError:
        return True, {"invalid_definition_rejected": True}
    result = plan_next_action(definition, current_node_id="lookup", slots={})
    return bool(definition.diagnostics) or result.action == "fail", {
        "compile_diagnostics": len(definition.diagnostics),
        "action": result.action,
        "arguments": result.operation_arguments,
        "missing_producer": "tool_result.future",
    }


def independent_boundary_oracle() -> tuple[bool, Payload]:
    """用需求先确定的 2/3/4 天期望，证明编译成功仍需独立边界断言。"""

    # 需求已固定为超过 3 天；期望表不从候选条件生成。
    expected = ["done", "done", "hr"]
    content = candidate()
    content["condition_schemas"] = {
        "slots": {"type": "object", "properties": {"days": {"type": "number"}}}
    }
    content["nodes"] = [
        {"node_id": "decide", "type": "decision", "name": "判断", "allowed_actions": []},
        {"node_id": "done", "type": "response", "name": "普通路径"},
        {"node_id": "hr", "type": "response", "name": "需要 HR 路径"},
    ]
    content["edges"] = [
        {
            "source_node_id": "decide",
            "next_node_id": "hr",
            "priority": 100,
            "condition": {"op": "gt", "left": {"path": "slots.days"}, "right": {"value": 3}},
        },
        {
            "source_node_id": "decide",
            "next_node_id": "done",
            "priority": 0,
            "condition": {"op": "always"},
        },
    ]
    content["start_node_id"] = "decide"
    content["terminal_node_ids"] = ["done", "hr"]
    traces: Payload = {}
    diagnostics: Payload = {}
    for operator in ("gt", "gte"):
        content["edges"][0]["condition"]["op"] = operator
        definition = compile_legacy_skill_card(content)
        diagnostics[operator] = len(definition.diagnostics)
        traces[operator] = [
            plan_next_action(
                definition, current_node_id="decide", slots={"days": days}
            ).next_node_id
            for days in (2, 3, 4)
        ]
    return traces["gt"] == expected and traces["gte"] != expected, {
        "expected": expected,
        "traces": traces,
        "compile_diagnostics": diagnostics,
        "scope": "只验证条件路径；hr 是观察点，不代表真实人工任务已执行",
    }


def main() -> int:
    """执行所有独立探针并可保存 JSON；存在闭环失败返回 1，脚本错误返回 2。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="可选的审核 JSON 输出文件")
    arguments = parser.parse_args()
    probes: list[tuple[str, Probe]] = [
        ("direct_publication_consumption", direct_publication_consumption),
        ("draft_then_publish", draft_then_publish),
        ("update_employee_branch", update_employee_branch),
        ("branch_global_version_collision", branch_global_version_collision),
        ("generation_normalization_fidelity", generation_normalization_fidelity),
        ("repeated_tool_receipt_keys", repeated_tool_receipt_keys),
        ("fabricated_future_input", fabricated_future_input),
        ("independent_boundary_oracle", independent_boundary_oracle),
    ]
    results = []
    for name, probe in probes:
        try:
            passed, details = probe()
            results.append({"case": name, "status": "pass" if passed else "fail", **details})
        except Exception as exc:
            results.append(
                {"case": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
    counts = {
        status: sum(row["status"] == status for row in results)
        for status in ("pass", "fail", "error")
    }
    report = {
        "scope": "源码边界审核；内存 SQLite；合成候选；模拟工具回执；无真实模型与外部业务调用",
        "authoring_feature_implemented": False,
        "counts": counts,
        "results": results,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 2 if counts["error"] else (1 if counts["fail"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
