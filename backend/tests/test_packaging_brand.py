"""
@Time       : 2026/07/27 16:35
@Author     : zhanglp8181
@File       : test_packaging_brand.py
@CallChain  : pytest → packaging 脚本/资源静态契约 → 跨平台发布入口
@Description: 验证打包身份、资源命名和 macOS 原生产物启动门禁。
"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"


def test_packaging_uses_canonical_file_names() -> None:
    assert (PACKAGING / "gongge-xuban.spec").is_file()
    assert (PACKAGING / "installer/gongge-xuban.iss").is_file()
    foreign_slug = "".join(("ultra", "rag"))
    assert not (PACKAGING / f"{foreign_slug}.spec").exists()
    assert not (PACKAGING / f"installer/{foreign_slug}.iss").exists()


def test_packaging_has_new_cross_platform_icons() -> None:
    for name in (
        "gongge-xuban-mark.svg",
        "gongge-xuban.png",
        "gongge-xuban.ico",
        "gongge-xuban.icns",
    ):
        path = PACKAGING / "assets" / name
        assert path.is_file(), name
        assert path.stat().st_size > 100, name


def test_packaging_configs_use_new_application_identity() -> None:
    spec = (PACKAGING / "gongge-xuban.spec").read_text(encoding="utf-8")
    installer = (PACKAGING / "installer/gongge-xuban.iss").read_text(encoding="utf-8")

    assert 'name="gongge-xuban"' in spec
    assert 'bundle_identifier="cn.gongge.xuban.desktop"' in spec
    assert "AppName=共格·序伴" in installer
    assert "AppUserModelID: \"cn.gongge.xuban.desktop\"" in installer
    foreign_scheme = "".join(("staff", "deck"))
    assert foreign_scheme not in spec.casefold()
    assert foreign_scheme not in installer.casefold()


def test_build_scripts_emit_gongge_xuban_artifacts() -> None:
    contents = "\n".join(
        (PACKAGING / name).read_text(encoding="utf-8")
        for name in ("build_linux.sh", "build_macos.sh", "build_windows.ps1")
    )
    assert "Gongge-Xuban-linux-x86_64" in contents
    assert "Gongge-Xuban-macos-" in contents
    assert "Gongge-Xuban-windows-x64-setup.exe" in contents


def test_macos_build_aligns_dependencies_and_smoke_tests_signed_app() -> None:
    """验证 macOS 构建不会复用失配依赖，并在制 DMG 前启动签名后的应用。"""

    build_script = (PACKAGING / "build_macos.sh").read_text(encoding="utf-8")
    smoke_script = (PACKAGING / "smoke_macos_app.sh").read_text(encoding="utf-8")
    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")

    assert '"cryptography>=42.0.0,<49.0.0"' in pyproject
    assert 'pip install $DEPS "pyinstaller>=6.6.0"' in build_script
    assert 'bash packaging/smoke_macos_app.sh "$APP"' in build_script
    assert "GONGGE_XUBAN_HEADLESS=1" in smoke_script
    assert '"product_id"' in smoke_script
    assert '"gongge-xuban"' in smoke_script
    subprocess.run(
        ["bash", "-n", str(PACKAGING / "build_macos.sh")],
        check=True,
    )
    subprocess.run(
        ["bash", "-n", str(PACKAGING / "smoke_macos_app.sh")],
        check=True,
    )
