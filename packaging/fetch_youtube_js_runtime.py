"""
@Time       : 2026/09/03 14:00
@Author     : zhanglp8181
@File       : fetch_youtube_js_runtime.py
@CallChain  : 跨平台构建脚本 → Node.js 官方发行包 → 桌面应用 runtime/youtube-js
@Description: 下载并校验 YouTube 所需的跨平台 Node.js 可执行文件，供 yt-dlp-ejs 使用。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile


DEFAULT_NODE_VERSION = "22.14.0"
NODE_DOWNLOAD_BASE = "https://nodejs.org/dist/v{version}"
_SHA256_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})\s+[* ](?P<name>\S+)$")
_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def main(argv: list[str] | None = None) -> int:
    """解析构建参数并把经过 SHA-256 校验的 Node.js 写入明确目标文件。"""

    parser = argparse.ArgumentParser(description="为 YouTube yt-dlp Runtime 准备 Node.js")
    parser.add_argument("output", type=Path, help="输出的 node 或 node.exe 路径")
    parser.add_argument("--version", default=DEFAULT_NODE_VERSION, help="Node.js 发行版本")
    args = parser.parse_args(argv)
    asset = _platform_asset(args.version)
    _fetch_verified_node(args.output, args.version, asset)
    print(f"YouTube JS Runtime ready at {args.output} (Node.js {args.version})")
    return 0


def _platform_asset(version: str) -> tuple[str, str, str]:
    """按当前构建机系统和架构选择 Node.js 官方压缩包与内部文件。"""

    system = platform.system()
    arch = _ARCH_ALIASES.get((platform.machine() or "").casefold(), "")
    targets = {
        ("Linux", "x86_64"): (f"node-v{version}-linux-x64.tar.xz", "tar", "bin/node"),
        ("Darwin", "x86_64"): (f"node-v{version}-darwin-x64.tar.gz", "tar", "bin/node"),
        ("Darwin", "arm64"): (f"node-v{version}-darwin-arm64.tar.gz", "tar", "bin/node"),
        ("Windows", "x86_64"): (f"node-v{version}-win-x64.zip", "zip", "node.exe"),
    }
    try:
        return targets[(system, arch)]
    except KeyError as exc:
        raise RuntimeError(f"不支持的 YouTube JS Runtime 构建平台：{system}/{arch or platform.machine()}") from exc


def _fetch_verified_node(output: Path, version: str, asset: tuple[str, str, str]) -> None:
    """下载发行包和官方校验清单，只提取 Node 可执行文件到目标路径。"""

    asset_name, archive_kind, member_name = asset
    base_url = NODE_DOWNLOAD_BASE.format(version=version)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.is_dir():
        raise RuntimeError(f"Node.js 输出路径不能是目录：{output}")
    with tempfile.TemporaryDirectory(prefix="gongge-youtube-node-") as temp_name:
        temp_dir = Path(temp_name)
        archive = temp_dir / asset_name
        manifest = temp_dir / "SHASUMS256.txt"
        _download(f"{base_url}/{asset_name}", archive)
        _download(f"{base_url}/SHASUMS256.txt", manifest)
        expected = _manifest_digest(manifest, asset_name)
        actual = _sha256(archive)
        if actual.casefold() != expected.casefold():
            raise RuntimeError(f"Node.js 官方压缩包 SHA-256 校验失败：{asset_name}")
        temporary_output = temp_dir / output.name
        if archive_kind == "tar":
            _extract_tar_member(archive, member_name, temporary_output)
        else:
            _extract_zip_member(archive, member_name, temporary_output)
        if os.name != "nt":
            temporary_output.chmod(0o755)
        os.replace(temporary_output, output)


def _download(url: str, destination: Path) -> None:
    """以固定超时下载单个构建输入，不把响应正文打印到日志。"""

    request = urllib.request.Request(url, headers={"User-Agent": "gongge-xuban-build/1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as target:
        shutil.copyfileobj(response, target)


def _manifest_digest(manifest: Path, asset_name: str) -> str:
    """从 Node.js 官方 SHA-256 清单中读取指定资产的唯一摘要。"""

    matches: list[str] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = _SHA256_LINE.match(line.strip())
        if match and match.group("name") == asset_name:
            matches.append(match.group("digest"))
    if len(matches) != 1:
        raise RuntimeError(f"Node.js 官方校验清单缺少唯一资产：{asset_name}")
    return matches[0]


def _sha256(path: Path) -> str:
    """分块计算构建压缩包摘要，避免一次性把发行包读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_tar_member(archive: Path, member_name: str, output: Path) -> None:
    """从 tar 发行包提取指定普通文件，拒绝目录、链接和路径穿越成员。"""

    with tarfile.open(archive) as source:
        member = next(
            (
                item
                for item in source.getmembers()
                if PurePosixPath(item.name).as_posix().endswith(f"/{member_name}")
                and item.isfile()
            ),
            None,
        )
        if member is None:
            raise RuntimeError(f"Node.js 发行包缺少可执行文件：{member_name}")
        extracted = source.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"Node.js 发行包文件不可读取：{member_name}")
        with extracted, output.open("wb") as target:
            shutil.copyfileobj(extracted, target)


def _extract_zip_member(archive: Path, member_name: str, output: Path) -> None:
    """从 Windows zip 发行包提取指定普通文件，拒绝目录和链接成员。"""

    with zipfile.ZipFile(archive) as source:
        member = next(
            (
                item
                for item in source.infolist()
                if PurePosixPath(item.filename).name.casefold() == member_name.casefold()
                and not item.is_dir()
                and (item.external_attr >> 16) & 0o170000 != 0o120000
            ),
            None,
        )
        if member is None:
            raise RuntimeError(f"Node.js 发行包缺少可执行文件：{member_name}")
        with source.open(member) as extracted, output.open("wb") as target:
            shutil.copyfileobj(extracted, target)


if __name__ == "__main__":
    raise SystemExit(main())
