"""
@Time       : 2026/09/02 11:00
@Author     : zhanglp8181
@File       : test_vertical_abilities.py
@CallChain  : pytest → VerticalAbilityCatalog/Resolver/Adapter → DynamicTask 快照契约
@Description: 验证平台内置垂直能力目录、版本快照、再授权和公开信息适配器边界。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.cancellation import TurnCancellationRequested
from app.db.models import SopInstance, SopOperation
from app.dynamic_tasks.capability_catalog import (
    CapabilityAccessDenied,
    DynamicCapabilityCatalog,
    capability_checksum,
)
from app.dynamic_tasks.agent import (
    DynamicTaskAgent,
    _ensure_vertical_failed_operation_disclosures,
    _ensure_vertical_required_result_evidence_disclosures,
    _ensure_vertical_result_limit_disclosures,
    _ensure_vertical_result_quality_disclosures,
    _ensure_vertical_source_url_disclosures,
    _successful_vertical_result_limitations,
)
from app.dynamic_tasks.planning import (
    ActionKind,
    CompletedProviderProposal,
    PlanStep,
    RuntimeActionProposal,
)
from app.dynamic_tasks.result_verifier import DynamicTaskResult
from app.sop_runtime.execution_store import (
    SopExecutionSkillAuthorizationError,
    SopExecutionStore,
)
from app.tools.tool_schema import ToolCall
from app.vertical_abilities import VerticalAbilityCatalog, VerticalAbilityResolver
from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    _provider_timeout_seconds,
    execute_vertical_adapter,
)
from app.vertical_abilities.executor import VerticalAbilityToolExecutor


def _settings() -> SimpleNamespace:
    """构造不依赖项目环境文件的适配器测试配置。"""

    return SimpleNamespace(
        vertical_ability_exa_mcp_url="https://mcp.exa.ai/mcp",
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def test_public_provider_timeout_uses_agent_reach_window_not_generic_tool_timeout() -> None:
    """公共 Provider 默认沿用 Agent-Reach 的 30 秒窗口，不被通用工具 8 秒配置截断。"""

    settings = _settings()
    assert _provider_timeout_seconds(settings) == 30.0
    settings.vertical_ability_provider_timeout_seconds = 45
    assert _provider_timeout_seconds(settings) == 45.0
    settings.vertical_ability_provider_timeout_seconds = 999
    assert _provider_timeout_seconds(settings) == 120.0


def test_public_catalog_is_data_driven_and_contains_web_intelligence() -> None:
    """验证已提交能力自动进入公共目录，并披露稳定版本与输出契约。"""

    catalog = VerticalAbilityCatalog().list_public()

    assert [item.id for item in catalog.items] == ["web-intelligence"]
    item = catalog.items[0]
    assert item.name == "全网情报员"
    assert item.health.status == "ready"
    assert item.version == "1.7.4"
    assert item.checksum
    assert item.output_contract["contract_id"] == "research_report.v1"
    assert {capability.name for capability in item.capabilities} == {
        "intelligence.web_search",
        "intelligence.web_read",
        "intelligence.rss_read",
        "intelligence.v2ex_topics",
        "intelligence.v2ex_user",
        "intelligence.github_repo",
        "intelligence.github_search",
        "intelligence.github_issue",
        "intelligence.github_pr",
        "intelligence.github_releases",
        "intelligence.bilibili_video",
        "intelligence.bilibili_search",
        "intelligence.bilibili_subtitle",
        "intelligence.twitter_read",
        "intelligence.twitter_search",
        "intelligence.reddit_search",
        "intelligence.reddit_read",
        "intelligence.facebook_read",
        "intelligence.instagram_read",
        "intelligence.xiaohongshu_search",
        "intelligence.xiaohongshu_read",
        "intelligence.linkedin_read",
        "intelligence.linkedin_search",
        "intelligence.xueqiu_read",
        "intelligence.xueqiu_search",
        "intelligence.youtube_video",
        "intelligence.youtube_search",
        "intelligence.media_transcribe",
        "intelligence.xiaoyuzhou_transcribe",
    }
    assert item.output_contract["required_sections"]
    assert item.platform_tags[-3:] == ["LinkedIn", "雪球", "小宇宙"]


def test_resolver_deduplicates_selected_packages_and_freezes_scope() -> None:
    """验证多选同一能力不会重复合成，并且快照绑定当前租户和数字员工。"""

    resolution = VerticalAbilityResolver().resolve(
        ["web-intelligence", "web-intelligence"],
        tenant_id="tenant_a",
        agent_id="agent_a",
    )

    assert resolution.ability_ids == ("web-intelligence",)
    assert resolution.guidance_use_ids == {"vertical_ability:web-intelligence"}
    assert len(resolution.capabilities) == 29
    assert resolution.snapshot["vertical_output_contracts"][0]["contract"]["contract_id"] == (
        "research_report.v1"
    )
    assert all(item.tenant_id == "tenant_a" for item in resolution.capabilities)
    assert all(item.agent_id == "agent_a" for item in resolution.capabilities)
    assert all(
        item.audit_view["vertical_ability"]["id"] == "web-intelligence"
        for item in resolution.capabilities
    )
    assert resolution.snapshot["composition_checksum"]


def test_resolver_rebuilds_identical_checksum_without_runtime_identity() -> None:
    """验证相同能力版本在不同执行上下文中保持定义 checksum 稳定。"""

    resolver = VerticalAbilityResolver()
    first = resolver.resolve(
        ["web-intelligence"],
        tenant_id="tenant_a",
        agent_id="agent_a",
    )
    second = resolver.resolve(
        ["web-intelligence"],
        tenant_id="tenant_b",
        agent_id="agent_b",
    )

    assert first.snapshot["composition_checksum"] == second.snapshot["composition_checksum"]
    assert [
        item.audit_view["vertical_ability"]["checksum"] for item in first.capabilities
    ] == [
        item.audit_view["vertical_ability"]["checksum"] for item in second.capabilities
    ]


def test_vertical_snapshot_is_reauthorized_against_current_manifest() -> None:
    """验证能力包快照不依赖租户 Tool 行，并在版本或 checksum 漂移时拒绝执行。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        snapshot = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        ).capabilities[0]
        catalog = DynamicCapabilityCatalog(db)

        assert catalog.reauthorize_tool(
            snapshot,
            actor_user_id="user_a",
            organization_unit_id=None,
        ) is None

        changed = snapshot.model_copy(update={"checksum": "f" * 64})
        with pytest.raises(CapabilityAccessDenied, match="VERTICAL_ABILITY_REVISION_CHANGED"):
            catalog.reauthorize_tool(
                changed,
                actor_user_id="user_a",
                organization_unit_id=None,
            )
    engine.dispose()


