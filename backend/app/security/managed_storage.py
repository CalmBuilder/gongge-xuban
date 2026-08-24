"""
@Time       : 2026/08/14 18:20
@Author     : zhanglp8181
@File       : managed_storage.py
@CallChain  : ManagedInputResourceService/ArtifactService → managed storage → local filesystem
@Description: 以fd-relative、O_NOFOLLOW和inode复核安全读写删除受管blob，阻断链接与目录替换竞态。
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator


class ManagedStorageError(RuntimeError):
    """表示受管locator、文件类型、链接计数或原子文件操作违反安全边界。"""


_SECURE_DIR_FD_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.link in os.supports_dir_fd
)


def managed_read_bytes(root: Path, locator: str) -> bytes:
    """通过已固定的目录fd读取普通单链接文件，目录重命名不改变授权目标。"""

    if not _supports_secure_dir_fd():
        return _fallback_read(root, locator)
    with _open_parent(root, locator, create=False) as (parent_fd, leaf):
        descriptor = _open_leaf(parent_fd, leaf, os.O_RDONLY)
        try:
            _assert_regular_single_link(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(descriptor)


def managed_open_read_fd(root: Path, locator: str) -> int:
    """返回调用方负责关闭的安全只读fd，供子进程继承同一已授权inode。"""

    if not _supports_secure_dir_fd():
        raise ManagedStorageError("MANAGED_STORAGE_FD_ISOLATION_UNAVAILABLE")
    with _open_parent(root, locator, create=False) as (parent_fd, leaf):
        descriptor = _open_leaf(parent_fd, leaf, os.O_RDONLY)
        try:
            _assert_regular_single_link(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor


def managed_write_bytes(root: Path, locator: str, data: bytes) -> None:
    """把内存内容写入受管目录的独占临时inode，再以不可覆盖链接原子发布。"""

    def copy(stream: BinaryIO) -> None:
        """把完整内存内容写入已受限的临时文件。"""

        stream.write(data)

    _managed_write(root, locator, copy)


def managed_write_from_path(root: Path, locator: str, source_path: Path) -> None:
    """拒绝链接源文件并流式复制到受管目录，避免信任调用方路径的后续变化。"""

    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source_path, source_flags)
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_SOURCE_INVALID") from exc
    try:
        _assert_regular_single_link(source_fd)

        def copy(target: BinaryIO) -> None:
            """从已经固定的源inode分块复制内容。"""

            while chunk := os.read(source_fd, 1024 * 1024):
                target.write(chunk)

        _managed_write(root, locator, copy)
    finally:
        os.close(source_fd)


def managed_unlink(root: Path, locator: str, *, missing_ok: bool = False) -> None:
    """先把目标原子改名为不可预测墓碑，再核对inode并删除，避免检查后替换。"""

    if not _supports_secure_dir_fd():
        _fallback_unlink(root, locator, missing_ok=missing_ok)
        return
    try:
        with _open_parent(root, locator, create=False) as (parent_fd, leaf):
            descriptor = _open_leaf(parent_fd, leaf, os.O_RDONLY)
            try:
                original = _assert_regular_single_link(descriptor)
                tombstone = f".purge-{secrets.token_hex(16)}"
                os.rename(leaf, tombstone, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                tombstone_fd = _open_leaf(parent_fd, tombstone, os.O_RDONLY)
                try:
                    moved = _assert_regular_single_link(tombstone_fd)
                    if (moved.st_dev, moved.st_ino) != (original.st_dev, original.st_ino):
                        raise ManagedStorageError("MANAGED_STORAGE_INODE_CHANGED")
                finally:
                    os.close(tombstone_fd)
                os.unlink(tombstone, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        if not missing_ok:
            raise ManagedStorageError("MANAGED_STORAGE_NOT_FOUND") from None
    except ManagedStorageError:
        raise
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_DELETE_FAILED") from exc


def managed_validate_path(root: Path, locator: str) -> Path:
    """在当前时点用fd-relative规则校验受管文件，并返回仅供受信调用方展示的词法路径。"""

    if not _supports_secure_dir_fd():
        return _fallback_path(root, locator)
    with _open_parent(root, locator, create=False) as (parent_fd, leaf):
        descriptor = _open_leaf(parent_fd, leaf, os.O_RDONLY)
        try:
            _assert_regular_single_link(descriptor)
        finally:
            os.close(descriptor)
    return root / Path(*_locator_parts(locator))


def _managed_write(root: Path, locator: str, copy: Callable[[BinaryIO], None]) -> None:
    """实现内存与文件源共用的独占inode发布协议。"""

    if not _supports_secure_dir_fd():
        _fallback_write(root, locator, copy)
        return
    with _open_parent(root, locator, create=True) as (parent_fd, leaf):
        temporary = f".write-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        published = False
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                copy(stream)
                stream.flush()
                os.fsync(descriptor)
            _assert_regular_single_link(descriptor)
            os.link(
                temporary,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError as exc:
            raise ManagedStorageError("MANAGED_STORAGE_ALREADY_EXISTS") from exc
        except ManagedStorageError:
            raise
        except OSError as exc:
            raise ManagedStorageError("MANAGED_STORAGE_WRITE_FAILED") from exc
        finally:
            os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


@contextmanager
def _open_parent(root: Path, locator: str, *, create: bool) -> Iterator[tuple[int, str]]:
    """固定根和每一级目录inode，拒绝中间symlink并返回最终父目录fd。"""

    parts = _locator_parts(locator)
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        current_fd = os.open(root, root_flags)
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_ROOT_INVALID") from exc
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise ManagedStorageError("MANAGED_STORAGE_DIRECTORY_INVALID") from exc
            os.close(current_fd)
            current_fd = next_fd
        yield current_fd, parts[-1]
    finally:
        os.close(current_fd)


def _open_leaf(parent_fd: int, leaf: str, flags: int) -> int:
    """在固定父目录下拒绝跟随最终symlink并打开文件。"""

    try:
        return os.open(leaf, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_FILE_INVALID") from exc


def _assert_regular_single_link(descriptor: int) -> os.stat_result:
    """只允许普通且链接计数为一的inode，拒绝hardlink、设备和目录。"""

    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ManagedStorageError("MANAGED_STORAGE_INODE_INVALID")
    return info


def _locator_parts(locator: str) -> tuple[str, ...]:
    """把数据库locator规范为非绝对、无点段的POSIX组件。"""

    value = PurePosixPath(locator)
    parts = value.parts
    if not parts or value.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ManagedStorageError("MANAGED_STORAGE_LOCATOR_INVALID")
    if any("/" in part or "\\" in part or "\x00" in part for part in parts):
        raise ManagedStorageError("MANAGED_STORAGE_LOCATOR_INVALID")
    return parts


def _supports_secure_dir_fd() -> bool:
    """仅在平台完整支持open/unlink/rename/link的dir_fd语义时启用强边界。"""

    return _SECURE_DIR_FD_AVAILABLE


def _fallback_path(root: Path, locator: str) -> Path:
    """为无dir_fd平台提供防路径逃逸的保守兼容校验。"""

    candidate = root / Path(*_locator_parts(locator))
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=True)
    if resolved_root not in resolved.parents or resolved.is_symlink():
        raise ManagedStorageError("MANAGED_STORAGE_FILE_INVALID")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ManagedStorageError("MANAGED_STORAGE_INODE_INVALID")
    return resolved


def _fallback_read(root: Path, locator: str) -> bytes:
    """读取无dir_fd平台上已完成保守校验的普通文件。"""

    try:
        return _fallback_path(root, locator).read_bytes()
    except ManagedStorageError:
        raise
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_READ_FAILED") from exc


def _fallback_write(root: Path, locator: str, copy: Callable[[BinaryIO], None]) -> None:
    """在无dir_fd平台以独占文件写入，明确不宣称抵抗目录替换竞态。"""

    parts = _locator_parts(locator)
    parent = root / Path(*parts[:-1])
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = parent / parts[-1]
    try:
        with target.open("xb") as stream:
            copy(stream)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_WRITE_FAILED") from exc


def _fallback_unlink(root: Path, locator: str, *, missing_ok: bool) -> None:
    """删除无dir_fd平台上已完成保守校验的普通文件。"""

    try:
        _fallback_path(root, locator).unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise ManagedStorageError("MANAGED_STORAGE_NOT_FOUND") from None
    except ManagedStorageError:
        raise
    except OSError as exc:
        raise ManagedStorageError("MANAGED_STORAGE_DELETE_FAILED") from exc
