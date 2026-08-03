"""
@Time       : 2026/07/22 09:27
@Author     : zhanglp8181
@File       : main.py
@CallChain  : ASGI Server → FastAPI lifespan/routers → application services
@Description: 创建后端 FastAPI 应用并注册生命周期、中间件和业务路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api import (
    agents,
    auth,
    chat,
    feedback,
    expert_taxonomy,
    general_skills,
    knowledge,
    knowledge_bases,
    management_audit,
    memories,
    mock,
    model_configs,
    organization,
    organization_assignments,
    organization_leaders,
    organization_units,
    persona,
    reference_data,
    scheduled_tasks,
    sessions,
    skills,
    sop_migrations,
    tools,
    traces,
    ui_config,
    work_items,
)
from app.async_jobs import shutdown_async_jobs
from app.brand import health_payload
from app.config import get_settings
from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.public_mock import router as public_mock_router
from app.scheduled_tasks.worker import start_background_worker, stop_background_worker

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
    start_background_worker()
    try:
        yield
    finally:
        stop_background_worker()
        shutdown_async_jobs()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    """返回不包含敏感配置的应用健康状态。"""

    return health_payload()


app.include_router(chat.router)
app.include_router(agents.chat_router)
app.include_router(ui_config.chat_router)
app.include_router(auth.router)
app.include_router(organization.router)
app.include_router(organization_assignments.router)
app.include_router(organization_leaders.router)
app.include_router(organization_units.router)
app.include_router(reference_data.router)
app.include_router(work_items.router)
app.include_router(agents.scope_router)
app.include_router(agents.enterprise_router)
app.include_router(expert_taxonomy.router)
app.include_router(general_skills.router)
app.include_router(knowledge_bases.router)
app.include_router(knowledge.router)
app.include_router(management_audit.router)
app.include_router(skills.router)
app.include_router(sop_migrations.router)
app.include_router(model_configs.router)
app.include_router(memories.router)
app.include_router(feedback.router)
app.include_router(persona.router)
app.include_router(scheduled_tasks.enterprise_router)
app.include_router(scheduled_tasks.chat_router)
app.include_router(scheduled_tasks.chat_draft_router)
app.include_router(ui_config.enterprise_router)
app.include_router(tools.router)
app.include_router(tools.mcp_router)
app.include_router(public_mock_router)
app.include_router(sessions.router)
app.include_router(traces.router)
app.include_router(mock.router)