def test_rss_adapter_normalizes_atom_entries_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 RSS/Atom 适配器只抽取有界来源字段，并兼容命名空间与 href 链接。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._validate_public_url",
        lambda _url, _settings, _resolver: None,
    )
    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: (
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>安全更新</title>"
            '<link href="https://example.com/advisory"/>'
            "<published>2026-09-02T00:00:00Z</published>"
            "<summary>请核验来源。</summary></entry></feed>"
        ).encode(
            "utf-8"
        ),
    )

    result = execute_vertical_adapter(
        "rss.atom",
        {"url": "https://example.com/feed.xml", "limit": 1},
        settings=_settings(),
    )

    assert result["provider"] == "RSS/Atom"
    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert result["entries"] == [
        {
            "title": "安全更新",
            "url": "https://example.com/advisory",
            "published_at": "2026-09-02T00:00:00Z",
            "summary": "请核验来源。",
        }
    ]


def test_exa_adapter_normalizes_text_mcp_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Exa MCP 返回的纯文本 Markdown 能转换为可追溯来源卡片。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters.execute_mcp_tool",
        lambda *_args, **_kwargs: (
            "Title: Agents SDK | OpenAI API\n"
            "URL: https://developers.openai.com/api/docs/guides/agents\n"
            "Published: N/A\n"
            "Author: N/A\n"
            "Highlights:\n"
            "Agents are applications that plan and call tools.\n"
            "\n---\n\n"
            "Title: OpenAI Agents SDK\n"
            "URL: https://openai.github.io/openai-agents-python/\n"
            "Published: 2026-01-01\n"
            "Highlights:\n"
            "A lightweight agent framework."
        ),
    )

    result = execute_vertical_adapter(
        "exa.web_search_exa",
        {"query": "OpenAI agents", "num_results": 2},
        settings=_settings(),
    )

    assert result["provider"] == "Exa via MCP"
    assert result["count"] == 2
    assert result["result_state"] == "success_with_data"
    assert result["results"] == [
        {
            "title": "Agents SDK | OpenAI API",
            "url": "https://developers.openai.com/api/docs/guides/agents",
            "published_at": "",
            "summary": "Agents are applications that plan and call tools.",
        },
        {
            "title": "OpenAI Agents SDK",
            "url": "https://openai.github.io/openai-agents-python/",
            "published_at": "2026-01-01",
            "summary": "A lightweight agent framework.",
        },
    ]


def test_exa_adapter_falls_back_to_non_empty_content_when_structured_results_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 MCP 结构化结果为空时不会丢弃同一响应中的真实文本来源。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters.execute_mcp_tool",
        lambda *_args, **_kwargs: {
            "structuredContent": {"results": []},
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Title: Public Runtime Docs\n"
                        "URL: https://example.com/runtime\n"
                        "Highlights:\n"
                        "A public runtime reference."
                    ),
                }
            ],
        },
    )

    result = execute_vertical_adapter(
        "exa.web_search_exa",
        {"query": "public runtime", "num_results": 1},
        settings=_settings(),
    )

    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert result["results"] == [
        {
            "title": "Public Runtime Docs",
            "url": "https://example.com/runtime",
            "published_at": "",
            "summary": "A public runtime reference.",
        }
    ]


