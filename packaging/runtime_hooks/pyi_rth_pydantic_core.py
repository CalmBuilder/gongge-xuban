"""
@Time       : 2026/08/31
@Author     : zhanglp8181
@File       : pyi_rth_pydantic_core.py
@CallChain  : PyInstaller 启动钩子 → 冻结版 importlib → pydantic_core._pydantic_core
@Description: 在冻结版中按完整模块名预加载 Pydantic v2 的 Rust 原生扩展，规避扩展文件已落盘但包路径未被解释器解析的问题。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path


_MODULE_NAME = "pydantic_core._pydantic_core"
_NATIVE_PATTERNS = ("_pydantic_core*.pyd", "_pydantic_core*.so", "_pydantic_core*.dylib")


def _load_frozen_pydantic_core() -> None:
    """在冻结包启动阶段按 CPython 完整模块名加载 Pydantic 原生扩展。"""

    if not getattr(sys, "frozen", False) or _MODULE_NAME in sys.modules:
        return

    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_root = Path(meipass)
        roots.extend((meipass_root, meipass_root / "_internal"))
    executable = getattr(sys, "executable", None)
    if executable:
        executable_root = Path(executable).resolve().parent
        roots.extend((executable_root, executable_root / "_internal"))

    package_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for root in roots:
        package_dir = root / "pydantic_core"
        if package_dir in seen_dirs or not package_dir.is_dir():
            continue
        seen_dirs.add(package_dir)
        package_dirs.append(package_dir)

    candidates: list[Path] = []
    for package_dir in package_dirs:
        for pattern in _NATIVE_PATTERNS:
            candidates.extend(sorted(package_dir.glob(pattern)))

    if not candidates:
        # 让常规 import 继续给出标准错误；构建门禁会在发布前阻止这种包进入产物。
        return

    extension_suffixes = tuple(
        suffix.casefold()
        for suffix in importlib.machinery.EXTENSION_SUFFIXES
        if suffix.casefold() not in {".pyd", ".so", ".dylib"}
    )
    compatible_candidates = [
        candidate
        for candidate in candidates
        if any(candidate.name.casefold().endswith(suffix) for suffix in extension_suffixes)
    ]
    if not compatible_candidates:
        expected = ", ".join(extension_suffixes) or sys.implementation.cache_tag
        found = ", ".join(str(candidate) for candidate in candidates)
        raise ImportError(
            "冻结版 Pydantic 原生扩展与当前 Python ABI 不匹配；"
            f"期望后缀：{expected}；实际文件：{found}"
        )

    native_path = sorted(compatible_candidates)[0]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, native_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为冻结版 Pydantic 原生扩展创建加载器：{native_path}")

    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_MODULE_NAME)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError) as exc:
        if previous is None:
            sys.modules.pop(_MODULE_NAME, None)
        else:
            sys.modules[_MODULE_NAME] = previous
        raise ImportError(f"无法加载冻结版 Pydantic 原生扩展 {native_path}: {exc}") from exc


_load_frozen_pydantic_core()
