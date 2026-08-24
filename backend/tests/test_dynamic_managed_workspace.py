"""
@Time       : 2026/08/13 03:20
@Author     : zhanglp8181
@File       : test_dynamic_managed_workspace.py
@CallChain  : pytest → DynamicTaskAgent → Attention/Operation → managed workspace ToolExecutor
@Description: 验证代码读取、写前零副作用、独立审批、恢复派发和结果闭环。
"""

from __future__ import annotations

import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ExecutionSignal,
    ModelConfig,
    SopOperation,
    SopWorkItem,
    Tenant,
    Tool,
    User,
)
from app.dynamic_tasks.agent import DynamicTaskAgent
from app.dynamic_tasks.worker import process_dynamic_task_signal
from app.dynamic_tasks.capability_catalog import (
    DynamicCapabilityCatalog,
    ToolReliabilityContract,
    capability_checksum,
    publish_tool_contract,
)
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    NormalizedPlan,
    PlanStep,
    RuntimeActionProposal,
    SuccessCriterion,
)
from app.sop_runtime.execution_control import ExecutionControlService
from app.sop_runtime.execution_store import SopExecutionStore


def _git(repo: Path, *args: str) -> None:
    """初始化验收仓库所需的固定 Git 命令。"""

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _contract(risk: str, handler: str) -> ToolReliabilityContract:
    """构造读取或本地写的严格动态可靠性契约。"""

    if handler == "read_file":
        allowed_paths = ["input.path", "output.content", "output.sha256"]
        user_paths = ["output.sha256"]
    elif handler == "apply_file":
        allowed_paths = [
            "input.path",
            "input.expected_sha256",
            "input.content",
            "output.sha256",
            "output.branch",
        ]
        user_paths = ["output.sha256", "output.branch"]
    elif handler == "run_check":
        allowed_paths = [
            "input.profile",
            "output.profile",
            "output.passed",
            "output.exit_code",
        ]
        user_paths = ["output.profile", "output.passed", "output.exit_code"]
    else:
        allowed_paths = [
            "input.message",
            "input.paths",
            "output.commit_sha",
            "output.branch",
        ]
        user_paths = ["output.commit_sha", "output.branch"]
    return ToolReliabilityContract.model_validate(
        {
            "risk_class": risk,
            "side_effect": "none" if risk == "read" else "local",
            "confirmation_policy": "none" if risk == "read" else "once",
            "idempotency": {"mode": "none"},
            "reconcile": {"supported": False},
            "model_visibility": {
                "allowed_paths": allowed_paths,
                "user_display_paths": user_paths,
                "audit_only_paths": [],
            },
            "timeout_policy": "failed",
            "dynamic_task_enabled": True,
        }
    )


