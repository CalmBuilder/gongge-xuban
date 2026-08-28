"""
@Time: 2026-08-10
@Author: zhanglp8181
@File: desktop_launcher.py
@CallChain: desktop executable -> desktop_launcher.main -> uvicorn single_port_app
@Description: 配置并启动桌面端单端口服务，负责端口选择、实例复用和浏览器唤起。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from app.brand import DESKTOP_APP_ID, PRODUCT_NAME, PRODUCT_SLUG, desktop_env_value, headless_enabled

APP_NAME = PRODUCT_NAME
APP_ID = DESKTOP_APP_ID
APP_VERSION = "0.1.0"
DEFAULT_PORT_RANGE_START = 5137
DEFAULT_PORT_RANGE_END = 5199
BROWSER_PAGE_TITLE = f"{APP_NAME}｜企业数字员工平台"
_MACOS_DELEGATE_REF = None
_MACOS_INSTANCE_LOCK_HANDLE = None
_WINDOWS_INSTANCE_MUTEX_HANDLE = None
_WINDOWS_BROWSER_OPENED = False
_WINDOWS_BROWSER_OPEN_LOCK = threading.Lock()
PRODUCT_ICON_PNG = ("packaging", "assets", "gongge-xuban.png")


def build_server_config() -> dict:
    host = desktop_env_value("HOST", "127.0.0.1")
    return {
        "app": "single_port_app:app",
        "host": host,
        "port": find_available_port(host),
    }


def _redirect_logs_when_frozen() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        from app.runtime_logging import configure_runtime_logging

        configure_runtime_logging()
    except Exception:
        pass


def apply_runtime_env(cfg: dict | None = None) -> None:
    # 时序契约：必须在任何 app.config 被 import 之前调用；仅 frozen 态断言，
    # 开发/测试进程通常已 import 过 app.config，无条件断言会误炸。
    if getattr(sys, "frozen", False):
        assert "app.config" not in sys.modules, "apply_runtime_env 必须在 import app.* 之前调用"

    cfg = cfg or build_server_config()
    origin = f"http://{cfg['host']}:{cfg['port']}"
    os.environ.setdefault("TOOL_BASE_URL", origin)
    existing_cors = os.environ.get("CORS_ORIGINS", "")
    if origin not in existing_cors:
        os.environ["CORS_ORIGINS"] = ",".join(filter(None, [existing_cors, origin]))

    # frozen 态把 .env 指向用户数据目录（不存在则 pydantic 不加载），避免误加载启动 cwd 的陌生 .env
    if getattr(sys, "frozen", False):
        from app import paths
        os.environ.setdefault("GONGGE_XUBAN_DOTENV", str(paths.user_data_dir() / ".env"))
        _ensure_frozen_public_mock_api_key()


def _ensure_frozen_public_mock_api_key() -> None:
    """为无配置文件的桌面包生成进程级 mock key，同时尊重用户已有配置文件。"""

    if os.environ.get("PUBLIC_MOCK_API_KEY", "").strip():
        return
    dotenv_value = os.environ.get("GONGGE_XUBAN_DOTENV", "").strip()
    if dotenv_value and Path(dotenv_value).expanduser().exists():
        return
    # 桌面包默认只绑定 loopback；随机 key 仅用于满足本地 mock API 的认证契约，
    # 不落盘、不打印，也不会成为可复用的公开凭据。
    os.environ["PUBLIC_MOCK_API_KEY"] = f"desktop-{secrets.token_urlsafe(32)}"


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _env_int(suffix: str, default: int) -> int:
    raw = desktop_env_value(suffix, "")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"GONGGE_XUBAN_{suffix} 必须是整数，当前值：{raw!r}") from exc


def _port_candidates() -> list[int]:
    start = _env_int("PORT_RANGE_START", DEFAULT_PORT_RANGE_START)
    end = _env_int("PORT_RANGE_END", DEFAULT_PORT_RANGE_END)
    if start > end:
        start, end = end, start

    candidates = list(range(start, end + 1))
    explicit = desktop_env_value("PORT", "")
    if explicit:
        port = _env_int("PORT", DEFAULT_PORT_RANGE_START)
        candidates = [port] + [candidate for candidate in candidates if candidate != port]
    return candidates


def find_available_port(host: str) -> int:
    for port in _port_candidates():
        if not port_in_use(host, port):
            return port
    first, last = _port_candidates()[0], _port_candidates()[-1]
    raise RuntimeError(f"{APP_NAME} 可用端口耗尽：{first}-{last} 都已被占用")


def _resource_path(*parts: str) -> str | None:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass, *parts))

    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        candidates.append(executable.parent.joinpath(*parts))
        if sys.platform == "darwin" and len(executable.parents) >= 2:
            candidates.append(executable.parents[1] / "Resources" / Path(*parts))

    candidates.append(Path(__file__).resolve().parent.parent.joinpath(*parts))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _product_icon_png_path() -> str | None:
    return _resource_path(*PRODUCT_ICON_PNG)


def _health_payload_matches(payload: dict) -> bool:
    return payload.get("status") == "ok" and payload.get("product_id") == PRODUCT_SLUG


def _health_ok(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _health_payload_matches(payload)
    except Exception:
        return False


def _find_existing_app_url(host: str) -> str | None:
    for port in _port_candidates():
        if not port_in_use(host, port):
            continue
        url = f"http://{host}:{port}"
        if _health_ok(url):
            return url
    return None


def _wait_for_existing_app_url(host: str, attempts: int = 20, delay: float = 0.3) -> str | None:
    for _ in range(attempts):
        url = _find_existing_app_url(host)
        if url:
            return url
        time.sleep(delay)
    return None


def _focus_existing_browser_window() -> bool:
    """在 Windows 上置前已经打开共格页面的浏览器窗口，避免重复创建标签页。"""

    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError):
        return False

    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    found: list[wintypes.HWND] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if BROWSER_PAGE_TITLE in title.value:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(collect_window, 0)
    if not found:
        return False

    browser_hwnd = found[0]
    # 9=SW_RESTORE：已最小化时恢复，未最小化时保持窗口状态；随后请求前台激活。
    user32.ShowWindow(browser_hwnd, 9)
    user32.SetForegroundWindow(browser_hwnd)
    return True


def _open_windows_browser_once(target: str) -> bool:
    """Windows 壳需要唤起页面时，优先复用浏览器且保证本进程最多新开一次。"""

    global _WINDOWS_BROWSER_OPENED
    if _focus_existing_browser_window():
        return False
    with _WINDOWS_BROWSER_OPEN_LOCK:
        if _WINDOWS_BROWSER_OPENED:
            return False
        if _focus_existing_browser_window():
            return False
        _open_browser(target)
        _WINDOWS_BROWSER_OPENED = True
        return True


def _acquire_macos_instance_lock() -> bool:
    if not _use_macos_dock_app():
        return True

    try:
        import fcntl
    except Exception:
        return True

    global _MACOS_INSTANCE_LOCK_HANDLE
    lock_path = Path(tempfile.gettempdir()) / f"{APP_ID}.lock"
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    lock_file.seek(0)
    lock_file.write(str(os.getpid()))
    lock_file.truncate()
    lock_file.flush()
    _MACOS_INSTANCE_LOCK_HANDLE = lock_file
    return True


def _acquire_windows_instance_mutex() -> bool:
    """以 Windows 命名互斥锁保证同一用户会话只运行一个桌面服务实例。"""

    if not _use_windows_taskbar_app():
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    mutex_name = f"Local\\{APP_ID}.instance"
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False

    global _WINDOWS_INSTANCE_MUTEX_HANDLE
    _WINDOWS_INSTANCE_MUTEX_HANDLE = handle
    return True


def _open_browser_when_ready(url: str) -> None:
    for _ in range(120):
        if _health_ok(url):
            _open_browser(url + "/chat/")
            return
        time.sleep(0.5)


def _open_windows_browser_when_ready(url: str) -> None:
    """等待 Windows 冻结版服务健康后，以单次策略唤起或复用浏览器页面。"""

    for _ in range(120):
        if _health_ok(url):
            _open_windows_browser_once(url + "/chat/")
            return
        time.sleep(0.5)


def _open_browser(target: str) -> None:
    """打开浏览器页面；macOS Dock 的重复唤起仍保持系统默认的标签页行为。"""
    webbrowser.open(target)


def _four_char_code(value: str) -> int:
    result = 0
    for byte in value.encode("macroman"):
        result = (result << 8) | byte
    return result


def _use_macos_dock_app() -> bool:
    # 仅 macOS 打包态用 Cocoa 壳（进 Dock + 点图标开页面）。
    # 开发态 / 其它平台保持简单主线程 uvicorn。
    return sys.platform == "darwin" and getattr(sys, "frozen", False)


def _use_windows_taskbar_app() -> bool:
    if headless_enabled():
        return False
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_windows_restore_command(message: int, wparam: int) -> bool:
    wm_syscommand = 0x0112
    sc_restore = 0xF120
    return message == wm_syscommand and (wparam & 0xFFF0) == sc_restore


def _serve(cfg: dict) -> None:
    import uvicorn

    if getattr(sys, "frozen", False):
        logging.getLogger("gongge_xuban.runtime").info(
            "Server starting host=%s port=%s",
            cfg["host"],
            cfg["port"],
        )
        uvicorn.run(
            cfg["app"],
            host=cfg["host"],
            port=cfg["port"],
            log_level="info",
            log_config=None,
            access_log=False,
        )
        return
    uvicorn.run(cfg["app"], host=cfg["host"], port=cfg["port"], log_level="info")


def preload_server_app(cfg: dict) -> None:
    app_ref = cfg.get("app")
    if not isinstance(app_ref, str):
        return
    module_name, separator, attribute_name = app_ref.partition(":")
    if not separator or not module_name or not attribute_name:
        raise RuntimeError(f"Invalid ASGI application reference: {app_ref!r}")
    module = importlib.import_module(module_name)
    cfg["app"] = getattr(module, attribute_name)


def _run_macos_dock_app(cfg: dict, url: str) -> int:
    """macOS：NSApplication 主循环。进 Dock/菜单栏，点入口重新打开浏览器。"""
    import AppKit
    from PyObjCTools import AppHelper

    global _MACOS_DELEGATE_REF

    def load_app_icon(point_size: float | None = None):
        icon_path = _product_icon_png_path()
        if not icon_path:
            return None
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None and point_size is not None:
            image.setSize_((point_size, point_size))
        return image

    class AppDelegate(AppKit.NSObject):
        def applicationDidFinishLaunching_(self, _notification):  # noqa: N802
            self.dock_visible = True
            self.server_started = False
            self._install_url_scheme_handler()
            self._install_status_menu()
            self._start_server()
            print(f"{APP_NAME} 启动中，就绪后将打开：{url}/chat/")

        def handleGetURLEvent_withReplyEvent_(self, event, _reply_event):  # noqa: N802
            direct_object = event.descriptorForKeyword_(_four_char_code("----"))
            deep_link = direct_object.stringValue() if direct_object is not None else ""
            print(f"收到 {APP_NAME} URL Scheme 唤起：{deep_link or '<empty>'}")
            threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):  # noqa: N802
            # 点 Dock 图标（app 已在运行）→ 打开浏览器页面（新标签）
            _open_browser(url + "/chat/")
            return True

        def applicationShouldTerminate_(self, _app):  # noqa: N802
            return AppKit.NSTerminateNow

        def applicationDockMenu_(self, _sender):  # noqa: N802
            # 右键 Dock 图标时展示同一套控制入口。
            self.dock_context_menu, self.dock_context_dock_item = self._build_control_menu()
            return self.dock_context_menu

        def openProduct_(self, _sender):  # noqa: N802
            _open_browser(url + "/chat/")

        def restartProduct_(self, _sender):  # noqa: N802
            os.execv(sys.executable, [sys.executable] + sys.argv[1:])

        def toggleDockIcon_(self, _sender):  # noqa: N802
            app = AppKit.NSApplication.sharedApplication()
            if self.dock_visible:
                app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
                self.dock_visible = False
                if hasattr(self, "status_dock_item"):
                    self.status_dock_item.setTitle_("显示 Dock 图标")
            else:
                app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
                app.activateIgnoringOtherApps_(True)
                self.dock_visible = True
                if hasattr(self, "status_dock_item"):
                    self.status_dock_item.setTitle_("隐藏 Dock 图标")

        def showAbout_(self, _sender):  # noqa: N802
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(APP_NAME)
            alert.setInformativeText_(f"版本：{APP_VERSION}\n本地服务：{url}")
            alert.addButtonWithTitle_("好")
            alert.runModal()

        def quitProduct_(self, _sender):  # noqa: N802
            AppKit.NSApplication.sharedApplication().terminate_(self)

        def _start_server(self) -> None:
            if self.server_started:
                return
            self.server_started = True
            # uvicorn 在后台线程跑（主线程要留给 Cocoa 事件循环）。这里必须等
            # NSApplication 完成注册后再启动，避免 LaunchServices 初始化竞态导致 abort。
            threading.Thread(target=_serve, args=(cfg,), daemon=True).start()
            threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

        def _install_url_scheme_handler(self) -> None:
            manager = AppKit.NSAppleEventManager.sharedAppleEventManager()
            manager.setEventHandler_andSelector_forEventClass_andEventID_(
                self,
                "handleGetURLEvent:withReplyEvent:",
                _four_char_code("GURL"),
                _four_char_code("GURL"),
            )

        def _menu_item(self, title: str, action: str | None = None, enabled: bool = True):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            item.setEnabled_(enabled)
            if action:
                item.setTarget_(self)
                item.setAction_(action)
            return item

        def _dock_toggle_title(self) -> str:
            return "隐藏 Dock 图标" if self.dock_visible else "显示 Dock 图标"

        def _build_control_menu(self):
            menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)
            menu.addItem_(self._menu_item("状态：运行中", enabled=False))
            menu.addItem_(self._menu_item(f"版本：{APP_VERSION}", enabled=False))
            menu.addItem_(self._menu_item(f"端口：{cfg['port']}", enabled=False))
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._menu_item(f"打开 {APP_NAME}", "openProduct:"))
            menu.addItem_(self._menu_item("重启服务", "restartProduct:"))
            dock_item = self._menu_item(self._dock_toggle_title(), "toggleDockIcon:")
            menu.addItem_(dock_item)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._menu_item(f"关于 {APP_NAME}", "showAbout:"))
            menu.addItem_(self._menu_item(f"退出 {APP_NAME}", "quitProduct:"))
            return menu, dock_item

        def _install_status_menu(self) -> None:
            self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSSquareStatusItemLength
            )
            button = self.status_item.button()
            if button is not None:
                status_icon = load_app_icon(18)
                if status_icon is not None:
                    status_icon.setTemplate_(False)
                    button.setImage_(status_icon)
                    button.setImagePosition_(AppKit.NSImageOnly)
                else:
                    button.setTitle_(APP_NAME)
                button.setToolTip_(APP_NAME)

            menu, self.status_dock_item = self._build_control_menu()
            self.status_item.setMenu_(menu)
            self.status_menu = menu

    app = AppKit.NSApplication.sharedApplication()
    # Regular：常规 GUI app，进 Dock、可激活
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    dock_icon = load_app_icon()
    if dock_icon is not None:
        app.setApplicationIconImage_(dock_icon)
    delegate = AppDelegate.alloc().init()
    # PyObjC 不总是按 Python 预期保留 delegate，模块级引用保证菜单和事件代理常驻。
    _MACOS_DELEGATE_REF = delegate
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
    return 0


def _run_windows_taskbar_app(cfg: dict, url: str) -> int:
    """Run the server behind a native window so the product owns a taskbar icon."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    WM_DESTROY = 0x0002
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_EX_APPWINDOW = 0x00040000
    SW_SHOWMINIMIZED = 2
    SW_SHOWMINNOACTIVE = 7
    CW_USEDEFAULT = -2147483648
    COLOR_WINDOW = 5

    WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LPARAM,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell32.ExtractIconExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HICON),
        ctypes.POINTER(wintypes.HICON),
        wintypes.UINT,
    ]
    shell32.ExtractIconExW.restype = wintypes.UINT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL

    shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    large_icon = wintypes.HICON()
    small_icon = wintypes.HICON()
    shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large_icon), ctypes.byref(small_icon), 1)

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if _is_windows_restore_command(message, wparam):
            print(f"Taskbar or Alt+Tab activated; focusing {APP_NAME} in the system default browser.")
            _open_windows_browser_once(url + "/chat/")
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = "GonggeXubanDesktopWindow"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.hIcon = large_icon
    window_class.hCursor = user32.LoadCursorW(None, 32512)
    window_class.hbrBackground = COLOR_WINDOW + 1
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        error = ctypes.get_last_error()
        if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise ctypes.WinError(error)

    hwnd = user32.CreateWindowExW(
        WS_EX_APPWINDOW,
        class_name,
        APP_NAME,
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        430,
        190,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    if large_icon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, ctypes.cast(large_icon, ctypes.c_void_p).value)
    if small_icon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, ctypes.cast(small_icon, ctypes.c_void_p).value)

    print(
        f"Windows shell ready: hwnd={hwnd}, "
        f"large_icon={ctypes.cast(large_icon, ctypes.c_void_p).value or 0}, "
        f"small_icon={ctypes.cast(small_icon, ctypes.c_void_p).value or 0}"
    )

    threading.Thread(target=_serve, args=(cfg,), daemon=True).start()
    threading.Thread(target=_open_windows_browser_when_ready, args=(url,), daemon=True).start()
    user32.ShowWindow(hwnd, SW_SHOWMINIMIZED)
    user32.UpdateWindow(hwnd)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))

    if large_icon:
        user32.DestroyIcon(large_icon)
    if small_icon:
        user32.DestroyIcon(small_icon)
    return 0


