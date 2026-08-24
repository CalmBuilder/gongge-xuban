"""
@Time       : 2026/08/14 09:35
@Author     : zhanglp8181
@File       : parser_sandbox.py
@CallChain  : input_parser_cli → parser sandbox → libseccomp
@Description: 在专用附件解析进程内安装Linux网络系统调用过滤器，避免fork后执行复杂初始化。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys


_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO_EPERM = 0x00050001


def install_network_seccomp_filter() -> None:
    """在Linux解析进程加载seccomp，机械拒绝新建网络socket和主动连接。"""

    if not sys.platform.startswith("linux"):
        return
    library_path = ctypes.util.find_library("seccomp")
    if not library_path:
        raise OSError("libseccomp is unavailable")
    library = ctypes.CDLL(library_path, use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise OSError("seccomp_init failed")
    try:
        for syscall_name in (b"socket", b"socketpair", b"connect"):
            syscall = library.seccomp_syscall_resolve_name(syscall_name)
            if syscall < 0 or library.seccomp_rule_add(
                context,
                _SCMP_ACT_ERRNO_EPERM,
                syscall,
                0,
            ) != 0:
                raise OSError(f"seccomp rule failed: {syscall_name.decode()}")
        if library.seccomp_load(context) != 0:
            raise OSError("seccomp_load failed")
    finally:
        library.seccomp_release(context)
