"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : final_f3_private_jsonl.py
@CallChain  : F3自有运行目录 → 有界跨进程观察账本追加 → SDK与记忆完整性读回
@Description: 为同一F3运行下的多PID JSONL文件共享字节、行数及文件数上限。
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import time


class PrivateJsonlLimitError(RuntimeError):
    """观察JSONL损坏、私有属性不符或运行累计预算耗尽。"""


def _validate_private_directory(runtime: Path) -> int:
    """打开并验证当前UID的真实700目录，拒绝链接路径与宽松权限。"""
    try:
        if runtime.is_symlink() or runtime.resolve() != runtime:
            raise ValueError
        descriptor = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            os.close(descriptor)
            raise ValueError
        return descriptor
    except (OSError, ValueError) as error:
        raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_RUNTIME_REQUIRED") from error


def _encode_bounded(value: object, maximum_bytes: int) -> bytes:
    """逐块编码JSON，只在行预算内积累字节，避免无界完整序列化副本。"""
    encoded = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(value):
            packet = chunk.encode("utf-8")
            if len(encoded) + len(packet) + 1 > maximum_bytes:
                raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_ROW_BYTE_LIMIT")
            encoded.extend(packet)
    except (TypeError, ValueError) as error:
        if isinstance(error, PrivateJsonlLimitError):
            raise
        raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_JSON_INVALID") from error
    encoded.append(0x0A)
    return bytes(encoded)


def append_private_jsonl(
    runtime: Path,
    path: Path,
    value: object,
    *,
    maximum_bytes: int,
    maximum_rows: int,
    maximum_files: int,
    patterns: tuple[str, ...],
    row_limit_code: str,
    byte_limit_code: str,
    file_limit_code: str,
) -> int:
    """原子预算核验后同步追加一条同轮JSONL，并返回实际新增字节数。"""
    row = _encode_bounded(value, maximum_bytes)
    return append_private_jsonl_bytes(
        runtime, path, row, maximum_bytes=maximum_bytes, maximum_rows=maximum_rows,
        maximum_files=maximum_files, patterns=patterns, row_limit_code=row_limit_code,
        byte_limit_code=byte_limit_code, file_limit_code=file_limit_code,
    )


def append_private_jsonl_bytes(
    runtime: Path,
    path: Path,
    row: bytes,
    *,
    maximum_bytes: int,
    maximum_rows: int,
    maximum_files: int,
    patterns: tuple[str, ...],
    row_limit_code: str,
    byte_limit_code: str,
    file_limit_code: str,
) -> int:
    """在跨进程排他锁内检查本轮所有SDK/记忆文件，再完成可靠续写。"""
    if (not row.endswith(b"\n") or len(row) > maximum_bytes or path.parent != runtime
            or not any(path.match(pattern) for pattern in patterns)):
        raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_PATH_INVALID")
    directory = _validate_private_directory(runtime)
    lock_descriptor = -1
    locked = False
    try:
        lock_descriptor = os.open(
            ".f3-observation-append.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        lock_info = os.fstat(lock_descriptor)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
            raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_LOCK_INVALID")
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_LOCK_TIMEOUT") from error
                time.sleep(0.01)

        names: list[str] = []
        for entry in os.scandir(directory):
            if any(entry.name.startswith(pattern.split("*")[0])
                   and entry.name.endswith(pattern.rsplit("*", 1)[-1]) for pattern in patterns):
                names.append(entry.name)
        if len(names) > maximum_files or (path.name not in names and len(names) >= maximum_files):
            raise PrivateJsonlLimitError(file_limit_code)

        total_bytes = 0
        total_rows = 0
        for name in names:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_FILE_INVALID")
                total_bytes += info.st_size
                file_rows = 0
                last_byte = b""
                while chunk := os.read(descriptor, 64 * 1024):
                    file_rows += chunk.count(b"\n")
                    last_byte = chunk[-1:]
                if info.st_size and last_byte != b"\n":
                    raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_FILE_INVALID")
                total_rows += file_rows
            finally:
                os.close(descriptor)
        if total_rows >= maximum_rows:
            raise PrivateJsonlLimitError(row_limit_code)
        if total_bytes + len(row) > maximum_bytes:
            raise PrivateJsonlLimitError(byte_limit_code)

        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_FILE_INVALID")
            remaining = memoryview(row)
            while remaining:
                try:
                    written = os.write(descriptor, remaining)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("F3_PRIVATE_OBSERVATION_WRITE_FAILED")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return len(row)
    except OSError as error:
        raise PrivateJsonlLimitError("F3_PRIVATE_OBSERVATION_WRITE_FAILED") from error
    finally:
        if lock_descriptor >= 0:
            if locked:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        os.close(directory)
