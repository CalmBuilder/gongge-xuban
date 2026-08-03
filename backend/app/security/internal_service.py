from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, HTTPException

from app.config import get_settings


INTERNAL_SERVICE_HEADER = "X-Gongge-Xuban-Internal-Token"
_INTERNAL_SERVICE_SCOPE = b"gongge-xuban-internal-mock-api-v1"


def internal_service_token() -> str:
    secret = get_settings().app_secret.encode("utf-8")
    return hmac.new(secret, _INTERNAL_SERVICE_SCOPE, hashlib.sha256).hexdigest()


def require_internal_service(
    token: str | None = Header(default=None, alias=INTERNAL_SERVICE_HEADER),
) -> None:
    if token is None or not hmac.compare_digest(token, internal_service_token()):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
