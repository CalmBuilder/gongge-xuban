"""
@Time       : 2026/08/10 23:40
@Author     : zhanglp8181
@File       : test_dynamic_task_operations.py
@CallChain  : pytest → dynamic task operations API/service → unified Runtime tables
@Description: 验证租户隔离的脱敏运行聚合、阈值就绪语义和全域审计权限门禁。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.dynamic_task_operations import get_dynamic_task_operational_snapshot
from app.db.models import (
    DynamicTaskQuotaLease,
    ExecutionPublication,
    ExecutionSignal,
    SopInstance,
    SopOperation,
    SopWorkItem,
    Tenant,
    User,
    utc_now,
)
from app.dynamic_tasks.operations import (
    DynamicTaskAlertThresholds,
    DynamicTaskOperationsService,
)
from app.dynamic_tasks.quotas import DynamicTaskQuotaLimits


def test_snapshot_aggregates_only_dynamic_rows_for_requested_tenant() -> None:
    """验证正式 SOP 和其他租户事实不会污染动态任务运行快照。"""

    with _session() as db:
        _seed_tenants(db)
        dynamic = _dynamic_instance("dynamic_a", "tenant_a", status="waiting")
        dynamic.updated_at = utc_now() - timedelta(hours=2)
        db.add(dynamic)
        db.add(_sop_instance("sop_a", "tenant_a"))
        db.add(_dynamic_instance("dynamic_b", "tenant_b", status="running"))
        db.flush()
        db.add(
            ExecutionSignal(
                id="signal_pending",
                tenant_id="tenant_a",
                execution_id=dynamic.id,
                signal_type="timer",
                dedupe_key="signal-pending",
                causation_type="timer",
                causation_id="timer-a",
                payload_checksum="checksum-signal",
                status="pending",
            )
        )
        db.add(
            ExecutionSignal(
                id="signal_cross_tenant_corrupt",
                tenant_id="tenant_b",
                execution_id=dynamic.id,
                signal_type="timer",
                dedupe_key="signal-cross-tenant",
                causation_type="timer",
                causation_id="timer-cross-tenant",
                payload_checksum="checksum-cross-tenant",
                status="dead_letter",
            )
        )
        db.add(
            SopOperation(
                id="operation_unknown",
                tenant_id="tenant_a",
                instance_id=dynamic.id,
                node_execution_id="step_a",
                operation_name="crm.read",
                idempotency_key="operation-key",
                logical_action_id="logical-action",
                request_fingerprint="request-checksum",
                status="unknown",
                effect_kind="external_write",
                effect_state="unknown",
            )
        )
        db.add(
            ExecutionPublication(
                id="publication_pending",
                tenant_id="tenant_a",
                execution_id=dynamic.id,
                result_id="result_a",
                publication_key="publication-key",
                target_type="application",
                required=True,
                status="pending",
            )
        )
        db.add(
            SopWorkItem(
                id="attention_offered",
                tenant_id="tenant_a",
                instance_id=dynamic.id,
                attention_kind="exception",
                attention_identity="attention-identity",
                status="offered",
            )
        )
        db.add(
            DynamicTaskQuotaLease(
                id="quota_tenant",
                tenant_id="tenant_a",
                scope_type="tenant",
                scope_ref="tenant_a",
                slot_number=0,
                holder_type="execution",
                holder_id=dynamic.id,
            )
        )
        db.commit()

        snapshot = DynamicTaskOperationsService(db).snapshot(
            tenant_id="tenant_a",
            thresholds=DynamicTaskAlertThresholds(
                signal_backlog=1,
                dead_letters=1,
                unknown_operations=1,
                publication_backlog=1,
                waiting_age_seconds=60,
            ),
            quota_limits=DynamicTaskQuotaLimits(tenant=8, agent=4, user=2, tool=2),
        )

        assert snapshot["executions"] == {"waiting": 1}
        assert snapshot["signals"] == {"pending": 1}
        assert snapshot["operations"] == {"unknown": 1}
        assert snapshot["publications"] == {"pending": 1}
        assert snapshot["attentions"] == {"offered": 1}
        assert snapshot["quota_limits_configured"] is True
        assert snapshot["quota_limits"] == {"tenant": 8, "agent": 4, "user": 2, "tool": 2}
        assert snapshot["quota_leases"] == {"tenant": 1}
        assert snapshot["oldest_waiting_age_seconds"] >= 7_100
        alerts = {item["code"]: item for item in snapshot["alerts"]}
        assert alerts["signal_backlog"]["triggered"] is True
        assert alerts["unknown_operations"]["triggered"] is True
        assert alerts["publication_backlog"]["triggered"] is True
        assert alerts["waiting_age_seconds"]["triggered"] is True
        assert alerts["dead_letters"]["triggered"] is False


def test_zero_thresholds_are_reported_as_unconfigured_not_healthy() -> None:
    """验证缺少生产阈值时显式返回未就绪，不能把零误解释为无告警。"""

    with _session() as db:
        _seed_tenants(db)

        snapshot = DynamicTaskOperationsService(db).snapshot(
            tenant_id="tenant_a",
            thresholds=DynamicTaskAlertThresholds(),
            quota_limits=DynamicTaskQuotaLimits(),
        )

        assert snapshot["thresholds_configured"] is False
        assert snapshot["quota_limits_configured"] is False
        assert all(item["enabled"] is False for item in snapshot["alerts"])
        assert all(item["threshold"] is None for item in snapshot["alerts"])


def test_operations_api_requires_tenant_wide_audit_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证组织子树审计员不能从聚合数字推断租户范围外运行事实。"""

    with _session() as db:
        owner = _seed_tenants(db)
        monkeypatch.setattr(
            "app.api.dynamic_task_operations.authorized_organization_ids",
            lambda grants, permission_code: frozenset({"org_limited"}),
        )

        with pytest.raises(HTTPException) as denied:
            get_dynamic_task_operational_snapshot(
                tenant_id="tenant_a",
                current_user=owner,
                db=db,
            )

        assert denied.value.status_code == 403
        assert denied.value.detail == "TENANT_WIDE_AUDIT_SCOPE_REQUIRED"


