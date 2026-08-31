# PyInstaller injects Analysis/PYZ/EXE/COLLECT/BUNDLE into spec-file globals.
# ruff: noqa: F821
"""
@Time       : 2026/08/31
@Author     : zhanglp8181
@File       : gongge-xuban.spec
@CallChain  : build_windows.ps1/build_linux.sh/build_macos.sh → PyInstaller → 冻结版桌面应用
@Description: 定义跨平台桌面冻结包的入口、资源、动态依赖和应用元数据。
"""

# 运行：cd backend && pyinstaller ../packaging/gongge-xuban.spec --noconfirm
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

BACKEND = Path.cwd()                      # 约定在 backend/ 下执行
REPO = BACKEND.parent
DIST = REPO / "frontend-enterprise" / "dist"
ASSETS = REPO / "packaging" / "assets"
ICNS = ASSETS / "gongge-xuban.icns"
ICO = ASSETS / "gongge-xuban.ico"
assert DIST.exists(), "先构建前端：npm --prefix frontend-enterprise run build"

RAW_VERSION = os.environ.get("VERSION", "0.1.0").strip() or "0.1.0"
BUNDLE_VERSION = RAW_VERSION[1:] if RAW_VERSION.startswith("v") else RAW_VERSION

# 平台图标：macOS 用 .icns，Windows 用 .ico，Linux(EXE) 不用
_exe_icon = None
if sys.platform == "win32" and ICO.exists():
    _exe_icon = str(ICO)

datas = [
    (str(DIST), "frontend-enterprise/dist"),
    (str(ASSETS / "gongge-xuban.png"), "packaging/assets"),
    (str(BACKEND / "app" / "llm" / "prompts"), "app/llm/prompts"),
    (str(BACKEND / "app" / "db" / "seed_fixtures"), "app/db/seed_fixtures"),
    (str(BACKEND / "mock_servers"), "mock_servers"),
] + collect_data_files("tzdata")

# pydantic-core 以 Rust 原生扩展提供 Pydantic v2 的核心实现。不同版本的
# wheel 文件名和 Python ABI 会变化，不能只依赖 PyInstaller 对 pydantic 的
# 静态导入推断；显式收集模块和各平台原生扩展，保证 Windows 的 .pyd、macOS
# 的 .dylib/.so 以及 Linux 的 .so 都进入冻结包。
pydantic_core_binaries = collect_dynamic_libs(
    "pydantic_core",
    search_patterns=["*.pyd", "*.dll", "*.dylib", "*.so"],
)

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("sqlmodel")
    + collect_submodules("app")
    + collect_submodules("pydantic_core")
    + [
        # 顶层单文件模块：uvicorn 用字符串 "single_port_app:app" 运行时动态 import
        "single_port_app",
        "cryptography", "certifi", "python_multipart", "docx", "pypdf", "PIL", "PIL.Image", "bs4", "openai",
        # 动态导入补充：pydantic/starlette/anyio 等
        "pydantic", "pydantic_settings", "pydantic.deprecated.decorator",
        "starlette", "anyio", "email_validator", "sqlalchemy",
    ]
)

# macOS：Dock/菜单栏壳需要 pyobjc（AppKit + PyObjCTools）
if sys.platform == "darwin":
    hiddenimports = hiddenimports + collect_submodules("objc") + [
        "AppKit", "Foundation", "PyObjCTools", "PyObjCTools.AppHelper",
    ]

a = Analysis(
    [str(BACKEND / "desktop_launcher.py")],
    pathex=[str(BACKEND)],
    binaries=pydantic_core_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# console=False：作为 GUI app 常驻 Dock（console=True 会加 LSBackgroundOnly 变纯后台不进 Dock）。
# 日志由 launcher 重定向到用户数据目录，启动问题可查文件。
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="gongge-xuban",
          console=False, disable_windowed_traceback=False, icon=_exe_icon)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="gongge-xuban")

# macOS：额外产出标准 .app bundle（PyInstaller 正确处理 Contents/Frameworks 布局）。
# 附带 python runtime 由 build 脚本在打包后拷进 .app/Contents/MacOS/runtime。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Gongge-Xuban.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="cn.gongge.xuban.desktop",
        info_plist={
            "CFBundleName": "共格·序伴",
            "CFBundleDisplayName": "共格·序伴",
            # 可执行名保持 gongge-xuban（COLLECT/EXE 名 + build 脚本按此路径拷 runtime）
            "CFBundleExecutable": "gongge-xuban",
            "CFBundleShortVersionString": BUNDLE_VERSION,
            "CFBundleVersion": BUNDLE_VERSION,
            "CFBundleURLTypes": [
                {
                    "CFBundleURLName": "Gongge-Xuban URL",
                    "CFBundleURLSchemes": ["gongge-xuban"],
                },
            ],
            "NSHighResolutionCapable": True,
            # 显式声明为常规 GUI app：进 Dock、可激活（非后台/非 agent）
            "LSBackgroundOnly": False,
            "LSUIElement": False,
        },
    )
