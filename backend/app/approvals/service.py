"""
@Time       : 2026/07/27 19:45
@Author     : zhanglp8181
@File       : service.py
@CallChain  : 内置审批工具 → ApprovalRequestService → 审批申请/工作项/决定表
@Description: 创建、查询通用审批申请，并只接受已完成结构化工作项形成的权威决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.db.models import (
    ApprovalRequest,
    ApprovalRequestDecision,
    EmployeeProfile,
    SopInstance,
    SopOperation,
    SopWorkItem,
    SopWorkItemDecision,
    User,
    utc_now,
)


class ApprovalRequestError(ValueError):
    """表示审批申请命令违反身份、状态、关联或决定事实约束。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与可安全返回的业务说明。"""

        self.code = code
        super().__init__(message)


class SealApplicationCreateCommand(BaseModel):
    """校验用章申请创建工具允许接收的冻结业务快照。"""

    employee_id: str = Field(min_length=1)
    employee_name: str | None = None
    seal_type: str = Field(pattern=r"^(company|contract|finance)$")
    seal_purpose: str = Field(min_length=2)
    document_name: str = Field(min_length=2)
    document_type: str = Field(pattern=r"^(ordinary_document|contract)$")
    contract_amount: float | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid")


class SealApplicationLookupCommand(BaseModel):
    """校验申请人查询用章申请状态时提供的申请单号。"""

    approval_request_id: str = Field(pattern=r"^SEAL-[A-F0-9]{12}$")

    model_config = ConfigDict(extra="forbid")


class ExpenseSpecialApprovalCreateCommand(BaseModel):
    """校验超标报销特批创建命令，并保留计算所需的原标准与申报额。"""

    employee_id: str = Field(min_length=1)
    employee_name: str | None = None
    expense_category: str = Field(min_length=2)
    original_limit: float = Field(gt=0)
    claimed_amount: float = Field(gt=0)
    over_limit_reason: str = Field(min_length=2)

    model_config = ConfigDict(extra="forbid")


class ExpenseSpecialApprovalLookupCommand(BaseModel):
    """校验申请人查询超标报销特批时提供的稳定申请单号。"""

    approval_request_id: str = Field(pattern=r"^SPECIAL-[A-F0-9]{12}$")

    model_config = ConfigDict(extra="forbid")


class ApprovalRequestService:
    """维护业务申请快照，并将工作项决定原子回写为审批业务状态。"""

    def __init__(self, db: Session) -> None:
        """绑定由调用方负责提交或回滚的数据库会话。"""

        self.db = db

    def create_seal_application(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """以可信登录员工创建待审批用章申请，并由服务端决定审批级别。"""

        command = SealApplicationCreateCommand.model_validate(payload)
        user, profile = self._actor_profile(tenant_id, actor_user_id)
        if profile.employee_id != command.employee_id:
            raise ApprovalRequestError(
                "APPROVAL_SUBJECT_MISMATCH",
                "用章申请人必须与当前登录员工一致。",
            )
        approval_level = (
            "important"
            if command.document_type == "contract"
            or command.seal_type in {"contract", "finance"}
            else "normal"
        )
        request = ApprovalRequest(
            tenant_id=tenant_id,
            request_number=f"SEAL-{uuid4().hex[:12].upper()}",
            request_type="seal_application",
            policy_key=f"SEAL-{approval_level.upper()}-V1",
            status="pending",
            initiator_user_id=user.id,
            subject_employee_profile_id=profile.id,
            current_step=1,
            total_steps=1,
            payload_json={
                **command.model_dump(mode="json"),
                "approval_level": approval_level,
            },
        )
        self.db.add(request)
        self.db.flush()
        return self._read(request)

    def decide_seal_application(
        self,
        *,
        tenant_id: str,
        approval_request_id: str,
        expected_outcome: str,
    ) -> dict[str, object]:
        """校验关联工作项的最终决定并幂等回写批准或驳回状态。"""

        if expected_outcome not in {"approved", "rejected"}:
            raise ApprovalRequestError("APPROVAL_OUTCOME_INVALID", "审批结果必须为批准或驳回。")
        request = self._lock_request(tenant_id, approval_request_id)
        operation = self._creation_operation(request)
        instance = self.db.get(SopInstance, operation.instance_id)
        if instance is None or instance.tenant_id != tenant_id:
            raise ApprovalRequestError(
                "APPROVAL_INSTANCE_NOT_FOUND",
                "审批申请关联的流程实例不存在。",
            )
        work_item = self.db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.status == "completed",
            )
            .order_by(SopWorkItem.completed_at.desc())
            .with_for_update()
        ).first()
        if work_item is None or work_item.outcome != expected_outcome:
            raise ApprovalRequestError(
                "APPROVAL_WORK_ITEM_NOT_DECIDED",
                "尚未形成与回写动作一致的权威审批决定。",
            )
        allowed_nodes = {
            "normal": "normal_seal_approval",
            "important": "important_seal_approval",
        }
        approval_level = str((request.payload_json or {}).get("approval_level") or "")
        if work_item.node_id != allowed_nodes.get(approval_level):
            raise ApprovalRequestError(
                "APPROVAL_WORK_ITEM_MISMATCH",
                "审批工作项与申请级别不匹配。",
            )
        existing = self.db.exec(
            select(ApprovalRequestDecision).where(
                ApprovalRequestDecision.tenant_id == tenant_id,
                ApprovalRequestDecision.work_item_id == work_item.id,
            )
        ).first()
        if existing is not None:
            if existing.request_id != request.id or existing.outcome != expected_outcome:
                raise ApprovalRequestError(
                    "APPROVAL_DECISION_CONFLICT",
                    "该工作项已经绑定不同的审批决定。",
                )
            return self._read(request)
        if request.status != "pending":
            raise ApprovalRequestError(
                "APPROVAL_REQUEST_NOT_PENDING",
                "审批申请已经结束，不能再次决定。",
            )
        decision = self.db.exec(
            select(SopWorkItemDecision).where(
                SopWorkItemDecision.tenant_id == tenant_id,
                SopWorkItemDecision.work_item_id == work_item.id,
                SopWorkItemDecision.outcome == expected_outcome,
            )
        ).first()
        if decision is None:
            raise ApprovalRequestError(
                "APPROVAL_DECISION_AUDIT_MISSING",
                "审批工作项缺少对应的决定审计记录。",
            )
        self.db.add(
            ApprovalRequestDecision(
                tenant_id=tenant_id,
                request_id=request.id,
                step_number=1,
                work_item_id=work_item.id,
                actor_user_id=decision.actor_user_id,
                outcome=expected_outcome,
                comment=decision.comment,
            )
        )
        request.instance_id = instance.id
        request.skill_version_id = instance.skill_version_id
        request.status = expected_outcome
        request.revision += 1
        request.decided_at = utc_now()
        request.updated_at = utc_now()
        self.db.add(request)
        self.db.flush()
        return self._read(request)

    def query_seal_application(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """仅允许原申请人按申请单号读取当前用章审批状态。"""

        command = SealApplicationLookupCommand.model_validate(payload)
        user, _ = self._actor_profile(tenant_id, actor_user_id)
        request = self._request_by_number(tenant_id, command.approval_request_id)
        if request.initiator_user_id != user.id:
            raise ApprovalRequestError(
                "APPROVAL_QUERY_FORBIDDEN",
                "只能查询本人发起的用章申请。",
            )
        return self._read(request)

    def create_expense_special_approval(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """以可信员工创建超标特批单，并由服务端计算比例和冻结顺序审批链。"""

        command = ExpenseSpecialApprovalCreateCommand.model_validate(payload)
        user, profile = self._actor_profile(tenant_id, actor_user_id)
        if profile.employee_id != command.employee_id:
            raise ApprovalRequestError(
                "APPROVAL_SUBJECT_MISMATCH",
                "超标报销申请人必须与当前登录员工一致。",
            )
        over_limit_amount = round(command.claimed_amount - command.original_limit, 2)
        if over_limit_amount <= 0:
            raise ApprovalRequestError(
                "EXPENSE_NOT_OVER_LIMIT",
                "申报金额必须高于原报销标准才能发起特批。",
            )
        over_limit_ratio = round(over_limit_amount / command.original_limit, 6)
        approval_route = (
            "department_only" if over_limit_ratio <= 0.2 else "department_finance"
        )
        total_steps = 1 if approval_route == "department_only" else 2
        request = ApprovalRequest(
            tenant_id=tenant_id,
            request_number=f"SPECIAL-{uuid4().hex[:12].upper()}",
            request_type="expense_special_approval",
            policy_key="EXPENSE-OVER-LIMIT-V1",
            status="pending",
            initiator_user_id=user.id,
            subject_employee_profile_id=profile.id,
            current_step=1,
            total_steps=total_steps,
            payload_json={
                **command.model_dump(mode="json"),
                "over_limit_amount": over_limit_amount,
                "over_limit_ratio": over_limit_ratio,
                "approval_route": approval_route,
            },
        )
        self.db.add(request)
        self.db.flush()
        return self._read_expense_special_approval(request)

    def decide_expense_special_approval(
        self,
        *,
        tenant_id: str,
        approval_request_id: str,
        expected_step: int,
        expected_outcome: str,
    ) -> dict[str, object]:
        """仅按当前顺序步骤的已完成工作项追加决定，并推进或结束特批业务单。"""

        if expected_outcome not in {"approved", "rejected"}:
            raise ApprovalRequestError("APPROVAL_OUTCOME_INVALID", "审批结果必须为批准或驳回。")
        request = self._lock_request_by_type(
            tenant_id,
            approval_request_id,
            "expense_special_approval",
            not_found_message="超标报销特批申请不存在。",
        )
        operation = self._creation_operation(
            request,
            operation_name="expense.special_approval_create",
        )
        instance = self.db.get(SopInstance, operation.instance_id)
        if instance is None or instance.tenant_id != tenant_id:
            raise ApprovalRequestError(
                "APPROVAL_INSTANCE_NOT_FOUND",
                "超标特批关联的流程实例不存在。",
            )
        expected_nodes = {
            1: "department_special_approval",
            2: "finance_special_approval",
        }
        expected_node = expected_nodes.get(expected_step)
        if expected_node is None or expected_step > request.total_steps:
            raise ApprovalRequestError(
                "APPROVAL_STEP_INVALID",
                "审批步骤不属于该申请冻结的审批链。",
            )
        work_item = self.db.exec(
            select(SopWorkItem)
            .where(
                SopWorkItem.tenant_id == tenant_id,
                SopWorkItem.instance_id == instance.id,
                SopWorkItem.node_id == expected_node,
                SopWorkItem.status == "completed",
                SopWorkItem.outcome == expected_outcome,
            )
            .order_by(SopWorkItem.completed_at.desc())
            .with_for_update()
        ).first()
        if work_item is None:
            raise ApprovalRequestError(
                "APPROVAL_WORK_ITEM_NOT_DECIDED",
                "尚未形成与当前步骤和回写动作一致的权威审批决定。",
            )
        existing = self.db.exec(
            select(ApprovalRequestDecision).where(
                ApprovalRequestDecision.tenant_id == tenant_id,
                ApprovalRequestDecision.work_item_id == work_item.id,
            )
        ).first()
        if existing is not None:
            if (
                existing.request_id != request.id
                or existing.step_number != expected_step
                or existing.outcome != expected_outcome
            ):
                raise ApprovalRequestError(
                    "APPROVAL_DECISION_CONFLICT",
                    "该工作项已经绑定不同的审批决定。",
                )
            return self._read_expense_special_approval(request)
        if request.status != "pending" or request.current_step != expected_step:
            raise ApprovalRequestError(
                "APPROVAL_STEP_OUT_OF_ORDER",
                "只能办理申请当前等待的审批步骤。",
            )
        decision = self.db.exec(
            select(SopWorkItemDecision).where(
                SopWorkItemDecision.tenant_id == tenant_id,
                SopWorkItemDecision.work_item_id == work_item.id,
                SopWorkItemDecision.outcome == expected_outcome,
            )
        ).first()
        if decision is None:
            raise ApprovalRequestError(
                "APPROVAL_DECISION_AUDIT_MISSING",
                "审批工作项缺少对应的决定审计记录。",
            )
        self.db.add(
            ApprovalRequestDecision(
                tenant_id=tenant_id,
                request_id=request.id,
                step_number=expected_step,
                work_item_id=work_item.id,
                actor_user_id=decision.actor_user_id,
                outcome=expected_outcome,
                comment=decision.comment,
            )
        )
        request.instance_id = instance.id
        request.skill_version_id = instance.skill_version_id
        if expected_outcome == "rejected":
            request.status = "rejected"
            request.decided_at = utc_now()
        elif expected_step == request.total_steps:
            request.status = "approved"
            request.decided_at = utc_now()
        else:
            request.current_step += 1
        request.revision += 1
        request.updated_at = utc_now()
        self.db.add(request)
        self.db.flush()
        return self._read_expense_special_approval(request)

    def query_expense_special_approval(
        self,
        *,
        tenant_id: str,
        actor_user_id: str | None,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """仅允许原申请人查询超标报销特批的当前步骤和最终状态。"""

        command = ExpenseSpecialApprovalLookupCommand.model_validate(payload)
        user, _ = self._actor_profile(tenant_id, actor_user_id)
        request = self._request_by_number_and_type(
            tenant_id,
            command.approval_request_id,
            "expense_special_approval",
            not_found_message="超标报销特批申请不存在。",
        )
        if request.initiator_user_id != user.id:
            raise ApprovalRequestError(
                "APPROVAL_QUERY_FORBIDDEN",
                "只能查询本人发起的超标报销特批。",
            )
        return self._read_expense_special_approval(request)

    def expire_pending_for_work_item(self, work_item: SopWorkItem) -> list[str]:
        """把超时工作项所属实例已创建的待审批业务单统一推进为过期终态。"""

        operations = self.db.exec(
            select(SopOperation).where(
                SopOperation.tenant_id == work_item.tenant_id,
                SopOperation.instance_id == work_item.instance_id,
                SopOperation.status == "succeeded",
            )
        ).all()
        request_numbers = {
            str((operation.result_json or {}).get("approval_request_id") or "")
            for operation in operations
        }
        request_numbers.discard("")
        if not request_numbers:
            return []

        requests = self.db.exec(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == work_item.tenant_id,
                ApprovalRequest.request_number.in_(request_numbers),
                ApprovalRequest.status == "pending",
            )
            .with_for_update()
        ).all()
        expired_ids: list[str] = []
        now = utc_now()
        for request in requests:
            request.instance_id = work_item.instance_id
            request.skill_version_id = work_item.skill_version_id
            request.status = "expired"
            request.revision += 1
            request.decided_at = now
            request.updated_at = now
            self.db.add(request)
            expired_ids.append(request.request_number)
        self.db.flush()
        return expired_ids

    def _actor_profile(
        self,
        tenant_id: str,
        actor_user_id: str | None,
    ) -> tuple[User, EmployeeProfile]:
        """读取当前登录用户和有效员工档案，拒绝缺失或跨租户身份。"""

        user = self.db.get(User, actor_user_id or "")
        if user is None or user.tenant_id != tenant_id:
            raise ApprovalRequestError("APPROVAL_ACTOR_NOT_FOUND", "当前申请人账号不存在。")
        profile = self.db.exec(
            select(EmployeeProfile).where(
                EmployeeProfile.tenant_id == tenant_id,
                EmployeeProfile.user_id == user.id,
                EmployeeProfile.status == "active",
            )
        ).first()
        if profile is None:
            raise ApprovalRequestError(
                "APPROVAL_EMPLOYEE_PROFILE_NOT_FOUND",
                "当前账号没有有效员工档案。",
            )
        return user, profile

    def _lock_request(self, tenant_id: str, request_number: str) -> ApprovalRequest:
        """加行锁读取待回写申请，兼容 SQLite 忽略锁语义。"""

        return self._lock_request_by_type(
            tenant_id,
            request_number,
            "seal_application",
            not_found_message="用章申请不存在。",
        )

    def _lock_request_by_type(
        self,
        tenant_id: str,
        request_number: str,
        request_type: str,
        *,
        not_found_message: str,
    ) -> ApprovalRequest:
        """按租户、业务类型和单号加锁读取审批申请。"""

        request = self.db.exec(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.request_number == request_number,
                ApprovalRequest.request_type == request_type,
            )
            .with_for_update()
        ).first()
        if request is None:
            raise ApprovalRequestError("APPROVAL_REQUEST_NOT_FOUND", not_found_message)
        return request

    def _request_by_number(self, tenant_id: str, request_number: str) -> ApprovalRequest:
        """按租户和业务单号读取用章申请。"""

        return self._request_by_number_and_type(
            tenant_id,
            request_number,
            "seal_application",
            not_found_message="用章申请不存在。",
        )

    def _request_by_number_and_type(
        self,
        tenant_id: str,
        request_number: str,
        request_type: str,
        *,
        not_found_message: str,
    ) -> ApprovalRequest:
        """按租户、业务类型和单号读取审批申请。"""

        request = self.db.exec(
            select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.request_number == request_number,
                ApprovalRequest.request_type == request_type,
            )
        ).first()
        if request is None:
            raise ApprovalRequestError("APPROVAL_REQUEST_NOT_FOUND", not_found_message)
        return request

    def _creation_operation(
        self,
        request: ApprovalRequest,
        *,
        operation_name: str = "admin.seal_application_create",
    ) -> SopOperation:
        """从持久工具回执反查创建本申请的唯一 SOP 操作。"""

        operations = self.db.exec(
            select(SopOperation)
            .where(
                SopOperation.tenant_id == request.tenant_id,
                SopOperation.operation_name == operation_name,
                SopOperation.status == "succeeded",
            )
            .order_by(SopOperation.completed_at.desc())
        ).all()
        matched = [
            operation
            for operation in operations
            if str((operation.result_json or {}).get("approval_request_id") or "")
            == request.request_number
        ]
        if len(matched) != 1:
            raise ApprovalRequestError(
                "APPROVAL_CREATION_OPERATION_INVALID",
                "用章申请缺少唯一的创建操作审计记录。",
            )
        return matched[0]

    @staticmethod
    def _read(request: ApprovalRequest) -> dict[str, object]:
        """返回可供 Runtime 条件和申请人查询消费的稳定审批摘要。"""

        payload = request.payload_json or {}
        status_messages = {
            "pending": "用章申请等待审批。",
            "approved": "用章申请已批准。",
            "rejected": "用章申请已驳回。",
            "expired": "用章申请已过期。",
            "cancelled": "用章申请已取消。",
        }
        return {
            "approval_request_id": request.request_number,
            "request_type": request.request_type,
            "status": request.status,
            "policy_key": request.policy_key,
            "approval_level": str(payload.get("approval_level") or ""),
            "current_step": request.current_step,
            "total_steps": request.total_steps,
            "document_name": str(payload.get("document_name") or ""),
            "revision": request.revision,
            "message": status_messages.get(request.status, "用章申请状态已更新。"),
        }

    @staticmethod
    def _read_expense_special_approval(request: ApprovalRequest) -> dict[str, object]:
        """返回供顺序审批路由、最终回复和本人查询使用的稳定特批摘要。"""

        payload = request.payload_json or {}
        status_messages = {
            "pending": "超标报销特批等待审批。",
            "approved": "超标报销特批已批准。",
            "rejected": "超标报销特批已驳回。",
            "expired": "超标报销特批已过期。",
            "cancelled": "超标报销特批已取消。",
        }
        return {
            "approval_request_id": request.request_number,
            "request_type": request.request_type,
            "status": request.status,
            "policy_key": request.policy_key,
            "approval_route": str(payload.get("approval_route") or ""),
            "original_limit": float(payload.get("original_limit") or 0),
            "claimed_amount": float(payload.get("claimed_amount") or 0),
            "over_limit_amount": float(payload.get("over_limit_amount") or 0),
            "over_limit_ratio": float(payload.get("over_limit_ratio") or 0),
            "current_step": request.current_step,
            "total_steps": request.total_steps,
            "revision": request.revision,
            "message": status_messages.get(request.status, "超标报销特批状态已更新。"),
        }
