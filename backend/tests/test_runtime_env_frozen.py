from app import paths
from app.general_skills import runtime_env


def test_bundled_python_used_when_frozen(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    bundled = tmp_path / "runtime" / "bin" / "python"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(runtime_env, "_bundled_python", lambda: bundled)
    resolved = runtime_env._resolve_runtime_python("", "")
    assert resolved == bundled


def test_network_install_disabled_skips_pip(monkeypatch, tmp_path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text("")
    called = {"pip": False}
    monkeypatch.setattr(runtime_env, "_ensure_packages", lambda *a, **k: called.__setitem__("pip", True))
    monkeypatch.setattr(runtime_env, "_resolve_runtime_python", lambda *a: fake_python)

    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("GENERAL_SKILL_NETWORK_INSTALL", "false")
    try:
        runtime_env.ensure_runtime_python()
    finally:
        get_settings.cache_clear()
    assert called["pip"] is False


def test_network_install_enabled_triggers_pip(monkeypatch, tmp_path) -> None:
    fake_python = tmp_path / "python3"
    fake_python.write_text("")
    called = {"pip": False}
    monkeypatch.setattr(runtime_env, "_ensure_packages", lambda *a, **k: called.__setitem__("pip", True))
    monkeypatch.setattr(runtime_env, "_resolve_runtime_python", lambda *a: fake_python)

    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("GENERAL_SKILL_NETWORK_INSTALL", "true")
    monkeypatch.setenv("GENERAL_SKILL_RUNTIME_AUTO_INSTALL", "true")
    try:
        runtime_env.ensure_runtime_python()
    finally:
        get_settings.cache_clear()
    assert called["pip"] is True
