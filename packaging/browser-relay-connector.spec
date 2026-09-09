# PyInstaller injects Analysis/PYZ/EXE globals into this spec-file namespace.
# ruff: noqa: F821
"""
@Time       : 2026/09/04 12:30
@Author     : zhanglp8181
@File       : browser-relay-connector.spec
@CallChain  : build_windows.ps1 → PyInstaller → browser-relay-connector.exe
@Description: 定义随 Windows 安装包分发的宿主机浏览器 Relay 连接器可执行文件。
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


BACKEND = Path.cwd()

a = Analysis(
    [str(BACKEND / "browser_relay_connector.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "app.vertical_abilities.browser_relay_connector",
        "app.vertical_abilities.platform_runtime",
        "app.vertical_abilities.browser_runtime",
        "app.cancellation",
    ] + collect_submodules("yaml"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="browser-relay-connector",
    console=True,
    disable_windowed_traceback=False,
)