def test_github_public_repo_reads_repository_and_filters_pull_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 GitHub 公共仓库适配器返回仓库/Issue 来源，并过滤 Issue 接口中的 PR。"""

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_get(url: str, **kwargs: object) -> bytes:
        """按 GitHub API 路径返回隔离的仓库、README 和 Issue fixture。"""

        calls.append((url, kwargs))
        if url.endswith("/readme"):
            return json.dumps({
                "type": "file", "encoding": "base64", "size": 13,
                "content": "UHVibGljIFJFQURNRQ==", "path": "README.md", "sha": "a" * 40,
                "html_url": "https://github.com/acme/demo/blob/main/README.md",
            }).encode()
        if "/issues?" in url:
            return json.dumps(
                [
                    {
                        "number": 7,
                        "title": "Improve runtime isolation",
                        "html_url": "https://github.com/acme/demo/issues/7",
                        "state": "open",
                        "user": {"login": "alice"},
                        "comments": 2,
                        "labels": [{"name": "enhancement"}],
                        "created_at": "2026-09-01T00:00:00Z",
                        "updated_at": "2026-09-02T00:00:00Z",
                    },
                    {
                        "number": 8,
                        "title": "A pull request",
                        "pull_request": {"url": "https://api.github.com/repos/acme/demo/pulls/8"},
                    },
                ]
            ).encode()
        return json.dumps(
            {
                "name": "demo",
                "full_name": "acme/demo",
                "html_url": "https://github.com/acme/demo",
                "description": "A public runtime demo",
                "owner": {"login": "acme"},
                "language": "Python",
                "default_branch": "main",
                "stargazers_count": 12,
                "forks_count": 3,
                "open_issues_count": 1,
                "license": {"spdx_id": "MIT"},
                "topics": ["runtime", "agents"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
            }
        ).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", fake_get)

    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "https://github.com/acme/demo", "issue_limit": 3},
        settings=_settings(),
    )

    assert result["provider"] == "GitHub public REST API"
    assert result["repository"]["url"] == "https://github.com/acme/demo"
    assert result["issues_count"] == 1
    assert result["issues_result_state"] == "success_with_data"
    assert result["issues"][0]["url"] == "https://github.com/acme/demo/issues/7"
    assert len(calls) == 3
    assert result["repository"]["content"] == "Public README"
    assert all("api.github.com" in kwargs["allowed_hosts"] for _, kwargs in calls)


def test_github_public_search_normalizes_empty_and_non_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 GitHub 公共仓库搜索使用统一来源卡片和空结果状态。"""

    captured: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> bytes:
        """返回固定的 GitHub 搜索结果，避免测试依赖公网。"""

        captured.append(url)
        return json.dumps(
            {
                "total_count": 1,
                "items": [
                    {
                        "full_name": "acme/runtime",
                        "html_url": "https://github.com/acme/runtime",
                        "description": "A runtime project",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", fake_get)

    result = execute_vertical_adapter(
        "github.public_search",
        {"query": "agent runtime", "num_results": 3},
        settings=_settings(),
    )

    assert result["results"] == [
        {
            "title": "acme/runtime",
            "url": "https://github.com/acme/runtime",
            "published_at": "2026-01-01T00:00:00Z",
            "summary": "A runtime project",
        }
    ]
    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert "q=agent+runtime" in captured[0]


def test_github_public_repo_marks_empty_issue_collection_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证公开仓库没有 Issue 时仍披露 no_results，而不是伪造 Issue。"""

    def fake_get(url: str, **_kwargs: object) -> bytes:
        """返回有仓库但无 Issue 的固定 GitHub 回执。"""

        if "/issues?" in url:
            return b"[]"
        return b'{"full_name":"acme/empty","html_url":"https://github.com/acme/empty"}'

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", fake_get)

    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/empty"},
        settings=_settings(),
    )

    assert result["repository"]["url"] == "https://github.com/acme/empty"
    assert result["issues"] == []
    assert result["issues_count"] == 0
    assert result["issues_result_state"] == "no_results"


def test_github_public_repo_falls_back_to_jina_when_anonymous_api_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证公共 GitHub API 限流时可经 Jina 读取公开仓库和 Issues 页面。"""

    def raise_rate_limit(*_args: object, **_kwargs: object) -> object:
        """模拟 GitHub 匿名 API 返回 403 限流。"""

        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            "互联网数据源返回异常状态码：403。",
            status_code=403,
        )

    def fake_jina(arguments: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        """按仓库或 Issues 页面返回隔离的 Jina Reader 内容。"""

        if "/issues?" in str(arguments["url"]):
            content = (
                "Markdown Content:\n"
                "*   ### [Improve public fallback](https://github.com/acme/demo/issues/9) "
                "Status: Open.    #9 In acme/demo;· [alice](https://github.com/acme/demo) "
                "opened on Sep 4, 2026"
            )
            return {"content": content}
        else:
            payload: object = {
                "name": "demo",
                "full_name": "acme/demo",
                "html_url": "https://github.com/acme/demo",
                "description": "A public runtime demo",
                "owner": {"login": "acme"},
            }
        return {"content": f"Markdown Content:\n{json.dumps(payload)}"}

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", raise_rate_limit)
    monkeypatch.setattr("app.vertical_abilities.adapters._jina_read", fake_jina)

    result = execute_vertical_adapter(
        "github.public_repo",
        {"repository": "acme/demo", "issue_limit": 3},
        settings=_settings(),
        jina_api_key="test-jina-key",
    )

    assert result["provider"] == "GitHub public pages via Jina Reader (rate-limit fallback)"
    assert result["repository"]["url"] == "https://github.com/acme/demo"
    assert result["issues_count"] == 1
    assert result["issues"][0]["url"] == "https://github.com/acme/demo/issues/9"


def test_github_public_search_falls_back_to_jina_when_anonymous_api_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证公共 GitHub 搜索限流时仍按通用来源集合返回真实 API JSON。"""

    def raise_rate_limit(*_args: object, **_kwargs: object) -> object:
        """模拟 GitHub 搜索接口返回 403 限流。"""

        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            "互联网数据源返回异常状态码：403。",
            status_code=403,
        )

    def fake_jina(arguments: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        """返回 GitHub 搜索 API 的 JSON 内容。"""

        assert "/search/repositories?" in str(arguments["url"])
        payload = {
            "items": [
                {
                    "full_name": "acme/runtime",
                    "html_url": "https://github.com/acme/runtime",
                    "description": "A runtime project",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
        return {"content": f"Markdown Content:\n{json.dumps(payload)}"}

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", raise_rate_limit)
    monkeypatch.setattr("app.vertical_abilities.adapters._jina_read", fake_jina)

    result = execute_vertical_adapter(
        "github.public_search",
        {"query": "agent runtime", "num_results": 3},
        settings=_settings(),
        jina_api_key="test-jina-key",
    )

    assert result["provider"] == "GitHub public search via Jina Reader (rate-limit fallback)"
    assert result["results"][0]["url"] == "https://github.com/acme/runtime"


def test_github_public_repo_does_not_downgrade_authenticated_forbidden_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证带 GitHub Token 的 403 不会降级到公开 Jina，避免误读私有资源。"""

    def raise_forbidden(*_args: object, **_kwargs: object) -> object:
        """模拟认证 GitHub 请求被拒绝。"""

        raise VerticalAbilityAdapterError(
            "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR",
            "互联网数据源返回异常状态码：403。",
            status_code=403,
        )

    def unexpected_jina(*_args: object, **_kwargs: object) -> object:
        """如果发生降级则让测试立即失败。"""

        raise AssertionError("authenticated GitHub request must not use public fallback")

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", raise_forbidden)
    monkeypatch.setattr("app.vertical_abilities.adapters._jina_read", unexpected_jina)

    with pytest.raises(VerticalAbilityAdapterError) as error:
        execute_vertical_adapter(
            "github.public_repo",
            {"repository": "acme/private"},
            settings=_settings(),
            jina_api_key="test-jina-key",
            platform_connections={"github": {"access_token": "test-github-token"}},
        )
    assert error.value.status_code == 403


def test_collection_adapters_mark_empty_results_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Exa、RSS 和 V2EX 的空集合都会保留成功回执但明确标记无结果。"""

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._validate_public_url",
        lambda _url, _settings, _resolver: None,
    )
    monkeypatch.setattr(
        "app.vertical_abilities.adapters.execute_mcp_tool",
        lambda *_args, **_kwargs: {"results": []},
    )
    exa_result = execute_vertical_adapter(
        "exa.web_search_exa",
        {"query": "不存在的主题", "num_results": 5},
        settings=_settings(),
    )
    assert exa_result["results"] == []
    assert exa_result["count"] == 0
    assert exa_result["result_state"] == "no_results"

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: b"<rss><channel /></rss>",
    )
    rss_result = execute_vertical_adapter(
        "rss.atom",
        {"url": "https://example.com/empty.xml"},
        settings=_settings(),
    )
    assert rss_result["entries"] == []
    assert rss_result["count"] == 0
    assert rss_result["result_state"] == "no_results"

    monkeypatch.setattr(
        "app.vertical_abilities.adapters._http_get_bytes",
        lambda *_args, **_kwargs: b"[]",
    )
    v2ex_result = execute_vertical_adapter(
        "v2ex.public_api",
        {"mode": "hot"},
        settings=_settings(),
    )
    assert v2ex_result["items"] == []
    assert v2ex_result["count"] == 0
    assert v2ex_result["result_state"] == "no_results"


def test_jina_adapter_uses_optional_server_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Jina 认证只从服务端设置注入，不进入用户参数。"""

    captured: dict[str, object] = {}
    settings = _settings()
    settings.vertical_ability_jina_api_key = "server-only-test-key"
    monkeypatch.setattr(
        "app.vertical_abilities.adapters._validate_public_url",
        lambda _url, _settings, _resolver: None,
    )

    def fake_get(*_args: object, **kwargs: object) -> bytes:
        """捕获 Jina 请求头并返回最小正文。"""

        captured.update(kwargs)
        return b"# Article"

    monkeypatch.setattr("app.vertical_abilities.adapters._http_get_bytes", fake_get)
    result = execute_vertical_adapter(
        "jina.reader",
        {"url": "https://example.com/"},
        settings=settings,
    )

    assert result["content"] == "# Article"
    assert captured["headers"]["Authorization"] == "Bearer server-only-test-key"


def test_adapter_rejects_invalid_url_and_unknown_adapter() -> None:
    """验证适配器入口拒绝未知实现和携带凭据的 URL。"""

    with pytest.raises(VerticalAbilityAdapterError) as unknown_error:
        execute_vertical_adapter("unknown.adapter", {}, settings=_settings())
    assert unknown_error.value.code == "VERTICAL_ABILITY_ADAPTER_NOT_FOUND"

    with pytest.raises(VerticalAbilityAdapterError) as invalid_url_error:
        execute_vertical_adapter(
            "rss.atom",
            {"url": "https://user:password@example.com/feed.xml"},
            settings=_settings(),
        )
    assert invalid_url_error.value.code == "VERTICAL_ABILITY_URL_INVALID"


def test_frozen_execution_instance_shape_can_store_vertical_snapshot() -> None:
    """验证动态 Execution 模型可以保存能力包快照而不新增数据库表或字段。"""

    resolution = VerticalAbilityResolver().resolve(
        ["web-intelligence"],
        tenant_id="tenant_a",
        agent_id="agent_a",
    )
    instance = SopInstance(
        id="sopinst_vertical",
        tenant_id="tenant_a",
        session_id="session_a",
        kind="dynamic_task",
        initiator_user_id="user_a",
        agent_id="agent_a",
        goal_snapshot_json={"goal": "查资料"},
        current_plan_revision_id="plan_a",
        current_plan_checksum="a" * 64,
        capability_snapshot_json=resolution.snapshot,
        status="created",
        active_slot_key="dynamic:vertical",
    )

    assert instance.capability_snapshot_json["ability_ids"] == ["web-intelligence"]
    assert instance.capability_snapshot_json["composition_checksum"]


def test_virtual_vertical_guidance_cause_uses_frozen_execution_authorization() -> None:
    """验证能力包虚拟 Guidance 因果可启动工具操作且不会伪造 GeneralSkillUse。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        )
        frozen_snapshot = {
            **resolution.snapshot,
            "tools": [
                item.model_dump(mode="json") for item in resolution.capabilities
            ],
        }
        operation_snapshot = resolution.capabilities[0].model_dump(
            mode="json",
            exclude={"agent_id", "checksum"},
        )
        instance = SopInstance(
            id="sopinst_vertical_cause",
            tenant_id="tenant_a",
            session_id="session_a",
            kind="dynamic_task",
            active_slot_key="foreground:session_a",
            initiator_user_id="user_a",
            agent_id="agent_a",
            goal_snapshot_json={"goal": "查资料"},
            current_plan_revision_id="plan_vertical_cause",
            current_plan_checksum="a" * 64,
            capability_snapshot_json=frozen_snapshot,
            capability_checksum=capability_checksum(frozen_snapshot),
            context_json={"vertical_ability_ids": ["web-intelligence"]},
            status="running",
        )
        operation = SopOperation(
            id="sopop_vertical_cause",
            tenant_id="tenant_a",
            instance_id=instance.id,
            node_execution_id="sopnode_vertical_cause",
            operation_name="intelligence.web_search",
            idempotency_key="b" * 64,
            logical_action_id="vertical-cause-read",
            request_fingerprint="c" * 64,
            caused_by_skill_use_id="vertical_ability:web-intelligence",
            caused_by_skill_use_ids_json=["vertical_ability:web-intelligence"],
            capability_snapshot_json=operation_snapshot,
            capability_checksum=capability_checksum(operation_snapshot),
        )
        db.add(instance)
        db.add(operation)
        db.commit()

        SopExecutionStore(db).authorize_operation_skill_causes(operation)

        operation.capability_snapshot_json = {
            **operation_snapshot,
            "name": "intelligence.web_read",
        }
        operation.capability_checksum = capability_checksum(
            operation.capability_snapshot_json
        )
        with pytest.raises(SopExecutionSkillAuthorizationError) as invalid:
            SopExecutionStore(db).authorize_operation_skill_causes(operation)
        assert invalid.value.authorization_code == "GENERAL_SKILL_TOOL_CAUSE_INVALID"
    engine.dispose()


def test_vertical_result_contract_requires_sections_and_successful_source_urls() -> None:
    """验证全网情报员结果必须有固定章节，并披露成功回执中的来源 URL。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        )
        instance = SopInstance(
            id="sopinst_result_contract",
            tenant_id="tenant_a",
            session_id="session_a",
            kind="dynamic_task",
            initiator_user_id="user_a",
            agent_id="agent_a",
            goal_snapshot_json={"goal": "查资料"},
            current_plan_revision_id="plan_a",
            current_plan_checksum="a" * 64,
            capability_snapshot_json=resolution.snapshot,
            status="running",
            active_slot_key="dynamic:vertical",
        )
        db.add(instance)
        db.add(
            SopOperation(
                id="sopop_result_contract",
                tenant_id="tenant_a",
                instance_id=instance.id,
                node_execution_id="node_a",
                operation_name="intelligence.web_read",
                idempotency_key="idem_a",
                logical_action_id="action_a",
                request_fingerprint="b" * 64,
                status="succeeded",
                result_json={"data": {"url": "https://example.com/article"}},
                capability_snapshot_json=resolution.capabilities[1].model_dump(mode="json"),
            )
        )
        db.add(
            SopOperation(
                id="sopop_result_quality",
                tenant_id="tenant_a",
                instance_id=instance.id,
                node_execution_id="node_search",
                operation_name="intelligence.web_search",
                idempotency_key="idem_search",
                logical_action_id="action_search",
                request_fingerprint="search-fingerprint",
                status="succeeded",
                result_json={
                    "data": {
                        "query": "没有结果的主题",
                        "results": [],
                        "count": 0,
                        "result_state": "no_results",
                        "provider": "Exa via MCP",
                    }
                },
                capability_snapshot_json=resolution.capabilities[0].model_dump(mode="json"),
            )
        )
        db.commit()

        agent = DynamicTaskAgent(db)
        incomplete = DynamicTaskResult(
            markdown="## 结论摘要\n目前信息不足。",
            criterion_evidence={},
        )
        errors = agent._vertical_ability_result_errors(instance, incomplete)

        assert "missing_section:关键事实" in errors
        assert "missing_section:来源与证据" in errors
        assert "missing_section:冲突/缺口" in errors
        assert "source_url_not_disclosed:https://example.com/article" in errors
        assert "no_results_not_disclosed:intelligence.web_search" in errors

        complete = DynamicTaskResult(
            markdown=(
                "## 结论摘要\n已核验。\n## 关键事实\n事实。\n"
                "## 来源与证据\nhttps://example.com/article\n## 冲突/缺口\n"
                "本次搜索返回 0 条结果。\n## 后续建议\n继续监测该来源。"
            ),
            criterion_evidence={},
        )
        assert agent._vertical_ability_result_errors(instance, complete) == []
    engine.dispose()


def test_vertical_source_url_repair_replays_authoritative_operation_urls() -> None:
    """验证垂直能力结果修复轮会重建成功 Operation 已回执的完整来源 URL。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_repair",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n已核验。\n## 冲突/缺口\n暂无。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="补全来源证据",
        ),
    )
    repaired = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback={
            "verification": {
                "vertical_ability_errors": [
                    "source_url_not_disclosed:https://example.com/article"
                ]
            }
        },
        source_urls=("https://example.com/article", "https://example.com/ignored"),
    )

    markdown = repaired.proposal.arguments["markdown"]
    assert "## 来源与证据" in markdown
    assert "https://example.com/article" in markdown
    assert "https://example.com/ignored" in markdown


def test_vertical_source_urls_are_added_on_first_answer_without_model_repair() -> None:
    """首轮答案漏写来源时只补成功回执 URL，不额外消耗一次模型修复。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_initial",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n已核验。\n## 后续建议\n继续。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="形成结果",
        ),
    )

    completed = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback=None,
        source_urls=("https://example.com/article",),
    )

    assert "## 来源与证据" in completed.proposal.arguments["markdown"]
    assert "https://example.com/article" in completed.proposal.arguments["markdown"]
    markdown = completed.proposal.arguments["markdown"]
    assert markdown.index("https://example.com/article") < markdown.index("## 后续建议")
    assert "https://example.com/article\n\n## 后续建议" in markdown


