"""
@Time       : 2026/09/28 18:30
@Author     : zhanglp8181
@File       : external_identity.py
@CallChain  : Operation remote key → callback credential → provider request/inbox
@Description: 生成并校验外部异步任务回调凭据，不在账本中保存明文 token。
"""

from __future__ import annotations

import hashlib
import hmac

from app.config import get_settings


_EXTERNAL_TASK_CALLBACK_SCOPE = b"gongge-xuban-external-task-callback-v1"


def external_task_callback_token(remote_idempotency_key: str) -> str:
    """按持久远端幂等键派生一次外部任务回调凭据。"""

    key = str(remote_idempotency_key or "").strip()
    if not key:
        raise ValueError("外部任务回调凭据必须绑定远端幂等键。")
    secret = get_settings().app_secret.encode("utf-8")
    material = _EXTERNAL_TASK_CALLBACK_SCOPE + b"\0" + key.encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def external_task_callback_token_hash(token: str) -> str:
    """只保存回调凭据摘要，避免数据库泄露可直接使用的 token。"""

    value = str(token or "").strip()
    if not value:
        raise ValueError("外部任务回调 token 不能为空。")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def matches_external_task_callback_token(token: str, expected_hash: str) -> bool:
    """以常量时间比较校验回调 token，拒绝空值和空摘要。"""

    if not str(token or "").strip() or not str(expected_hash or "").strip():
        return False
    candidate = external_task_callback_token_hash(token)
    return hmac.compare_digest(candidate, str(expected_hash))