def test_operations_api_returns_validated_snapshot_for_tenant_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证租户全域管理员获得稳定响应模型，且未配置阈值不会被伪装成健康。"""

    with _session() as db:
        owner = _seed_tenants(db)
        monkeypatch.setattr(
            "app.api.dynamic_task_operations.get_settings",
            lambda: SimpleNamespace(
                dynamic_task_alert_signal_backlog_threshold=0,
                dynamic_task_alert_dead_letter_threshold=0,
                dynamic_task_alert_unknown_operation_threshold=0,
                dynamic_task_alert_publication_backlog_threshold=0,
                dynamic_task_alert_waiting_age_seconds=0,
                dynamic_task_max_active_per_tenant=0,
                dynamic_task_max_active_per_agent=0,
                dynamic_task_max_active_per_user=0,
                dynamic_task_max_active_per_tool=0,
            ),
        )

        snapshot = get_dynamic_task_operational_snapshot(
            tenant_id="tenant_a",
            current_user=owner,
            db=db,
        )

        assert snapshot.tenant_id == "tenant_a"
        assert snapshot.thresholds_configured is False
        assert {item.code for item in snapshot.alerts} == {
            "signal_backlog",
            "dead_letters",
            "unknown_operations",
            "publication_backlog",
            "waiting_age_seconds",
        }


def test_operations_api_rejects_cross_tenant_query() -> None:
    """验证当前租户管理员不能借聚合接口读取其他租户状态。"""

    with _session() as db:
        owner = _seed_tenants(db)

        with pytest.raises(HTTPException) as denied:
            get_dynamic_task_operational_snapshot(
                tenant_id="tenant_b",
                current_user=owner,
                db=db,
            )

        assert denied.value.status_code == 403


def _session() -> Session:
    """创建加载全部统一 Runtime 表的独占内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_tenants(db: Session) -> User:
    """创建两个租户及 tenant_a 全域管理员并返回管理员。"""

    db.add(Tenant(id="tenant_a", name="企业甲"))
    db.add(Tenant(id="tenant_b", name="企业乙"))
    owner = User(
        id="owner_a",
        tenant_id="tenant_a",
        username="owner",
        password_hash="test",
        role="admin",
    )
    db.add(owner)
    db.commit()
    return owner


def _dynamic_instance(instance_id: str, tenant_id: str, *, status: str) -> SopInstance:
    """创建满足动态身份约束的最小 Execution，供跨表聚合测试使用。"""

    active = status in {"created", "running", "waiting"}
    return SopInstance(
        id=instance_id,
        tenant_id=tenant_id,
        session_id=f"session_{instance_id}",
        kind="dynamic_task",
        active_slot_key=f"foreground:session_{instance_id}" if active else None,
        initiator_user_id=f"user_{tenant_id}",
        agent_id=f"agent_{tenant_id}",
        goal_snapshot_json={"goal": "验证运行聚合", "success_criteria": []},
        current_plan_revision_id=f"plan_{instance_id}",
        current_plan_checksum=f"plan-checksum-{instance_id}",
        capability_snapshot_json={"model": {"id": "model"}},
        capability_checksum=f"capability-checksum-{instance_id}",
        current_node_id="step_a",
        status=status,
    )


def _sop_instance(instance_id: str, tenant_id: str) -> SopInstance:
    """创建同租户正式 SOP，证明运维快照不会混入第二种执行语义。"""

    return SopInstance(
        id=instance_id,
        tenant_id=tenant_id,
        session_id=f"session_{instance_id}",
        skill_id="skill_a",
        skill_version_id="skill_version_a",
        skill_version="1.0.0",
        definition_checksum="definition-checksum",
        kind="sop",
        active_slot_key=f"foreground:session_{instance_id}",
        current_node_id="start",
        status="running",
    )
