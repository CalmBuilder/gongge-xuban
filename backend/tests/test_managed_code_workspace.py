"""
@Time       : 2026/08/13 02:32
@Author     : zhanglp8181
@File       : test_managed_code_workspace.py
@CallChain  : pytest → ManagedCodeWorkspaceService → 受管 Git 工作区/容器测试
@Description: 固化路径隔离、任务分支、乐观锁、固定检查档案和幂等提交边界。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from app.tools.managed_workspace import (
    ManagedCodeWorkspaceError,
    ManagedCodeWorkspaceService,
)


def _git(repo: Path, *args: str) -> str:
    """在测试仓库执行固定 Git 命令并返回标准输出。"""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def managed_repo(tmp_path: Path) -> tuple[Path, ManagedCodeWorkspaceService]:
    """创建没有凭据、可重复初始化的最小受管 Git 仓库。"""

    root = tmp_path / "managed"
    repo = root / "tenant_a" / "refund-demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "robot@example.invalid")
    _git(repo, "config", "user.name", "Workspace Robot")
    (repo / "refund.py").write_text("STATUS = 'pending'\n", encoding="utf-8")
    _git(repo, "add", "refund.py")
    _git(repo, "commit", "-m", "baseline")
    return repo, ManagedCodeWorkspaceService(root=root)


def test_read_and_patch_use_task_branch_and_content_precondition(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService],
) -> None:
    """验证读取不泄露宿主路径，写入只发生在 Execution 专属分支且要求旧内容哈希。"""

    repo, service = managed_repo
    before = service.read_file(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        path="refund.py",
    )
    assert before == {
        "path": "refund.py",
        "content": "STATUS = 'pending'\n",
        "sha256": hashlib.sha256(b"STATUS = 'pending'\n").hexdigest(),
    }
    assert str(repo.parent) not in str(before)

    changed = service.apply_file(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_refund_001",
        path="refund.py",
        expected_sha256=before["sha256"],
        content="STATUS = 'approved'\n",
    )
    assert changed["branch"] == "task/exec_refund_001"
    assert changed["changed"] is True
    assert _git(repo, "branch", "--show-current") == "task/exec_refund_001"

    with pytest.raises(ManagedCodeWorkspaceError, match="WORKSPACE_CONTENT_CHANGED"):
        service.apply_file(
            tenant_id="tenant_a",
            workspace_id="refund-demo",
            execution_id="exec_refund_001",
            path="refund.py",
            expected_sha256=before["sha256"],
            content="STATUS = 'rejected'\n",
        )


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", ".git/config", "link", "folder/../../secret"],
)
def test_workspace_rejects_escape_git_metadata_and_symlink(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService],
    path: str,
) -> None:
    """验证相对路径、Git 元数据和符号链接均不能越过受管资源边界。"""

    repo, service = managed_repo
    (repo / "link").symlink_to("/etc/passwd")
    with pytest.raises(ManagedCodeWorkspaceError):
        service.read_file(
            tenant_id="tenant_a",
            workspace_id="refund-demo",
            path=path,
        )


def test_commit_is_execution_scoped_and_idempotent(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService],
) -> None:
    """验证提交只能落在同一任务分支，重复调用返回同一 SHA 而不制造空提交。"""

    repo, service = managed_repo
    before = service.read_file(
        tenant_id="tenant_a", workspace_id="refund-demo", path="refund.py"
    )
    service.apply_file(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_commit_001",
        path="refund.py",
        expected_sha256=before["sha256"],
        content="STATUS = 'approved'\n",
    )
    first = service.commit(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_commit_001",
        message="feat: add refund approval",
        paths=["refund.py"],
    )
    second = service.commit(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_commit_001",
        message="feat: add refund approval",
        paths=["refund.py"],
    )
    assert first["commit_sha"] == second["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert second["replayed"] is True


def test_commit_rejects_changes_not_named_in_approved_paths(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService],
) -> None:
    """验证检查过程或其他并发来源夹带的文件不会被一次性提交静默纳入。"""

    repo, service = managed_repo
    before = service.read_file(
        tenant_id="tenant_a", workspace_id="refund-demo", path="refund.py"
    )
    service.apply_file(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_commit_guard",
        path="refund.py",
        expected_sha256=before["sha256"],
        content="STATUS = 'approved'\n",
    )
    (repo / "unapproved.txt").write_text("must not be committed\n", encoding="utf-8")

    with pytest.raises(ManagedCodeWorkspaceError, match="WORKSPACE_UNAPPROVED_CHANGES"):
        service.commit(
            tenant_id="tenant_a",
            workspace_id="refund-demo",
            execution_id="exec_commit_guard",
            message="feat: guarded commit",
            paths=["refund.py"],
        )


def test_checks_accept_only_published_profile_and_pinned_container(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证模型不能提交 argv/image/network 参数，运行器只消费部署方冻结的检查档案。"""

    _repo, service = managed_repo
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """捕获固定 Docker argv，模拟容器检查成功。"""

        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    monkeypatch.setattr(service, "_run_container", fake_run)
    result = service.run_check(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_check_001",
        profile="backend-unit",
        profiles={
            "backend-unit": {
                "image": "python@sha256:" + "a" * 64,
                "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
                "timeout_seconds": 60,
            }
        },
    )
    assert result["exit_code"] == 0
    assert result["profile"] == "backend-unit"
    command = captured[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in command
    assert "--read-only" in command
    assert f"{_repo}:/workspace:ro" in command
    assert "PYTHONDONTWRITEBYTECODE=1" in command
    assert "python@sha256:" + "a" * 64 in command

    with pytest.raises(ManagedCodeWorkspaceError, match="WORKSPACE_CHECK_PROFILE_UNKNOWN"):
        service.run_check(
            tenant_id="tenant_a",
            workspace_id="refund-demo",
            execution_id="exec_check_001",
            profile="model-supplied-command",
            profiles={},
        )


def test_real_container_check_has_no_network_and_returns_test_receipt(
    managed_repo: tuple[Path, ManagedCodeWorkspaceService],
) -> None:
    """使用固定 digest 的真实 Python 容器执行标准库测试，验证隔离 argv 与回执。"""

    repo, service = managed_repo
    image = (
        "python@sha256:"
        "9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4"
    )
    available = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
    )
    if available.returncode != 0:
        pytest.skip("pinned Python container image is not available")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_refund.py").write_text(
        "import socket\n"
        "import unittest\n\n"
        "class RefundTest(unittest.TestCase):\n"
        "    def test_status(self):\n"
        "        self.assertEqual('approval_required', 'approval_required')\n\n"
        "    def test_network_is_disabled(self):\n"
        "        with self.assertRaises(OSError):\n"
        "            socket.create_connection(('1.1.1.1', 53), timeout=0.2)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_refund.py")
    _git(repo, "commit", "-m", "add baseline test")
    result = service.run_check(
        tenant_id="tenant_a",
        workspace_id="refund-demo",
        execution_id="exec_real_container",
        profile="backend-unit",
        profiles={
            "backend-unit": {
                "image": image,
                "argv": [
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ],
                "timeout_seconds": 60,
            }
        },
    )
    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert "test_status" in str(result["stderr"])
    assert "test_network_is_disabled" in str(result["stderr"])