def test_vertical_source_urls_are_preserved_when_repair_targets_another_error() -> None:
    """验证修复轮即使只报告一个 URL 缺口也会重建全部权威来源。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_other_error",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n已补充查询证据。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="修复结果证据",
        ),
    )

    repaired = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback={
            "verification": {
                "vertical_ability_errors": [
                    "source_url_not_disclosed:https://example.com/article",
                ],
                "missing_result_evidence": ["step_search:query"],
            }
        },
        source_urls=(
            "https://example.com/search",
            "https://example.com/article",
            "https://example.com/community",
        ),
    )

    markdown = repaired.proposal.arguments["markdown"]
    assert "https://example.com/search" in markdown
    assert "https://example.com/article" in markdown
    assert "https://example.com/community" in markdown


def test_vertical_source_url_disclosure_binds_existing_url_to_operation() -> None:
    """已有来源 URL 也要补上 Operation 身份，便于把截断说明绑定到对应回执。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_label",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": (
                    "## 结论摘要\n已核验。\n## 来源与证据\n"
                    "https://example.com/article\n"
                ),
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="绑定来源回执",
        ),
    )

    completed = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback=None,
        source_urls=("https://example.com/article",),
        source_operation_names={
            "https://example.com/article": "intelligence.web_read",
        },
    )

    markdown = completed.proposal.arguments["markdown"]
    assert "intelligence.web_read：https://example.com/article" in markdown


