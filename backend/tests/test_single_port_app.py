"""
@Time       : 2026/07/28 10:10
@Author     : zhanglp8181
@File       : test_single_port_app.py
@CallChain  : pytest → single_port_app.FrontendStaticFiles → Starlette FileResponse
@Description: 验证单端口前端资源路由、跨平台 MIME 修正及诊断日志隐私边界。
"""

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import single_port_app
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


def test_unrecognized_icon_route_is_not_registered() -> None:
    foreign_path = "/" + "".join(("staff", "deck")) + "-icon.png"
    registered_paths = {getattr(route, "path", None) for route in single_port_app.app.routes}

    assert foreign_path not in registered_paths


def test_javascript_assets_override_broken_windows_mime_mapping(tmp_path: Path) -> None:
    original_media_type = mimetypes.guess_type("bundle.js")[0]
    mimetypes.add_type("text/plain", ".js", strict=True)

    try:
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
        app = FastAPI()
        app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

        response = TestClient(app).head("/assets/bundle.js")

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    finally:
        mimetypes.add_type(original_media_type or "text/javascript", ".js", strict=True)


def test_valid_application_javascript_mapping_is_not_reported_as_correction(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """确认合法 JavaScript MIME 不会被误记为修正事件。"""

    def application_javascript_response(
        _self,
        _full_path,
        _stat_result,
        _scope,
        status_code=200,
    ) -> Response:
        """模拟 Starlette 已返回浏览器可接受的 JavaScript MIME。"""

        return Response(
            status_code=status_code,
            headers={"Content-Type": "application/javascript"},
        )

    monkeypatch.setattr(single_port_app.logger, "disabled", False)
    monkeypatch.setattr(single_port_app.logger, "propagate", True)
    monkeypatch.setattr(StaticFiles, "file_response", application_javascript_response)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "bundle.js").write_text("export const ready = true;", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    with caplog.at_level("INFO", logger="gongge_xuban.static"):
        response = TestClient(app).head("/assets/bundle.js")

    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert "Corrected frontend MIME" not in caplog.text
    assert "Frontend module MIME" not in caplog.text


def test_mime_diagnostic_does_not_record_requested_asset_name(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """确认 MIME 修正日志只记录扩展名，不泄露请求资源名称。"""

    def broken_mime_response(
        _self,
        _full_path,
        _stat_result,
        _scope,
        status_code=200,
    ) -> Response:
        """模拟 Starlette 或操作系统返回错误的纯文本 MIME。"""

        return Response(
            status_code=status_code,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    monkeypatch.setattr(single_port_app.logger, "disabled", False)
    monkeypatch.setattr(single_port_app.logger, "propagate", True)
    monkeypatch.setattr(StaticFiles, "file_response", broken_mime_response)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    sensitive_name = "customer-secret-name.js"
    (asset_dir / sensitive_name).write_text("export {};", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", single_port_app.FrontendStaticFiles(directory=asset_dir))

    with caplog.at_level("WARNING", logger="gongge_xuban.static"):
        response = TestClient(app).head(f"/assets/{sensitive_name}")

    assert response.status_code == 200
    assert "Corrected frontend MIME suffix=.js" in caplog.text
    assert sensitive_name not in caplog.text
