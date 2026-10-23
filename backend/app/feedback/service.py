"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : service.py
@CallChain  : 本人反馈/后台分析 → 精确原轮payload → 每次SDK身份守卫 → 修订CAS结算
@Description: 内部修复及供应商重试也拒绝失效分析，保留本人归因与最终CAS。
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import ChatSession, Message, MessageFeedback, ModelConfig, User, utc_now
from app.feedback.source import feedback_source
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation


FEEDBACK_BUCKET_LABELS: dict[str, str] = {
    "model_issue": "模型问题",
    "skill_issue": "技能问题",
    "tool_or_system_issue": "工具/系统问题",
    "user_random_or_unclear": "用户随意或上下文不足",
    "positive_or_resolved": "正向反馈",
    "needs_model_analysis": "待模型分析",
    "unknown": "未知",
}

ALLOWED_BUCKETS = set(FEEDBACK_BUCKET_LABELS)
FEEDBACK_ANALYSIS_MAX_ATTEMPTS = 3
FEEDBACK_ANALYSIS_RETRY_DELAY_SECONDS = 0.6

FEEDBACK_ANALYSIS_PROMPT = """
你是客服 Agent 质量分析器。请根据用户反馈、消息上下文和执行轨迹，判断反馈原因。

你只输出 JSON，字段：
{
  "bucket": "model_issue | skill_issue | tool_or_system_issue | user_random_or_unclear | positive_or_resolved | unknown",
  "confidence": 0.0,
  "reason": "一句话原因，不超过 80 字",
  "summary": "给运营看的简短总结，不超过 120 字",
  "evidence": ["最多 3 条依据"],
  "suggested_action": "建议动作，不超过 80 字"
}

分类标准：
- model_issue：模型理解、推理、回复组织、语气或事实引用有问题。
- skill_issue：技能定义、步骤、槽位、确认规则或工具编排设计导致问题。
- tool_or_system_issue：工具未配置、调用失败、系统异常、返回值不足或错误。
- user_random_or_unclear：用户点踩缺少可解释问题，或上下文不足以判断。
- positive_or_resolved：点赞或正向确认。
- unknown：仍无法判断。

不要把执行轨迹逐字复述为原因；要判断根因。
""".strip()


class _FeedbackAnalysisInvalidated(LLMError):
    """原分析身份已失效；无供应商status/code，内部不重试，调用方安全结束。"""


