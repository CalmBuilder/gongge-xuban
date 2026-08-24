"""
@Time       : 2026/08/13 20:57
@Author     : zhanglp8181
@File       : input_parser_process.py
@CallChain  : extraction worker → 隔离子进程 → input_parser_cli → ParsedElement
@Description: 以超时、临时目录和可选POSIX资源上限运行附件解析器，失败时清除全部中间文件。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from app import paths
from app.session.input_extraction import InputExtractionError, ParsedElement


def _parser_command(
    *,
    file_format: str,
    output_path: Path,
    input_path: Path | None = None,
    input_fd: int | None = None,
) -> list[str]:
    """按开发或冻结形态生成parser命令，打包态复用同一受签名桌面可执行文件。"""

    if (input_path is None) == (input_fd is None):
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入身份无效。")
    arguments = [
        "--format",
        file_format,
        "--output",
        str(output_path),
    ]
    if input_fd is not None:
        arguments.extend(("--input-fd", str(input_fd)))
    else:
        arguments.extend(("--input", str(input_path)))
    if paths.is_frozen():
        return [sys.executable, "--input-parser", *arguments]
    return [sys.executable, "-m", "app.session.input_parser_cli", *arguments]


def run_attachment_parser_isolated(
    data: bytes,
    *,
    file_format: str,
    timeout_seconds: int,
    memory_mb: int,
) -> list[ParsedElement]:
    """在独立Python进程解析CSV，强制超时并在POSIX平台限制地址空间。"""

    with tempfile.TemporaryDirectory(prefix="gongge-input-parser-") as directory:
        root = Path(directory)
        input_path = root / f"input.{file_format}"
        input_path.write_bytes(data)
        os.chmod(input_path, 0o600)
        return run_attachment_parser_path_isolated(
            input_path,
            file_format=file_format,
            timeout_seconds=timeout_seconds,
            memory_mb=memory_mb,
        )


def run_attachment_parser_path_isolated(
    input_path: Path,
    *,
    file_format: str,
    timeout_seconds: int,
    memory_mb: int,
) -> list[ParsedElement]:
    """安全打开普通单链接文件并委托fd解析；该入口主要服务fixture和兼容调用。"""

    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise InputExtractionError(
            "ATTACHMENT_PARSER_FD_ISOLATION_UNAVAILABLE",
            "当前平台未启用安全附件解析。",
        )
    try:
        descriptor = os.open(input_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。")
        return run_attachment_parser_fd_isolated(
            descriptor,
            file_format=file_format,
            timeout_seconds=timeout_seconds,
            memory_mb=memory_mb,
        )
    finally:
        os.close(descriptor)


def run_attachment_parser_fd_isolated(
    input_fd: int,
    *,
    file_format: str,
    timeout_seconds: int,
    memory_mb: int,
) -> list[ParsedElement]:
    """把父进程已授权的只读inode fd显式继承给parser，禁止子进程重新解析受管路径。"""

    if os.name != "posix" or not hasattr(subprocess, "run"):
        raise InputExtractionError(
            "ATTACHMENT_PARSER_FD_ISOLATION_UNAVAILABLE",
            "当前平台未启用安全附件解析。",
        )
    try:
        info = os.fstat(input_fd)
    except OSError as exc:
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InputExtractionError("ATTACHMENT_PARSER_INPUT_INVALID", "解析输入不可用。")

    with tempfile.TemporaryDirectory(prefix="gongge-input-parser-output-") as directory:
        output_path = Path(directory) / "output.json"

        def limits() -> None:
            """在支持resource的平台限制parser地址空间和CPU时间。"""

            try:
                import resource

                memory_bytes = int(memory_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                cpu_seconds = max(1, int(timeout_seconds))
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
            except (ImportError, OSError, ValueError):
                if sys.platform.startswith("linux"):
                    raise

        try:
            completed = subprocess.run(
                _parser_command(
                    file_format=file_format,
                    input_fd=input_fd,
                    output_path=output_path,
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                    "PYTHONNOUSERSITE": "1",
                    "NO_PROXY": "*",
                },
                pass_fds=(input_fd,),
                preexec_fn=limits if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise InputExtractionError("ATTACHMENT_PARSER_TIMEOUT", "附件解析超时。") from exc
        if completed.returncode != 0 or not output_path.is_file():
            try:
                failure = json.loads(completed.stdout.strip())
            except (json.JSONDecodeError, AttributeError):
                failure = {}
            raise InputExtractionError(
                str(failure.get("error_code") or "ATTACHMENT_PARSER_FAILED"),
                str(failure.get("detail") or "附件解析失败。"),
            )
        raw = json.loads(output_path.read_text(encoding="utf-8"))
        return [ParsedElement(**item) for item in raw]


def run_csv_parser_isolated(
    data: bytes,
    *,
    timeout_seconds: int,
    memory_mb: int,
) -> list[ParsedElement]:
    """保留CSV调用兼容层并委托统一隔离parser入口。"""

    return run_attachment_parser_isolated(
        data,
        file_format="csv",
        timeout_seconds=timeout_seconds,
        memory_mb=memory_mb,
    )