class _WorkspaceProposer:
    """按冻结步骤返回真实文件读取、写入和最终答复提案。"""

    def __init__(self, before_sha: str) -> None:
        """保存补丁乐观锁使用的初始内容哈希。"""

        self.before_sha = before_sha

    def propose(self, *, view, step: PlanStep) -> CompletedProviderProposal:
        """根据当前步骤生成精确参数，不传递 workspace 根或执行命令。"""

        if step.kind == "tool.read":
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref="workspace.refund.read",
                arguments={"path": "refund.py"},
                rationale="先读取当前实现和内容哈希",
            )
        elif step.kind == "tool.write":
            if step.step_key == "apply_refund":
                proposal = RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL,
                    capability_ref="workspace.refund.apply",
                    arguments={
                        "path": "refund.py",
                        "expected_sha256": self.before_sha,
                        "content": "STATUS = 'approval_required'\n",
                    },
                    rationale="按已读取版本提交受控修改",
                )
            else:
                proposal = RuntimeActionProposal(
                    action_kind=ActionKind.CALL_TOOL,
                    capability_ref="workspace.refund.commit",
                    arguments={
                        "message": "feat: require high refund approval",
                        "paths": ["refund.py"],
                    },
                    rationale="在所有检查通过后提交一次性任务分支",
                )
        elif step.kind == "tool.execute":
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.CALL_TOOL,
                capability_ref="workspace.refund.check",
                arguments={"profile": "backend-unit"},
                rationale="在无网络受限容器运行固定回归",
            )
        else:
            completed = [item["step_key"] for item in view.execution_context["completed_steps"]]
            proposal = RuntimeActionProposal(
                action_kind=ActionKind.ANSWER,
                arguments={
                    "markdown": "代码已在一次性任务分支完成审批后修改。",
                    "criterion_evidence": {"changed": completed},
                    "pending_questions": [],
                },
                rationale="依据持久化读写 Operation 形成结果",
            )
        return CompletedProviderProposal(
            response_id=f"response-{step.step_key}",
            finish_reason="stop",
            proposal=proposal,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def test_dynamic_workspace_write_waits_for_approval_and_resumes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证确认前零副作用，批准后的长续跑受 signal 心跳保护且可安全重放。"""

    heartbeat_signal_ids: list[str] = []
    original_heartbeat = DynamicTaskAgent._signal_lease_heartbeat

    @contextmanager
    def record_signal_heartbeat(
        self: DynamicTaskAgent,
        signal_id: str,
        *,
        worker_id: str,
    ) -> Iterator[None]:
        """记录每次本地工具成功后的继续执行是否处于同一 signal 续租作用域。"""

        heartbeat_signal_ids.append(signal_id)
        with original_heartbeat(self, signal_id, worker_id=worker_id):
            yield

    monkeypatch.setattr(
        DynamicTaskAgent,
        "_signal_lease_heartbeat",
        record_signal_heartbeat,
    )

    root = tmp_path / "managed"
    repo = root / "tenant_workspace" / "refund-demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "robot@example.invalid")
    _git(repo, "config", "user.name", "Workspace Robot")
    initial = "STATUS = 'pending'\n"
    (repo / "refund.py").write_text(initial, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_refund.py").write_text(
        "import unittest\n"
        "from refund import STATUS\n\n"
        "class RefundApprovalTest(unittest.TestCase):\n"
        "    def test_high_refund_requires_approval(self):\n"
        "        self.assertEqual(STATUS, 'approval_required')\n",
        encoding="utf-8",
    )
    _git(repo, "add", "refund.py", "tests/test_refund.py")
    _git(repo, "commit", "-m", "baseline")
    before_sha = hashlib.sha256(initial.encode()).hexdigest()

    settings = get_settings()
    old_enabled = settings.dynamic_task_managed_workspace_enabled
    old_root = settings.dynamic_task_managed_workspace_root
    settings.dynamic_task_managed_workspace_enabled = True
    settings.dynamic_task_managed_workspace_root = str(root)
    try:
        engine = create_engine("sqlite://", poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as db:
            tenant = Tenant(id="tenant_workspace", name="Workspace tenant")
            requester = User(
                id="workspace_requester",
                tenant_id=tenant.id,
                username="requester",
                password_hash="x",
                role="member",
            )
            approver = User(
                id="workspace_approver",
                tenant_id=tenant.id,
                username="approver",
                password_hash="x",
                role="admin",
            )
            agent = AgentProfile(
                id="agent_workspace",
                tenant_id=tenant.id,
                owner_user_id=requester.id,
                name="研发交付数字员工",
                status="active",
            )
            chat = ChatSession(
                id="session_workspace",
                tenant_id=tenant.id,
                user_id=requester.id,
                agent_id=agent.id,
            )
            facts = {
                "protocol_version": "dynamic-v1",
                "sdk_available": True,
                "credentials_verified": True,
                "tool_calling": True,
                "structured_output": True,
            }
            model = ModelConfig(
                id="model_workspace",
                tenant_id=tenant.id,
                name="Workspace model",
                api_key_encrypted="x",
                model="workspace-model",
                preflight_status="ready",
                capability_snapshot_json=facts,
                capability_checksum=capability_checksum(facts),
            )
            db.add_all([tenant, requester, approver, agent, chat, model])
            db.flush()
            image = (
                "python@sha256:"
                "9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4"
            )
            schemas = {
                "read": (
                    {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "sha256": {"type": "string"},
                        },
                    },
                ),
                "local_write": (
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "expected_sha256": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "expected_sha256", "content"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "sha256": {"type": "string"},
                            "branch": {"type": "string"},
                        },
                    },
                ),
                "execute": (
                    {
                        "type": "object",
                        "properties": {"profile": {"type": "string"}},
                        "required": ["profile"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "profile": {"type": "string"},
                            "passed": {"type": "boolean"},
                            "exit_code": {"type": "integer"},
                        },
                    },
                ),
                "commit": (
                    {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "paths": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["message", "paths"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "commit_sha": {"type": "string"},
                            "branch": {"type": "string"},
                        },
                    },
                ),
            }
            tools: list[Tool] = []
            for name, handler, risk, schema_key in (
                ("workspace.refund.read", "read_file", "read", "read"),
                ("workspace.refund.apply", "apply_file", "local_write", "local_write"),
                ("workspace.refund.check", "run_check", "execute", "execute"),
                ("workspace.refund.commit", "commit", "local_write", "commit"),
            ):
                input_schema, output_schema = schemas[schema_key]
                config = {
                    "workspace_id": "refund-demo",
                    "base_ref": "main",
                    "handler": handler,
                }
                if handler == "run_check":
                    config["check_profiles"] = {
                        "backend-unit": {
                            "image": image,
                            "argv": [
                                "python",
                                "-m",
                                "unittest",
                                "discover",
                                "-s",
                                "tests",
                                "-v",
                            ],
                            "timeout_seconds": 60,
                        }
                    }
                tool = Tool(
                    tenant_id=tenant.id,
                    name=name,
                    tool_type="managed_workspace",
                    method="POST",
                    url="",
                    config_json=config,
                    input_schema=input_schema,
                    output_schema=output_schema,
                )
                publish_tool_contract(tool, _contract(risk, handler))
                db.add(tool)
                db.flush()
                ensure_private_resource_binding(
                    db, tenant.id, agent.id, "tool", tool.id, "active"
                )
                tools.append(tool)
            db.commit()
            snapshots = DynamicCapabilityCatalog(db).list_tools(tenant.id, agent.id)
            plan = NormalizedPlan(
                goal="修改退款状态",
                success_criteria=(
                    SuccessCriterion(id="changed", type="assertion", spec={"required": True}),
                ),
                steps=(
                    PlanStep(
                        step_key="read_refund",
                        title="读取退款实现",
                        kind="tool.read",
                        capability_refs=("workspace.refund.read",),
                    ),
                    PlanStep(
                        step_key="apply_refund",
                        title="修改退款实现",
                        kind="tool.write",
                        depends_on=("read_refund",),
                        capability_refs=("workspace.refund.apply",),
                    ),
                    PlanStep(
                        step_key="check_refund",
                        title="运行退款回归",
                        kind="tool.execute",
                        depends_on=("apply_refund",),
                        capability_refs=("workspace.refund.check",),
                    ),
                    PlanStep(
                        step_key="commit_refund",
                        title="提交退款变更",
                        kind="tool.write",
                        depends_on=("check_refund",),
                        capability_refs=("workspace.refund.commit",),
                    ),
                    PlanStep(
                        step_key="answer_result",
                        title="形成交付说明",
                        kind="answer",
                        depends_on=("commit_refund",),
                    ),
                ),
                budget={"max_steps": 5, "max_tool_calls": 4, "max_model_calls": 8},
            )
            instance, _ = SopExecutionStore(db).start_dynamic_instance(
                tenant_id=tenant.id,
                session_id=chat.id,
                agent_id=agent.id,
                initiator_user_id=requester.id,
                plan=plan,
                capability_snapshot={
                    "tools": [item.model_dump(mode="json") for item in snapshots],
                    "connectors": [],
                    "knowledge": [],
                    "general_skills": [],
                    "model": {
                        "model_config_id": model.id,
                        "capabilities": facts,
                        "checksum": model.capability_checksum,
                    },
                },
                source_kind="chat",
                source_ref="workspace-message",
            )
            instance.context_json = {
                "dynamic_budget_usage": {"model_calls": 0, "tool_calls": 0}
            }
            db.add(instance)
            db.commit()
            dynamic = DynamicTaskAgent(
                db,
                action_proposer=_WorkspaceProposer(before_sha),
            )

            waiting = dynamic.run_until_blocked_or_complete(
                execution_id=instance.id,
                model_config=model,
                worker_id="workspace-prepare",
                actor_user_id=requester.id,
            )
            assert waiting.status == "waiting"
            assert (repo / "refund.py").read_text(encoding="utf-8") == initial
            last_signal: ExecutionSignal | None = None
            completed = waiting
            for index in range(3):
                attention = db.exec(
                    select(SopWorkItem)
                    .where(
                        SopWorkItem.attention_kind == "tool_approval",
                        SopWorkItem.status == "offered",
                    )
                    .order_by(SopWorkItem.created_at.desc())
                ).first()
                assert attention is not None
                control = ExecutionControlService(db)
                with control.store.owned(instance, worker_id=f"workspace-resolve-{index}"):
                    control.resolve_attention(
                        instance,
                        attention,
                        actor_user_id=approver.id,
                        command_id=f"workspace-allow-once-{index}",
                        command="allow_once",
                        expected_revision=attention.revision,
                    )
                db.commit()
                last_signal = next(
                    signal
                    for signal in db.exec(
                        select(ExecutionSignal).where(
                            ExecutionSignal.causation_type == "attention_resolution"
                        )
                    ).all()
                    if signal.payload_json.get("attention_id") == attention.id
                )
                if index == 0:
                    dynamic = DynamicTaskAgent(
                        db,
                        action_proposer=_WorkspaceProposer(before_sha),
                    )
                if index == 1:
                    check_binding = db.exec(
                        select(AgentResourceBinding).where(
                            AgentResourceBinding.tenant_id == tenant.id,
                            AgentResourceBinding.agent_id == agent.id,
                            AgentResourceBinding.resource_type == "tool",
                            AgentResourceBinding.resource_id == tools[2].id,
                        )
                    ).one()
                    check_binding.status = "inactive"
                    db.add(check_binding)
                    db.commit()
                    assert process_dynamic_task_signal(
                        db,
                        last_signal,
                        agent_factory=lambda session: DynamicTaskAgent(
                            session,
                            action_proposer=_WorkspaceProposer(before_sha),
                        ),
                    ) is None
                    db.refresh(last_signal)
                    assert last_signal.status == "pending"
                    assert last_signal.last_error_json == {
                        "code": "CAPABILITY_BINDING_REVOKED"
                    }
                    ensure_private_resource_binding(
                        db,
                        tenant.id,
                        agent.id,
                        "tool",
                        tools[2].id,
                        "active",
                    )
                    last_signal.available_at = SopExecutionStore(db).database_now()
                    db.add(last_signal)
                    db.commit()
                    completed = process_dynamic_task_signal(
                        db,
                        last_signal,
                        agent_factory=lambda session: DynamicTaskAgent(
                            session,
                            action_proposer=_WorkspaceProposer(before_sha),
                        ),
                    )
                    assert completed is not None
                else:
                    completed = dynamic.resume_tool_approval_signal(
                        signal_id=last_signal.id,
                        model_config=model,
                        worker_id=f"workspace-dispatch-{index}",
                        actor_user_id=approver.id,
                    )
                assert completed.status == ("waiting" if index < 2 else "succeeded")
            assert last_signal is not None
            replay = dynamic.resume_tool_approval_signal(
                signal_id=last_signal.id,
                model_config=model,
                worker_id="workspace-replay",
                actor_user_id=approver.id,
            )
            assert completed.status == replay.status == "succeeded"
            assert sorted(heartbeat_signal_ids) == sorted(
                signal.id
                for signal in db.exec(
                    select(ExecutionSignal).where(
                        ExecutionSignal.causation_type == "attention_resolution"
                    )
                ).all()
            )
            assert (repo / "refund.py").read_text(encoding="utf-8") == (
                "STATUS = 'approval_required'\n"
            )
            operations = db.exec(select(SopOperation).order_by(SopOperation.created_at)).all()
            assert [item.effect_kind for item in operations] == [
                "read",
                "local_write",
                "execute",
                "local_write",
            ]
            assert all(item.status == "succeeded" for item in operations)
            assert all(item.approved_by_user_id == approver.id for item in operations[1:])
            assert operations[1].effect_state == operations[3].effect_state == "complete"
            assert operations[2].result_json["data"]["passed"] is True
            assert operations[3].result_json["data"]["commit_sha"]
    finally:
        settings.dynamic_task_managed_workspace_enabled = old_enabled
        settings.dynamic_task_managed_workspace_root = old_root
