"""
@Time       : 2026/08/12 00:35
@Author     : zhanglp8181
@File       : object_store.py
@CallChain  : Skill ImportJob service → FileSystemSkillObjectStore → content-addressed filesystem
@Description: 隔离暂存对象与已确认内容对象，文件路径只由服务端 ID 和 checksum 组成。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.general_skills.package_security import NormalizedResource


OPAQUE_ID = re.compile(r"^[a-z][a-z0-9]*_[a-f0-9]{16,64}$")
CHECKSUM = re.compile(r"^[a-f0-9]{64}$")


class SkillObjectStoreError(RuntimeError):
    """表示内容对象不可用或服务端标识违反存储边界。"""


class FileSystemSkillObjectStore:
    """使用同文件系统原子替换实现本地/桌面模式的内容寻址对象存储。"""

    def __init__(self, root: str | Path) -> None:
        """固定并创建由部署配置提供的对象存储根目录。"""

        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def stage_resources(self, job_id: str, resources: tuple[NormalizedResource, ...]) -> None:
        """把规范资源按 checksum 原子写入不透明作业命名空间。"""

        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        for resource in resources:
            destination = job_dir / self._validated_checksum(resource.content_checksum)
            self._write_once(destination, resource.content)

    def stage_payload(self, job_id: str, payload: bytes, checksum: str) -> None:
        """按服务端 checksum 暂存原始包，使规范化可在进程重启后确定性重放。"""

        destination = self._job_dir(job_id) / self._validated_checksum(checksum)
        self._write_once(destination, payload)

    def promote(self, job_id: str, checksum: str) -> str:
        """把暂存内容幂等提升到全局内容寻址区并返回不透明 object key。"""

        normalized = self._validated_checksum(checksum)
        source = self._job_dir(job_id) / normalized
        if not source.is_file():
            raise SkillObjectStoreError("staged object is not available")
        destination = self.root / "objects" / normalized[:2] / normalized
        self._write_once(destination, source.read_bytes())
        return f"sha256:{normalized}"

    def read_staged(self, job_id: str, checksum: str) -> bytes:
        """按服务端作业 ID 和 checksum 读取暂存内容，不接受用户路径。"""

        source = self._job_dir(job_id) / self._validated_checksum(checksum)
        if not source.is_file():
            raise SkillObjectStoreError("staged object is not available")
        return source.read_bytes()

    def read_staged_or_object(self, job_id: str, checksum: str) -> bytes:
        """优先读取暂存内容，恢复 confirming 时回退到已提升的内容对象。"""

        normalized = self._validated_checksum(checksum)
        staged = self._job_dir(job_id) / normalized
        if staged.is_file():
            return staged.read_bytes()
        promoted = self.root / "objects" / normalized[:2] / normalized
        if promoted.is_file():
            return promoted.read_bytes()
        raise SkillObjectStoreError("skill object is not available")

    def release_staging(self, job_id: str) -> None:
        """逐文件移除已终止作业的暂存命名空间并回收配额。"""

        job_dir = self._job_dir(job_id)
        if not job_dir.exists():
            return
        for child in job_dir.iterdir():
            if child.is_file() and CHECKSUM.fullmatch(child.name):
                child.unlink()
        try:
            job_dir.rmdir()
        except OSError as exc:
            raise SkillObjectStoreError("staging directory contains unexpected entries") from exc

    def sweep_unreferenced_objects(
        self,
        referenced_checksums: set[str],
        *,
        older_than: datetime,
    ) -> list[str]:
        """清理提交失败遗留且超过宽限期的内容对象，保留所有修订仍引用的 checksum。"""

        normalized_references = {
            self._validated_checksum(checksum) for checksum in referenced_checksums
        }
        objects_root = self.root / "objects"
        if not objects_root.exists():
            return []
        removed: list[str] = []
        cutoff = (
            older_than.replace(tzinfo=timezone.utc).timestamp()
            if older_than.tzinfo is None
            else older_than.timestamp()
        )
        for prefix_dir in sorted(objects_root.iterdir()):
            if not prefix_dir.is_dir() or prefix_dir.is_symlink():
                continue
            for candidate in sorted(prefix_dir.iterdir()):
                if (
                    not candidate.is_file()
                    or candidate.is_symlink()
                    or not CHECKSUM.fullmatch(candidate.name)
                    or candidate.name in normalized_references
                    or candidate.stat().st_mtime > cutoff
                ):
                    continue
                candidate.unlink()
                removed.append(candidate.name)
            try:
                prefix_dir.rmdir()
            except OSError:
                pass
        return removed

    def _job_dir(self, job_id: str) -> Path:
        """把服务端生成的 opaque job ID 映射为固定 staging 子目录。"""

        if not OPAQUE_ID.fullmatch(job_id):
            raise SkillObjectStoreError("invalid import job identifier")
        return self.root / "staging" / job_id

    @staticmethod
    def _validated_checksum(checksum: str) -> str:
        """只允许规范 SHA-256 用作对象文件名。"""

        if not CHECKSUM.fullmatch(checksum):
            raise SkillObjectStoreError("invalid object checksum")
        return checksum

    @staticmethod
    def _write_once(destination: Path, payload: bytes) -> None:
        """原子创建内容对象；已存在对象必须逐字节一致。"""

        if destination.name != hashlib.sha256(payload).hexdigest():
            raise SkillObjectStoreError("content does not match object checksum")
        if destination.exists():
            if destination.read_bytes() != payload:
                raise SkillObjectStoreError("content-addressed object checksum collision")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
