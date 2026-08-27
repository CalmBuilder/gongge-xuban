"""
@Time       : 2026/08/20 16:45
@Author     : zhanglp8181
@File       : provider_input_reconciliation_worker.py
@CallChain  : 部署层provider adapter → reconciliation worker → 对账/删除作业
@Description: 提供显式注入供应商适配器的恢复入口，不在无能力时伪造第三方删除。
"""

from __future__ import annotations

import threading
from typing import Any

from sqlmodel import Session

from app.db import engine
from app.db.models import ModelConfig
from app.session.provider_file_adapters import (
    ProviderFileApiAdapter,
    ProviderFileProfile,
    provider_file_profile_payload,
)
from app.session.provider_input_reconciliation import (
    ProviderExposureAdapter,
    ProviderInputReconciliationService,
)


def build_provider_exposure_adapter(model_config: ModelConfig) -> ProviderFileApiAdapter:
    """从租户模型配置构造真实 Files 适配器，保持 worker 的显式注入边界。"""

    return ProviderFileApiAdapter(model_config)


def run_reconciliation_once(
    adapter: ProviderExposureAdapter,
    *,
    worker_id: str,
    tenant_id: str | None = None,
) -> int:
    """用部署层显式提供的adapter处理一个作业，返回是否实际领取。"""

    with Session(engine) as db:
        job = ProviderInputReconciliationService(db).run_once(
            adapter,
            worker_id=worker_id,
            tenant_id=tenant_id,
        )
    return int(job is not None)


def run_worker(
    adapter: ProviderExposureAdapter,
    *,
    stop_event: threading.Event,
    worker_id: str,
    poll_seconds: float = 2.0,
) -> None:
    """持续处理对账作业；adapter异常由service收敛为retry/dead-letter。"""

    while not stop_event.is_set():
        run_reconciliation_once(adapter, worker_id=worker_id)
        stop_event.wait(max(0.5, poll_seconds))


def adapter_health_payload(adapter: ProviderExposureAdapter) -> dict[str, Any]:
    """返回不包含凭据的适配器能力标记，供维护面显示是否可自动对账。"""

    payload: dict[str, Any] = {
        "provider_exposure_reconciliation": "configured",
        "adapter_type": type(adapter).__name__,
    }
    profile = getattr(adapter, "profile", None)
    if isinstance(profile, ProviderFileProfile):
        payload["provider_file_api"] = provider_file_profile_payload(profile)
    return payload
