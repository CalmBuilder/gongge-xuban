"""
@Time       : 2026/08/13 02:38
@Author     : zhanglp8181
@File       : managed_workspace.py
@CallChain  : ToolExecutor → ManagedCodeWorkspaceService → Git/隔离检查容器
@Description: 在部署方受管根目录内提供路径隔离、任务分支、乐观写入和固定容器检查能力。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTAINER_IMAGE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
_ALLOWED_CHECK_EXECUTABLES = frozenset({"python", "python3", "pytest", "ruff", "npm", "node"})
_MAX_TEXT_BYTES = 512 * 1024
_MAX_CHANGE_SET_BYTES = 2 * 1024 * 1024
_MAX_CHECK_OUTPUT_BYTES = 128 * 1024


class ManagedCodeWorkspaceError(RuntimeError):
    """表示受管工作区配置、路径、并发前置条件或隔离执行被拒绝。"""


class ManagedCodeWorkspaceService:
    """只在固定根目录和 Execution 专属 Git 分支内执行明确的代码动作。"""

    def __init__(self, *, root: str | Path) -> None:
        """固定部署方工作区根目录；调用参数不能改变该根目录。"""

        self.root = Path(root).expanduser().resolve()

    def read_file(self, *, tenant_id: str, workspace_id: str, path: str) -> dict[str, object]:
        """读取受限 UTF-8 文本并返回相对路径与内容哈希，不披露宿主绝对路径。"""

        repo = self._repository(tenant_id, workspace_id)
        target = self._safe_file(repo, path, must_exist=True)
        raw = target.read_bytes()
        if len(raw) > _MAX_TEXT_BYTES:
            raise ManagedCodeWorkspaceError("WORKSPACE_FILE_TOO_LARGE")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManagedCodeWorkspaceError("WORKSPACE_FILE_NOT_UTF8") from exc
        return {
            "path": target.relative_to(repo).as_posix(),
            "content": content,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def status(self, *, tenant_id: str, workspace_id: str) -> dict[str, object]:
        """返回分支、HEAD 与规范化变更列表，不返回仓库宿主路径。"""

        repo = self._repository(tenant_id, workspace_id)
        branch = self._git(repo, "branch", "--show-current")
        head = self._git(repo, "rev-parse", "HEAD")
        changes = [line for line in self._git(repo, "status", "--porcelain=v1").splitlines() if line]
        return {"branch": branch, "head": head, "changes": changes[:500]}

    def apply_file(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        execution_id: str,
        base_ref: str = "main",
        path: str,
        expected_sha256: str,
        content: str,
    ) -> dict[str, object]:
        """在任务分支按旧内容哈希原子替换单个文本文件，阻止并发覆盖。"""

        if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ManagedCodeWorkspaceError("WORKSPACE_FILE_TOO_LARGE")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ManagedCodeWorkspaceError("WORKSPACE_EXPECTED_SHA256_INVALID")
        repo = self._repository(tenant_id, workspace_id)
        with self._exclusive_lock(repo):
            branch = self._ensure_task_branch(repo, execution_id, base_ref=base_ref)
            target = self._safe_file(repo, path, must_exist=True)
            current = target.read_bytes()
            encoded = content.encode("utf-8")
            if encoded == current:
                return {
                    "path": target.relative_to(repo).as_posix(),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "branch": branch,
                    "changed": False,
                    "replayed": True,
                }
            if hashlib.sha256(current).hexdigest() != expected_sha256:
                raise ManagedCodeWorkspaceError("WORKSPACE_CONTENT_CHANGED")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".gongge-write-", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(target.stat().st_mode & 0o777)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {
                "path": target.relative_to(repo).as_posix(),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "branch": branch,
                "changed": True,
                "replayed": False,
            }

    def run_check(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        execution_id: str,
        base_ref: str = "main",
        profile: str,
        profiles: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        """在无网络、去能力、限资源容器中执行部署方发布的固定检查档案。"""

        definition = profiles.get(profile)
        if not isinstance(definition, Mapping):
            raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_PROFILE_UNKNOWN")
        image = str(definition.get("image") or "")
        raw_argv = definition.get("argv")
        timeout_seconds = int(definition.get("timeout_seconds") or 0)
        raw_expected_exit_codes = definition.get("expected_exit_codes", [0])
        required_output = definition.get("required_output_substrings", [])
        if (
            not _CONTAINER_IMAGE_PATTERN.fullmatch(image)
            or not isinstance(raw_argv, Sequence)
            or isinstance(raw_argv, (str, bytes))
            or not raw_argv
            or timeout_seconds < 1
            or timeout_seconds > 1800
            or not isinstance(raw_expected_exit_codes, list)
            or not raw_expected_exit_codes
            or len(raw_expected_exit_codes) > 16
            or any(
                not isinstance(code, int) or isinstance(code, bool) or code < 0 or code > 255
                for code in raw_expected_exit_codes
            )
            or not isinstance(required_output, list)
            or len(required_output) > 10
            or any(
                not isinstance(value, str) or not value or len(value) > 200
                for value in required_output
            )
        ):
            raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_PROFILE_INVALID")
        argv = [str(value) for value in raw_argv]
        if argv[0] not in _ALLOWED_CHECK_EXECUTABLES or any("\x00" in value for value in argv):
            raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_COMMAND_FORBIDDEN")
        if argv[0] in {"python", "python3"} and "-c" in argv:
            raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_COMMAND_FORBIDDEN")
        repo = self._repository(tenant_id, workspace_id)
        with self._exclusive_lock(repo):
            branch = self._ensure_task_branch(repo, execution_id, base_ref=base_ref)
            command = [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--pids-limit=128",
                "--memory=512m",
                "--cpus=1",
                "--security-opt=no-new-privileges",
                "--cap-drop=ALL",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--volume",
                f"{repo}:/workspace:ro",
                "--workdir",
                "/workspace",
                image,
                *argv,
            ]
            try:
                completed = self._run_container(command, timeout_seconds=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise ManagedCodeWorkspaceError("WORKSPACE_CHECK_TIMEOUT") from exc
            stdout = self._bounded_output(completed.stdout)
            stderr = self._bounded_output(completed.stderr)
            expected_exit_codes = sorted(set(raw_expected_exit_codes))
            output = f"{stdout}\n{stderr}"
            return {
                "profile": profile,
                "branch": branch,
                "exit_code": completed.returncode,
                "passed": completed.returncode in expected_exit_codes
                and all(value in output for value in required_output),
                "expected_exit_codes": expected_exit_codes,
                "required_output_substrings": list(required_output),
                "stdout": stdout,
                "stderr": stderr,
            }

    def apply_files(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        execution_id: str,
        changes: Sequence[Mapping[str, object]],
        base_ref: str = "main",
    ) -> dict[str, object]:
        """预检整组新增/更新后原子替换，失败时恢复已替换文件且不接受删除。"""

        if (
            not changes
            or len(changes) > 50
            or isinstance(changes, (str, bytes))
        ):
            raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_INVALID")
        repo = self._repository(tenant_id, workspace_id)
        with self._exclusive_lock(repo):
            branch = self._ensure_task_branch(repo, execution_id, base_ref=base_ref)
            prepared: list[tuple[Path, bytes | None, bytes, int]] = []
            seen: set[str] = set()
            total_bytes = 0
            for raw_change in changes:
                if not isinstance(raw_change, Mapping) or set(raw_change) != {
                    "path",
                    "expected_sha256",
                    "content",
                }:
                    raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_INVALID")
                raw_path = str(raw_change.get("path") or "")
                content = str(raw_change.get("content") or "")
                expected = raw_change.get("expected_sha256")
                target = self._safe_file(repo, raw_path, must_exist=False)
                relative = target.relative_to(repo).as_posix()
                if relative in seen or any(
                    parent.exists() and not parent.is_dir()
                    for parent in target.parents
                    if parent != repo and parent.is_relative_to(repo)
                ):
                    raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_INVALID")
                seen.add(relative)
                encoded = content.encode("utf-8")
                total_bytes += len(encoded)
                if len(encoded) > _MAX_TEXT_BYTES or total_bytes > _MAX_CHANGE_SET_BYTES:
                    raise ManagedCodeWorkspaceError("WORKSPACE_FILE_TOO_LARGE")
                if target.exists():
                    if not target.is_file():
                        raise ManagedCodeWorkspaceError("WORKSPACE_FILE_NOT_FOUND")
                    current = target.read_bytes()
                    if encoded != current and (
                        not isinstance(expected, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected)
                        or hashlib.sha256(current).hexdigest() != expected
                    ):
                        raise ManagedCodeWorkspaceError("WORKSPACE_CONTENT_CHANGED")
                    prepared.append((target, current, encoded, target.stat().st_mode & 0o777))
                else:
                    if expected is not None:
                        raise ManagedCodeWorkspaceError("WORKSPACE_CREATE_PRECONDITION_INVALID")
                    prepared.append((target, None, encoded, 0o644))
            replaced: list[tuple[Path, bytes | None, int]] = []
            created_directories: list[Path] = []
            try:
                for target, _current, _encoded, _mode in prepared:
                    created_directories.extend(
                        self._create_missing_directories(repo, target.parent)
                    )
                for target, current, encoded, mode in prepared:
                    if current == encoded:
                        continue
                    self._atomic_replace(target, encoded, mode=mode)
                    replaced.append((target, current, mode))
            except (OSError, ManagedCodeWorkspaceError) as exc:
                for target, current, mode in reversed(replaced):
                    if current is None:
                        target.unlink(missing_ok=True)
                    else:
                        self._atomic_replace(target, current, mode=mode)
                for directory in reversed(created_directories):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_WRITE_FAILED") from exc
            return {
                "branch": branch,
                "files": [
                    {
                        "path": target.relative_to(repo).as_posix(),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "changed": current != encoded,
                        "created": current is None,
                    }
                    for target, current, encoded, _mode in prepared
                ],
                "changed_count": len(replaced),
                "replayed": not replaced,
            }

    def commit(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        execution_id: str,
        base_ref: str = "main",
        message: str,
        paths: Sequence[str],
    ) -> dict[str, object]:
        """仅提交审批中明确列出的变更文件，并拒绝工作树夹带其他改动。"""

        normalized_message = " ".join(message.split())
        if not normalized_message or len(normalized_message) > 200:
            raise ManagedCodeWorkspaceError("WORKSPACE_COMMIT_MESSAGE_INVALID")
        if not paths or len(paths) > 100 or isinstance(paths, (str, bytes)):
            raise ManagedCodeWorkspaceError("WORKSPACE_COMMIT_PATHS_INVALID")
        repo = self._repository(tenant_id, workspace_id)
        with self._exclusive_lock(repo):
            branch = self._ensure_task_branch(repo, execution_id, base_ref=base_ref)
            normalized_paths = [
                self._safe_file(repo, str(path), must_exist=True).relative_to(repo).as_posix()
                for path in paths
            ]
            if len(set(normalized_paths)) != len(normalized_paths):
                raise ManagedCodeWorkspaceError("WORKSPACE_COMMIT_PATHS_INVALID")
            changes = self._git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            if not changes:
                return {
                    "branch": branch,
                    "commit_sha": self._git(repo, "rev-parse", "HEAD"),
                    "replayed": True,
                }
            changed_paths: list[str] = []
            for line in changes.splitlines():
                raw_path = line[3:]
                if " -> " in raw_path:
                    raise ManagedCodeWorkspaceError("WORKSPACE_UNAPPROVED_CHANGES")
                changed_paths.append(raw_path)
            if set(changed_paths) != set(normalized_paths):
                raise ManagedCodeWorkspaceError("WORKSPACE_UNAPPROVED_CHANGES")
            self._git(repo, "add", "--", *normalized_paths)
            self._git(repo, "commit", "-m", normalized_message)
            return {
                "branch": branch,
                "commit_sha": self._git(repo, "rev-parse", "HEAD"),
                "replayed": False,
            }

    def _repository(self, tenant_id: str, workspace_id: str) -> Path:
        """按两个稳定标识解析仓库，并拒绝符号链接、非仓库和根目录逃逸。"""

        if not _IDENTIFIER_PATTERN.fullmatch(tenant_id) or not _IDENTIFIER_PATTERN.fullmatch(
            workspace_id
        ):
            raise ManagedCodeWorkspaceError("WORKSPACE_ID_INVALID")
        candidate = self.root / tenant_id / workspace_id
        if candidate.is_symlink():
            raise ManagedCodeWorkspaceError("WORKSPACE_SYMLINK_FORBIDDEN")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_dir():
            raise ManagedCodeWorkspaceError("WORKSPACE_NOT_FOUND")
        if not (resolved / ".git").is_dir():
            raise ManagedCodeWorkspaceError("WORKSPACE_GIT_REQUIRED")
        return resolved

    @staticmethod
    def _atomic_replace(target: Path, content: bytes, *, mode: int) -> None:
        """在目标目录落临时文件并 fsync 后替换，避免暴露半写文件。"""

        descriptor, temporary_name = tempfile.mkstemp(prefix=".gongge-write-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _create_missing_directories(repo: Path, parent: Path) -> list[Path]:
        """在受管仓库内逐级创建缺失父目录，并返回可供失败回滚的创建清单。"""

        missing: list[Path] = []
        current = parent
        while current != repo and not current.exists():
            if not current.is_relative_to(repo):
                raise ManagedCodeWorkspaceError("WORKSPACE_PATH_FORBIDDEN")
            missing.append(current)
            current = current.parent
        if current != repo and (not current.is_dir() or current.is_symlink()):
            raise ManagedCodeWorkspaceError("WORKSPACE_CHANGE_SET_INVALID")
        created: list[Path] = []
        for directory in reversed(missing):
            directory.mkdir(mode=0o755)
            created.append(directory)
        return created

    def _safe_file(self, repo: Path, raw_path: str, *, must_exist: bool) -> Path:
        """将模型路径限制为普通相对文件，并逐级拒绝符号链接和 `.git`。"""

        normalized = raw_path.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or ".." in relative.parts
            or ".git" in relative.parts
        ):
            raise ManagedCodeWorkspaceError("WORKSPACE_PATH_FORBIDDEN")
        target = repo.joinpath(*relative.parts)
        current = repo
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ManagedCodeWorkspaceError("WORKSPACE_SYMLINK_FORBIDDEN")
        resolved = target.resolve(strict=must_exist)
        if not resolved.is_relative_to(repo):
            raise ManagedCodeWorkspaceError("WORKSPACE_PATH_FORBIDDEN")
        if must_exist and not resolved.is_file():
            raise ManagedCodeWorkspaceError("WORKSPACE_FILE_NOT_FOUND")
        return resolved

    def _ensure_task_branch(self, repo: Path, execution_id: str, *, base_ref: str) -> str:
        """创建或核对 Execution 专属分支，拒绝在其他任务分支上交叉写入。"""

        if (
            not _IDENTIFIER_PATTERN.fullmatch(execution_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,190}", base_ref)
            or ".." in base_ref.split("/")
            or base_ref.startswith("task/")
        ):
            raise ManagedCodeWorkspaceError("WORKSPACE_EXECUTION_ID_INVALID")
        expected = f"task/{execution_id}"
        current = self._git(repo, "branch", "--show-current")
        if current == expected:
            return expected
        if self._git(repo, "status", "--porcelain=v1"):
            raise ManagedCodeWorkspaceError("WORKSPACE_BASE_DIRTY")
        existing = self._git(repo, "branch", "--list", expected)
        if existing:
            self._git(repo, "switch", expected)
        else:
            try:
                self._git(repo, "rev-parse", "--verify", base_ref)
            except ManagedCodeWorkspaceError as exc:
                raise ManagedCodeWorkspaceError("WORKSPACE_BASE_REF_NOT_FOUND") from exc
            self._git(repo, "switch", "-c", expected, base_ref)
        return expected

    @staticmethod
    @contextmanager
    def _exclusive_lock(repo: Path):
        """以跨进程文件锁串行化分支切换和工作树动作，十秒内拿不到即失败。"""

        lock_path = repo.parent / f".{repo.name}.gongge-workspace.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise ManagedCodeWorkspaceError("WORKSPACE_LOCK_TIMEOUT")
                    time.sleep(0.05)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        """以固定二进制和关闭交互的环境执行 Git，规范化失败为领域错误。"""

        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0"},
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ManagedCodeWorkspaceError("WORKSPACE_GIT_FAILED") from exc
        return result.stdout.rstrip()

    @staticmethod
    def _run_container(
        command: list[str], *, timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        """把容器输出直接落临时文件并有界读取，防止恶意测试日志耗尽内存。"""

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                env={"PATH": os.environ.get("PATH", "")},
            )
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(_MAX_CHECK_OUTPUT_BYTES + 1).decode(
                "utf-8", errors="replace"
            )
            stderr = stderr_file.read(_MAX_CHECK_OUTPUT_BYTES + 1).decode(
                "utf-8", errors="replace"
            )
        return subprocess.CompletedProcess(command, return_code, stdout, stderr)

    @staticmethod
    def _bounded_output(value: str) -> str:
        """按 UTF-8 字节截断检查输出，避免结果账本被无限日志耗尽。"""

        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= _MAX_CHECK_OUTPUT_BYTES:
            return value
        return encoded[:_MAX_CHECK_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"
