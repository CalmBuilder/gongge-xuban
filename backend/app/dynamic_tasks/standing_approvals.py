"""
@Time       : 2026/08/11 22:20
@Author     : zhanglp8181
@File       : standing_approvals.py
@CallChain  : Standing Approval API/DynamicCapabilityCatalog/DynamicTaskAgent → 规则治理与匹配
@Description: 创建、撤销并在调度外部写派发前重新验证精确目标长期批准规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping

from sqlmodel import Session, select

from app.agents.identity import agent_owner_user_id
from app.audit.service import append_management_audit
from app.config import get_settings
from app.connectors.service import (
    WECOM_MESSAGE_SEND_ACTION,
    ConnectionError,
    authorize_connection_write_actor,
)
from app.db.models import (
    AgentConnectionBinding,
    AgentProfile,
    ConnectionProfile,
    ConnectorThreadBinding,
    ScheduledTask,
    ScheduledTaskRun,
    SopInstance,
    SopOperation,
    StandingApprovalCommandReceipt,
    StandingApprovalRule,
    User,
    utc_now,
)
from app.dynamic_tasks.capability_catalog import CapabilitySnapshot, capability_checksum
from app.organization.governance import has_governance_permission
from app.sop_runtime.execution_control import canonical_checksum


STANDING_APPROVAL_PERMISSION_CODE = "dynamic_task.standing_approval.manage"
SUPPORTED_TOOL_ACTION = WECOM_MESSAGE_SEND_ACTION
MAX_RULE_LIFETIME = timedelta(days=90)


class StandingApprovalError(ValueError):
    """表示长期批准管理或匹配过程中的稳定业务拒绝。"""

    def __init__(self, code: str) -> None:
        """保存可供 API 和 Runtime 断言的错误码。"""

        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StandingApprovalMatch:
    """保存一次派发命中的权威规则、规则创建人与审计证据。"""

    rule: StandingApprovalRule
    authorization_actor_user_id: str
    evidence: dict[str, object]


@dataclass(frozen=True, slots=True)
class StandingApprovalCandidate:
    """描述管理端可选择的精确企业微信会话，不包含外部接收者原始标识。"""

    thread_binding_id: str
    profile_id: str
    profile_display_name: str
    target_label: str
    tool_snapshot_checksum: str
    target_hash: str


def schedule_definition_checksum(task: ScheduledTask) -> str:
    """只对影响无人值守授权语义的调度定义字段计算稳定摘要。"""

    return canonical_checksum(
        {
            "tenant_id": task.tenant_id,
            "scheduled_task_id": task.id,
            "agent_id": task.agent_id,
            "created_by_user_id": task.created_by_user_id,
            "prompt": task.prompt,
            "schedule_type": task.schedule_type,
            "schedule": dict(task.schedule_json or {}),
            "timezone": task.timezone,
            "rrule": task.rrule,
            "concurrency_policy": task.concurrency_policy,
            "misfire_policy": task.misfire_policy,
        }
    )


def list_standing_approval_candidates(
    db: Session,
    *,
    tenant_id: str,
    source_schedule_id: str,
    current_user: User,
) -> list[StandingApprovalCandidate]:
    """列出当前真人可为指定调度授权的精确会话，所有资源逐项按当前状态过滤。"""

    _require_rule_manager(db, tenant_id=tenant_id, user=current_user)
    task = db.get(ScheduledTask, source_schedule_id)
    agent = db.get(AgentProfile, task.agent_id) if task is not None else None
    if (
        task is None
        or task.tenant_id != tenant_id
        or task.status != "active"
        or not _can_manage_schedule_and_agent(db, current_user, task, agent)
    ):
        raise StandingApprovalError("STANDING_APPROVAL_RESOURCE_SCOPE_DENIED")
    threads = db.exec(
        select(ConnectorThreadBinding)
        .where(
            ConnectorThreadBinding.tenant_id == tenant_id,
            ConnectorThreadBinding.agent_id == task.agent_id,
            ConnectorThreadBinding.provider == "wecom",
            ConnectorThreadBinding.status == "active",
        )
        .order_by(ConnectorThreadBinding.updated_at.desc(), ConnectorThreadBinding.id)
    ).all()
    candidates: list[StandingApprovalCandidate] = []
    for thread in threads:
        profile = db.get(ConnectionProfile, thread.profile_id)
        if profile is None:
            continue
        binding = db.exec(
            select(AgentConnectionBinding).where(
                AgentConnectionBinding.tenant_id == tenant_id,
                AgentConnectionBinding.agent_id == task.agent_id,
                AgentConnectionBinding.profile_id == profile.id,
                AgentConnectionBinding.enabled.is_(True),
            )
        ).first()
        if (
            binding is None
            or profile.provider != "wecom"
            or profile.status != "active"
            or SUPPORTED_TOOL_ACTION not in set(profile.tool_allowlist_json or [])
            or SUPPORTED_TOOL_ACTION not in set(binding.allowed_actions_json or [])
        ):
            continue
        snapshot = _wecom_snapshot(profile, binding, thread)
        principal = db.get(User, thread.user_id)
        if (
            principal is None
            or principal.tenant_id != tenant_id
            or principal.membership_status != "active"
        ):
            continue
        principal_name = principal.display_name or principal.username
        candidates.append(
            StandingApprovalCandidate(
                thread_binding_id=thread.id,
                profile_id=profile.id,
                profile_display_name=profile.display_name,
                target_label=f"{profile.display_name} · {principal_name} 的企业微信会话",
                tool_snapshot_checksum=snapshot.checksum,
                target_hash=str(snapshot.contract["target_checksum"]),
            )
        )
    return candidates


def create_standing_approval_rule(
    db: Session,
    *,
    tenant_id: str,
    command_id: str,
    current_user: User,
    agent_id: str,
    source_schedule_id: str,
    profile_id: str,
    thread_binding_id: str,
    tool_action: str,
    argument_constraints: Mapping[str, object],
    valid_from: datetime,
    valid_to: datetime,
) -> StandingApprovalRule:
    """由具备多重治理和业务权限的真人创建不可原地扩权的精确规则。"""

    payload = {
        "agent_id": agent_id,
        "source_schedule_id": source_schedule_id,
        "profile_id": profile_id,
        "thread_binding_id": thread_binding_id,
        "tool_action": tool_action,
        "argument_constraints": dict(argument_constraints),
        "valid_from": valid_from.isoformat(),
        "valid_to": valid_to.isoformat(),
    }
    payload_checksum = canonical_checksum(payload)
    replay = _command_replay(
        db,
        tenant_id=tenant_id,
        command_id=command_id,
        command_type="create",
        actor_user_id=current_user.id,
        payload_checksum=payload_checksum,
    )
    if replay is not None:
        return replay
    _require_rule_manager(db, tenant_id=tenant_id, user=current_user)
    now = utc_now()
    valid_from = _utc_naive(valid_from)
    valid_to = _utc_naive(valid_to)
    if tool_action != SUPPORTED_TOOL_ACTION:
        raise StandingApprovalError("STANDING_APPROVAL_TOOL_UNSUPPORTED")
    if valid_from < now - timedelta(minutes=5) or valid_to <= valid_from:
        raise StandingApprovalError("STANDING_APPROVAL_VALIDITY_INVALID")
    if valid_to - valid_from > MAX_RULE_LIFETIME:
        raise StandingApprovalError("STANDING_APPROVAL_VALIDITY_TOO_LONG")
    constraints = normalize_argument_constraints(argument_constraints)
    task = db.get(ScheduledTask, source_schedule_id)
    agent = db.get(AgentProfile, agent_id)
    profile = db.get(ConnectionProfile, profile_id)
    thread = db.get(ConnectorThreadBinding, thread_binding_id)
    target_user = db.get(User, thread.user_id) if thread is not None else None
    binding = db.exec(
        select(AgentConnectionBinding).where(
            AgentConnectionBinding.tenant_id == tenant_id,
            AgentConnectionBinding.agent_id == agent_id,
            AgentConnectionBinding.profile_id == profile_id,
            AgentConnectionBinding.enabled.is_(True),
        )
    ).first()
    if (
        task is None
        or task.tenant_id != tenant_id
        or task.agent_id != agent_id
        or task.status != "active"
    ):
        raise StandingApprovalError("STANDING_APPROVAL_SCHEDULE_INVALID")
    if not _can_manage_schedule_and_agent(db, current_user, task, agent):
        raise StandingApprovalError("STANDING_APPROVAL_RESOURCE_SCOPE_DENIED")
    if (
        profile is None
        or profile.tenant_id != tenant_id
        or profile.provider != "wecom"
        or profile.status != "active"
        or binding is None
        or tool_action not in set(profile.tool_allowlist_json or [])
        or tool_action not in set(binding.allowed_actions_json or [])
    ):
        raise StandingApprovalError("STANDING_APPROVAL_CONNECTION_INVALID")
    if (
        thread is None
        or thread.tenant_id != tenant_id
        or thread.profile_id != profile_id
        or thread.agent_id != agent_id
        or thread.status != "active"
        or target_user is None
        or target_user.tenant_id != tenant_id
        or target_user.membership_status != "active"
    ):
        raise StandingApprovalError("STANDING_APPROVAL_TARGET_INVALID")
    snapshot = _wecom_snapshot(profile, binding, thread)
    schedule_checksum = schedule_definition_checksum(task)
    active_scope_key = canonical_checksum(
        {
            "agent_id": agent_id,
            "source_schedule_id": task.id,
            "tool_snapshot_checksum": snapshot.checksum,
            "target_hash": snapshot.contract["target_checksum"],
            "argument_constraints": constraints,
        }
    )
    duplicate = db.exec(
        select(StandingApprovalRule).where(
            StandingApprovalRule.tenant_id == tenant_id,
            StandingApprovalRule.source_schedule_id == task.id,
            StandingApprovalRule.agent_id == agent_id,
            StandingApprovalRule.tool_snapshot_checksum == snapshot.checksum,
            StandingApprovalRule.target_hash == str(snapshot.contract["target_checksum"]),
            StandingApprovalRule.status == "active",
            StandingApprovalRule.argument_constraints_json == constraints,
        )
    ).first()
    if duplicate is not None:
        raise StandingApprovalError("STANDING_APPROVAL_ACTIVE_DUPLICATE")
    rule = StandingApprovalRule(
        tenant_id=tenant_id,
        agent_id=agent_id,
        source_schedule_id=task.id,
        source_schedule_checksum=schedule_checksum,
        profile_id=profile.id,
        binding_id=binding.id,
        tool_id=snapshot.name,
        tool_snapshot_checksum=snapshot.checksum,
        target_type="wecom_thread",
        canonical_target=str(snapshot.contract["canonical_target"]),
        target_hash=str(snapshot.contract["target_checksum"]),
        argument_constraints_json=constraints,
        active_scope_key=active_scope_key,
        valid_from=valid_from,
        valid_to=valid_to,
        created_by_user_id=current_user.id,
    )
    db.add(rule)
    db.flush()
    _append_rule_audit(
        db,
        rule=rule,
        actor=current_user,
        action="standing_approval.create",
        outcome="succeeded",
        detail={"command_id": command_id},
    )
    _save_command_receipt(
        db,
        tenant_id=tenant_id,
        command_id=command_id,
        command_type="create",
        actor_user_id=current_user.id,
        payload_checksum=payload_checksum,
        rule=rule,
    )
    db.commit()
    db.refresh(rule)
    return rule


def revoke_standing_approval_rule(
    db: Session,
    *,
    tenant_id: str,
    rule_id: str,
    command_id: str,
    expected_revision: int,
    current_user: User,
) -> StandingApprovalRule:
    """以命令幂等和 revision CAS 撤销规则，不提供任何原地扩权更新。"""

    payload_checksum = canonical_checksum(
        {"rule_id": rule_id, "expected_revision": expected_revision}
    )
    replay = _command_replay(
        db,
        tenant_id=tenant_id,
        command_id=command_id,
        command_type="revoke",
        actor_user_id=current_user.id,
        payload_checksum=payload_checksum,
    )
    if replay is not None:
        return replay
    _require_rule_manager(db, tenant_id=tenant_id, user=current_user)
    rule = db.exec(
        select(StandingApprovalRule)
        .where(
            StandingApprovalRule.id == rule_id,
            StandingApprovalRule.tenant_id == tenant_id,
        )
        .with_for_update()
    ).first()
    if rule is None:
        raise StandingApprovalError("STANDING_APPROVAL_NOT_FOUND")
    if rule.revision != expected_revision:
        raise StandingApprovalError("STANDING_APPROVAL_REVISION_CONFLICT")
    if rule.status != "active":
        raise StandingApprovalError("STANDING_APPROVAL_NOT_ACTIVE")
    rule.status = "revoked"
    rule.active_scope_key = None
    rule.revision += 1
    rule.revoked_by_user_id = current_user.id
    rule.revoked_at = utc_now()
    rule.updated_at = rule.revoked_at
    db.add(rule)
    _append_rule_audit(
        db,
        rule=rule,
        actor=current_user,
        action="standing_approval.revoke",
        outcome="succeeded",
        detail={"command_id": command_id},
    )
    _save_command_receipt(
        db,
        tenant_id=tenant_id,
        command_id=command_id,
        command_type="revoke",
        actor_user_id=current_user.id,
        payload_checksum=payload_checksum,
        rule=rule,
    )
    db.commit()
    db.refresh(rule)
    return rule


def scheduled_write_snapshots(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str,
    initiator_user_id: str,
    run_id: str,
) -> list[CapabilitySnapshot]:
    """仅为来源和规则仍完全匹配的调度运行投影精确企业微信写能力。"""

    settings = get_settings()
    if not (
        settings.dynamic_task_high_risk_external_write_allows(tenant_id, agent_id)
        and settings.dynamic_task_standing_approval_enabled
    ):
        return []
    run = db.get(ScheduledTaskRun, run_id)
    if (
        run is None
        or run.tenant_id != tenant_id
        or run.agent_id != agent_id
        or run.user_id != initiator_user_id
        or run.status != "running"
        or run.source_kind not in {"schedule", "manual"}
        or not _valid_run_source(run)
    ):
        return []
    task = db.get(ScheduledTask, run.scheduled_task_id)
    if task is None or task.status != "active":
        return []
    now = utc_now()
    rules = db.exec(
        select(StandingApprovalRule).where(
            StandingApprovalRule.tenant_id == tenant_id,
            StandingApprovalRule.agent_id == agent_id,
            StandingApprovalRule.source_schedule_id == task.id,
            StandingApprovalRule.status == "active",
            StandingApprovalRule.valid_from <= now,
            StandingApprovalRule.valid_to > now,
        )
    ).all()
    snapshots: list[CapabilitySnapshot] = []
    for rule in rules:
        snapshot = _current_rule_snapshot(db, rule, task=task)
        if snapshot is not None and snapshot.checksum == rule.tool_snapshot_checksum:
            snapshots.append(snapshot)
    return snapshots


def match_standing_approval_rule(
    db: Session,
    *,
    instance: SopInstance,
    snapshot: CapabilitySnapshot,
    arguments: Mapping[str, object],
) -> StandingApprovalMatch | None:
    """在 Operation 派发前锁定并重查规则、来源、资源授权、精确目标和参数。"""

    settings = get_settings()
    if not (
        settings.dynamic_task_high_risk_external_write_allows(
            instance.tenant_id,
            instance.agent_id,
        )
        and settings.dynamic_task_standing_approval_enabled
        and instance.source_kind == "schedule"
    ):
        return None
    run = db.get(ScheduledTaskRun, instance.source_ref)
    if (
        run is None
        or run.execution_id not in {None, instance.id}
        or run.tenant_id != instance.tenant_id
        or run.agent_id != instance.agent_id
        or run.status not in {"running", "waiting"}
        or not _valid_run_source(run)
    ):
        return None
    task = db.get(ScheduledTask, run.scheduled_task_id)
    if task is None or task.status != "active":
        return None
    now = utc_now()
    rules = db.exec(
        select(StandingApprovalRule)
        .where(
            StandingApprovalRule.tenant_id == instance.tenant_id,
            StandingApprovalRule.agent_id == instance.agent_id,
            StandingApprovalRule.source_schedule_id == task.id,
            StandingApprovalRule.tool_id == snapshot.name,
            StandingApprovalRule.tool_snapshot_checksum == snapshot.checksum,
            StandingApprovalRule.target_hash == str(snapshot.contract.get("target_checksum") or ""),
            StandingApprovalRule.status == "active",
            StandingApprovalRule.valid_from <= now,
            StandingApprovalRule.valid_to > now,
        )
        .order_by(StandingApprovalRule.created_at, StandingApprovalRule.id)
        .with_for_update()
    ).all()
    for rule in rules:
        if rule.source_schedule_checksum != schedule_definition_checksum(task):
            continue
        if rule.canonical_target != snapshot.contract.get("canonical_target"):
            continue
        if not arguments_satisfy_constraints(arguments, rule.argument_constraints_json):
            continue
        creator = db.get(User, rule.created_by_user_id)
        if creator is None or creator.membership_status != "active":
            continue
        try:
            _require_rule_manager(db, tenant_id=instance.tenant_id, user=creator)
        except StandingApprovalError:
            continue
        current = _current_rule_snapshot(db, rule, task=task)
        if current is None or current.checksum != snapshot.checksum:
            continue
        evidence = {
            "authorization_source": "standing_rule",
            "standing_rule_id": rule.id,
            "standing_rule_revision": rule.revision,
            "source_schedule_id": task.id,
            "source_schedule_checksum": rule.source_schedule_checksum,
            "tool_snapshot_checksum": rule.tool_snapshot_checksum,
            "canonical_target": rule.canonical_target,
            "target_hash": rule.target_hash,
            "arguments_fingerprint": capability_checksum(dict(arguments)),
            "valid_from": rule.valid_from.isoformat(),
            "valid_to": rule.valid_to.isoformat(),
        }
        return StandingApprovalMatch(rule, creator.id, evidence)
    return None


def _valid_run_source(run: ScheduledTaskRun) -> bool:
    """验证调度运行的不可变来源快照未损坏且与索引身份一致。"""

    snapshot = dict(run.source_snapshot_json or {})
    return bool(snapshot) and (
        run.source_checksum == canonical_checksum(snapshot)
        and snapshot.get("scheduled_task_id") == run.scheduled_task_id
        and snapshot.get("tenant_id") == run.tenant_id
        and snapshot.get("agent_id") == run.agent_id
        and snapshot.get("initiator_user_id") == run.user_id
        and snapshot.get("source_kind") == run.source_kind
        and snapshot.get("source_ref") == run.source_ref
    )


def record_standing_rule_hit(
    db: Session,
    *,
    match: StandingApprovalMatch,
    instance: SopInstance,
    operation: SopOperation,
) -> None:
    """为每次自动放行追加不含正文的治理审计，关联 Execution 与 Operation。"""

    append_management_audit(
        db,
        tenant_id=instance.tenant_id,
        actor_user_id=match.authorization_actor_user_id,
        actor_display_name=None,
        actor_type="standing_rule",
        action="standing_approval.hit",
        action_kind="execute",
        outcome="succeeded",
        resource_type="sop_operation",
        resource_id=operation.id,
        permission_code=STANDING_APPROVAL_PERMISSION_CODE,
        correlation_id=instance.id,
        detail=match.evidence,
    )


def normalize_argument_constraints(raw: Mapping[str, object]) -> dict[str, object]:
    """只接受字段级 equals/enum/长度范围，不允许正则、表达式或可执行代码。"""

    if set(raw) != {"content"} or not isinstance(raw.get("content"), Mapping):
        raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
    constraint = dict(raw["content"])
    allowed = {"equals", "enum", "min_length", "max_length"}
    if not constraint or set(constraint) - allowed:
        raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
    if "equals" in constraint:
        value = constraint["equals"]
        if not isinstance(value, str) or not value or len(value) > 4000:
            raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
    if "enum" in constraint:
        values = constraint["enum"]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 20
            or any(not isinstance(value, str) or not value or len(value) > 4000 for value in values)
        ):
            raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
        constraint["enum"] = list(dict.fromkeys(values))
    for key in ("min_length", "max_length"):
        if key in constraint and (
            not isinstance(constraint[key], int) or not 0 <= int(constraint[key]) <= 4000
        ):
            raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
    if int(constraint.get("min_length", 0)) > int(constraint.get("max_length", 4000)):
        raise StandingApprovalError("STANDING_APPROVAL_ARGUMENT_CONSTRAINTS_INVALID")
    return {"content": constraint}


def arguments_satisfy_constraints(
    arguments: Mapping[str, object],
    constraints: Mapping[str, object],
) -> bool:
    """机械匹配受限参数规则；任何未知字段、类型或约束均 fail closed。"""

    if set(arguments) != {"content"}:
        return False
    value = arguments.get("content")
    raw_constraint = constraints.get("content")
    if not isinstance(value, str) or not isinstance(raw_constraint, Mapping):
        return False
    try:
        normalized = normalize_argument_constraints({"content": dict(raw_constraint)})["content"]
    except StandingApprovalError:
        return False
    if "equals" in normalized and value != normalized["equals"]:
        return False
    if "enum" in normalized and value not in normalized["enum"]:
        return False
    return int(normalized.get("min_length", 0)) <= len(value) <= int(
        normalized.get("max_length", 4000)
    )


def _current_rule_snapshot(
    db: Session,
    rule: StandingApprovalRule,
    *,
    task: ScheduledTask,
) -> CapabilitySnapshot | None:
    """按当前 profile/binding/thread 重新生成能力快照，漂移即不匹配。"""

    if rule.source_schedule_checksum != schedule_definition_checksum(task):
        return None
    profile = db.get(ConnectionProfile, rule.profile_id)
    binding = db.get(AgentConnectionBinding, rule.binding_id)
    thread_id = rule.canonical_target.removeprefix("wecom_thread:")
    thread = db.get(ConnectorThreadBinding, thread_id)
    target_user = db.get(User, thread.user_id) if thread is not None else None
    if (
        profile is None
        or profile.tenant_id != rule.tenant_id
        or profile.provider != "wecom"
        or profile.status != "active"
        or binding is None
        or binding.tenant_id != rule.tenant_id
        or binding.agent_id != rule.agent_id
        or binding.profile_id != rule.profile_id
        or not binding.enabled
        or SUPPORTED_TOOL_ACTION not in set(profile.tool_allowlist_json or [])
        or SUPPORTED_TOOL_ACTION not in set(binding.allowed_actions_json or [])
        or thread is None
        or thread.tenant_id != rule.tenant_id
        or thread.agent_id != rule.agent_id
        or thread.profile_id != rule.profile_id
        or thread.status != "active"
        or target_user is None
        or target_user.tenant_id != rule.tenant_id
        or target_user.membership_status != "active"
    ):
        return None
    return _wecom_snapshot(profile, binding, thread)


def _utc_naive(value: datetime) -> datetime:
    """把 API 的带时区时间归一为项目数据库使用的 UTC naive 语义。"""

    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _wecom_snapshot(
    profile: ConnectionProfile,
    binding: AgentConnectionBinding,
    thread: ConnectorThreadBinding,
) -> CapabilitySnapshot:
    """复用目录的唯一企业微信 canonicalizer 生成可比较快照。"""

    from app.dynamic_tasks.capability_catalog import DynamicCapabilityCatalog

    return DynamicCapabilityCatalog._wecom_message_snapshot(profile, binding, thread)


def _require_rule_manager(db: Session, *, tenant_id: str, user: User) -> None:
    """同时要求专门治理权、连接管理权和外部写业务权，任一撤销立即失效。"""

    if user.tenant_id != tenant_id or user.membership_status != "active":
        raise StandingApprovalError("STANDING_APPROVAL_MANAGER_DENIED")
    for permission_code in (
        STANDING_APPROVAL_PERMISSION_CODE,
        "connection_profile.manage",
        "agent.manage",
    ):
        if not has_governance_permission(
            db,
            tenant_id=tenant_id,
            user_id=user.id,
            permission_code=permission_code,
        ):
            raise StandingApprovalError("STANDING_APPROVAL_MANAGER_DENIED")
    try:
        authorize_connection_write_actor(
            db,
            tenant_id=tenant_id,
            actor_user_id=user.id,
        )
    except ConnectionError as exc:
        raise StandingApprovalError("STANDING_APPROVAL_MANAGER_DENIED") from exc


def _can_manage_schedule_and_agent(
    db: Session,
    user: User,
    task: ScheduledTask,
    agent: AgentProfile | None,
) -> bool:
    """要求有效 Agent 且用户拥有调度任务或具备 Execution 治理权，禁止仅凭平台角色旁路。"""

    if agent is None or agent.tenant_id != task.tenant_id or agent.status != "active":
        return False
    schedule_scope = task.created_by_user_id == user.id or has_governance_permission(
        db,
        tenant_id=task.tenant_id,
        user_id=user.id,
        permission_code="execution.manage",
    )
    agent_scope = agent_owner_user_id(agent) == user.id or has_governance_permission(
        db,
        tenant_id=task.tenant_id,
        user_id=user.id,
        permission_code="agent.manage",
    )
    return schedule_scope and agent_scope


def _command_replay(
    db: Session,
    *,
    tenant_id: str,
    command_id: str,
    command_type: str,
    actor_user_id: str,
    payload_checksum: str,
) -> StandingApprovalRule | None:
    """相同命令返回原规则，不同语义或 actor 复用同一 ID 时拒绝。"""

    receipt = db.exec(
        select(StandingApprovalCommandReceipt).where(
            StandingApprovalCommandReceipt.tenant_id == tenant_id,
            StandingApprovalCommandReceipt.command_id == command_id,
        )
    ).first()
    if receipt is None:
        return None
    if (
        receipt.command_type != command_type
        or receipt.actor_user_id != actor_user_id
        or receipt.payload_checksum != payload_checksum
    ):
        raise StandingApprovalError("STANDING_APPROVAL_COMMAND_CONFLICT")
    rule = db.get(StandingApprovalRule, receipt.rule_id)
    if rule is None or rule.tenant_id != tenant_id:
        raise StandingApprovalError("STANDING_APPROVAL_COMMAND_CORRUPT")
    return rule


def _save_command_receipt(
    db: Session,
    *,
    tenant_id: str,
    command_id: str,
    command_type: str,
    actor_user_id: str,
    payload_checksum: str,
    rule: StandingApprovalRule,
) -> None:
    """与规则变更同事务保存最小幂等回执，不复制敏感参数。"""

    db.add(
        StandingApprovalCommandReceipt(
            tenant_id=tenant_id,
            command_id=command_id,
            command_type=command_type,
            actor_user_id=actor_user_id,
            payload_checksum=payload_checksum,
            rule_id=rule.id,
            result_json={"rule_id": rule.id, "revision": rule.revision, "status": rule.status},
        )
    )


def _append_rule_audit(
    db: Session,
    *,
    rule: StandingApprovalRule,
    actor: User,
    action: str,
    outcome: str,
    detail: Mapping[str, object],
) -> None:
    """记录规则治理动作，只保存约束摘要和资源身份，不写正文。"""

    append_management_audit(
        db,
        tenant_id=rule.tenant_id,
        actor_user_id=actor.id,
        actor_display_name=actor.display_name or actor.username,
        action=action,
        action_kind="manage",
        outcome=outcome,
        resource_type="standing_approval_rule",
        resource_id=rule.id,
        permission_code=STANDING_APPROVAL_PERMISSION_CODE,
        before={"status": "active"} if action.endswith("revoke") else {},
        after={"status": rule.status, "revision": rule.revision},
        detail={
            **dict(detail),
            "source_schedule_id": rule.source_schedule_id,
            "agent_id": rule.agent_id,
            "profile_id": rule.profile_id,
            "tool_id": rule.tool_id,
            "target_hash": rule.target_hash,
            "constraints_checksum": capability_checksum(rule.argument_constraints_json),
            "valid_from": rule.valid_from.isoformat(),
            "valid_to": rule.valid_to.isoformat(),
        },
    )
