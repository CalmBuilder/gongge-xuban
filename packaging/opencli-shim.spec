# PyInstaller injects Analysis/PYZ/EXE globals into this spec-file namespace.
# ruff: noqa: F821
"""
@Time       : 2026/09/04 12:45
@Author     : zhanglp8181
@File       : opencli-shim.spec
@CallChain  : build_windows.ps1 → PyInstaller → runtime/opencli/opencli.exe
@Description: 把 npm 形态的 OpenCLI 固定包装为安装目录内的 Windows 可执行入口。
"""

from pathlib import Path


BACKEND = Path.cwd()

a = Analysis(
    [str(BACKEND / "opencli_launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name="opencli",
    console=True,
    disable_windowed_traceback=False,
)