def main(argv: list[str] | None = None) -> int:
    """按CLI模式运行受限parser或启动桌面单端口服务。"""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--input-parser":
        os.environ.setdefault("APP_ENV", "attachment-parser")
        os.environ.setdefault("PUBLIC_MOCK_API_KEY", "attachment-parser-disabled")
        os.environ.setdefault("PUBLIC_MOCK_LLM_ENABLED", "false")
        from app.session.input_parser_cli import main as parser_main

        return parser_main(arguments[1:])
    _redirect_logs_when_frozen()

    host = desktop_env_value("HOST", "127.0.0.1")
    if _use_windows_taskbar_app() and not _acquire_windows_instance_mutex():
        existing_url = _wait_for_existing_app_url(host)
        if existing_url:
            print(f"{APP_NAME} 正在运行：{existing_url}")
            _open_windows_browser_once(existing_url + "/chat/")
        else:
            print(f"{APP_NAME} 已有实例正在启动，当前实例退出。")
        return 0

    existing_url = _find_existing_app_url(host)
    if existing_url:
        print(f"{APP_NAME} 已在运行：{existing_url}/chat/")
        if _use_windows_taskbar_app():
            _open_windows_browser_once(existing_url + "/chat/")
        else:
            _open_browser(existing_url + "/chat/")
        return 0

    if _use_macos_dock_app() and not _acquire_macos_instance_lock():
        existing_url = _wait_for_existing_app_url(host)
        if existing_url:
            print(f"{APP_NAME} 正在运行：{existing_url}/chat/")
            _open_browser(existing_url + "/chat/")
        else:
            print(f"{APP_NAME} 已有实例正在启动，当前实例退出。")
        return 0

    # 时序：先选定端口并设 env，再 import uvicorn / 触发 app.* import。
    cfg = build_server_config()
    apply_runtime_env(cfg)
    url = f"http://{cfg['host']}:{cfg['port']}"
    preload_server_app(cfg)

    if _use_macos_dock_app():
        return _run_macos_dock_app(cfg, url)

    if _use_windows_taskbar_app():
        return _run_windows_taskbar_app(cfg, url)

    if not headless_enabled():
        print(f"{APP_NAME} 启动中，就绪后将打开：{url}/chat/")
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    else:
        print(f"{APP_NAME} headless 启动中：{url}")
    _serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
