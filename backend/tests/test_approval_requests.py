"""
@Time       : 2026/07/27 19:45
@Author     : zhanglp8181
@File       : test_approval_requests.py
@CallChain  : pytest → ApprovalRequestService → SQLModel 审批申请与工作项事实
@Description: 验证用章申请创建、分级、本人查询及权威工作项决定回写边界。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.approvals import ApprovalRequestError, ApprovalRequestService
from app.db.models import (
    ApprovalRequest,
    ApprovalRequestDecision,
    EmployeeProfile,
    SkillVersion,
    SopInstance,
    SopNodeExecution,
    SopOperation,
    SopWorkItem,
    SopWorkItemDecision,
    Tenant,
    User,
)


def _session() -> Session:
    """创建共享单连接的内存 SQLite 会话。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_people(db: Session) -> None:
    """创建申请人、审批人及其员工档案。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    for user_id, employee_id in (("applicant", "E002"), ("approver", "E003")):
        db.add(
            User(
                id=user_id,
                tenant_id="tenant_demo",
                username=user_id,
                role="member",
                password_hash="test",
            )
        )
        db.add(
            EmployeeProfile(
                id=f"profile_{user_id}",
                tenant_id="tenant_demo",
                user_id=user_id,
                employee_id=employee_id,
                employee_name=user_id,
            )
        )
    db.commit()


def _create(db: Session, *, document_type: str = "ordinary_document") -> dict[str, object]:
    """以申请人身份创建普通或重要用章申请。"""

    return ApprovalRequestService(db).create_seal_application(
        tenant_id="tenant_demo",
        actor_user_id="applicant",
        payload={
            "employee_id": "E002",
            "employee_name": "applicant",
            "seal_type": "company",
            "seal_purpose": "客户资质证明",
            "document_name": "合作资质证明",
            "document_type": document_type,
        },
    )


def test_create_seal_application_uses_actor_identity_and_server_classification() -> None:
    """验证申请人不可覆盖身份，合同文件由服务端确定为重要审批。"""

    with _session() as db:
        _seed_people(db)
        result = _create(db, document_type="contract")

        assert result["status"] == "pending"
        assert result["approval_level"] == "important"
        assert str(result["approval_request_id"]).startswith("SEAL-")
        row = db.exec(select(ApprovalRequest)).one()
        assert row.initiator_user_id == "applicant"
        assert row.policy_key == "SEAL-IMPORTANT-V1"

        with pytest.raises(ApprovalRequestError, match="当前登录员工一致"):
            ApprovalRequestService(db).create_seal_application(
                tenant_id="tenant_demo",
                actor_user_id="applicant",
                payload={
                    "employee_id": "E999",
                    "seal_type": "company",
                    "seal_purpose": "越权申请",
                    "document_name": "越权文件",
                    "document_type": "ordinary_document",
                },
            )


def test_decision_requires_matching_completed_work_item_and_persists_audit() -> None:
    """验证没有权威工作项不能回写，批准后主状态与追加决定同时成立。"""

    with _session() as db:
        _seed_people(db)
        created = _create(db)
        request_number = str(created["approval_request_id"])
        version = SkillVersion(
            id="skillver_seal",
            tenant_id="tenant_demo",
            skill_id="seal_application_approval",
            version="2.0.0",
            name="用章申请审批",
            content_json={},
            status="published",
        )
        instance = SopInstance(
            id="sopinst_seal",
            tenant_id="tenant_demo",
            session_id="session_seal",
            skill_id="seal_application_approval",
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum="checksum",
            status="waiting",
            active_slot_key="foreground:session_seal",
            current_node_id="normal_seal_approval",
        )
        execution = SopNodeExecution(
            id="sopnode_seal",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_id="normal_seal_approval",
            status="waiting",
        )
        operation = SopOperation(
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_execution_id=execution.id,
            operation_name="admin.seal_application_create",
            idempotency_key="seal-create",
            status="succeeded",
            result_json={"approval_request_id": request_number},
        )
        db.add(version)
        db.add(instance)
        db.add(execution)
        db.add(operation)
        db.commit()

        with pytest.raises(ApprovalRequestError, match="权威审批决定"):
            ApprovalRequestService(db).decide_seal_application(
                tenant_id="tenant_demo",
                approval_request_id=request_number,
                expected_outcome="approved",
            )

        work_item = SopWorkItem(
            id="sopwork_seal",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_execution_id=execution.id,
            skill_version_id=version.id,
            node_id="normal_seal_approval",
            status="completed",
            initiator_user_id="applicant",
            assignee_user_id="approver",
            outcome="approved",
            comment="同意用于客户资质证明。",
        )
        decision = SopWorkItemDecision(
            tenant_id="tenant_demo",
            work_item_id=work_item.id,
            actor_user_id="approver",
            outcome="approved",
            comment=work_item.comment,
            idempotency_key="seal-decision",
        )
        db.add(work_item)
        db.add(decision)
        db.commit()

        result = ApprovalRequestService(db).decide_seal_application(
            tenant_id="tenant_demo",
            approval_request_id=request_number,
            expected_outcome="approved",
        )

        assert result["status"] == "approved"
        assert result["revision"] == 1
        audit = db.exec(select(ApprovalRequestDecision)).one()
        assert audit.actor_user_id == "approver"
        assert audit.work_item_id == work_item.id
        assert audit.comment == "同意用于客户资质证明。"


def test_query_allows_only_original_applicant() -> None:
    """验证申请单号不是访问凭证，其他员工不能查询用章申请。"""

    with _session() as db:
        _seed_people(db)
        created = _create(db)
        payload = {"approval_request_id": created["approval_request_id"]}

        own = ApprovalRequestService(db).query_seal_application(
            tenant_id="tenant_demo",
            actor_user_id="applicant",
            payload=payload,
        )
        assert own["status"] == "pending"

        with pytest.raises(ApprovalRequestError, match="本人发起"):
            ApprovalRequestService(db).query_seal_application(
                tenant_id="tenant_demo",
                actor_user_id="approver",
                payload=payload,
            )


def test_expired_work_item_expires_linked_pending_request() -> None:
    """验证人工任务超时会联动业务申请为过期，而不是永久残留为待审批。"""

    with _session() as db:
        _seed_people(db)
        created = _create(db)
        request_number = str(created["approval_request_id"])
        version = SkillVersion(
            id="skillver_expired",
            tenant_id="tenant_demo",
            skill_id="seal_application_approval",
            version="2.0.0",
            name="用章申请审批",
            content_json={},
            status="published",
        )
        instance = SopInstance(
            id="sopinst_expired",
            tenant_id="tenant_demo",
            session_id="session_expired",
            skill_id="seal_application_approval",
            skill_version_id=version.id,
            skill_version=version.version,
            definition_checksum="checksum",
            status="waiting",
            active_slot_key="foreground:session_expired",
            current_node_id="normal_seal_approval",
        )
        execution = SopNodeExecution(
            id="sopnode_expired",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_id="normal_seal_approval",
            status="waiting",
        )
        work_item = SopWorkItem(
            id="sopwork_expired",
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_execution_id=execution.id,
            skill_version_id=version.id,
            node_id="normal_seal_approval",
            status="expired",
            initiator_user_id="applicant",
        )
        operation = SopOperation(
            tenant_id="tenant_demo",
            instance_id=instance.id,
            node_execution_id=execution.id,
            operation_name="admin.seal_application_create",
            idempotency_key="seal-create-expired",
            status="succeeded",
            result_json={"approval_request_id": request_number},
        )
        db.add(version)
        db.add(instance)
        db.add(execution)
        db.add(work_item)
        db.add(operation)
        db.commit()

        expired_ids = ApprovalRequestService(db).expire_pending_for_work_item(work_item)
        result = ApprovalRequestService(db).query_seal_application(
            tenant_id="tenant_demo",
            actor_user_id="applicant",
            payload={"approval_request_id": request_number},
        )

        assert expired_ids == [request_number]
        assert result["status"] == "expired"
        assert result["message"] == "用章申请已过期。"
        assert result["revision"] == 1
