"""
@Time       : 2026/09/08
@Author     : zhanglp8181
@File       : test_vertical_ability_github_depth.py
@CallChain  : pytest → GitHub 垂直能力详情适配器 → Issue/PR/Release 关联资源契约
@Description: 验证 Agent-Reach GitHub 深度读取的正文、评论、Review、作者、资产、分页和部分失败语义。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.vertical_abilities.adapters import (
    VerticalAbilityAdapterError,
    execute_vertical_adapter,
)


def _settings() -> SimpleNamespace:
    """构造不触达真实网络的 GitHub 适配器设置。"""

    return SimpleNamespace(
        tool_timeout_seconds=8,
        tool_outbound_allowed_hosts="",
        normalized_tool_base_url="http://localhost:5137",
    )


def _issue_fixture() -> dict[str, object]:
    """构造包含正文、评论计数和作者身份的公开 Issue 回执。"""

    return {
        "number": 42,
        "title": "CLI completion loses state",
        "html_url": "https://github.com/acme/tool/issues/42",
        "state": "open",
        "user": {"login": "alice"},
        "comments": 3,
        "body": "The completion state disappears after resume.",
        "labels": [{"name": "bug"}],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
    }


def _comment(comment_id: int, body: str) -> dict[str, object]:
    """构造一个带来源和作者的 GitHub 评论。"""

    return {
        "id": comment_id,
        "html_url": f"https://github.com/acme/tool/issues/42#issuecomment-{comment_id}",
        "user": {"login": f"reviewer-{comment_id}"},
        "body": body,
        "created_at": "2026-09-03T00:00:00Z",
        "updated_at": "2026-09-03T00:00:00Z",
        "author_association": "CONTRIBUTOR",
    }


def test_github_issue_reads_body_comments_author_and_page_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Issue 详情不再退化为标题列表，并披露评论未读取完的事实。"""

    calls: list[str] = []

    def github(path: str, **_kwargs: object) -> object:
        """按固定 GitHub API 路径返回详情、分页评论和作者公开资料。"""

        calls.append(path)
        if path == "/repos/acme/tool/issues/42":
            return _issue_fixture()
        if path == "/repos/acme/tool/issues/42/comments?per_page=2&page=1":
            return [_comment(1, "first"), _comment(2, "second")]
        if path == "/repos/acme/tool/issues/42/comments?per_page=2&page=2":
            return []
        if path == "/users/alice":
            return {
                "login": "alice",
                "name": "Alice",
                "html_url": "https://github.com/alice",
                "bio": "CLI maintainer",
                "public_repos": 4,
                "followers": 8,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", github)
    result = execute_vertical_adapter(
        "github.issue",
        {
            "repository": "acme/tool",
            "issue_number": 42,
            "comments_limit": 2,
            "comments_page_count": 2,
            "include_author_profile": True,
        },
        settings=_settings(),
    )

    issue = result["issue"]
    assert issue["url"] == "https://github.com/acme/tool/issues/42"
    assert issue["body"] == "The completion state disappears after resume."
    assert issue["comments_items"][0]["body"] == "first"
    assert issue["comments_count_fetched"] == 2
    assert issue["comments_incomplete"] is True
    assert issue["author_profile"]["login"] == "alice"
    assert issue["author_profile_result_state"] == "success_with_data"
    assert result["partial"] is True
    assert result["subresource_errors"] == []
    assert calls == [
        "/repos/acme/tool/issues/42",
        "/repos/acme/tool/issues/42/comments?per_page=2&page=1",
        "/users/alice",
    ]


def _pull_request_fixture() -> dict[str, object]:
    """构造包含分支、合并状态和变更统计的 Pull Request 回执。"""

    return {
        "number": 7,
        "title": "Improve shell completion",
        "html_url": "https://github.com/acme/tool/pull/7",
        "state": "open",
        "user": {"login": "bob"},
        "comments": 1,
        "review_comments": 1,
        "body": "Add completion for PowerShell.",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "head": {"ref": "feature/completion", "sha": "a" * 40, "repo": {"full_name": "acme/tool"}},
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "acme/tool"}},
        "commits": 2,
        "changed_files": 1,
        "additions": 20,
        "deletions": 3,
    }


