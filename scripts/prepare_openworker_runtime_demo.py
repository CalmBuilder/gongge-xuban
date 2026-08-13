"""
@Time       : 2026/08/13 12:05
@Author     : zhanglp8181
@File       : prepare_openworker_runtime_demo.py
@CallChain  : 手工验收命令 → 正式数据库/Execution Store/DynamicTaskAgent → 浏览器会话
@Description: 建立可重复的即时 Skill、空目录反例和真实并行读取浏览器验收会话。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlmodel import Session, select  # noqa: E402

from app.agents.branching import ensure_private_resource_binding  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentProfile,
    ChatSession,
    Message,
    ModelConfig,
    Tool,
    User,
    new_id,
)
from app.dynamic_tasks.agent import DynamicTaskAgent  # noqa: E402
from app.dynamic_tasks.capability_catalog import (  # noqa: E402
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    publish_tool_contract,
)
from app.dynamic_tasks.planning import (  # noqa: E402
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_store import SopExecutionStore  # noqa: E402
from app.tools.tool_schema import ToolResult  # noqa: E402


TENANT_ID = "tenant_demo"
USER_ID = "user_demo"
SKILL_AGENT_ID = "agent_skill_demo_a_docs"
EMPTY_AGENT_ID = "agent_skill_demo_b_diagnosis"


class DemoParallelProposer:
    """为两个演示只读步骤生成冻结、无副作用且可审计的确定性提案。"""

    def propose(self, *, view, step):
        """把计划步骤映射到同名演示只读能力，不调用模型伪造并行时序。"""

        del view
        return CompletedProviderProposal(
            response_id=f"demo-response-{step.step_key}",
            finish_reason="stop",
            proposal=RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref=step.capability_refs[0],
                arguments={"source": step.capability_refs[0]},
                rationale="读取两个互不依赖的演示事实源",
            ),
        )


class DemoParallelExecutor:
    """用 barrier 形成可证明的两个独立 Session 同时在途读取。"""

    barrier = threading.Barrier(2)

    def __init__(self, db: Session):
        """保存每个调度项独享的数据库会话，禁止跨线程共享协调 Session。"""

        self.db = db

    def execute(self, tenant_id, tool_call, **kwargs):
        """等待同波另一读取到达后返回确定性、无敏感字段的结果。"""

        del tenant_id, kwargs
        self.barrier.wait(timeout=10)
        return ToolResult(
            tool_name=tool_call.name,
            success=True,
            data={"source": tool_call.name, "verified": True},
        )


def main() -> None:
    """建立三个浏览器会话并输出不含凭据的验收 URL 与 Execution ID。"""

    with Session(engine, expire_on_commit=False) as db:
        user, model = _required_identities(db)
        instant = _create_answer_only_session(
            db,
            user=user,
            model=model,
            agent_id=SKILL_AGENT_ID,
            label="运行中增加 Skill 正向场景",
        )
        empty = _create_answer_only_session(
            db,
            user=user,
            model=model,
            agent_id=EMPTY_AGENT_ID,
            label="运行中增加 Skill 空目录反例",
        )
        parallel = _create_parallel_session(db, user=user, model=model)
        print(json.dumps({"instant_skill": instant, "empty_skill": empty, "parallel": parallel}, ensure_ascii=False, indent=2))


def _required_identities(db: Session) -> tuple[User, ModelConfig]:
    """解析现有演示用户、三个数字员工和已通过预检的模型，不创建账号或凭据。"""

    user = db.get(User, USER_ID)
    if user is None or user.tenant_id != TENANT_ID or user.membership_status != "active":
        raise RuntimeError("活动演示用户 user_demo 不存在")
    for agent_id in (SKILL_AGENT_ID, EMPTY_AGENT_ID):
        agent = db.get(AgentProfile, agent_id)
        if agent is None or agent.tenant_id != TENANT_ID or agent.owner_user_id != user.id:
            raise RuntimeError(f"请先初始化 Skill 五闭环数字员工：{agent_id}")
    model = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == TENANT_ID,
            ModelConfig.preflight_status == "ready",
        )
    ).first()
    if model is None:
        raise RuntimeError("没有已通过预检的演示模型")
    return user, model


def _create_answer_only_session(
    db: Session,
    *,
    user: User,
    model: ModelConfig,
    agent_id: str,
    label: str,
) -> dict[str, str]:
    """创建停在 answer 安全边界的真实动态 Execution，供页面提交 add_skill 命令。"""

    session_id = new_id("session")
    user_message_id = new_id("msg")
    db.add(ChatSession(id=session_id, tenant_id=TENANT_ID, user_id=user.id, agent_id=agent_id, title=label))
    plan = NormalizedPlan(
        goal="把售后升级处理要求整理成含输入、步骤、异常与验收标准的操作规范",
        success_criteria=(SuccessCriterion(id="complete_spec", type="assertion", spec={"required": True}),),
        steps=(PlanStep(step_key="answer", title="生成完整操作规范", kind="answer"),),
        budget={"max_steps": 4, "max_model_calls": 8, "max_tool_calls": 2},
    )
    instance, _ = SopExecutionStore(db).start_dynamic_instance(
        tenant_id=TENANT_ID,
        session_id=session_id,
        agent_id=agent_id,
        initiator_user_id=user.id,
        plan=plan,
        capability_snapshot={
            "model": {
                "model_config_id": model.id,
                "capabilities": dict(model.capability_snapshot_json or {}),
                "checksum": model.capability_checksum,
            }
        },
        source_kind="chat",
        source_ref=user_message_id,
    )
    db.add(Message(id=user_message_id, tenant_id=TENANT_ID, session_id=session_id, role="user", content=label))
    db.add(Message(tenant_id=TENANT_ID, session_id=session_id, role="assistant", content="动态任务已建立，请在执行卡中继续验收。", metadata_json={"execution_id": instance.id}))
    db.commit()
    return {"session_id": session_id, "execution_id": instance.id, "url": f"/workspace/chat/{session_id}"}


def _create_parallel_session(db: Session, *, user: User, model: ModelConfig) -> dict[str, str]:
    """通过正式 DynamicTaskAgent 派发两个同波读取，并把权威批次呈现在聊天执行卡。"""

    agent = db.get(AgentProfile, SKILL_AGENT_ID)
    assert agent is not None
    tool_names = ("demo.runtime.read_contract", "demo.runtime.read_partner")
    for index, name in enumerate(tool_names):
        tool = db.exec(select(Tool).where(Tool.tenant_id == TENANT_ID, Tool.name == name)).first()
        if tool is None:
            tool = Tool(
                id=f"tool_runtime_demo_{index}",
                tenant_id=TENANT_ID,
                name=name,
                display_name=f"运行时演示读取 {index + 1}",
                method="GET",
                url="https://example.invalid/runtime-demo",
                output_schema={"type": "object", "properties": {"source": {"type": "string"}, "verified": {"type": "boolean"}}},
            )
        publish_tool_contract(
            tool,
            ToolReliabilityContract(
                risk_class="read",
                side_effect="none",
                confirmation_policy="none",
                timeout_policy="failed",
                dynamic_task_enabled=True,
                parallel_safe=True,
                concurrency_key=f"runtime-demo-{index}",
                max_in_flight=2,
            ),
        )
        db.add(tool)
        db.flush()
        ensure_private_resource_binding(db, TENANT_ID, agent.id, "tool", tool.id)
    db.commit()
    catalog = DynamicCapabilityCatalog(db)
    snapshots = [row for row in catalog.list_tools(TENANT_ID, agent.id) if row.name in tool_names]
    plan = NormalizedPlan(
        goal="并行读取合同与合作方事实，并按计划顺序汇总",
        success_criteria=(SuccessCriterion(id="two_sources", type="assertion", spec={"required": True}),),
        steps=(
            PlanStep(step_key="read_contract", title="读取合同事实", kind="tool.read", capability_refs=(tool_names[0],)),
            PlanStep(step_key="read_partner", title="读取合作方事实", kind="tool.read", capability_refs=(tool_names[1],)),
            PlanStep(step_key="answer", title="按固定顺序汇总", kind="answer", depends_on=("read_contract", "read_partner")),
        ),
        budget={"max_steps": 4, "max_model_calls": 6, "max_tool_calls": 4},
    )
    session_id = new_id("session")
    user_message_id = new_id("msg")
    db.add(ChatSession(id=session_id, tenant_id=TENANT_ID, user_id=user.id, agent_id=agent.id, title="低风险并行读取增强场景"))
    instance, _ = SopExecutionStore(db).start_dynamic_instance(
        tenant_id=TENANT_ID,
        session_id=session_id,
        agent_id=agent.id,
        initiator_user_id=user.id,
        plan=plan,
        capability_snapshot={
            "tools": [row.model_dump(mode="json") for row in snapshots],
            "model": {"model_config_id": model.id, "capabilities": dict(model.capability_snapshot_json or {}), "checksum": model.capability_checksum},
        },
        source_kind="chat",
        source_ref=user_message_id,
    )
    db.commit()
    previous = get_settings().dynamic_task_max_parallel_reads
    get_settings().dynamic_task_max_parallel_reads = 2
    try:
        DynamicTaskAgent(
            db,
            catalog=catalog,
            action_proposer=DemoParallelProposer(),
            parallel_tool_executor_factory=DemoParallelExecutor,
        ).advance_ready_parallel_reads(
            execution_id=instance.id,
            model_config=model,
            worker_id="runtime-demo-parallel",
            actor_user_id=user.id,
        )
    finally:
        get_settings().dynamic_task_max_parallel_reads = previous
    db.add(Message(id=user_message_id, tenant_id=TENANT_ID, session_id=session_id, role="user", content="并行核验两个只读事实源"))
    db.add(Message(tenant_id=TENANT_ID, session_id=session_id, role="assistant", content="两个低风险读取已由统一 Runtime 同波执行；执行卡展示持久批次与稳定顺序。", metadata_json={"execution_id": instance.id}))
    db.commit()
    return {"session_id": session_id, "execution_id": instance.id, "url": f"/workspace/chat/{session_id}"}


if __name__ == "__main__":
    main()
