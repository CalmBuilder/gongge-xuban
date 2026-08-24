"""
@Time       : 2026/08/14 18:36
@Author     : zhanglp8181
@File       : test_managed_storage.py
@CallChain  : pytest攻击线程 → managed_storage → POSIX dir_fd文件系统
@Description: 用真实symlink、hardlink和目录替换竞态验证受管输入与Artifact共用的文件边界。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.security import managed_storage
from app.security.managed_storage import (
    ManagedStorageError,
    managed_read_bytes,
    managed_unlink,
    managed_write_bytes,
)


pytestmark = pytest.mark.skipif(
    not managed_storage._supports_secure_dir_fd(),
    reason="攻击竞态需要POSIX dir_fd与O_NOFOLLOW",
)


def test_managed_storage_rejects_leaf_symlink_and_hardlink(tmp_path: Path) -> None:
    """读取和删除必须拒绝指向外部的symlink及拥有第二目录项的hardlink。"""

    root = tmp_path / "managed"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-secret")
    symlink_locator = "tenant/resource/symlink"
    symlink_path = root / symlink_locator
    symlink_path.parent.mkdir(parents=True)
    symlink_path.symlink_to(outside)

    with pytest.raises(ManagedStorageError):
        managed_read_bytes(root, symlink_locator)
    with pytest.raises(ManagedStorageError):
        managed_unlink(root, symlink_locator)
    assert outside.read_bytes() == b"outside-secret"

    hardlink_locator = "tenant/resource/blob"
    managed_write_bytes(root, hardlink_locator, b"managed-secret")
    hardlink = tmp_path / "stolen-hardlink"
    os.link(root / hardlink_locator, hardlink)

    with pytest.raises(ManagedStorageError, match="INODE_INVALID"):
        managed_read_bytes(root, hardlink_locator)
    with pytest.raises(ManagedStorageError, match="INODE_INVALID"):
        managed_unlink(root, hardlink_locator)
    assert hardlink.read_bytes() == b"managed-secret"


def test_managed_storage_read_holds_parent_inode_during_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """攻击线程替换父目录时，读取只能得到打开目录fd对应的原始blob。"""

    root = tmp_path / "managed"
    locator = "tenant/resource/blob"
    managed_write_bytes(root, locator, b"trusted-content")
    parent = root / "tenant" / "resource"
    moved_parent = root / "tenant" / "resource-old"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "blob").write_bytes(b"attacker-content")
    reached_leaf = threading.Event()
    continue_open = threading.Event()
    real_open = os.open

    def blocked_open(path, flags, mode=0o777, *, dir_fd=None):
        """在最终openat前建立可重复的目录替换窗口。"""

        if path == "blob" and dir_fd is not None and not reached_leaf.is_set():
            reached_leaf.set()
            assert continue_open.wait(5)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_storage.os, "open", blocked_open)
    outcome: dict[str, object] = {}

    def read() -> None:
        """在受攻击窗口执行一次受管读取。"""

        try:
            outcome["data"] = managed_read_bytes(root, locator)
        except Exception as exc:  # noqa: BLE001 - 测试需要把线程异常带回主线程。
            outcome["error"] = exc

    thread = threading.Thread(target=read)
    thread.start()
    assert reached_leaf.wait(5)
    parent.rename(moved_parent)
    parent.symlink_to(outside, target_is_directory=True)
    continue_open.set()
    thread.join(5)

    assert not thread.is_alive()
    assert outcome == {"data": b"trusted-content"}


def test_managed_storage_unlink_is_bound_to_original_parent_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除线性化前替换目录时，只能移除原父目录中的目标，不能删除外部替代文件。"""

    root = tmp_path / "managed"
    locator = "tenant/resource/blob"
    managed_write_bytes(root, locator, b"managed-content")
    parent = root / "tenant" / "resource"
    moved_parent = root / "tenant" / "resource-old"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_blob = outside / "blob"
    outside_blob.write_bytes(b"outside-content")
    reached_rename = threading.Event()
    continue_rename = threading.Event()
    real_rename = os.rename

    def blocked_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        """在最终renameat前建立可重复的目录替换窗口。"""

        if src == "blob" and src_dir_fd is not None and not reached_rename.is_set():
            reached_rename.set()
            assert continue_rename.wait(5)
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(managed_storage.os, "rename", blocked_rename)
    outcome: dict[str, object] = {}

    def remove() -> None:
        """在线程中执行受管删除并回传异常。"""

        try:
            managed_unlink(root, locator)
            outcome["removed"] = True
        except Exception as exc:  # noqa: BLE001 - 测试需要把线程异常带回主线程。
            outcome["error"] = exc

    thread = threading.Thread(target=remove)
    thread.start()
    assert reached_rename.wait(5)
    parent.rename(moved_parent)
    parent.symlink_to(outside, target_is_directory=True)
    continue_rename.set()
    thread.join(5)

    assert not thread.is_alive()
    assert outcome == {"removed": True}
    assert not (moved_parent / "blob").exists()
    assert outside_blob.read_bytes() == b"outside-content"


def test_managed_storage_write_rejects_symlinked_directory(tmp_path: Path) -> None:
    """发布新blob时任一级受管目录被替换为symlink都必须fail closed。"""

    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "tenant").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedStorageError, match="DIRECTORY_INVALID"):
        managed_write_bytes(root, "tenant/resource/blob", b"must-not-escape")

    assert list(outside.iterdir()) == []