class FeedbackAnalysisService:
    def __init__(self, db: Session):
        """绑定反馈分析短会话，不持锁跨越模型调用。"""
        self.db = db

    def analyze_feedback(self, feedback_id: str) -> MessageFeedback | None:
        """每次实际SDK前fresh核验原身份，事务外调用并按原revision/generation结算。"""
        feedback = self.db.get(MessageFeedback, feedback_id, populate_existing=True)
        if not feedback:
            return None
        identity = (feedback.id, feedback.tenant_id, feedback.revision,
                    feedback.analysis_generation, feedback.rating)
        model_config = self._default_model_config(feedback.tenant_id)
        if not model_config:
            analysis = {"bucket": "needs_model_analysis", "confidence": 0.0,
                        "reason": "没有可用默认模型，无法完成自动归因。",
                        "summary": "反馈已记录，等待配置模型后重新分析。", "evidence": [],
                        "suggested_action": "配置默认模型后重新触发分析。"}
            return self._settle_analysis(identity, analysis, "needs_model")
        payload = self._analysis_payload(feedback)
        client = LLMClient(model_config)
        client.dispatch_observer = lambda: self._assert_analysis_current(identity)
        self.db.commit()
        for attempt in range(1, FEEDBACK_ANALYSIS_MAX_ATTEMPTS + 1):
            if not self._analysis_still_current(identity):
                return None
            try:
                with llm_operation("feedback.analyze", attempt=attempt):
                    raw = client.generate_json(FEEDBACK_ANALYSIS_PROMPT, payload)
                return self._settle_analysis(identity, _normalize_analysis(raw, identity[4]),
                                             "analyzed")
            except _FeedbackAnalysisInvalidated:
                return None
            except LLMError:
                if attempt < FEEDBACK_ANALYSIS_MAX_ATTEMPTS:
                    time.sleep(FEEDBACK_ANALYSIS_RETRY_DELAY_SECONDS * attempt)
        return self._settle_analysis(identity, {
            "bucket": "unknown", "confidence": None, "reason": "模型分析暂时失败。",
            "summary": "反馈已记录，后台分析暂时失败，可重新分析。", "evidence": [],
            "suggested_action": "稍后重新分析。", "error_type": "llm_error",
            "retryable": True, "attempts": FEEDBACK_ANALYSIS_MAX_ATTEMPTS,
        }, "failed")

    def _assert_analysis_current(self, identity: tuple) -> None:
        """复用SDK派发观察器；失效异常穿过内部修复/重试，交由原分析入口安全结束。"""
        if not self._analysis_still_current(identity):
            raise _FeedbackAnalysisInvalidated("FEEDBACK_ANALYSIS_INVALIDATED")

    def _analysis_still_current(self, identity: tuple) -> bool:
        """独立短Session核对原评分/分析身份及本人会话，不把分享撤回当本人失权。"""
        row_id, tenant_id, revision, generation, rating = identity
        with Session(self.db.get_bind()) as current:
            row = current.get(MessageFeedback, row_id)
            if (row is None or row.tenant_id != tenant_id or row.revision != revision
                    or row.analysis_generation != generation or row.rating != rating):
                return False
            user = current.get(User, row.user_id)
            chat = current.get(ChatSession, row.session_id)
            return bool(user and user.tenant_id == tenant_id and user.membership_status == "active"
                        and chat and chat.tenant_id == tenant_id and chat.user_id == user.id)

    def _settle_analysis(self, identity: tuple, analysis: dict[str, Any],
                         status: str) -> MessageFeedback | None:
        """CAS只更新归因字段；评分修改、重新分析、删除使所有旧模型结果失效。"""
        row_id, tenant_id, revision, generation, rating = identity
        now = utc_now()
        confidence = analysis.get("confidence")
        changed = self.db.exec(update(MessageFeedback).where(
            MessageFeedback.id == row_id, MessageFeedback.tenant_id == tenant_id,
            MessageFeedback.revision == revision,
            MessageFeedback.analysis_generation == generation,
            MessageFeedback.rating == rating,
        ).values(analysis_status=status, analysis_bucket=str(analysis.get("bucket") or "unknown"),
                 analysis_reason=str(analysis.get("reason") or "")[:300],
                 analysis_summary=str(analysis.get("summary") or "")[:500],
                 analysis_confidence=None if confidence is None else _float_in_range(confidence, 0, 1),
                 analysis_json=analysis, analyzed_at=now, updated_at=now)
            .execution_options(synchronize_session=False))
        self.db.commit()
        if changed.rowcount != 1:
            return None
        return self.db.get(MessageFeedback, row_id, populate_existing=True)

    def _default_model_config(self, tenant_id: str) -> ModelConfig | None:
        """读取租户自己的启用默认模型，不回退跨租户配置。"""
        return self.db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.is_default == True,  # noqa: E712
                ModelConfig.enabled == True,  # noqa: E712
            )
        ).first()

    def _analysis_payload(self, feedback: MessageFeedback) -> dict[str, Any]:
        """只读取原回复及显式原轮，不从附近消息/最新事件/当前会话猜执行事实。"""
        message = self.db.get(Message, feedback.message_id)
        source = feedback_source(self.db, message) if message else None
        turn = self.db.get(Message, source.turn_id) if source and source.turn_id else None
        snapshot = (message.metadata_json or {}).get("feedback_source") if message else None
        return {
            "feedback": {"rating": feedback.rating, "message_id": feedback.message_id,
                         "revision": feedback.revision},
            "source": source.model_dump() if source else {"status": "missing"},
            "session": {"session_id": feedback.session_id,
                        "active_skill_id": source.skill_id if source else None,
                        "active_step_id": None, "slots": {}},
            "target_message": {"role": message.role if message else None,
                               "content": message.content if message else ""},
            "nearby_messages": [{"id": turn.id, "role": turn.role, "content": turn.content}]
                               if turn and turn.session_id == feedback.session_id else [],
            "recent_agent_events": [],
            "execution_facts": snapshot if isinstance(snapshot, dict) else {},
        }


