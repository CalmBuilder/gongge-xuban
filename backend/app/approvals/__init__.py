"""提供跨 SOP 复用的审批申请台账与权威工作项结果回写服务。"""

from app.approvals.service import ApprovalRequestError, ApprovalRequestService

__all__ = ["ApprovalRequestError", "ApprovalRequestService"]
