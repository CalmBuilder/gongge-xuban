"""Small cross-platform process helpers used by development scripts."""

from __future__ import annotations

import os
import sys


def pid_alive(pid: int, platform: str | None = None) -> bool:
    """Return whether a process exists without sending it a signal on Windows."""
    platform = platform or sys.platform
    if platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        queried = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return bool(queried) and exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