def feedback_analysis_read(row: MessageFeedback) -> dict[str, Any]:
    bucket = row.analysis_bucket or "unknown"
    status = _effective_analysis_status(row)
    confidence = None if status == "failed" else row.analysis_confidence
    return {
        "status": status,
        "bucket": bucket,
        "bucket_label": FEEDBACK_BUCKET_LABELS.get(bucket, bucket),
        "reason": row.analysis_reason,
        "summary": row.analysis_summary,
        "confidence": confidence,
        "metadata": row.analysis_json or {},
        "analyzed_at": row.analyzed_at.isoformat() if row.analyzed_at else None,
    }


def _effective_analysis_status(row: MessageFeedback) -> str:
    if row.analysis_status != "analyzed":
        return row.analysis_status
    metadata = row.analysis_json or {}
    if metadata.get("error_type") or metadata.get("retryable"):
        return "failed"
    return row.analysis_status


def feedback_summary(rows: list[MessageFeedback]) -> dict[str, Any]:
    total = len(rows)
    down_rows = [row for row in rows if row.rating == "down"]
    up_rows = [row for row in rows if row.rating == "up"]
    bucket_counts = Counter(row.analysis_bucket or "unknown" for row in down_rows)
    status_counts = Counter(_effective_analysis_status(row) or "pending" for row in rows)
    top_summaries = [
        {
            "message_id": row.message_id,
            "bucket": row.analysis_bucket or "unknown",
            "bucket_label": FEEDBACK_BUCKET_LABELS.get(row.analysis_bucket or "unknown", row.analysis_bucket or "unknown"),
            "summary": row.analysis_summary,
            "reason": row.analysis_reason,
            "confidence": row.analysis_confidence,
        }
        for row in sorted(down_rows, key=lambda item: item.updated_at, reverse=True)
        if row.analysis_summary or row.analysis_reason
    ][:5]
    return {
        "total_feedback": total,
        "down_count": len(down_rows),
        "up_count": len(up_rows),
        "bucket_counts": [
            {
                "bucket": bucket,
                "label": FEEDBACK_BUCKET_LABELS.get(bucket, bucket),
                "count": count,
            }
            for bucket, count in bucket_counts.most_common()
        ],
        "status_counts": dict(status_counts),
        "summary": _compact_overall_summary(bucket_counts, top_summaries),
        "top_summaries": top_summaries,
    }


def _normalize_analysis(raw: dict[str, Any], rating: str) -> dict[str, Any]:
    bucket = str(raw.get("bucket") or "").strip()
    if rating == "up" and bucket in {"", "unknown", "user_random_or_unclear"}:
        bucket = "positive_or_resolved"
    if bucket not in ALLOWED_BUCKETS:
        bucket = "unknown"
    evidence = raw.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "bucket": bucket,
        "confidence": _float_in_range(raw.get("confidence"), 0.0, 1.0),
        "reason": str(raw.get("reason") or FEEDBACK_BUCKET_LABELS.get(bucket, "未知"))[:300],
        "summary": str(raw.get("summary") or raw.get("reason") or "")[:500],
        "evidence": [str(item)[:200] for item in evidence[:3]],
        "suggested_action": str(raw.get("suggested_action") or "")[:300],
    }


def _compact_overall_summary(bucket_counts: Counter[str], top_summaries: list[dict[str, Any]]) -> str:
    if not bucket_counts:
        return "暂无点踩归因数据。"
    leader, count = bucket_counts.most_common(1)[0]
    label = FEEDBACK_BUCKET_LABELS.get(leader, leader)
    detail = next((item.get("summary") or item.get("reason") for item in top_summaries if item.get("bucket") == leader), "")
    if detail:
        return f"当前点踩主要集中在「{label}」（{count} 次）：{detail}"
    return f"当前点踩主要集中在「{label}」（{count} 次）。"


def _float_in_range(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))
