"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : test_packaging_brand.py
@CallChain  : pytest → packaging 脚本/资源静态契约 → 跨平台发布入口
@Description: 验证打包身份、资源命名、Windows 单文件产物路径和原生产物启动门禁。
"""

import ast
import importlib.util
import re
import subprocess
from pathlib import Path, PureWindowsPath

import pytest


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
        "browser-relay-connector.stdout.log",
        "browser-relay-connector.stderr.log",
        "Start-Process -FilePath $relayConnector",
        "Full output:",
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
    assert "EXTENSION_SUFFIXES" in spec
    assert 'search_patterns=["*.pyd", "*.dll", "*.dylib", "*.so"]' in spec
    assert "pydantic_core_binaries" in spec
    assert (PACKAGING / "runtime_hooks/pyi_rth_pydantic_core.py").is_file()
    assert "pyi_rth_pydantic_core.py" in spec
    assert "Assert-BundledPydanticCore" in build_script
    assert "_pydantic_core*.pyd" in build_script
    assert "Assert-InstalledPydanticCore" in smoke_script
    assert "Directory.Name -ieq \"pydantic_core\"" in build_script
    assert "Directory.Name -ieq \"pydantic_core\"" in smoke_script
    assert "Get-PythonAbiToken" in build_script
    assert "Assert-PydanticCoreBuildDependency" in build_script
    assert "packaging\\.windows-build-venv" in build_script
    assert "-m PyInstaller" in build_script
    assert "Pydantic core ABI mismatch" in build_script
    assert "packaging\\build" in build_script
    assert "multiple pydantic_core native extensions" in build_script
    assert "_internal\\pydantic_core" in build_script
    assert "Inno Setup 7" in build_script
    assert "ISCC.exe was not found" in build_script
    assert "ExpectedPydanticCoreAbi" in build_script
    assert "ExpectedPydanticCoreAbi" in smoke_script
    assert "multiple pydantic_core native extensions" in smoke_script
    assert "_internal\\pydantic_core" in smoke_script


def test_frozen_package_carries_builtin_expert_fixture() -> None:
    """验证 frozen 版会携带启动时生成内置专家所需的固定中文包。"""

    fixture = ROOT / "backend/app/experts/data/agency_agents_builtin_v2.json"
    spec = (PACKAGING / "gongge-xuban.spec").read_text(encoding="utf-8")

    assert fixture.is_file()
    assert fixture.stat().st_size > 1_000_000
    assert "BUILTIN_EXPERT_FIXTURE" in spec
    assert '"app/experts/data"' in spec


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


def test_desktop_package_carries_youtube_runtime_dependencies() -> None:
    """桌面包必须同时携带 yt-dlp-ejs 数据和 Node.js，避免公开视频能力依赖开发机。"""

    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    spec = (PACKAGING / "gongge-xuban.spec").read_text(encoding="utf-8")
    fetch_script = (PACKAGING / "fetch_youtube_js_runtime.py").read_text(encoding="utf-8")
    build_linux = (PACKAGING / "build_linux.sh").read_text(encoding="utf-8")
    build_macos = (PACKAGING / "build_macos.sh").read_text(encoding="utf-8")
    build_windows = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    smoke_windows = (PACKAGING / "smoke_windows.ps1").read_text(encoding="utf-8")

    assert '"yt-dlp[default]>=2026.07.04"' in pyproject
    assert 'collect_submodules("yt_dlp")' in spec
    assert 'collect_submodules("yt_dlp_ejs")' in spec
    assert 'collect_data_files("yt_dlp")' in spec
    assert 'collect_data_files("yt_dlp_ejs")' in spec
    assert "SHASUMS256.txt" in fetch_script
    assert "node-v{version}-win-x64.zip" in fetch_script
    assert "runtime/youtube-js/node" in build_linux
    assert "runtime/youtube-js/node" in build_macos
    assert "runtime\\youtube-js\\node.exe" in build_windows
    assert "runtime\\youtube-js\\node.exe" in smoke_windows
    assert "Bundled YouTube Node.js runtime probe" in smoke_windows


def test_windows_package_carries_browser_relay_and_fixed_opencli_runtime() -> None:
    """Windows 安装包必须携带 Relay 连接器和安装目录内 OpenCLI，不依赖用户全局 PATH。"""

    build_windows = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    smoke_windows = (PACKAGING / "smoke_windows.ps1").read_text(encoding="utf-8")
    installer = (PACKAGING / "installer/gongge-xuban.iss").read_text(encoding="utf-8")
    fetch_opencli = (PACKAGING / "fetch_opencli_runtime.py").read_text(encoding="utf-8")
    platform_runtime = (ROOT / "backend/app/vertical_abilities/platform_runtime.py").read_text(encoding="utf-8")
    launcher = (ROOT / "backend/opencli_launcher.py").read_text(encoding="utf-8")

    for name in (
        "browser-relay-connector.spec",
        "opencli-shim.spec",
        "fetch_opencli_runtime.py",
        "assets/opencli-wrapper.cmd",
    ):
        assert (PACKAGING / name).is_file(), name
    for marker in (
        "browser-relay-connector.spec",
        "browser-relay-connector.exe",
        "opencli-shim.spec",
        "runtime\\opencli\\opencli.exe",
        "runtime\\opencli\\node.exe",
        "fetch_opencli_runtime.py",
        "OPENCLI_VERSION",
    ):
        assert marker in build_windows
    for marker in (
        "browser-relay-connector.exe",
        "runtime\\opencli\\opencli.exe",
        "runtime\\opencli\\node.exe",
        "node_modules\\@jackwener\\opencli\\dist\\src\\main.js",
        "Bundled browser Relay connector help probe",
    ):
        assert marker in smoke_windows
    assert 'Source: "..\\out\\gongge-xuban\\*"' in installer
    assert '"--prefix"' in fetch_opencli
    assert '"--ignore-scripts"' in fetch_opencli
    assert 'collect_submodules("yaml")' in (PACKAGING / "browser-relay-connector.spec").read_text(
        encoding="utf-8"
    )
    assert '"runtime" / "opencli"' in platform_runtime
    assert "shutil.which(name)" in platform_runtime
    assert "node.exe" in launcher and "node_modules" in launcher


def test_opencli_fetch_resolves_windows_npm_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCLI 构建必须把 Windows 的 npm.cmd 解析为可执行启动器。"""

    script_path = PACKAGING / "fetch_opencli_runtime.py"
    spec = importlib.util.spec_from_file_location("fetch_opencli_runtime", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("NPM_COMMAND", raising=False)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: r"C:\Program Files\nodejs\npm.cmd" if name == "npm.cmd" else None,
    )

    assert module._resolve_npm_command(is_windows=True).endswith(r"nodejs\npm.cmd")


