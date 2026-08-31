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
    """构建入口必须输出当前产品的跨平台发布产物。"""
    contents = "\n".join(
        (PACKAGING / name).read_text(encoding="utf-8")
        for name in ("build_linux.sh", "build_macos.sh", "build_windows.ps1")
    )
    assert "Gongge-Xuban-linux-x86_64" in contents
    assert "Gongge-Xuban-macos-" in contents
    assert "Gongge-Xuban-windows-x64-setup.exe" in contents


def test_windows_build_has_native_installation_smoke_gate() -> None:
    """Windows 构建必须在原生机安装、启动、检查 runtime 后再允许收口。"""

    build_script = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    smoke_script = (PACKAGING / "smoke_windows.ps1").read_text(encoding="utf-8")

    assert (PACKAGING / "smoke_windows.ps1").is_file()
    assert "smoke_windows.ps1" in build_script
    assert "Windows native smoke test" in build_script
    assert "npm ci --prefix frontend-enterprise --no-audit --no-fund" in build_script
    assert "if (-not (Test-Path frontend-enterprise\\node_modules))" not in build_script
    for marker in (
        "Assert-WindowsX64Host",
        "Assert-PythonX64",
        "Assert-PeX64",
        "Assert-BundlePeX64",
        "Test-NonRuntimeExecutableTemplate",
        "PROCESSOR_ARCHITECTURE",
        "--clean",
        "Stop-ExistingProductProcesses",
        "Clear-ApplicationBuildOutput",
        "0x8664",
    ):
        assert marker in build_script
    for marker in (
        "Invoke-Installer",
        "Invoke-Uninstaller",
        "runtime\\python.exe",
        "/api/health",
        'product_id -eq "gongge-xuban"',
        "GONGGE_XUBAN_DOTENV",
        "PUBLIC_MOCK_API_KEY",
        "WINDOWS_NATIVE_SMOKE_PASS",
    ):
        assert marker in smoke_script


def test_windows_packaging_collects_pydantic_core_extension() -> None:
    """验证 Windows 冻结包显式携带 Pydantic v2 的原生核心扩展。"""

    spec = (PACKAGING / "gongge-xuban.spec").read_text(encoding="utf-8")
    build_script = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    smoke_script = (PACKAGING / "smoke_windows.ps1").read_text(encoding="utf-8")

    assert "collect_dynamic_libs" in spec
    assert 'collect_submodules("pydantic_core")' in spec
    assert '"pydantic_core._pydantic_core"' in spec
    assert 'destdir="pydantic_core"' in spec
    assert 'search_patterns=["*.pyd", "*.dll", "*.dylib", "*.so"]' in spec
    assert "pydantic_core_binaries" in spec
    assert (PACKAGING / "runtime_hooks/pyi_rth_pydantic_core.py").is_file()
    assert "pyi_rth_pydantic_core.py" in spec
    assert "Assert-BundledPydanticCore" in build_script
    assert "_pydantic_core*.pyd" in build_script
    assert "Assert-InstalledPydanticCore" in smoke_script
    assert "Directory.Name -ieq \"pydantic_core\"" in build_script
    assert "Directory.Name -ieq \"pydantic_core\"" in smoke_script


def test_macos_build_aligns_dependencies_and_smoke_tests_signed_app() -> None:
    """验证 macOS 构建不会复用失配依赖，并在制 DMG 前启动签名后的应用。"""

    build_script = (PACKAGING / "build_macos.sh").read_text(encoding="utf-8")
    smoke_script = (PACKAGING / "smoke_macos_app.sh").read_text(encoding="utf-8")
    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")

    assert '"cryptography>=42.0.0,<49.0.0"' in pyproject
    assert '"pillow>=10.4.0,<13.0.0"' in pyproject
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


def test_bundled_runtime_preloads_attachment_parser_dependencies() -> None:
    """随包Python必须包含PDF、Office与图片parser依赖，禁止开发态通过而安装包失效。"""

    fetch_script = (PACKAGING / "fetch_runtime_python.py").read_text(encoding="utf-8")
    linux_script = (PACKAGING / "build_linux.sh").read_text(encoding="utf-8")

    for package in ("python-docx", "openpyxl", "pypdf", "pillow"):
        assert f'"{package}"' in fetch_script
    assert "import ssl, requests, docx, openpyxl, pypdf, PIL" in fetch_script
    assert 'import requests, docx, openpyxl, pypdf, PIL' in linux_script
    assert "libsframe1" in linux_script