def test_vertical_source_url_disclosure_adds_missing_operation_from_shared_url() -> None:
    """搜索与读取共用 URL 时，报告必须补齐缺失的 web_read 身份。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_shared",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": (
                    "## 结论摘要\n已核验。\n## 来源与证据\n"
                    "- intelligence.web_search：https://example.com/article\n"
                ),
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="补齐共享来源回执",
        ),
    )

    completed = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback=None,
        source_urls=("https://example.com/article",),
        source_operation_names={
            "https://example.com/article": "intelligence.web_read、intelligence.web_search",
        },
    )

    markdown = completed.proposal.arguments["markdown"]
    assert "intelligence.web_read、intelligence.web_search：https://example.com/article" in markdown


def test_vertical_source_url_repair_does_not_accept_a_longer_malformed_url_prefix() -> None:
    """来源 URL 后拼接标题时必须补回可独立点击的权威 URL，而不是接受合法前缀。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_source_prefix",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": (
                    "## 结论摘要\n已核验。\n## 来源与证据\n"
                    "https://example.com/article%EF%BC%88标题）\n"
                ),
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="保留来源",
        ),
    )

    repaired = _ensure_vertical_source_url_disclosures(
        response,
        repair_feedback=None,
        source_urls=("https://example.com/article",),
    )

    markdown = repaired.proposal.arguments["markdown"]
    assert markdown.count("https://example.com/article") == 2
    assert "- https://example.com/article" in markdown


