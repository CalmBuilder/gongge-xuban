"""
@Time: 2026-08-10
@Author: zhanglp8181
@File: test_desktop_launcher.py
@CallChain: pytest -> desktop_launcher -> single-port desktop server configuration
@Description: 验证桌面启动器的端口、品牌、健康检查和平台集成行为。
"""

import desktop_launcher


def _clear_port_env(monkeypatch) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_PORT", raising=False)
    monkeypatch.delenv("GONGGE_XUBAN_PORT_RANGE_START", raising=False)
    monkeypatch.delenv("GONGGE_XUBAN_PORT_RANGE_END", raising=False)


def test_build_server_config_defaults(monkeypatch) -> None:
    """未配置桌面端环境变量时应使用统一应用的 5137 默认端口。"""
    monkeypatch.delenv("GONGGE_XUBAN_HOST", raising=False)
    _clear_port_env(monkeypatch)
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: False)
    cfg = desktop_launcher.build_server_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 5137
    assert cfg["app"] == "single_port_app:app"


def test_build_server_config_env_override(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("GONGGE_XUBAN_PORT", "6000")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: False)
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 6000


def test_build_server_config_ignores_unrecognized_port_env(monkeypatch) -> None:
    """非产品命名空间的端口变量不得改变桌面端默认端口。"""
    _clear_port_env(monkeypatch)
    foreign_prefix = "".join(("ULTRA", "RAG"))
    monkeypatch.setenv(f"{foreign_prefix}_PORT", "6000")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: False)

    assert desktop_launcher.build_server_config()["port"] == 5137


def test_build_server_config_uses_next_port_in_range(monkeypatch) -> None:
    """默认端口被占用时应选择端口范围中的下一个可用端口。"""
    _clear_port_env(monkeypatch)
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, port: port == 5137)
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 5138


def test_build_server_config_honors_custom_port_range(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("GONGGE_XUBAN_PORT_RANGE_START", "6200")
    monkeypatch.setenv("GONGGE_XUBAN_PORT_RANGE_END", "6202")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, port: port in {6200, 6201})
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 6202


def test_explicit_port_is_tried_before_range(monkeypatch) -> None:
    """显式端口占用时应回退到用户配置范围内的首个可用端口。"""
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("GONGGE_XUBAN_PORT", "7000")
    monkeypatch.setenv("GONGGE_XUBAN_PORT_RANGE_START", "5137")
    monkeypatch.setenv("GONGGE_XUBAN_PORT_RANGE_END", "5138")
    checked_ports = []

    def fake_port_in_use(_host, port):
        checked_ports.append(port)
        return port == 7000

    monkeypatch.setattr(desktop_launcher, "port_in_use", fake_port_in_use)
    cfg = desktop_launcher.build_server_config()
    assert checked_ports == [7000, 5137]
    assert cfg["port"] == 5137


def test_port_in_use_false_for_unused_port() -> None:
    assert desktop_launcher.port_in_use("127.0.0.1", 59999) is False


def test_desktop_identity_uses_new_brand() -> None:
    assert desktop_launcher.APP_NAME == "共格·序伴"
    assert desktop_launcher.APP_ID == "cn.gongge.xuban.desktop"


def test_health_accepts_new_product_marker(monkeypatch) -> None:
    """健康检查应接受 5137 端口返回的当前产品标识。"""
    class FakeResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(url, timeout):
        assert url == "http://127.0.0.1:5137/api/health"
        assert timeout == 1
        return FakeResponse(
            '{"status":"ok","product_id":"gongge-xuban","app":"共格·序伴"}'.encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert desktop_launcher._health_ok("http://127.0.0.1:5137") is True


def test_health_rejects_unrecognized_product_marker(monkeypatch) -> None:
    """健康检查应拒绝缺少当前产品标识的本地服务。"""
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            foreign_name = "".join(("Staff", "Deck"))
            return f'{{"status":"ok","app":"{foreign_name}"}}'.encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert desktop_launcher._health_ok("http://127.0.0.1:5137") is False


def test_health_rejects_other_local_service(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert desktop_launcher._health_ok("http://127.0.0.1:5175") is False


def test_preload_server_app_imports_reference_on_calling_thread(monkeypatch) -> None:
    app = object()

    class FakeModule:
        pass

    module = FakeModule()
    module.app = app
    monkeypatch.setattr(desktop_launcher.importlib, "import_module", lambda name: module)
    cfg = {"app": "single_port_app:app"}

    desktop_launcher.preload_server_app(cfg)

    assert cfg["app"] is app


def test_windows_taskbar_app_only_used_for_frozen_windows(monkeypatch) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_HEADLESS", raising=False)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.delattr(desktop_launcher.sys, "frozen", raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is False

    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is True


def test_windows_taskbar_app_disabled_in_headless_mode(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setenv("GONGGE_XUBAN_HEADLESS", "1")
    assert desktop_launcher._use_windows_taskbar_app() is False


def test_windows_taskbar_app_ignores_unrecognized_headless_variable(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.delenv("GONGGE_XUBAN_HEADLESS", raising=False)
    foreign_prefix = "".join(("STAFF", "DECK"))
    monkeypatch.setenv(f"{foreign_prefix}_HEADLESS", "1")

    assert desktop_launcher._use_windows_taskbar_app() is True


def test_windows_restore_command_detection() -> None:
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF120) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF122) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF020) is False
    assert desktop_launcher._is_windows_restore_command(0x0002, 0xF120) is False


def test_frozen_server_disables_api_access_logging(monkeypatch) -> None:
    """冻结版桌面服务应在统一端口启动且关闭访问日志。"""
    import uvicorn

    calls = []
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    desktop_launcher._serve({"app": "single_port_app:app", "host": "127.0.0.1", "port": 5137})

    assert calls[0][1]["access_log"] is False
    assert calls[0][1]["log_config"] is None