def test_github_pr_reads_discussion_reviews_files_and_author_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 PR 详情链同时保留讨论、Review、行级评论和文件变更。"""

    def github(path: str, **_kwargs: object) -> object:
        """按 PR 详情链返回各个独立关联资源。"""

        if path == "/repos/acme/tool/pulls/7":
            return _pull_request_fixture()
        if path == "/repos/acme/tool/issues/7/comments?per_page=20&page=1":
            return [_comment(10, "Looks good.")]
        if path == "/repos/acme/tool/pulls/7/reviews?per_page=20&page=1":
            return [
                {
                    "id": 11,
                    "html_url": "https://github.com/acme/tool/pull/7#pullrequestreview-11",
                    "user": {"login": "reviewer"},
                    "body": "Approved.",
                    "state": "APPROVED",
                    "submitted_at": "2026-09-04T00:00:00Z",
                    "commit_id": "a" * 40,
                }
            ]
        if path == "/repos/acme/tool/pulls/7/comments?per_page=20&page=1":
            return [_comment(12, "Rename this flag.")]
        if path == "/repos/acme/tool/pulls/7/files?per_page=50&page=1":
            return [
                {
                    "filename": "completion.ps1",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 3,
                    "changes": 23,
                    "blob_url": "https://github.com/acme/tool/blob/a/completion.ps1",
                    "patch": "@@ -1 +1 @@",
                }
            ]
        if path == "/users/bob":
            return {"login": "bob", "html_url": "https://github.com/bob", "public_repos": 2}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", github)
    result = execute_vertical_adapter(
        "github.pr",
        {"repository": "acme/tool", "pr_number": 7},
        settings=_settings(),
    )

    pull_request = result["pull_request"]
    assert pull_request["url"] == "https://github.com/acme/tool/pull/7"
    assert pull_request["head"]["ref"] == "feature/completion"
    assert pull_request["review_items"][0]["state"] == "APPROVED"
    assert pull_request["comment_items"][0]["body"] == "Looks good."
    assert pull_request["review_comment_items"][0]["body"] == "Rename this flag."
    assert pull_request["file_items"][0]["filename"] == "completion.ps1"
    assert pull_request["author_profile"]["login"] == "bob"
    assert result["partial"] is False
    assert result["subresource_errors"] == []


def test_github_pr_subresource_failure_is_partial_without_erasing_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Review 子资源失败只形成结构化部分结果，不把有效 PR 正文伪装成空成功。"""

    def github(path: str, **_kwargs: object) -> object:
        """让详情成功、Review 失败、其他关联资源返回空列表。"""

        if path == "/repos/acme/tool/pulls/7":
            return _pull_request_fixture()
        if "/reviews?" in path:
            raise VerticalAbilityAdapterError(
                "VERTICAL_ABILITY_PROVIDER_HTTP_ERROR", "HTTP 503", status_code=503,
            )
        if path == "/users/bob":
            return {"login": "bob", "html_url": "https://github.com/bob"}
        return []

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", github)
    result = execute_vertical_adapter(
        "github.pr",
        {"repository": "acme/tool", "pr_number": 7},
        settings=_settings(),
    )

    assert result["pull_request"]["body"] == "Add completion for PowerShell."
    assert result["pull_request"]["review_items_result_state"] == "error"
    assert result["partial"] is True
    assert result["subresource_errors"][0]["resource"] == "github.pr.review_items"
    assert result["subresource_errors"][0]["status_code"] == 503


def test_github_releases_preserve_release_notes_assets_and_no_results_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Release 说明、发布时间和资产下载统计可直接用于版本情报报告。"""

    def github(path: str, **_kwargs: object) -> object:
        """返回一页 Release，并让分页探测在第二页明确结束。"""

        if path == "/repos/acme/tool/releases?per_page=10&page=1":
            return [
                {
                    "id": 1,
                    "tag_name": "v2.0.0",
                    "name": "2.0.0",
                    "html_url": "https://github.com/acme/tool/releases/tag/v2.0.0",
                    "author": {"login": "alice"},
                    "body": "Faster startup and PowerShell completion.",
                    "draft": False,
                    "prerelease": False,
                    "created_at": "2026-09-01T00:00:00Z",
                    "published_at": "2026-09-02T00:00:00Z",
                    "assets": [
                        {
                            "name": "tool-windows.zip",
                            "browser_download_url": "https://github.com/acme/tool/releases/download/v2.0.0/tool-windows.zip",
                            "content_type": "application/zip",
                            "size": 123,
                            "download_count": 9,
                        }
                    ],
                }
            ]
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr("app.vertical_abilities.adapters._github_api_json", github)
    result = execute_vertical_adapter(
        "github.releases",
        {"repository": "acme/tool", "release_limit": 10, "page_count": 1},
        settings=_settings(),
    )

    release = result["releases"][0]
    assert result["count"] == 1
    assert result["result_state"] == "success_with_data"
    assert release["tag_name"] == "v2.0.0"
    assert release["body"] == "Faster startup and PowerShell completion."
    assert release["assets"][0]["download_count"] == 9
    assert result["partial"] is False
    assert result["has_more"] is False