def test_opencli_fetch_falls_back_when_npm_override_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """错误拼接的 NPM_COMMAND 不得阻断构建，应回退到 PATH 中的 npm.cmd。"""

    script_path = PACKAGING / "fetch_opencli_runtime.py"
    spec = importlib.util.spec_from_file_location("fetch_opencli_runtime_fallback", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv(
        "NPM_COMMAND",
        r"D:\Program Files\nodejs\npm.cmd C:\Users\Administrator\AppData\Roaming\npm\npm.cmd",
    )
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: r"C:\Program Files\nodejs\npm.cmd" if name == "npm.cmd" else None,
    )

    assert module._resolve_npm_command(is_windows=True).endswith(r"nodejs\npm.cmd")


@pytest.mark.parametrize("spec_name,variable", [
    ("browser-relay-connector.spec", "relayConnector"),
    ("opencli-shim.spec", "opencliShim"),
])
def test_windows_onefile_lookup_matches_spec_output(spec_name: str, variable: str) -> None:
    """根据 EXE 名称核对单文件实际输出，不执行打包且不能用 spec 文件名猜子目录。"""

    tree = ast.parse((PACKAGING / spec_name).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    exe_calls = [node for node in calls if node.func.id == "EXE"]
    assert len(exe_calls) == 1
    assert not any(node.func.id == "COLLECT" for node in calls)
    options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in exe_calls[0].keywords}
    # PyInstaller EXE 默认 exclude_binaries=False，输出为 DISTPATH/name，而不是 spec/name。
    assert options.get("exclude_binaries", False) is False
    name = options["name"]
    assert isinstance(name, str)
    filename = name if name.casefold().endswith(".exe") else name + ".exe"
    expected = PureWindowsPath("packaging", "out", filename)
    build_script = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    invocation = next(line for line in build_script.splitlines() if f"\\{spec_name} " in line)
    assert "--distpath ..\\packaging\\out" in invocation
    match = re.search(r"^\$" + re.escape(variable) + r' = "([^"]+)"', build_script, re.MULTILINE)
    assert match is not None
    assert PureWindowsPath(match.group(1)) == expected