def test_vertical_required_result_evidence_replays_scalar_operation_facts() -> None:
    """验证垂直结果漏写 query/provider/url 时只补成功 Operation 的标量回证。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_required_evidence",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n已核验。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="形成结果",
        ),
    )

    completed = _ensure_vertical_required_result_evidence_disclosures(
        response,
        required_evidence_by_step={
            "step_search": {"query": "公开设计系统", "provider": "Exa via MCP"},
            "step_read": {"url": "https://example.com/article", "provider": "Jina Reader"},
            "step_regular": {"query": "不应被补入"},
        },
        vertical_step_keys={"step_search", "step_read"},
    )

    markdown = completed.proposal.arguments["markdown"]
    assert "step_search.query：公开设计系统" in markdown
    assert "step_search.provider：Exa via MCP" in markdown
    assert "step_read.url：https://example.com/article" in markdown
    assert "step_read.provider：Jina Reader" in markdown
    assert "不应被补入" not in markdown


def test_vertical_no_result_repair_replays_authoritative_empty_state() -> None:
    """验证空结果修复只补入运行时确认的 0 条状态，不生成 Provider 内容。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_no_result_repair",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n当前信息不足。\n## 冲突/缺口\n待补充。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="补充空结果状态",
        ),
    )
    repaired = _ensure_vertical_result_quality_disclosures(
        response,
        repair_feedback={
            "verification": {
                "vertical_ability_errors": [
                    "no_results_not_disclosed:intelligence.web_search",
                ]
            }
        },
        no_result_operations=("intelligence.web_search",),
    )

    markdown = repaired.proposal.arguments["markdown"]
    assert "intelligence.web_search：Provider 返回 0 条" in markdown
    assert "`no_results`" in markdown


