"""
@Time       : 2026/08/01 20:42
@Author     : zhanglp8181
@File       : test_enterprise_auth_guards.py
@CallChain  : pytest → 企业只读路由 → 认证依赖
@Description: 验证企业端只读、分页和概览接口在未认证时统一拒绝访问。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agents import enterprise_router as agents_router
from app.api.agents import scope_router as agent_scope_router
from app.api.feedback import router as feedback_router
from app.api.expert_taxonomy import router as expert_taxonomy_router
from app.api.general_skills import router as general_skills_router
from app.api.knowledge import router as knowledge_router
from app.api.knowledge_bases import router as knowledge_bases_router
from app.api.memories import router as memories_router
from app.api.model_configs import router as model_configs_router
from app.api.organization import router as organization_router
from app.api.persona import router as persona_router
from app.api.scheduled_tasks import enterprise_router as scheduled_tasks_router
from app.api.sessions import router as sessions_router
from app.api.skills import router as skills_router
from app.api.tools import mcp_router, router as tools_router
from app.api.traces import router as traces_router
from app.api.ui_config import enterprise_router as ui_config_router


def test_enterprise_read_endpoints_require_authentication() -> None:
    """逐一验证企业只读入口不会绕过统一登录认证。"""

    app = FastAPI()
    app.include_router(memories_router)
    app.include_router(tools_router)
    app.include_router(mcp_router)
    app.include_router(general_skills_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_bases_router)
    app.include_router(model_configs_router)
    app.include_router(organization_router)
    app.include_router(persona_router)
    app.include_router(skills_router)
    app.include_router(traces_router)
    app.include_router(ui_config_router)
    app.include_router(agents_router)
    app.include_router(agent_scope_router)
    app.include_router(feedback_router)
    app.include_router(expert_taxonomy_router)
    app.include_router(scheduled_tasks_router)
    app.include_router(sessions_router)
    client = TestClient(app)

    paths = [
        "/api/enterprise/memories?tenant_id=tenant_demo",
        "/api/enterprise/memories/page?tenant_id=tenant_demo",
        "/api/enterprise/tools?tenant_id=tenant_demo",
        "/api/enterprise/tools/buckets?tenant_id=tenant_demo",
        "/api/enterprise/tools/tool_demo?tenant_id=tenant_demo",
        "/api/enterprise/mcp-servers?tenant_id=tenant_demo",
        "/api/enterprise/mcp-servers/server_demo?tenant_id=tenant_demo",
        "/api/enterprise/general-skills?tenant_id=tenant_demo",
        "/api/enterprise/knowledge/jobs?tenant_id=tenant_demo",
        "/api/enterprise/knowledge-bases?tenant_id=tenant_demo",
        "/api/enterprise/model-configs?tenant_id=tenant_demo",
        "/api/enterprise/persona?tenant_id=tenant_demo",
        "/api/enterprise/skills?tenant_id=tenant_demo",
        "/api/enterprise/traces?tenant_id=tenant_demo",
        "/api/enterprise/ui-config?tenant_id=tenant_demo",
        "/api/enterprise/agents?tenant_id=tenant_demo",
        "/api/enterprise/agents/gallery-page?tenant_id=tenant_demo&scope=owned",
        "/api/enterprise/agents/management-page?tenant_id=tenant_demo&view=all",
        "/api/organization/business-roles/page?tenant_id=tenant_demo",
        "/api/organization/business-role-options?tenant_id=tenant_demo",
        "/api/enterprise/expert-taxonomy?tenant_id=tenant_demo",
        "/api/enterprise/agent-scope?tenant_id=tenant_demo",
        "/api/enterprise/feedback/summary?tenant_id=tenant_demo",
        "/api/enterprise/scheduled-tasks?tenant_id=tenant_demo",
        "/api/enterprise/scheduled-tasks/page?tenant_id=tenant_demo",
        "/api/enterprise/scheduled-tasks/overview?tenant_id=tenant_demo",
        "/api/enterprise/scheduled-tasks/runs/page?tenant_id=tenant_demo",
        "/api/enterprise/sessions?tenant_id=tenant_demo",
        "/api/enterprise/sessions/page?tenant_id=tenant_demo",
        "/api/enterprise/sessions/overview?tenant_id=tenant_demo",
    ]

    for path in paths:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