def test_vertical_no_result_settlement_binds_empty_state_to_operation() -> None:
    """最终结算即使没有修复轮也必须绑定空集合的 Operation 身份与状态。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_no_result_settlement",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n当前信息不足。\n## 冲突/缺口\n待补充。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="形成带回执的结果",
        ),
    )
    settled = _ensure_vertical_result_quality_disclosures(
        response,
        repair_feedback=None,
        no_result_operations=("intelligence.github_releases",),
        no_result_operation_records=(
            (
                "intelligence.github_releases",
                "operation_id=sopop_empty",
                "repository=example/project",
            ),
        ),
    )

    markdown = settled.proposal.arguments["markdown"]
    assert "intelligence.github_releases" in markdown
    assert "operation_id=sopop_empty" in markdown
    assert "result_state=no_results" in markdown
    assert "count=0" in markdown


def test_vertical_result_limit_disclosure_replays_provider_partial_facts() -> None:
    """验证成功垂直回执的截断和子资源错误会绑定 Operation 进入最终报告。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        )
        v2ex_capability = next(
            item for item in resolution.capabilities if item.name == "intelligence.v2ex_topics"
        )
        instance = SopInstance(
            id="sopinst_result_limit",
            tenant_id="tenant_a",
            session_id="session_a",
            kind="dynamic_task",
            initiator_user_id="user_a",
            agent_id="agent_a",
            goal_snapshot_json={"goal": "查社区反馈"},
            current_plan_revision_id="plan_a",
            current_plan_checksum="a" * 64,
            capability_snapshot_json=resolution.snapshot,
            status="running",
            active_slot_key="dynamic:vertical",
        )
        db.add(instance)
        db.add(
            SopOperation(
                id="sopop_result_limit",
                tenant_id="tenant_a",
                instance_id=instance.id,
                node_execution_id="node_a",
                operation_name="intelligence.v2ex_topics",
                idempotency_key="idem_limit",
                logical_action_id="action_limit",
                request_fingerprint="b" * 64,
                status="succeeded",
                request_json={"mode": "node", "node_name": "programmer"},
                result_json={
                    "data": {
                        "mode": "node",
                        "items": [
                            {
                                "id": 1,
                                "content_truncated": True,
                                "subresource_errors": [
                                    {
                                        "resource": "v2ex.replies",
                                        "code": "FIXTURE_REPLY_TIMEOUT",
                                        "url": "https://www.v2ex.com/api/replies/show.json",
                                    }
                                ],
                            }
                        ],
                        "count": 1,
                        "result_state": "success_with_data",
                        "provider": "V2EX public API",
                    }
                },
                capability_snapshot_json=v2ex_capability.model_dump(mode="json"),
            )
        )
        db.commit()

        limitations = _successful_vertical_result_limitations(db, instance)

    response = CompletedProviderProposal(
        response_id="provider_vertical_result_limit",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": "## 结论摘要\n已核验。\n## 后续建议\n继续。",
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="形成结果",
        ),
    )
    completed = _ensure_vertical_result_limit_disclosures(
        response,
        limitations=limitations,
    )

    markdown = completed.proposal.arguments["markdown"]
    assert "operation_id=sopop_result_limit" in markdown
    assert "content_truncated" in markdown
    assert "FIXTURE_REPLY_TIMEOUT" in markdown
    assert "不能视为完整读取" in markdown
    engine.dispose()


def test_vertical_failed_operation_replays_request_and_error_code() -> None:
    """验证失败垂直 Operation 的目标、状态和错误码会进入最终报告缺口。"""

    response = CompletedProviderProposal(
        response_id="provider_vertical_failed_operation",
        finish_reason="stop",
        proposal=RuntimeActionProposal(
            action_kind=ActionKind.ANSWER,
            arguments={
                "markdown": (
                    "## 结论摘要\n已使用公共来源完成核对。\n## 冲突/缺口\n"
                    "## 后续建议\n稍后补读失败来源。"
                ),
                "criterion_evidence": {},
                "pending_questions": [],
            },
            rationale="披露失败来源",
        ),
    )

    completed = _ensure_vertical_failed_operation_disclosures(
        response,
        failed_operations=(
            (
                "intelligence.web_read",
                "url=https://www.example.com/blocked",
                "failed",
                "VERTICAL_ABILITY_ANTIBOT_PAGE",
            ),
        ),
    )

    markdown = completed.proposal.arguments["markdown"]
    assert "intelligence.web_read" in markdown
    assert "https://www.example.com/blocked" in markdown
    assert "status=failed" in markdown
    assert "VERTICAL_ABILITY_ANTIBOT_PAGE" in markdown
    assert markdown.index("VERTICAL_ABILITY_ANTIBOT_PAGE") < markdown.index("## 后续建议")


def test_vertical_executor_propagates_turn_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证能力适配器观察到取消后不会被执行器吞掉并伪装成普通 Provider 错误。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        )
        frozen_snapshot = {
            **resolution.snapshot,
            "tools": [item.model_dump(mode="json") for item in resolution.capabilities],
        }
        instance = SopInstance(
            id="sopinst_cancel_vertical",
            tenant_id="tenant_a",
            session_id="session_a",
            kind="dynamic_task",
            initiator_user_id="user_a",
            agent_id="agent_a",
            goal_snapshot_json={"goal": "查资料"},
            current_plan_revision_id="plan_a",
            current_plan_checksum="a" * 64,
            capability_snapshot_json=frozen_snapshot,
            capability_checksum=capability_checksum(frozen_snapshot),
            status="running",
            active_slot_key="dynamic:vertical",
        )
        db.add(instance)
        db.commit()

        def cancelled_adapter(*_args: object, **_kwargs: object) -> dict[str, object]:
            """模拟适配器底层观察到当前 Turn 已取消。"""

            raise TurnCancellationRequested("当前 Turn 已取消。")

        monkeypatch.setattr(
            "app.vertical_abilities.executor.execute_vertical_adapter",
            cancelled_adapter,
        )
        executor = VerticalAbilityToolExecutor(db)
        with pytest.raises(TurnCancellationRequested):
            executor.execute(
                "tenant_a",
                ToolCall(name="intelligence.web_search", arguments={"query": "test"}),
                agent_id="agent_a",
                actor_user_id="user_a",
                execution_id=instance.id,
            )
    engine.dispose()


def test_vertical_guidance_can_be_attached_to_only_relevant_plan_step() -> None:
    """验证多步骤计划中未引用能力包 Guidance 的步骤不会被误判为快照损坏。"""

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        resolution = VerticalAbilityResolver().resolve(
            ["web-intelligence"],
            tenant_id="tenant_a",
            agent_id="agent_a",
        )
        instance = SopInstance(
            id="sopinst_guidance_scope",
            tenant_id="tenant_a",
            session_id="session_a",
            kind="dynamic_task",
            initiator_user_id="user_a",
            agent_id="agent_a",
            goal_snapshot_json={"goal": "查资料"},
            current_plan_revision_id="plan_a",
            current_plan_checksum="a" * 64,
            capability_snapshot_json=resolution.snapshot,
            status="running",
            active_slot_key="dynamic:vertical",
        )
        unrelated_step = PlanStep(
            step_key="summarize",
            title="整理其他步骤",
            kind="answer",
            guidance_skill_use_ids=(),
        )

        assert DynamicTaskAgent(db)._vertical_step_guidance(
            instance,
            unrelated_step,
            {"vertical_ability:web-intelligence"},
        ) == []
    engine.dispose()
